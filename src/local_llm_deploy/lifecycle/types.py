from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class LifecycleError(RuntimeError):
    """An actionable lifecycle failure; never means an unrelated PID was killed."""


@dataclass(frozen=True)
class ServiceSpec:
    key: str
    alias: str
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    host: str
    port: int
    log_path: Path
    pid_path: Path
    management: str = "process"
    ready_path: str = "/health"
    ready_timeout: float = 180.0
    label: str | None = None
    run_at_load: bool = False
    keep_alive: Any = False
    model_path: Path | None = None
    api_key_file: Path | None = None
    api_key: str | None = field(default=None, repr=False)
    foreground: bool = False
    quant: str | None = None
    engine_profile: str | None = None
    engine_revision: str | None = None
    supervisor_label: str | None = None
    launch_token: str | None = None

    def as_dict(self) -> dict[str, Any]:
        argv = list(self.argv)
        for i, arg in enumerate(argv[:-1]):
            if arg == "--api-key":
                argv[i + 1] = "<redacted>"
        env = {k: ("<redacted>" if any(x in k.upper() for x in ("KEY", "TOKEN", "SECRET", "PASSWORD")) else v)
               for k, v in self.env.items()}
        return {"key": self.key, "alias": self.alias, "argv": argv, "cwd": str(self.cwd),
                "env": env, "host": self.host, "port": self.port, "log_path": str(self.log_path),
                "pid_path": str(self.pid_path), "management": self.management,
                "ready_path": self.ready_path, "ready_timeout": self.ready_timeout,
                "label": self.label, "run_at_load": self.run_at_load, "keep_alive": self.keep_alive,
                "model_path": str(self.model_path) if self.model_path else None, "quant": self.quant,
                "engine_profile": self.engine_profile, "engine_revision": self.engine_revision}


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    started_at: str
    command_hash: str
    command: str = field(repr=False)


@dataclass(frozen=True)
class InstanceObservation:
    key: str
    pid: int | None
    port: int
    alias: str
    status: str
    management: str = "process"
    healthy: bool | None = None
    host: str = "127.0.0.1"
    identity: ProcessIdentity | None = field(default=None, repr=False)
    label: str | None = None
    reason: str = ""
    observed_pid: int | None = None
    model_path: str | None = None
    api_key_file: str | None = None
    auth_source: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "pid": self.pid, "port": self.port, "alias": self.alias,
                "status": self.status, "management": self.management, "healthy": self.healthy,
                "host": self.host, "label": self.label, "reason": self.reason, "observed_pid": self.observed_pid,
                "model_path": self.model_path, "api_key_file": self.api_key_file, "auth_source": self.auth_source}
