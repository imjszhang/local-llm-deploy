"""Bounded HTTP relay, protocol-aware SSE errors and explicit cancellation ownership."""
from __future__ import annotations

import http.client
import json
import queue
import select
import socket
import threading
import time
from urllib.parse import urlsplit

from local_llm_deploy.observability import BoundedCapture, log

HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'content-length'}


def end_to_end_headers(headers, *, multi=False):
    pairs = list(headers.items()) if hasattr(headers, 'items') else list(headers)
    connection_tokens = set()
    for name, value in pairs:
        if name.lower() == 'connection':
            connection_tokens.update(token.strip().lower() for token in value.split(','))
    filtered = [(k, v) for k, v in pairs if k.lower() not in HOP_HEADERS | connection_tokens]
    return filtered if multi else dict(filtered)


def stream_error(protocol, message, code='server_error'):
    error = {'message': message, 'type': code}
    if protocol == 'messages':
        return ('event: error\ndata: ' + json.dumps({'type': 'error', 'error': error}) + '\n\n').encode()
    if protocol == 'responses':
        return ('event: error\ndata: ' + json.dumps({'type': 'error', 'code': code, 'message': message}) + '\n\n').encode()
    if protocol == 'ollama':
        return (json.dumps({'error': message}) + '\n').encode()
    return ('data: ' + json.dumps({'error': error}) + '\n\ndata: [DONE]\n\n').encode()


def _client_disconnected(handler):
    connection = getattr(handler, 'connection', None)
    if connection is None:
        return False
    try:
        readable, _, _ = select.select([connection], [], [], 0)
        if readable:
            # Do not consume a pipelined request. An empty peek is only a FIN:
            # clients may half-close their request side and still read our
            # response. Only a reset/error proves the response is unwanted.
            connection.recv(1, socket.MSG_PEEK)
    except (BlockingIOError, InterruptedError, socket.timeout):
        return False
    except (OSError, ValueError):
        return True
    except (AttributeError, TypeError):
        # Transport is also used with writers that have no real client socket.
        return False
    return False


class ResponseWriter:
    def __init__(self, handler):
        self.handler = handler
        self.started = False
        self.finished = False
        self.status = None
        self.chunked = False
        self.outcome = None

    def start(self, status, headers=None):
        if self.started:
            return
        self.started, self.status = True, status
        self.handler.send_response(status)
        for name, value in end_to_end_headers(headers or {}, multi=True):
            self.handler.send_header(name, value)
        self.chunked = self.handler.command != 'HEAD' and status not in (204, 304) and status >= 200
        if self.chunked:
            self.handler.send_header('Transfer-Encoding', 'chunked')
        self.handler.end_headers()

    def start_stream(self, protocol):
        content_type = 'application/x-ndjson' if protocol == 'ollama' else 'text/event-stream'
        self.start(200, {'Content-Type': content_type, 'Cache-Control': 'no-cache'})

    def write(self, data):
        if not data or self.handler.command == 'HEAD' or not self.chunked:
            return
        self.handler.wfile.write(f'{len(data):x}\r\n'.encode() + data + b'\r\n')
        self.handler.wfile.flush()

    def finish(self):
        if self.finished:
            return
        self.finished = True
        if self.outcome is None:
            self.outcome = 'failed' if self.status and self.status >= 400 else 'complete'
        if self.chunked:
            self.handler.wfile.write(b'0\r\n\r\n')
            self.handler.wfile.flush()

    def error(self, status, message, *, protocol=None, code='server_error'):
        self.outcome = 'failed'
        if self.started:
            if protocol:
                self.write(stream_error(protocol, message, code))
            else:
                # A partial ordinary response cannot become a successful,
                # cleanly terminated response after an upstream failure.
                self.handler.close_connection = True
                return
        else:
            self.start(status, {'Content-Type': 'application/json'})
            self.write(json.dumps({'error': {'message': message, 'type': code}}).encode())
        self.finish()


class Relay:
    """The worker owns the upstream connection; only fixed-size chunks are queued.

    On downstream cancellation the worker drains into a discard sink briefly.
    EOF confirms completion. If that cannot be confirmed, close the connection
    and keep the scheduler reservation quarantined instead of overcommitting.
    """
    def __init__(self, url, method, body, headers, settings, timeout):
        self.url, self.method, self.body = url, method, body
        self.headers = end_to_end_headers(headers)
        self.settings, self.timeout = settings, timeout
        self.events = queue.Queue(maxsize=settings.stream_buffer_chunks)
        self.cancelled = threading.Event()
        self.finished = threading.Event()
        self.connection = None
        self.socket = None
        self.outcome = None
        self.dispatched = False
        self.worker = threading.Thread(target=self._read, name='gateway-upstream', daemon=True)

    def start(self):
        self.worker.start()
        return self

    def _emit(self, kind, value=None):
        while not self.cancelled.is_set():
            try:
                self.events.put((kind, value), timeout=.1)
                return
            except queue.Full:
                pass

    def _read(self):
        try:
            parsed = urlsplit(self.url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                raise ValueError('Invalid backend URL')
            cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
            conn = self.connection = cls(parsed.hostname, parsed.port, timeout=min(self.timeout, self.settings.connect_timeout))
            # Separating connect from dispatch distinguishes a refused connection
            # from a request whose model execution may already have started.
            conn.connect()
            self.socket = conn.sock
            if self.cancelled.is_set():
                self.outcome = 'not_dispatched'
                return
            self.socket.settimeout(self.timeout)
            self.dispatched = True
            target = (parsed.path or '/') + ('?' + parsed.query if parsed.query else '')
            conn.request(self.method, target, body=self.body, headers=self.headers)
            response = conn.getresponse()
            self._emit('headers', (response.status, response.getheaders()))
            while True:
                chunk = response.read1(8192)
                if not chunk:
                    break
                self._emit('data', chunk)
            if response.length not in (None, 0):
                raise http.client.IncompleteRead(b'')
            self.outcome = 'complete'
            self._emit('end')
        except (OSError, http.client.HTTPException, ValueError) as exc:
            self.outcome = 'uncertain' if self.dispatched else 'not_dispatched'
            status = 504 if isinstance(exc, (TimeoutError, socket.timeout)) else 502
            # Raw exception text may contain backend URLs or credentials.
            self._emit('error', (status, 'Backend timeout' if status == 504 else 'Backend connection failed'))
        finally:
            if self.connection:
                self.connection.close()
            self.finished.set()

    def abandon(self, ticket=None):
        self.cancelled.set()
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                break

        def cleanup():
            completed = self.finished.wait(self.settings.cancel_grace)
            if completed and self.outcome in ('complete', 'not_dispatched'):
                if ticket:
                    ticket.release()
            else:
                if ticket:
                    ticket.quarantine('Upstream cancellation could not be confirmed')
                conn = self.connection
                if conn:
                    # Interrupt a blocked socket read; close alone may leave the
                    # HTTPResponse file wrapper retaining the socket.
                    sock = self.socket
                    if sock:
                        try:
                            sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                    conn.close()
                log.warning('Gateway backend completion uncertain; confirm idle or restart backend then gateway')
        threading.Thread(target=cleanup, name='gateway-cancel-cleanup', daemon=True).start()


class Transport:
    def __init__(self, settings):
        self.settings = settings

    def forward(self, handler, writer, url, method, body, headers, *, ticket=None,
                stream=False, protocol=None, timeout=None, capture=False):
        """Always consumes ownership of ticket, including failed downstream writes."""
        try:
            relay = Relay(url, method, body, headers, self.settings, timeout or self.settings.api_timeout).start()
        except BaseException:
            if ticket:
                ticket.release()
            raise
        recorded = BoundedCapture(self.settings.capture_bytes if capture else 0)
        upstream_error = None
        error_body = BoundedCapture(16384)
        try:
            while True:
                try:
                    kind, value = relay.events.get(timeout=self.settings.keepalive)
                except queue.Empty:
                    if _client_disconnected(handler):
                        raise ConnectionResetError('Client disconnected')
                    if stream and protocol != 'ollama':
                        writer.write(b': keepalive\n\n')
                    continue
                if kind == 'headers':
                    status, response_headers = value
                    if writer.started and stream and status >= 400:
                        upstream_error = status
                    else:
                        writer.start(status, response_headers)
                elif kind == 'data':
                    if upstream_error:
                        error_body.append(value)
                    else:
                        writer.write(value)
                        if capture:
                            recorded.append(value)
                elif kind == 'error':
                    status, message = value
                    if ticket:
                        if relay.outcome == 'not_dispatched':
                            ticket.release()
                        else:
                            ticket.quarantine('Upstream transport failed after request dispatch')
                    writer.error(status, message, protocol=protocol if stream else None)
                    return recorded
                elif kind == 'end':
                    # EOF is confirmed. Publish resource state before sending
                    # the downstream terminator so a completed request has no
                    # transient reservation visible to its next request.
                    if ticket:
                        ticket.release()
                    if upstream_error:
                        message = f'Backend error {upstream_error}'
                        try:
                            detail = json.loads(error_body.data)
                            error = detail.get('error', {})
                            message = error.get('message', message) if isinstance(error, dict) else str(error)
                        except (ValueError, TypeError):
                            pass
                        writer.error(upstream_error, message, protocol=protocol)
                    else:
                        writer.finish()
                    return recorded
        except (BrokenPipeError, ConnectionResetError, OSError):
            # The cleanup worker takes ownership; handler must not release again.
            writer.outcome = 'cancelled'
            relay.abandon(ticket)
            if handler is not None:
                handler.close_connection = True
            return recorded
        except BaseException:
            relay.abandon(ticket)
            raise
