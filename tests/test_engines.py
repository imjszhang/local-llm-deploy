from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from local_llm_deploy.config import ConfigError, ProjectPaths
from local_llm_deploy.engines import load_profiles, main, register_profile, resolve_engine, use_profile


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = ProjectPaths(Path(self.temp.name))

    def engine(self, name):
        path = self.paths.root / name
        (path / "build/bin").mkdir(parents=True)
        (path / ".gitignore").write_text("build/\n")
        binary = path / "build/bin/llama-server"
        binary.write_text('#!/bin/sh\necho "fixture-engine 1.0"\n')
        binary.chmod(0o755)
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(path), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                        "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"], check=True)
        return path

    def test_profiles_select_and_rollback_without_touching_builds(self):
        first, second = self.engine("stable"), self.engine("candidate")
        register_profile(self.paths, "stable", first, make_default=True)
        register_profile(self.paths, "candidate", second)
        self.assertEqual(resolve_engine(self.paths)["directory"], str(first))
        use_profile(self.paths, "candidate")
        self.assertEqual(resolve_engine(self.paths)["directory"], str(second))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["rollback"], paths=self.paths), 0)
            self.assertEqual(main(["verify", "stable"], paths=self.paths), 0)
        self.assertEqual(resolve_engine(self.paths)["directory"], str(first))
        self.assertTrue((second / "build/bin/llama-server").is_file())

    def test_repeated_use_preserves_previous_rollback_target(self):
        first, second = self.engine("stable"), self.engine("candidate")
        register_profile(self.paths, "stable", first, make_default=True)
        register_profile(self.paths, "candidate", second)
        use_profile(self.paths, "candidate")
        use_profile(self.paths, "candidate")
        self.assertEqual(load_profiles(self.paths)["previous"], "stable")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["rollback"], paths=self.paths), 0)
        self.assertEqual(load_profiles(self.paths)["default"], "stable")

    def test_register_default_remembers_previous_profile(self):
        first, second = self.engine("stable"), self.engine("candidate")
        register_profile(self.paths, "stable", first, make_default=True)
        register_profile(self.paths, "candidate", second, make_default=True)
        self.assertEqual(load_profiles(self.paths)["previous"], "stable")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["rollback"], paths=self.paths), 0)
        self.assertEqual(load_profiles(self.paths)["default"], "stable")

    def test_snapshot_subdirectory_cannot_borrow_ancestor_git_revision(self):
        parent = self.engine("ancestor")
        snapshot = parent / "snapshot"
        binary = snapshot / "build/bin/llama-server"
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/bin/sh\necho "fixture-snapshot"\n')
        binary.chmod(0o755)
        # All snapshot content is ignored by the ancestor's build/ rule, so
        # rejecting dirty trees alone cannot detect this false provenance.
        self.assertEqual(subprocess.check_output(["git", "-C", str(snapshot), "status", "--porcelain"], text=True), "")
        with self.assertRaisesRegex(ConfigError, "根目录"):
            register_profile(self.paths, "snapshot", snapshot)
        errors = io.StringIO()
        with patch.dict(os.environ, {"CPP_DIR": str(snapshot)}), contextlib.redirect_stderr(errors):
            self.assertEqual(main(["verify"], paths=self.paths), 1)
        self.assertIn("根目录", errors.getvalue())
        self.assertFalse((self.paths.root / "engines.json").exists())

    def test_dirty_engine_cannot_be_recorded_as_reproducible(self):
        engine = self.engine("dirty")
        (engine / "changed.cpp").write_text("uncommitted")
        with self.assertRaises(ConfigError):
            register_profile(self.paths, "dirty", engine)

    def test_override_and_missing_profile(self):
        with self.assertRaises(ConfigError):
            resolve_engine(self.paths, "absent")
        value = resolve_engine(self.paths, "absent", cpp_dir="separate")
        self.assertEqual(value["directory"], str(self.paths.root / "separate"))

    def test_immutable_names_and_full_revision_required(self):
        engine = self.engine("first")
        register_profile(self.paths, "first", engine)
        with self.assertRaises(ConfigError):
            register_profile(self.paths, "first", engine)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["update", "candidate", "--revision", "main"], paths=self.paths), 1)
        self.assertFalse((self.paths.root / "work_dir").exists())


if __name__ == "__main__":
    unittest.main()
