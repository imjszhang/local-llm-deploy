"""Black-box HTTP regressions with temporary files and a controllable fake backend."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from local_llm_deploy.config import ProjectPaths, normalize_models
from local_llm_deploy.gateway.app import GatewayContext, create_server
from local_llm_deploy.gateway.discovery import Backend, Discovery
from local_llm_deploy.gateway.settings import GatewaySettings
from local_llm_deploy.gateway.transport import Relay, Transport
from local_llm_deploy.observability import BoundedCapture


class FakeState:
    def __init__(self):
        self.requests = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.first_sent = threading.Event()


class ControlledBackend(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def do_DELETE(self):
        self.respond()

    def respond(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        state = self.server.state
        state.requests.append((self.command, self.path, body, dict(self.headers)))
        try:
            data = json.loads(body or b'{}')
        except ValueError:
            data = {}
        mode = data.get('mode')
        state.started.set()
        try:
            if mode == 'hold':
                state.release.wait(3)
            if mode == 'stream':
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(b'data: {"delta":"first"}\n\n')
                self.wfile.flush()
                state.first_sent.set()
                state.release.wait(3)
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
                self.close_connection = True
                return
            if mode == 'truncate':
                self.send_response(200)
                self.send_header('Content-Length', '100')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(b'partial')
                self.wfile.flush()
                self.close_connection = True
                return
            payload = json.dumps({'error': {'message': 'fake failure'}} if mode == 'error' else {'ok': True}).encode()
            self.send_response(422 if mode == 'error' else 200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Set-Cookie', 'first=1; Path=/')
            self.send_header('Set-Cookie', 'second=2; Path=/')
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'static').mkdir()
        self.state = FakeState()
        self.backend = ThreadingHTTPServer(('127.0.0.1', 0), ControlledBackend)
        self.backend.state = self.state
        threading.Thread(target=self.backend.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.backend.server_port}'
        configs = {
            'chat': {'alias': 'friendly-chat', 'endpoints': ['/v1/chat/completions', '/v1/completions', '/v1/responses', '/v1/messages']},
            'embedding': {'type': 'embedding', 'alias': 'friendly-embed'},
            'rerank': {'type': 'rerank', 'alias': 'friendly-rerank'},
            'asr': {'type': 'asr', 'alias': 'friendly-asr'},
            'offline': {'alias': 'offline-alias'},
            'ollama': {'type': 'ollama', 'alias': 'friendly-ollama', 'ollama_model': 'tag:latest'},
        }
        self.specs = normalize_models(configs)
        self.models = {key: Backend(key, spec.alias, self.url, spec.capabilities, spec.backend,
                                   spec.raw.get('ollama_model'), endpoints=spec.raw.get('endpoints'))
                       for key, spec in self.specs.items() if key != 'offline'}
        owner = self
        class FakeDiscovery:
            def models(self):
                return owner.models.copy()
            def ollama_status(self):
                return {'status': 'offline'}
        self.settings = GatewaySettings(keepalive=.03, queue_timeout=.2, cancel_grace=.08,
                                        api_timeout=1, client_write_timeout=1,
                                        ollama_host=self.url, knowledge_url=self.url + '/knowledge-backend',
                                        max_body_bytes=4096, max_queue_depth=2)
        self.context = GatewayContext(ProjectPaths(self.root), settings=self.settings,
                                      specs=self.specs, discovery=FakeDiscovery())
        self.gateway = create_server(self.context, port=0)
        threading.Thread(target=self.gateway.serve_forever, daemon=True).start()

    def tearDown(self):
        self.state.release.set()
        self.gateway.shutdown()
        self.gateway.server_close()
        self.backend.shutdown()
        self.backend.server_close()
        self.tmp.cleanup()

    def open(self, path='/v1/chat/completions', body=None, headers=None, method=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.gateway.server_port, timeout=2)
        if isinstance(body, dict):
            body = json.dumps(body)
            headers = {'Content-Type': 'application/json', **(headers or {})}
        connection.request(method or ('POST' if body is not None else 'GET'), path, body, headers or {})
        return connection, connection.getresponse()

    def request(self, path='/v1/chat/completions', body=None, headers=None, method=None):
        connection, response = self.open(path, body, headers, method)
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def wait_for(self, condition, timeout=2):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if condition():
                return
            time.sleep(.01)
        self.fail('Condition did not become true')

    def test_unknown_offline_missing_and_wrong_capability(self):
        for body, status in (({'model': 'missing'}, 404), ({'model': 'offline-alias'}, 503),
                             ({}, 400), ({'model': 'friendly-rerank'}, 400)):
            self.assertEqual(self.request(body=body)[0], status)
        self.assertFalse(self.state.requests)

    def test_explicit_default_and_backend_tag_rewrite(self):
        self.context.router.settings = GatewaySettings(defaults={'chat': 'chat'})
        self.assertEqual(self.request(body={'messages': []})[0], 200)
        self.assertEqual(self.request(body={'model': 'friendly-ollama', 'messages': []})[0], 200)
        self.assertEqual(json.loads(self.state.requests[-1][2])['model'], 'tag:latest')

    def test_rerank_and_default_proxy_use_capability(self):
        for path in ('/v1/rerank', '/api/v1/rerank', '/api/rerank/v1/rerank'):
            status, _, _ = self.request(path, {'model': 'friendly-rerank', 'query': 'q', 'documents': ['d']})
            self.assertEqual(status, 200)
            self.assertEqual(self.state.requests[-1][1], '/v1/rerank')
        self.assertEqual(self.context.scheduler.snapshots()[0]['rerank']['active'], 0)

    def test_all_model_proxies_require_auth_monitor_is_read_only(self):
        (self.root / '.api-key').write_text('synthetic-test-key')
        for path in ('/v1/models', '/v1/chat/completions', '/api/chat/health',
                     '/api/v1/chat/completions', '/api/ollama/api/tags'):
            self.assertEqual(self.request(path)[0], 401, path)
        self.assertEqual(self.request('/api/models')[0], 200)
        self.assertEqual(self.request('/api/models', {})[0], 405)
        self.assertEqual(self.request('/api/chat/health', headers={'Authorization': 'Bearer synthetic-test-key'})[0], 200)
        self.assertEqual(len(self.state.requests), 1)

    def test_knowledge_auth_never_replaced_by_model_auth(self):
        (self.root / '.api-key').write_text('synthetic-model-key')
        self.assertEqual(self.request('/knowledge/items', headers={'Authorization': 'Bearer knowledge-fixture'})[0], 200)
        self.assertEqual(self.state.requests[-1][3]['Authorization'], 'Bearer knowledge-fixture')

    def test_knowledge_preserves_multiple_response_cookies(self):
        connection, response = self.open('/knowledge/items')
        self.assertEqual(response.status, 200)
        self.assertEqual([v for k, v in response.getheaders() if k.lower() == 'set-cookie'],
                         ['first=1; Path=/', 'second=2; Path=/'])
        response.read()
        connection.close()

    def test_invalid_json_and_size_limit(self):
        for body in (b'[]', b'not-json', b'{"model":3}', b'{"model":"chat","max_tokens":"x"}', b'{"model":"chat","stream":"yes"}'):
            self.assertEqual(self.request(body=body, headers={'Content-Type': 'application/json'})[0], 400)
        self.assertEqual(self.request(body=b'x' * 4097)[0], 413)
        self.assertFalse(self.state.requests)

    def test_multipart_body_is_preserved_byte_for_byte(self):
        body = b'--boundary\r\nContent-Disposition: form-data; name="model"\r\n\r\nfriendly-asr\r\n--boundary\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\nContent-Type: audio/wav\r\n\r\n\x00\xff\x01binary\r\n--boundary--\r\n'
        status, _, _ = self.request('/v1/audio/transcriptions', body, {'Content-Type': 'multipart/form-data; boundary=boundary'})
        self.assertEqual(status, 200)
        self.assertEqual(self.state.requests[-1][2], body)

    def test_normal_upstream_error_preserves_status(self):
        status, body, _ = self.request(body={'model': 'chat', 'mode': 'error'})
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(body)['error']['message'], 'fake failure')
        self.assertEqual(self.context.scheduler.snapshots()[0]['chat']['active'], 0)

    def test_small_sse_event_is_forwarded_before_upstream_finishes(self):
        connection, response = self.open(body={'model': 'chat', 'stream': True, 'mode': 'stream'})
        try:
            self.assertEqual(response.status, 200)
            received = b''
            until = time.monotonic() + .5
            while b'first' not in received and time.monotonic() < until:
                received += response.read1(4096)
            self.assertIn(b'first', received)
            self.assertFalse(self.state.release.is_set())
            self.state.release.set()
            self.assertIn(b'[DONE]', response.read())
        finally:
            connection.close()
        self.wait_for(lambda: self.context.scheduler.snapshots()[0]['chat']['active'] == 0)

    def test_stream_errors_use_protocol_specific_events(self):
        for endpoint, expected in (('chat/completions', b'data: [DONE]'), ('responses', b'event: error'), ('messages', b'event: error')):
            status, body, _ = self.request('/v1/' + endpoint, {'model': 'chat', 'stream': True, 'mode': 'error'})
            self.assertEqual(status, 200)
            self.assertIn(expected, body)
            self.assertIn(b'fake failure', body)
            if endpoint != 'chat/completions':
                self.assertNotIn(b'[DONE]', body)

    def test_undeclared_responses_endpoint_rejected(self):
        status, _, _ = self.request('/v1/responses', {'model': 'friendly-ollama'})
        self.assertEqual(status, 400)
        self.assertFalse(self.state.requests)

    def test_queue_full_and_timeout_release_admission(self):
        active = self.context.scheduler.submit('chat', 'chat', 1)
        active.acquire()
        queued = self.context.scheduler.submit('chat', 'chat', 1)
        status, _, headers = self.request(body={'model': 'chat'})
        self.assertEqual(status, 429)
        self.assertEqual(headers['Retry-After'], '30')
        queued.release()
        status, _, _ = self.request(body={'model': 'chat'})
        self.assertEqual(status, 504)
        self.assertEqual(self.context.scheduler.snapshots()[0]['chat']['queue_depth'], 1)
        active.release()

    def test_queued_stream_timeout_has_keepalive_and_releases_ticket(self):
        active = self.context.scheduler.submit('chat', 'chat', 1)
        active.acquire()
        status, body, _ = self.request(body={'model': 'chat', 'stream': True})
        self.assertEqual(status, 200)
        self.assertIn(b': keepalive', body)
        self.assertIn(b'timed out', body)
        self.assertEqual(self.context.scheduler.snapshots()[0]['chat']['queue_depth'], 1)
        active.release()

    def test_connection_refusal_releases_slot(self):
        reserve = socket.socket()
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
        self.models['chat'] = Backend('chat', 'friendly-chat', f'http://127.0.0.1:{port}')
        try:
            status, _, _ = self.request(body={'model': 'chat'})
            self.assertIn(status, (502, 504))
            self.wait_for(lambda: self.context.scheduler.snapshots()[0]['chat']['active'] == 0)
        finally:
            reserve.close()

    def test_truncated_upstream_quarantines_slot(self):
        self.request(body={'model': 'chat', 'stream': True, 'mode': 'truncate'})
        self.wait_for(lambda: 'chat' in self.context.scheduler.uncertain)
        self.assertEqual(self.context.scheduler.snapshots()[0]['chat']['active'], 1)
        self.assertEqual(self.request(body={'model': 'chat'})[0], 503)

    def test_disconnect_does_not_release_unfinished_backend(self):
        connection, response = self.open(body={'model': 'chat', 'stream': True, 'mode': 'hold'})
        self.assertTrue(self.state.started.wait(1))
        connection.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        response.close()
        connection.close()
        self.wait_for(lambda: 'chat' in self.context.scheduler.uncertain)
        self.assertEqual(self.context.scheduler.snapshots()[0]['chat']['active'], 1)
        self.assertEqual(self.request(body={'model': 'chat'})[0], 503)

    def test_native_ollama_inference_is_scheduled_on_every_proxy_form(self):
        for path in ('/api/ollama/api/chat', '/api/api/generate'):
            status, _, _ = self.request(path, {'model': 'tag:latest', 'stream': False})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(self.state.requests[-1][2])['model'], 'tag:latest')
        ticket = self.context.scheduler.submit('ollama', 'chat', 1)
        ticket.acquire()
        status, _, _ = self.request('/api/ollama/api/chat', {'model': 'tag:latest', 'stream': False})
        self.assertEqual(status, 504)
        ticket.release()

    def test_native_embedding_and_llama_completion_are_scheduled(self):
        self.models['ollama-embed'] = Backend('ollama-embed', 'embed-tag', self.url,
                                              ('embedding',), 'ollama', 'embed-tag', external=True)
        status, _, _ = self.request('/api/ollama/api/embed', {'model': 'embed-tag', 'input': 'text'})
        self.assertEqual(status, 200)
        self.assertIn('ollama-embed', self.context.scheduler.models)
        status, _, _ = self.request('/api/chat/completion', {'prompt': 'hello'})
        self.assertEqual(status, 200)
        self.assertEqual(self.state.requests[-1][1], '/completion')
        self.assertEqual(self.context.scheduler.snapshots()[0]['chat']['active'], 0)

    def test_path_model_conflict_and_chunked_upload_are_rejected(self):
        self.assertEqual(self.request('/api/chat/v1/chat/completions', {'model': 'friendly-ollama'})[0], 400)
        self.assertEqual(self.request(body=b'fake', headers={'Transfer-Encoding': 'chunked'})[0], 400)
        self.assertFalse(self.state.requests)

    def test_encoded_paths_cannot_bypass_auth_or_quarantined_model(self):
        (self.root / '.api-key').write_text('fixture-gateway-key')
        path = '/%61pi/chat/v1/%63hat/completions'
        self.assertEqual(self.request(path, {'model': 'chat'})[0], 401)
        headers = {'Authorization': 'Bearer fixture-gateway-key'}
        ticket = self.context.scheduler.submit('chat', 'chat', 1)
        ticket.acquire()
        ticket.quarantine('fixture')
        self.assertEqual(self.request(path, {'model': 'chat'}, headers)[0], 503)
        self.assertFalse(self.state.requests)
        self.context.scheduler.recover_model('chat', confirmed_idle=True)
        self.assertEqual(self.request(path, {'model': 'chat'}, headers)[0], 200)
        self.assertEqual(self.state.requests[-1][1], '/v1/chat/completions')
        for path in ('/api/chat/v1/%2563hat/completions', '/api/chat/v1/%zzhat/completions',
                     '/api/chat/../v1/chat/completions', '/api/chat/v1//chat/completions',
                     '/api/chat/v1/%00chat/completions'):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, {'model': 'chat'}, headers)[0], 400)
        self.assertEqual(len(self.state.requests), 1)

    def test_native_aliases_obey_capabilities_declarations_and_scheduling(self):
        ticket = self.context.scheduler.submit('chat', 'chat', 1)
        ticket.acquire()
        ticket.quarantine('fixture')
        for path in ('/api/chat/completion', '/api/chat/infill', '/api/chat/completions'):
            self.assertEqual(self.request(path, {'model': 'chat'})[0], 503)
        for path in ('/api/chat/embedding', '/api/chat/reranking', '/api/chat/v1/reranking'):
            self.assertEqual(self.request(path, {'model': 'chat'})[0], 400)
        self.assertFalse(self.state.requests)
        self.models['native-rerank'] = Backend('native-rerank', 'native-rerank', self.url, ('rerank',), 'llama_cpp')
        for path in ('/api/native-rerank/reranking', '/api/native-rerank/v1/reranking'):
            self.assertEqual(self.request(path, {'query': 'q', 'documents': ['d']})[0], 200)
        self.assertEqual(self.context.scheduler.snapshots()[0]['rerank']['active'], 0)
        self.models['chat-only'] = Backend('chat-only', 'chat-only', self.url, endpoints=('/v1/chat/completions',))
        self.assertEqual(self.request('/api/chat-only/completion', {})[0], 400)
        self.assertEqual(self.request('/api/chat-only/v1/%72esponses', {})[0], 400)

    def test_unknown_backend_writes_and_wrong_methods_are_rejected(self):
        for path in ('/api/chat/unknown', '/api/ollama/api/unknown', '/api/chat/health'):
            self.assertIn(self.request(path, {})[0], (404, 405))
        for method in ('GET', 'HEAD', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'):
            with self.subTest(method=method):
                self.assertEqual(self.request('/api/chat/v1/%63hat/completions', {'model': 'chat'}, method=method)[0], 405)
        self.assertFalse(self.state.requests)
        self.assertEqual(self.request('/api/ollama/api/show', {'model': 'tag:latest'})[0], 200)
        self.assertEqual(self.request('/api/ollama/api/delete', {'model': 'tag:latest'}, method='DELETE')[0], 200)
        self.assertEqual(self.request('/api/chat/health', headers={'Host': 'untrusted.invalid'})[0], 200)
        self.assertNotEqual(self.state.requests[-1][3]['Host'], 'untrusted.invalid')

    def test_backend_key_file_is_separate_from_gateway_client_auth(self):
        from dataclasses import replace
        from types import SimpleNamespace
        from local_llm_deploy.gateway.auth import backend_credentials
        (self.root / '.api-key').write_text('fixture-gateway-key')
        (self.root / 'backend.key').write_text('fixture-backend-key')
        (self.root / 'observed.key').write_text('fixture-observed-key')
        self.context.specs['chat'] = normalize_models({'chat': {'runtime': {'api_key_file': 'backend.key'}}})['chat']
        self.assertEqual(self.request(body={'model': 'chat'}, headers={'Authorization': 'Bearer fixture-backend-key'})[0], 401)
        headers = {'Authorization': 'Bearer fixture-gateway-key'}
        self.assertEqual(self.request(body={'model': 'chat'}, headers=headers)[0], 200)
        self.assertEqual(self.state.requests[-1][3]['Authorization'], 'Bearer fixture-backend-key')
        observed = SimpleNamespace(api_key_file='observed.key', auth_source='file')
        credentials = backend_credentials(self.context.paths, self.context.specs['chat'], observed)
        self.models['chat'] = replace(self.models['chat'], credentials=credentials)
        self.assertEqual(self.request(body={'model': 'chat'}, headers=headers)[0], 200)
        self.assertEqual(self.state.requests[-1][3]['Authorization'], 'Bearer fixture-observed-key')
        (self.root / 'observed.key').unlink()
        self.assertEqual(self.request(body={'model': 'chat'}, headers=headers)[0], 503)
        self.assertEqual(len(self.state.requests), 2)
        self.models.pop('chat')
        self.context.discovery.unavailable = {'chat': 'Backend uses inline API_KEY; restart with API_KEY_FILE'}
        status, body, _ = self.request(body={'model': 'chat'}, headers=headers)
        self.assertEqual(status, 503)
        self.assertIn(b'API_KEY_FILE', body)
        monitor = json.loads(self.request('/api/models')[1])
        self.assertEqual(monitor['unavailable_backends'], self.context.discovery.unavailable)

    def test_disconnect_after_confirmed_backend_completion_releases(self):
        connection, response = self.open(body={'model': 'chat', 'stream': True, 'mode': 'stream'})
        self.assertTrue(self.state.first_sent.wait(1))
        response.read1(4096)
        connection.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        response.close()
        connection.close()
        self.state.release.set()
        self.wait_for(lambda: self.context.scheduler.snapshots()[0]['chat']['active'] == 0)
        self.assertNotIn('chat', self.context.scheduler.uncertain)

    def test_disconnected_queued_request_releases_without_dispatch(self):
        active = self.context.scheduler.submit('chat', 'chat', 1)
        active.acquire()
        connection, response = self.open(body={'model': 'chat', 'stream': True})
        connection.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        response.close()
        connection.close()
        self.wait_for(lambda: self.context.scheduler.snapshots()[0]['chat']['queue_depth'] == 1)
        self.assertFalse(self.state.requests)
        active.release()


class BoundTests(unittest.TestCase):
    def test_failed_worker_start_does_not_leak_reservation(self):
        from local_llm_deploy.gateway.scheduling import Scheduler
        settings = GatewaySettings()
        scheduler = Scheduler(settings)
        ticket = scheduler.submit('chat', 'chat', 10)
        ticket.acquire()
        with patch('threading.Thread.start', side_effect=RuntimeError('cannot start thread')):
            with self.assertRaises(RuntimeError):
                Transport(settings).forward(None, None, 'http://127.0.0.1:1', 'POST', b'{}', {}, ticket=ticket)
        self.assertEqual(scheduler.snapshots()[0]['chat']['active'], 0)

    def test_capture_is_bounded(self):
        capture = BoundedCapture(10)
        for _ in range(5):
            capture.append(b'0123456789')
        self.assertEqual(len(capture.data), 10)
        self.assertTrue(capture.truncated)

    def test_relay_queue_is_bounded(self):
        relay = Relay('http://127.0.0.1:1', 'GET', None, {}, GatewaySettings(stream_buffer_chunks=2), 1)
        relay.events.put(('data', b'x' * 8192))
        relay.events.put(('data', b'x' * 8192))
        self.assertTrue(relay.events.full())
        relay.cancelled.set()
        relay._emit('data', b'more')
        self.assertEqual(relay.events.qsize(), 2)


if __name__ == '__main__':
    unittest.main()
