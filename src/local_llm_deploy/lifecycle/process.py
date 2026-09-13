from __future__ import annotations

import os
from pathlib import Path
import signal
import socket
import subprocess
import shutil
import time

from .observe import same_identity
from .types import LifecycleError, ProcessIdentity, ServiceSpec


_children = {}


def check_port(host: str, port: int) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            # Match restartable HTTP listeners: old accepted connections in
            # TIME_WAIT are not an active service occupying the endpoint.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
    except OSError as exc:
        raise LifecycleError(f"端口 {host}:{port} 无法绑定（可能已占用）；未终止任何进程: {exc}") from exc


def spawn(spec: ServiceSpec) -> subprocess.Popen:
    spec.log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.pop("LOCAL_LLM_SERVICE_SPEC", None)
    env.update(spec.env)
    if spec.foreground:
        child = subprocess.Popen(spec.argv, cwd=spec.cwd, env=env, start_new_session=not bool(spec.supervisor_label))
    else:
        with spec.log_path.open("ab") as log:
            child = subprocess.Popen(spec.argv, cwd=spec.cwd, env=env, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    _children[child.pid] = child
    return child


def child_handle(pid):
    return _children.get(pid)


def owns_listener(pid: int, port: int) -> bool:
    """A healthy response from a racing, unrelated listener must not publish this instance."""
    lsof = shutil.which("lsof")
    if lsof:
        result = subprocess.run([lsof, "-nP", "-a", "-p", str(pid), f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
                                capture_output=True, text=True, timeout=3)
        return result.returncode == 0 and f"p{pid}" in result.stdout.splitlines()
    # Linux CI often omits lsof. Match listening socket inodes to this process's descriptors.
    proc = Path("/proc") / str(pid)
    try:
        sockets = {link.readlink().as_posix()[8:-1] for link in (proc / "fd").iterdir()
                   if link.readlink().as_posix().startswith("socket:[")}
        for name in ("tcp", "tcp6"):
            for line in (proc / "net" / name).read_text().splitlines()[1:]:
                columns = line.split()
                if columns[3] == "0A" and int(columns[1].split(":")[-1], 16) == port and columns[9] in sockets:
                    return True
    except (OSError, IndexError, ValueError):
        return False
    return False


def terminate(identity: ProcessIdentity, *, timeout: float = 10.0) -> None:
    # PID reuse can happen during waits: check the entire identity before every signal.
    child = _children.get(identity.pid)
    if child and child.poll() is not None:
        _children.pop(identity.pid, None)
        return
    if not same_identity(identity):
        return
    try:
        os.kill(identity.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child and child.poll() is not None:
            _children.pop(identity.pid, None)
            return
        if not same_identity(identity):
            return
        time.sleep(0.05)
    if same_identity(identity):
        try:
            os.kill(identity.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if child:
        child.wait(timeout=3)
        _children.pop(identity.pid, None)
