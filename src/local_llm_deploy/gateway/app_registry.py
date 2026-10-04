"""Self-registered HTTP app mounts. Stored under data/, never the git registry."""
from __future__ import annotations

import json
import os
from pathlib import Path

from local_llm_deploy.config import (
    ConfigError, _prefixes_overlap, _registry_key, load_apps, load_catalog, normalize_app,
)
from local_llm_deploy.domain import AppSpec
from local_llm_deploy.storage import atomic_json, file_lock


def registration_path(registry_path) -> Path:
    return Path(registry_path).resolve().parent / "data" / "gateway-apps.json"


def _read_document(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"无法读取自行注册的应用 {path}: {exc}") from exc
    apps = raw.get("apps") if isinstance(raw, dict) else None
    if raw.get("schema_version") != 1 or not isinstance(apps, dict):
        raise ConfigError(f"自行注册的应用文件无效: {path}")
    return apps


def load_registered_apps(path: Path) -> dict[str, AppSpec]:
    try:
        raw_apps = _read_document(path)
    except ConfigError:
        return {}
    specs = {}
    for key, value in raw_apps.items():
        try:
            if not isinstance(value, dict) or value.get("kind") != "http":
                continue
            specs[_registry_key(key)] = normalize_app(key, value)
        except ConfigError:
            continue
    return specs


def _occupied_names(models, proxies, apps):
    names = {}
    for spec in models.values():
        for name in (spec.key, spec.alias, spec.backend_model):
            names.setdefault(name, spec.key)
    for spec in proxies.values():
        for name in (spec.key, spec.alias):
            names.setdefault(name, spec.key)
    for spec in apps.values():
        for name in (spec.key, spec.alias):
            names.setdefault(name, spec.key)
    return names


def _reject_conflicts(spec: AppSpec, models, proxies, apps, *, replacing=False):
    names = _occupied_names(models, proxies, apps)
    for name in (spec.key, spec.alias):
        owner = names.get(name)
        if owner and owner != spec.key:
            raise ConfigError(f"名称冲突: {name!r} 已被 {owner} 使用")
        if owner == spec.key and not replacing and name == spec.key and spec.key in apps:
            raise ConfigError(f"{spec.key} 已在 models.json 中登记，不能用自行注册覆盖")
    if spec.key in models or spec.key in proxies:
        raise ConfigError(f"{spec.key} 已是模型或外部服务，不能再注册为应用入口")
    if spec.key in apps and not replacing:
        raise ConfigError(f"{spec.key} 已在 models.json 中登记，不能用自行注册覆盖")
    for prefix in (spec.prefix, *spec.legacy_prefixes):
        for other in apps.values():
            if replacing and other.key == spec.key:
                continue
            for owned in (other.prefix, *other.legacy_prefixes):
                if _prefixes_overlap(prefix, owned):
                    raise ConfigError(f"{spec.key}: 路径 {prefix} 与 {other.key} 的 {owned} 冲突")


def merge_mounted_apps(declared: dict[str, AppSpec], registered: dict[str, AppSpec]) -> dict[str, AppSpec]:
    mounted = dict(declared)
    for key, spec in registered.items():
        if key in declared:
            continue
        try:
            _reject_conflicts(spec, {}, {}, mounted, replacing=False)
        except ConfigError:
            continue
        mounted[key] = spec
    return mounted


def default_monitor_app() -> AppSpec:
    return normalize_app("monitor", {
        "type": "app", "kind": "monitor", "alias": "运行监控", "prefix": "/monitor",
    })


def ensure_monitor_app(apps: dict[str, AppSpec]) -> dict[str, AppSpec]:
    if any(spec.kind == "monitor" for spec in apps.values()):
        return apps
    spec = default_monitor_app()
    try:
        _reject_conflicts(spec, {}, {}, apps, replacing=False)
    except ConfigError:
        return apps
    mounted = dict(apps)
    mounted[spec.key] = spec
    return mounted


def load_mounted_apps(registry_path) -> dict[str, AppSpec]:
    path = Path(registry_path)
    declared = load_apps(path) if path.is_file() else {}
    return ensure_monitor_app(merge_mounted_apps(declared, load_registered_apps(registration_path(path))))


class AppRegistrationStore:
    def __init__(self, registry_path):
        self.registry_path = Path(registry_path)
        self.path = registration_path(self.registry_path)

    def list_registered(self) -> dict[str, AppSpec]:
        with file_lock(self.path.with_suffix(".json.lock"), shared=True):
            return load_registered_apps(self.path)

    def register(self, key: str, body: dict) -> AppSpec:
        key = _registry_key(key)
        if not isinstance(body, dict):
            raise ConfigError(f"{key}: 注册内容必须是对象")
        if body.get("type") not in (None, "app") or body.get("kind") not in (None, "http"):
            raise ConfigError(f"{key}: 自行注册只支持 kind=http")
        payload = dict(body)
        payload["type"] = "app"
        payload["kind"] = "http"
        spec = normalize_app(key, payload)
        with file_lock(self.path.with_suffix(".json.lock")):
            current = _read_document(self.path) if self.path.is_file() else {}
            models, proxies, declared = ({}, {}, {})
            if self.registry_path.is_file():
                models, proxies = load_catalog(self.registry_path)
                declared = load_apps(self.registry_path)
            registered = {}
            for item_key, item in current.items():
                if item_key == key or not isinstance(item, dict):
                    continue
                try:
                    registered[_registry_key(item_key)] = normalize_app(item_key, item)
                except ConfigError:
                    continue
            _reject_conflicts(spec, models, proxies, {**declared, **registered}, replacing=key in current)
            current[key] = spec.raw
            self._write(current)
        return spec

    def remove(self, key: str) -> None:
        key = _registry_key(key)
        with file_lock(self.path.with_suffix(".json.lock")):
            current = _read_document(self.path) if self.path.is_file() else {}
            if key not in current:
                raise ConfigError(f"未找到自行注册的应用 {key}")
            current.pop(key)
            self._write(current)

    def _write(self, apps: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        atomic_json(self.path, {"schema_version": 1, "apps": apps})
        os.chmod(self.path, 0o600)
