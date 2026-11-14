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
        "alias": "Service Tool",
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
        specs = normalize_models({"chat": {"alias": "chat-alias"}, "tool": proxy_cfg()})
        self.assertEqual(set(specs), {"chat"})
        self.assertNotIn("tool", specs)

    def test_normalize_registry_keeps_proxy_and_rejects_alias_clash(self):
        models, proxies, apps = normalize_registry({"chat": {"alias": "chat-alias"}, "tool": proxy_cfg()})
        self.assertEqual(set(models), {"chat"})
        self.assertEqual(set(proxies), {"tool"})
        self.assertEqual(apps, {})
        self.assertEqual(proxies["tool"].prefix, "/services/tool")
        bearer = proxy_cfg(auth={"gateway": "api_key", "upstream": "bearer", "bearer": "upstream-token", "console": False})
        self.assertEqual(normalize_proxy("tool", bearer).auth_upstream, "bearer")
        self.assertEqual(normalize_proxy("tool", bearer).raw["auth"]["bearer"], "upstream-token")
        self.assertEqual(proxies["tool"].host, "192.168.0.20")
        self.assertEqual(proxies["tool"].port, 8188)
        with self.assertRaises(ConfigError):
            normalize_registry({"chat": {"alias": "Service Tool"}, "tool": proxy_cfg()})
        with self.assertRaises(ConfigError):
            normalize_registry({"tool": {"alias": "same"}, "other": proxy_cfg(alias="same")})

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
            {"auth": {"upstream": "bearer"}},
            {"auth": {"upstream": "none", "bearer": "upstream-token"}},
            {"websocket": "true"},
            {"browser": 1},
            {"health_check": "false"},
            {"timeout": 0},
            {"max_body_bytes": True},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ConfigError):
                    normalize_proxy("tool", proxy_cfg(**bad))

    def test_dump_and_disk_round_trip_retain_proxy(self):
        raw = {"chat": {"alias": "chat-alias"}, "tool": proxy_cfg(), "_comment": "ignored"}
        dumped = dump_registry(raw)
        self.assertIn("tool", dumped)
        self.assertEqual(dumped["tool"]["type"], "proxy")
        self.assertNotIn("_comment", dumped)
        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary))
            atomic_json(paths.registry, dumped)
            self.assertEqual(set(load_specs(paths.registry)), {"chat"})
            self.assertEqual(set(load_models(paths.registry)), {"chat"})
            self.assertEqual(set(load_proxies(paths.registry)), {"tool"})
            models, proxies = load_catalog(paths.registry)
            self.assertEqual(set(models), {"chat"})
            self.assertEqual(proxies["tool"].upstream, "http://192.168.0.20:8188")
            registry = Registry(paths)
            registry.update(lambda data: data.update({"extra": proxy_cfg(alias="Other", upstream="http://127.0.0.1:9")}))
            saved = json.loads(paths.registry.read_text(encoding="utf-8"))
            self.assertEqual(set(saved), {"chat", "tool", "extra"})
            self.assertEqual(set(registry.specs()), {"chat"})
            self.assertEqual(set(registry.proxies()), {"tool", "extra"})


if __name__ == "__main__":
    unittest.main()
