"""Self-registered HTTP apps mount a prefix without editing models.json."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from local_llm_deploy.config import ProjectPaths, normalize_app, normalize_models
from local_llm_deploy.gateway.app import GatewayContext, create_server
from local_llm_deploy.gateway.app_registry import load_mounted_apps, registration_path
from local_llm_deploy.gateway.discovery import Backend
from local_llm_deploy.gateway.settings import GatewaySettings


class Upstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        payload = json.dumps({'path': self.path, 'authorization': self.headers.get('Authorization')}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class AppRegistrationGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'static').mkdir()
        (self.root / '.api-key').write_text('service-key\n')
        self.backend = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        threading.Thread(target=self.backend.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.backend.server_port}'
        specs = normalize_models({'chat': {'alias': 'friendly-chat'}})

        class FakeDiscovery:
            def models(self):
                return {'chat': Backend('chat', 'friendly-chat', 'http://127.0.0.1:9', ('chat',), 'llama_cpp')}

            def ollama_status(self):
                return {'status': 'offline'}

        self.context = GatewayContext(
            ProjectPaths(self.root), settings=GatewaySettings(api_timeout=1, client_write_timeout=1),
            specs=specs, discovery=FakeDiscovery(), apps={},
        )
        self.context._app_loader = load_mounted_apps
        self.gateway = create_server(self.context, port=0)
        threading.Thread(target=self.gateway.serve_forever, daemon=True).start()

    def tearDown(self):
        self.gateway.shutdown()
        self.gateway.server_close()
        self.backend.shutdown()
        self.backend.server_close()
        self.tmp.cleanup()

    def request(self, path, body=None, headers=None, method=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.gateway.server_port, timeout=2)
        headers = {'Authorization': 'Bearer service-key', **(headers or {})}
        if isinstance(body, dict):
            body = json.dumps(body)
            headers = {'Content-Type': 'application/json', **headers}
        connection.request(method or ('POST' if body is not None else 'GET'), path, body, headers)
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def test_register_mounts_prefix_and_delete_removes_it(self):
        status, payload = self.request('/gateway-api/v1/apps/notes', {
            'alias': '笔记', 'prefix': '/notes', 'upstream': self.url,
        }, method='PUT')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)['endpoint'], '/notes/')
        self.assertTrue(registration_path(self.root / 'models.json').is_file())
        status, payload = self.request('/notes/hello', headers={'Authorization': 'Bearer app-token'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)['path'], '/hello')
        self.assertEqual(json.loads(payload)['authorization'], 'Bearer app-token')
        status, _ = self.request('/gateway-api/v1/apps/notes', method='DELETE')
        self.assertEqual(status, 200)
        self.assertEqual(self.request('/notes/hello')[0], 404)
        self.assertNotIn('notes', self.context.apps)
        self.assertIn('monitor', {spec.kind for spec in self.context.apps.values()})

    def test_root_lists_mounted_app_without_upstream(self):
        self.context.apps = {'notes': normalize_app('notes', {
            'type': 'app', 'kind': 'http', 'alias': '笔记', 'prefix': '/notes',
            'upstream': 'http://192.168.0.20:9/secret',
        })}
        status, payload = self.request('/')
        self.assertEqual(status, 200)
        page = payload.decode()
        self.assertIn('笔记', page)
        self.assertIn('href="/notes/"', page)
        self.assertNotIn('href="/monitor.html"', page)
        self.assertNotIn('192.168.0.20', page)
        self.assertNotIn('secret', page)

    def test_registration_requires_api_key_and_rejects_reserved_prefix(self):
        self.assertEqual(self.request('/gateway-api/v1/apps', headers={'Authorization': ''})[0], 401)
        status, payload = self.request('/gateway-api/v1/apps/leak', {
            'alias': '泄漏', 'prefix': '/v1', 'upstream': self.url,
        }, method='PUT')
        self.assertEqual(status, 400)
        self.assertIn('prefix', payload.decode())


if __name__ == '__main__':
    unittest.main()
