"""Generic /services/<key>/ forwarding stays off the chat lane."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from local_llm_deploy.config import ProjectPaths, normalize_models, normalize_proxy
from local_llm_deploy.gateway.app import GatewayContext, create_server
from local_llm_deploy.gateway.discovery import Backend
from local_llm_deploy.gateway.proxies import path_allowed, parse_service_request
from local_llm_deploy.gateway.settings import GatewaySettings


class FakeState:
    def __init__(self):
        self.requests = []
        self.release = threading.Event()


class Upstream(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.state.requests.append((self.command, self.path, body, dict(self.headers)))
        if self.path.startswith('/hold'):
            self.server.state.release.wait(3)
        payload = json.dumps({'path': self.path, 'authorization': self.headers.get('Authorization')}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


class ServiceProxyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'static').mkdir()
        (self.root / '.api-key').write_text('service-key\n')
        self.state = FakeState()
        self.backend = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.backend.state = self.state
        threading.Thread(target=self.backend.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.backend.server_port}'
        self.specs = normalize_models({'chat': {'alias': 'friendly-chat'}})
        self.proxy = normalize_proxy('tool', {
            'type': 'proxy', 'alias': 'Tool', 'upstream': self.url,
            'health_path': '/system_stats', 'timeout': 0.2, 'max_body_bytes': 64,
            'paths': ['/prompt', '/system_stats', '/view', '/history', '/hold'],
            'auth': {'gateway': 'api_key', 'upstream': 'none', 'console': False},
            'websocket': False,
        })
        class FakeDiscovery:
            def models(self):
                return {'chat': Backend('chat', 'friendly-chat', self_url, ('chat',), 'llama_cpp')}
            def ollama_status(self):
                return {'status': 'offline'}
        self_url = self.url
        settings = GatewaySettings(api_timeout=1, client_write_timeout=1, max_body_bytes=4096,
                                   knowledge_url=self.url + '/knowledge-backend')
        self.context = GatewayContext(ProjectPaths(self.root), settings=settings, specs=self.specs,
                                      proxies={'tool': self.proxy}, discovery=FakeDiscovery())
        self.context.proxy_catalog.probe = lambda *a, **k: True
        self.context.proxy_catalog.health = lambda *a, **k: True
        self.context.proxy_catalog.update({'tool': self.proxy})
        self.gateway = create_server(self.context, port=0)
        threading.Thread(target=self.gateway.serve_forever, daemon=True).start()

    def tearDown(self):
        self.state.release.set()
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
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def test_parse_and_path_allowlist(self):
        self.assertEqual(parse_service_request('/services/tool/prompt?x=1'), ('tool', '/prompt?x=1'))
        self.assertEqual(parse_service_request('/services/tool'), ('tool', '/'))
        self.assertEqual(parse_service_request('/services'), (None, None))
        self.assertTrue(path_allowed(('/history',), '/history/abc?x=1'))
        self.assertFalse(path_allowed(('/history',), '/secret'))
        self.assertFalse(path_allowed(('/history',), '/historyx'))

    def test_auth_strip_and_forward(self):
        status, payload, _ = self.request('/services/tool/system_stats', headers={'Authorization': 'Bearer service-key'})
        self.assertEqual(status, 200)
        data = json.loads(payload)
        self.assertEqual(data['path'], '/system_stats')
        self.assertIsNone(data['authorization'])
        status, payload, _ = self.request('/services/tool/prompt', {'prompt': 'ok'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)['path'], '/prompt')
        self.assertEqual(self.state.requests[-1][0], 'POST')
        self.assertNotIn('Authorization', {key.title(): value for key, value in self.state.requests[-1][3].items()})

    def test_missing_key_and_console_session_are_rejected(self):
        status, _, _ = self.request('/services/tool/system_stats', headers={'Authorization': ''})
        self.assertEqual(status, 401)
        status, _, _ = self.request('/services/missing/system_stats')
        self.assertEqual(status, 404)
        status, _, _ = self.request('/services/tool/secret')
        self.assertEqual(status, 404)

    def test_websocket_timeout_body_limit_and_no_lane(self):
        status, _, _ = self.request('/services/tool/system_stats', headers={'Upgrade': 'websocket'})
        self.assertEqual(status, 501)
        status, _, _ = self.request('/services/tool/prompt', b'x' * 65, {'Content-Type': 'text/plain'})
        self.assertEqual(status, 413)
        before = self.context.scheduler.snapshots()[0]['chat']
        status, _, _ = self.request('/services/tool/prompt', {'ok': True})
        self.assertEqual(status, 200)
        after = self.context.scheduler.snapshots()[0]['chat']
        self.assertEqual(before['active'], after['active'])
        self.assertEqual(before['waiting'], after['waiting'])
        self.assertEqual(before['queue_depth'], after['queue_depth'])
        self.proxy = normalize_proxy('tool', {**self.proxy.raw, 'timeout': 0.05})
        self.context.proxies = {'tool': self.proxy}
        self.context.proxy_catalog.update({'tool': self.proxy})
        started = time.monotonic()
        status, _, _ = self.request('/services/tool/hold', {'wait': True})
        self.assertEqual(status, 504)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_unready_and_knowledge_and_models_list(self):
        self.context.proxy_catalog.health = lambda *a, **k: False
        self.context.proxy_catalog.update({'tool': self.proxy})
        self.assertEqual(self.request('/services/tool/system_stats')[0], 503)
        self.assertEqual(self.request('/knowledge/items')[0], 200)
        status, payload, _ = self.request('/v1/models')
        self.assertEqual(status, 200)
        self.assertNotIn('tool', {item['id'] for item in json.loads(payload)['data']})
        self.assertNotIn('comfyui', {item['id'] for item in json.loads(payload)['data']})


if __name__ == '__main__':
    unittest.main()
