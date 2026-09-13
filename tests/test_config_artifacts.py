from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from local_llm_deploy.config import ConfigError, ProjectPaths, load_env_file, load_models, load_specs, normalize_models
from local_llm_deploy.registry import Registry
from local_llm_deploy.artifacts.paths import chat_weights_complete, primary_model_file, resolve_installation, resolve_target_dir, weights_complete
from local_llm_deploy.artifacts.manifest import Manifest
from local_llm_deploy.artifacts.download import plan_download, execute_download
from local_llm_deploy.artifacts.inventory import installations, removal_plan, remove_installations
from local_llm_deploy.storage import atomic_json


def chat_config(**overrides):
    cfg = {"repo_id": "example/model", "default_quant": "Q4", "default_port": 8123,
           "quants": {"Q4": {"pattern": "*.gguf"}}, "params": {"ctx_size": 1024}}
    cfg.update(overrides)
    return cfg


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = ProjectPaths(Path(self.temp.name))
        atomic_json(self.paths.registry, {"chat": chat_config(), "_comment": "ignored"})

    def test_normalization_preserves_extension_and_separates_capabilities(self):
        raw = {"embed": {"type": "embedding", "extension": {"x": 1}},
               "ollama": {"type": "ollama"}, "_notes": "comment"}
        specs = normalize_models(raw)
        self.assertEqual(specs["embed"].backend, "transformers_embedding")
        self.assertEqual(specs["ollama"].capabilities, ("chat",))
        self.assertEqual(specs["ollama"].management, "external")
        self.assertEqual(specs["embed"].raw["extension"], {"x": 1})
        specs["embed"].raw["extension"]["x"] = 2
        self.assertEqual(raw["embed"]["extension"]["x"], 1)

    def test_duplicate_names_and_defaults_rejected(self):
        for raw in ({"one": {"alias": "two"}, "two": {}},
                    {"one": {"default_for": ["chat"]}, "two": {"default_for": ["chat"]}}):
            with self.assertRaises(ConfigError):
                normalize_models(raw)

    def test_invalid_config_rejected_with_context(self):
        for raw in ({"../bad": {}}, {"good": {"default_port": -1}},
                    {"good": {"params": {"extra_args": "--jinja"}}},
                    {"good": {"repo_name": "../weights"}}):
            with self.assertRaises(ConfigError):
                normalize_models(raw)

    def test_exported_environment_overrides_literal_file(self):
        file = self.paths.root / "test.env"
        file.write_text('export HF_ENDPOINT="https://file.invalid"\n# ignore\nVALUE=$(touch never)\n')
        values = load_env_file(file, {"HF_ENDPOINT": "https://env.invalid"})
        self.assertEqual(values["HF_ENDPOINT"], "https://env.invalid")
        self.assertEqual(values["VALUE"], "$(touch never)")
        self.assertFalse((self.paths.root / "never").exists())

    def test_cache_does_not_publish_invalid_update(self):
        registry = Registry(self.paths, ttl=0)
        self.assertEqual(set(registry.load_raw()), {"chat"})
        self.paths.registry.write_text("not json")
        with self.assertRaises(ConfigError):
            registry.load_raw(refresh=True)
        registry.ttl = 999999
        self.assertEqual(set(registry.load_raw()), {"chat"})

    def test_concurrent_registry_updates_are_not_lost(self):
        def add(i):
            Registry(self.paths).update(lambda raw: raw.update({f"m{i}": {"type": "ollama"}}))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(add, range(16)))
        self.assertEqual(len(load_specs(self.paths.registry)), 17)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = ProjectPaths(Path(self.temp.name))
        atomic_json(self.paths.registry, {"chat": chat_config()})
        self.spec = load_specs(self.paths.registry)["chat"]

    def weights(self, name="example-model/Q4"):
        path = self.paths.models / name
        path.mkdir(parents=True)
        (path / "model.gguf").write_bytes(b"fake-weights")
        return path

    def test_default_explicit_and_registered_path_selection(self):
        default = self.weights()
        self.assertEqual(resolve_installation(self.spec, self.paths).path, default)
        other = self.weights("other with spaces")
        Manifest(self.paths).record("chat", "Q4", other, revision="abc")
        selected = resolve_installation(self.spec, self.paths)
        self.assertEqual(selected.path, other)
        self.assertEqual(selected.revision, "abc")
        self.assertTrue(selected.complete)
        Manifest(self.paths).record("chat", "Q4", default)
        with self.assertRaises(ConfigError):
            resolve_installation(self.spec, self.paths)
        self.assertEqual(resolve_installation(self.spec, self.paths, explicit=other).path, other)

    def test_nested_shards_require_all_parts(self):
        path = self.paths.models / "nested" / "Q4"
        path.mkdir(parents=True)
        (path / "model-00001-of-00002.gguf").write_bytes(b"part1")
        self.assertFalse(chat_weights_complete(path.parent))
        (path / "model-00002-of-00002.gguf").write_bytes(b"part2")
        self.assertTrue(chat_weights_complete(path.parent))

    def test_concurrent_manifest_updates_are_atomic(self):
        def add(i):
            Manifest(self.paths).record("chat", "Q4", self.paths.models / f"weight{i}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(add, range(20)))
        self.assertEqual(len(Manifest(self.paths).entries("chat")), 20)

    def test_repo_root_variants_select_requested_quant_and_reject_ambiguous_gguf(self):
        cfg = chat_config(download_target_mode="repo_root", quants={"Q4": {"pattern": "Q4/*Q4.gguf"}, "Q5": {"pattern": "Q5/*Q5.gguf"}})
        spec = normalize_models({"chat": cfg})["chat"]
        for quant in ("Q4", "Q5"):
            path = self.paths.models / "example-model" / quant
            path.mkdir(parents=True)
            (path / f"model-{quant}.gguf").write_bytes(b"fake")
        install = resolve_installation(spec, self.paths, quant="Q5")
        self.assertEqual(install.path.name, "Q5")
        self.assertTrue(install.complete)
        self.assertEqual(primary_model_file(spec, install.path, "Q5").name, "model-Q5.gguf")
        (install.path / "other-Q5.gguf").write_bytes(b"fake")
        with self.assertRaises(ConfigError):
            primary_model_file(spec, install.path, "Q5")

    def test_root_and_symlink_escape_cannot_be_deleted(self):
        self.paths.models.mkdir()
        outside = self.paths.root / "precious"
        outside.mkdir()
        (self.paths.models / "link").symlink_to(outside, target_is_directory=True)
        for target in (self.paths.models, self.paths.models / "link"):
            with self.assertRaises(ConfigError):
                removal_plan(self.spec, self.paths, explicit=target)
        self.assertTrue(outside.exists())

    def test_delete_only_selected_registered_installation(self):
        selected = self.weights("selected")
        untouched = self.weights("untouched")
        manifest = Manifest(self.paths)
        manifest.record("chat", "Q4", selected)
        manifest.record("chat", "Q4", untouched)
        with contextlib.redirect_stdout(io.StringIO()):
            remove_installations(self.spec, self.paths, explicit=selected)
        self.assertFalse(selected.exists())
        self.assertTrue(untouched.exists())
        self.assertEqual(len(manifest.entries("chat")), 1)

    def test_download_plan_precedence_and_revision(self):
        spec = normalize_models({"chat": chat_config(download_source="huggingface", revision="a1")})["chat"]
        plan = plan_download(spec, self.paths, env={"DOWNLOAD_SOURCE": "modelscope"})
        self.assertEqual(plan.source, "modelscope")
        self.assertEqual(plan.revision, "a1")
        plan = plan_download(spec, self.paths, source="huggingface", revision="b2", env={})
        self.assertEqual(plan.source, "huggingface")
        self.assertEqual(plan.revision, "b2")

    def test_live_shared_weight_cannot_be_removed_or_overwritten(self):
        from local_llm_deploy.lifecycle.observe import write_pid
        selected = self.weights()
        atomic_json(self.paths.registry, {"chat": chat_config(), "other": chat_config(alias="other-alias")})
        write_pid(self.paths.run / "other.pid", os.getpid(), 8123, "other-alias", metadata={"ready": True})
        with self.assertRaises(ConfigError):
            remove_installations(self.spec, self.paths, quant="Q4")
        plan = plan_download(self.spec, self.paths, env={})
        with self.assertRaises(ConfigError):
            execute_download(plan, self.spec, self.paths, env={}, downloader=lambda *a, **kw: self.fail("must not download"))
        self.assertTrue(selected.is_dir())

    def test_live_configured_path_is_listed_and_protected_with_legacy_pid(self):
        from local_llm_deploy.lifecycle.observe import write_pid
        selected = self.weights("configured")
        atomic_json(self.paths.registry, {"chat": chat_config(model_path="models/configured")})
        spec = load_specs(self.paths.registry)["chat"]
        write_pid(self.paths.run / "chat.pid", os.getpid(), 8123, "chat")
        self.assertIn(selected, {entry["path"] for entry in installations(spec, self.paths)})
        with self.assertRaises(ConfigError):
            remove_installations(spec, self.paths, quant="Q4")
        plan = plan_download(spec, self.paths, to_path="configured", env={})
        with self.assertRaises(ConfigError):
            execute_download(plan, spec, self.paths, env={}, downloader=lambda *a, **kw: self.fail("must not download"))
        self.assertTrue(selected.exists())

    def test_manifest_preserves_multiple_quants_in_custom_repo_root(self):
        spec = normalize_models({"chat": chat_config(download_target_mode="repo_root", quants={
            "Q4": {"pattern": "*Q4.gguf"}, "Q5": {"pattern": "*Q5.gguf"}})})["chat"]
        custom = self.paths.models / "custom"
        custom.mkdir(parents=True)
        for quant in ("Q4", "Q5"):
            (custom / f"model-{quant}.gguf").write_bytes(b"weights")
            Manifest(self.paths).record("chat", quant, custom, revision=f"rev-{quant}")
        self.assertEqual(len(Manifest(self.paths).entries("chat")), 2)
        for quant in ("Q4", "Q5"):
            installed = resolve_installation(spec, self.paths, quant=quant)
            self.assertTrue(installed.complete)
            self.assertEqual(installed.path, custom)
            self.assertEqual(installed.revision, f"rev-{quant}")
        with self.assertRaises(ConfigError):
            removal_plan(spec, self.paths, quant="Q4")

    def test_safetensors_index_requires_all_referenced_shards(self):
        spec = normalize_models({"embed": {"type": "embedding", "repo_id": "example/embed"}})["embed"]
        path = self.paths.models / "partial"
        path.mkdir(parents=True)
        (path / "config.json").write_text("{}")
        (path / "part-1.safetensors").write_bytes(b"weights")
        index = path / "model.safetensors.index.json"
        index.write_text(json.dumps({"weight_map": {"one": "part-1.safetensors", "two": "part-2.safetensors"}}))
        self.assertFalse(weights_complete(spec, path))
        plan = plan_download(spec, self.paths, to_path="partial", env={})
        with self.assertRaises(ConfigError):
            execute_download(plan, spec, self.paths, env={}, downloader=lambda *a, **kw: None)
        self.assertEqual(Manifest(self.paths).entries("embed"), [])
        (path / "part-2.safetensors").write_bytes(b"weights")
        self.assertTrue(weights_complete(spec, path))
        for data in ({"weight_map": {"one": "../../outside"}}, {"weight_map": {}}, [], {"weight_map": {"one": []}}):
            index.write_text(json.dumps(data))
            self.assertFalse(weights_complete(spec, path))

    def test_fresh_launchagent_explicit_weights_are_protected_without_pid_file(self):
        from local_llm_deploy.lifecycle.types import InstanceObservation
        selected = self.weights("explicit-launchd")
        Manifest(self.paths).record("chat", "Q4", selected)
        instance = InstanceObservation("not-in-registry", os.getpid(), 8123, "agent", "starting",
                                       "launchd", model_path=str(selected))
        with mock.patch("local_llm_deploy.lifecycle.observe.observe_instances", return_value={instance.key: instance}):
            with self.assertRaises(ConfigError):
                remove_installations(self.spec, self.paths, explicit=selected)
        self.assertTrue(selected.exists())

    def test_fake_download_records_only_complete_results(self):
        plan = plan_download(self.spec, self.paths, source="huggingface", revision="abc123", env={})
        calls = []
        def fake(repo, **kwargs):
            calls.append((repo, kwargs))
            (Path(kwargs["local_dir"]) / "model.gguf").write_bytes(b"fake")
        execute_download(plan, self.spec, self.paths, env={}, downloader=fake)
        self.assertEqual(calls[0][1]["revision"], "abc123")
        self.assertEqual(Manifest(self.paths).entries("chat")[0]["revision"], "abc123")
        broken = plan_download(self.spec, self.paths, to_path="broken", env={})
        with self.assertRaises(ConfigError):
            execute_download(broken, self.spec, self.paths, env={}, downloader=lambda *a, **kw: None)
        self.assertEqual(len(Manifest(self.paths).entries("chat")), 1)


if __name__ == "__main__":
    unittest.main()
