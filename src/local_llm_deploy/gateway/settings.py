"""Validated gateway configuration, captured once when the process starts."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
import math
from typing import Mapping


# Shared with the launcher: launchd starts with a different environment from the
# invoking shell. Forward exactly the settings this module consumes.
GATEWAY_ENV_NAMES = frozenset({
    'API_PROXY_TIMEOUT', 'BACKEND_CONNECT_TIMEOUT', 'MONITOR_PROXY_TIMEOUT',
    'QUEUE_KEEPALIVE_SEC', 'QUEUE_TIMEOUT', 'MAX_QUEUE_DEPTH',
    'CHAT_LANE_CONCURRENT', 'MAX_GLOBAL_CONCURRENT', 'EMBED_LANE_CONCURRENT',
    'RERANK_LANE_CONCURRENT', 'ASR_LANE_CONCURRENT', 'KV_CHARS_PER_TOKEN',
    'OLLAMA_HOST', 'OLLAMA_AUTO_DISCOVER', 'EXTERNAL_BACKEND_PROBE_TTL', 'OLLAMA_CACHE_TTL',
    'KNOWLEDGE_COLLECTOR_URL', 'KNOWLEDGE_PROXY_TIMEOUT', 'MAX_REQUEST_BODY_BYTES',
    'STREAM_BUFFER_CHUNKS', 'ACCESS_LOG_CAPTURE_BYTES', 'CANCEL_GRACE_SEC',
    'CLIENT_WRITE_TIMEOUT', 'SERVE_UI_ACCESS_LOG', 'SERVE_UI_LOG_BODY',
    'DEFAULT_CHAT_MODEL', 'DEFAULT_EMBEDDING_MODEL', 'DEFAULT_RERANK_MODEL', 'DEFAULT_ASR_MODEL',
})


@dataclass(frozen=True)
class GatewaySettings:
    api_timeout: float = 3600
    connect_timeout: float = 5
    monitor_timeout: float = 8
    keepalive: float = 5
    queue_timeout: float = 3600
    max_queue_depth: int = 12
    chat_concurrent: int = 1
    embed_concurrent: int = 2
    rerank_concurrent: int = 1
    asr_concurrent: int = 1
    chars_per_token: float = 2.5
    ollama_host: str = 'http://localhost:11434'
    ollama_auto_discover: bool = True
    discovery_ttl: float = 2
    ollama_ttl: float = 5
    system_ttl: float = 3
    knowledge_url: str = 'http://127.0.0.1:18789/plugins/js-knowledge'
    knowledge_timeout: float = 30
    max_body_bytes: int = 64 * 1024 * 1024
    stream_buffer_chunks: int = 16
    capture_bytes: int = 65536
    cancel_grace: float = 10
    client_write_timeout: float = 30
    access_log: str | None = None
    log_body: bool = False
    defaults: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        for name in ('api_timeout', 'connect_timeout', 'monitor_timeout', 'keepalive', 'queue_timeout',
                     'max_queue_depth', 'chat_concurrent', 'embed_concurrent',
                     'rerank_concurrent', 'asr_concurrent', 'chars_per_token',
                     'stream_buffer_chunks', 'max_body_bytes', 'cancel_grace', 'client_write_timeout'):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f'{name} must be positive')
        if self.capture_bytes < 0:
            raise ValueError('capture_bytes must not be negative')

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        def number(name, default, cast=float):
            return cast(env.get(name, default))
        def flag(name, default=True):
            return str(env.get(name, str(int(default)))).lower() not in ('0', 'false', 'no', '')
        return cls(
            api_timeout=number('API_PROXY_TIMEOUT', 3600),
            connect_timeout=number('BACKEND_CONNECT_TIMEOUT', 5),
            monitor_timeout=number('MONITOR_PROXY_TIMEOUT', 8),
            keepalive=number('QUEUE_KEEPALIVE_SEC', 5),
            queue_timeout=number('QUEUE_TIMEOUT', env.get('API_PROXY_TIMEOUT', 3600)),
            max_queue_depth=number('MAX_QUEUE_DEPTH', 12, int),
            chat_concurrent=number('CHAT_LANE_CONCURRENT', env.get('MAX_GLOBAL_CONCURRENT', 1), int),
            embed_concurrent=number('EMBED_LANE_CONCURRENT', 2, int),
            rerank_concurrent=number('RERANK_LANE_CONCURRENT', 1, int),
            asr_concurrent=number('ASR_LANE_CONCURRENT', 1, int),
            chars_per_token=number('KV_CHARS_PER_TOKEN', 2.5),
            ollama_host=env.get('OLLAMA_HOST', 'http://localhost:11434').rstrip('/'),
            ollama_auto_discover=flag('OLLAMA_AUTO_DISCOVER'),
            discovery_ttl=number('EXTERNAL_BACKEND_PROBE_TTL', 2),
            ollama_ttl=number('OLLAMA_CACHE_TTL', 5),
            knowledge_url=env.get('KNOWLEDGE_COLLECTOR_URL', cls.knowledge_url).rstrip('/'),
            knowledge_timeout=number('KNOWLEDGE_PROXY_TIMEOUT', 30),
            max_body_bytes=number('MAX_REQUEST_BODY_BYTES', 64 * 1024 * 1024, int),
            stream_buffer_chunks=number('STREAM_BUFFER_CHUNKS', 16, int),
            capture_bytes=number('ACCESS_LOG_CAPTURE_BYTES', 65536, int),
            cancel_grace=number('CANCEL_GRACE_SEC', 10),
            client_write_timeout=number('CLIENT_WRITE_TIMEOUT', 30),
            access_log=env.get('SERVE_UI_ACCESS_LOG') or None,
            log_body=flag('SERVE_UI_LOG_BODY', False),
            defaults={cap: env[f'DEFAULT_{cap.upper()}_MODEL'] for cap in
                      ('chat', 'embedding', 'rerank', 'asr') if env.get(f'DEFAULT_{cap.upper()}_MODEL')},
        )
