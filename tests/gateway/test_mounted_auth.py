"""Opt-in HTTP app authentication preserves standalone upstream credentials."""
import hashlib
import hmac
import http.client
import json
import threading
import time
import unittest
from email.message import Message
from types import SimpleNamespace

from local_llm_deploy.gateway.auth import ConsoleSessions
from http.server import BaseHTTPRequestHandler

from tests.gateway import test_gateway_app_registration as support
from local_llm_deploy.config import normalize_app, ConfigError
from local_llm_deploy.gateway.mounted_auth import signing_key, HEADER


class Echo(BaseHTTPRequestHandler):
    def do_GET(self):
        self.reply()

    def do_POST(self):
        self.reply()

    def reply(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        result = json.dumps({'path': self.path, 'headers': dict(self.headers), 'body': body.decode()}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(result)))
        self.end_headers()
        self.wfile.write(result)

    def log_message(self, *args):
        pass


class MountedAuthTests(unittest.TestCase):
    setUp = support.AppRegistrationGatewayTests.setUp
    tearDown = support.AppRegistrationGatewayTests.tearDown
    request = support.AppRegistrationGatewayTests.request

    def register(self):
        self.backend.RequestHandlerClass = Echo
        self.assertEqual(self.request('/gateway-api/v1/apps/notes', {
            'alias': 'Notes', 'prefix': '/notes', 'upstream': self.url,
            'auth': {'gateway': 'console', 'upstream': 'signed-v1'},
        }, method='PUT')[0], 200)

    def raw(self, path, method='GET', headers=None, body=None):
        c = http.client.HTTPConnection('127.0.0.1', self.gateway.server_port, timeout=2)
        c.request(method, path, body, headers or {})
        r = c.getresponse()
        result = r.status, dict(r.getheaders()), r.read()
        c.close()
        return result

    def local(self):
        return {'Origin': f'http://127.0.0.1:{self.gateway.server_port}',
                'X-Local-Console': '1', 'X-Gateway-App-Request': '1', 'Sec-Fetch-Site': 'same-origin'}

    def login(self):
        status, headers, _ = self.raw('/notes/api/v1/gateway-session', 'POST', self.local())
        self.assertEqual(status, 200)
        self.assertIn('HttpOnly; SameSite=Strict', headers['Set-Cookie'])
        self.assertIn('Path=/notes/', headers['Set-Cookie'])
        return headers['Set-Cookie'].split(';')[0]

    def test_local_session_signs_scoped_requests_and_logout_revokes(self):
        self.register()
        self.assertEqual(self.raw('/notes/')[0], 200)
        self.assertEqual(self.raw('/notes/api/v1/status')[0], 401)
        cookie = self.login()
        body = '{"action":"example"}'
        status, _, raw = self.raw('/notes/api/v1/pause?test=1', 'POST',
                                  {**self.local(), 'Cookie': cookie}, body)
        self.assertEqual(status, 200)
        echoed = json.loads(raw)
        self.assertNotIn('Cookie', echoed['headers'])
        self.assertNotIn('Authorization', echoed['headers'])
        assertion = echoed['headers'][HEADER]
        version, stamp, nonce, signature = assertion.split('.')
        message = '\n'.join((version, 'notes', '/notes', 'POST', '/api/v1/pause?test=1', stamp, nonce,
                             hashlib.sha256(body.encode()).hexdigest()))
        expected = hmac.new(signing_key('service-key', self.context.apps['notes']), message.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(signature, expected)
        self.assertEqual(self.raw('/notes/api/v1/gateway-session', 'DELETE', {**self.local(), 'Cookie': cookie})[0], 200)
        self.assertEqual(self.raw('/notes/api/v1/status', headers={'Cookie': cookie})[0], 401)

    def test_lan_session_without_api_key_signs_upstream(self):
        self.register()
        origin = f'http://192.168.1.20:{self.gateway.server_port}'
        headers = {**self.local(), 'Host': origin.removeprefix('http://'), 'Origin': origin}
        status, response, _ = self.raw('/notes/api/v1/gateway-session', 'POST', headers)
        self.assertEqual(status, 200)
        cookie = response['Set-Cookie'].split(';')[0]
        status, _, body = self.raw('/notes/api/v1/status', headers={**headers, 'Cookie': cookie})
        self.assertEqual(status, 200)
        self.assertIn(HEADER, json.loads(body)['headers'])

    def test_vpn_ingress_session_requires_private_client_and_same_origin(self):
        self.register()
        authority = f'10.8.0.5:{self.gateway.server_port}'
        headers = {**self.local(), 'Host': authority, 'Origin': 'http://' + authority,
                   'Forwarded': 'for=unknown;by=vpn-ingress', 'Via': '1.1 vpn-ingress',
                   'X-Forwarded-For': '10.8.0.6', 'X-Forwarded-Host': authority,
                   'X-Forwarded-Proto': 'http'}
        self.assertEqual(self.raw('/notes/api/v1/gateway-session', 'POST', headers)[0], 200)
        for change in ({'X-Forwarded-For': '8.8.8.8'}, {'X-Forwarded-For': '10.8.0.6, 8.8.8.8'},
                       {'Origin': 'http://evil.example'}, {'X-Forwarded-Host': 'localhost'},
                       {'X-Local-Console': ''}, {'Authorization': 'Bearer wrong'}):
            with self.subTest(change=change):
                self.assertIn(self.raw('/notes/api/v1/gateway-session', 'POST',
                                       {**headers, **change})[0], (401, 403))

    def test_lan_peer_boundary_does_not_expand_console_access(self):
        for peer in ('192.168.1.2', '10.1.2.3', '172.16.1.2', 'fd12::2', '::ffff:192.168.1.2',
                     '8.8.8.8', '100.64.0.1', '169.254.1.2', '0.0.0.0'):
            headers = Message()
            for key, value in {'Host': '192.168.1.20:8888', 'Origin': 'http://192.168.1.20:8888',
                               'X-Local-Console': '1', 'Sec-Fetch-Site': 'same-origin'}.items():
                headers[key] = value
            handler = SimpleNamespace(headers=headers, client_address=(peer, 1234),
                                      server=SimpleNamespace(server_port=8888))
            with self.subTest(peer=peer):
                expected = peer not in ('8.8.8.8', '100.64.0.1', '169.254.1.2', '0.0.0.0')
                self.assertEqual(bool(ConsoleSessions.local_origin(handler, issuing=True, allow_lan=True)), expected)
                self.assertIsNone(ConsoleSessions.local_origin(handler, issuing=True))
                headers['X-Forwarded-For'] = '192.168.1.2'
                self.assertIsNone(ConsoleSessions.local_origin(handler, issuing=True, allow_lan=True))

    def test_spoofing_cross_site_and_invalid_explicit_credentials_fail(self):
        self.register()
        self.assertEqual(self.raw('/notes/api/v1/status', headers={HEADER: 'spoof'})[0], 401)
        for change in ({'Origin': 'https://evil.example'}, {'X-Forwarded-For': '127.0.0.1'},
                       {'Authorization': 'Bearer wrong'}, {'Sec-Fetch-Site': 'cross-site'}):
            with self.subTest(change=change):
                self.assertIn(self.raw('/notes/api/v1/gateway-session', 'POST', {**self.local(), **change})[0], (401, 403))
        cookie = self.login()
        self.assertEqual(self.raw('/notes/api/v1/status', headers={'Cookie': cookie, 'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.raw('/notes/api/v1/pause', 'POST', {'Cookie': cookie})[0], 403)
        self.assertEqual(self.raw('/notes/api/v1/status', headers={'Cookie': cookie, 'Authorization': 'Bearer wrong'})[0], 401)

    def test_gateway_key_auth_strips_credentials_and_rotation_invalidates_sessions(self):
        self.register()
        status, _, raw = self.raw('/notes/api/v1/status', headers={'Authorization': 'Bearer service-key', HEADER: 'spoof'})
        self.assertEqual(status, 200)
        self.assertNotIn('service-key', raw.decode())
        self.assertNotIn('spoof', raw.decode())
        cookie = self.login()
        (self.root / '.api-key').write_text('rotated-key\n')
        self.assertEqual(self.raw('/notes/api/v1/status', headers={'Cookie': cookie})[0], 401)
        (self.root / '.api-key').unlink()
        self.assertEqual(self.raw('/notes/api/v1/status')[0], 503)

    def test_sessions_do_not_cross_app_boundary_or_expiry(self):
        self.register()
        cookie = self.login()
        self.assertEqual(self.request('/gateway-api/v1/apps/second', {
            'prefix': '/second', 'upstream': self.url,
            'auth': {'gateway': 'console', 'upstream': 'signed-v1'},
        }, method='PUT')[0], 200)
        self.assertEqual(self.raw('/second/api/v1/status', headers={'Cookie': cookie})[0], 401)
        self.context.mounted_auth.sessions = {k: (time.time()-1, *v[1:]) for k, v in self.context.mounted_auth.sessions.items()}
        self.assertEqual(self.raw('/notes/api/v1/status', headers={'Cookie': cookie})[0], 401)

    def test_invalid_auth_configuration_is_rejected(self):
        for auth in ({'gateway': 'none'}, {'gateway': 'console', 'upstream': 'none'}):
            with self.assertRaises(ConfigError):
                normalize_app('notes', {'type': 'app', 'kind': 'http', 'prefix': '/notes', 'upstream': self.url, 'auth': auth})
