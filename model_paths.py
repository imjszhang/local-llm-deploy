"""
models.json 约定的本地路径与权重检测（与 download_model / deploy 对齐）。
"""
from __future__ import annotations

import json
import os
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(PROJECT_ROOT, "models")
MODELS_JSON = os.path.join(PROJECT_ROOT, "models.json")


def load_models(path: str | None = None) -> dict[str, Any]:
    p = path or MODELS_JSON
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def repo_name(cfg: dict[str, Any]) -> str:
    return cfg.get("repo_name") or cfg["repo_id"].replace("/", "-")


def download_target_mode(cfg: dict[str, Any]) -> str:
    return cfg.get("download_target_mode", "quant_subdir")


def resolve_target_dir(
    *,
    repo_name_val: str,
    quant: str | None,
    to_path: str | None,
    download_target_mode_val: str,
) -> str:
    """下载落盘目录（与 download_model 一致）。to_path 含 '..' 时抛出 ValueError。"""
    if to_path:
        if ".." in to_path:
            raise ValueError("--to 路径中不允许 '..'")
        if os.path.isabs(to_path):
            return to_path
        return os.path.join(MODELS_DIR, to_path)
    if download_target_mode_val == "repo_root":
        return os.path.join(MODELS_DIR, repo_name_val)
    if quant is None:
        return os.path.join(MODELS_DIR, repo_name_val)
    return os.path.join(MODELS_DIR, repo_name_val, quant)


def chat_quant_dir(cfg: dict[str, Any], quant: str) -> str:
    """对话模型某量化在磁盘上的期望目录（deploy 默认 MODEL_DIR）。"""
    return os.path.join(MODELS_DIR, repo_name(cfg), quant)


def embedding_dir(cfg: dict[str, Any]) -> str:
    return os.path.join(MODELS_DIR, repo_name(cfg))


def repo_root_dir(cfg: dict[str, Any]) -> str:
    return os.path.join(MODELS_DIR, repo_name(cfg))


def count_gguf_maxdepth(path: str, max_depth: int = 2) -> int:
    """在 path 下统计 .gguf，子目录深度相对 path 不超过 max_depth（与 manage.sh find -maxdepth 2 对齐）。"""
    if not os.path.isdir(path):
        return 0
    n = 0
    base = os.path.realpath(path)
    for root, dirs, files in os.walk(base):
        rel = os.path.relpath(root, base)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth > max_depth:
            dirs[:] = []
            continue
        for fn in files:
            if fn.endswith(".gguf"):
                n += 1
        if depth >= max_depth:
            dirs[:] = []
    return n


def dir_has_chat_weights(path: str) -> bool:
    return count_gguf_maxdepth(path, 2) > 0


def dir_has_embedding_weights(path: str) -> bool:
    """与 manage.sh is_downloaded embedding/rerank 一致：仅顶层 .safetensors。"""
    if not os.path.isdir(path):
        return False
    try:
        for fn in os.listdir(path):
            if fn.endswith(".safetensors") and os.path.isfile(
                os.path.join(path, fn)
            ):
                return True
    except OSError:
        return False
    return False


def dir_has_rerank_weights(path: str) -> bool:
    """MLX rerank 需 model.safetensors + projector.safetensors + rerank.py。"""
    if not os.path.isdir(path):
        return False
    required = ("model.safetensors", "projector.safetensors", "rerank.py")
    return all(os.path.isfile(os.path.join(path, name)) for name in required)


def dir_size_bytes(path: str) -> int:
    if not os.path.isdir(path):
        return 0
    total = 0
    for root, _, files in os.walk(path):
        for fn in files:
            fp = os.path.join(root, fn)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def format_size(num_bytes: int) -> str:
    if num_bytes >= 1024**4:
        return f"{num_bytes / (1024 ** 4):.2f}T"
    if num_bytes >= 1024**3:
        return f"{num_bytes / (1024 ** 3):.2f}G"
    if num_bytes >= 1024**2:
        return f"{num_bytes / (1024 ** 2):.1f}M"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.1f}K"
    return f"{num_bytes}B"


def model_type(cfg: dict[str, Any]) -> str:
    return cfg.get("type") or "chat"


def is_model_downloaded(model_key: str, models: dict[str, Any] | None = None) -> bool:
    """与 manage.sh is_downloaded 语义对齐（默认量化 / embedding）。"""
    data = models if models is not None else load_models()
    cfg = data.get(model_key)
    if not cfg:
        return False
    mt = model_type(cfg)
    if mt == "external":
        return True
    if mt == "embedding":
        return dir_has_embedding_weights(embedding_dir(cfg))
    if mt == "rerank":
        return dir_has_rerank_weights(embedding_dir(cfg))
    dq = cfg.get("default_quant")
    if not dq:
        return False
    return dir_has_chat_weights(chat_quant_dir(cfg, dq))
