"""Discovery uses supplied observations and never modifies runtime records."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from local_llm_deploy.config import ProjectPaths, normalize_models
from local_llm_deploy.gateway.discovery import Discovery
from local_llm_deploy.gateway.settings import GatewaySettings


class DiscoveryTests(unittest.TestCase):
    def test_pid_observation_only_includes_registered_running_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = ProjectPaths(Path(tmp))
            paths.run.mkdir()
            marker = paths.run / 'stale.pid'
            marker.write_text('999999\n8001\nstale\n')
            specs = normalize_models({'chat': {'alias': 'a'}, 'stale': {}})
            observations = {'chat': SimpleNamespace(status='running', pid=123, port=8001),
                            'stale': SimpleNamespace(status='stale', pid=None, port=8002),
                            'serve-ui': SimpleNamespace(status='ready', pid=234, port=8888)}
            discovery = Discovery(paths, specs, GatewaySettings(), observe=lambda *a: observations,
                                  health=lambda *a, **kw: True, fetch_json=lambda url: (_ for _ in ()).throw(OSError('offline')))
            models = discovery.models()
            self.assertEqual(set(models), {'chat'})
            self.assertEqual(models['chat'].alias, 'a')
            self.assertTrue(marker.exists())

    def test_external_and_ollama_tags_have_distinct_capabilities(self):
        specs = normalize_models({
            'remote-embed': {'type': 'embedding', 'external_backend': True, 'default_port': 9000},
            'chat': {'type': 'ollama', 'ollama_model': 'tag:latest', 'alias': 'friendly'},
        })
        def fetch(url):
            if url.endswith('/version'):
                return {'version': 'test-version'}
            return {'models': [{'name': 'tag:latest'}, {'name': 'another:latest'}]}
        discovery = Discovery(ProjectPaths(Path('/unused-test-root')), specs, GatewaySettings(),
                              observe=lambda *a: {}, probe=lambda host, port: True, health=lambda *a, **kw: True, fetch_json=fetch)
        models = discovery.models()
        self.assertEqual(models['remote-embed'].capabilities, ('embedding',))
        self.assertEqual(models['chat'].backend_model, 'tag:latest')
        self.assertEqual(models['another-latest'].backend_model, 'another:latest')
        self.assertEqual(discovery.ollama_status()['version'], 'test-version')

    def test_cache_refresh_and_auto_discovery_disabled(self):
        now = [0]
        calls = []
        specs = normalize_models({})
        def fetch(url):
            calls.append(url)
            return {'version': 'v', 'models': [{'name': 'tag:latest'}]}
        discovery = Discovery(ProjectPaths(Path('/unused-test-root')), specs,
                              GatewaySettings(ollama_auto_discover=False),
                              observe=lambda *a: {}, fetch_json=fetch, clock=lambda: now[0])
        self.assertEqual(discovery.models(), {})
        self.assertEqual(len(calls), 3)
        discovery.models()
        self.assertEqual(len(calls), 3)
        now[0] = 6
        discovery.models()
        self.assertEqual(len(calls), 6)


    def test_alive_but_unhealthy_managed_backend_is_not_routed(self):
        specs = normalize_models({'chat': {}})
        observations = {'chat': SimpleNamespace(status='ready', pid=123, port=8001, host='0.0.0.0')}
        checked = []
        def health(host, port, path, **kwargs):
            checked.append((host, port, path))
            return False
        discovery = Discovery(ProjectPaths(Path('/unused-test-root')), specs, GatewaySettings(),
                              observe=lambda *a: observations, health=health,
                              fetch_json=lambda url: (_ for _ in ()).throw(OSError('offline')))
        self.assertEqual(discovery.models(), {})
        self.assertEqual(checked, [('127.0.0.1', 8001, '/health')])

    def test_observed_host_and_health_credentials_are_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = ProjectPaths(Path(tmp))
            paths.api_key.write_text('fake-health-key')
            specs = normalize_models({'chat': {'host': '127.0.0.2'}})
            observations = {'chat': SimpleNamespace(status='running', pid=123, port=8123, host='127.0.0.1')}
            checked = []
            def health(host, port, path, **kwargs):
                checked.append((host, port, kwargs['headers'].get('Authorization')))
                return True
            discovery = Discovery(paths, specs, GatewaySettings(), observe=lambda *a: observations,
                                  health=health, fetch_json=lambda url: (_ for _ in ()).throw(OSError('offline')))
            self.assertEqual(discovery.models()['chat'].url, 'http://127.0.0.1:8123')
            self.assertEqual(checked, [('127.0.0.1', 8123, 'Bearer fake-health-key')])

    def test_registry_refresh_preserves_old_snapshot_on_invalid_update(self):
        from local_llm_deploy.gateway.app import GatewayContext
        class FakeDiscovery:
            def update_specs(self, specs):
                self.specs = specs
        now = [0]
        candidate = [normalize_models({'chat': {'params': {'max_concurrent': 1}}})]
        def loader(path):
            if isinstance(candidate[0], Exception):
                raise candidate[0]
            return candidate[0]
        with tempfile.TemporaryDirectory() as tmp:
            context = GatewayContext(ProjectPaths(Path(tmp)), specs=candidate[0],
                                     discovery=FakeDiscovery(), spec_loader=loader, clock=lambda: now[0])
            old_router = context.router
            candidate[0] = normalize_models({'chat': {'alias': 'renamed', 'params': {'max_concurrent': 5}},
                                            'new': {'type': 'embedding'}})
            now[0] = 31
            context.refresh()
            self.assertEqual(context.specs['chat'].alias, 'renamed')
            self.assertEqual(context.scheduler.models['chat']['max_slots'], 1)
            self.assertIn('new', context.scheduler.models)
            self.assertEqual(old_router.specs['chat'].alias, 'chat')
            candidate[0] = ValueError('invalid update')
            now[0] = 62
            context.refresh()
            self.assertEqual(context.specs['chat'].alias, 'renamed')

    def test_backend_credentials_use_observed_then_configured_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = ProjectPaths(Path(tmp))
            paths.api_key.write_text('gateway-fixture')
            (paths.root / 'config.key').write_text('config-fixture')
            (paths.root / 'observed.key').write_text('observed-fixture')
            specs = normalize_models({'chat': {'runtime': {'api_key_file': 'config.key'}}})
            item = SimpleNamespace(status='ready', pid=123, port=8001, host='127.0.0.1')
            checked = []
            def health(*args, **kwargs):
                checked.append(kwargs['headers']['Authorization'])
                return True
            discovery = Discovery(paths, specs, GatewaySettings(discovery_ttl=0), observe=lambda *args: {'chat': item},
                                  health=health, fetch_json=lambda url: (_ for _ in ()).throw(OSError('offline')))
            self.assertIn('chat', discovery.models())
            self.assertEqual(checked[-1], 'Bearer config-fixture')
            item.api_key_file, item.auth_source = 'observed.key', 'file'
            self.assertIn('chat', discovery.models())
            self.assertEqual(checked[-1], 'Bearer observed-fixture')
            item.auth_source = 'inline'
            self.assertEqual(discovery.models(), {})
            self.assertIn('API_KEY_FILE', discovery.unavailable['chat'])
            self.assertNotIn('fixture', discovery.unavailable['chat'])
            item.auth_source = 'file'
            (paths.root / 'observed.key').unlink()
            self.assertEqual(discovery.models(), {})
            self.assertIn('unavailable', discovery.unavailable['chat'])
            self.assertEqual(len(checked), 2)

    def test_malformed_ollama_metadata_is_treated_as_unavailable(self):
        discovery = Discovery(ProjectPaths(Path('/unused-test-root')), {}, GatewaySettings(),
                              observe=lambda *a: {}, fetch_json=lambda url: [])
        self.assertEqual(discovery.models(), {})
        self.assertEqual(discovery.ollama_status()['status'], 'offline')

    def test_ollama_discovery_and_dynamic_models_use_backend_file(self):
        import io
        import json
        with tempfile.TemporaryDirectory() as tmp:
            paths = ProjectPaths(Path(tmp))
            paths.api_key.write_text('gateway-fixture')
            (paths.root / 'ollama.key').write_text('ollama-fixture')
            specs = normalize_models({'ollama-chat': {'type': 'ollama', 'ollama_model': 'tag:latest',
                                                     'runtime': {'api_key_file': 'ollama.key'}}})
            checked = []
            def response(request, **kwargs):
                checked.append(request.get_header('Authorization'))
                payload = {'version': 'fixture'} if request.full_url.endswith('/version') else {
                    'models': [{'name': 'tag:latest'}, {'name': 'dynamic:latest'}]}
                return io.BytesIO(json.dumps(payload).encode())
            discovery = Discovery(paths, specs, GatewaySettings(), observe=lambda *args: {})
            with patch('local_llm_deploy.gateway.discovery.urlopen', side_effect=response):
                models = discovery.models()
            self.assertEqual(checked, ['Bearer ollama-fixture'] * 3)
            self.assertEqual(models['ollama-chat'].credentials.key(), 'ollama-fixture')
            self.assertEqual(models['dynamic-latest'].credentials.key(), 'ollama-fixture')

    def test_settings_validate_limits_and_preserve_legacy_env(self):
        settings = GatewaySettings.from_env({'MAX_GLOBAL_CONCURRENT': '3', 'DEFAULT_CHAT_MODEL': 'chat'})
        self.assertEqual(settings.chat_concurrent, 3)
        self.assertEqual(settings.defaults, {'chat': 'chat'})
        settings = GatewaySettings.from_env({'MAX_GLOBAL_CONCURRENT': '3', 'CHAT_LANE_CONCURRENT': '2'})
        self.assertEqual(settings.chat_concurrent, 2)
        with self.assertRaises(ValueError):
            GatewaySettings(stream_buffer_chunks=0)


if __name__ == '__main__':
    unittest.main()
