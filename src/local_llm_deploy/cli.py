"""The public CLI and compatibility entry points. Imports remain inference-free."""
from __future__ import annotations

import argparse
from collections import deque
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

from .config import ConfigError, load_specs, normalize_models, project_paths
from .backends import build_ds4, build_gateway, build_service
from .backends.builders import launchd_settings
from .lifecycle import launchd
from .lifecycle.manager import foreground_service, instance_lock, reconcile, start_service, stop_service
from .lifecycle.observe import health_ready, observe_instances
from .lifecycle.types import LifecycleError


COMMANDS = ("list", "models", "registry", "download", "register", "remove", "start", "stop",
            "status", "logs", "plan", "foreground", "daemon", "reconcile", "config", "engine",
            "deploy", "monitor", "compat", "run", "help")


def _redact(value):
    if isinstance(value, dict):
        return {key: ("<redacted>" if any(part in key.lower() for part in ("api_key", "token", "secret", "password", "authorization")) else _redact(item))
                for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_redact(item) for item in value]
    return value


def _parser(name, description=None):
    return argparse.ArgumentParser(prog="llm " + name, description=description)


def _start_options(parser, *, model=True):
    if model:
        parser.add_argument("model")
    parser.add_argument("--port", type=int)
    parser.add_argument("--host")
    parser.add_argument("--lan", dest="host", action="store_const", const="0.0.0.0")
    parser.add_argument("--no-lan", dest="host", action="store_const", const="127.0.0.1")
    parser.add_argument("--quant")
    parser.add_argument("--model-dir")
    parser.add_argument("--cpp-dir")
    parser.add_argument("--engine")
    parser.add_argument("--api-key")
    parser.add_argument("--api-key-file")
    parser.add_argument("--management", choices=("process", "launchd"))
    parser.add_argument("--timeout", type=float, help="等待服务 ready 的秒数")
    parser.add_argument("--dry-run", action="store_true", help="打印脱敏启动描述，不执行")
    return parser


def _models(paths, *, optional=False):
    if optional and not paths.registry.exists():
        return {}
    return load_specs(paths.registry)


def _model(models, key):
    if key in models:
        return models[key]
    matches = [spec for spec in models.values() if key in (spec.alias, spec.backend_model)]
    if len(matches) == 1:
        return matches[0]
    raise LifecycleError(f"未知模型 {key!r}；用 list 查看注册表")


def _build(key, paths, args, models, *, validate=True, explicit_ds4=False):
    if key == "serve-ui":
        return build_gateway(paths, args, validate=validate)
    if explicit_ds4:
        return build_ds4(paths, args, validate=validate)
    return build_service(_model(models, key), paths, args, validate=validate)


def _start(paths, argv, *, action="start", explicit_ds4=False):
    parser = _start_options(_parser(action))
    args = parser.parse_args(argv)
    if action == "foreground":
        args.management = "process"
    models = _models(paths, optional=args.model == "serve-ui" or explicit_ds4)
    spec = _build(args.model, paths, args, models, validate=not (args.dry_run or action == "plan"), explicit_ds4=explicit_ds4)
    if args.dry_run or action == "plan":
        print(json.dumps(spec.as_dict(), ensure_ascii=False, indent=2))
        return 0
    if action == "foreground":
        return foreground_service(spec, paths, models=models)
    identity = start_service(spec, paths, models=models)
    print(f"{spec.key} ready (PID {identity.pid}, port {spec.port}, {spec.management})")
    print(f"日志: {spec.log_path}")
    return 0


def _status(paths, argv):
    parser = _parser("status")
    parser.add_argument("model", nargs="?")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--probe", action="store_true", help="额外执行只读 HTTP 就绪检查")
    args = parser.parse_args(argv)
    models = _models(paths, optional=True)
    observations = observe_instances(paths, models, probe=args.probe)
    selected = [args.model] if args.model else list(dict.fromkeys([*models, *observations]))
    rows = []
    for key in selected:
        spec = models.get(key)
        observation = observations.get(key)
        if observation:
            rows.append(observation.as_dict())
        else:
            external = bool(spec and spec.management == "external")
            healthy = health_ready(spec.host, spec.port, spec.raw.get("health_path", "/v1/models")) if external and args.probe else None
            rows.append({"key": key, "pid": None, "port": spec.port if spec else None,
                         "alias": spec.alias if spec else key, "status": ("ready" if healthy else "unavailable") if external and args.probe else ("external" if external else "stopped"),
                         "management": spec.management if spec else "process", "healthy": healthy})
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(f"{row['key']}: {row['status']} (PID {row['pid'] or '-'}, port {row['port'] or '-'}, {row['management']})")
        if not rows:
            print("无已注册或运行中的实例")
    return 0


def _stop(paths, argv, *, explicit=False):
    parser = _parser("stop")
    parser.add_argument("model", nargs="?")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)
    if bool(args.model) == args.all:
        parser.error("提供一个模型名或 --all")
    models = _models(paths, optional=True)
    keys = [key for key, spec in models.items() if spec.management != "external"] if args.all else [args.model]
    for key in keys:
        spec = models.get(key)
        if spec and spec.management == "external" and not explicit:
            raise LifecycleError(f"{key} 是外部服务，本项目不停止其进程")
        label = launchd_settings(key, spec.backend if spec else "")[0]
        stopped = stop_service(paths, key, models=models, label=label, timeout=args.timeout)
        print(f"{key}: {'已停止' if stopped else '未运行（失效记录已清理）'}")
    return 0


def _daemon(paths, argv, *, explicit_ds4=False):
    parser = _start_options(_parser("daemon"))
    parser.add_argument("operation", choices=("install", "uninstall", "start"), nargs="?", default="start")
    args = parser.parse_args(argv)
    models = _models(paths, optional=args.model in ("serve-ui", "ds4"))
    label = launchd_settings(args.model, "")[0]
    if args.operation == "uninstall":
        with instance_lock(paths, args.model):
            launchd.uninstall(label, root=paths.root)
        print(f"已卸载 {label}")
        return 0
    args.management = "launchd"
    spec = _build(args.model, paths, args, models, validate=not args.dry_run, explicit_ds4=explicit_ds4)
    if args.dry_run:
        print(json.dumps(spec.as_dict(), ensure_ascii=False, indent=2))
    elif args.operation == "install":
        with instance_lock(paths, spec.key):
            if launchd.loaded(spec.label):
                raise LifecycleError("服务已加载；请先 stop 再更新 LaunchAgent 定义")
            print(f"已写入 {launchd.install(spec)}")
    else:
        identity = start_service(spec, paths, models=models)
        print(f"{spec.key} ready (PID {identity.pid}, launchd)")
    return 0


def _deploy(paths, argv):
    parser = _start_options(_parser("deploy"), model=False)
    parser.add_argument("--model-name")
    args = parser.parse_args(argv)
    model_name = args.model_name or os.environ.get("MODEL_NAME")
    model_dir = args.model_dir or os.environ.get("MODEL_DIR")
    if model_name:
        models = _models(paths)
        model = _model(models, model_name)
    elif model_dir:
        # Manual GGUF remains a supported, named local instance without editing the registry.
        models = normalize_models({"custom": {"alias": "custom-model", "repo_id": "custom/model", "default_port": 8001,
                                              "quants": {"custom": {}}, "default_quant": "custom"}})
        model = models["custom"]
    else:
        parser.error("提供 --model-name 或 --model-dir")
    args.model_dir = model_dir
    spec = build_service(model, paths, args, validate=not args.dry_run)
    if args.dry_run:
        print(json.dumps(spec.as_dict(), ensure_ascii=False, indent=2))
    else:
        identity = start_service(spec, paths, models=models)
        print(f"{spec.key} ready (PID {identity.pid}, port {spec.port})")
    return 0


def _compat(paths, argv):
    if not argv:
        raise LifecycleError("compat 需要入口名称")
    entry, *tail = argv
    if entry == "jina":
        if not tail or tail[0] in ("help", "--help", "-h"):
            print("jina.sh {embed|rerank|all} {start|stop|status} [启动选项]")
            return 0
        service, *tail = tail
        if service not in ("embed", "rerank", "all"):
            raise LifecycleError("jina.sh 服务必须是 embed、rerank 或 all")
        if service == "all":
            return max(_compat(paths, ["jina", "embed", *tail]), _compat(paths, ["jina", "rerank", *tail]))
        key = os.environ.get("JINA_EMBED_MODEL_NAME" if service == "embed" else "JINA_RERANK_MODEL_NAME") or os.environ.get("JINA_MODEL_NAME") or ("jina-embed" if service == "embed" else "jina-rerank-mlx")
        port = os.environ.get("JINA_EMBED_PORT" if service == "embed" else "JINA_RERANK_PORT")
    elif entry == "whisper":
        key, port = os.environ.get("WHISPER_MODEL_NAME", "whisper-large-v3"), None
    elif entry in ("serve-ui", "ds4"):
        key, port = entry, None
    else:
        raise LifecycleError(f"未知兼容入口: {entry}")
    operation = tail.pop(0) if tail else ("status" if entry == "jina" else "start")
    if operation in ("help", "--help", "-h"):
        print(f"{entry}.sh {{start|stop|status|foreground|nohup|daemon}} [选项]")
        print("daemon {install|uninstall|start}；start 支持 --port/--host/--timeout/--dry-run")
        return 0
    if operation == "status":
        return _status(paths, [key, *tail])
    if operation == "stop":
        return _stop(paths, [key, *tail], explicit=entry == "ds4")
    if port and "--port" not in tail:
        tail += ["--port", port]
    if operation == "daemon":
        return _daemon(paths, [key, *tail], explicit_ds4=entry == "ds4")
    if operation in ("fg", "foreground"):
        return _start(paths, [key, *tail], action="foreground", explicit_ds4=entry == "ds4")
    if operation == "nohup":
        tail += ["--management", "process"]
    elif operation != "start":
        raise LifecycleError(f"未知命令: {operation}")
    return _start(paths, [key, *tail], explicit_ds4=entry == "ds4")


def _run_entry(paths, argv):
    parser = _start_options(_parser("run"), model=False)
    parser.add_argument("entry", choices=("embedding", "rerank", "whisper", "ds4"))
    parser.add_argument("--model-name")
    args, backend_args = parser.parse_known_args(argv)
    args.management = "process"
    keys = {"embedding": os.environ.get("JINA_EMBED_MODEL_NAME") or os.environ.get("JINA_MODEL_NAME", "jina-embed"),
            "rerank": os.environ.get("JINA_RERANK_MODEL_NAME") or os.environ.get("JINA_MODEL_NAME", "jina-rerank-mlx"),
            "whisper": os.environ.get("WHISPER_MODEL_NAME", "whisper-large-v3"), "ds4": "ds4"}
    key = args.model_name or keys[args.entry]
    args.backend_args = backend_args
    models = _models(paths, optional=args.entry == "ds4")
    spec = _build(key, paths, args, models, validate=not args.dry_run, explicit_ds4=args.entry == "ds4")
    if args.dry_run:
        print(json.dumps(spec.as_dict(), ensure_ascii=False, indent=2))
    else:
        # launchd already supervises start-* entry scripts installed before the migration.
        return foreground_service(spec, paths, models=models)
    return 0


def _monitor(paths, argv):
    parser = _parser("monitor")
    parser.add_argument("metric", choices=("health", "metrics", "slots"), nargs="?", default="metrics")
    parser.add_argument("--model")
    parser.add_argument("--port", type=int)
    args = parser.parse_args(argv)
    port = args.port or (int(os.environ["PORT"]) if os.environ.get("PORT") else None)
    if args.model and port is None:
        observation = observe_instances(paths, _models(paths)).get(args.model)
        if not observation or not observation.pid:
            raise LifecycleError(f"{args.model} 未在运行")
        port = observation.port
    headers = {}
    if paths.api_key.is_file():
        key = paths.api_key.read_text(encoding="utf-8").splitlines()
        if key:
            headers["Authorization"] = "Bearer " + key[0].strip()
    request = urllib.request.Request(f"http://127.0.0.1:{port or 8001}/{args.metric}", headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        print(response.read().decode("utf-8"), end="")
    return 0


def main(argv=None):
    effective_args = list(argv) if argv is not None else sys.argv[1:]
    if effective_args == ["__service-runner"]:
        from .lifecycle.runner import main as run_service
        return run_service()
    parser = argparse.ArgumentParser(prog="llm", description="本地模型部署、统一状态与运行管理")
    parser.add_argument("--project-root", help="部署目录；默认 LOCAL_LLM_ROOT 或源码目录")
    parser.add_argument("command", nargs="?", choices=COMMANDS, default="help")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    ns = parser.parse_args(argv)
    if ns.command == "help":
        parser.print_help()
        return 0
    try:
        paths = project_paths(ns.project_root)
        cmd, args = ns.command, ns.args
        if cmd == "registry":
            from . import registry
            return registry.main(args, paths=paths)
        if cmd in ("models", "register", "remove"):
            from .artifacts import inventory
            return inventory.main(["list" if cmd == "models" else cmd, *args], paths=paths)
        if cmd == "download":
            from .artifacts import download
            return download.main(args, paths=paths)
        if cmd == "engine":
            from . import engines
            return engines.main(args, paths=paths)
        if cmd == "list":
            sub = _parser("list")
            sub.add_argument("--json", action="store_true")
            opts = sub.parse_args(args)
            models = _models(paths)
            observations = observe_instances(paths, models)
            rows = [{"key": key, "alias": model.alias, "backend": model.backend, "capabilities": model.capabilities,
                     "port": model.port, "management": model.management,
                     "status": observations[key].status if key in observations else ("external" if model.management == "external" else "stopped")}
                    for key, model in models.items()]
            if opts.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                for row in rows:
                    print(f"{row['key']:24s} {row['backend']:24s} :{row['port']} {row['status']}  alias={row['alias']}")
            return 0
        if cmd == "status":
            return _status(paths, args)
        if cmd in ("start", "plan", "foreground"):
            return _start(paths, args, action=cmd)
        if cmd == "stop":
            return _stop(paths, args)
        if cmd == "daemon":
            return _daemon(paths, args)
        if cmd == "deploy":
            return _deploy(paths, args)
        if cmd == "compat":
            return _compat(paths, args)
        if cmd == "run":
            return _run_entry(paths, args)
        if cmd == "monitor":
            return _monitor(paths, args)
        if cmd == "reconcile":
            _parser(cmd).parse_args(args)
            print(json.dumps({"removed": reconcile(paths, models=_models(paths, optional=True))}, ensure_ascii=False))
            return 0
        if cmd == "logs":
            sub = _parser("logs")
            sub.add_argument("model")
            sub.add_argument("--no-follow", action="store_true")
            sub.add_argument("--lines", type=int, default=100)
            opts = sub.parse_args(args)
            if Path(opts.model).name != opts.model or opts.model in (".", ".."):
                raise LifecycleError("无效模型名")
            path = paths.logs / f"{opts.model}.log"
            if not path.is_file():
                raise LifecycleError(f"日志不存在: {path}")
            if opts.no_follow:
                if opts.lines < 0:
                    raise LifecycleError("--lines 不能小于 0")
                with path.open(encoding="utf-8", errors="replace") as stream:
                    print("".join(deque(stream, maxlen=opts.lines)), end="")
            else:
                os.execlp("tail", "tail", "-n", str(opts.lines), "-f", str(path))
            return 0
        if cmd == "config":
            sub = _parser("config")
            sub.add_argument("operation", choices=("validate", "show"), default="validate", nargs="?")
            sub.add_argument("--resolved", action="store_true")
            opts = sub.parse_args(args)
            models = _models(paths)
            if opts.operation == "validate":
                print(f"配置有效: {len(models)} 个模型")
            else:
                content = {key: {"alias": model.alias, "backend": model.backend, "capabilities": model.capabilities,
                                 "management": model.management, "port": model.port, "host": model.host}
                           for key, model in models.items()} if opts.resolved else {key: model.raw for key, model in models.items()}
                print(json.dumps(_redact(content), ensure_ascii=False, indent=2))
            return 0
        parser.error(f"未实现命令: {cmd}")
    except (ConfigError, LifecycleError, OSError, ValueError, urllib.error.URLError, subprocess.SubprocessError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("操作已取消", file=sys.stderr)
        return 130
