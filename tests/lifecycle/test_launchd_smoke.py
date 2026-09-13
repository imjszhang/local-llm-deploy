"""Opt-in macOS LaunchAgent test; owns only a UUID label and a temporary fake server.

Run: LOCAL_LLM_TEST_LAUNCHD=1 python -m unittest discover -s tests/lifecycle -t . -p test_launchd_smoke.py -v
"""
from __future__ import annotations

import os
import fcntl
import plistlib
import json
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
import uuid

from local_llm_deploy.config import ProjectPaths
from local_llm_deploy.lifecycle import launchd
from local_llm_deploy.lifecycle.manager import start_service, stop_service
from local_llm_deploy.lifecycle.observe import health_ready, launchd_pid, observe_instances, read_pid
from local_llm_deploy.lifecycle.types import ServiceSpec
from tests.lifecycle.test_lifecycle import FAKE_SERVER, free_port


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("LOCAL_LLM_TEST_LAUNCHD") == "1",
                     "explicit isolated macOS launchd test")
class LaunchdSmokeTests(unittest.TestCase):
    def _diagnostics(self, spec, paths):
        # Only this test's UUID job and fake backend log; never print launchctl's
        # EnvironmentVariables (which can contain credentials for real jobs).
        summary = launchd._run("print", launchd.target(spec.label), check=False).stdout
        allowed = ("state =", "pid =", "runs =", "last exit code =", "last terminating signal =", "program =", "active count =")
        details = [line.strip() for line in summary.splitlines() if line.strip().startswith(allowed)]
        log = spec.log_path.read_text(errors="replace")[-12000:] if spec.log_path.exists() else "<missing log>"
        observed = observe_instances(paths, {}, probe=True).get(spec.key)
        return "\n" + json.dumps({"job": details, "observation": observed.as_dict() if observed else None}, indent=2) + "\n" + log

    def test_autostart_runner_publishes_after_ready_and_observation_is_read_only(self):
        label = "com.local-llm-deploy.test." + uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="llm-launchd-no-pid-") as folder:
            paths = ProjectPaths(Path(folder))
            script = paths.root / "fake_server.py"
            script.write_text(FAKE_SERVER)
            port = free_port()
            spec = ServiceSpec("fresh-test", "fresh-test", (sys.executable, str(script), str(port)), paths.root,
                               {"LOCAL_LLM_MANAGED_INSTANCE": "1"}, "127.0.0.1", port,
                               paths.logs / "fake.log", paths.run / "fresh-test.pid", "launchd", label=label,
                               run_at_load=True, keep_alive=False)
            try:
                # Equivalent to a login/bootstrapped RunAtLoad job: no CLI manager is waiting.
                launchd.start(spec)
                deadline = time.monotonic() + 10
                observed = None
                while time.monotonic() < deadline:
                    candidate = observe_instances(paths, {}, probe=True).get(spec.key)
                    if candidate and candidate.status == "ready":
                        observed = candidate
                        break
                    time.sleep(0.1)
                self.assertIsNotNone(observed, "fresh owned LaunchAgent was invisible without a PID file")
                self.assertTrue(spec.pid_path.exists(), "autostart runner must publish the backend")
                before = spec.pid_path.read_bytes(), spec.pid_path.stat().st_mtime_ns
                observe_instances(paths, {}, probe=True)
                self.assertEqual(before, (spec.pid_path.read_bytes(), spec.pid_path.stat().st_mtime_ns))
                self.assertEqual(observed.label, label)
                self.assertTrue(stop_service(paths, spec.key, models={}))
                self.assertFalse(launchd.loaded(label))
                self.assertFalse(health_ready("127.0.0.1", port))
            finally:
                launchd.stop(label)
                launchd.plist_path(label).unlink(missing_ok=True)
                launchd.plist_path(label).with_suffix(".plist.bak").unlink(missing_ok=True)

    def test_legacy_launchagent_entry_supervises_child_and_stops_safely(self):
        label = "com.local-llm-deploy.test." + uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="llm-launchd-entry-") as folder:
            paths = ProjectPaths(Path(folder))
            (paths.root / "fake_server.py").write_text(FAKE_SERVER)
            wrapper = paths.root / "legacy_entry.py"
            wrapper.write_text('''from pathlib import Path
import sys
from local_llm_deploy.config import ProjectPaths
from local_llm_deploy.lifecycle.types import ServiceSpec
from local_llm_deploy.lifecycle.manager import foreground_service
p=ProjectPaths(Path(sys.argv[1])); port=int(sys.argv[2])
s=ServiceSpec("entry-test","entry-test",(sys.executable,str(p.root/"fake_server.py"),str(port)),p.root,{},"127.0.0.1",port,p.logs/"backend.log",p.run/"entry-test.pid",ready_timeout=5)
raise SystemExit(foreground_service(s,p,models={}))
''')
            port = free_port()
            spec = ServiceSpec("entry-test", "entry-test", (sys.executable, str(wrapper), str(paths.root), str(port)),
                               paths.root, {}, "127.0.0.1", port, paths.logs / "wrapper.log", paths.run / "entry-test.pid",
                               "launchd", label=label, run_at_load=True, keep_alive=False)
            try:
                # Simulate a previously installed legacy wrapper, bypassing the new runner.
                path = launchd.install(spec)
                data = plistlib.loads(path.read_bytes())
                data["ProgramArguments"] = list(spec.argv)
                data["EnvironmentVariables"].pop("LOCAL_LLM_SERVICE_SPEC")
                path.write_bytes(plistlib.dumps(data))
                launchd._run("bootstrap", f"gui/{os.getuid()}", str(path))
                deadline = time.monotonic() + 10
                instance = None
                while time.monotonic() < deadline:
                    candidate = observe_instances(paths, {}, probe=True).get(spec.key)
                    if candidate and candidate.status == "ready":
                        instance = candidate
                        break
                    time.sleep(0.1)
                self.assertIsNotNone(instance, "legacy foreground wrapper did not publish its child")
                self.assertNotEqual(instance.pid, launchd_pid(label))
                self.assertTrue(stop_service(paths, spec.key, models={}, label=label, timeout=3))
                self.assertFalse(launchd.loaded(label))
                self.assertFalse(health_ready("127.0.0.1", port))
                self.assertFalse(spec.pid_path.exists())
            finally:
                launchd.stop(label)
                launchd.plist_path(label).unlink(missing_ok=True)
                launchd.plist_path(label).with_suffix(".plist.bak").unlink(missing_ok=True)

    def test_temporary_agent_start_crash_recovery_stop_and_uninstall(self):
        label = "com.local-llm-deploy.test." + uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="llm-launchd-test-") as folder:
            paths = ProjectPaths(Path(folder))
            script = paths.root / "fake_server.py"
            script.write_text(FAKE_SERVER)
            port = free_port()
            spec = ServiceSpec("isolated-test", "isolated-test", (sys.executable, str(script), str(port)), paths.root,
                               {}, "127.0.0.1", port, paths.logs / "fake.log", paths.run / "isolated-test.pid",
                               "launchd", ready_timeout=15, label=label, run_at_load=True,
                               keep_alive={"SuccessfulExit": False})
            try:
                first = start_service(spec, paths, models={})
                self.assertNotEqual(launchd_pid(label), first.pid)
                self.assertEqual(read_pid(spec.pid_path)[3]["wrapper_pid"], launchd_pid(label))
                self.assertTrue(health_ready("127.0.0.1", port))
                os.kill(first.pid, signal.SIGKILL)
                deadline = time.monotonic() + 35
                restarted = None
                while time.monotonic() < deadline:
                    current = observe_instances(paths, {}, probe=True).get(spec.key)
                    if current and current.pid != first.pid and current.status == "ready":
                        restarted = current
                        break
                    time.sleep(0.25)
                self.assertIsNotNone(restarted, "KeepAlive did not recover the isolated fake backend" +
                                     (self._diagnostics(spec, paths) if restarted is None else ""))
                self.assertTrue(stop_service(paths, spec.key, models={}, label=label, timeout=3))
                self.assertFalse(launchd.loaded(label))
                self.assertFalse(health_ready("127.0.0.1", port))
                self.assertFalse(spec.pid_path.exists())
                launchd.uninstall(label, root=paths.root)
                self.assertFalse(launchd.plist_path(label).exists())
            finally:
                # UUID prevents any overlap with a user's model jobs.
                launchd.stop(label)
                launchd.plist_path(label).unlink(missing_ok=True)
                launchd.plist_path(label).with_suffix(".plist.bak").unlink(missing_ok=True)

    def test_autostart_cannot_load_or_publish_while_deletion_holds_installation_lock(self):
        label = "com.local-llm-deploy.test." + uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="llm-launchd-lock-") as folder:
            paths = ProjectPaths(Path(folder))
            paths.run.mkdir()
            model_path = paths.root / "weights"
            model_path.mkdir()
            script = paths.root / "fake_server.py"
            marker = paths.root / "load-started"
            script.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n" + FAKE_SERVER)
            port = free_port()
            spec = ServiceSpec("locked-test", "locked-test", (sys.executable, str(script), str(port)), paths.root,
                               {}, "127.0.0.1", port, paths.logs / "fake.log", paths.run / "locked-test.pid",
                               "launchd", ready_timeout=10, label=label, run_at_load=True, model_path=model_path)
            try:
                with (paths.run / ".artifacts.lock").open("a") as stream:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                    launchd.start(spec)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline and not launchd_pid(label):
                        time.sleep(0.05)
                    self.assertIsNotNone(launchd_pid(label))
                    time.sleep(0.5)
                    self.assertFalse(marker.exists(), "backend loaded while deletion held the exclusive lock")
                    self.assertFalse(spec.pid_path.exists())
                    observation = observe_instances(paths, {}).get(spec.key)
                    self.assertIsNotNone(observation)
                    self.assertEqual(observation.status, "starting")
                    self.assertEqual(observation.model_path, str(model_path))
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not spec.pid_path.exists():
                    time.sleep(0.1)
                self.assertTrue(marker.exists())
                self.assertTrue(spec.pid_path.exists())
                self.assertEqual(observe_instances(paths, {}, probe=True)[spec.key].status, "ready")
                self.assertTrue(stop_service(paths, spec.key, models={}))
            finally:
                launchd.stop(label)
                launchd.plist_path(label).unlink(missing_ok=True)
                launchd.plist_path(label).with_suffix(".plist.bak").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
