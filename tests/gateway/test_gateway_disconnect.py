"""Cancellation regressions using local HTTP sockets and a held fake backend."""
from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import struct
import threading
import time
import unittest

from local_llm_deploy.gateway.scheduling import Scheduler
from local_llm_deploy.gateway.settings import GatewaySettings
from local_llm_deploy.gateway.transport import ResponseWriter, Transport


class FixtureState:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.upstream_complete = threading.Event()
        self.forward_done = threading.Event()
        self.ticket = None
        self.writer = None


class HeldBackend(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        state = self.server.state
        state.started.set()
        state.release.wait(5)
        payload = b'{"ok":true}\n'
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            state.upstream_complete.set()
        except OSError:
            pass
        self.close_connection = True

    def log_message(self, *args):
        pass


class RelayGateway(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        state = self.server.state
        ticket = state.ticket = self.server.scheduler.submit('model', 'chat', 1)
        ticket.acquire()
        writer = state.writer = ResponseWriter(self)
        stream = self.path == '/ollama'
        try:
            if stream:
                writer.start_stream('ollama')
            self.server.transport.forward(
                self, writer, self.server.backend_url, 'POST', body, {},
                ticket=ticket, stream=stream, protocol='ollama' if stream else None)
        finally:
            state.forward_done.set()

    def log_message(self, *args):
        pass


class DisconnectTests(unittest.TestCase):
    def setUp(self):
        self.state = FixtureState()
        self.settings = GatewaySettings(keepalive=.025, cancel_grace=.3, api_timeout=4)
        self.scheduler = Scheduler(self.settings)
        self.backend = ThreadingHTTPServer(('127.0.0.1', 0), HeldBackend)
        self.backend.daemon_threads = True
        self.backend.state = self.state
        self.backend_url = f'http://127.0.0.1:{self.backend.server_port}/inference'
        self.gateway = ThreadingHTTPServer(('127.0.0.1', 0), RelayGateway)
        self.gateway.daemon_threads = True
        self.gateway.state = self.state
        self.gateway.scheduler = self.scheduler
        self.gateway.transport = Transport(self.settings)
        self.gateway.backend_url = self.backend_url
        for server in (self.backend, self.gateway):
            threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True).start()
        self.clients = []

    def tearDown(self):
        self.state.release.set()
        for client in self.clients:
            client.close()
        for server in (self.gateway, self.backend):
            server.shutdown()
            server.server_close()

    def request(self, path):
        client = socket.create_connection(('127.0.0.1', self.gateway.server_port), timeout=2)
        self.clients.append(client)
        client.sendall(('POST ' + path + ' HTTP/1.1\r\nHost: localhost\r\n'
                        'Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}').encode())
        self.assertTrue(self.state.started.wait(1), 'Upstream was not dispatched')
        return client

    def wait_for(self, condition, timeout=1.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.005)
        self.fail('Expected transport state was not reached')

    def reset_client(self, path):
        client = self.request(path)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        client.close()
        self.assertTrue(self.state.forward_done.wait(1), 'Reset was not detected while waiting for upstream')
        self.assertEqual(self.state.writer.outcome, 'cancelled')
        self.assertFalse(self.state.upstream_complete.is_set())
        self.assertEqual(self.state.ticket.state, 'running', 'Reservation released before upstream completion')
        self.assertEqual(self.scheduler.snapshots()[0]['chat']['active'], 1)

    def assert_uncertain_after_reset(self, path):
        self.reset_client(path)
        self.wait_for(lambda: 'model' in self.scheduler.uncertain)
        self.assertEqual(self.state.ticket.state, 'uncertain')
        self.assertEqual(self.scheduler.snapshots()[0]['chat']['active'], 1)
        self.assertFalse(self.state.upstream_complete.is_set())

    def assert_release_after_reset_and_completion(self, path):
        self.reset_client(path)
        self.state.release.set()
        self.wait_for(lambda: self.state.ticket.state == 'released')
        self.assertTrue(self.state.upstream_complete.is_set())
        self.assertNotIn('model', self.scheduler.uncertain)
        self.assertEqual(self.scheduler.snapshots()[0]['chat']['active'], 0)

    def test_nonstream_reset_before_first_response_quarantines(self):
        self.assert_uncertain_after_reset('/nonstream')

    def test_ollama_reset_before_first_upstream_response_quarantines(self):
        self.assert_uncertain_after_reset('/ollama')

    def test_nonstream_reset_releases_after_upstream_eof_within_grace(self):
        self.assert_release_after_reset_and_completion('/nonstream')

    def test_ollama_reset_releases_after_upstream_eof_within_grace(self):
        self.assert_release_after_reset_and_completion('/ollama')

    def test_request_write_half_close_still_receives_response(self):
        client = self.request('/nonstream')
        client.shutdown(socket.SHUT_WR)
        self.assertFalse(self.state.forward_done.wait(.1))
        self.assertEqual(self.state.ticket.state, 'running')
        self.state.release.set()
        response = http.client.HTTPResponse(client)
        response.begin()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b'{"ok":true}\n')
        response.close()
        self.assertEqual(self.state.ticket.state, 'released')
        self.assertNotIn('model', self.scheduler.uncertain)

    def test_transport_without_client_handler_can_wait_and_complete(self):
        class MemoryWriter:
            started = False
            finished = False
            outcome = None

            def __init__(self):
                self.body = bytearray()

            def start(self, status, headers):
                self.started = True
                self.status = status

            def write(self, data):
                self.body.extend(data)

            def finish(self):
                self.finished = True

        writer = MemoryWriter()
        ticket = self.scheduler.submit('model', 'chat', 1)
        ticket.acquire()

        def forward():
            self.gateway.transport.forward(None, writer, self.backend_url, 'POST', b'{}', {}, ticket=ticket)
            self.state.forward_done.set()

        worker = threading.Thread(target=forward, daemon=True)
        worker.start()
        self.assertTrue(self.state.started.wait(1))
        self.assertFalse(self.state.forward_done.wait(.1))
        self.state.release.set()
        self.assertTrue(self.state.forward_done.wait(1))
        self.assertTrue(writer.finished)
        self.assertEqual(writer.status, 200)
        self.assertEqual(writer.body, b'{"ok":true}\n')
        self.assertEqual(ticket.state, 'released')


if __name__ == '__main__':
    unittest.main()
