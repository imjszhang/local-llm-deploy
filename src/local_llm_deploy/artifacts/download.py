"""Explicit, reproducible download plans. Dependency installation is a separate step."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import importlib
import json
import os
from pathlib import Path
import sys

from ..config import ConfigError, load_env_file, load_specs, project_paths
from ..storage import file_lock
from .manifest import Manifest
from .paths import installation_lock, repo_name, resolve_target_dir, selected_quant, weights_complete


@dataclass(frozen=True)
class DownloadPlan:
    model_key: str
    repo_id: str
    source: str
    path: Path
    quant: str | None
    patterns: tuple[str, ...] | None
    revision: str | None


def plan_download(spec, paths, *, quant=None, source=None, to_path=None, revision=None, env=None):
    if spec.management == "external":
        raise ConfigError(f"{spec.key} 由外部服务管理，不能下载")
    env = load_env_file(paths.root / ".hf-env", env)
    source = source or env.get("DOWNLOAD_SOURCE") or spec.raw.get("download_source") or "modelscope"
    if source not in ("huggingface", "modelscope"):
        raise ConfigError(f"无效下载源: {source}")
    quant = selected_quant(spec, quant)
    cfg = spec.raw
    repo = cfg.get("repo_id")
    if not isinstance(repo, str) or not repo:
        raise ConfigError(f"{spec.key}: 缺少 repo_id")
    patterns = cfg.get("download_allow_patterns")
    if patterns is None and quant:
        qinfo = cfg["quants"][quant]
        patterns = qinfo.get("patterns") or qinfo.get("pattern")
        if not patterns:
            raise ConfigError(f"{spec.key}/{quant}: 缺少下载 patterns")
    if isinstance(patterns, str):
        patterns = [patterns]
    if patterns is not None and (not isinstance(patterns, list) or any(not isinstance(x, str) for x in patterns)):
        raise ConfigError("download_allow_patterns 必须是字符串列表")
    target = resolve_target_dir(repo_name_val=repo_name(cfg), quant=quant, to_path=to_path,
                                download_target_mode_val=cfg.get("download_target_mode", "quant_subdir"), paths=paths)
    return DownloadPlan(spec.key, repo, source, Path(target), quant,
                        tuple(patterns) if patterns else None,
                        revision or cfg.get("hf_revision") or cfg.get("revision"))


def execute_download(plan, spec, paths, *, env=None, downloader=None):
    env = load_env_file(paths.root / ".hf-env", env)
    with installation_lock(paths, plan.path):
        # Updating in-place weights used by a live instance is unsafe; --to creates an alternative.
        from .inventory import active_installations
        specs = load_specs(paths.registry)
        for key, used_paths in active_installations(paths, specs):
            for path in used_paths:
                if path == plan.path or path in plan.path.parents or plan.path in path.parents:
                    raise ConfigError(f"{key} 可能正在使用下载目标；请 stop 或指定 --to 新目录")
        if downloader is None:
            # Hub libraries may read these values at import time. Never change unrelated environment.
            for key in ("HF_TOKEN", "HF_ENDPOINT", "HF_HUB_ENABLE_HF_TRANSFER"):
                if key in env:
                    os.environ[key] = env[key]
            if plan.source == "huggingface":
                os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
                if os.environ["HF_ENDPOINT"].rstrip("/") != "https://huggingface.co":
                    os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
                module = "huggingface_hub"
            else:
                module = "modelscope"
            try:
                downloader = importlib.import_module(module).snapshot_download
            except ImportError as exc:
                raise ConfigError(f"缺少 {module}；请显式执行 python -m pip install -e '.[download]' 后重试") from exc
        kwargs = {"local_dir": str(plan.path)}
        if plan.patterns:
            kwargs["allow_patterns"] = list(plan.patterns)
        if plan.revision:
            kwargs["revision"] = plan.revision
        if plan.source == "modelscope":
            kwargs["cache_dir"] = str(paths.models / ".cache")
        plan.path.mkdir(parents=True, exist_ok=True)
        downloader(plan.repo_id, **kwargs)
        if not weights_complete(spec, plan.path, plan.quant):
            raise ConfigError(f"下载结果缺少完整权重: {plan.path}；未登记为成功安装")
        Manifest(paths).record(plan.model_key, plan.quant, plan.path,
                               revision=plan.revision, source=plan.source)
    return plan.path


def main(argv=None, *, paths=None):
    parser = argparse.ArgumentParser(description="模型下载")
    parser.add_argument("--project-root")
    parser.add_argument("model_name")
    parser.add_argument("--quant")
    parser.add_argument("--source", choices=("huggingface", "modelscope"))
    parser.add_argument("--to", dest="to_path")
    parser.add_argument("--revision")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    paths = paths or project_paths(args.project_root)
    try:
        spec = load_specs(paths.registry)[args.model_name]
        plan = plan_download(spec, paths, quant=args.quant, source=args.source,
                             to_path=args.to_path, revision=args.revision)
        print(json.dumps(asdict(plan), ensure_ascii=False, indent=2, default=str))
        if not args.dry_run:
            execute_download(plan, spec, paths)
            print(f"下载并登记完成: {plan.path}")
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
