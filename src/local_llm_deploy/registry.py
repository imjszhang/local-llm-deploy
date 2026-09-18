"""Validated registry operations. Library operations never exit the process."""
from __future__ import annotations

import argparse
import copy
import json
import sys
import threading
import time
from pathlib import Path

from .config import (
    ConfigError, ProjectPaths, dump_registry, load_registry_document, normalize_models,
    normalize_registry, project_paths, sanitize_top_level,
)
from .storage import atomic_json, file_lock


class Registry:
    def __init__(self, paths: ProjectPaths | None = None, *, ttl=30.0, clock=time.monotonic):
        self.paths = paths or project_paths()
        self.ttl = ttl
        self.clock = clock
        self._lock = threading.Lock()
        self._raw = None
        self._loaded = float("-inf")

    def load_raw(self, *, refresh=False):
        with self._lock:
            if refresh or self._raw is None or self.clock() - self._loaded >= self.ttl:
                candidate = load_registry_document(self.paths.registry)
                self._raw = candidate
                self._loaded = self.clock()
            return copy.deepcopy(self._raw)

    def specs(self, *, refresh=False):
        return normalize_models(self.load_raw(refresh=refresh))

    def proxies(self, *, refresh=False):
        return normalize_registry(self.load_raw(refresh=refresh))[1]

    def update(self, operation):
        with file_lock(self.paths.registry.with_suffix(".json.lock")):
            raw = load_registry_document(self.paths.registry)
            operation(raw)
            raw = dump_registry(raw)
            atomic_json(self.paths.registry, raw)
        self.load_raw(refresh=True)

    def save(self, raw):
        normalized = dump_registry(raw)
        with file_lock(self.paths.registry.with_suffix(".json.lock")):
            atomic_json(self.paths.registry, normalized)
        self.load_raw(refresh=True)


def main(argv=None, *, paths=None):
    parser = argparse.ArgumentParser(description="模型注册表")
    parser.add_argument("--project-root")
    sub = parser.add_subparsers(dest="cmd", required=True)
    init = sub.add_parser("init")
    init.add_argument("--force", action="store_true")
    sub.add_parser("list")
    sub.add_parser("validate")
    show = sub.add_parser("show")
    show.add_argument("key", nargs="?")
    show.add_argument("--resolved", action="store_true")
    merge = sub.add_parser("merge")
    merge.add_argument("file")
    remove = sub.add_parser("remove")
    remove.add_argument("key")
    args = parser.parse_args(argv)
    paths = paths or project_paths(args.project_root)
    registry = Registry(paths)
    try:
        if args.cmd == "init":
            template = paths.model_template
            raw = load_registry_document(template)
            with file_lock(paths.registry.with_suffix(".json.lock")):
                if paths.registry.exists() and not args.force:
                    raise ConfigError("models.json 已存在；覆盖需 --force")
                atomic_json(paths.registry, raw)
            print(f"已生成 {paths.registry}")
        elif args.cmd == "merge":
            patch = sanitize_top_level(json.loads(Path(args.file).read_text(encoding="utf-8")))
            registry.update(lambda raw: raw.update(patch))
            print(f"已合并 {len(patch)} 个注册项")
        elif args.cmd == "remove":
            registry.update(lambda raw: raw.pop(args.key))
            print(f"已删除注册项: {args.key}")
        elif args.cmd == "validate":
            models, proxies = normalize_registry(registry.load_raw())
            print(f"配置有效：{len(models)} 个模型，{len(proxies)} 个外部服务")
        elif args.cmd == "list":
            for key, spec in registry.specs().items():
                print(f"{key:28s} {','.join(spec.capabilities):10s} {spec.backend:24s} :{spec.port} {spec.alias}")
            for key, spec in registry.proxies().items():
                print(f"{key:28s} {'proxy':10s} {'external_http':24s} :{spec.port} {spec.alias}")
        else:
            raw = registry.load_raw()
            if args.resolved:
                models, proxies = normalize_registry(raw)
                raw = {k: dict(s.raw, capabilities=list(s.capabilities), backend=s.backend,
                               management=s.management, endpoints=list(s.endpoints))
                       for k, s in models.items()}
                raw.update({k: dict(s.raw, kind="proxy", prefix=s.prefix, host=s.host, port=s.port)
                            for k, s in proxies.items()})
            value = raw[args.key] if args.key else raw
            print(json.dumps(_redact(value), ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


def _redact(value):
    if isinstance(value, dict):
        return {k: "<redacted>" if any(word in k.lower() for word in ("token", "secret", "password", "api_key"))
                else _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value
