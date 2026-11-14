"""Public MCP discovery is live, explicit about availability, and secret-free."""
from __future__ import annotations

import copy
import http.client
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from local_llm_deploy.config import ProjectPaths
from local_llm_deploy.gateway.guide import build_guide, render_guide
from local_llm_deploy.gateway.mcp_guide import MCPGuide
from local_llm_deploy.mcp_hub.config import HubConfigStore, default_config


class MCPGuideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name))
        self.store = HubConfigStore(self.paths)
        config = default_config()
        config['services'] = [
            {'id': 'comfyui', 'name': 'ComfyUI', 'kind': 'comfyui', 'command': '/private/comfy-mcp',
             'url': 'http://10.9.8.7:8188', 'env': {'PRIVATE_KEY': 'env-secret'}, 'args': ['arg-secret']},
            {'id': 'disabled', 'name': 'Disabled', 'kind': 'stdio', 'enabled': False},
        ]
        self.config = self.store.save(config)
        self.runtime = {'status': 'ready', 'host': '0.0.0.0', 'port': 8899,
                        'pid': 987654, 'error': 'process-secret', 'endpoint': 'http://private.example:8899'}
        self.live = {'available': True, 'applied_revision': self.config['revision'], 'services': [
            {'id': 'comfyui', 'status': 'running', 'available': True, 'tool_count': 17,
             'remote': {'online': True, 'url': 'http://10.9.8.7:8188', 'error': 'remote-secret'},
             'bridge': {'command': '/private/comfy-mcp', 'error': 'bridge-secret'}},
            {'id': 'disabled', 'status': 'disabled', 'available': False},
        ], 'devices': [{'token': 'device-secret'}]}
        self.controller = SimpleNamespace(status=lambda: copy.deepcopy(self.runtime))
        self.guide = MCPGuide(self.paths, store=self.store, controller=self.controller,
                              status_reader=lambda runtime: copy.deepcopy(self.live))

    def test_available_services_publish_only_client_metadata(self):
        hub = self.guide.snapshot('hub.example:8888')
        self.assertTrue(hub['available'])
        self.assertFalse(hub['requires_restart'])
        self.assertEqual(hub['status'], 'ready')
        comfy, disabled = hub['services']
        self.assertEqual(comfy, {'id': 'comfyui', 'name': 'ComfyUI', 'kind': 'comfyui', 'enabled': True,
                                'available': True, 'status': 'running', 'url': 'http://hub.example:8899/mcp/comfyui',
                                'transport': 'streamable-http', 'tool_count': 17, 'remote_online': True})
        self.assertFalse(disabled['available'])
        self.assertEqual(disabled['status'], 'disabled')
        self.assertEqual(hub['aggregate']['url'], 'http://hub.example:8899/mcp/default')
        self.assertEqual(hub['authentication']['credential'], 'device_token')
        self.assertEqual([row['name'] for row in hub['permissions']], ['read', 'generate'])
        examples = hub['connection_examples']
        self.assertEqual([item['service_id'] for item in examples], ['comfyui', 'default'])
        for example in examples:
            self.assertTrue(example['available'])
            configs = {item['client']: item for item in example['configs']}
            self.assertEqual(set(configs), {'Claude Code', 'Cursor', 'Codex'})
            for client in ('Claude Code', 'Cursor'):
                server, = json.loads(configs[client]['content'])['mcpServers'].values()
                self.assertEqual(server['url'], example['url'])
                self.assertEqual(server['headers'], {'Authorization': 'Bearer <设备令牌>'})
                if client == 'Claude Code':
                    self.assertEqual(server['type'], 'http')
                else:
                    self.assertNotIn('type', server)
            self.assertIn(json.dumps(example['url']), configs['Codex']['content'])
            self.assertEqual(configs['Codex']['config_file'], '.codex/config.toml')
        page = render_guide(build_guide({}, {}, {}, {}, mcp_hub=hub)).decode()
        self.assertIn('MCP Hub 服务', page)
        self.assertIn('http://hub.example:8899/mcp/comfyui', page)
        self.assertIn('当前可用', page)
        self.assertIn('Authorization: Bearer &lt;设备令牌&gt;', page)
        self.assertIn('创建设备令牌', page)
        self.assertIn('默认勾选，只读接入请取消勾选', page)
        self.assertIn('原令牌只显示一次', page)
        self.assertIn('<details><summary>Claude Code', page)
        self.assertIn('&lt;设备令牌&gt;', page)
        self.assertNotIn('<设备令牌>', page)
        output = json.dumps(hub) + page
        for secret in ('env-secret', 'arg-secret', 'device-secret', 'process-secret', 'remote-secret',
                       'bridge-secret', '/private/comfy-mcp', 'private.example', '10.9.8.7', '987654'):
            self.assertNotIn(secret, output)

    def test_saved_configuration_is_reloaded_and_pending_changes_are_not_available(self):
        self.config['services'][0]['name'] = 'Renamed'
        self.store.save(self.config)
        hub = self.guide.snapshot('hub.example:8888')
        self.assertTrue(hub['requires_restart'])
        self.assertEqual(hub['services'][0]['name'], 'Renamed')
        self.assertFalse(hub['services'][0]['available'])
        self.assertEqual(hub['services'][0]['status'], 'pending_restart')
        self.assertEqual(hub['services'][1]['status'], 'disabled')
        self.assertTrue(all(not item['available'] for item in hub['connection_examples']))
        page = render_guide(build_guide({}, {}, {}, {}, mcp_hub=hub)).decode()
        self.assertIn('此入口当前未就绪', page)

    def test_recent_remote_failure_is_not_presented_as_available(self):
        self.live['services'][0]['remote']['online'] = False
        row = self.guide.snapshot('hub.example')['services'][0]
        self.assertFalse(row['available'])
        self.assertFalse(row['remote_online'])
        self.assertEqual(row['status'], 'upstream_unavailable')

    def test_unprobed_remote_does_not_claim_remote_health(self):
        self.live['services'][0]['remote'] = None
        hub = self.guide.snapshot('hub.example')
        self.assertTrue(hub['services'][0]['available'])
        self.assertIsNone(hub['services'][0]['remote_online'])
        self.assertIn('不代表生成模型或模板已就绪', hub['summary'])

    def test_stopped_hub_keeps_configuration_visible_but_not_available(self):
        self.runtime['status'] = 'stopped'
        self.live = None
        hub = self.guide.snapshot('hub.example')
        self.assertFalse(hub['available'])
        self.assertEqual(hub['status'], 'stopped')
        self.assertFalse(hub['services'][0]['available'])
        self.assertEqual(hub['services'][0]['status'], 'unavailable')

    def test_public_url_and_ipv6_host_generate_connectable_addresses(self):
        self.assertEqual(self.guide.snapshot('[2001:db8::1]:8888')['services'][0]['url'],
                         'http://[2001:db8::1]:8899/mcp/comfyui')
        self.config['listen']['public_url'] = 'https://mcp.example/hub/'
        self.store.save(self.config)
        hub = self.guide.snapshot('ignored.example')
        self.assertEqual(hub['services'][0]['url'], 'https://mcp.example/hub/mcp/comfyui')
        self.assertEqual(hub['scope'], 'public')
        self.assertEqual(hub['connection_examples'][0]['url'], 'https://mcp.example/hub/mcp/comfyui')

    def test_query_credential_is_never_published(self):
        self.config['listen']['public_url'] = 'https://mcp.example/?token=query-secret'
        self.store.save(self.config)
        hub = self.guide.snapshot('hub.example')
        self.assertEqual(hub['services'][0]['url'], 'http://hub.example:8899/mcp/comfyui')
        self.assertNotIn('query-secret', json.dumps(hub))

    def test_host_injection_is_not_used_as_connection_address(self):
        for host in ('user:pass@evil.example', 'hub.example/other', 'hub.example?token=secret',
                     'hub.example:bad', 'hub.example\\evil', '<script>alert(1)</script>', None):
            with self.subTest(host=host):
                hub = self.guide.snapshot(host)
                self.assertIsNone(hub['services'][0]['url'])
                self.assertEqual(hub['connection_examples'], [])

    def test_loopback_binding_is_described_as_local_only(self):
        for host, expected in (('127.0.0.1', '127.0.0.1'), ('::1', '[::1]')):
            with self.subTest(host=host):
                self.runtime['host'] = host
                hub = self.guide.snapshot('lan.example:8888')
                self.assertEqual(hub['services'][0]['url'], f'http://{expected}:8899/mcp/comfyui')
                self.assertEqual(hub['scope'], 'local')
                self.assertIn('仅供 Hub 主机本机使用', hub['summary'])

    def test_bad_config_and_failed_status_never_expose_raw_errors(self):
        self.controller.status = Mock(side_effect=OSError('private-error-secret'))
        hub = self.guide.snapshot('hub.example')
        self.assertFalse(hub['available'])
        self.assertNotIn('private-error-secret', json.dumps(hub))
        self.store.path.write_text('{"token":"config-secret",broken')
        hub = self.guide.snapshot('hub.example')
        self.assertEqual(hub['status'], 'unavailable')
        self.assertEqual(hub['services'], [])
        self.assertNotIn('config-secret', json.dumps(hub))

    def test_missing_or_malformed_runtime_services_are_unavailable(self):
        for services in (None, {}, 'private-error', [None, {}, {'id': []}]):
            with self.subTest(services=services):
                self.live['services'] = services
                hub = self.guide.snapshot('hub.example')
                self.assertFalse(hub['services'][0]['available'])
                self.assertIsNone(hub['services'][0]['tool_count'])

    def test_html_escapes_service_labels(self):
        self.config['services'][0]['name'] = '<img src=x onerror=alert(1)>'
        self.store.save(self.config)
        page = render_guide(build_guide({}, {}, {}, {}, mcp_hub=self.guide.snapshot('hub.example'))).decode()
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', page)
        self.assertNotIn('<img', page)

    def test_unconfigured_guide_does_not_create_runtime_files(self):
        with tempfile.TemporaryDirectory() as root:
            guide = MCPGuide(ProjectPaths(Path(root)))
            hub = guide.snapshot('hub.example')
            self.assertEqual(hub['status'], 'unconfigured')
            self.assertEqual(hub['connection_examples'], [])
            self.assertTrue(hub['token_setup']['steps'])
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_internal_status_reads_existing_key_over_loopback_only(self):
        key_path = self.store.path.parent / 'internal.key'
        key_path.write_text('internal-secret\n')
        connection = Mock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.return_value = json.dumps(self.live).encode()
        guide = MCPGuide(self.paths, controller=self.controller)
        with patch('local_llm_deploy.gateway.mcp_guide.http.client.HTTPConnection', return_value=connection) as factory:
            hub = guide.snapshot('attacker.example:12345')
        factory.assert_called_once_with('127.0.0.1', 8899, timeout=1)
        connection.request.assert_called_once_with('GET', '/internal/status', headers={
            'X-Hub-Internal-Key': 'internal-secret', 'Accept': 'application/json'})
        connection.close.assert_called_once()
        self.assertTrue(hub['available'])
        self.assertNotIn('internal-secret', json.dumps(hub))

    def test_internal_errors_and_redirects_fail_closed(self):
        (self.store.path.parent / 'internal.key').write_text('internal-secret')
        guide = MCPGuide(self.paths, controller=self.controller)
        for response_status, data in ((302, b'{}'), (500, b'error-secret'), (200, b'not-json'),
                                      (200, b'[]'), (200, b'a' * (1024 * 1024 + 1))):
            with self.subTest(status=response_status, size=len(data)):
                connection = Mock()
                connection.getresponse.return_value.status = response_status
                connection.getresponse.return_value.read.return_value = data
                with patch('local_llm_deploy.gateway.mcp_guide.http.client.HTTPConnection', return_value=connection):
                    hub = guide.snapshot('hub.example')
                self.assertFalse(hub['available'])
                connection.request.assert_called_once()
                connection.close.assert_called_once()
        guide.status_reader = Mock(side_effect=http.client.HTTPException('error-secret'))
        self.assertNotIn('error-secret', json.dumps(guide.snapshot('hub.example')))

    def test_missing_internal_key_never_creates_one(self):
        guide = MCPGuide(self.paths, controller=self.controller)
        hub = guide.snapshot('hub.example')
        self.assertFalse(hub['available'])
        self.assertFalse((self.store.path.parent / 'internal.key').exists())
        self.assertFalse((self.store.path.parent / 'state.sqlite3').exists())
