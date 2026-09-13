"""Shared installation selection, including legacy nested GGUF layouts."""
from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re

from ..config import ConfigError, ProjectPaths, normalize_models, project_paths
from ..domain import ModelInstallation, ModelSpec
from ..storage import file_lock


@contextlib.contextmanager
def installation_lock(paths, target):
    target = Path(target).resolve()
    try:
        relative = target.relative_to(paths.models.resolve())
        # A repository-root download and a quant-subdirectory start must share a lock.
        target = paths.models.resolve() / relative.parts[0]
    except (ValueError, IndexError):
        pass
    identity = hashlib.sha256(str(target).encode()).hexdigest()
    with file_lock(paths.run / ".artifacts.lock", shared=True):
        with file_lock(paths.run / "artifact-locks" / (identity + ".lock")):
            yield


def repo_name(cfg):
    name = cfg.get("repo_name") or str(cfg.get("repo_id", "")).replace("/", "-")
    if not name or Path(name).name != name or name in (".", ".."):
        raise ConfigError("本地模型必须声明有效 repo_id 或 repo_name")
    return name


def selected_quant(spec, quant=None):
    chosen = quant or spec.raw.get("default_quant")
    if spec.backend != "llama_cpp":
        if quant:
            raise ConfigError(f"{spec.key} 不支持量化选择")
        return None
    if not chosen or chosen not in spec.raw.get("quants", {}):
        raise ConfigError(f"{spec.key}: 请指定已注册量化（--quant）")
    if Path(chosen).name != chosen or chosen in (".", ".."):
        raise ConfigError("量化名不能包含目录分隔符")
    return chosen


def default_path(spec, paths, quant=None):
    quant = selected_quant(spec, quant)
    base = paths.models / repo_name(spec.raw)
    return base / quant if quant and spec.raw.get("download_target_mode") != "repo_root" else base


def gguf_files(path):
    p = Path(path)
    if not p.is_dir():
        return []
    candidates = sorted(f for f in p.glob("*.gguf") if "mmproj" not in f.name.lower())
    if not candidates:
        candidates = sorted(f for f in p.glob("*/*.gguf") if "mmproj" not in f.name.lower())
    return [f for f in candidates if f.is_file()]


def chat_weights_complete(path):
    return _shards_complete(gguf_files(path))


def _shards_complete(files):
    if not files:
        return False
    names = {f.name for f in files}
    for f in files:
        match = re.fullmatch(r"(.+)-(\d{5})-of-(\d{5})\.gguf", f.name)
        if match:
            stem, _, total = match.groups()
            if not all(f"{stem}-{i:05d}-of-{total}.gguf" in names for i in range(1, int(total) + 1)):
                return False
    return True


def model_files(spec, path, quant=None):
    """Select only the requested GGUF variant, never an unrelated quant or projector."""
    quant = quant or spec.raw.get("default_quant")
    path = Path(path)
    files = gguf_files(path)
    qinfo = spec.raw.get("quants", {}).get(quant, {})
    patterns = qinfo.get("patterns") or qinfo.get("pattern")
    if isinstance(patterns, str):
        patterns = [patterns]
    if patterns:
        selected = []
        for file in files:
            relative = str(file.relative_to(path))
            names = (relative, f"{path.name}/{relative}")
            if any(fnmatch.fnmatch(name, pattern) for name in names for pattern in patterns):
                selected.append(file)
        files = selected
    return files


def primary_model_file(spec, path, quant=None):
    files = model_files(spec, path, quant)
    if not _shards_complete(files):
        raise ConfigError("指定量化权重缺失或 GGUF 分片不完整")
    groups = {re.sub(r"-\d{5}-of-\d{5}\.gguf$", "", str(f)) if re.search(r"-\d{5}-of-\d{5}\.gguf$", f.name)
              else str(f) for f in files}
    if len(groups) != 1:
        raise ConfigError("匹配到多组 GGUF，请在注册表中缩小量化 pattern")
    return files[0]


def weights_complete(spec, path, quant=None):
    path = Path(path)
    if spec.management == "external":
        return False
    if spec.backend == "llama_cpp":
        return _shards_complete(model_files(spec, path, quant))
    if spec.backend == "mlx_rerank":
        return (all((path / f).is_file() for f in ("model.safetensors", "projector.safetensors", "rerank.py"))
                and _indexed_weights_complete(path))
    if spec.backend == "mlx_whisper":
        return ((path / "config.json").is_file()
                and bool(list(path.glob("*.npz")) + list(path.glob("*.safetensors")))
                and _indexed_weights_complete(path))
    return ((path / "config.json").is_file() and bool(list(path.glob("*.safetensors")))
            and _indexed_weights_complete(path))


def _indexed_weights_complete(path):
    """A partial snapshot with an index is incomplete until every named shard exists."""
    root = path.resolve()
    for index in path.glob("*.safetensors.index.json"):
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
            weights = data.get("weight_map") if isinstance(data, dict) else None
            if not isinstance(weights, dict) or not weights:
                return False
            for name in weights.values():
                if not isinstance(name, str) or not name:
                    return False
                shard = (root / name).resolve()
                if root not in shard.parents or not shard.is_file():
                    return False
        except (OSError, ValueError):
            return False
    return True


def installation_path(spec, path, quant=None):
    path = Path(path).expanduser().resolve()
    if quant and spec.raw.get("download_target_mode") == "repo_root" and (path / quant).is_dir():
        path /= quant
    return path


def resolve_installation(spec: ModelSpec, paths: ProjectPaths, quant=None, explicit=None):
    quant = selected_quant(spec, quant)
    revision = spec.raw.get("hf_revision") or spec.raw.get("revision")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = paths.root / path
    else:
        from .manifest import Manifest
        entries = Manifest(paths).entries(spec.key, quant)
        configured = spec.raw.get("model_path")
        if configured:
            path = Path(configured)
            if not path.is_absolute():
                path = paths.root / path
        elif entries:
            candidates = {str(Manifest(paths).absolute(e).resolve()): e for e in entries}
            if len(candidates) > 1:
                raise ConfigError(f"{spec.key}: 多份权重安装，请通过 --model-dir 明确选择: {', '.join(candidates)}")
            path = Path(next(iter(candidates)))
            entry = candidates[str(path)]
            revision = entry.get("revision") or revision
        else:
            path = default_path(spec, paths, quant)
    path = installation_path(spec, path, quant)
    return ModelInstallation(spec.key, path, quant, revision, weights_complete(spec, path, quant))


def dir_size_bytes(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def format_size(size):
    for exponent, suffix in ((4, "T"), (3, "G"), (2, "M"), (1, "K")):
        if size >= 1024 ** exponent:
            return f"{size / 1024 ** exponent:.2f}{suffix}"
    return f"{size}B"


def inside_models(path, paths, *, allow_root=False):
    target, base = Path(path).resolve(), paths.models.resolve()
    return (allow_root and target == base) or base in target.parents


def resolve_target_dir(*, repo_name_val, quant, to_path, download_target_mode_val, paths=None):
    paths = paths or project_paths()
    if to_path:
        target = Path(to_path).expanduser()
        target = target if target.is_absolute() else paths.models / target
    else:
        target = paths.models / repo_name_val
        if quant and download_target_mode_val != "repo_root":
            target /= quant
    if not inside_models(target, paths):
        raise ConfigError("下载目标必须在 models/ 的子目录中")
    return str(target.resolve())
