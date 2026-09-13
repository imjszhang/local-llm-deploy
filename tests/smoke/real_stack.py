"""Opt-in serial smoke of fresh service environments and an existing chat backend.

Creates isolated registry/PID/log files and ephemeral ports. Existing services are
never stopped. Run only when the existing gateway has no active inference.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from dataclasses import replace
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import urllib.request
import wave

from local_llm_deploy.config import ProjectPaths, load_models, normalize_models, project_paths
from local_llm_deploy.backends import build_gateway, build_service
from local_llm_deploy.lifecycle.manager import start_service, stop_service
from local_llm_deploy.lifecycle.process import terminate


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", required=True)
    parser.add_argument("--project-root")
    parser.add_argument("--embedding-python", required=True)
    parser.add_argument("--rerank-python", required=True)
    parser.add_argument("--whisper-python", required=True)
    parser.add_argument("--chat-model", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    source = project_paths(args.project_root)
    original = load_models(source.registry)
    # Read only the queue counters; never output request contents or credentials.
    with urllib.request.urlopen("http://127.0.0.1:8888/api/models", timeout=5) as response:
        active = json.load(response)
    if any(lane.get("active", 0) or lane.get("waiting", 0) for lane in active.get("lanes", {}).values()):
        parser.error("existing gateway is busy; retry after its requests finish")
    from local_llm_deploy.artifacts.paths import resolve_installation
    source_specs = normalize_models(original)
    targets = [("jina-embed", "embedding", args.embedding_python),
               ("jina-rerank-mlx", "rerank", args.rerank_python),
               ("whisper-large-v3", "whisper", args.whisper_python)]
    previous_env = dict(os.environ)
    records = []
    with tempfile.TemporaryDirectory(prefix="local-llm-real-smoke-") as temp:
        paths = ProjectPaths(Path(temp))
        os.chmod(paths.root, 0o700)
        raw = {}
        for key, module, python in targets:
            installation = resolve_installation(source_specs[key], source)
            cfg = dict(original[key], model_path=str(installation.path), default_port=free_port(),
                       runtime={"python": os.path.abspath(python), "management": "process", "ready_timeout": 180})
            raw[key] = cfg
            (paths.root / f"serve_{module}.py").write_text(
                f"from local_llm_deploy.services.{module} import main\nraise SystemExit(main())\n")
        chat = dict(original[args.chat_model], type="external", backend="external_http", management="external")
        raw[args.chat_model] = chat
        paths.registry.write_text(json.dumps(raw))
        (paths.root / "serve-ui.py").write_text("from local_llm_deploy.gateway.app import main\nraise SystemExit(main())\n")
        paths.static.symlink_to(source.static, target_is_directory=True)
        (paths.root / "tools").symlink_to(source.root / "tools", target_is_directory=True)
        if source.api_key.is_file():
            shutil.copyfile(source.api_key, paths.api_key)
            paths.api_key.chmod(0o600)
        os.environ.update(LOCAL_LLM_ROOT=str(paths.root), OLLAMA_AUTO_DISCOVER="0",
                          API_PROXY_TIMEOUT="180", QUEUE_TIMEOUT="180")
        specs = normalize_models(raw)
        started = []
        try:
            for key, module, python in targets:
                spec = build_service(specs[key], paths, SimpleNamespace(management="process"), env={})
                print(f"Starting isolated {key} on {spec.port}", flush=True)
                identity = start_service(spec, paths, models=specs)
                started.append((key, spec.label, identity))
                records.append({"model": key, "port": spec.port, "ready": True})
            gateway_port = free_port()
            gateway = build_gateway(paths, SimpleNamespace(port=gateway_port, management="process"),
                                    env={"PYTHON_BIN": sys.executable, "OLLAMA_AUTO_DISCOVER": "0"})
            identity = start_service(gateway, paths, models=specs)
            started.append(("serve-ui", gateway.label, identity))
            audio = paths.root / "sample.aiff"
            if shutil.which("say"):
                subprocess.run(["say", "-o", str(audio), "This is a local speech recognition test."], check=True)
            else:
                audio = paths.root / "sample.wav"
                with wave.open(str(audio), "wb") as output:
                    output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                    output.writeframes(b"\x00\x00" * 32000)
            loader = importlib.util.spec_from_file_location("live_smoke", source.root / "tests/smoke/services.py")
            runner = importlib.util.module_from_spec(loader)
            loader.loader.exec_module(runner)
            output = io.StringIO()
            with redirect_stdout(output):
                result = runner.main(["--project-root", str(paths.root), "--proxy", str(gateway_port),
                                      "--jina", str(raw["jina-embed"]["default_port"]),
                                      "--rerank", str(raw["jina-rerank-mlx"]["default_port"]),
                                      "--whisper", str(raw["whisper-large-v3"]["default_port"]),
                                      "--audio", str(audio), "--chat-model", args.chat_model])
            print(output.getvalue(), flush=True)
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps({"exit_code": result, "services": records,
                                               "checks": output.getvalue().splitlines()}, indent=2) + "\n")
            return result
        except Exception:
            for log in paths.logs.glob("*.log"):
                print(f"Diagnostic {log.name}:\n" + log.read_text(errors="replace")[-3000:], file=sys.stderr)
            raise
        finally:
            cleanup_errors = []
            try:
                for key, label, identity in reversed(started):
                    try:
                        stop_service(paths, key, models=specs, label=label)
                    except Exception as exc:
                        cleanup_errors.append(f"{key}: {exc}")
                        # Only the process started by this run; identity includes start time/hash.
                        terminate(identity, timeout=8)
            finally:
                os.environ.clear()
                os.environ.update(previous_env)
            if args.report.exists():
                report = json.loads(args.report.read_text())
                report.update(cleanup_complete=not cleanup_errors, cleanup_errors=cleanup_errors)
                args.report.write_text(json.dumps(report, indent=2) + "\n")
            if cleanup_errors:
                raise RuntimeError("Isolated cleanup failed: " + "; ".join(cleanup_errors))


if __name__ == "__main__":
    raise SystemExit(main())
