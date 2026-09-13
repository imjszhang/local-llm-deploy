"""Private history HTTP routes never become anonymous model-API pass-through."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from local_llm_deploy.config import ProjectPaths
from local_llm_deploy.gateway.app import GatewayContext, create_server
from local_llm_deploy.gateway.chat_store import MAX_BYTES
from tests.gateway.test_chat_store import session


class ChatHistoryHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name))
        self.paths.api_key.write_text('synthetic-history-root')
        self.paths.static.mkdir()
        (self.paths.static / 'monitor.html').write_text('static fixture')
        self.context = GatewayContext(self.paths, specs={})
        self.gateway = create_server(self.context, port=0)
        self.thread = threading.Thread(target=self.gateway.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.origin = 'http://127.0.0.1:' + str(self.gateway.server_port)
        self.local = {'X-Local-Console': '1', 'Origin': self.origin, 'Sec-Fetch-Site': 'same-origin'}

    def close(self):
        self.gateway.shutdown()
        self.gateway.server_close()
        self.thread.join(2)

    def request(self, path='/chat-api/v1/sessions', method='GET', body=None, headers=None, authorized=True):
        actual = {'Authorization': 'Bearer synthetic-history-root'} if authorized else {}
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            actual['Content-Type'] = 'application/json'
        actual.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.gateway.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=actual)
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def issue(self):
        status, body, _ = self.request('/console-api/v1/session', 'POST', headers=self.local, authorized=False)
        self.assertEqual(status, 200)
        return {**self.local, 'Authorization': 'Bearer ' + json.loads(body)['token']}

    def test_root_authorized_crud_and_no_store_headers(self):
        with patch.object(self.context.transport, 'forward', side_effect=AssertionError('Inference transport used')):
            envelope = {'schema_version': 1, 'session': session(), 'revision': 0}
            status, body, headers = self.request('/chat-api/v1/sessions/session-1', 'PUT', envelope)
            self.assertEqual(status, 200)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertEqual(json.loads(body)['revision'], 1)
            status, body, headers = self.request()
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['sessions'][0]['id'], 'session-1')
            self.assertEqual(headers['Cache-Control'], 'no-store')
            status, body, _ = self.request('/chat-api/v1/sessions/session-1')
            self.assertEqual(json.loads(body)['session'], session())
            self.assertEqual(self.request('/chat-api/v1/sessions/session-1', 'PUT', envelope)[0], 409)
            status, body, headers = self.request('/chat-api/v1/sessions/session-1', 'DELETE', {'revision': 1})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {'deleted': True, 'id': 'session-1'})
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertEqual(self.request('/chat-api/v1/sessions/session-1')[0], 404)
        for file in self.context.chat_store.path.parent.iterdir():
            self.assertNotIn(b'synthetic-history-root', file.read_bytes())
            self.assertNotIn(b'Authorization', file.read_bytes())

    def test_missing_root_key_never_opens_anonymous_private_history(self):
        for root_exists in (True, False):
            if not root_exists:
                self.paths.api_key.unlink()
            for headers in ({}, {'Authorization': 'Bearer arbitrary'}, self.local):
                status, body, result_headers = self.request(headers=headers, authorized=False)
                self.assertEqual(status, 401)
                self.assertEqual(result_headers['Cache-Control'], 'no-store')
                self.assertNotIn(str(self.paths.root).encode(), body)
            self.assertFalse(self.context.chat_store.path.parent.exists())

    def test_local_grant_allows_only_same_origin_crud_even_without_root_key(self):
        self.paths.api_key.unlink()
        headers = self.issue()
        target = '/chat-api/v1/sessions/session-1'
        envelope = {'schema_version': 1, 'session': session(), 'revision': 0}
        self.assertEqual(self.request(target, 'PUT', envelope, headers, authorized=False)[0], 200)
        self.assertEqual(self.request(headers=headers, authorized=False)[0], 200)
        self.assertEqual(self.request(target, headers=headers, authorized=False)[0], 200)
        for override in ({'Origin': 'http://evil.example'}, {'Sec-Fetch-Site': 'cross-site'},
                         {'X-Local-Console': '0'}, {'Forwarded': 'for=127.0.0.1'}, {'Host': 'evil.example'}):
            status, _, result_headers = self.request(target, headers={**headers, **override}, authorized=False)
            self.assertEqual(status, 401)
            self.assertEqual(result_headers['Cache-Control'], 'no-store')
        # A stolen local token does not authorize a connection with a LAN peer.
        with patch.object(self.context.console_sessions, 'local_origin', return_value=None):
            self.assertEqual(self.request(target, headers=headers, authorized=False)[0], 401)
        self.assertEqual(self.request(target, 'DELETE', {'revision': 1}, headers, authorized=False)[0], 200)
        self.assertEqual(self.request('/v1/models', headers=headers, authorized=False)[0], 401)

    def test_namespace_methods_shapes_and_limits_are_checked_before_database_io(self):
        cases = [('/chat-api', 'GET', None, 404), ('/chat-api/v2/sessions', 'GET', None, 404),
                 ('/chat-api/v1/sessions/', 'GET', None, 404), ('/chat-api/v1/sessions?x=1', 'GET', None, 400),
                 ('/chat-api/v1/sessions/a/extra', 'GET', None, 404), ('/chat-api/v1/sessions/a%2Fb', 'GET', None, 404),
                 ('/chat-api/v1/sessions', 'GET', {}, 400), ('/chat-api/v1/sessions/a', 'PUT', {}, 400),
                 ('/chat-api/v1/sessions/a', 'DELETE', {'revision': True}, 400)]
        for path, method, body, expected in cases:
            status, _, headers = self.request(path, method, body)
            self.assertEqual(status, expected, (path, method))
            self.assertEqual(headers['Cache-Control'], 'no-store')
        for method in ('HEAD', 'POST', 'PATCH', 'OPTIONS', 'TRACE', 'CONNECT', 'PUT', 'DELETE'):
            status, body, headers = self.request(method=method)
            self.assertEqual(status, 405, method)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            if method == 'HEAD':
                self.assertEqual(body, b'')
        self.assertEqual(self.request('/chat-api/v1/sessions/a', 'PUT', b'{}')[0], 415)
        self.assertEqual(self.request('/chat-api/v1/sessions/a', 'PUT', b'{}',
                         {'Content-Type': 'application/json', 'Transfer-Encoding': 'chunked'})[0], 400)
        self.assertFalse(self.context.chat_store.path.parent.exists())

    def test_expect_rejects_auth_size_type_and_method_before_100_continue(self):
        cases = [({}, 'PUT', 2, 401), ({'Authorization': 'Bearer synthetic-history-root'}, 'PUT', MAX_BYTES + 1, 413),
                 ({'Authorization': 'Bearer synthetic-history-root'}, 'POST', 2, 405),
                 ({'Authorization': 'Bearer synthetic-history-root', 'Content-Type': 'text/plain'}, 'PUT', 2, 415)]
        for headers, method, length, expected in cases:
            with socket.create_connection(('127.0.0.1', self.gateway.server_port), timeout=3) as sock:
                request = (f'{method} /chat-api/v1/sessions/id HTTP/1.1\r\nHost: 127.0.0.1:{self.gateway.server_port}\r\n'
                           f'Content-Length: {length}\r\nExpect: 100-continue\r\n')
                request += ''.join(f'{name}: {value}\r\n' for name, value in headers.items())
                sock.sendall((request + '\r\n').encode())
                response = sock.recv(4096)
                self.assertIn(str(expected).encode(), response.split(b'\r\n', 1)[0])
                self.assertNotIn(b'100 Continue', response)
        self.assertFalse(self.context.chat_store.path.parent.exists())

    def test_invalid_json_and_unknown_headers_field_cannot_be_saved(self):
        envelope = {'schema_version': 1, 'session': session(), 'revision': 0}
        envelope['session']['turns'][0]['answers'][0]['request']['headers'] = {'Authorization': 'synthetic-private'}
        for body in (envelope, b'{"revision":0,"revision":1}', b'{"session":NaN}'):
            status, response, headers = self.request('/chat-api/v1/sessions/session-1', 'PUT', body,
                                                       {'Content-Type': 'application/json'})
            self.assertEqual(status, 400)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertNotIn(b'synthetic-private', response)
        self.assertFalse(self.context.chat_store.path.parent.exists())

    def test_session_near_upload_limit_remains_readable_with_bounded_envelope(self):
        value = session()
        answer = value['turns'][0]['answers'][0]
        answer['content'] = 'x' * (4 * 1024 * 1024)
        answer['reasoning'] = ''
        envelope = {'schema_version': 1, 'session': value, 'revision': 0}
        encode = lambda item: json.dumps(item, ensure_ascii=False, separators=(',', ':')).encode()
        answer['reasoning'] = 'y' * (MAX_BYTES - 16 - len(encode(envelope)))
        upload = encode(envelope)
        self.assertEqual(len(upload), MAX_BYTES - 16)
        target = '/chat-api/v1/sessions/session-1'
        status, written, headers = self.request(target, 'PUT', upload, {'Content-Type': 'application/json'})
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertGreater(len(written), MAX_BYTES)
        self.assertLessEqual(len(written), MAX_BYTES + 1024)
        self.assertEqual(written, encode(json.loads(written)))
        status, restored, _ = self.request(target)
        self.assertEqual(status, 200)
        self.assertEqual(restored, written)
        self.assertEqual(json.loads(restored)['session'], value)

    def test_session_capacity_has_distinct_507_status(self):
        self.context.chat_store.max_sessions = 0
        status, body, headers = self.request('/chat-api/v1/sessions/session-1', 'PUT',
                                              {'schema_version': 1, 'session': session(), 'revision': 0})
        self.assertEqual(status, 507)
        self.assertEqual(json.loads(body)['error']['type'], 'history_limit')
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_unavailable_credentials_and_storage_fail_closed(self):
        self.paths.api_key.unlink()
        self.paths.api_key.mkdir()
        status, body, headers = self.request()
        self.assertEqual(status, 503)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn(str(self.paths.root).encode(), body)
        self.assertFalse(self.context.chat_store.path.parent.exists())
        self.paths.api_key.rmdir()
        self.paths.api_key.write_text('synthetic-history-root')
        self.context.chat_store.path.parent.write_text('blocked')
        status, body, headers = self.request()
        self.assertEqual(status, 503)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn(str(self.paths.root).encode(), body)


if __name__ == '__main__':
    unittest.main()
