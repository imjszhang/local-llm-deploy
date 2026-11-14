"""The 8888 control plane remains usable with the independent Hub offline."""
from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from local_llm_deploy.config import ProjectPaths
from local_llm_deploy.gateway.app import GatewayContext, create_server
from local_llm_deploy.gateway.mcp_admin import MAX_BODY_BYTES, MCPAdminAPI
from local_llm_deploy.mcp_hub.config import HubConfigStore
from local_llm_deploy.mcp_hub.state import HubState


class Controller:
    def __init__(self):
        self.value = {'key': 'mcp-hub', 'status': 'stopped', 'pid': None,
                      'host': '0.0.0.0', 'port': 8899, 'healthy': False}
        self.entered = threading.Event()
        self.release = threading.Event()
        self.log_text = ''

    def status(self):
        return dict(self.value)

    def start(self):
        self.entered.set()
        self.release.wait(3)
        self.value.update(status='ready', healthy=True, pid=123)
        return self.status()

    def stop(self):
        self.value.update(status='stopped', healthy=False, pid=None)
        return self.status()

    def restart(self):
        raise RuntimeError('Bearer root-fixture-key password=hidden-fixture')

    def logs(self, limit=200):
        return self.log_text


class MCPAdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.tmp.name))
        self.paths.api_key.write_text('root-fixture-key\n')
        self.paths.static.mkdir()
        (self.paths.static / 'monitor.html').write_text('<html>MCP page fixture</html>')
        self.context = GatewayContext(
            self.paths, specs={}, discovery=SimpleNamespace(models=lambda: {}, ollama_status=lambda: {}),
            monitor_api=SimpleNamespace(close=lambda: None), agent_monitor=SimpleNamespace(start=lambda: None))
        self.store = HubConfigStore(self.paths)
        self.state = HubState(self.paths)
        self.controller = Controller()
        self.api = MCPAdminAPI(self.context, store=self.store, controller=self.controller, state=self.state)
        self.context.mcp_admin = self.api
        self.server = create_server(self.context, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.controller.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.tmp.cleanup()

    def request(self, suffix, method='GET', body=None, *, authorized=True, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=4)
        request_headers = {'Authorization': 'Bearer root-fixture-key'} if authorized else {}
        if isinstance(body, dict):
            body = json.dumps(body)
            request_headers['Content-Type'] = 'application/json'
        request_headers.update(headers or {})
        target = suffix if suffix.startswith('/mcp-hub') or suffix == '/' else '/mcp-admin-api/v1' + suffix
        connection.request(method, target, body, request_headers)
        response = connection.getresponse()
        data = response.read()
        result = response.status, dict(response.getheaders()), data
        connection.close()
        return result

    def payload(self, suffix, method='GET', body=None, **kwargs):
        status, headers, data = self.request(suffix, method, body, **kwargs)
        return status, json.loads(data), headers

    def test_page_and_offline_snapshot_work_without_starting_hub(self):
        status, headers, page = self.request('/mcp-hub/', authorized=False)
        self.assertEqual(status, 200)
        self.assertIn(b'MCP page fixture', page)
        self.assertEqual(headers['Cache-Control'], 'no-cache')
        self.assertEqual(self.request('/mcp-hub', authorized=False)[:1], (308,))
        self.assertIn(b'href="/mcp-hub/"', self.request('/', authorized=False)[2])
        status, data, headers = self.payload('/snapshot')
        self.assertEqual(status, 200)
        self.assertEqual(set(data), {'config', 'runtime', 'devices', 'tasks', 'connections'})
        self.assertEqual(data['runtime']['status'], 'stopped')
        self.assertFalse(data['runtime']['available'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertFalse(self.controller.entered.is_set())

    def test_real_root_key_and_same_origin_required(self):
        self.assertEqual(self.request('/snapshot', authorized=False)[0], 401)
        token = self.context.console_sessions.issue(self.origin)['token']
        self.assertEqual(self.request('/snapshot', headers={'Authorization': 'Bearer ' + token})[0], 401)
        self.assertEqual(self.request('/snapshot', headers={'Origin': 'https://foreign.example'})[0], 403)
        self.assertEqual(self.request('/snapshot', headers={'Origin': 'null'})[0], 403)
        self.assertEqual(self.request('/snapshot', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.request('/snapshot', headers={'Origin': self.origin})[0], 200)
        self.paths.api_key.unlink()
        self.assertEqual(self.request('/snapshot', authorized=False)[0], 401)
        self.assertEqual(self.request('/snapshot')[0], 401)

    def test_config_revision_save_and_connections_when_offline(self):
        config = self.store.load()
        config['listen']['public_url'] = 'http://hub.example:8899'
        config['services'] = [{'id': 'comfyui', 'kind': 'comfyui', 'command': 'comfy-mcp', 'url': 'http://127.0.0.1:8188',
                               'env': {'COMFY_TOKEN': 'secret-in-config'}}]
        status, payload, _ = self.payload('/config', 'PUT', {'config': config, 'expected_revision': 0})
        self.assertEqual(status, 200)
        self.assertEqual(payload['config']['revision'], 1)
        self.assertEqual(self.payload('/config', 'PUT', {'config': config, 'expected_revision': 0})[0], 409)
        snapshot = self.payload('/snapshot')[1]
        self.assertEqual(snapshot['connections'], [{'service_id': 'comfyui',
                         'url': 'http://hub.example:8899/mcp/comfyui', 'transport': 'streamable-http'}])
        self.assertEqual(snapshot['config']['services'][0]['env']['COMFY_TOKEN'], 'secret-in-config')

    def test_device_token_only_appears_once_and_revocation_is_effective(self):
        status, created, _ = self.payload('/devices', 'POST', {
            'name': 'Laptop', 'services': ['comfyui'], 'permissions': ['read', 'generate']})
        self.assertEqual(status, 201)
        self.assertIsNotNone(self.state.authenticate(created['token'], 'comfyui'))
        snapshot = self.payload('/snapshot')[1]
        self.assertNotIn(created['token'], json.dumps(snapshot))
        self.assertNotIn('digest', json.dumps(snapshot))
        ident = created['device']['id']
        self.assertEqual(self.payload('/devices/' + ident, 'DELETE')[0], 200)
        self.assertIsNone(self.state.authenticate(created['token'], 'comfyui'))
        self.assertEqual(self.payload('/devices', 'POST', {
            'name': 'bad', 'services': ['comfyui'], 'permissions': ['admin']})[0], 400)

    def test_actions_are_background_serialized_and_pollable(self):
        status, payload, _ = self.payload('/actions', 'POST', {'action': 'start'})
        self.assertEqual(status, 202)
        ident = payload['operation']['id']
        self.assertEqual(payload['operation']['status'], 'running')
        self.assertTrue(self.controller.entered.wait(1))
        self.assertEqual(self.payload('/actions', 'POST', {'action': 'stop'})[0], 409)
        self.controller.release.set()
        operation = self.wait_operation(ident)
        self.assertEqual(operation['status'], 'succeeded')
        self.assertEqual(operation['result']['status'], 'ready')
        failed = self.payload('/actions', 'POST', {'action': 'restart'})[1]['operation']['id']
        operation = self.wait_operation(failed)
        self.assertEqual(operation['status'], 'failed')
        self.assertNotIn('root-fixture-key', operation['error'])
        self.assertNotIn('hidden-fixture', operation['error'])

    def wait_operation(self, ident):
        for _ in range(100):
            operation = self.payload('/operations/' + ident)[1]['operation']
            if operation['status'] != 'running':
                return operation
            time.sleep(.01)
        self.fail('Management operation did not finish')

    def test_internal_api_uses_only_its_private_key_and_does_not_follow_redirects(self):
        calls = []
        internal_key = self.state.internal_key()
        class Runtime(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append((self.path, dict(self.headers)))
                if self.path == '/internal/services/redirect/tools':
                    self.send_response(302)
                    self.send_header('Location', 'http://127.0.0.1:1/private')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if self.path.endswith('/tools'):
                    payload = {'tools': [{'name': 'server_info'}]}
                else:
                    payload = {'services': [{'id': 'comfyui', 'status': 'ready'}], 'tool_count': 1, 'applied_revision': 4}
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *_args):
                pass
        runtime = ThreadingHTTPServer(('127.0.0.1', 0), Runtime)
        thread = threading.Thread(target=runtime.serve_forever, daemon=True)
        thread.start()
        try:
            self.controller.value.update(status='ready', port=runtime.server_port)
            snapshot = self.payload('/snapshot')[1]
            self.assertTrue(snapshot['runtime']['available'])
            self.assertEqual(snapshot['runtime']['tool_count'], 1)
            self.assertEqual(snapshot['runtime']['applied_revision'], 4)
            self.assertEqual(self.payload('/services/comfyui/tools')[1]['tools'][0]['name'], 'server_info')
            self.assertEqual(self.payload('/services/redirect/tools')[0], 503)
            self.assertTrue(calls)
            for _target, headers in calls:
                self.assertEqual(headers['X-Hub-Internal-Key'], internal_key)
                self.assertNotIn('Authorization', headers)
                self.assertNotIn('root-fixture-key', str(headers))
        finally:
            runtime.shutdown()
            runtime.server_close()
            thread.join(3)
        self.controller.value.update(status='stopped')
        self.assertEqual(self.payload('/services/comfyui/tools')[0], 503)
        self.assertEqual(self.payload('/services/comfyui/probe', 'POST', {})[0], 503)

    def test_logs_and_bad_requests_do_not_expose_secrets(self):
        self.controller.log_text = 'Bearer root-fixture-key password=secret-fixture raw mcp_hub_deviceSecret'
        status, payload, _ = self.payload('/logs')
        self.assertEqual(status, 200)
        for secret in ('root-fixture-key', 'secret-fixture', 'mcp_hub_deviceSecret'):
            self.assertNotIn(secret, payload['logs'])
        self.assertEqual(self.request('/actions', 'POST', '{}')[0], 415)
        self.assertEqual(self.request('/actions', 'POST', '{bad', headers={'Content-Type': 'application/json'})[0], 400)
        self.assertEqual(self.request('/actions', 'POST', 'x' * (MAX_BODY_BYTES + 1),
                                      headers={'Content-Type': 'application/json'})[0], 413)
        self.assertEqual(self.request('/snapshot', body='x')[0], 400)
        self.assertEqual(self.payload('/config', 'PUT', {'config': {}, 'expected_revision': True})[0], 400)
        self.assertEqual(self.payload('/services/%2e%2e/tools')[0], 404)

    def test_argv_repr_and_arbitrary_environment_values_are_redacted_in_operation_errors(self):
        config = self.store.load()
        config['services'] = [{'id': 'fixture', 'kind': 'stdio', 'command': 'fixture-server',
                               'args': ['--token', 'opaque-argv-fixture'], 'env': {'SESSION': 'opaque-env-fixture'}}]
        self.store.save(config, expected_revision=0)
        def fail():
            raise RuntimeError("argv=['--token', 'opaque-argv-fixture'] SESSION='opaque-env-fixture'")
        self.controller.restart = fail
        operation_id = self.payload('/actions', 'POST', {'action': 'restart'})[1]['operation']['id']
        error = self.wait_operation(operation_id)['error']
        self.assertNotIn('opaque-argv-fixture', error)
        self.assertNotIn('opaque-env-fixture', error)
        self.controller.log_text = "['--token', 'opaque-argv-fixture'] SESSION='opaque-env-fixture'"
        logs = self.payload('/logs')[1]['logs']
        self.assertNotIn('opaque-argv-fixture', logs)
        self.assertNotIn('opaque-env-fixture', logs)


if __name__ == '__main__':
    unittest.main()
