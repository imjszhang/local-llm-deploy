from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import replace
import fcntl
import os
import signal
import sys
import time
import uuid

from . import launchd, process
from .observe import health_ready, launchd_pid, observe_instances, process_identity, read_pid, same_identity, write_pid
from .types import LifecycleError, ServiceSpec


@contextmanager
def instance_lock(paths, key: str, *, wait=False):
    if not key or any(c in key for c in ("/", "\\", "\n", "\x00")) or key in (".", ".."):
        raise LifecycleError("无效实例标识")
    paths.run.mkdir(parents=True, exist_ok=True)
    with (paths.run / f".{key}.lock").open("a") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise LifecycleError(f"{key} 已有生命周期操作正在执行") from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _headers(spec):
    key = spec.api_key
    if not key and spec.api_key_file:
        lines = spec.api_key_file.read_text(encoding="utf-8").splitlines()
        key = lines[0].strip() if lines else ""
    return {"Authorization": f"Bearer {key}"} if key else {}


def _start_launchd(spec: ServiceSpec, paths, models):
    """Bootstrap once, then let the foreground runner own locks and publication."""
    spec = replace(spec, launch_token=uuid.uuid4().hex)
    with instance_lock(paths, spec.key):
        existing = observe_instances(paths, models).get(spec.key)
        if existing and existing.pid:
            raise LifecycleError(f"{spec.key} 已在运行（PID {existing.pid}）；请先 stop")
        process.check_port(spec.host, spec.port)
        launchd.start(spec)
    # Never wait here while holding the lock needed by the runner.
    try:
        deadline = time.monotonic() + spec.ready_timeout
        while time.monotonic() < deadline:
            record = read_pid(spec.pid_path)
            if record and record[3].get("launch_token") == spec.launch_token and record[3].get("ready"):
                meta = record[3]
                identity = process_identity(record[0])
                wrapper = process_identity(meta.get("wrapper_pid", 0))
                if (identity and wrapper and wrapper.pid == launchd_pid(spec.label) and
                        identity.started_at == meta.get("started_at") and identity.command_hash == meta.get("command_hash") and
                        wrapper.started_at == meta.get("wrapper_started_at") and wrapper.command_hash == meta.get("wrapper_command_hash")):
                    return identity
            time.sleep(0.1)
        raise LifecycleError(f"{spec.key} 启动超时，未发布 ready 实例；日志: {spec.log_path}")
    except BaseException:
        if launchd.owns_launch(spec):
            launchd.stop(spec.label)
        raise


def start_service(spec: ServiceSpec, paths, *, models=None):
    if spec.management not in ("launchd", "process"):
        raise LifecycleError(f"不支持的运行方式: {spec.management}")
    if spec.management == "launchd":
        return _start_launchd(spec, paths, models or {})
    from ..artifacts.paths import installation_lock, weights_complete
    artifact_guard = installation_lock(paths, spec.model_path) if spec.model_path else nullcontext()
    with artifact_guard, instance_lock(paths, spec.key, wait=bool(spec.supervisor_label)):
        model = (models or {}).get(spec.key)
        if model and model.management != "external" and spec.model_path and not weights_complete(model, spec.model_path, quant=spec.quant):
            raise LifecycleError(f"模型权重在启动前发生变化或不完整: {spec.model_path}")
        existing = observe_instances(paths, models or {}).get(spec.key)
        own_supervisor = bool(existing and existing.pid == os.getpid() and spec.foreground and
                              existing.management == "launchd" and existing.label and launchd_pid(existing.label) == os.getpid())
        if existing and existing.pid and not own_supervisor:
            raise LifecycleError(f"{spec.key} 已在运行（PID {existing.pid}）；请先 stop")
        process.check_port(spec.host, spec.port)
        child = None
        identity = None
        launched = False
        try:
            if spec.management == "launchd":
                launchd.start(spec)
            else:
                child = process.spawn(spec)
            launched = True
            deadline = time.monotonic() + spec.ready_timeout
            headers = _headers(spec)
            while time.monotonic() < deadline:
                pid = launchd_pid(spec.label) if spec.management == "launchd" else child.pid
                if child is not None and child.poll() is not None:
                    raise LifecycleError(f"{spec.key} 在就绪前退出（退出码 {child.returncode}）；日志: {spec.log_path}")
                identity = process_identity(pid) if pid else None
                if identity and health_ready(spec.host, spec.port, spec.ready_path, headers=headers) and process.owns_listener(identity.pid, spec.port):
                    if not same_identity(identity):
                        continue
                    metadata = {"ready": True, "management": "launchd" if spec.supervisor_label else spec.management,
                                        "label": spec.supervisor_label or spec.label,
                                        "host": spec.host, "ready_path": spec.ready_path,
                                        "model_path": str(spec.model_path) if spec.model_path else None,
                                        "quant": spec.quant, "engine_profile": spec.engine_profile,
                                        "engine_revision": spec.engine_revision, "launch_token": spec.launch_token,
                                        "api_key_file": str(spec.api_key_file) if spec.api_key_file else None,
                                        "auth_source": "inline" if spec.api_key else ("file" if spec.api_key_file else "none")}
                    if spec.foreground:
                        wrapper = process_identity(os.getpid())
                        if wrapper:
                            metadata.update(wrapper_pid=wrapper.pid, wrapper_started_at=wrapper.started_at,
                                            wrapper_command_hash=wrapper.command_hash)
                    write_pid(spec.pid_path, identity.pid, spec.port, spec.alias, identity=identity, metadata=metadata)
                    return identity
                time.sleep(0.1)
            raise LifecycleError(f"{spec.key} 启动超时，未发布 ready 实例；日志: {spec.log_path}")
        except BaseException:
            if launched:
                if spec.management == "launchd":
                    launchd.stop(spec.label)
                if identity:
                    process.terminate(identity, timeout=2)
                elif child is not None:
                    # Popen still owns this child PID; use its handle, not a stale record.
                    if child.poll() is None:
                        child.terminate()
                if child is not None:
                    try:
                        child.wait(timeout=3)
                    except Exception:
                        if child.poll() is None:
                            child.kill()
                        child.wait(timeout=3)
            raise


def stop_service(paths, key: str, *, models=None, label=None, timeout=10.0) -> bool:
    with instance_lock(paths, key):
        observation = observe_instances(paths, models or {}).get(key)
        identity = observation.identity if observation else None
        stopped = False
        candidate_label = observation.label if observation and observation.label else label
        metadata = (read_pid(paths.run / f"{key}.pid") or (None, None, None, {}))[3]
        # A process-mode instance can share its model key with another checkout's
        # launchd job. Only its actual supervisor may be inspected or stopped.
        observed_launchd = bool(observation and observation.management == "launchd")
        legacy_owned = bool(candidate_label and (not identity or metadata.get("wrapper_pid")) and
                            launchd.owned_plist(candidate_label, paths.root))
        if candidate_label and sys.platform == "darwin" and (observed_launchd or legacy_owned) and launchd.loaded(candidate_label):
            current = launchd_pid(candidate_label)
            wrapper = process_identity(current) if current and current == metadata.get("wrapper_pid") else None
            wrapper_owned = bool(wrapper and wrapper.started_at == metadata.get("wrapper_started_at") and
                                 wrapper.command_hash == metadata.get("wrapper_command_hash"))
            if ((identity and current == identity.pid) or
                    wrapper_owned or
                    (current is None and launchd.owned_plist(candidate_label, paths.root))):
                launchd.stop(candidate_label)
                stopped = True
            else:
                raise LifecycleError(f"无法确认 LaunchAgent {candidate_label} 的进程归属；拒绝停止")
        if identity:
            process.terminate(identity, timeout=timeout)
            if same_identity(identity):
                raise LifecycleError(f"{key} 未能停止；保留 PID 记录")
            stopped = True
        path = paths.run / f"{key}.pid"
        record = read_pid(path)
        same_job = bool(record and observation and observation.management == "launchd" and
                        observation.label == record[3].get("label") and identity and
                        identity.command_hash == record[3].get("command_hash"))
        if record and (not observation or not observation.pid or record[0] == observation.pid or same_job):
            path.unlink(missing_ok=True)
        elif path.exists() and record is None and observation and observation.status == "stale":
            path.unlink()
        # Legacy launcher cache is removed only during an explicit lifecycle mutation.
        cache = {"serve-ui": "serve-ui.pid", "ds4": "ds4.pid", "whisper-large-v3": "whisper.pid"}.get(key)
        if cache and stopped:
            (paths.logs / cache).unlink(missing_ok=True)
        return stopped


def foreground_service(spec: ServiceSpec, paths, *, models=None) -> int:
    """Keep a foreground parent to forward signals, reap the backend, and clean its record."""
    handlers = {}
    identity = None
    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)
    for signum in (signal.SIGTERM, signal.SIGINT):
        handlers[signum] = signal.signal(signum, interrupted)
    try:
        identity = start_service(replace(spec, management="process", foreground=True), paths, models=models)
        child = process.child_handle(identity.pid)
        return child.wait() if child else 1
    finally:
        # Restore handlers before cleanup so a caller's subsequent signal policy is unchanged.
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
        if identity:
            # Do not bootout a LaunchAgent that is currently running this foreground wrapper.
            if same_identity(identity):
                process.terminate(identity, timeout=3)
            record = read_pid(spec.pid_path)
            if record and record[0] == identity.pid:
                spec.pid_path.unlink(missing_ok=True)


def reconcile(paths, *, models=None):
    removed = []
    for key, observation in observe_instances(paths, models or {}).items():
        if observation.status != "stale":
            continue
        with instance_lock(paths, key):
            latest = observe_instances(paths, models or {}).get(key)
            if latest and latest.status == "stale":
                path = paths.run / f"{key}.pid"
                if path.exists():
                    path.unlink()
                    removed.append(key)
    return removed
