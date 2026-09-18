"""Generic external HTTP service routing. No model credentials or inference policy."""
from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit

from local_llm_deploy.domain import ProxySpec
from local_llm_deploy.lifecycle.observe import health_ready

from .discovery import tcp_connect_ok


SERVICES_PREFIX = "/services"


def is_service_path(path):
    pathname = path.split("?", 1)[0]
    return pathname == SERVICES_PREFIX or pathname.startswith(SERVICES_PREFIX + "/")


def parse_service_request(path):
    parsed = urlsplit(path)
    parts = [part for part in parsed.path.split("/") if part != ""]
    if not parts or parts[0] != "services":
        return None, None
    if len(parts) == 1:
        return None, None
    key = parts[1]
    remainder = "/" + "/".join(parts[2:]) if len(parts) > 2 else "/"
    query = "?" + parsed.query if parsed.query else ""
    return key, remainder + query


def path_allowed(allowed, remainder):
    pathname = remainder.split("?", 1)[0] or "/"
    if allowed is None:
        return True
    return any(pathname == prefix or pathname.startswith(prefix + "/") for prefix in allowed)


def service_backend_url(spec: ProxySpec, remainder):
    pathname = remainder.split("?", 1)[0] or "/"
    query = remainder[len(pathname):]
    target = pathname if spec.strip_prefix else spec.prefix + ("" if pathname == "/" else pathname)
    return spec.upstream.rstrip("/") + target + query


class ProxyCatalog:
    """Cached TCP + health_path observations that never enter the model backend table."""

    def __init__(self, proxies=None, *, probe=tcp_connect_ok, health=health_ready,
                 ttl=2.0, clock=time.monotonic):
        self.proxies = dict(proxies or {})
        self.probe, self.health, self.ttl, self.clock = probe, health, ttl, clock
        self.lock = threading.RLock()
        self._rows = {}
        self._loaded = float("-inf")

    def update(self, proxies):
        with self.lock:
            self.proxies = dict(proxies or {})
            self._loaded = float("-inf")

    def _refresh(self):
        now = self.clock()
        if now - self._loaded < self.ttl:
            return
        rows = {}
        for key, spec in self.proxies.items():
            host, port = spec.host, spec.port
            if not self.probe(host, port):
                state, reason = "unreachable", "Backend could not be reached"
            elif urlsplit(spec.upstream).scheme != "http":
                state, reason = "healthy", None
            elif self.health(host, port, spec.health_path, timeout=.5):
                state, reason = "healthy", None
            else:
                state, reason = "unready", "Backend has not reported ready"
            rows[key] = {
                "key": key, "alias": spec.alias, "upstream": spec.upstream,
                "host": host, "port": port, "health_path": spec.health_path,
                "endpoint": spec.prefix + "/", "availability": state, "reason": reason,
            }
        self._rows = rows
        self._loaded = now

    def rows(self):
        with self.lock:
            self._refresh()
            return [dict(row) for row in self._rows.values()]

    def availability(self, key):
        with self.lock:
            self._refresh()
            row = self._rows.get(key)
            return None if row is None else row["availability"]
