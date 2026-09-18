"""One registry loader and one definition of project-relative paths."""
from __future__ import annotations

import copy
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .domain import ModelSpec, ProxySpec


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ProjectPaths:
    root: Path

    def __post_init__(self):
        object.__setattr__(self, "root", Path(self.root).expanduser().resolve())

    @property
    def registry(self):
        return self.root / "models.json"

    @property
    def models(self):
        return self.root / "models"

    @property
    def run(self):
        return self.root / "run"

    @property
    def logs(self):
        return self.root / "logs"

    @property
    def api_key(self):
        return self.root / ".api-key"

    @property
    def static(self):
        return self.root / "static"

    @property
    def model_template(self):
        template = self.root / "config" / "examples" / "models.json.example"
        return template if template.is_file() else self.root / "models.json.example"


def project_paths(root: str | Path | None = None) -> ProjectPaths:
    selected = root or os.environ.get("LOCAL_LLM_ROOT")
    if selected is None:
        source_root = Path(__file__).resolve().parents[2]
        if not ProjectPaths(source_root).model_template.is_file():
            raise ConfigError("请设置 LOCAL_LLM_ROOT 或 --project-root 指向部署目录")
        selected = source_root
    return ProjectPaths(Path(selected))


def sanitize_top_level(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError("models.json 顶层必须是对象")
    return {str(k): v for k, v in raw.items() if not str(k).startswith("_")}



def _finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _string_value(key, field, value, *, nullable=False, nonempty=False, controls=False):
    if value is None and nullable:
        return
    if not isinstance(value, str) or (nonempty and not value):
        raise ConfigError(f"{key}: {field} 必须是{'非空' if nonempty else ''}字符串")
    if controls and any(char in value for char in ("\x00", "\r", "\n")):
        raise ConfigError(f"{key}: {field} 不能包含换行或 NUL")


def _patterns_value(key, field, value):
    if value is None:
        return
    if isinstance(value, str):
        return
    if not isinstance(value, list) or any(not isinstance(part, str) for part in value):
        raise ConfigError(f"{key}: {field} 必须是字符串或字符串列表")


def _validate_chat_controls(key, value):
    fields = {"thinking", "reasoning_efforts", "reasoning_budget", "default_thinking", "default_effort"}
    if not isinstance(value, dict):
        raise ConfigError(f"{key}: chat_controls 必须是对象")
    if set(value) - fields:
        raise ConfigError(f"{key}: chat_controls 包含未知字段")
    if fields - set(value):
        raise ConfigError(f"{key}: chat_controls 必须完整声明 thinking、reasoning_efforts、reasoning_budget、default_thinking、default_effort")
    for field in ("thinking", "reasoning_budget"):
        if type(value[field]) is not bool:
            raise ConfigError(f"{key}: chat_controls.{field} 必须是布尔值")
    if value["default_thinking"] is not None and type(value["default_thinking"]) is not bool:
        raise ConfigError(f"{key}: chat_controls.default_thinking 必须是布尔值或 null")
    efforts = value["reasoning_efforts"]
    if (not isinstance(efforts, list) or len(efforts) > 8 or
            any(not isinstance(effort, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,23}", effort)
                or effort == "none" for effort in efforts)):
        raise ConfigError(f"{key}: chat_controls.reasoning_efforts 必须是至多 8 个非空短标识的列表（不能包含 none）")
    if len(set(efforts)) != len(efforts):
        raise ConfigError(f"{key}: chat_controls.reasoning_efforts 不能重复")
    default = value["default_effort"]
    if default is not None and (not isinstance(default, str) or default not in efforts):
        raise ConfigError(f"{key}: chat_controls.default_effort 必须是 reasoning_efforts 中的值或 null")
    if not value["thinking"] and (efforts or value["reasoning_budget"] or value["default_thinking"] is True or default is not None):
        raise ConfigError(f"{key}: chat_controls.thinking 为 false 时不能声明推理强度、预算或默认开启")


def _validate_known_fields(key, cfg):
    """Validate JSON shapes before hashing, path construction or coercion.

    Optional nulls retain their existing unset meaning. Unknown extension fields
    are preserved; only fields consumed by the control plane are constrained.
    """
    scalar_strings = (
        "type", "backend", "management", "alias", "repo_id", "repo_name", "default_quant",
        "host", "external_host", "download_source", "download_target_mode", "revision", "hf_revision",
        "model_path", "cpp_dir", "engine_profile", "engine", "mmproj", "chat_template_file", "health_path",
    )
    for field in scalar_strings:
        if field in cfg:
            _string_value(key, field, cfg[field], nullable=True, controls=True)
    for field in ("backend_model", "ollama_model"):
        if field in cfg:
            _string_value(key, field, cfg[field], nonempty=True, controls=True)
    for field in ("full_model_name", "startup_hint"):
        if field in cfg:
            _string_value(key, field, cfg[field], nullable=True)
    for field in ("params", "runtime", "quants"):
        if field in cfg and not isinstance(cfg[field], dict):
            raise ConfigError(f"{key}: {field} 必须是对象")
    if "chat_controls" in cfg:
        _validate_chat_controls(key, cfg["chat_controls"])
    for field in ("capabilities", "default_for", "endpoints"):
        if field not in cfg or (field == "endpoints" and cfg[field] is None):
            continue
        if not isinstance(cfg[field], list) or any(not isinstance(item, str) for item in cfg[field]):
            raise ConfigError(f"{key}: {field} 必须是字符串列表")
    if cfg.get("external_backend") is not None and type(cfg["external_backend"]) is not bool:
        raise ConfigError(f"{key}: external_backend 必须是布尔值")
    _patterns_value(key, "download_allow_patterns", cfg.get("download_allow_patterns"))
    for quant, info in cfg.get("quants", {}).items():
        if not isinstance(info, dict):
            raise ConfigError(f"{key}: quants.{quant} 必须是对象")
        for field in ("pattern", "patterns"):
            _patterns_value(key, f"quants.{quant}.{field}", info.get(field))
        size = info.get("size_gb")
        if size is not None and (not _finite_number(size) or size < 0):
            raise ConfigError(f"{key}: quants.{quant}.size_gb 必须是有限非负数")
    params = cfg.get("params", {})
    for field in ("ctx_size", "max_concurrent", "max_documents", "n_predict", "dimensions"):
        if field in params and not (field == "dimensions" and params[field] is None) and type(params[field]) is not int:
            raise ConfigError(f"{key}: params.{field} 必须是整数")
    for field in ("temp", "top_p", "repeat_penalty", "kv_budget_ratio"):
        if field in params and not _finite_number(params[field]):
            raise ConfigError(f"{key}: params.{field} 必须是有限数值")
    for field in ("language", "task", "response_format", "default_task"):
        if field in params:
            _string_value(key, f"params.{field}", params[field], nonempty=True, controls=True)
    runtime = cfg.get("runtime", {})
    for field in ("python", "cpp_dir", "api_key_file", "ready_path", "management"):
        if field in runtime:
            _string_value(key, f"runtime.{field}", runtime[field],
                          nullable=field in ("python", "cpp_dir", "api_key_file"), nonempty=True, controls=True)
    if "ready_timeout" in runtime and (not _finite_number(runtime["ready_timeout"]) or runtime["ready_timeout"] <= 0):
        raise ConfigError(f"{key}: runtime.ready_timeout 必须是有限正数")


def _registry_key(key: Any) -> str:
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", key):
        raise ConfigError(f"无效模型键 {key!r}（不能包含路径分隔符或空白）")
    return key


def _absolute_http_url(key: str, field: str, value: Any) -> str:
    _string_value(key, field, value, nonempty=True, controls=True)
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise ConfigError(f"{key}: {field} 必须是不含用户信息的绝对 http(s) URL")
    if parsed.hostname in (None, "") or any(part in (".", "..") for part in parsed.path.split("/")):
        raise ConfigError(f"{key}: {field} 必须是不含用户信息的绝对 http(s) URL")
    return value


def _service_path(key: str, field: str, value: Any) -> str:
    _string_value(key, field, value, nonempty=True, controls=True)
    if not value.startswith("/") or value.startswith("//") or any(part in (".", "..") for part in value.split("/")):
        raise ConfigError(f"{key}: {field} 必须是不含 .. 的绝对路径")
    return value


def normalize_proxy(key: str, value: Any) -> ProxySpec:
    if not isinstance(value, dict):
        raise ConfigError(f"{key}: 模型配置必须是对象")
    cfg = copy.deepcopy(value)
    if cfg.get("type") != "proxy":
        raise ConfigError(f"{key}: 不是 proxy 条目")
    for field in ("alias", "full_model_name", "startup_hint", "health_path"):
        if field in cfg:
            _string_value(key, field, cfg[field], nullable=field != "health_path",
                          nonempty=field == "health_path", controls=field == "health_path")
    upstream = _absolute_http_url(key, "upstream", cfg.get("upstream"))
    health_path = _service_path(key, "health_path", cfg["health_path"]) if cfg.get("health_path") is not None else "/"
    timeout = cfg.get("timeout")
    if timeout is not None and (not _finite_number(timeout) or timeout <= 0):
        raise ConfigError(f"{key}: timeout 必须是有限正数")
    if "strip_prefix" in cfg and type(cfg["strip_prefix"]) is not bool:
        raise ConfigError(f"{key}: strip_prefix 必须是布尔值")
    methods = cfg.get("methods", ["GET", "HEAD", "POST", "PUT", "DELETE"])
    allowed_methods = {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"}
    if (not isinstance(methods, list) or not methods or
            any(not isinstance(item, str) or item.upper() not in allowed_methods for item in methods)):
        raise ConfigError(f"{key}: methods 必须是 HTTP 方法列表")
    paths = cfg.get("paths")
    if paths is not None:
        if not isinstance(paths, list) or not paths or any(not isinstance(item, str) for item in paths):
            raise ConfigError(f"{key}: paths 必须是非空字符串列表")
        paths = tuple(_service_path(key, "paths", item) for item in paths)
    auth = cfg.get("auth", {})
    if not isinstance(auth, dict):
        raise ConfigError(f"{key}: auth 必须是对象")
    gateway = auth.get("gateway", "api_key")
    upstream_auth = auth.get("upstream", "none")
    console = auth.get("console", False)
    if gateway != "api_key":
        raise ConfigError(f"{key}: auth.gateway 目前只支持 api_key")
    if upstream_auth not in ("none", "forward_client"):
        raise ConfigError(f"{key}: auth.upstream 必须是 none 或 forward_client")
    if type(console) is not bool:
        raise ConfigError(f"{key}: auth.console 必须是布尔值")
    if "websocket" in cfg and type(cfg["websocket"]) is not bool:
        raise ConfigError(f"{key}: websocket 必须是布尔值")
    if cfg.get("websocket"):
        raise ConfigError(f"{key}: websocket 本期必须为 false")
    max_body = cfg.get("max_body_bytes")
    if max_body is not None and (type(max_body) is not int or isinstance(max_body, bool) or max_body <= 0):
        raise ConfigError(f"{key}: max_body_bytes 必须是正整数")
    alias = cfg.get("alias") or key
    if not isinstance(alias, str) or not alias or any(c in alias for c in ("\n", "\r", "\x00")):
        raise ConfigError(f"{key}: alias 必须是非空字符串")
    cfg.setdefault("health_path", health_path)
    cfg.setdefault("strip_prefix", True)
    cfg.setdefault("methods", [item.upper() for item in methods])
    cfg.setdefault("auth", {"gateway": gateway, "upstream": upstream_auth, "console": console})
    cfg.setdefault("websocket", False)
    return ProxySpec(
        key, alias, upstream, health_path, None if timeout is None else float(timeout),
        bool(cfg.get("strip_prefix", True)), tuple(item.upper() for item in methods), paths,
        gateway, upstream_auth, console, False, max_body, cfg,
    )


def normalize_models(raw: Any) -> dict[str, ModelSpec]:
    data = sanitize_top_level(raw)
    result: dict[str, ModelSpec] = {}
    names: dict[str, str] = {}
    defaults: dict[str, str] = {}
    legacy = {"chat": ("chat", "llama_cpp"), "embedding": ("embedding", "transformers_embedding"),
              "rerank": ("rerank", "mlx_rerank"), "asr": ("asr", "mlx_whisper"),
              "ollama": ("chat", "ollama"), "external": ("chat", "external_http")}
    for key, value in data.items():
        key = _registry_key(key)
        if not isinstance(value, dict):
            raise ConfigError(f"{key}: 模型配置必须是对象")
        if value.get("type") == "proxy":
            continue
        cfg = copy.deepcopy(value)
        _validate_known_fields(key, cfg)
        typ = cfg.get("type") or "chat"
        if typ not in legacy:
            raise ConfigError(f"{key}: 未知旧模型类型 {typ!r}")
        capability, backend = legacy[typ]
        backend = cfg.get("backend") or ("external_http" if cfg.get("external_backend") else backend)
        if backend == "ollama" and cfg.get("chat_controls", {}).get("reasoning_budget"):
            raise ConfigError(f"{key}: Ollama chat_controls.reasoning_budget 必须为 false")
        caps = cfg.get("capabilities", [capability])
        if not isinstance(caps, list) or not caps or any(c not in ("chat", "embedding", "rerank", "asr") for c in caps):
            raise ConfigError(f"{key}: capabilities 必须是有效能力列表")
        management = cfg.get("management") or ("external" if backend in ("ollama", "external_http") else "managed")
        if management not in ("managed", "external"):
            raise ConfigError(f"{key}: management 必须是 managed 或 external")
        alias = cfg.get("alias") or key
        if not isinstance(alias, str):
            raise ConfigError(f"{key}: alias 必须是字符串")
        for name in dict.fromkeys((key, alias, cfg.get("backend_model", key), cfg.get("ollama_model", key))):
            if not isinstance(name, str) or not name or any(c in name for c in ("\n", "\r", "\x00")):
                raise ConfigError(f"{key}: 模型名称必须是非空字符串")
            if name in names and names[name] != key:
                raise ConfigError(f"模型别名冲突: {name!r} ({names[name]}, {key})")
            names[name] = key
        port = cfg.get("default_port", 8001)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ConfigError(f"{key}: default_port 必须是 1..65535 的整数")
        params = cfg.get("params", {})
        if not isinstance(params, dict):
            raise ConfigError(f"{key}: params 必须是对象")
        for param in ("ctx_size", "max_concurrent", "max_documents"):
            if param in params and (type(params[param]) is not int or params[param] <= 0):
                raise ConfigError(f"{key}: {param} 必须是正整数")
        ratio = params.get("kv_budget_ratio", 0.9)
        if type(ratio) not in (int, float) or not 0 < ratio <= 1:
            raise ConfigError(f"{key}: kv_budget_ratio 必须在 (0, 1] 内")
        extra = params.get("extra_args", [])
        if not isinstance(extra, list) or any(not isinstance(x, str) for x in extra):
            raise ConfigError(f"{key}: extra_args 必须是字符串列表")
        quants = cfg.get("quants", {})
        if not isinstance(quants, dict) or any(not isinstance(q, dict) for q in quants.values()):
            raise ConfigError(f"{key}: quants 必须是对象映射")
        if cfg.get("default_quant") and cfg["default_quant"] not in quants:
            raise ConfigError(f"{key}: default_quant 未在 quants 声明")
        for quant in quants:
            if not isinstance(quant, str) or Path(quant).name != quant or quant in (".", ".."):
                raise ConfigError(f"{key}: 量化名不能包含目录分隔符")
        runtime = cfg.get("runtime", {})
        if not isinstance(runtime, dict):
            raise ConfigError(f"{key}: runtime 必须是对象")
        for field in ("python", "cpp_dir", "api_key_file", "ready_path"):
            value = runtime.get(field)
            if value is not None and (not isinstance(value, str) or not value or any(c in value for c in ("\x00", "\r", "\n"))):
                raise ConfigError(f"{key}: runtime.{field} 必须是非空字符串")
        if runtime.get("management", "process") not in ("process", "launchd"):
            raise ConfigError(f"{key}: runtime.management 必须是 process 或 launchd")
        timeout = runtime.get("ready_timeout", 180)
        if not _finite_number(timeout) or timeout <= 0:
            raise ConfigError(f"{key}: runtime.ready_timeout 必须是有限正数")
        if "run_at_load" in runtime and not isinstance(runtime["run_at_load"], bool):
            raise ConfigError(f"{key}: runtime.run_at_load 必须是布尔值")
        if "keep_alive" in runtime and not isinstance(runtime["keep_alive"], (bool, dict)):
            raise ConfigError(f"{key}: runtime.keep_alive 必须是布尔值或 launchd 策略对象")
        rn = cfg.get("repo_name")
        if rn and (Path(rn).name != rn or rn in (".", "..")):
            raise ConfigError(f"{key}: repo_name 必须是单一目录名")
        default_for = cfg.get("default_for", [])
        if not isinstance(default_for, list) or any(c not in caps for c in default_for):
            raise ConfigError(f"{key}: default_for 必须是本模型能力的子集")
        for c in default_for:
            if c in defaults:
                raise ConfigError(f"{c} 默认模型冲突: {defaults[c]}, {key}")
            defaults[c] = key
        endpoints = cfg.get("endpoints")
        if endpoints is not None and (not isinstance(endpoints, list) or any(not isinstance(p, str) or not p.startswith("/v1/") for p in endpoints)):
            raise ConfigError(f"{key}: endpoints 必须是 /v1/ 路径列表")
        result[key] = ModelSpec(key, alias, tuple(dict.fromkeys(caps)), str(backend), management, cfg)
    return result


def normalize_registry(raw: Any) -> tuple[dict[str, ModelSpec], dict[str, ProxySpec]]:
    data = sanitize_top_level(raw)
    proxies: dict[str, ProxySpec] = {}
    names: dict[str, str] = {}
    for key, value in data.items():
        key = _registry_key(key)
        if isinstance(value, dict) and value.get("type") == "proxy":
            spec = normalize_proxy(key, value)
            for name in dict.fromkeys((spec.key, spec.alias)):
                if name in names and names[name] != key:
                    raise ConfigError(f"模型别名冲突: {name!r} ({names[name]}, {key})")
                names[name] = key
            proxies[key] = spec
    models = normalize_models(raw)
    for spec in models.values():
        for name in dict.fromkeys((spec.key, spec.alias, spec.backend_model)):
            if name in names and names[name] != spec.key:
                raise ConfigError(f"模型别名冲突: {name!r} ({names[name]}, {spec.key})")
            names[name] = spec.key
    return models, proxies


def dump_registry(raw: Any) -> dict[str, Any]:
    models, proxies = normalize_registry(raw)
    data = sanitize_top_level(raw)
    return {key: (proxies[key].raw if key in proxies else models[key].raw) for key in data}


def _read_registry_json(path: str | Path | None = None) -> Any:
    target = Path(path) if path is not None else project_paths().registry
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"无法读取注册表 {target}: {exc}") from exc


def load_registry_document(path: str | Path | None = None) -> dict[str, Any]:
    return dump_registry(_read_registry_json(path))


def load_catalog(path: str | Path | None = None) -> tuple[dict[str, ModelSpec], dict[str, ProxySpec]]:
    return normalize_registry(_read_registry_json(path))


def load_models(path: str | Path | None = None) -> dict[str, Any]:
    models, _proxies = load_catalog(path)
    return {key: spec.raw for key, spec in models.items()}


def load_specs(path: str | Path | None = None) -> dict[str, ModelSpec]:
    models, _proxies = load_catalog(path)
    return models


def load_proxies(path: str | Path | None = None) -> dict[str, ProxySpec]:
    _models, proxies = load_catalog(path)
    return proxies


def load_env_file(path: Path, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Read literal assignments; exported environment always wins. Never execute Shell."""
    values: dict[str, str] = {}
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            key, sep, value = line.partition("=")
            if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
                values[key.strip()] = value.strip().strip("\"'")
    values.update(os.environ if env is None else env)
    return values
