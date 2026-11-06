"""Validated WebSocket upgrade followed by a bounded, opaque duplex relay."""
from __future__ import annotations

import base64
import hashlib
import http.client
import select
import ssl
import time
from urllib.parse import urlsplit

from .transport import end_to_end_headers


class UnbufferedSocket:
    # HTTPResponse must not read ahead into the first WebSocket frame.
    def __init__(self, sock):
        self.sock = sock

    def makefile(self, mode):
        return self.sock.makefile(mode, buffering=0)


def tunnel(client, upstream, write_timeout):
    sockets = (client, upstream)
    pending = {client: bytearray(), upstream: bytearray()}
    blocked_since = {}
    eof = False
    for sock in sockets:
        sock.setblocking(False)
    while not eof or any(pending.values()):
        reads = [] if eof else [s for s in sockets if len(pending[sockets[1 - sockets.index(s)]]) < 65536]
        writes = [s for s in sockets if pending[s]]
        ready, writable, _ = select.select(reads, writes, [], .25)
        for sock in reads:
            if isinstance(sock, ssl.SSLSocket) and sock.pending() and sock not in ready:
                ready.append(sock)
        for sock in ready:
            try:
                data = sock.recv(16384)
            except (BlockingIOError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
                continue
            if not data:
                eof = True
                continue
            destination = upstream if sock is client else client
            if not pending[destination]:
                blocked_since[destination] = time.monotonic()
            pending[destination].extend(data)
        for sock in writable:
            try:
                sent = sock.send(pending[sock])
            except (BlockingIOError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
                continue
            if sent == 0:
                raise ConnectionResetError('WebSocket peer disconnected')
            del pending[sock][:sent]
            blocked_since[sock] = time.monotonic()
        if any(pending[s] and time.monotonic() - blocked_since[s] > write_timeout for s in sockets):
            raise TimeoutError('WebSocket peer stopped reading')


def forward_websocket(handler, writer, url, headers, settings, timeout):
    if (handler.command != 'GET' or handler.headers.get('Sec-WebSocket-Version') != '13' or
            'upgrade' not in {v.strip().lower() for v in handler.headers.get('Connection', '').split(',')}):
        writer.error(400, 'Invalid WebSocket handshake')
        return
    key = handler.headers.get('Sec-WebSocket-Key', '')
    try:
        if len(base64.b64decode(key, validate=True)) != 16:
            raise ValueError()
    except ValueError:
        writer.error(400, 'Invalid WebSocket key')
        return
    if handler.headers.get('Transfer-Encoding') or handler.headers.get('Content-Length', '0') != '0':
        writer.error(400, 'WebSocket upgrade must not contain a body')
        return
    parsed = urlsplit(url)
    cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
    conn = cls(parsed.hostname, parsed.port, timeout=min(timeout, settings.connect_timeout))
    response = None
    try:
        conn.connect()
        conn.sock.settimeout(timeout)
        outgoing = end_to_end_headers(headers)
        outgoing.update({'Connection': 'Upgrade', 'Upgrade': 'websocket'})
        target = (parsed.path or '/') + ('?' + parsed.query if parsed.query else '')
        conn.request('GET', target, headers=outgoing)
        response = http.client.HTTPResponse(UnbufferedSocket(conn.sock))
        response.begin()
        if response.status != 101:
            writer.start(response.status, response.getheaders())
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                writer.write(chunk)
            writer.finish()
            return
        accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        if (response.getheader('Sec-WebSocket-Accept') != accept or
                response.getheader('Upgrade', '').lower() != 'websocket' or
                'upgrade' not in {v.strip().lower() for v in response.getheader('Connection', '').split(',')}):
            writer.error(502, 'Invalid upstream WebSocket handshake')
            return
        # ResponseWriter intentionally strips hop headers, so emit the upgrade here.
        handler.send_response(101)
        mapped = writer.map_headers(response.getheaders()) if hasattr(writer, 'map_headers') else response.getheaders()
        for name, value in end_to_end_headers(mapped, multi=True):
            handler.send_header(name, value)
        handler.send_header('Connection', 'Upgrade')
        handler.send_header('Upgrade', 'websocket')
        handler.end_headers()
        handler.wfile.flush()
        target_writer = getattr(writer, 'writer', writer)
        target_writer.started, target_writer.status = True, 101
        tunnel(handler.connection, conn.sock, settings.client_write_timeout)
        target_writer.finished, target_writer.outcome = True, 'complete'
    except (OSError, http.client.HTTPException, ValueError) as exc:
        writer.error(504 if isinstance(exc, TimeoutError) else 502, 'WebSocket upstream connection failed')
    finally:
        handler.close_connection = True
        if response:
            response.close()
        conn.close()
