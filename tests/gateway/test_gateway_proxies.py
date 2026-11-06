"""Generic /services/<key>/ forwarding stays off the chat lane."""
from __future__ import annotations

import base64
import hashlib
import socket
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

    do_PATCH = do_POST
    do_OPTIONS = do_POST

    def respond(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.server.state.requests.append((self.command, self.path, body, dict(self.headers)))
        if self.path.startswith('/hold'):
            self.server.state.release.wait(3)
        if self.path.startswith('/ws'):
            key = self.headers.get('Sec-WebSocket-Key', '')
            accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            self.send_header('Sec-WebSocket-Accept', accept)
            self.end_headers()
            # Send a frame immediately with the upgrade, exercising read-ahead.
            self.wfile.write(b'\x82\x03abc')
            self.wfile.flush()
            frame = self.rfile.read(8)
            if len(frame) == 8:
                payload = bytes(frame[6 + i] ^ frame[2 + i % 4] for i in range(2))
                self.wfile.write(b'\x81\x02' + payload)
                self.wfile.flush()
            self.close_connection = True
            return
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/target?x=1')
            self.send_header('Set-Cookie', 'upstream=ok; Path=/; HttpOnly')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
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
        from tests.gateway.app_fixtures import knowledge_app
        self.context = GatewayContext(ProjectPaths(self.root), settings=settings, specs=self.specs,
                                      proxies={'tool': self.proxy}, discovery=FakeDiscovery(),
                                      apps=knowledge_app(settings.knowledge_url))
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

    def enable_browser(self):
        self.proxy = normalize_proxy('tool', {**self.proxy.raw, 'paths': None,
            'browser': True, 'websocket': True, 'health_check': False, 'timeout': 1,
            'methods': ['GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS']})
        self.context.proxies = {'tool': self.proxy}
        self.context.proxy_catalog.health = lambda *a, **k: False
        self.context.proxy_catalog.update({'tool': self.proxy})

    def browser_login(self):
        basic = 'Basic ' + base64.b64encode(b'user:service-key').decode()
        status, _, headers = self.request('/services/tool/', headers={'Authorization': basic})
        self.assertEqual(status, 200)
        return headers['Set-Cookie'].split(';', 1)[0]

    def test_browser_login_cookie_resources_and_scope(self):
        self.enable_browser()
        status, _, headers = self.request('/services/tool/', headers={'Authorization': ''})
        self.assertEqual(status, 401)
        self.assertIn('Basic', headers['WWW-Authenticate'])
        cookie = self.browser_login()
        status, payload, _ = self.request('/services/tool/assets/app.js?x=1',
            headers={'Authorization': '', 'Cookie': cookie + '; upstream=ok'})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)['path'], '/assets/app.js?x=1')
        sent = self.state.requests[-1][3]
        self.assertEqual(sent.get('Cookie'), 'upstream=ok')
        self.assertNotIn('Authorization', sent)
        self.assertEqual(self.request('/v1/models', headers={'Authorization': '', 'Cookie': cookie})[0], 401)
        self.assertEqual(self.request('/services/tool/', headers={
            'Authorization': '', 'Cookie': cookie, 'Origin': 'https://evil.example'})[0], 401)
        (self.root / '.api-key').write_text('rotated')
        self.assertEqual(self.request('/services/tool/', headers={'Authorization': '', 'Cookie': cookie})[0], 401)

    def test_browser_redirect_and_upstream_cookie_mapping(self):
        self.enable_browser()
        status, _, headers = self.request('/services/tool?x=1')
        self.assertEqual(status, 308)
        self.assertEqual(headers['Location'], './tool/?x=1')
        status, _, headers = self.request('/services/tool/redirect')
        self.assertEqual(status, 302)
        self.assertEqual(headers['Location'], '/services/tool/target?x=1')
        self.assertIn('Path=/services/tool/', headers['Set-Cookie'])

    def test_unrestricted_routes_binary_upload_and_path_preservation(self):
        self.enable_browser()
        for method in ('PATCH', 'OPTIONS'):
            self.assertEqual(self.request('/services/tool/custom', method=method)[0], 200)
        payload = b'--boundary\r\nContent-Type: image/png\r\n\r\n\x00\xff\r\n--boundary--'
        status, _, _ = self.request('/services/tool/upload/mask', payload,
                                   {'Content-Type': 'multipart/form-data; boundary=boundary'})
        self.assertEqual(status, 200)
        self.assertEqual(self.state.requests[-1][2], payload)
        status, body, _ = self.request('/services/tool/extensions/custom/?x=a%2Fb')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['path'], '/extensions/custom/?x=a%2Fb')
        status, body, _ = self.request('/services/tool/userdata/%E6%B5%8B%E8%AF%95%20file.json')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['path'], '/userdata/%E6%B5%8B%E8%AF%95%20file.json')

    def test_websocket_binary_and_text_with_browser_cookie(self):
        self.enable_browser()
        cookie = self.browser_login()
        sock = socket.create_connection(('127.0.0.1', self.gateway.server_port), timeout=2)
        self.addCleanup(sock.close)
        key = base64.b64encode(b'0123456789abcdef').decode()
        host = f'127.0.0.1:{self.gateway.server_port}'
        sock.sendall((f'GET /services/tool/ws?clientId=test HTTP/1.1\r\nHost: {host}\r\n'
            f'Origin: http://{host}\r\nCookie: {cookie}\r\nUpgrade: websocket\r\n'
            f'Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\n\r\n').encode())
        stream = sock.makefile('rb', buffering=0)
        self.addCleanup(stream.close)
        self.assertIn(b'101', stream.readline())
        while stream.readline() != b'\r\n':
            pass
        def read_exact(count):
            data = b''
            while len(data) < count:
                chunk = stream.read(count - len(data))
                self.assertTrue(chunk)
                data += chunk
            return data
        self.assertEqual(read_exact(5), b'\x82\x03abc')
        mask = b'abcd'
        sock.sendall(b'\x81\x82' + mask + bytes([ord('o') ^ mask[0], ord('k') ^ mask[1]]))
        self.assertEqual(read_exact(4), b'\x81\x02ok')
        request = self.state.requests[-1]
        self.assertEqual(request[1], '/ws?clientId=test')
        self.assertNotIn('Cookie', request[3])
        self.assertEqual(request[3]['Origin'], self.url)

    def test_absolute_asset_redirect_is_scoped_to_service_referrer(self):
        self.enable_browser()
        host = f'127.0.0.1:{self.gateway.server_port}'
        status, _, headers = self.request('/assets/images/progress.png', headers={
            'Authorization': '', 'Referer': f'http://{host}/services/tool/'})
        self.assertEqual(status, 307)
        self.assertEqual(headers['Location'], '/services/tool/assets/images/progress.png')
        self.assertEqual(self.request('/assets/images/progress.png', headers={
            'Referer': 'http://evil.example/services/tool/'})[0], 404)
        self.assertEqual(self.request('/v1/models', headers={
            'Authorization': '', 'Referer': f'http://{host}/services/tool/'})[0], 401)

    def test_websocket_requires_authorization_and_valid_handshake(self):
        self.enable_browser()
        self.assertEqual(self.request('/services/tool/ws', headers={
            'Authorization': '', 'Upgrade': 'websocket'})[0], 401)
        self.assertEqual(self.request('/services/tool/ws', headers={'Upgrade': 'websocket'})[0], 400)


if __name__ == '__main__':
    unittest.main()
