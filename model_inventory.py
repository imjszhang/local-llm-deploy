#!/usr/bin/env python3
"""
本地模型清单：详细列表、删除权重目录、manifest（并行下载 / register）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from typing import Any, Callable

from model_paths import (
    MODELS_DIR,
    MODELS_JSON,
    PROJECT_ROOT,
    chat_quant_dir,
    dir_has_chat_weights,
    dir_has_embedding_weights,
    dir_has_rerank_weights,
    dir_size_bytes,
    embedding_dir,
    format_size,
    is_model_downloaded,
    load_models,
    model_type,
    repo_name,
)

MANIFEST_NAME = ".manifest.json"


def manifest_file() -> str:
    return os.path.join(MODELS_DIR, MANIFEST_NAME)


def load_manifest() -> dict[str, Any]:
    p = manifest_file()
    if not os.path.isfile(p):
        return {"version": 1, "entries": []}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_manifest(data: dict[str, Any]) -> None:
    os.makedirs(MODELS_DIR, exist_ok=True)
    with open(manifest_file(), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def record_download_path(model_key: str, quant: str | None, abs_path: str) -> None:
    """下载成功后登记路径（相对项目根目录），用于 models 列表展示。"""
    try:
        root = os.path.realpath(PROJECT_ROOT)
        ap = os.path.realpath(abs_path)
        rel = os.path.relpath(ap, root)
    except (OSError, ValueError):
        return
    if rel.startswith(".."):
        return
    data = load_manifest()
    entries: list[dict[str, Any]] = data.setdefault("entries", [])
    for e in entries:
        if e.get("model_key") == model_key and e.get("path") == rel:
            e["quant"] = quant
            e["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            save_manifest(data)
            return
    entries.append(
        {
            "model_key": model_key,
            "quant": quant,
            "path": rel,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
    )
    save_manifest(data)


def remove_manifest_paths_predicate(
    predicate: Callable[[dict[str, Any]], bool],
) -> None:
    data = load_manifest()
    entries = data.get("entries", [])
    kept = [e for e in entries if not predicate(e)]
    if len(kept) != len(entries):
        data["entries"] = kept
        save_manifest(data)


def _models_realpath() -> str:
    return os.path.realpath(MODELS_DIR)


def _is_under_models(path: str) -> bool:
    try:
        rp = os.path.realpath(path)
        base = _models_realpath()
        return rp == base or rp.startswith(base + os.sep)
    except OSError:
        return False


def model_pid_running(model_key: str) -> bool:
    pid_file = os.path.join(PROJECT_ROOT, "run", f"{model_key}.pid")
    if not os.path.isfile(pid_file):
        return False
    try:
        with open(pid_file, encoding="utf-8") as f:
            line = f.readline().strip()
        pid = int(line)
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def cmd_is_downloaded(model_key: str) -> int:
    models = load_models()
    ok = is_model_downloaded(model_key, models)
    return 0 if ok else 1


def cmd_list() -> None:
    models = load_models()
    mf = load_manifest()
    entries_by_model: dict[str, list[dict[str, Any]]] = {}
    for e in mf.get("entries", []):
        mk = e.get("model_key")
        if not mk:
            continue
        entries_by_model.setdefault(mk, []).append(e)

    print("========================================")
    print("  本地模型详情（推导路径 + manifest）")
    print("========================================")
    print("")

    for name in sorted(models.keys()):
        cfg = models[name]
        mt = model_type(cfg)
        print(f"【{name}】 类型: {mt}")

        if mt == "external":
            print("  (external，无本地权重目录)")
            print("")
            continue

        if mt == "embedding":
            ed = embedding_dir(cfg)
            ok = dir_has_embedding_weights(ed)
            sz = dir_size_bytes(ed) if ok else 0
            print(f"  目录: {ed}")
            print(f"  状态: {'已就绪' if ok else '未就绪'}  体积: {format_size(sz)}")
            for e in entries_by_model.get(name, []):
                p = os.path.join(PROJECT_ROOT, e["path"])
                ex = os.path.isdir(p)
                print(
                    f"  manifest: {e['path']}  quant={e.get('quant')}  "
                    f"{'存在' if ex else '缺失'}"
                )
            print("")
            continue

        if mt == "rerank":
            rd = embedding_dir(cfg)
            ok = dir_has_rerank_weights(rd)
            sz = dir_size_bytes(rd) if ok else 0
            print(f"  目录: {rd}")
            print(f"  状态: {'已就绪' if ok else '未就绪'}  体积: {format_size(sz)}")
            for e in entries_by_model.get(name, []):
                p = os.path.join(PROJECT_ROOT, e["path"])
                ex = os.path.isdir(p)
                print(
                    f"  manifest: {e['path']}  quant={e.get('quant')}  "
                    f"{'存在' if ex else '缺失'}"
                )
            print("")
            continue

        quants = cfg.get("quants") or {}
        default_q = cfg.get("default_quant") or ""
        for q in sorted(quants.keys()):
            qdir = chat_quant_dir(cfg, q)
            ok = dir_has_chat_weights(qdir)
            sz = dir_size_bytes(qdir) if ok else 0
            mark = " [默认]" if q == default_q else ""
            print(f"  量化 {q}{mark}: {'已就绪' if ok else '未就绪'}  {format_size(sz)}")
            print(f"         {qdir}")

        for e in entries_by_model.get(name, []):
            p = os.path.join(PROJECT_ROOT, e["path"])
            ex = os.path.isdir(p)
            mq = e.get("quant")
            print(
                f"  manifest: {e['path']}  quant={mq}  "
                f"{'存在' if ex else '缺失'}"
            )
        print("")

    print("提示: 简略视图请用 ./manage.sh list；删除权重: ./manage.sh remove <模型> ...")


def safe_rmtree(target: str, *, force: bool, model_key: str) -> None:
    if not target or not os.path.isdir(target):
        print(f"目录不存在，跳过: {target}", file=sys.stderr)
        return
    if not _is_under_models(target):
        print(f"拒绝: 路径不在 models/ 下: {target}", file=sys.stderr)
        sys.exit(1)
    if not force and model_pid_running(model_key):
        print(
            f"拒绝: {model_key} 似乎在运行中（见 run/{model_key}.pid）。请先 stop 或使用 --force。",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"删除目录: {target}")
    shutil.rmtree(target)


def cmd_remove(args: argparse.Namespace) -> None:
    models = load_models()
    model_key = args.model
    cfg = models.get(model_key)
    if not cfg:
        print(f"未知模型: {model_key}", file=sys.stderr)
        sys.exit(1)
    mt = model_type(cfg)
    force = bool(args.force)

    if mt == "external":
        print("external 模型无本地下载目录。", file=sys.stderr)
        sys.exit(1)

    if mt == "embedding" or mt == "rerank":
        safe_rmtree(embedding_dir(cfg), force=force, model_key=model_key)
        remove_manifest_paths_predicate(lambda e: e.get("model_key") == model_key)
        return

    quants_cfg = cfg.get("quants") or {}
    if args.all:
        for q in sorted(quants_cfg.keys()):
            safe_rmtree(chat_quant_dir(cfg, q), force=force, model_key=model_key)
        remove_manifest_paths_predicate(lambda e: e.get("model_key") == model_key)
        return

    if args.quant:
        if args.quant not in quants_cfg:
            print(f"未知量化: {args.quant}，可选: {list(quants_cfg.keys())}", file=sys.stderr)
            sys.exit(1)
        removed_root = os.path.realpath(chat_quant_dir(cfg, args.quant))
        safe_rmtree(removed_root, force=force, model_key=model_key)

        def match_ent(e: dict[str, Any], rp: str = removed_root) -> bool:
            if e.get("model_key") != model_key:
                return False
            p = e.get("path")
            if not p:
                return False
            try:
                return os.path.realpath(os.path.join(PROJECT_ROOT, p)) == rp
            except OSError:
                return False

        remove_manifest_paths_predicate(match_ent)
        return

    print(
        "对话模型请指定 --quant <量化名> 或 --all。示例: ./manage.sh remove qwen3.5 --quant UD-Q2_K_XL",
        file=sys.stderr,
    )
    sys.exit(1)


def cmd_register(args: argparse.Namespace) -> None:
    models = load_models()
    model_key = args.model
    cfg = models.get(model_key)
    if not cfg:
        print(f"未知模型: {model_key}", file=sys.stderr)
        sys.exit(1)
    mt = model_type(cfg)
    raw_path = args.path
    if ".." in raw_path:
        print("路径不允许包含 '..'", file=sys.stderr)
        sys.exit(1)
    abs_path = (
        os.path.realpath(raw_path)
        if os.path.isabs(raw_path)
        else os.path.realpath(os.path.join(PROJECT_ROOT, raw_path))
    )
    if not _is_under_models(abs_path):
        print("路径必须在 models/ 目录下。", file=sys.stderr)
        sys.exit(1)
    if not os.path.isdir(abs_path):
        print(f"不是目录: {abs_path}", file=sys.stderr)
        sys.exit(1)

    quant = args.quant or None
    if mt == "embedding" or mt == "rerank":
        quant = None
        ok_fn = dir_has_rerank_weights if mt == "rerank" else dir_has_embedding_weights
        if not ok_fn(abs_path):
            print("目录内未发现完整权重，仍登记 manifest。", file=sys.stderr)
    else:
        if not quant:
            print("对话模型请提供 --quant <量化名>", file=sys.stderr)
            sys.exit(1)
        if quant not in (cfg.get("quants") or {}):
            print(f"未知量化: {quant}", file=sys.stderr)
            sys.exit(1)
        if not dir_has_chat_weights(abs_path):
            print("目录内未发现 .gguf（maxdepth 2），仍登记 manifest。", file=sys.stderr)

    record_download_path(model_key, quant, abs_path)
    print(f"已登记: {model_key} -> {os.path.relpath(abs_path, PROJECT_ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="本地模型清单")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dl = sub.add_parser("is-downloaded", help="检查默认量化/embedding 是否就绪（退出码 0/1）")
    p_dl.add_argument("model")

    p_ls = sub.add_parser("list", help="列出各量化目录与 manifest")

    p_rm = sub.add_parser("remove", help="删除本地权重目录")
    p_rm.add_argument("model")
    p_rm.add_argument("--quant", default="")
    p_rm.add_argument("--all", action="store_true")
    p_rm.add_argument("--force", action="store_true")

    p_reg = sub.add_parser("register", help="将自定义路径写入 manifest")
    p_reg.add_argument("model")
    p_reg.add_argument("--path", required=True)
    p_reg.add_argument("--quant", default="")

    ns = parser.parse_args()
    if ns.cmd == "is-downloaded":
        sys.exit(cmd_is_downloaded(ns.model))
    if ns.cmd == "list":
        cmd_list()
        return
    if ns.cmd == "remove":
        cmd_remove(ns)
        return
    if ns.cmd == "register":
        cmd_register(ns)
        return


if __name__ == "__main__":
    main()
