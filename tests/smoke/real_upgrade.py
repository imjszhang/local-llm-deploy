"""Explicit real Embedding code/environment upgrade and rollback on an isolated port.

Runs the specified baseline source with the old environment, current package with
a freshly installed environment, and baseline again. Weights and live services
stay in place. No import-time requests or model loads.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request

from local_llm_deploy.artifacts.paths import resolve_installation
from local_llm_deploy.config import load_models, normalize_models, project_paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", required=True)
    parser.add_argument("--baseline-commit", required=True)
    parser.add_argument("--old-python", required=True)
    parser.add_argument("--new-python", required=True)
    parser.add_argument("--model", default="jina-embed")
    parser.add_argument("--project-root")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{40}", args.baseline_commit):
        parser.error("baseline-commit must be an exact 40-character commit")
    root = project_paths(args.project_root)
    with urllib.request.urlopen("http://127.0.0.1:8888/api/models", timeout=5) as response:
        status = json.load(response)
    if any(lane.get("active", 0) or lane.get("waiting", 0) for lane in status.get("lanes", {}).values()):
        parser.error("existing gateway is busy")
    raw = load_models(root.registry)
    model = normalize_models(raw)[args.model]
    weights = resolve_installation(model, root).path
    baseline = subprocess.check_output(["git", "-C", str(root.root), "show",
                                        args.baseline_commit + ":serve_embedding.py"])
    phases = []
    vectors = []
    with tempfile.TemporaryDirectory(prefix="local-llm-upgrade-") as temp:
        checkout = Path(temp)
        old_script = checkout / "serve_embedding_old.py"
        old_script.write_bytes(baseline)
        new_script = checkout / "serve_embedding_new.py"
        new_script.write_text("from local_llm_deploy.services.embedding import main\nraise SystemExit(main())\n")
        (checkout / "models.json").write_text(json.dumps({args.model: raw[args.model]}))
        headers = {"Content-Type": "application/json"}
        if root.api_key.is_file():
            shutil.copyfile(root.api_key, checkout / ".api-key")
            (checkout / ".api-key").chmod(0o600)
            key = root.api_key.read_text().splitlines()[0].strip()
            headers["Authorization"] = "Bearer " + key
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        for name, python, script in (("baseline", args.old_python, old_script),
                                     ("upgrade", args.new_python, new_script),
                                     ("rollback", args.old_python, old_script)):
            env = dict(os.environ, LOCAL_LLM_ROOT=str(checkout))
            env.pop("LOCAL_LLM_MANAGED_INSTANCE", None)
            log_path = checkout / (name + ".log")
            print(f"{name}: starting isolated Embedding on {port}", flush=True)
            with log_path.open("wb") as log:
                child = subprocess.Popen([os.path.abspath(python), str(script), "--model-name", args.model,
                                          "--model-dir", str(weights), "--port", str(port)],
                                         env=env, cwd=checkout, stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 180
                while True:
                    if child.poll() is not None:
                        raise RuntimeError(f"{name} exited: {log_path.read_text()[-2000:]}")
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                            assert json.load(response)["status"] == "ok"
                        break
                    except (OSError, AssertionError):
                        if time.monotonic() >= deadline:
                            raise RuntimeError(name + " did not become ready")
                        time.sleep(0.1)
                checks = []
                phase_vectors = []
                for task, dims in (("text-matching", 1024), ("retrieval.query", 256), ("retrieval.passage", 1024)):
                    body = {"model": model.alias, "input": ["维护项目需要可靠的测试。"],
                            "task": task, "dimensions": dims}
                    request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/embeddings",
                                                     data=json.dumps(body).encode(), headers=headers)
                    with urllib.request.urlopen(request, timeout=60) as response:
                        result = json.load(response)
                    vector = result["data"][0]["embedding"]
                    assert len(vector) == dims and all(math.isfinite(value) for value in vector)
                    assert result["usage"]["total_tokens"] > 0
                    phase_vectors.append(vector)
                    checks.append({"task": task, "dimensions": dims, "passed": True})
                vectors.append(phase_vectors)
                phases.append({"phase": name, "python": os.path.abspath(python), "checks": checks})
            finally:
                if child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
                deadline = time.monotonic() + 5
                while True:
                    with socket.socket() as sock:
                        occupied = sock.connect_ex(("127.0.0.1", port)) == 0
                    if not occupied:
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError("test port was not released")
                    time.sleep(0.1)
                print(f"{name}: stopped; port released", flush=True)
        similarities = []
        for phase_vectors in vectors[1:]:
            for before, after in zip(vectors[0], phase_vectors):
                cosine = sum(a * b for a, b in zip(before, after)) / math.sqrt(sum(a*a for a in before) * sum(b*b for b in after))
                assert cosine > 0.999, cosine
                similarities.append(cosine)
        report = {"passed": True, "baseline_commit": args.baseline_commit, "phases": phases,
                  "weights_unchanged_path": str(weights), "minimum_cosine_to_baseline": min(similarities),
                  "cleanup_complete": True}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print("Real code/environment upgrade and rollback passed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
