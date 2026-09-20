"""Shared values; no HTTP, filesystem operations or inference imports."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ModelSpec:
    key: str
    alias: str
    capabilities: tuple[str, ...]
    backend: str
    management: str
    raw: dict[str, Any] = field(repr=False)

    @property
    def params(self) -> dict[str, Any]:
        return self.raw.get("params", {})

    @property
    def port(self) -> int:
        return int(self.raw.get("default_port", 8001))

    @property
    def host(self) -> str:
        return str(self.raw.get("host") or self.raw.get("external_host") or "127.0.0.1")

    @property
    def backend_model(self) -> str:
        return str(self.raw.get("backend_model") or self.raw.get("ollama_model") or self.alias)

    @property
    def endpoints(self) -> tuple[str, ...]:
        explicit = self.raw.get("endpoints")
        if explicit is not None:
            return tuple(explicit)
        routes = {"chat": ("/v1/chat/completions", "/v1/completions"),
                  "embedding": ("/v1/embeddings",), "rerank": ("/v1/rerank",),
                  "asr": ("/v1/audio/transcriptions",), "tts": ("/v1/audio/speech",)}
        return tuple(p for c in self.capabilities for p in routes[c])


@dataclass(frozen=True)
class ProxySpec:
    key: str
    alias: str
    upstream: str
    health_path: str
    timeout: float | None
    strip_prefix: bool
    methods: tuple[str, ...]
    paths: tuple[str, ...] | None
    auth_gateway: str
    auth_upstream: str
    auth_console: bool
    websocket: bool
    max_body_bytes: int | None
    raw: dict[str, Any] = field(repr=False)

    @property
    def host(self) -> str:
        return str(urlsplit(self.upstream).hostname or "127.0.0.1")

    @property
    def port(self) -> int:
        parsed = urlsplit(self.upstream)
        if parsed.port:
            return parsed.port
        return 443 if parsed.scheme == "https" else 80

    @property
    def prefix(self) -> str:
        return f"/services/{self.key}"


@dataclass(frozen=True)
class AppSpec:
    key: str
    alias: str
    kind: str
    prefix: str
    legacy_prefixes: tuple[str, ...]
    upstream: str
    home: str
    root: str
    node: str
    timeout: float | None
    raw: dict[str, Any] = field(repr=False)

    @property
    def endpoint(self) -> str:
        return self.prefix.rstrip("/") + "/"


@dataclass(frozen=True)
class ModelInstallation:
    model_key: str
    path: Path
    quant: str | None = None
    revision: str | None = None
    complete: bool = False
