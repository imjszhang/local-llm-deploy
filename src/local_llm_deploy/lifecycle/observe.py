"""Observe legacy PID records without rewriting or deleting them."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

from .types import InstanceObservation, ProcessIdentity, LifecycleError


def process_identity(pid: int) -> ProcessIdentity | None:
    if pid <= 1:
        return None
    try:
        result = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "lstart=", "-o", "command="],
                                capture_output=True, text=True, timeout=3)
        line = result.stdout.strip()
        # ps lstart has five fields, followed by the command including its original spaces.
        parts = line.split(None, 5)
        if result.returncode or len(parts) != 6 or "<defunct>" in parts[5]:
            return None
        started, command = " ".join(parts[:5]), parts[5]
        return ProcessIdentity(pid, started, hashlib.sha256(command.encode()).hexdigest(), command)
    except (OSError, subprocess.SubprocessError):
        return None


def same_identity(expected: ProcessIdentity) -> bool:
    actual = process_identity(expected.pid)
    return bool(actual and actual.started_at == expected.started_at and actual.command_hash == expected.command_hash)


def read_pid(path: Path) -> tuple[int, int, str, dict] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        pid = int(lines[0])
        port = int(lines[1]) if len(lines) > 1 else 0
        alias = lines[2] if len(lines) > 2 else path.stem
        metadata = json.loads(lines[3]) if len(lines) > 3 else {}
        if not isinstance(metadata, dict) or pid <= 1 or not 0 <= port < 65536:
            return None
        return pid, port, alias, metadata
    except (OSError, ValueError, IndexError):
        return None


def write_pid(path: Path, pid: int, port: int, alias: str, *, identity=None, metadata=None) -> Path:
    """Atomic legacy-compatible record. The optional fourth line is ownership metadata."""
    identity = identity or process_identity(pid)
    details = dict(metadata or {})
    details["version"] = 1
    if identity:
        details.update(started_at=identity.started_at, command_hash=identity.command_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(f"{pid}\n{port}\n{alias}\n{json.dumps(details, ensure_ascii=False)}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return path


def publish_pid(paths, key: str, port: int, alias: str, *, model_path=None, api_key_file=None, auth_source=None) -> Path:
    previous = read_pid(paths.run / f"{key}.pid")
    if previous and previous[0] != os.getpid() and process_identity(previous[0]):
        raise LifecycleError(f"{key} 已有存活 PID {previous[0]}，拒绝覆盖实例记录")
    return write_pid(paths.run / f"{key}.pid", os.getpid(), port, alias,
                     metadata={"ready": True, "management": "process",
                               "model_path": str(Path(model_path).resolve()) if model_path is not None else None,
                               "api_key_file": str(Path(api_key_file).resolve()) if api_key_file is not None else None,
                               "auth_source": auth_source or ("file" if api_key_file else "none")})


def remove_pid(paths, key: str, *, expected_pid: int) -> None:
    path = paths.run / f"{key}.pid"
    record = read_pid(path)
    if record and record[0] == expected_pid:
        path.unlink(missing_ok=True)


def legacy_command_matches(identity: ProcessIdentity, paths, key: str, spec=None) -> bool:
    """Conservative adoption: known absolute entry path AND model identity are required."""
    command = identity.command
    scripts = {"transformers_embedding": "serve_embedding.py", "mlx_rerank": "serve_rerank.py",
               "mlx_whisper": "serve_whisper.py"}
    backend = getattr(spec, "backend", "")
    if key == "serve-ui":
        return str(paths.root / "serve-ui.py") in command
    if key == "ds4":
        binary = os.environ.get("DS4_BIN")
        if not binary and os.environ.get("DS4_ROOT"):
            binary = str(Path(os.environ["DS4_ROOT"]) / "ds4-server")
        if not binary:
            # The installed legacy job is an explicit record of this checkout's DS4 path.
            import plistlib
            from .launchd import owned_plist, plist_path
            label = "com.local-llm-deploy.ds4"
            if owned_plist(label, paths.root):
                try:
                    data = plistlib.loads(plist_path(label).read_bytes())
                    binary = data.get("EnvironmentVariables", {}).get("DS4_BIN")
                except (OSError, ValueError, plistlib.InvalidFileException):
                    pass
        return bool(binary and command.startswith(str(binary) + " ") and " --port " in command)
    script = scripts.get(backend)
    if script:
        return (str(paths.root / script) in command and
                re.search(r"(?:^|\s)--model-name\s+" + re.escape(key) + r"(?:\s|$)", command) is not None)
    if backend == "llama_cpp":
        raw = getattr(spec, "raw", {})
        runtime = raw.get("runtime", {})
        cpp = runtime.get("cpp_dir") or raw.get("cpp_dir") or str(paths.root / "llama.cpp")
        binary = str(Path(cpp).expanduser() / "build/bin/llama-server")
        actual_binary = command.split(" --model", 1)[0].strip()
        # Legacy deployments accepted CPP_DIR overrides, including work_dir engine builds.
        # Preserve them only for an absolute llama-server inside this checkout.
        owned_build = (actual_binary.startswith(str(paths.root) + os.sep) and
                       actual_binary.endswith("/build/bin/llama-server"))
        alias = getattr(spec, "alias", key)
        return ((binary in command or owned_build) and
                re.search(r"(?:^|\s)--alias\s+" + re.escape(alias) + r"(?:\s|$)", command) is not None)
    # Unknown run/*.pid entries are never adopted based on PID existence alone.
    return False


def health_ready(host: str, port: int, path: str = "/health", *, timeout: float = 1.0, headers=None) -> bool:
    connect_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    authority = f"[{connect_host}]" if ":" in connect_host else connect_host
    try:
        req = urllib.request.Request(f"http://{authority}:{port}{path}", headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                return False
            raw = response.read(65536)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return True
        return not (isinstance(body, dict) and
                    (body.get("status") in ("loading", "starting", "error", "unhealthy") or body.get("error")))
    except urllib.error.HTTPError as exc:
        exc.close()
        return False
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        return False


def launchd_pid(label: str) -> int | None:
    if sys.platform != "darwin":
        return None
    try:
        output = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                                capture_output=True, text=True, timeout=3)
        match = re.search(r"^\s*pid = (\d+)\s*$", output.stdout, re.M)
        return int(match.group(1)) if output.returncode == 0 and match else None
    except (OSError, subprocess.SubprocessError):
        return None


def launch_command_matches(identity: ProcessIdentity, argv) -> bool:
    expected = " ".join(argv)
    canonical = " ".join([str(Path(argv[0]).resolve()), *argv[1:]])
    if identity.command in (expected, canonical):
        return True
    # macOS framework Python re-execs Resources/Python.app/.../Python. Preserve
    # exact script/module and every argument, while accepting this interpreter rewrite.
    suffix = " " + " ".join(argv[1:])
    if len(argv) > 1 and Path(argv[0]).name.lower().startswith("python") and identity.command.endswith(suffix):
        executable = identity.command[:-len(suffix)]
        return Path(executable).is_absolute() and Path(executable).name.lower().startswith("python")
    return False


def observe_instances(paths, models=None, *, probe: bool = False) -> dict[str, InstanceObservation]:
    if models is None:
        from ..config import load_specs
        try:
            models = load_specs(paths.registry)
        except (OSError, ValueError):
            models = {}
    records = {p.stem: p for p in paths.run.glob("*.pid")} if paths.run.is_dir() else {}
    for key, name in (("serve-ui", "serve-ui.pid"), ("ds4", "ds4.pid"), ("whisper-large-v3", "whisper.pid")):
        if key not in records and (paths.logs / name).is_file():
            records[key] = paths.logs / name
    result = {}
    for key, path in records.items():
        record = read_pid(path)
        spec = models.get(key)
        default_port = getattr(spec, "port", {"serve-ui": 8888, "ds4": int(os.environ.get("DS4_PORT", "8005"))}.get(key, 0))
        if not record:
            result[key] = InstanceObservation(key, None, default_port, key, "stale", reason="invalid PID record")
            continue
        pid, port, alias, meta = record
        port = port or default_port
        management = meta.get("management", "process")
        label = meta.get("label")
        identity = process_identity(pid)
        valid = bool(identity and (identity.started_at == meta.get("started_at") and
                                  identity.command_hash == meta.get("command_hash"))) if meta.get("command_hash") else bool(identity and legacy_command_matches(identity, paths, key, spec))
        if valid and not meta.get("command_hash") and sys.platform == "darwin":
            from ..backends.builders import launchd_settings
            legacy_label = launchd_settings(key, getattr(spec, "backend", ""))[0]
            if launchd_pid(legacy_label) == pid:
                management, label = "launchd", legacy_label
        restarted = False
        if management == "launchd" and label:
            current = launchd_pid(label)
            if current and current != pid:
                replacement = process_identity(current)
                # launchd is an ownership anchor, but the command must still match the stored hash.
                if replacement and replacement.command_hash == meta.get("command_hash"):
                    pid, identity, valid, restarted = current, replacement, True, True
        host = meta.get("host", getattr(spec, "host", "127.0.0.1"))
        healthy = health_ready(host, port, meta.get("ready_path", "/api/models" if key == "serve-ui" else "/health")) if valid and probe else None
        ready = healthy if probe else bool(meta.get("ready") and not restarted)
        status = ("ready" if ready else "running") if valid else "stale"
        result[key] = InstanceObservation(key, pid if valid else None, port, alias, status,
                                          management, healthy, host, identity if valid else None, label,
                                          "" if valid else "PID absent or process identity does not match", record[0], meta.get("model_path"),
                                          meta.get("api_key_file"), meta.get("auth_source"))
    if sys.platform == "darwin":
        from .launchd import configured_jobs
        from .process import owns_listener
        for job in configured_jobs(paths.root):
            key = job["key"]
            if key in result and result[key].pid:
                continue
            pid = launchd_pid(job["label"])
            identity = process_identity(pid) if pid else None
            if not identity:
                continue
            # Match every argument from the owned plist. Do not infer ownership
            # from an arbitrary listener on the configured port.
            if not launch_command_matches(identity, job["argv"]):
                continue
            listening = owns_listener(pid, job["port"])
            healthy = health_ready(job["host"], job["port"], job["ready_path"]) if probe and listening else None
            status = "ready" if healthy else ("running" if listening else "starting")
            result[key] = InstanceObservation(key, pid, job["port"], job["alias"], status, "launchd",
                                              healthy, job["host"], identity, job["label"],
                                              "observed from owned LaunchAgent", pid, job.get("model_path"),
                                              job.get("api_key_file"), job.get("auth_source"))
    return result
