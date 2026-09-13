from __future__ import annotations

import argparse
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from local_llm_deploy.backends.builders import build_gateway, build_service
from local_llm_deploy.cli import main
from local_llm_deploy.config import ProjectPaths, normalize_models
from local_llm_deploy.lifecycle.types import LifecycleError


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="llm cli ")
        self.paths = ProjectPaths(Path(self.temp.name))
        self.raw = {"second-embed": {"type": "embedding", "repo_id": "x/embed", "alias": "embedding-two", "default_port": 8024},
                    "external": {"type": "external", "default_port": 8030},
                    "chat": {"repo_id": "x/chat", "alias": "chat-alias", "default_port": 8021,
                             "default_quant": "Q4", "quants": {"Q4": {}}, "params": {"extra_args": ["--chat-template-kwargs", '{"a":"with spaces"}']}}}
        self.paths.registry.write_text(json.dumps(self.raw))
        self.models = normalize_models(self.raw)

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(["--project-root", str(self.paths.root), *args])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_list_status_are_lightweight_and_read_only(self):
        for args in (("list", "--json"), ("status", "--json"), ("config", "validate")):
            code, out, err = self.invoke(*args)
            self.assertEqual(code, 0, err)
        self.assertFalse(self.paths.run.exists())
        self.assertFalse(self.paths.logs.exists())
        self.assertFalse(self.paths.models.exists())

    def test_second_embedding_uses_registry_key_and_unique_label(self):
        options = argparse.Namespace(port=8029, host="0.0.0.0")
        spec = build_service(self.models["second-embed"], self.paths, options, env={}, validate=False)
        self.assertIn("second-embed", spec.argv)
        self.assertNotIn("jina-embed", spec.argv)
        self.assertEqual(spec.label, "com.local-llm-deploy.model.second-embed")
        self.assertEqual(spec.port, 8029)
        self.assertTrue(spec.run_at_load)

    def test_gateway_launchd_preserves_all_effective_gateway_settings(self):
        from local_llm_deploy.gateway.settings import GatewaySettings
        from local_llm_deploy.lifecycle.launchd import plist_data
        env = {
            "API_PROXY_TIMEOUT": "79", "BACKEND_CONNECT_TIMEOUT": "2", "MONITOR_PROXY_TIMEOUT": "4",
            "QUEUE_KEEPALIVE_SEC": "2", "QUEUE_TIMEOUT": "73", "MAX_QUEUE_DEPTH": "19",
            "CHAT_LANE_CONCURRENT": "3", "EMBED_LANE_CONCURRENT": "4", "RERANK_LANE_CONCURRENT": "2",
            "ASR_LANE_CONCURRENT": "2", "KV_CHARS_PER_TOKEN": "3.5", "OLLAMA_HOST": "http://127.0.0.1:11999",
            "OLLAMA_AUTO_DISCOVER": "0", "EXTERNAL_BACKEND_PROBE_TTL": "6", "OLLAMA_CACHE_TTL": "7",
            "KNOWLEDGE_COLLECTOR_URL": "http://127.0.0.1:18888/knowledge", "KNOWLEDGE_PROXY_TIMEOUT": "8",
            "MAX_REQUEST_BODY_BYTES": "32768", "STREAM_BUFFER_CHUNKS": "5", "ACCESS_LOG_CAPTURE_BYTES": "1024",
            "CANCEL_GRACE_SEC": "11", "CLIENT_WRITE_TIMEOUT": "12", "SERVE_UI_ACCESS_LOG": "/tmp/fixture.jsonl",
            "SERVE_UI_LOG_BODY": "1", "DEFAULT_CHAT_MODEL": "chat", "DEFAULT_EMBEDDING_MODEL": "embed",
            "DEFAULT_RERANK_MODEL": "rank", "DEFAULT_ASR_MODEL": "asr", "UNRELATED_SECRET": "do-not-forward",
            "GATEWAY_UNRECOGNIZED_SECRET": "also-do-not-forward",
        }
        spec = build_gateway(self.paths, env=env, validate=False)
        launched_env = plist_data(spec)["EnvironmentVariables"]
        self.assertEqual(GatewaySettings.from_env(launched_env), GatewaySettings.from_env(env))
        self.assertNotIn("UNRELATED_SECRET", launched_env)
        self.assertNotIn("GATEWAY_UNRECOGNIZED_SECRET", launched_env)
        self.assertEqual(launched_env["LOCAL_LLM_ROOT"], str(self.paths.root))
        alias_spec = build_gateway(self.paths, env={"MAX_GLOBAL_CONCURRENT": "6"}, validate=False)
        self.assertEqual(GatewaySettings.from_env(alias_spec.env).chat_concurrent, 6)

    def test_precedence_cli_environment_config(self):
        with patch.dict(os.environ, {}, clear=True):
            spec = build_service(self.models["second-embed"], self.paths, argparse.Namespace(port=8028), env={"JINA_PORT": "8027"}, validate=False)
            self.assertEqual(spec.port, 8028)
            spec = build_service(self.models["second-embed"], self.paths, env={"JINA_PORT": "8027"}, validate=False)
            self.assertEqual(spec.port, 8027)
            spec = build_service(self.models["second-embed"], self.paths, env={}, validate=False)
            self.assertEqual(spec.port, 8024)

    def test_extra_argument_spaces_are_preserved(self):
        spec = build_service(self.models["chat"], self.paths, env={}, validate=False)
        index = spec.argv.index("--chat-template-kwargs")
        self.assertEqual(spec.argv[index + 1], '{"a":"with spaces"}')

    def test_explicit_quant_selects_its_files_and_is_recorded(self):
        self.raw["chat"]["quants"] = {"Q4": {"pattern": "*-Q4.gguf"}, "Q8": {"pattern": "*-Q8.gguf"}}
        model = normalize_models(self.raw)["chat"]
        folder = self.paths.models / "shared"
        folder.mkdir(parents=True)
        (folder / "aaa-Q4.gguf").write_bytes(b"fixture")
        chosen = folder / "zzz-Q8.gguf"
        chosen.write_bytes(b"fixture")
        spec = build_service(model, self.paths, argparse.Namespace(quant="Q8", model_dir=str(folder)), env={}, validate=False)
        self.assertEqual(spec.argv[spec.argv.index("--model") + 1], str(chosen))
        self.assertEqual(spec.quant, "Q8")
        self.assertEqual(spec.as_dict()["quant"], "Q8")

    def test_extra_args_cannot_change_routed_port(self):
        self.raw["chat"]["params"]["extra_args"] = ["--port", "99"]
        model = normalize_models(self.raw)["chat"]
        with self.assertRaisesRegex(LifecycleError, "extra_args"):
            build_service(model, self.paths, env={}, validate=False)

    def test_external_is_not_managed_and_stop_all_skips_it(self):
        code, _, err = self.invoke("start", "external", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("外部", err)
        with patch("local_llm_deploy.cli.stop_service", return_value=False) as stop:
            code, _, err = self.invoke("stop", "--all")
            self.assertEqual(code, 0, err)
            self.assertEqual({call.args[1] for call in stop.call_args_list}, {"second-embed", "chat"})

    def test_plan_is_redacted_and_does_not_create_runtime_files(self):
        code, out, err = self.invoke("plan", "chat", "--api-key", "fixture-secret")
        self.assertEqual(code, 0, err)
        self.assertNotIn("fixture-secret", out)
        self.assertIn("<redacted>", out)
        self.assertFalse(self.paths.run.exists())

    def test_jina_compat_passes_custom_model_name_and_port(self):
        with patch.dict(os.environ, {"JINA_MODEL_NAME": "second-embed", "JINA_EMBED_PORT": "8026"}):
            code, out, err = self.invoke("compat", "jina", "embed", "start", "--dry-run")
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertEqual(plan["key"], "second-embed")
        self.assertEqual(plan["port"], 8026)

    def test_ds4_requires_explicit_root_and_keeps_external_ownership(self):
        with patch.dict(os.environ, {}, clear=True):
            code, _, err = self.invoke("compat", "ds4", "start", "--dry-run")
            self.assertEqual(code, 1)
            self.assertIn("DS4_ROOT", err)
        with patch.dict(os.environ, {"DS4_ROOT": str(self.paths.root / "external ds4")}):
            code, out, err = self.invoke("compat", "ds4", "start", "--dry-run", "--port", "8009")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["port"], 8009)

    def test_manual_deploy_and_unknown_flags_have_explicit_behavior(self):
        code, out, err = self.invoke("deploy", "--model-dir", "models/custom", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["key"], "custom")
        with self.assertRaises(SystemExit) as raised:
            self.invoke("start", "chat", "--mispelled-option")
        self.assertEqual(raised.exception.code, 2)

    def test_compat_help_does_not_require_models(self):
        self.paths.registry.unlink()
        for entry in ("jina", "whisper", "ds4", "serve-ui"):
            code, _, err = self.invoke("compat", entry, "--help")
            self.assertEqual(code, 0, err)

    def test_real_wrapper_is_independent_of_working_directory(self):
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run([str(root / "manage.sh"), "help"], cwd=self.paths.root,
                                capture_output=True, text=True, timeout=10,
                                env={**os.environ, "LOCAL_LLM_PYTHON": sys.executable})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("project-root", result.stdout)


if __name__ == "__main__":
    unittest.main()
