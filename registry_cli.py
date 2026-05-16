#!/usr/bin/env python3
"""models.json 注册表：初始化、列出、展示、合并、删除顶层条目。"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
MODELS_JSON = os.path.join(PROJECT_ROOT, "models.json")
MODELS_JSON_EXAMPLE = os.path.join(PROJECT_ROOT, "models.json.example")


def sanitize_top_level(data: dict[str, Any]) -> dict[str, Any]:
    """忽略以下划线开头的键（仅供本地备注）。"""
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


def load_registry(path: str | None = None) -> dict[str, Any]:
    p = path or MODELS_JSON
    if not os.path.isfile(p):
        print(f"错误: 文件不存在: {p}", file=sys.stderr)
        print("先执行: ./manage.sh registry init", file=sys.stderr)
        sys.exit(1)
    with open(p, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        print("错误: models.json 顶层必须是 JSON 对象", file=sys.stderr)
        sys.exit(1)
    return sanitize_top_level(raw)


def save_registry(data: dict[str, Any]) -> None:
    clean = sanitize_top_level(data)
    tmp = MODELS_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, MODELS_JSON)


def cmd_init(args: argparse.Namespace) -> None:
    if not os.path.isfile(MODELS_JSON_EXAMPLE):
        print(f"错误: 缺少模板 {MODELS_JSON_EXAMPLE}", file=sys.stderr)
        sys.exit(1)
    if os.path.isfile(MODELS_JSON) and not args.force:
        print(
            f"错误: {MODELS_JSON} 已存在。若要覆盖请加 --force",
            file=sys.stderr,
        )
        sys.exit(1)
    shutil.copyfile(MODELS_JSON_EXAMPLE, MODELS_JSON)
    print(f"已从模板生成: {MODELS_JSON}")


def cmd_list(_args: argparse.Namespace) -> None:
    data = load_registry()
    print("已注册的模型键（models.json 顶层）:")
    for k in sorted(data.keys()):
        cfg = data[k]
        mt = cfg.get("type") or "chat"
        port = cfg.get("default_port", "?")
        alias = cfg.get("alias", "")
        extra = f"  alias={alias}" if alias else ""
        print(f"  {k:20s}  type={mt:10s}  port={port}{extra}")


def cmd_show(args: argparse.Namespace) -> None:
    data = load_registry()
    if args.key:
        if args.key not in data:
            print(f"错误: 无此键: {args.key}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(data[args.key], indent=2, ensure_ascii=False))
        return
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_merge(args: argparse.Namespace) -> None:
    path = args.file
    if not os.path.isfile(path):
        print(f"错误: 文件不存在: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        patch = json.load(f)
    if not isinstance(patch, dict):
        print("错误: 合并文件顶层必须是 JSON 对象", file=sys.stderr)
        sys.exit(1)
    patch = sanitize_top_level(patch)
    data = load_registry()
    for k, v in patch.items():
        if not isinstance(v, dict):
            print(f"错误: 键 {k!r} 的值必须是对象", file=sys.stderr)
            sys.exit(1)
        data[k] = v
    save_registry(data)
    print(f"已合并 {len(patch)} 个顶层键 -> {MODELS_JSON}")


def cmd_remove(args: argparse.Namespace) -> None:
    data = load_registry()
    key = args.key
    if key not in data:
        print(f"错误: 无此键: {key}", file=sys.stderr)
        sys.exit(1)
    del data[key]
    save_registry(data)
    print(f"已删除键: {key}")


def main() -> None:
    parser = argparse.ArgumentParser(description="models.json 注册表管理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="从 models.json.example 生成 models.json")
    p_init.add_argument(
        "--force",
        action="store_true",
        help="覆盖已存在的 models.json",
    )
    p_init.set_defaults(func=cmd_init)

    p_list = sub.add_parser("list", help="列出所有模型键")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="打印 JSON（可选单个键）")
    p_show.add_argument("key", nargs="?")
    p_show.set_defaults(func=cmd_show)

    p_merge = sub.add_parser(
        "merge",
        help="将 JSON 文件中的顶层键合并写入 models.json（同键覆盖）",
    )
    p_merge.add_argument("file")
    p_merge.set_defaults(func=cmd_merge)

    p_rm = sub.add_parser("remove", help="删除顶层模型键")
    p_rm.add_argument("key")
    p_rm.set_defaults(func=cmd_remove)

    ns = parser.parse_args()
    ns.func(ns)


if __name__ == "__main__":
    main()
