"""Local console grants never expose the root key or widen the proxy API."""
from __future__ import annotations

from email.message import Message
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest

from local_llm_deploy.gateway.auth import ApiAuth, ConsoleSessions
from tests.gateway import test_gateway_runtime as runtime


class ConsoleSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.key_path = Path(self.tmp.name) / '.api-key'
        self.key_path.write_text('synthetic-console-root-key')
        self.now = [10.]
        self.sessions = ConsoleSessions(ApiAuth(self.key_path), ttl=60, limit=2,
                                        clock=lambda: self.now[0], wall_clock=lambda: 1_800_000_000.)
        self.origin = 'http://127.0.0.1:8888'

    def handler(self, *, headers=None, peer='127.0.0.1', method='GET'):
        values = {'Host': '127.0.0.1:8888', 'X-Local-Console': '1', 'Origin': self.origin}
        values.update(headers or {})
        message = Message()
        for key, value in values.items():
            if value is not None:
                message[key] = value
        return SimpleNamespace(headers=message, client_address=(peer, 1234),
                               server=SimpleNamespace(server_port=8888), command=method)

    def test_actual_peer_host_port_origin_and_proxy_boundaries(self):
        self.assertEqual(self.sessions.local_origin(self.handler(), issuing=True), self.origin)
        cases = [({'Host': 'external.example:8888'}, '127.0.0.1'),
                 ({'Host': '127.0.0.1:9999'}, '127.0.0.1'),
                 ({'Host': 'localhost'}, '127.0.0.1'),
                 ({'Host': 'user@127.0.0.1:8888'}, '127.0.0.1'),
                 ({'Host': '127.0.0.1:8888/path'}, '127.0.0.1'),
                 ({'Origin': 'http://evil.example'}, '127.0.0.1'),
                 ({'Origin': 'null'}, '127.0.0.1'),
                 ({'Origin': self.origin + '/'}, '127.0.0.1'),
                 ({'Origin': None}, '127.0.0.1'),
                 ({'X-Local-Console': None}, '127.0.0.1'),
                 ({'Forwarded': 'for=127.0.0.1'}, '127.0.0.1'),
                 ({'Via': '1.1 proxy'}, '127.0.0.1'),
                 ({'X-Forwarded-Host': 'localhost'}, '127.0.0.1'),
                 ({'X-Forwarded-Anything': ''}, '127.0.0.1'),
                 ({'X-Real-IP': '127.0.0.1'}, '127.0.0.1'),
                 ({'Sec-Fetch-Site': 'cross-site'}, '127.0.0.1'),
                 ({}, '192.168.1.3'), ({}, '::ffff:192.168.1.3')]
        for headers, peer in cases:
            with self.subTest(headers=headers, peer=peer):
                self.assertIsNone(self.sessions.local_origin(self.handler(headers=headers, peer=peer), issuing=True))
        self.assertEqual(self.sessions.local_origin(self.handler(peer='::ffff:127.0.0.1')), self.origin)
        for host in ('localhost:8888', '[::1]:8888'):
            origin = 'http://' + host
            handler = self.handler(headers={'Host': host, 'Origin': origin}, peer='::1')
            self.assertEqual(self.sessions.local_origin(handler, issuing=True), origin)

    def test_duplicate_security_headers_are_rejected(self):
        for name, value in [('Host', '127.0.0.1:8888'), ('Origin', self.origin),
                            ('X-Local-Console', '1'), ('Sec-Fetch-Site', 'same-origin')]:
            handler = self.handler(headers={'Sec-Fetch-Site': 'same-origin'})
            handler.headers[name] = value
            self.assertIsNone(self.sessions.local_origin(handler, issuing=True), name)

    def test_expiration_key_rotation_origin_binding_and_bounded_storage(self):
        first = self.sessions.issue(self.origin)
        self.assertEqual(first['expires_at'], 1_800_000_060_000)
        handler = self.handler(headers={'Authorization': 'Bearer ' + first['token']})
        self.assertTrue(self.sessions.authorized(handler, '/monitor-api/v1/snapshot'))
        self.now[0] += 60
        self.assertFalse(self.sessions.authorized(handler, '/monitor-api/v1/snapshot'))
        token = self.sessions.issue(self.origin)['token']
        handler.headers.replace_header('Authorization', 'Bearer ' + token)
        handler.headers.replace_header('Host', 'localhost:8888')
        handler.headers.replace_header('Origin', 'http://localhost:8888')
        self.assertFalse(self.sessions.authorized(handler, '/monitor-api/v1/snapshot'))
        handler = self.handler(headers={'Authorization': 'Bearer ' + token})
        self.key_path.write_text('rotated-synthetic-console-key')
        self.assertFalse(self.sessions.authorized(handler, '/monitor-api/v1/snapshot'))
        oldest = self.sessions.issue(self.origin)['token']
        self.sessions.issue(self.origin)
        newest = self.sessions.issue(self.origin)['token']
        self.assertEqual(len(self.sessions._sessions), 2)
        self.assertFalse(self.sessions.authorized(self.handler(headers={'Authorization': 'Bearer ' + oldest}),
                                                  '/monitor-api/v1/snapshot'))
        self.assertTrue(self.sessions.authorized(self.handler(headers={'Authorization': 'Bearer ' + newest}),
                                                 '/monitor-api/v1/snapshot'))
        self.assertNotIn(newest, repr(self.sessions._sessions))
        self.assertNotIn('rotated-synthetic-console-key', repr(self.sessions._sessions))

    def test_browser_get_without_origin_requires_same_origin_fetch_metadata(self):
        token = self.sessions.issue(self.origin)['token']
        headers = {'Authorization': 'Bearer ' + token, 'Origin': None, 'Sec-Fetch-Site': 'same-origin'}
        self.assertTrue(self.sessions.authorized(self.handler(headers=headers), '/monitor-api/v1/snapshot'))
        for site in (None, 'none', 'same-site', 'cross-site'):
            headers['Sec-Fetch-Site'] = site
            self.assertFalse(self.sessions.authorized(self.handler(headers=headers), '/monitor-api/v1/snapshot'))
        headers['Sec-Fetch-Site'] = 'same-origin'
        self.assertIsNone(self.sessions.local_origin(self.handler(headers=headers), issuing=True))

    def test_token_scope_tampering_duplicate_and_missing_key_rotation(self):
        token = self.sessions.issue(self.origin)['token']
        handler = self.handler(headers={'Authorization': 'Bearer ' + token})
        for method, path in [('GET', '/v1/models'), ('POST', '/v1/embeddings'),
                             ('POST', '/api/chat/v1/chat/completions'), ('GET', '/api/chat/health'),
                             ('DELETE', '/monitor-api/v1/snapshot'), ('GET', '/v1/chat/completions')]:
            handler.command = method
            self.assertFalse(self.sessions.authorized(handler, path))
        handler.command = 'POST'
        self.assertTrue(self.sessions.authorized(handler, '/v1/chat/completions'))
        handler.headers['Authorization'] = 'Bearer ' + token
        self.assertFalse(self.sessions.authorized(handler, '/v1/chat/completions'))
        handler = self.handler(headers={'Authorization': 'Bearer ' + token + 'tampered'})
        self.assertFalse(self.sessions.authorized(handler, '/monitor-api/v1/snapshot'))
        self.key_path.unlink()
        handler = self.handler(headers={'Authorization': 'Bearer ' + token})
        self.assertFalse(self.sessions.authorized(handler, '/monitor-api/v1/snapshot'))
        fresh = self.sessions.issue(self.origin)['token']
        self.assertTrue(self.sessions.authorized(self.handler(headers={'Authorization': 'Bearer ' + fresh}),
                                                 '/monitor-api/v1/snapshot'))


class ConsoleHTTPTests(unittest.TestCase):
    def setUp(self):
        runtime.RuntimeTests.setUp(self)
        self.context.paths.api_key.write_text('synthetic-console-root-key')
        self.context.monitor_api.close()
        self.context.monitor_api = SimpleNamespace(snapshot=lambda: {'schema_version': 1, 'models': []},
                                                   detail=lambda key, output: {'key': key, 'output': output},
                                                   close=lambda: None)
        self.origin = f'http://127.0.0.1:{self.gateway.server_port}'
        self.local_headers = {'Origin': self.origin, 'X-Local-Console': '1'}

    tearDown = runtime.RuntimeTests.tearDown
    open = runtime.RuntimeTests.open
    request = runtime.RuntimeTests.request

    def issue(self):
        status, body, headers = self.request('/console-api/v1/session', headers=self.local_headers, method='POST')
        self.assertEqual(status, 200, body)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        self.assertNotIn('Set-Cookie', headers)
        value = json.loads(body)
        self.assertEqual(set(value), {'token', 'expires_at'})
        self.assertNotIn('synthetic-console-root-key', body.decode())
        return value['token']

    def test_automatic_session_monitor_and_chat_with_root_key_kept_server_side(self):
        token = self.issue()
        headers = {**self.local_headers, 'Authorization': 'Bearer ' + token}
        self.assertEqual(self.request('/monitor-api/v1/snapshot', headers=headers)[0], 200)
        self.assertEqual(self.request('/monitor-api/v1/models/chat?include_output=1', headers=headers)[0], 200)
        status, body, _ = self.request('/monitor-api/v1/snapshot', headers=headers, method='HEAD')
        self.assertEqual((status, body), (200, b''))
        get_headers = {'X-Local-Console': '1', 'Sec-Fetch-Site': 'same-origin', 'Authorization': 'Bearer ' + token}
        self.assertEqual(self.request('/monitor-api/v1/snapshot', headers=get_headers)[0], 200)
        self.assertEqual(self.request(body={'model': 'friendly-chat', 'messages': []}, headers=headers)[0], 200)
        self.assertEqual(self.state.requests[-1][3]['Authorization'], 'Bearer synthetic-console-root-key')
        self.assertNotIn(token, repr(self.state.requests))

    def test_scope_and_connection_boundaries_checked_on_every_use(self):
        token = self.issue()
        headers = {**self.local_headers, 'Authorization': 'Bearer ' + token}
        for path in ('/v1/models', '/api/chat/health', '/api/ollama/api/tags', '/knowledge/items'):
            self.assertEqual(self.request(path, headers=headers)[0], 401, path)
        for path in ('/v1/embeddings', '/api/chat/v1/chat/completions', '/v1/responses'):
            self.assertEqual(self.request(path, {'model': 'friendly-chat'}, headers)[0], 401, path)
        self.assertEqual(self.request('/monitor-api/v1/snapshot', headers={'Authorization': 'Bearer ' + token})[0], 401)
        for extra in ({'Origin': 'http://evil.example'}, {'X-Forwarded-For': '127.0.0.1'},
                      {'Host': f'localhost:{self.gateway.server_port}', 'Origin': f'http://localhost:{self.gateway.server_port}'}):
            self.assertEqual(self.request('/monitor-api/v1/snapshot', headers={**headers, **extra})[0], 401)
        self.assertFalse(self.state.requests)

    def test_root_credentials_and_public_routes_keep_existing_behavior(self):
        root = {'Authorization': 'Bearer synthetic-console-root-key'}
        for path in ('/monitor-api/v1/snapshot', '/v1/models', '/api/chat/health'):
            self.assertEqual(self.request(path, headers=root)[0], 200, path)
        for path in ('/monitor-api/v1/snapshot', '/v1/models', '/v1/chat/completions'):
            self.assertEqual(self.request(path)[0], 401, path)
        self.assertEqual(self.request('/api/models')[0], 200)

    def test_root_and_independent_knowledge_keys_can_share_the_session_prefix(self):
        key = 'local-console.synthetic-valid-root-key'
        self.context.paths.api_key.write_text(key)
        headers = {'Authorization': 'Bearer ' + key}
        for path in ('/monitor-api/v1/snapshot', '/v1/models', '/api/chat/health'):
            self.assertEqual(self.request(path, headers=headers)[0], 200, path)
        self.assertEqual(self.request(body={'model': 'friendly-chat'}, headers=headers)[0], 200)
        for knowledge_key in (key, 'local-console.synthetic-independent-knowledge-key'):
            authorization = 'Bearer ' + knowledge_key
            self.assertEqual(self.request('/knowledge/items', headers={'Authorization': authorization})[0], 200)
            self.assertEqual(self.state.requests[-1][3]['Authorization'], authorization)

    def test_session_namespace_methods_bodies_queries_errors_do_not_issue(self):
        for method in ('GET', 'HEAD', 'OPTIONS', 'PUT', 'DELETE', 'PATCH', 'TRACE', 'CONNECT'):
            status, body, headers = self.request('/console-api/v1/session', headers=self.local_headers, method=method)
            self.assertEqual(status, 405, method)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            if method == 'HEAD':
                self.assertEqual(body, b'')
        for path in ('/console-api', '/console-api/v2/session', '/console-api/v1/session?x=1', '/console-api/v1/session/'):
            self.assertEqual(self.request(path, headers=self.local_headers, method='POST')[0], 404)
        status, _, headers = self.request('/console-api/v1/session', b'{}', self.local_headers)
        self.assertEqual(status, 400)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        for extra in ({}, {'Origin': self.origin}, {'X-Local-Console': '1'},
                      {**self.local_headers, 'Origin': 'null'}, {**self.local_headers, 'Forwarded': 'for=127.0.0.1'}):
            status, _, headers = self.request('/console-api/v1/session', headers=extra, method='POST')
            self.assertEqual(status, 403)
            self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertFalse(self.context.console_sessions._sessions)

    def test_key_rotation_missing_key_and_backend_key_replacement(self):
        token = self.issue()
        headers = {**self.local_headers, 'Authorization': 'Bearer ' + token}
        self.context.paths.api_key.write_text('rotated-console-root-key')
        self.assertEqual(self.request('/monitor-api/v1/snapshot', headers=headers)[0], 401)
        self.context.paths.api_key.unlink()
        token = self.issue()
        headers['Authorization'] = 'Bearer ' + token
        self.assertEqual(self.request('/monitor-api/v1/snapshot', headers=headers)[0], 200)
        self.assertEqual(self.request('/v1/models', headers=headers)[0], 401)
        self.assertEqual(self.request(body={'model': 'friendly-chat'}, headers=headers)[0], 200)
        self.assertNotIn('Authorization', self.state.requests[-1][3])
        self.assertNotIn(token, repr(self.state.requests))

    def test_expect_rejects_before_accepting_unauthorized_or_nonempty_upload(self):
        cases = [(None, 0, '403'), (self.local_headers, 4, '400')]
        for headers, length, expected in cases:
            with socket.create_connection(('127.0.0.1', self.gateway.server_port), timeout=2) as sock:
                request = (f'POST /console-api/v1/session HTTP/1.1\r\nHost: 127.0.0.1:{self.gateway.server_port}\r\n'
                           f'Content-Length: {length}\r\nExpect: 100-continue\r\n')
                request += ''.join(f'{name}: {value}\r\n' for name, value in (headers or {}).items())
                sock.sendall((request + '\r\n').encode())
                response = sock.recv(4096)
                self.assertIn(expected.encode(), response.split(b'\r\n', 1)[0])
                self.assertNotIn(b'100 Continue', response)
        self.assertFalse(self.context.console_sessions._sessions)
        token = self.issue()
        headers = {**self.local_headers, 'Authorization': 'Bearer ' + token, 'Expect': '100-continue'}
        self.assertEqual(self.request(body={'model': 'friendly-chat'}, headers=headers)[0], 200)

    def test_unreadable_root_key_fails_closed_without_exposing_internal_path(self):
        self.context.paths.api_key.unlink()
        self.context.paths.api_key.mkdir()
        status, body, headers = self.request('/console-api/v1/session', headers=self.local_headers, method='POST')
        self.assertEqual(status, 503)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn(str(self.root).encode(), body)
        self.assertFalse(self.context.console_sessions._sessions)


if __name__ == '__main__':
    unittest.main()
