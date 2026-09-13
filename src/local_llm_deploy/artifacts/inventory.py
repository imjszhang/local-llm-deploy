"""Weight inventory and guarded deletion using the shared installation resolver."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

from ..config import ConfigError, load_specs, project_paths
from ..storage import file_lock
from .manifest import Manifest
from .paths import default_path, dir_size_bytes, format_size, inside_models, installation_path, resolve_installation, selected_quant, weights_complete


def installations(spec, paths):
    if spec.management == "external":
        return []
    found = {}
    quants = list(spec.raw.get("quants", {})) if spec.backend == "llama_cpp" else [None]
    for quant in quants:
        path = installation_path(spec, default_path(spec, paths, quant), quant)
        found[(path, quant)] = {"model_key": spec.key, "quant": quant, "path": path, "source": "default"}
    manifest = Manifest(paths)
    for entry in manifest.entries(spec.key):
        path = installation_path(spec, manifest.absolute(entry), entry.get("quant"))
        found[(path, entry.get("quant"))] = dict(entry, path=path, source=entry.get("source", "manifest"))
    if spec.raw.get("model_path"):
        configured = Path(spec.raw["model_path"]).expanduser()
        configured = configured if configured.is_absolute() else paths.root / configured
        for quant in quants:
            path = installation_path(spec, configured, quant)
            found[(path, quant)] = {"model_key": spec.key, "quant": quant, "path": path, "source": "config"}
    for item in found.values():
        item["complete"] = weights_complete(spec, item["path"], item["quant"])
    return list(found.values())


def removal_plan(spec, paths, *, quant=None, all_quants=False, explicit=None):
    if spec.management == "external":
        raise ConfigError("外部模型没有本项目管理的权重")
    if spec.backend == "llama_cpp" and not (quant or all_quants or explicit):
        raise ConfigError("对话模型删除须指定 --quant 或 --all")
    if explicit:
        target = Path(explicit)
        target = target if target.is_absolute() else paths.root / target
        candidates = [target.resolve()]
        owned = {entry["path"].resolve() for entry in installations(spec, paths)}
        if target.resolve() not in owned:
            raise ConfigError("显式删除路径未登记到此模型；请先检查 models 清单")
    elif all_quants:
        candidates = [entry["path"] for entry in installations(spec, paths)]
    else:
        candidates = [resolve_installation(spec, paths, quant=quant).path]
    if spec.raw.get("download_target_mode") == "repo_root" and not all_quants:
        others = [entry["path"] for entry in installations(spec, paths) if entry["quant"] != quant]
        for candidate in candidates:
            if (candidate == (paths.models / spec.raw.get("repo_name", spec.raw.get("repo_id", "").replace("/", "-"))).resolve()
                    or any(candidate == other or candidate in other.parents for other in others)):
                raise ConfigError("此量化共享仓库根目录；不能单独删除整个根目录，请 --all 或登记独立目录")
    selected = sorted(set(candidates))
    for target in selected:
        if not inside_models(target, paths):
            raise ConfigError(f"拒绝删除 models/ 根目录或越界目录: {target}")
    # Parent directories subsume selected children; deletion remains deterministic.
    return [target for target in selected if not any(parent in selected for parent in target.parents)]


def remove_installations(spec, paths, *, quant=None, all_quants=False, explicit=None, dry_run=False):
    with file_lock(paths.run / ".artifacts.lock"):
        targets = removal_plan(spec, paths, quant=quant, all_quants=all_quants, explicit=explicit)
        specs = load_specs(paths.registry)
        for key, used_paths in active_installations(paths, specs):
            for used in used_paths:
                if any(t == used or t in used.parents or used in t.parents for t in targets):
                    raise ConfigError(f"拒绝删除: {key} 正在使用或可能使用该权重")
        for target in targets:
            print(f"{'计划删除' if dry_run else '删除'}: {target}")
        if not dry_run:
            for target in targets:
                if target.exists():
                    shutil.rmtree(target)
            manifest = Manifest(paths)
            def prune(data):
                data["entries"] = [e for e in data["entries"] if not any(
                    manifest.absolute(e).resolve() == t or t in manifest.absolute(e).resolve().parents for t in targets)]
            manifest.update(prune)
        return targets


def active_installations(paths, specs):
    """Include recorded explicit installations and conservatively handle live legacy PIDs."""
    from ..lifecycle.observe import observe_instances, process_identity, read_pid
    for key, instance in observe_instances(paths, specs).items():
        if key == "serve-ui":
            continue
        pid = instance.pid or getattr(instance, "observed_pid", None)
        if not pid or process_identity(pid) is None:
            continue
        record = read_pid(paths.run / f"{key}.pid")
        actual = getattr(instance, "model_path", None) or (record[3].get("model_path") if record else None)
        used_paths = {Path(actual).resolve()} if actual else set()
        owner = specs.get(key)
        if owner:
            used_paths.update(entry["path"].resolve() for entry in installations(owner, paths))
        if not used_paths:
            raise ConfigError(f"运行实例 {key} 权重归属不明；先 reconcile 或停止")
        yield key, used_paths


def main(argv=None, *, paths=None):
    parser = argparse.ArgumentParser(description="本地权重安装")
    parser.add_argument("--project-root")
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("is-downloaded")
    check.add_argument("model")
    listing = sub.add_parser("list")
    listing.add_argument("--json", action="store_true")
    reg = sub.add_parser("register")
    reg.add_argument("model")
    reg.add_argument("--path", required=True)
    reg.add_argument("--quant")
    reg.add_argument("--revision")
    rm = sub.add_parser("remove")
    rm.add_argument("model")
    rm.add_argument("--quant")
    rm.add_argument("--all", action="store_true")
    rm.add_argument("--path")
    rm.add_argument("--force", action="store_true", help="兼容参数；不绕过正在使用或路径归属检查")
    rm.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    paths = paths or project_paths(args.project_root)
    try:
        specs = load_specs(paths.registry)
        if args.cmd == "list":
            records = []
            for spec in specs.values():
                if spec.management == "external":
                    records.append({"model_key": spec.key, "external": True})
                records.extend(installations(spec, paths))
            if args.json:
                print(json.dumps(records, ensure_ascii=False, indent=2, default=str))
            else:
                for entry in records:
                    print(f"{entry['model_key']}  {entry.get('quant') or '-'}  "
                          f"{'external' if entry.get('external') else 'ready' if entry['complete'] else 'incomplete'}  "
                          f"{entry.get('path', '')}")
            return 0
        spec = specs[args.model]
        if args.cmd == "is-downloaded":
            if spec.management == "external":
                return 1
            return 0 if resolve_installation(spec, paths).complete else 1
        if args.cmd == "register":
            if spec.management == "external":
                raise ConfigError("外部模型不能登记本地权重")
            quant = selected_quant(spec, args.quant)
            path = Path(args.path).expanduser()
            path = path if path.is_absolute() else paths.root / path
            if not path.is_dir():
                raise ConfigError(f"不是目录: {path}")
            Manifest(paths).record(spec.key, quant, path, revision=args.revision)
            print(f"已登记: {spec.key} -> {path.resolve()}")
            if not weights_complete(spec, path, quant):
                print("权重尚不完整；启动前需要补齐", file=sys.stderr)
            return 0
        remove_installations(spec, paths, quant=args.quant, all_quants=args.all,
                             explicit=args.path, dry_run=args.dry_run)
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
