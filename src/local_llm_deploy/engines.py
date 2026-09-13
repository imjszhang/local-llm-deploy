"""Named, revision-pinned llama.cpp builds and explicit upgrade/rollback operations."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from .config import ConfigError, project_paths
from .storage import atomic_json, file_lock


def _git(directory, *args):
    return subprocess.check_output(["git", "-C", str(directory), *args], text=True, stderr=subprocess.PIPE).strip()


def _engine_revision(directory):
    directory = Path(directory).resolve()
    top = Path(_git(directory, "rev-parse", "--show-toplevel")).resolve()
    if top != directory:
        raise ConfigError("引擎目录必须是独立 Git 工作区的根目录，不能使用祖先仓库的版本")
    return _git(directory, "rev-parse", "HEAD")


def load_profiles(paths):
    file = paths.root / "engines.json"
    if not file.exists():
        return {"version": 1, "default": None, "profiles": {}}
    data = json.loads(file.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("profiles"), dict):
        raise ConfigError("engines.json 必须是 version=1 的 profiles 配置")
    return data


def _binary_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_engine(paths, name=None, cpp_dir=None):
    data = load_profiles(paths)
    override = cpp_dir or os.environ.get("CPP_DIR")
    name = name or data.get("default")
    if override:
        directory = Path(override).expanduser()
        directory = directory if directory.is_absolute() else paths.root / directory
        return {"directory": str(directory.resolve()), "executable": str(directory.resolve() / "build/bin/llama-server"),
                "revision": None, "profile": "CPP_DIR"}
    if name:
        try:
            profile = data["profiles"][name]
        except KeyError as exc:
            raise ConfigError(f"引擎档案不存在: {name}") from exc
        directory = Path(profile["directory"])
        directory = directory if directory.is_absolute() else paths.root / directory
        executable = Path(profile.get("executable", "build/bin/llama-server"))
        executable = executable if executable.is_absolute() else directory / executable
        return dict(profile, directory=str(directory.resolve()), executable=str(executable.resolve()), profile=name)
    directory = paths.root / "llama.cpp"
    return {"directory": str(directory), "executable": str(directory / "build/bin/llama-server"),
            "revision": None, "profile": "legacy"}


def register_profile(paths, name, directory, *, make_default=False, build_args=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise ConfigError("引擎名称只能包含字母、数字、点、横线和下划线")
    directory = Path(directory).resolve()
    revision = _engine_revision(directory)
    if _git(directory, "status", "--porcelain"):
        raise ConfigError("引擎源码有未提交改动，无法登记可复现版本")
    executable = directory / "build/bin/llama-server"
    if not executable.is_file():
        raise ConfigError(f"未找到引擎程序: {executable}")
    try:
        repository = _git(directory, "remote", "get-url", "origin")
    except subprocess.CalledProcessError:
        repository = None
    record = {"directory": str(directory), "executable": "build/bin/llama-server",
              "revision": revision, "repository": repository, "binary_sha256": _binary_hash(executable),
              "build_args": list(build_args or []), "registered_at": datetime.now(timezone.utc).isoformat(),
              "validation": "registered; inference smoke pending"}
    with file_lock(paths.root / "engines.json.lock"):
        data = load_profiles(paths)
        if name in data["profiles"]:
            raise ConfigError(f"档案 {name} 已存在；请使用新名称保留回退版本")
        data["profiles"][name] = record
        if make_default:
            if data.get("default") != name:
                data["previous"] = data.get("default")
            data["default"] = name
        atomic_json(paths.root / "engines.json", data)
    return record


def use_profile(paths, name):
    with file_lock(paths.root / "engines.json.lock"):
        data = load_profiles(paths)
        if name not in data["profiles"]:
            raise ConfigError(f"引擎档案不存在: {name}")
        previous = data.get("default")
        if previous != name:
            data["previous"] = previous
            data["default"] = name
        atomic_json(paths.root / "engines.json", data)
    return previous


def build_engine(directory, cmake_args=()):
    subprocess.run(["cmake", "-S", str(directory), "-B", str(directory / "build"),
                    "-DCMAKE_BUILD_TYPE=Release", *cmake_args], check=True)
    subprocess.run(["cmake", "--build", str(directory / "build"), "--target", "llama-server", "llama-cli", "-j"], check=True)


def main(argv=None, *, paths=None):
    parser = argparse.ArgumentParser(description="推理引擎版本管理")
    parser.add_argument("--project-root")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    reg = sub.add_parser("register")
    reg.add_argument("name")
    reg.add_argument("--directory", required=True)
    reg.add_argument("--default", action="store_true")
    use = sub.add_parser("use")
    use.add_argument("name")
    sub.add_parser("rollback")
    verify = sub.add_parser("verify")
    verify.add_argument("name", nargs="?")
    init = sub.add_parser("init")
    init.add_argument("--directory")
    init.add_argument("--repository", default="https://github.com/ggml-org/llama.cpp")
    init.add_argument("--revision", required=True)
    build = sub.add_parser("build")
    build.add_argument("--directory")
    build.add_argument("--cmake-arg", action="append", default=[])
    update = sub.add_parser("update")
    update.add_argument("name")
    update.add_argument("--revision", required=True)
    update.add_argument("--repository", default="https://github.com/ggml-org/llama.cpp")
    update.add_argument("--cmake-arg", action="append", default=[])
    record = sub.add_parser("record-validation")
    record.add_argument("name")
    record.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    paths = paths or project_paths(args.project_root)
    try:
        if args.cmd == "list":
            print(json.dumps(load_profiles(paths), ensure_ascii=False, indent=2))
        elif args.cmd == "register":
            print(json.dumps(register_profile(paths, args.name, args.directory, make_default=args.default), indent=2))
        elif args.cmd in ("use", "rollback"):
            name = args.name if args.cmd == "use" else load_profiles(paths).get("previous")
            if not name:
                raise ConfigError("没有可回退的上一引擎档案")
            previous = use_profile(paths, name)
            print(f"默认引擎: {previous} -> {name}；在服务下次启动时生效")
        elif args.cmd == "verify":
            engine = resolve_engine(paths, args.name)
            actual = _engine_revision(engine["directory"])
            if engine.get("revision") and actual != engine["revision"]:
                raise ConfigError("引擎源码版本与登记档案不一致")
            if _git(engine["directory"], "status", "--porcelain"):
                raise ConfigError("引擎源码有未提交改动")
            if not Path(engine["executable"]).is_file():
                raise ConfigError("引擎可执行文件不存在")
            if engine.get("binary_sha256") and _binary_hash(engine["executable"]) != engine["binary_sha256"]:
                raise ConfigError("引擎程序在登记后发生变化，请重新构建并登记新档案")
            result = subprocess.run([engine["executable"], "--version"], capture_output=True, text=True, timeout=20)
            if result.returncode:
                raise ConfigError("引擎 --version 检查失败")
            print(f"{engine['profile']}: {actual}")
            print((result.stdout + result.stderr)[:3000])
        elif args.cmd == "record-validation":
            report = Path(args.report).resolve()
            if not report.is_file() or not report.stat().st_size:
                raise ConfigError("验证报告不存在或为空")
            with file_lock(paths.root / "engines.json.lock"):
                data = load_profiles(paths)
                data["profiles"][args.name]["validation"] = {"report": str(report), "recorded_at": datetime.now(timezone.utc).isoformat()}
                atomic_json(paths.root / "engines.json", data)
            print("已关联验证报告（此命令不代替执行推理测试）")
        elif args.cmd == "build":
            directory = Path(args.directory or os.environ.get("CPP_DIR") or paths.root / "llama.cpp").resolve()
            build_engine(directory, args.cmake_arg)
        else:
            if not re.fullmatch(r"[0-9a-fA-F]{40}", args.revision):
                raise ConfigError("--revision 必须是完整 40 位 commit，避免可变分支导致结果漂移")
            if args.cmd == "update":
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.name):
                    raise ConfigError("无效引擎名称")
                directory = paths.root / "work_dir" / f"llama.cpp-{args.name}"
            else:
                directory = Path(args.directory or os.environ.get("CPP_DIR") or paths.root / "llama.cpp").resolve()
            if directory.exists():
                raise ConfigError(f"目标目录已存在，不覆盖现有构建: {directory}")
            subprocess.run(["git", "clone", "--no-checkout", args.repository, str(directory)], check=True)
            subprocess.run(["git", "-C", str(directory), "checkout", "--detach", args.revision], check=True)
            if args.cmd == "update":
                build_engine(directory, args.cmake_arg)
                register_profile(paths, args.name, directory, build_args=args.cmake_arg)
                print(f"引擎 {args.name} 已构建并登记；验证后执行 engine use {args.name}")
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
