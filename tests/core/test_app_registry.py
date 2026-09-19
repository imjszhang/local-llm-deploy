"""AppSpec stays in the registry document and never becomes a proxy or model."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from local_llm_deploy.config import (
    ConfigError, ProjectPaths, dump_registry, load_apps, load_catalog, load_models,
    normalize_app, normalize_models, normalize_registry,
)
from tests.core.test_proxy_registry import proxy_cfg


def knowledge_cfg(**overrides):
    cfg = {
        "type": "app",
        "kind": "knowledge",
        "alias": "知识库",
        "upstream": "http://127.0.0.1:18789/plugins/js-knowledge",
    }
    cfg.update(overrides)
    return cfg


def video_cfg(**overrides):
    cfg = {
        "type": "app",
        "kind": "video",
        "alias": "视频库",
        "home": "~/.yt-study-archive",
        "legacy_prefixes": ["/archive"],
    }
    cfg.update(overrides)
    return cfg


class AppRegistryTests(unittest.TestCase):
    def test_normalize_models_skips_app_entries(self):
        specs = normalize_models({"chat": {"alias": "chat-alias"}, "knowledge": knowledge_cfg()})
        self.assertEqual(set(specs), {"chat"})

    def test_normalize_registry_keeps_apps_apart_from_proxies(self):
        models, proxies, apps = normalize_registry({
            "chat": {"alias": "chat-alias"},
            "comfyui": proxy_cfg(),
            "knowledge": knowledge_cfg(),
            "video": video_cfg(),
        })
        self.assertEqual(set(models), {"chat"})
        self.assertEqual(set(proxies), {"comfyui"})
        self.assertEqual(set(apps), {"knowledge", "video"})
        self.assertEqual(apps["knowledge"].prefix, "/knowledge")
        self.assertEqual(apps["knowledge"].endpoint, "/knowledge/")
        self.assertEqual(apps["video"].legacy_prefixes, ("/archive",))
        self.assertNotEqual(proxies["comfyui"].prefix, apps["video"].prefix)

    def test_kind_alias_and_prefix_conflicts_are_rejected(self):
        with self.assertRaises(ConfigError):
            normalize_registry({"chat": {"alias": "知识库"}, "knowledge": knowledge_cfg()})
        with self.assertRaises(ConfigError):
            normalize_registry({"a": knowledge_cfg(), "b": knowledge_cfg(alias="知识库二")})
        with self.assertRaises(ConfigError):
            normalize_registry({
                "knowledge": knowledge_cfg(),
                "video": video_cfg(prefix="/knowledge"),
            })
        with self.assertRaises(ConfigError):
            normalize_app("knowledge", knowledge_cfg(prefix="/services/leak"))
        with self.assertRaises(ConfigError):
            normalize_app("video", video_cfg(kind="other"))

    def test_dump_and_disk_round_trip_retain_apps(self):
        raw = {"chat": {"alias": "chat-alias"}, "knowledge": knowledge_cfg(), "comfyui": proxy_cfg()}
        dumped = dump_registry(raw)
        self.assertEqual(dumped["knowledge"]["type"], "app")
        self.assertEqual(dumped["comfyui"]["type"], "proxy")
        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary))
            paths.registry.write_text(json.dumps(raw), encoding="utf-8")
            models, proxies = load_catalog(paths.registry)
            apps = load_apps(paths.registry)
            self.assertEqual(set(models), {"chat"})
            self.assertEqual(set(proxies), {"comfyui"})
            self.assertEqual(set(apps), {"knowledge"})
            self.assertNotIn("knowledge", load_models(paths.registry))


if __name__ == "__main__":
    unittest.main()
