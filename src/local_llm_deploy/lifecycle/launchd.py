from __future__ import annotations

import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import replace

from .types import LifecycleError, ServiceSpec


def plist_path(label: str, *, home=None) -> Path:
    return Path(home or Path.home()) / "Library/LaunchAgents" / f"{label}.plist"


def plist_data(spec: ServiceSpec) -> dict:
    if not spec.label:
        raise LifecycleError("launchd 服务缺少 label")
    env = dict(spec.env)
    from .runner import encode_spec, PAYLOAD_ENV
    spec = replace(spec, launch_token=spec.launch_token or uuid.uuid4().hex)
    env[PAYLOAD_ENV] = encode_spec(spec)
    # Descriptor values allow read-only discovery after login even if no PID
    # record survived. Process identity still comes from the loaded job itself.
    env.update(LOCAL_LLM_INSTANCE=spec.key, LOCAL_LLM_HOST=spec.host, LOCAL_LLM_PORT=str(spec.port),
               LOCAL_LLM_ALIAS=spec.alias, LOCAL_LLM_READY_PATH=spec.ready_path)
    env.setdefault("LOCAL_LLM_ROOT", str(spec.cwd))
    if spec.model_path:
        env["LOCAL_LLM_MODEL_DIR"] = str(spec.model_path)
    env["LOCAL_LLM_AUTH_SOURCE"] = "inline" if spec.api_key else ("file" if spec.api_key_file else "none")
    if spec.api_key_file:
        env["LOCAL_LLM_API_KEY_FILE"] = str(spec.api_key_file)
    entry = Path(__file__).resolve().parents[3] / "llm.py"
    argv = [sys.executable, str(entry), "__service-runner"] if entry.is_file() else [sys.executable, "-m", "local_llm_deploy.lifecycle.runner"]
    return {"Label": spec.label, "ProgramArguments": argv, "WorkingDirectory": str(spec.cwd),
            "EnvironmentVariables": env, "StandardOutPath": str(spec.log_path),
            "StandardErrorPath": str(spec.log_path), "RunAtLoad": spec.run_at_load, "KeepAlive": spec.keep_alive}


def require_macos() -> None:
    if sys.platform != "darwin":
        raise LifecycleError("launchd 仅适用于 macOS；请指定 --management process 或使用 nohup/foreground")


def _run(*args, check=True):
    require_macos()
    result = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=15)
    if check and result.returncode:
        raise LifecycleError(f"launchctl {' '.join(args)} 失败: {result.stderr.strip()}")
    return result


def target(label):
    return f"gui/{os.getuid()}/{label}"


def loaded(label):
    return _run("print", target(label), check=False).returncode == 0


def install(spec: ServiceSpec, *, home=None) -> Path:
    require_macos()
    path = plist_path(spec.label, home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    spec.log_path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        owner_root = Path(spec.env.get("LOCAL_LLM_ROOT", str(spec.cwd)))
        if not owned_plist(spec.label, owner_root, home=home):
            raise LifecycleError(f"LaunchAgent {spec.label} 属于其他项目，拒绝覆盖")
        shutil.copy2(path, path.with_suffix(".plist.bak"))
        path.with_suffix(".plist.bak").chmod(0o600)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            plistlib.dump(plist_data(spec), stream)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def start(spec: ServiceSpec, *, home=None) -> None:
    if loaded(spec.label):
        raise LifecycleError(f"LaunchAgent {spec.label} 已加载；请先 stop，再 start")
    path = install(spec, home=home)
    _run("bootstrap", f"gui/{os.getuid()}", str(path))
    try:
        # RunAtLoad services can already be running; kickstart without -k does not replace them.
        _run("kickstart", target(spec.label))
    except Exception:
        _run("bootout", target(spec.label), check=False)
        raise


def stop(label: str) -> None:
    result = _run("bootout", target(label), check=False)
    deadline = time.monotonic() + 10
    while loaded(label):
        if result.returncode:
            raise LifecycleError(f"无法卸载 LaunchAgent {label}: {result.stderr.strip()}")
        if time.monotonic() >= deadline:
            raise LifecycleError(f"LaunchAgent {label} 未在停止期限内退出")
        time.sleep(0.05)


def uninstall(label: str, *, root: Path, home=None) -> None:
    path = plist_path(label, home=home)
    if loaded(label):
        raise LifecycleError(f"{label} 仍已加载；请先 stop 再 daemon uninstall")
    if path.exists() and not owned_plist(label, root, home=home):
        raise LifecycleError(f"LaunchAgent {label} 属于其他项目，拒绝删除")
    path.unlink(missing_ok=True)


def owned_plist(label: str, root: Path, *, home=None) -> bool:
    """Only permit stopping a dormant legacy job belonging to this checkout."""
    try:
        data = plistlib.loads(plist_path(label, home=home).read_bytes())
        args = data.get("ProgramArguments", [])
        configured_root = data.get("EnvironmentVariables", {}).get("LOCAL_LLM_ROOT")
        if data.get("Label") != label:
            return False
        if configured_root is not None:
            return configured_root == str(root)
        return any(str(root) + os.sep in str(arg) for arg in args)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False


def owns_launch(spec: ServiceSpec) -> bool:
    """Do not clean up a replacement launch installed after this operation began."""
    from .runner import decode_spec, PAYLOAD_ENV
    try:
        data = plistlib.loads(plist_path(spec.label).read_bytes())
        configured = decode_spec(data["EnvironmentVariables"][PAYLOAD_ENV])
        return configured.launch_token == spec.launch_token and configured.cwd == spec.cwd
    except (OSError, ValueError, KeyError, TypeError, plistlib.InvalidFileException):
        return False


def configured_jobs(root: Path, *, home=None) -> list[dict]:
    """Owned launch configurations only; never load, rewrite or remove a job."""
    directory = Path(home or Path.home()) / "Library/LaunchAgents"
    result = []
    for path in directory.glob("com.local-llm-deploy.*.plist"):
        try:
            data = plistlib.loads(path.read_bytes())
            label = data.get("Label")
            env = data.get("EnvironmentVariables", {})
            key = env.get("LOCAL_LLM_INSTANCE")
            if not isinstance(label, str) or path.name != label + ".plist":
                continue
            if env.get("LOCAL_LLM_ROOT") != str(root):
                continue
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", key):
                continue
            argv = data.get("ProgramArguments")
            if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) for arg in argv):
                continue
            port = int(env["LOCAL_LLM_PORT"])
            if not 1 <= port <= 65535:
                continue
            result.append({"key": key, "label": label, "argv": argv, "port": port,
                           "host": env.get("LOCAL_LLM_HOST", "127.0.0.1"), "alias": env.get("LOCAL_LLM_ALIAS", key),
                           "ready_path": env.get("LOCAL_LLM_READY_PATH", "/health"),
                           "model_path": env.get("LOCAL_LLM_MODEL_DIR"),
                           "api_key_file": env.get("LOCAL_LLM_API_KEY_FILE"),
                           "auth_source": env.get("LOCAL_LLM_AUTH_SOURCE")})
        except (OSError, ValueError, TypeError, KeyError, plistlib.InvalidFileException):
            continue
    return result
