"""Shared CLI/Web control plane for the independent MCP Hub process."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
import re
import sys
import time

from ..backends.builders import build_mcp_hub
from ..lifecycle import launchd
from ..lifecycle.manager import foreground_service, start_service, stop_service
from ..lifecycle.observe import health_ready, observe_instances
from ..lifecycle.types import LifecycleError
from .config import HubConfigError, HubConfigStore, default_config


HUB_KEY = "mcp-hub"
HUB_LABEL = "com.local-llm-deploy.mcp-hub"


class HubController:
    def __init__(self, paths):
        self.paths = paths
        self.store = HubConfigStore(paths)

    @contextmanager
    def operation(self):
        """Serialize whole operations across Web/CLI, including both restart halves.

        This is deliberately distinct from the lifecycle instance lock: a launchd
        parent waits for its runner, and the runner must still acquire that lock.
        """
        self.paths.run.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.paths.run / ".mcp-hub.control.lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a+") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LifecycleError("MCP Hub 已有管理操作正在执行，请稍后重试") from None
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def plan(self, options=None):
        return build_mcp_hub(self.paths, options, validate=False).as_dict()

    def status(self):
        error = None
        try:
            config = self.store.load()
        except (HubConfigError, OSError, UnicodeError):
            config, error = default_config(), "Hub 配置不可用，请检查并修复配置"
        listen = config["listen"]
        row = {"key": HUB_KEY, "alias": "MCP Hub", "status": "stopped", "pid": None,
               "host": listen["host"], "port": listen["port"], "healthy": None,
               "management": "launchd" if sys.platform == "darwin" else "process",
               "config_revision": config["revision"], "config_error": error}
        observation = observe_instances(self.paths, {}, probe=False).get(HUB_KEY)
        if observation is not None:
            for key in ("status", "pid", "host", "port", "management"):
                row[key] = getattr(observation, key)
            if observation.pid and observation.status != "starting":
                row["healthy"] = health_ready(row["host"], row["port"], "/health", timeout=.5)
                row["status"] = "ready" if row["healthy"] else "unhealthy"
        host = "127.0.0.1" if row["host"] == "0.0.0.0" else "::1" if row["host"] == "::" else row["host"]
        authority = f"[{host}]" if ":" in host else host
        row["endpoint"] = listen["public_url"].rstrip("/") or f"http://{authority}:{row['port']}"
        return row

    def start(self, options=None):
        with self.operation():
            start_service(build_mcp_hub(self.paths, options), self.paths, models={})
        return self.status()

    def stop(self, *, timeout=10.0):
        with self.operation():
            stop_service(self.paths, HUB_KEY, models={}, label=HUB_LABEL, timeout=timeout)
        return self.status()

    def restart(self, options=None):
        # Build/validate before stopping so a bad edit cannot take a healthy Hub down.
        with self.operation():
            spec = build_mcp_hub(self.paths, options)
            stop_service(self.paths, HUB_KEY, models={}, label=HUB_LABEL)
            start_service(spec, self.paths, models={})
        return self.status()

    def foreground(self, options=None):
        spec = build_mcp_hub(self.paths, options)
        return foreground_service(spec, self.paths, models={}, startup_guard=self.operation)

    def daemon(self, action, options=None):
        if action == "start":
            return self.start(options)
        if action not in ("install", "uninstall"):
            raise LifecycleError("无效的 Hub daemon 操作")
        with self.operation():
            if action == "uninstall":
                launchd.uninstall(HUB_LABEL, root=self.paths.root)
            else:
                spec = build_mcp_hub(self.paths, options)
                if launchd.loaded(HUB_LABEL):
                    raise LifecycleError("MCP Hub 已加载；请先停止后再更新启动配置")
                launchd.install(spec)
        return self.status()

    def logs(self, limit=200):
        if type(limit) is not int or not 0 <= limit <= 1000:
            raise ValueError("日志行数必须在 0–1000 之间")
        if not limit:
            return ""
        try:
            with (self.paths.logs / "mcp-hub.log").open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - 1024 * 1024))
                content = stream.read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            return ""
        return self.redact_log("\n".join(content.splitlines()[-limit:]) + ("\n" if content else ""))

    def redact_log(self, content):
        values = []
        try:
            services = self.store.load()["services"]
            values.extend(value for item in services for key, value in item["env"].items()
                          if len(value) >= 4 or any(word in key.lower() for word in ("key", "token", "secret", "password")))
            values.extend(value for item in services for value in item["args"] if len(value) >= 4)
        except (HubConfigError, OSError, UnicodeError):
            pass
        for path in (self.paths.api_key, self.store.path.parent / "internal.key"):
            try:
                values.append(path.read_text(encoding="utf-8").strip())
            except (OSError, UnicodeError):
                pass
        for value in sorted({value for value in values if value}, key=len, reverse=True):
            content = content.replace(value, "<redacted>")
        content = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1<redacted>@", content)
        content = re.sub(r"(?i)\bBearer\s+[^\s\"',;]+", "Bearer <redacted>", content)
        content = re.sub(r"(?i)((?:api[_-]?key|token|secret|password|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}&]+", r"\1<redacted>", content)
        return content

    def follow_logs(self, limit=200):
        """Follow the owned log with the same redaction as the Web endpoint."""
        if type(limit) is not int or not 0 <= limit <= 1000:
            raise ValueError("日志行数必须在 0–1000 之间")
        path = self.paths.logs / "mcp-hub.log"
        first = True
        while True:
            while not path.is_file():
                time.sleep(.5)
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                if first:
                    stream.seek(0, os.SEEK_END)
                    offset = stream.tell()
                    yield self.logs(limit)
                    stream.seek(offset)
                    first = False
                while True:
                    line = stream.readline()
                    if line:
                        yield self.redact_log(line)
                        continue
                    time.sleep(.5)
                    try:
                        stat = path.stat()
                    except FileNotFoundError:
                        continue
                    if stat.st_ino != os.fstat(stream.fileno()).st_ino:
                        break
                    if stat.st_size < stream.tell():
                        stream.seek(0)
