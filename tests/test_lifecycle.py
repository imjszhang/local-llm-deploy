from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from local_llm_deploy.config import ConfigError, ProjectPaths, normalize_models
from local_llm_deploy.lifecycle import process
from local_llm_deploy.lifecycle.launchd import plist_data
from local_llm_deploy.lifecycle.manager import instance_lock, start_service, stop_service
from local_llm_deploy.lifecycle.observe import observe_instances, process_identity, publish_pid, read_pid, write_pid
from local_llm_deploy.lifecycle.types import LifecycleError, ProcessIdentity, ServiceSpec


FAKE_SERVER = '''import http.server, os, sys, time
port = int(sys.argv[1]); delay = float(sys.argv[2]) if len(sys.argv)>2 else 0
started = time.monotonic()
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        ready = time.monotonic()-started >= delay
        body = b'{"status":"ok"}' if ready else b'{"status":"loading"}'
        self.send_response(200 if ready else 503); self.end_headers(); self.wfile.write(body)
    def log_message(self, *args): pass
http.server.HTTPServer(("127.0.0.1",port), Handler).serve_forever()
'''


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="llm-lifecycle-")
        self.paths = ProjectPaths(Path(self.temp.name))
        self.script = self.paths.root / "fake_backend.py"
        self.script.write_text(FAKE_SERVER)
        self.spec = ServiceSpec("test", "test-alias", (sys.executable, str(self.script), str(free_port())),
                                self.paths.root, {}, "127.0.0.1", 0, self.paths.logs / "test.log",
                                self.paths.run / "test.pid", ready_timeout=4)
        self.spec = replace(self.spec, port=int(self.spec.argv[-1]))

    def tearDown(self):
        try:
            stop_service(self.paths, "test", models={}, timeout=0.3)
        finally:
            self.temp.cleanup()

    def test_start_publishes_only_after_ready_and_stop_is_idempotent(self):
        spec = replace(self.spec, argv=self.spec.argv + ("0.7",))
        errors = []
        thread = threading.Thread(target=lambda: self._start_capture(spec, errors))
        thread.start()
        time.sleep(0.2)
        self.assertFalse(spec.pid_path.exists(), "starting process must not be published ready")
        thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        record = read_pid(spec.pid_path)
        self.assertTrue(record[3]["ready"])
        obs = observe_instances(self.paths, {})["test"]
        self.assertEqual(obs.status, "ready")
        self.assertEqual(obs.port, spec.port)
        with self.assertRaises(LifecycleError):
            start_service(spec, self.paths, models={})
        self.assertTrue(stop_service(self.paths, "test", models={}, timeout=0.3))
        self.assertFalse(stop_service(self.paths, "test", models={}, timeout=0.3))

    def _start_capture(self, spec, errors):
        try:
            start_service(spec, self.paths, models={})
        except Exception as exc:
            errors.append(exc)

    def test_port_conflict_leaves_owner_untouched(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0)); listener.listen()
            port = listener.getsockname()[1]
            spec = replace(self.spec, port=port, argv=self.spec.argv[:-1] + (str(port),))
            with patch("local_llm_deploy.lifecycle.manager.process.spawn") as spawn:
                with self.assertRaisesRegex(LifecycleError, "未终止任何进程"):
                    start_service(spec, self.paths, models={})
                spawn.assert_not_called()
            self.assertEqual(listener.getsockname()[1], port)

    def test_port_preflight_allows_closed_http_connection_time_wait(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            with socket.create_connection(("127.0.0.1", port)) as client:
                accepted, _ = listener.accept()
                # Server closes first, leaving its accepted connection in
                # TIME_WAIT after the peer receives EOF and closes.
                accepted.close()
                self.assertEqual(client.recv(1), b"")
        process.check_port("127.0.0.1", port)
        # The same socket policy must still refuse a genuinely live listener.
        with socket.socket() as live:
            live.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            live.bind(("127.0.0.1", port))
            live.listen()
            with self.assertRaisesRegex(LifecycleError, "未终止任何进程"):
                process.check_port("127.0.0.1", port)

    def test_failed_start_cleans_owned_child_without_ready_record(self):
        spec = replace(self.spec, argv=self.spec.argv + ("10",), ready_timeout=0.25)
        spawned = []
        real_spawn = process.spawn
        def capture(service):
            child = real_spawn(service); spawned.append(child); return child
        with patch("local_llm_deploy.lifecycle.manager.process.spawn", side_effect=capture):
            with self.assertRaisesRegex(LifecycleError, "超时"):
                start_service(spec, self.paths, models={})
        self.assertFalse(spec.pid_path.exists())
        self.assertIsNotNone(spawned[0].poll())

    def test_process_exit_before_ready_is_failure(self):
        spec = replace(self.spec, argv=(sys.executable, "-c", "raise SystemExit(7)"))
        with self.assertRaisesRegex(LifecycleError, "退出码 7"):
            start_service(spec, self.paths, models={})
        self.assertFalse(spec.pid_path.exists())

    def test_observe_never_cleans_stale_or_reused_pid(self):
        self.paths.run.mkdir()
        record = self.paths.run / "test.pid"
        old = ProcessIdentity(424242, "old-start", "old-hash", "/old")
        write_pid(record, old.pid, 9000, "test", identity=old)
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.paths.run.iterdir()}
        replacement = ProcessIdentity(old.pid, "new-start", "new-hash", "/unrelated")
        with patch("local_llm_deploy.lifecycle.observe.process_identity", return_value=replacement):
            observation = observe_instances(self.paths, {})["test"]
            self.assertIsNone(observation.pid)
            self.assertEqual(observation.observed_pid, old.pid)
            self.assertEqual(observation.status, "stale")
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.paths.run.iterdir()}
        self.assertEqual(before, after)

    def test_stop_does_not_signal_reused_pid(self):
        self.paths.run.mkdir()
        old = ProcessIdentity(424242, "old", "old-hash", "old")
        write_pid(self.spec.pid_path, old.pid, self.spec.port, "test", identity=old)
        replacement = ProcessIdentity(old.pid, "new", "new-hash", "/unrelated")
        with patch("local_llm_deploy.lifecycle.observe.process_identity", return_value=replacement), patch("os.kill") as kill:
            self.assertFalse(stop_service(self.paths, "test", models={}))
            kill.assert_not_called()

    def test_legacy_pid_accepts_exact_service_and_model(self):
        self.paths.run.mkdir()
        (self.paths.run / "embed.pid").write_text("424242\n8004\nembed-alias\n")
        models = normalize_models({"embed": {"type": "embedding", "repo_id": "x/y", "alias": "embed-alias"}})
        valid = ProcessIdentity(424242, "now", "hash", f"/python {self.paths.root}/serve_embedding.py --model-name embed --port 8004")
        with patch("local_llm_deploy.lifecycle.observe.process_identity", return_value=valid):
            self.assertEqual(observe_instances(self.paths, models)["embed"].status, "running")
        with patch("local_llm_deploy.lifecycle.observe.process_identity", return_value=replace(valid, command=valid.command.replace("--model-name embed", "--model-name embed-other"))):
            self.assertEqual(observe_instances(self.paths, models)["embed"].status, "stale")

    def test_same_instance_operations_are_mutually_exclusive(self):
        with instance_lock(self.paths, "test"):
            with self.assertRaisesRegex(LifecycleError, "正在执行"):
                with instance_lock(self.paths, "test"):
                    self.fail("lock was not held")

    def test_plist_preserves_argv_environment_and_policies(self):
        from local_llm_deploy.lifecycle.runner import decode_spec, PAYLOAD_ENV
        spec = replace(self.spec, label="com.local-llm-deploy.test", management="launchd", run_at_load=True,
                       keep_alive={"SuccessfulExit": False}, env={"MODEL": "model with spaces"})
        data = plist_data(spec)
        restored = decode_spec(data["EnvironmentVariables"][PAYLOAD_ENV])
        self.assertEqual(restored.argv, spec.argv)
        self.assertEqual(restored.model_path, spec.model_path)
        self.assertTrue("__service-runner" in data["ProgramArguments"] or
                        data["ProgramArguments"][1:] == ["-m", "local_llm_deploy.lifecycle.runner"])
        self.assertEqual(data["EnvironmentVariables"]["MODEL"], "model with spaces")
        self.assertTrue(data["RunAtLoad"])
        self.assertEqual(data["KeepAlive"], {"SuccessfulExit": False})

    def test_launchd_install_and_uninstall_refuse_another_checkout(self):
        from local_llm_deploy.lifecycle import launchd
        from local_llm_deploy.lifecycle.runner import decode_spec, PAYLOAD_ENV
        spec = replace(self.spec, label="com.local-llm-deploy.test", api_key="test-secret", management="launchd")
        home = self.paths.root / "test-home"
        with patch.object(launchd, "require_macos"), patch.object(launchd, "loaded", return_value=False):
            path = launchd.install(spec, home=home)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            original = path.read_bytes()
            other = replace(spec, cwd=self.paths.root / "other")
            with self.assertRaisesRegex(LifecycleError, "其他项目"):
                launchd.install(other, home=home)
            with self.assertRaisesRegex(LifecycleError, "其他项目"):
                launchd.uninstall(spec.label, root=other.cwd, home=home)
            self.assertEqual(path.read_bytes(), original)
            self.assertNotIn("test-secret", json.dumps(spec.as_dict()))
            self.assertEqual(decode_spec(plist_data(spec)["EnvironmentVariables"][PAYLOAD_ENV]).api_key, "test-secret")
            launchd.uninstall(spec.label, root=self.paths.root, home=home)
            self.assertFalse(path.exists())

    def test_launchd_owner_is_project_root_even_with_external_working_directory(self):
        from local_llm_deploy.lifecycle import launchd
        external = self.paths.root / "external-engine"
        spec = replace(self.spec, label="com.local-llm-deploy.ds4-test", cwd=external,
                       env={"LOCAL_LLM_ROOT": str(self.paths.root)}, management="launchd")
        home = self.paths.root / "test-home"
        with patch.object(launchd, "require_macos"), patch.object(launchd, "loaded", return_value=False):
            path = launchd.install(spec, home=home)
            launchd.install(spec, home=home)
            self.assertTrue(path.with_suffix(".plist.bak").exists())
            self.assertEqual(path.with_suffix(".plist.bak").stat().st_mode & 0o777, 0o600)
            launchd.uninstall(spec.label, root=self.paths.root, home=home)

    def test_observation_retains_auth_location_without_secret(self):
        key_file = self.paths.root / "test-api-key"
        key_file.write_text("private-test-value")
        publish_pid(self.paths, "direct", 8123, "direct", api_key_file=key_file)
        observed = observe_instances(self.paths, {})["direct"]
        self.assertEqual(observed.api_key_file, str(key_file))
        self.assertEqual(observed.auth_source, "file")
        self.assertNotIn("private-test-value", json.dumps(observed.as_dict()))

    def test_managed_inline_credential_is_never_written_to_pid_metadata(self):
        spec = replace(self.spec, api_key="inline-test-secret")
        start_service(spec, self.paths, models={})
        observed = observe_instances(self.paths, {})[spec.key]
        self.assertEqual(observed.auth_source, "inline")
        self.assertNotIn("inline-test-secret", spec.pid_path.read_text())

    def test_listener_must_belong_to_started_pid(self):
        spec = replace(self.spec, ready_timeout=0.2)
        with patch("local_llm_deploy.lifecycle.manager.process.owns_listener", return_value=False):
            with self.assertRaisesRegex(LifecycleError, "超时"):
                start_service(spec, self.paths, models={})
        self.assertFalse(spec.pid_path.exists())

    def test_foreground_forwards_termination_and_removes_record(self):
        code = '''from pathlib import Path
import sys
from local_llm_deploy.config import ProjectPaths
from local_llm_deploy.lifecycle.types import ServiceSpec
from local_llm_deploy.lifecycle.manager import foreground_service
p=ProjectPaths(Path(sys.argv[1])); port=int(sys.argv[2])
s=ServiceSpec("test","test",(sys.executable,str(p.root/"fake_backend.py"),str(port)),p.root,{},"127.0.0.1",port,p.logs/"test.log",p.run/"test.pid",ready_timeout=3)
raise SystemExit(foreground_service(s,p,models={}))
'''
        wrapper = subprocess.Popen([sys.executable, "-c", code, str(self.paths.root), str(self.spec.port)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not self.spec.pid_path.exists() and wrapper.poll() is None:
                time.sleep(0.05)
            record = read_pid(self.spec.pid_path)
            self.assertIsNotNone(record, wrapper.stderr.read().decode() if wrapper.poll() is not None else "not ready")
            backend_pid = record[0]
            wrapper.terminate()
            self.assertEqual(wrapper.wait(timeout=5), 143)
            self.assertFalse(self.spec.pid_path.exists())
            self.assertIsNone(process_identity(backend_pid))
        finally:
            if wrapper.poll() is None:
                wrapper.terminate(); wrapper.wait(timeout=5)
            wrapper.stderr.close()

    def test_direct_service_installation_record_blocks_shared_weight_deletion(self):
        from local_llm_deploy.artifacts.inventory import remove_installations
        raw = {"chat": {"repo_id": "x/chat", "default_quant": "Q4", "quants": {"Q4": {}}}}
        self.paths.registry.write_text(json.dumps(raw))
        target = self.paths.models / "x-chat/Q4"
        target.mkdir(parents=True)
        (target / "weights.gguf").write_bytes(b"fixture")
        # A direct service can use the same installation without a registered model key.
        publish_pid(self.paths, "direct", 8123, "direct", model_path=target)
        with self.assertRaisesRegex(ConfigError, "正在使用或可能使用"):
            remove_installations(normalize_models(raw)["chat"], self.paths, quant="Q4")
        self.assertTrue((target / "weights.gguf").is_file())

    def test_process_stop_ignores_other_checkout_launchd_with_same_key(self):
        identity = start_service(self.spec, self.paths, models={})
        # The same service key/label may already be used in another checkout.
        with patch("local_llm_deploy.lifecycle.manager.launchd.loaded", return_value=True) as loaded, \
             patch("local_llm_deploy.lifecycle.manager.launchd.stop") as bootout:
            self.assertTrue(stop_service(self.paths, "test", models={},
                                         label="com.local-llm-deploy.test", timeout=0.3))
            loaded.assert_not_called()
            bootout.assert_not_called()
        self.assertIsNone(process_identity(identity.pid))


if __name__ == "__main__":
    unittest.main()
