from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys

from ..lifecycle.types import LifecycleError, ServiceSpec


def _option(options, name, default=None):
    return getattr(options, name, None) if getattr(options, name, None) is not None else default


def _value(options, name, env, env_names, configured, default=None):
    value = getattr(options, name, None)
    if value is not None:
        return value
    for key in env_names:
        if env.get(key) not in (None, ""):
            return env[key]
    return configured if configured is not None else default


def _absolute(value, root):
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def _extra_args(values):
    protected = {"--model", "-m", "--alias", "--host", "--port", "--api-key", "--api-key-file", "--model-name"}
    if any(str(value).split("=", 1)[0] in protected for value in values):
        raise LifecycleError("extra_args 不允许覆盖模型、地址或认证参数；请使用对应配置字段或 CLI 参数")
    return [str(value) for value in values]


def _python(paths, backend, runtime, env):
    configured = env.get("PYTHON_BIN") or runtime.get("python")
    if configured:
        return str(_absolute(configured, paths.root)) if "/" in configured else (shutil.which(configured) or configured)
    candidates = {
        "transformers_embedding": (".venv-embed", ".venv"),
        "mlx_rerank": (".venv-rerank", ".venv-embed", ".venv"),
        "mlx_whisper": (".venv-whisper", ".venv"),
        "gateway": (".venv",),
    }.get(backend, (".venv",))
    for name in candidates:
        candidate = paths.root / name / "bin/python"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return sys.executable


def launchd_settings(key, backend):
    labels = {"jina-embed": "jina-embed", "jina-rerank-mlx": "jina-rerank",
              "whisper-large-v3": "whisper", "serve-ui": "serve-ui", "ds4": "ds4"}
    # Existing labels are stable. Other models get their own model-key label.
    label = "com.local-llm-deploy." + labels.get(key, "model." + key)
    autostart = backend in ("transformers_embedding", "mlx_rerank", "mlx_whisper", "gateway")
    keep_alive = {"SuccessfulExit": False} if backend in ("transformers_embedding", "mlx_rerank") else False
    return label, autostart, keep_alive


def _check(spec):
    binary = Path(spec.argv[0])
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise LifecycleError(f"找不到可执行文件: {binary}；请先安装对应运行环境")
    if spec.api_key and spec.api_key_file:
        raise LifecycleError("--api-key 与 --api-key-file 不能同时使用")
    if spec.api_key_file and not spec.api_key_file.is_file():
        raise LifecycleError(f"密钥文件不存在: {spec.api_key_file}")
    if not 1 <= spec.port <= 65535:
        raise LifecycleError("端口必须在 1..65535 之间")
    if spec.ready_timeout <= 0:
        raise LifecycleError("就绪超时必须大于 0")
    return spec


def build_service(model, paths, options=None, *, env=None, validate=True):
    env = dict(os.environ if env is None else env)
    raw = model.raw
    runtime = raw.get("runtime") or {}
    backend = model.backend
    if model.management in ("external", "observe", "unmanaged") or backend in ("ollama", "external_http"):
        raise LifecycleError(f"{model.key} 由外部服务管理；仅支持查询和连接。{raw.get('startup_hint', '')}")
    prefix = {"transformers_embedding": "JINA", "mlx_rerank": "JINA", "mlx_whisper": "WHISPER"}.get(backend, "")
    specific = {"transformers_embedding": "JINA_EMBED", "mlx_rerank": "JINA_RERANK"}.get(backend)
    port_env = ([f"{specific}_PORT"] if specific else []) + ([f"{prefix}_PORT"] if prefix else []) + ["PORT"]
    host_env = ([f"{specific}_HOST"] if specific else []) + ([f"{prefix}_HOST"] if prefix else []) + ["HOST"]
    port = int(_value(options, "port", env, port_env, model.port, 8001))
    host = str(_value(options, "host", env, host_env, model.host, "127.0.0.1"))
    management = _option(options, "management", runtime.get("management") or
                         (model.management if model.management in ("launchd", "process") else
                          ("process" if backend == "llama_cpp" else "launchd")))
    key_file = _value(options, "api_key_file", env, ["API_KEY_FILE"], runtime.get("api_key_file"))
    key = _value(options, "api_key", env, ["API_KEY"], None)
    if not key_file and not key and paths.api_key.is_file():
        key_file = str(paths.api_key)
    key_file = _absolute(key_file, paths.root) if key_file else None
    from ..artifacts.paths import primary_model_file, resolve_installation
    installation = resolve_installation(model, paths, quant=_option(options, "quant"),
                                        explicit=_option(options, "model_dir", env.get("MODEL_DIR")))
    if validate and not installation.complete:
        raise LifecycleError(f"模型权重不完整: {installation.path}；先 download/register 或指定 --model-dir")
    service_env = {"LOCAL_LLM_MANAGED_INSTANCE": "1", "LOCAL_LLM_ROOT": str(paths.root)}
    model_dir = installation.path
    engine = {}
    if backend == "llama_cpp":
        from ..engines import resolve_engine
        cpp_override = _value(options, "cpp_dir", env, ["CPP_DIR"], runtime.get("cpp_dir") or raw.get("cpp_dir"))
        engine = resolve_engine(paths, name=_option(options, "engine", raw.get("engine_profile") or raw.get("engine")), cpp_dir=cpp_override)
        cpp = Path(engine["directory"])
        model_file = primary_model_file(model, model_dir, quant=installation.quant) if installation.complete else model_dir / "<model.gguf>"
        params = model.params
        argv = [str(engine["executable"]), "--model", str(model_file), "--alias", model.alias, "--fit", "on"]
        for flag, field, default in (("--temp", "temp", 1.0), ("--top-p", "top_p", 0.95),
                                    ("--ctx-size", "ctx_size", 16384), ("--n-predict", "n_predict", 32768),
                                    ("--repeat-penalty", "repeat_penalty", 1.0)):
            argv += [flag, str(params.get(field, default))]
        argv += ["--host", host, "--port", str(port), "--parallel", str(params.get("max_concurrent", 1)),
                 "--metrics", "--slots", "--threads-http", "64"]
        mmproj = raw.get("mmproj")
        if mmproj:
            candidates = [model_dir / mmproj, paths.models / (raw.get("repo_name") or raw.get("repo_id", "").replace("/", "-")) / mmproj]
            selected = next((p for p in candidates if p.is_file()), None)
            if selected:
                argv += ["--mmproj", str(selected)]
            elif validate:
                raise LifecycleError(f"配置的 mmproj 文件不存在: {mmproj}")
        template = raw.get("chat_template_file")
        if template:
            template_path = _absolute(template, paths.root)
            if validate and not template_path.is_file():
                raise LifecycleError(f"配置的 chat_template_file 不存在: {template_path}")
            argv += ["--chat-template-file", str(template_path)]
        argv.extend(_extra_args(params.get("extra_args", [])))
        if key:
            argv += ["--api-key", key]
        elif key_file:
            argv += ["--api-key-file", str(key_file)]
        service_env.update(LLAMA_SERVER_SLOTS_DEBUG="1", DYLD_LIBRARY_PATH=str(cpp / "build/bin") + (":" + env["DYLD_LIBRARY_PATH"] if env.get("DYLD_LIBRARY_PATH") else ""))
    else:
        scripts = {"transformers_embedding": "serve_embedding.py", "mlx_rerank": "serve_rerank.py", "mlx_whisper": "serve_whisper.py"}
        if backend not in scripts:
            raise LifecycleError(f"尚无启动适配器: {backend}")
        argv = [_python(paths, backend, runtime, env), str(paths.root / scripts[backend]),
                "--model-name", model.key, "--host", host, "--port", str(port)]
        # All services receive the same selected installation via a scoped environment value.
        service_env["LOCAL_LLM_MODEL_DIR"] = str(model_dir)
        if key_file:
            service_env["API_KEY_FILE"] = str(key_file)
        if key:
            service_env["API_KEY"] = key
        if backend == "mlx_whisper" and (paths.root / "tools/ffmpeg").is_file():
            service_env["PATH"] = str(paths.root / "tools") + os.pathsep + env.get("PATH", os.defpath)
    argv.extend(_extra_args(_option(options, "backend_args", []) or []))
    label, autostart, keep_alive = launchd_settings(model.key, backend)
    spec = ServiceSpec(model.key, model.alias, tuple(argv), paths.root, service_env, host, port,
                       paths.logs / f"{model.key}.log", paths.run / f"{model.key}.pid", management,
                       runtime.get("ready_path", "/health"), float(_option(options, "timeout", runtime.get("ready_timeout", 180))),
                       label, runtime.get("run_at_load", autostart), runtime.get("keep_alive", keep_alive),
                       model_dir, key_file, key, quant=installation.quant,
                       engine_profile=engine.get("profile"), engine_revision=engine.get("revision"))
    return _check(spec) if validate else spec


def build_gateway(paths, options=None, *, env=None, validate=True):
    from ..gateway.settings import GATEWAY_ENV_NAMES
    env = dict(os.environ if env is None else env)
    port = int(_value(options, "port", env, ["UI_PORT"], None, 8888))
    host = str(_value(options, "host", env, ["UI_HOST"], None, "0.0.0.0"))
    service_env = {key: env[key] for key in GATEWAY_ENV_NAMES if key in env}
    service_env.update(LOCAL_LLM_MANAGED_INSTANCE="1", LOCAL_LLM_ROOT=str(paths.root), UI_PORT=str(port), UI_HOST=host)
    spec = ServiceSpec("serve-ui", "serve-ui", (_python(paths, "gateway", {}, env), str(paths.root / "serve-ui.py")),
                       paths.root, service_env, host, port, paths.logs / "serve-ui.log", paths.run / "serve-ui.pid",
                       _option(options, "management", "launchd"), "/api/models", float(_option(options, "timeout", 30)),
                       "com.local-llm-deploy.serve-ui", True, False)
    return _check(spec) if validate else spec


def build_ds4(paths, options=None, *, env=None, validate=True):
    """Explicit ds4.sh invocation opts into managing DS4; registry external entries do not."""
    env = dict(os.environ if env is None else env)
    root_value = env.get("DS4_ROOT")
    if not root_value:
        raise LifecycleError("请设置 DS4_ROOT 指向本地 ds4 仓库；不再使用个人机器的硬编码路径")
    root = _absolute(root_value, paths.root)
    binary = _absolute(env.get("DS4_BIN", str(root / "ds4-server")), paths.root)
    model = _absolute(env.get("DS4_MODEL", str(root / "ds4flash.gguf")), paths.root)
    host = str(_value(options, "host", env, ["DS4_HOST"], None, "127.0.0.1"))
    port = int(_value(options, "port", env, ["DS4_PORT"], None, 8005))
    argv = [str(binary), "--chdir", str(root), "-m", str(model), "--host", host, "--port", str(port),
            "--ctx", env.get("DS4_CTX", "100000"), "--batched-session", env.get("DS4_BATCHED_SESSION", "4"),
            "--kv-disk-dir", env.get("DS4_KV_DIR", "/tmp/ds4-kv"), "--kv-disk-space-mb", env.get("DS4_KV_SPACE_MB", "8192")]
    argv.extend(_option(options, "backend_args", []) or [])
    service_env = {"LOCAL_LLM_MANAGED_INSTANCE": "1", "LOCAL_LLM_ROOT": str(paths.root)}
    if env.get("DS4_LOCK_FILE"):
        service_env["DS4_LOCK_FILE"] = env["DS4_LOCK_FILE"]
    spec = ServiceSpec("ds4", "ds4", tuple(argv), root, service_env, host, port,
                       paths.logs / "ds4.log", paths.run / "ds4.pid", _option(options, "management", "launchd"),
                       "/v1/models", float(_option(options, "timeout", 300)), "com.local-llm-deploy.ds4", False, False, model)
    if validate and not model.exists():
        raise LifecycleError(f"找不到 DS4 模型: {model}")
    return _check(spec) if validate else spec
