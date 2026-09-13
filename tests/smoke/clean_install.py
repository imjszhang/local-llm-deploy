"""Validate installation from an isolated copy of the current source checkout.

Requires an explicit local wheelhouse for offline build dependencies. No model
weights, credentials, current deployment registry or virtualenv are copied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import venv


def source_files(source):
    listed = subprocess.check_output([
        "git", "-C", str(source), "ls-files", "--cached", "--others", "--exclude-standard", "-z",
    ])
    paths = set(os.fsdecode(name) for name in listed.split(b"\x00") if name)
    # Also exclude tracked files that now match ignore rules, so an accidentally
    # tracked runtime directory cannot make this a deployment-state copy.
    ignored = subprocess.run([
        "git", "-C", str(source), "check-ignore", "--no-index", "-z", "--stdin",
    ], input=b"\x00".join(os.fsencode(name) for name in paths) + b"\x00", capture_output=True)
    if ignored.returncode not in (0, 1):
        raise RuntimeError(ignored.stderr.decode(errors="replace"))
    excluded = set(os.fsdecode(name) for name in ignored.stdout.split(b"\x00") if name)
    return sorted(name for name in paths - excluded if (source / name).exists())


def copy_source(source, destination):
    manifest = []
    for relative in source_files(source):
        original = source / relative
        if original.is_symlink():
            # A clean checkout must not retain a symlink to the original model,
            # environment or any other file outside the copied source.
            raise ValueError(f"Review source symlink before clean installation: {relative}")
        if not original.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original, target)
        manifest.append({"path": relative, "bytes": target.stat().st_size,
                         "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
    if not manifest:
        raise ValueError("No source files selected")
    for forbidden in ("models", "models.json", "run", "logs", ".api-key", ".hf-env", ".venv", "work_dir"):
        if (destination / forbidden).exists():
            raise ValueError(f"Deployment state leaked into clean source: {forbidden}")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--wheelhouse", type=Path, required=True, help="Local cached wheels, including setuptools")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--run-tests", action="store_true", help="Run the full standard-library no-model test suite")
    parser.add_argument("--launchd-tests", action="store_true", help="Explicitly run isolated UUID LaunchAgents against the installed wheel (macOS)")
    args = parser.parse_args(argv)
    if args.launchd_tests and sys.platform != "darwin":
        parser.error("--launchd-tests requires macOS")
    source = args.project_root.resolve()
    wheelhouse = args.wheelhouse.resolve()
    report_path = args.report.resolve()
    if not wheelhouse.is_dir():
        parser.error("--wheelhouse must be an existing directory")
    result = {"source": str(source), "python": sys.version.split()[0], "offline": True, "checks": []}
    status = 0
    try:
        with tempfile.TemporaryDirectory(prefix="local-llm-clean-install-") as directory:
            temporary = Path(directory)
            checkout = temporary / "source"
            outside = temporary / "elsewhere"
            isolated_env = temporary / "environment"
            wheels = temporary / "wheels"
            for path in (checkout, outside, wheels):
                path.mkdir()
            result["source_files"] = copy_source(source, checkout)
            result["copied_bytes"] = sum(item["bytes"] for item in result["source_files"])
            print(f"Copied {len(result['source_files'])} source files ({result['copied_bytes']} bytes); deployment state excluded", flush=True)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("LOCAL_LLM_", "JINA_", "WHISPER_", "OLLAMA_", "GATEWAY_", "SERVE_UI_", "PIP_"))
                   and key not in ("PYTHONPATH", "PYTHONHOME", "PYTHON_BIN", "API_KEY", "API_KEY_FILE",
                                   "OPENAI_API_KEY", "MODEL_NAME", "MODEL_DIR", "PORT", "HOST")}

            def run(name, command, *, runtime_env=None, timeout=180):
                completed = subprocess.run([str(arg) for arg in command], cwd=outside,
                                           env=runtime_env or env, capture_output=True, text=True, timeout=timeout)
                result["checks"].append({"name": name, "argv": [str(arg) for arg in command],
                                         "cwd": str(outside), "exit_code": completed.returncode,
                                         "stdout": completed.stdout, "stderr": completed.stderr})
                print(f"[{'OK' if completed.returncode == 0 else 'FAIL'}] {name}", flush=True)
                if completed.returncode:
                    raise RuntimeError(f"{name} failed:\n{completed.stdout}\n{completed.stderr}")
                return completed

            run("offline wheel build", [sys.executable, "-m", "pip", "--isolated", "wheel", "--no-index", "--find-links",
                                        wheelhouse, "--no-deps", "--wheel-dir", wheels, checkout])
            package_wheels = list(wheels.glob("local_llm_deploy-*.whl"))
            if len(package_wheels) != 1:
                raise RuntimeError("Expected exactly one project wheel")
            result["wheel"] = {"name": package_wheels[0].name,
                               "sha256": hashlib.sha256(package_wheels[0].read_bytes()).hexdigest()}
            venv.EnvBuilder(with_pip=True).create(isolated_env)
            python = isolated_env / "bin/python"
            cli = isolated_env / "bin/local-llm"
            env["PATH"] = str(isolated_env / "bin") + os.pathsep + env.get("PATH", os.defpath)
            env["LOCAL_LLM_PYTHON"] = str(python)
            run("install core wheel without dependencies", [python, "-m", "pip", "--isolated", "install", "--no-index",
                                                            "--no-deps", package_wheels[0]])
            run("core dependency check", [python, "-m", "pip", "--isolated", "check"])
            import_check = """import importlib.util, json, sys
from pathlib import Path
import local_llm_deploy
import local_llm_deploy.cli
import local_llm_deploy.gateway.app
import local_llm_deploy.services.embedding
import local_llm_deploy.services.rerank
import local_llm_deploy.services.whisper
package_path = Path(local_llm_deploy.__file__).resolve()
assert Path(sys.prefix).resolve() in package_path.parents, package_path
heavy = ('torch', 'mlx', 'mlx_whisper', 'transformers', 'huggingface_hub')
assert not set(heavy).intersection(sys.modules)
assert all(importlib.util.find_spec(name) is None for name in heavy)
print(json.dumps({'package': str(package_path), 'heavy_dependencies': 'absent'}))
"""
            run("installed package imports with heavy runtimes absent", [python, "-I", "-c", import_check])
            run("installed CLI help from another cwd", [cli, "--help"])
            run("installed module help from another cwd", [python, "-I", "-m", "local_llm_deploy", "--help"])
            run("initialize sample registry via installed CLI", [cli, "--project-root", checkout, "registry", "init"])
            run("validate sample registry via installed CLI", [cli, "--project-root", checkout, "config", "validate"])
            run("list sample models without weights", [cli, "--project-root", checkout, "list", "--json"])
            for script in ("manage.sh", "deploy.sh", "jina.sh", "whisper.sh", "serve-ui.sh", "ds4.sh",
                           "download.sh", "init_llamacpp.sh", "setup_llamacpp.sh"):
                run(f"compatibility {script} --help", [checkout / script, "--help"])
            for script in ("serve-ui.py", "serve_embedding.py", "serve_rerank.py", "serve_whisper.py",
                           "download_model.py", "registry_cli.py", "model_inventory.py"):
                run(f"compatibility {script} --help", [python, checkout / script, "--help"])
            run("validate sample registry via compatibility wrapper", [checkout / "manage.sh", "config", "validate"])
            if args.run_tests:
                run("clean source full no-model tests", [python, "-m", "unittest", "discover", "-s", checkout / "tests", "-t", checkout, "-v"])
            if args.launchd_tests:
                run("installed wheel isolated launchd tests", [python, "-m", "unittest", "discover", "-s",
                                                              checkout / "tests/lifecycle", "-t", checkout, "-p", "test_launchd_smoke.py", "-v"],
                    runtime_env=dict(env, LOCAL_LLM_TEST_LAUNCHD="1"))
            result["status"] = "passed"
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
        print(str(exc), file=sys.stderr)
        status = 1
    finally:
        result["temporary_copy_removed"] = True
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(f"Report: {report_path}", flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
