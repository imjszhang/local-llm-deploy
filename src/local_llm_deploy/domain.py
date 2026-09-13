"""Shared values; no HTTP, filesystem operations or inference imports."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
                  "asr": ("/v1/audio/transcriptions",)}
        return tuple(p for c in self.capabilities for p in routes[c])


@dataclass(frozen=True)
class ModelInstallation:
    model_key: str
    path: Path
    quant: str | None = None
    revision: str | None = None
    complete: bool = False

