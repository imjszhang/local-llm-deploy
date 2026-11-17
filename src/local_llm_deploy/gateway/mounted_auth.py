"""Opt-in app-scoped browser grants and authenticated upstream requests."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import ssl
import threading
import time
from http.cookies import SimpleCookie, CookieError
from urllib.parse import urlsplit

from .auth import ConsoleSessions
from .routing import RoutingError

HEADER = 'X-Gateway-App-Authorization'
STATIC = ('/', '/index.html', '/app.js', '/style.css')
SESSION = '/api/v1/gateway-session'
TTL = 12 * 3600


def enabled(app):
    return app.kind == 'http' and app.raw.get('auth') == {'gateway': 'console', 'upstream': 'signed-v1'}


def signing_key(key, app):
    binding = f'js-eyes-mounted-app-v1\n{app.key}\n{app.prefix}\n{app.upstream.rstrip("/")}'
    return hmac.new(key.encode(), binding.encode(), hashlib.sha256).digest()


class MountedAppAuth:
    def __init__(self, auth):
        self.auth = auth
        self.sessions = {}
        self.lock = threading.Lock()

    @staticmethod
    def origin(handler):
        host = handler.headers.get('Host', '')
        if len(handler.headers.get_all('Host', [])) != 1 or any(c.isspace() for c in host):
            raise RoutingError(403, 'Invalid authority')
        scheme = 'https' if isinstance(handler.connection, ssl.SSLSocket) else 'http'
        parsed = urlsplit(f'{scheme}://{host}')
        if not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise RoutingError(403, 'Invalid authority')
        return f'{scheme}://{host}'

    def check_origin(self, handler):
        origin = self.origin(handler)
        origins = handler.headers.get_all('Origin', [])
        if origins and origins != [origin] or handler.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise RoutingError(403, 'Cross-origin app request rejected')
        if handler.command not in ('GET', 'HEAD') and handler.headers.get('X-Gateway-App-Request') != '1' and not handler.headers.get('Authorization'):
            raise RoutingError(403, 'Explicit app request header required')
        return origin

    def credentials(self, handler, app):
        key = self.auth.key()
        if not key:
            raise RoutingError(503, 'Configure a gateway API key before enabling app authentication')
        secret = signing_key(key, app)
        origin = self.check_origin(handler)
        name = 'gateway_app_' + hashlib.sha256(app.key.encode()).hexdigest()[:16]
        try:
            cookie = SimpleCookie(handler.headers.get('Cookie', ''))
            token = cookie[name].value
        except (KeyError, CookieError):
            token = ''
        digest = hashlib.sha256(token.encode()).hexdigest()
        authorization = handler.headers.get('Authorization')
        with self.lock:
            now = time.time()
            self.sessions = {k: v for k, v in self.sessions.items() if v[0] > now}
            session = self.sessions.get(digest)
        authorized = bool(session and session[1:] == (app.key, origin, secret))
        if authorization is not None:
            authorized = len(handler.headers.get_all('Authorization', [])) == 1 and self.auth.matches_configured_key(authorization)
        return secret, origin, name, digest, authorized

    def session(self, handler, writer, app):
        secret, origin, name, digest, authorized = self.credentials(handler, app)
        method = handler.command
        if method not in ('GET', 'POST', 'DELETE'):
            raise RoutingError(405, 'Method not allowed')
        if handler._content_length():
            raise RoutingError(400, 'App session requests must not contain a body')
        cookie = None
        if method == 'POST':
            # Allow direct same-origin LAN access for mounted apps.
            # An explicitly invalid credential never falls back to local trust.
            local = not handler.headers.get('Authorization') and ConsoleSessions.local_origin(handler, issuing=True, allow_lan=True)
            if not authorized and not local:
                raise RoutingError(401, '请使用统一网关凭据登录；自动认证仅支持本机或局域网同源入口')
            token = secrets.token_urlsafe(32)
            with self.lock:
                if len(self.sessions) >= 256:
                    self.sessions.pop(next(iter(self.sessions)))
                self.sessions[hashlib.sha256(token.encode()).hexdigest()] = (time.time() + TTL, app.key, origin, secret)
            cookie = f'{name}={token}; Max-Age={TTL}'
            authorized = True
        elif method == 'DELETE':
            with self.lock:
                self.sessions.pop(digest, None)
            cookie = f'{name}=; Max-Age=0'
            authorized = False
        headers = {'Content-Type': 'application/json', 'Cache-Control': 'no-store'}
        if cookie:
            headers['Set-Cookie'] = cookie + f'; Path={app.prefix}/; HttpOnly; SameSite=Strict' + ('; Secure' if origin.startswith('https:') else '')
        writer.start(200, headers)
        writer.write(json.dumps({'ok': True, 'data': {'mode': 'gateway', 'authenticated': authorized}}).encode())
        writer.finish()

    def headers(self, handler, app, target, body):
        secret, _, _, _, authorized = self.credentials(handler, app)
        route = urlsplit(target).path
        public = handler.command in ('GET', 'HEAD') and route in STATIC
        if not authorized and not public:
            raise RoutingError(401, '请通过统一网关登录')
        # Neither user credentials nor caller-supplied assertions reach upstream.
        headers = {k: v for k, v in handler.headers.items()
                   if k.lower() not in ('authorization', 'cookie', HEADER.lower())}
        if authorized:
            stamp, nonce = str(int(time.time())), secrets.token_hex(16)
            parsed = urlsplit(target)
            path = parsed.path + ('?' + parsed.query if parsed.query else '')
            message = '\n'.join(('v1', app.key, app.prefix, handler.command, path, stamp, nonce,
                                 hashlib.sha256(body or b'').hexdigest()))
            signature = hmac.new(secret, message.encode(), hashlib.sha256).hexdigest()
            headers[HEADER] = f'v1.{stamp}.{nonce}.{signature}'
        return headers
