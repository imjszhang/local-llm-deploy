"""Validated registry operations. Library operations never exit the process."""
from __future__ import annotations

import argparse
import copy
import json
import sys
import threading
import time
from pathlib import Path

from .config import ConfigError, ProjectPaths, load_models, normalize_models, project_paths, sanitize_top_level
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
                candidate = load_models(self.paths.registry)
                self._raw = candidate
                self._loaded = self.clock()
            return copy.deepcopy(self._raw)

    def specs(self, *, refresh=False):
        return normalize_models(self.load_raw(refresh=refresh))

    def update(self, operation):
        with file_lock(self.paths.registry.with_suffix(".json.lock")):
            raw = load_models(self.paths.registry)
            operation(raw)
            normalize_models(raw)
            atomic_json(self.paths.registry, raw)
        self.load_raw(refresh=True)

    def save(self, raw):
        normalized = {k: spec.raw for k, spec in normalize_models(raw).items()}
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
            template = paths.root / "models.json.example"
            raw = load_models(template)
            with file_lock(paths.registry.with_suffix(".json.lock")):
                if paths.registry.exists() and not args.force:
                    raise ConfigError("models.json 已存在；覆盖需 --force")
                atomic_json(paths.registry, raw)
            print(f"已生成 {paths.registry}")
        elif args.cmd == "merge":
            patch = sanitize_top_level(json.loads(Path(args.file).read_text(encoding="utf-8")))
            registry.update(lambda raw: raw.update(patch))
            print(f"已合并 {len(patch)} 个模型")
        elif args.cmd == "remove":
            registry.update(lambda raw: raw.pop(args.key))
            print(f"已删除注册项: {args.key}")
        elif args.cmd == "validate":
            print(f"配置有效：{len(registry.specs())} 个模型")
        elif args.cmd == "list":
            for key, spec in registry.specs().items():
                print(f"{key:28s} {','.join(spec.capabilities):10s} {spec.backend:24s} :{spec.port} {spec.alias}")
        else:
            raw = registry.load_raw()
            if args.resolved:
                raw = {k: dict(s.raw, capabilities=list(s.capabilities), backend=s.backend,
                               management=s.management, endpoints=list(s.endpoints))
                       for k, s in registry.specs().items()}
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
