#!/usr/bin/env python3
"""
统一模型下载：依据 models.json 支持对话模型（GGUF）、Embedding（safetensors），以及可选的 repo 根目录布局。

用法:
  ./download.sh <模型键名> [--quant Q] [--source modelscope|huggingface] [--to <路径>]
  # 或与 manage.sh download 相同参数

环境:
  DOWNLOAD_SOURCE — 全局默认源（models.json 未写 download_source 且命令行未指定 --source 时为 modelscope）
  HF_ENDPOINT — Hugging Face Hub API；走 Hugging Face 下载且未设置时默认为 https://hf-mirror.com

可选 models.json 字段:
  download_source: \"modelscope\" | \"huggingface\" — 该模型的默认下载源（命令行 --source 优先）
  hf_revision / revision: 字符串 — 仅在 Hugging Face 分支传入 snapshot_download(revision=...)
  download_target_mode（对话）: \"quant_subdir\"（默认）| \"repo_root\"
    — repo_root 时文件落到 models/<repo_name>/，再由量化 patterns 带子路径（如 BF16/*）。

对话模型 quants 每项: \"pattern\"（单 glob）或 \"patterns\"（glob 列表，可同时匹配 GGUF 与 mmproj 等）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from model_paths import (
    MODELS_DIR,
    MODELS_JSON,
    PROJECT_ROOT,
    download_target_mode,
    load_models,
    repo_name,
    resolve_target_dir,
)


def load_hf_env() -> None:
    path = os.path.join(PROJECT_ROOT, ".hf-env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key:
                os.environ[key] = val


def ensure_import(
    pip_packages: list[str],
    module: str,
    *,
    upgrade: bool = False,
) -> None:
    try:
        __import__(module)
    except ImportError:
        print(f"正在安装: {' '.join(pip_packages)}")
        cmd = [sys.executable, "-m", "pip", "install", "-q"]
        if upgrade:
            cmd.append("-U")
        cmd.extend(pip_packages)
        subprocess.check_call(cmd, cwd=PROJECT_ROOT)


def apply_hf_endpoint_default() -> None:
    if os.environ.get("HF_ENDPOINT"):
        return
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
    print(
        f"\033[0;36mHF_ENDPOINT 未设置，已使用默认镜像: {os.environ['HF_ENDPOINT']}"
        "（官方 Hub: export HF_ENDPOINT=https://huggingface.co）\033[0m"
    )


def _hf_revision_kwargs(cfg: dict) -> dict:
    rev = cfg.get("hf_revision") or cfg.get("revision")
    if rev:
        return {"revision": str(rev)}
    return {}


def configure_hf_transfer() -> None:
    ep = os.environ.get("HF_ENDPOINT", "")
    if not ep:
        return
    print(f"HF_ENDPOINT: {ep}")
    if ep.rstrip("/") == "https://huggingface.co":
        return
    v = os.environ.get("HF_HUB_ENABLE_HF_TRANSFER", "")
    if v.lower() in ("1", "true", "yes"):
        return
    os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"


def print_footer_chat(model_name: str, target_dir: str, to_path: str | None) -> None:
    print("")
    print("========================================")
    print("下载完成！文件列表：")
    print("========================================")
    for root, _, files in os.walk(target_dir):
        for fn in files:
            if fn.endswith(".gguf"):
                fp = os.path.join(root, fn)
                try:
                    st = os.stat(fp)
                    sz = st.st_size
                    hs = f"{sz / (1024 ** 3):.2f}G" if sz >= 1024 ** 3 else f"{sz / (1024 ** 2):.1f}M"
                except OSError:
                    hs = "?"
                print(f"  {hs:>8}  {fp}")
    print("")
    print("部署示例:")
    if to_path:
        print(f"  # 下载期间默认目录未动，继续用: ./manage.sh start {model_name}")
        print("  # 新版就绪后切到新目录（建议先 stop，再启新路径；或换端口见 DEPLOY.md）:")
        print(
            f'  ./deploy.sh --model-name {model_name} --model-dir "{target_dir}" --port <端口>'
        )
        print(
            f'  ./manage.sh start {model_name} --model-dir "{target_dir}" --port <端口>'
        )
    else:
        print(f"  ./manage.sh start {model_name}")


def print_footer_embedding(target_dir: str) -> None:
    n = sum(
        1
        for fn in os.listdir(target_dir)
        if fn.endswith(".safetensors") and os.path.isfile(os.path.join(target_dir, fn))
    )
    print("")
    print("=" * 50)
    print(f"下载完成！共 {n} 个顶层 safetensors 文件")
    print(f"路径: {target_dir}")
    print("=" * 50)


def _resolve_chat_allow_patterns(cfg: dict, qinfo: dict) -> list[str]:
    if cfg.get("download_allow_patterns") is not None:
        raw = cfg["download_allow_patterns"]
        return raw if isinstance(raw, list) else [raw]
    raw = qinfo.get("patterns")
    if raw is not None:
        return raw if isinstance(raw, list) else [raw]
    pat = qinfo.get("pattern")
    if pat is None:
        print("错误: 量化配置须包含 pattern 或 patterns", file=sys.stderr)
        sys.exit(1)
    return [pat]


def download_chat(
    model_name: str,
    cfg: dict,
    quant: str,
    source: str,
    to_path: str | None,
) -> None:
    quants = cfg.get("quants") or {}
    qinfo = quants.get(quant)
    if not qinfo:
        print(f"可用量化版本: {list(quants.keys())}", file=sys.stderr)
        sys.exit(1)
    patterns = _resolve_chat_allow_patterns(cfg, qinfo)
    repo_id = cfg["repo_id"]
    mode = download_target_mode(cfg)
    rn = repo_name(cfg)
    try:
        target_dir = resolve_target_dir(
            repo_name_val=rn,
            quant=quant,
            to_path=to_path,
            download_target_mode_val=mode,
        )
    except ValueError as e:
        print(f"\033[0;31m错误: {e}\033[0m", file=sys.stderr)
        sys.exit(1)

    src_label = "ModelScope 魔搭" if source == "modelscope" else "HuggingFace"
    print("========================================")
    print(f"模型下载: {model_name}")
    print("========================================")
    print(f"下载源:    {src_label}")
    print(f"仓库:      {repo_id}")
    print(f"量化版本:  {quant}")
    print(f"匹配模式:  {', '.join(patterns)}")
    print(f"目标目录:  {target_dir}")
    if to_path:
        print(f"\033[0;36m并行下载:\033[0m  默认目录 {os.path.join(MODELS_DIR, rn, quant)} 不会被写入")
    print(f"Python:    {sys.executable}")
    print("========================================")

    os.makedirs(target_dir, exist_ok=True)

    if source == "modelscope":
        ensure_import(["modelscope"], "modelscope")
        from modelscope import snapshot_download

        print(f"开始下载 {repo_id} (模式: {patterns}) [ModelScope]...")
        snapshot_download(
            repo_id,
            cache_dir=os.path.join(MODELS_DIR, ".cache"),
            local_dir=target_dir,
            allow_patterns=patterns,
        )
    else:
        apply_hf_endpoint_default()
        ensure_import(["huggingface_hub", "hf_transfer"], "huggingface_hub", upgrade=True)
        configure_hf_transfer()
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "0")
        from huggingface_hub import snapshot_download

        hf_xtra = _hf_revision_kwargs(cfg)
        print(f"开始下载 {repo_id} (模式: {patterns}) [HuggingFace]...")
        snapshot_download(
            repo_id=repo_id,
            local_dir=target_dir,
            allow_patterns=patterns,
            **hf_xtra,
        )

    try:
        from model_inventory import record_download_path

        record_download_path(model_name, quant, target_dir)
    except Exception:
        pass

    print_footer_chat(model_name, target_dir, to_path)


def download_embedding(
    model_name: str,
    cfg: dict,
    source: str,
    to_path: str | None,
) -> None:
    repo_id = cfg["repo_id"]
    rn = repo_name(cfg)
    try:
        target_dir = resolve_target_dir(
            repo_name_val=rn,
            quant=None,
            to_path=to_path,
            download_target_mode_val="quant_subdir",
        )
    except ValueError as e:
        print(f"\033[0;31m错误: {e}\033[0m", file=sys.stderr)
        sys.exit(1)
    patterns = cfg.get("download_allow_patterns")

    print("=" * 50)
    print(f"{model_name} 下载 ({source})")
    print("=" * 50)
    print(f"仓库:     {repo_id}")
    print(f"目标目录: {target_dir}")
    print("=" * 50)

    os.makedirs(target_dir, exist_ok=True)

    if source == "modelscope":
        ensure_import(["modelscope"], "modelscope")
        from modelscope import snapshot_download

        kw: dict = {
            "cache_dir": os.path.join(MODELS_DIR, ".cache"),
            "local_dir": target_dir,
        }
        if patterns is not None:
            kw["allow_patterns"] = patterns
        snapshot_download(repo_id, **kw)
    else:
        apply_hf_endpoint_default()
        ensure_import(["huggingface_hub", "hf_transfer"], "huggingface_hub", upgrade=True)
        configure_hf_transfer()
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "0")
        from huggingface_hub import snapshot_download

        kw2: dict = {"local_dir": target_dir, **_hf_revision_kwargs(cfg)}
        if patterns is not None:
            kw2["allow_patterns"] = patterns
        snapshot_download(repo_id=repo_id, **kw2)

    try:
        from model_inventory import record_download_path

        record_download_path(model_name, None, target_dir)
    except Exception:
        pass

    print_footer_embedding(target_dir)
    print("")
    if cfg.get("type") == "rerank":
        print("使用示例:")
        print("  ./manage.sh start jina-rerank-mlx")
        print('  curl -X POST http://127.0.0.1:8006/v1/rerank -H "Content-Type: application/json" \\')
        print('    -d \'{"model":"jina-reranker-v3","query":"你的问题","documents":["文档1","文档2"]}\'')
        return
    if cfg.get("type") == "asr":
        port = cfg.get("default_port") or 8007
        alias = cfg.get("alias") or model_name
        print("使用示例:")
        print(f"  ./manage.sh start {model_name}")
        print(
            f'  curl -X POST http://127.0.0.1:{port}/v1/audio/transcriptions \\'
        )
        print(f'    -F file=@audio.mp3 -F model={alias} -F language=zh')
        return
    print("使用示例 (Python):")
    print("  from sentence_transformers import SentenceTransformer")
    print(f'  model = SentenceTransformer("{target_dir}")')
    print("  embeddings = model.encode(['文本1', '文本2'])")


def print_usage_list(models: dict) -> None:
    print(
        "用法: download_model.py <模型名> [--quant X] [--source modelscope|huggingface] [--to <路径>]"
    )
    print("")
    print("可用模型:")
    for name, cfg in models.items():
        if cfg.get("type") == "embedding":
            ds = cfg.get("download_source") or ""
            tag = f"  default_hub={ds}" if ds else ""
            print(f"  {name:15s} 类型: embedding（safetensors）{tag}")
            continue
        if cfg.get("type") == "rerank":
            ds = cfg.get("download_source") or ""
            tag = f"  default_hub={ds}" if ds else ""
            print(f"  {name:15s} 类型: rerank（MLX safetensors）{tag}")
            continue
        if cfg.get("type") == "asr":
            ds = cfg.get("download_source") or ""
            tag = f"  default_hub={ds}" if ds else ""
            print(f"  {name:15s} 类型: asr（MLX Whisper）{tag}")
            continue
        if cfg.get("type") == "external":
            print(f"  {name:15s} 类型: external（不可 download）")
            continue
        quants = ", ".join((cfg.get("quants") or {}).keys())
        dq = cfg.get("default_quant") or ""
        ds = cfg.get("download_source") or ""
        hub = f"  hub={ds}" if ds else ""
        print(f"  {name:15s} 量化: {quants}  (默认: {dq}){hub}")


def main() -> None:
    load_hf_env()
    parser = argparse.ArgumentParser(description="统一模型下载（models.json）")
    parser.add_argument("model_name", nargs="?")
    parser.add_argument("--quant", default="")
    parser.add_argument(
        "--source",
        choices=("modelscope", "huggingface"),
        default=None,
        help="覆盖 models.json 中的 download_source / 环境变量 DOWNLOAD_SOURCE",
    )
    parser.add_argument("--to", dest="to_path", default="")
    args = parser.parse_args()

    if not args.model_name:
        models = load_models()
        print_usage_list(models)
        sys.exit(1)

    models = load_models()
    cfg = models.get(args.model_name)
    if not cfg:
        print(f"\033[0;31m错误: 未知模型 '{args.model_name}'\033[0m", file=sys.stderr)
        sys.exit(1)

    mtype = cfg.get("type") or "chat"
    to_path = args.to_path.strip() or None

    src = args.source or cfg.get("download_source") or os.environ.get(
        "DOWNLOAD_SOURCE", "modelscope"
    )
    if src not in ("modelscope", "huggingface"):
        print(
            f"\033[0;31m错误: 无效下载源 {src!r}（须为 modelscope / huggingface）\033[0m",
            file=sys.stderr,
        )
        sys.exit(1)

    if mtype == "external":
        print(
            f"\033[0;31m错误: {args.model_name} 为外部推理进程，勿使用本仓库 download\033[0m",
            file=sys.stderr,
        )
        hint = cfg.get("startup_hint")
        if hint:
            print(hint, file=sys.stderr)
        sys.exit(1)

    if mtype == "embedding" or mtype == "rerank" or mtype == "asr":
        if args.quant:
            print(
                "\033[0;31m错误: embedding/rerank/asr 模型不支持 --quant\033[0m",
                file=sys.stderr,
            )
            sys.exit(1)
        download_embedding(args.model_name, cfg, src, to_path)
        return

    quants = cfg.get("quants") or {}
    if not quants:
        print(
            "\033[0;31m错误: 对话模型缺少 quants 配置\033[0m",
            file=sys.stderr,
        )
        sys.exit(1)

    quant = args.quant or cfg.get("default_quant")
    if not quant:
        print("\033[0;31m错误: 未指定量化且 models.json 无 default_quant\033[0m", file=sys.stderr)
        sys.exit(1)

    download_chat(args.model_name, cfg, quant, src, to_path)


if __name__ == "__main__":
    main()