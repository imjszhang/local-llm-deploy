from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from local_llm_deploy.backends.builders import build_mcp_hub, launchd_settings
from local_llm_deploy.cli import main
from local_llm_deploy.config import ProjectPaths
from local_llm_deploy.lifecycle.manager import stop_service
from local_llm_deploy.lifecycle.observe import observe_instances
from local_llm_deploy.lifecycle.runner import decode_spec
from local_llm_deploy.lifecycle.launchd import plist_data
from local_llm_deploy.lifecycle.types import LifecycleError, ServiceSpec
from local_llm_deploy.mcp_hub.config import HubConfigConflict, HubConfigError, HubConfigStore, default_config, validate_config
from local_llm_deploy.mcp_hub.controller import HUB_LABEL, HubController


FAKE_SERVER = '''import http.server, sys
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b'{"status":"ok"}')
    def log_message(self, *args): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
'''


class HubConfigTests(unittest.TestCase):
    def test_explicit_forwarded_hosts(self):
        config = default_config()
        config['allowed_hosts'] = ['10.8.0.5', '10.8.0.5', '::1']
        self.assertEqual(validate_config(config)['allowed_hosts'], ['10.8.0.5', '::1'])
        for value in ['*', '10.8.0.0/24', 'http://10.8.0.5', '10.8.0.5:8899', 123, None]:
            config['allowed_hosts'] = [value]
            with self.assertRaises(HubConfigError):
                validate_config(config)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-config-")
        self.paths = ProjectPaths(Path(self.temp.name))
        self.store = HubConfigStore(self.paths)

    def tearDown(self):
        self.temp.cleanup()

    def test_default_read_is_independent_and_does_not_create_files(self):
        config = self.store.load()
        config["services"].append({})
        self.assertEqual(self.store.load(), default_config())
        self.assertFalse((self.paths.root / "data").exists())

    def test_atomic_save_permissions_and_stale_revision(self):
        config = self.store.load()
        config["listen"]["port"] = 19091
        saved = self.store.save(config)
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.load(), saved)
        with self.assertRaises(HubConfigConflict):
            self.store.save(config)
        self.assertEqual(self.store.load()["listen"]["port"], 19091)
        self.assertEqual(self.store.save(saved)["revision"], 2)

    def test_simultaneous_editors_cannot_overwrite_each_other(self):
        barrier, results = threading.Barrier(2), []
        def save(port):
            store = HubConfigStore(self.paths)
            draft = store.load()
            draft["listen"]["port"] = port
            barrier.wait()
            try:
                results.append(store.save(draft)["listen"]["port"])
            except HubConfigConflict:
                results.append("conflict")
        threads = [threading.Thread(target=save, args=(port,)) for port in (19092, 19093)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results.count("conflict"), 1)
        self.assertEqual(self.store.load()["revision"], 1)

    def test_invalid_types_commands_hosts_and_urls_rejected_without_secret_echo(self):
        invalid = [
            {"listen": {"port": True}}, {"revision": True}, {"schema_version": 2},
            {"listen": {"port": 65536}}, {"listen": {"host": "bad/host"}},
            {"listen": {"host": "192.0.2.1"}},
            {"allowed_origins": ["*"]}, {"allowed_origins": ["https://example.test/path"]},
            {"services": [{"id": "Bad ID", "kind": "stdio", "command": "python"}]},
            {"services": [{"id": "default", "kind": "stdio", "command": "python"}]},
            {"services": [{"id": "worker", "kind": "stdio", "command": "python -c"}]},
            {"services": [{"id": "worker", "kind": "stdio", "command": "./python"}]},
            {"services": [{"id": "worker", "kind": "stdio", "command": "python", "env": {"BAD-NAME": "x"}}]},
            {"services": [{"id": "worker", "kind": "stdio", "command": "python", "args": [1]}]},
            {"services": [{"id": "worker", "kind": "http", "url": "http://user:secret-value@example.test/mcp"}]},
            {"services": [{"id": "worker", "kind": "http", "url": "http://example.test:99999/mcp"}]},
            {"services": [{"id": "worker", "kind": "comfyui", "command": "comfy-mcp", "url": "https://example.test:8188"}]},
            {"services": [{"id": "worker", "kind": "comfyui", "command": "comfy-mcp", "url": "http://example.test/comfy"}]},
            {"services": [{"id": "worker", "kind": "comfyui", "command": "comfy-mcp", "url": ""}]},
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(HubConfigError) as caught:
                    validate_config(value)
                self.assertNotIn("secret-value", str(caught.exception))
        self.assertFalse(self.store.path.exists())

    def test_duplicate_ids_and_unknown_fields_fail(self):
        service = {"id": "same", "kind": "stdio", "command": "python"}
        with self.assertRaises(HubConfigError):
            validate_config({"services": [service, service]})
        with self.assertRaises(HubConfigError):
            validate_config({"unknown": "secret-value"})


class HubControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-controller-")
        self.paths = ProjectPaths(Path(self.temp.name))
        self.controller = HubController(self.paths)

    def tearDown(self):
        try:
            if (self.paths.run / "mcp-hub.pid").exists():
                stop_service(self.paths, "mcp-hub", models={}, timeout=.5)
        finally:
            self.temp.cleanup()

    def test_status_survives_missing_and_invalid_config_without_writing(self):
        row = self.controller.status()
        self.assertEqual(row["status"], "stopped")
        self.assertEqual(row["port"], 8899)
        self.assertFalse(self.paths.run.exists())
        self.assertFalse((self.paths.root / "data").exists())
        self.controller.store.path.parent.mkdir(parents=True)
        self.controller.store.path.write_text("invalid configuration")
        self.assertIsNotNone(self.controller.status()["config_error"])

    def test_builder_uses_isolated_interpreter_and_roundtrips_launchd(self):
        spec = build_mcp_hub(self.paths, env={"UNRELATED_SECRET": "do-not-forward"}, validate=False)
        self.assertEqual(spec.argv[0], str(self.paths.root / ".venv-mcp-hub/bin/python"))
        self.assertIn("local_llm_deploy.mcp_hub.server", spec.argv)
        self.assertEqual(spec.ready_path, "/health")
        self.assertEqual(spec.label, HUB_LABEL)
        self.assertNotIn("UNRELATED_SECRET", spec.env)
        self.assertEqual(launchd_settings("mcp-hub", "")[0], HUB_LABEL)
        launched = plist_data(spec)
        restored = decode_spec(launched["EnvironmentVariables"]["LOCAL_LLM_SERVICE_SPEC"])
        self.assertEqual(restored.argv, spec.argv)
        self.assertEqual(restored.pid_path, self.paths.run / "mcp-hub.pid")

    def test_fake_process_start_restart_and_stop_without_model_registry(self):
        self.paths.registry.write_text("broken models configuration")
        script = self.paths.root / "fake_hub.py"
        script.write_text(FAKE_SERVER)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        spec = ServiceSpec("mcp-hub", "MCP Hub", (sys.executable, str(script), str(port)),
                           self.paths.root, {}, "127.0.0.1", port, self.paths.logs / "mcp-hub.log",
                           self.paths.run / "mcp-hub.pid", ready_timeout=5, label=HUB_LABEL)
        with patch("local_llm_deploy.mcp_hub.controller.build_mcp_hub", return_value=spec):
            first = self.controller.start()
            self.assertEqual(first["status"], "ready")
            self.assertEqual(first["port"], port)
            self.assertTrue(observe_instances(self.paths, {})["mcp-hub"].identity)
            second = self.controller.restart()
            self.assertEqual(second["status"], "ready")
            self.assertNotEqual(first["pid"], second["pid"])
            self.assertEqual(self.controller.stop()["status"], "stopped")
        self.assertFalse((self.paths.run / "mcp-hub.pid").exists())

    def test_restart_holds_control_lock_between_stop_and_start(self):
        spec = build_mcp_hub(self.paths, validate=False)
        def during_stop(*args, **kwargs):
            with self.assertRaisesRegex(LifecycleError, "已有管理操作"):
                HubController(self.paths).start()
        with patch("local_llm_deploy.mcp_hub.controller.build_mcp_hub", return_value=spec), \
                patch("local_llm_deploy.mcp_hub.controller.stop_service", side_effect=during_stop), \
                patch("local_llm_deploy.mcp_hub.controller.start_service") as start:
            self.controller.restart()
            start.assert_called_once()

    def test_control_lock_also_excludes_a_separate_process(self):
        source = str(Path(__file__).resolve().parents[2] / "src")
        code = ("from local_llm_deploy.config import ProjectPaths; "
                "from local_llm_deploy.mcp_hub.controller import HubController; "
                "from pathlib import Path; import sys\n"
                "try:\n"
                " with HubController(ProjectPaths(Path(sys.argv[1]))).operation(): pass\n"
                "except RuntimeError: sys.exit(3)\n")
        with self.controller.operation():
            result = subprocess.run([sys.executable, "-c", code, str(self.paths.root)],
                                    env={**os.environ, "PYTHONPATH": source}, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 3, result.stderr.decode())

    def test_bad_restart_config_leaves_existing_service_untouched(self):
        self.controller.store.path.parent.mkdir(parents=True)
        self.controller.store.path.write_text("not json")
        with patch("local_llm_deploy.mcp_hub.controller.stop_service") as stop:
            with self.assertRaises(HubConfigError):
                self.controller.restart()
            stop.assert_not_called()

    def test_logs_mask_configuration_and_internal_credentials(self):
        config = default_config()
        config["services"] = [{"id": "worker", "kind": "stdio", "command": "python",
                               "args": ["--token", "argument-secret"], "env": {"SERVICE_TOKEN": "environment-secret"}}]
        self.controller.store.save(config)
        self.paths.api_key.write_text("root-secret")
        (self.controller.store.path.parent / "internal.key").write_text("internal-secret")
        self.paths.logs.mkdir()
        (self.paths.logs / "mcp-hub.log").write_text(
            "start\nenvironment-secret root-secret internal-secret argument-secret\nAuthorization: Bearer unknown-value\n"
            "url http://person:credential@example.test/\npassword=private-value\n")
        result = self.controller.logs()
        for secret in ("environment-secret", "root-secret", "internal-secret", "argument-secret", "unknown-value", "credential", "private-value"):
            self.assertNotIn(secret, result)
        self.assertEqual(self.controller.logs(1), "password=<redacted>\n")


class HubCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-cli-")
        self.paths = ProjectPaths(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(["--project-root", str(self.paths.root), *args])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_plan_and_status_do_not_require_model_registry_or_create_files(self):
        code, out, err = self.invoke("plan", "mcp-hub", "--port", "19100", "--no-lan")
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertEqual((plan["host"], plan["port"]), ("127.0.0.1", 19100))
        code, out, err = self.invoke("status", "mcp-hub", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)[0]["status"], "stopped")
        self.assertEqual(list(self.paths.root.iterdir()), [])

    def test_cli_mutations_use_shared_controller(self):
        row = {"status": "ready", "pid": 123, "port": 8899}
        for verb, method in (("start", "start"), ("stop", "stop"), ("restart", "restart")):
            with self.subTest(verb=verb), patch.object(HubController, method, return_value=row) as action:
                code, out, err = self.invoke(verb, "mcp-hub")
                self.assertEqual(code, 0, err)
                action.assert_called_once()
        with patch.object(HubController, "daemon", return_value=row) as daemon:
            code, out, err = self.invoke("daemon", "mcp-hub", "install")
            self.assertEqual(code, 0, err)
            self.assertEqual(daemon.call_args.args[0], "install")
        with patch.object(HubController, "foreground", return_value=0) as foreground:
            self.assertEqual(self.invoke("foreground", "mcp-hub")[0], 0)
            foreground.assert_called_once()

    def test_stop_all_does_not_stop_hub(self):
        self.paths.registry.write_text("{}")
        with patch.object(HubController, "stop") as stop:
            self.assertEqual(self.invoke("stop", "--all")[0], 0)
            stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
