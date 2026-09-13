"""HTTP composition root. Routing, scheduling, discovery and relay live separately."""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import threading
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from local_llm_deploy.config import load_specs, project_paths
from local_llm_deploy.observability import AccessLogger, log
from .auth import ApiAuth, BackendAuthError, backend_credentials
from .discovery import Discovery
from .knowledge import is_knowledge_path, knowledge_backend_url, knowledge_redirect_location
from .monitoring import Monitoring
from .monitor_api import MonitorAPI, monitor_target
from .routing import Router, RoutingError, normalize_proxy_path
from .scheduling import Scheduler, QueueFull, BackendUncertain, estimate_kv_tokens
from .settings import GatewaySettings
from .transport import ResponseWriter, Transport, _client_disconnected


class GatewayContext:
    def __init__(self, paths=None, *, settings=None, specs=None, discovery=None,
                 scheduler=None, transport=None, auth=None, monitoring=None,
                 spec_loader=None, registry_ttl=30, clock=time.monotonic, monitor_api=None):
        self.paths = paths or project_paths()
        self.settings = settings or GatewaySettings.from_env()
        # Registry refresh is atomic; runtime scheduling limits remain owned
        # by this process until restart.
        self._catalog_lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._registry_attempt_at = self._registry_success_at = int(time.time() * 1000)
        self._registry_error = None
        self._spec_loader = spec_loader or (None if specs is not None else load_specs)
        self._registry_ttl, self._clock = registry_ttl, clock
        self._loaded_at = clock()
        self.specs = specs if specs is not None else load_specs(self.paths.registry)
        self.discovery = discovery or Discovery(self.paths, self.specs, self.settings)
        self.scheduler = scheduler or Scheduler(self.settings)
        self.scheduler.register_models(self.specs)
        self.router = Router(self.specs, self.settings)
        self.transport = transport or Transport(self.settings)
        self.auth = auth or ApiAuth(self.paths.api_key)
        self.monitoring = monitoring or Monitoring(self.discovery, self.scheduler, self.settings)
        self.monitor_api = monitor_api or MonitorAPI(self)
        self.logger = AccessLogger(self.settings.access_log, capture_bytes=self.settings.capture_bytes,
                                   log_body=self.settings.log_body)

    def catalog_snapshot(self):
        with self._catalog_lock:
            now = int(time.time() * 1000)
            return dict(self.specs), {
                'last_attempt_at': self._registry_attempt_at,
                'last_success_at': self._registry_success_at,
                'stale_after_ms': max(90000, int(self._registry_ttl * 3000)),
                'stale': bool(self._registry_error or (self._spec_loader is not None and
                              now - self._registry_success_at > max(90000, self._registry_ttl * 3000))),
                'error': self._registry_error}

    def refresh(self):
        if self._spec_loader is None or self._clock() - self._loaded_at < self._registry_ttl:
            return
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            if self._clock() - self._loaded_at < self._registry_ttl:
                return
            self._loaded_at = self._clock()
            with self._catalog_lock:
                self._registry_attempt_at = int(time.time() * 1000)
            try:
                candidate = self._spec_loader(self.paths.registry)
            except (OSError, ValueError):
                with self._catalog_lock:
                    self._registry_error = {'code': 'registry_invalid',
                                            'message': 'Registry refresh failed; last validated configuration is retained'}
                log.warning('Registry refresh failed; keeping last validated configuration')
                return
            router = Router(candidate, self.settings)
            with self._catalog_lock:
                self.scheduler.register_models(candidate)
                if hasattr(self.discovery, 'update_specs'):
                    self.discovery.update_specs(candidate)
                self.specs, self.router = candidate, router
                self._registry_success_at = int(time.time() * 1000)
                self._registry_error = None
        finally:
            self._refresh_lock.release()

    def resolve(self, path, method, body, headers):
        self.refresh()
        with self._catalog_lock:
            router = self.router
        models = self.discovery.models()
        unavailable = dict(getattr(self.discovery, 'unavailable', {}))
        return router.resolve(path, method, body, headers, models, unavailable)


class ProxyHandler(SimpleHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def __init__(self, *args, context=None, **kwargs):
        self.context = context
        super().__init__(*args, directory=str(context.paths.static), **kwargs)

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            self.close_connection = True

    def do_GET(self):
        self.dispatch()

    def do_HEAD(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def do_PUT(self):
        self.dispatch()

    def do_PATCH(self):
        self.dispatch()

    def do_DELETE(self):
        self.dispatch()

    def do_OPTIONS(self):
        self.dispatch()

    def do_TRACE(self):
        self.dispatch()

    def do_CONNECT(self):
        self.dispatch()

    def send_response(self, code, message=None):
        self._response_status = code
        super().send_response(code, message)

    def end_headers(self):
        cache_control = getattr(self, '_static_cache_control', None)
        if cache_control and getattr(self, '_response_status', None) in (200, 304):
            self.send_header('Cache-Control', cache_control)
        super().end_headers()

    def log_message(self, fmt, *args):
        # Never log the raw URL query or request/authorization headers.
        log.debug('HTTP %s %s', self.command, urlsplit(self.path).path)

    def handle_expect_100(self):
        # Validate credentials and size before accepting an upload, including
        # Expect: 100-continue clients. The regular dispatcher repeats checks.
        writer = ResponseWriter(self)
        try:
            path = urlsplit(self.path if is_knowledge_path(self.path) else normalize_proxy_path(self.path)).path
            if (path.startswith(('/api/', '/v1/', '/monitor-api/')) or path == '/monitor-api') and path not in ('/api/models', '/api/system'):
                if not self.context.auth.authorized(self.headers.get('Authorization')):
                    self.close_connection = True
                    writer.error(401, 'Invalid API key', code='invalid_request_error')
                    return False
            if path == '/monitor-api' or path.startswith('/monitor-api/'):
                if self.command not in ('GET', 'HEAD'):
                    raise RoutingError(405, 'Read-only endpoint')
                if self._content_length():
                    raise RoutingError(400, 'Monitor requests must not contain a body')
                monitor_target(normalize_proxy_path(self.path))
            self._content_length()
        except RoutingError as exc:
            self.close_connection = True
            writer.error(exc.status, str(exc))
            return False
        return super().handle_expect_100()

    def _content_length(self):
        if self.headers.get('Transfer-Encoding'):
            raise RoutingError(400, 'Chunked request bodies are not supported')
        values = self.headers.get_all('Content-Length', [])
        if len(values) > 1:
            raise RoutingError(400, 'Duplicate Content-Length')
        try:
            length = int(values[0]) if values else 0
        except ValueError:
            raise RoutingError(400, 'Invalid Content-Length') from None
        if length < 0:
            raise RoutingError(400, 'Invalid Content-Length')
        if length > self.context.settings.max_body_bytes:
            raise RoutingError(413, 'Request body exceeds configured size limit')
        return length

    def _body(self):
        length = self._content_length()
        body = self.rfile.read(length) if length else None
        if length and len(body) != length:
            raise RoutingError(400, 'Incomplete request body')
        return body

    def _json(self, writer, payload):
        writer.start(200, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'})
        writer.write(json.dumps(payload, ensure_ascii=False).encode())
        writer.finish()

    def dispatch(self):
        ctx = self.context
        self._static_cache_control = None
        writer = ResponseWriter(self)
        self.connection.settimeout(ctx.settings.client_write_timeout)
        path = urlsplit(self.path).path
        try:
            if is_knowledge_path(self.path):
                if path == '/knowledge':
                    writer.start(301, {'Location': knowledge_redirect_location(self.path)})
                    writer.finish()
                    return
                body = self._body()
                ctx.transport.forward(self, writer, knowledge_backend_url(self.path, ctx.settings.knowledge_url),
                                      self.command, body, dict(self.headers), timeout=ctx.settings.knowledge_timeout)
                return
            request_path = normalize_proxy_path(self.path)
            path = urlsplit(request_path).path
            if path == '/monitor-api' or path.startswith('/monitor-api/'):
                if not ctx.auth.authorized(self.headers.get('Authorization')):
                    self.close_connection = True
                    writer.error(401, 'Invalid API key', code='invalid_request_error')
                    return
                if self.command not in ('GET', 'HEAD'):
                    raise RoutingError(405, 'Read-only endpoint')
                if self._content_length():
                    raise RoutingError(400, 'Monitor requests must not contain a body')
                key, include_output = monitor_target(request_path)
                self._json(writer, ctx.monitor_api.snapshot() if key is None else ctx.monitor_api.detail(key, include_output))
                return
            if not path.startswith(('/api/', '/v1/')):
                if path in ('/', '/index.html', '/monitor.html'):
                    self._static_cache_control = 'no-cache'
                elif re.fullmatch(r'/monitor-assets/[A-Za-z0-9_-]+-[A-Za-z0-9_-]{8,}\.(?:js|css|svg|png|webp|woff2?)', path):
                    self._static_cache_control = 'public, max-age=31536000, immutable'
                if self.command == 'GET':
                    super().do_GET()
                elif self.command == 'HEAD':
                    super().do_HEAD()
                else:
                    writer.error(405, 'Method not allowed')
                return
            ctx.refresh()
            # The only unauthenticated API routes are these two exact, read-only
            # monitor endpoints. Every default, named and Ollama proxy is gated.
            if path in ('/api/models', '/api/system'):
                if self.command not in ('GET', 'HEAD'):
                    raise RoutingError(405, 'Read-only endpoint')
                self._json(writer, ctx.monitoring.models() if path == '/api/models' else ctx.monitoring.system())
                return
            if not ctx.auth.authorized(self.headers.get('Authorization')):
                self.close_connection = True
                writer.error(401, 'Invalid API key', code='invalid_request_error')
                return
            if path.rstrip('/') == '/v1/models':
                if self.command not in ('GET', 'HEAD'):
                    raise RoutingError(405, 'Read-only endpoint')
                self._json(writer, ctx.monitoring.openai_models())
                return
            body = self._body()
            route = ctx.resolve(request_path, self.command, body, self.headers)
            self._execute(writer, route)
        except RoutingError as exc:
            self.close_connection = True
            writer.error(exc.status, str(exc), code=exc.code)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception:
            self.close_connection = True
            log.exception('Gateway request failed')
            try:
                writer.error(500, 'Gateway request failed')
            except OSError:
                pass

    def _execute(self, writer, route):
        ctx = self.context
        ticket = None
        handed_off = False
        started = time.monotonic()
        request_id = uuid.uuid4().hex
        capture = None
        try:
            if route.capability:
                tokens = estimate_kv_tokens(route.body, route.backend.params, ctx.settings.chars_per_token) if route.capability == 'chat' else 0
                ticket = ctx.scheduler.submit(route.backend.key, route.capability, tokens,
                                              priority=int(route.stream), params=route.backend.params)
                if route.stream:
                    writer.start_stream(route.protocol)
                deadline = started + ctx.settings.queue_timeout
                while not ticket.acquire(timeout=min(ctx.settings.keepalive, max(0, deadline - time.monotonic()))):
                    if time.monotonic() >= deadline:
                        writer.error(504, 'Inference queue wait timed out', protocol=route.protocol if route.stream else None)
                        return
                    if route.stream and route.protocol != 'ollama':
                        writer.write(b': keepalive\n\n')
                    elif self._client_disconnected():
                        writer.outcome = 'cancelled'
                        return
            credentials = route.backend.credentials
            if credentials is None:
                credentials = backend_credentials(ctx.paths, ctx.specs.get(route.backend.key))
            headers = ctx.auth.backend_headers(dict(self.headers), credentials)
            headers['X-Request-ID'] = request_id
            timeout = ctx.settings.monitor_timeout if urlsplit(route.url).path.rstrip('/').split('/')[-1] in ('health', 'metrics', 'slots') else ctx.settings.api_timeout
            handed_off = True
            capture = ctx.transport.forward(self, writer, route.url, self.command, route.body, headers,
                                            ticket=ticket, stream=route.stream, protocol=route.protocol,
                                            timeout=timeout, capture=bool(ctx.settings.access_log and ctx.settings.log_body))
        except QueueFull as exc:
            writer.start(429, {'Content-Type': 'application/json', 'Retry-After': '30'})
            writer.write(json.dumps({'error': {'message': str(exc), 'type': 'server_error'}}).encode())
            writer.finish()
        except BackendUncertain:
            writer.error(503, 'Backend completion is uncertain; confirm idle or restart backend then gateway',
                         protocol=route.protocol if route.stream else None)
        except BackendAuthError as exc:
            writer.error(503, str(exc), protocol=route.protocol if route.stream else None)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            writer.outcome = 'cancelled'
            raise
        except ValueError as exc:
            writer.error(400, str(exc), code='invalid_request_error')
        finally:
            if ticket and not handed_off:
                ticket.release()
            ctx.logger.write(request_id=request_id, path=self.path, method=self.command,
                             model=route.backend.key, capability=route.capability,
                             elapsed=time.monotonic() - started, status=writer.status,
                             body=route.body, response=capture, outcome=writer.outcome)

    def _client_disconnected(self):
        return _client_disconnected(self)


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, context):
        def handler(*args, **kwargs):
            return ProxyHandler(*args, context=context, **kwargs)
        self.context = context
        super().__init__(address, handler)

    def server_close(self):
        self.context.monitor_api.close()
        super().server_close()


def create_server(context, host='127.0.0.1', port=8888):
    return GatewayServer((host, port), context)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local model API gateway')
    parser.add_argument('--project-root')
    parser.add_argument('--host', default=os.environ.get('UI_HOST', '0.0.0.0'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('UI_PORT', 8888)))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='[gateway] %(message)s')
    ctx = GatewayContext(project_paths(args.project_root))
    with create_server(ctx, args.host, args.port) as server:
        log.info('Listening on http://%s:%s/monitor.html; scheduling config takes effect on restart', args.host, server.server_port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
