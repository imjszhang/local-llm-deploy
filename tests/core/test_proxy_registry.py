"""ProxySpec stays in the registry document and never becomes a chat model."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from local_llm_deploy.config import (
    ConfigError, ProjectPaths, dump_registry, load_catalog, load_models, load_proxies, load_specs,
    normalize_models, normalize_proxy, normalize_registry,
)
from local_llm_deploy.registry import Registry
from local_llm_deploy.storage import atomic_json


def proxy_cfg(**overrides):
    cfg = {
        "type": "proxy",
        "alias": "ComfyUI",
        "upstream": "http://192.168.0.20:8188",
        "health_path": "/system_stats",
        "timeout": 120,
        "strip_prefix": True,
        "methods": ["GET", "HEAD", "POST", "PUT", "DELETE"],
        "paths": ["/prompt", "/queue", "/history", "/view", "/upload/image", "/object_info", "/system_stats"],
        "auth": {"gateway": "api_key", "upstream": "none", "console": False},
        "websocket": False,
    }
    cfg.update(overrides)
    return cfg


class ProxyRegistryTests(unittest.TestCase):
    def test_normalize_models_skips_proxy_entries(self):
        specs = normalize_models({"chat": {"alias": "chat-alias"}, "comfyui": proxy_cfg()})
        self.assertEqual(set(specs), {"chat"})
        self.assertNotIn("comfyui", specs)

    def test_normalize_registry_keeps_proxy_and_rejects_alias_clash(self):
        models, proxies = normalize_registry({"chat": {"alias": "chat-alias"}, "comfyui": proxy_cfg()})
        self.assertEqual(set(models), {"chat"})
        self.assertEqual(set(proxies), {"comfyui"})
        self.assertEqual(proxies["comfyui"].prefix, "/services/comfyui")
        self.assertEqual(proxies["comfyui"].host, "192.168.0.20")
        self.assertEqual(proxies["comfyui"].port, 8188)
        with self.assertRaises(ConfigError):
            normalize_registry({"chat": {"alias": "ComfyUI"}, "comfyui": proxy_cfg()})
        with self.assertRaises(ConfigError):
            normalize_registry({"comfyui": {"alias": "same"}, "other": proxy_cfg(alias="same")})

    def test_invalid_proxy_shapes_are_rejected(self):
        for bad in (
            {"upstream": "ftp://192.168.0.20:8188"},
            {"upstream": "/local"},
            {"upstream": "http://user:pass@192.168.0.20:8188"},
            {"upstream": "http://192.168.0.20:8188/../escape"},
            {"health_path": "system_stats"},
            {"health_path": "/../stats"},
            {"paths": ["/history/../secret"]},
            {"paths": ["//evil"]},
            {"methods": ["TRACE"]},
            {"auth": {"gateway": "none"}},
            {"auth": {"upstream": "basic"}},
            {"websocket": True},
            {"timeout": 0},
            {"max_body_bytes": True},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ConfigError):
                    normalize_proxy("comfyui", proxy_cfg(**bad))

    def test_dump_and_disk_round_trip_retain_proxy(self):
        raw = {"chat": {"alias": "chat-alias"}, "comfyui": proxy_cfg(), "_comment": "ignored"}
        dumped = dump_registry(raw)
        self.assertIn("comfyui", dumped)
        self.assertEqual(dumped["comfyui"]["type"], "proxy")
        self.assertNotIn("_comment", dumped)
        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary))
            atomic_json(paths.registry, dumped)
            self.assertEqual(set(load_specs(paths.registry)), {"chat"})
            self.assertEqual(set(load_models(paths.registry)), {"chat"})
            self.assertEqual(set(load_proxies(paths.registry)), {"comfyui"})
            models, proxies = load_catalog(paths.registry)
            self.assertEqual(set(models), {"chat"})
            self.assertEqual(proxies["comfyui"].upstream, "http://192.168.0.20:8188")
            registry = Registry(paths)
            registry.update(lambda data: data.update({"extra": proxy_cfg(alias="Other", upstream="http://127.0.0.1:9")}))
            saved = json.loads(paths.registry.read_text(encoding="utf-8"))
            self.assertEqual(set(saved), {"chat", "comfyui", "extra"})
            self.assertEqual(set(registry.specs()), {"chat"})
            self.assertEqual(set(registry.proxies()), {"comfyui", "extra"})


if __name__ == "__main__":
    unittest.main()
