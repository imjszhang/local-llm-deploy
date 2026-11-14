"""Browser credentials and response URL mapping for mounted HTTP services."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import ssl
import time
from http.cookies import SimpleCookie, CookieError
from urllib.parse import urlsplit

COOKIE_PREFIX = 'gateway_service_'
TTL = 12 * 3600


def cookie_name(spec):
    return COOKIE_PREFIX + hashlib.sha256(spec.key.encode()).hexdigest()[:16]


def same_origin(handler):
    origin = handler.headers.get('Origin')
    if handler.headers.get('Sec-Fetch-Site') == 'cross-site':
        return False
    if not origin:
        return True
    parsed = urlsplit(origin)
    return parsed.scheme in ('http', 'https') and parsed.netloc == handler.headers.get('Host')


def _single_header(headers, name):
    values = headers.get_all(name, [])
    return values[0] if len(values) == 1 else None


def local_browser_navigation(handler):
    """A person opening a service from this machine's own gateway page."""
    headers = handler.headers
    if handler.command not in ('GET', 'HEAD'):
        return False
    if any(name.lower() in ('forwarded', 'via', 'x-real-ip') or name.lower().startswith('x-forwarded-')
           for name in headers):
        return False
    try:
        peer = ipaddress.ip_address(handler.client_address[0])
        if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped:
            peer = peer.ipv4_mapped
        if not peer.is_loopback:
            return False
        host = _single_header(headers, 'Host')
        if not host or any(char.isspace() for char in host) or '%' in host:
            return False
        parsed = urlsplit('//' + host)
        if (parsed.username is not None or parsed.password is not None or parsed.path or
                parsed.query or parsed.fragment):
            return False
        if parsed.hostname != 'localhost' and not ipaddress.ip_address(parsed.hostname).is_loopback:
            return False
        if (80 if parsed.port is None else parsed.port) != handler.server.server_port:
            return False
    except (ValueError, TypeError):
        return False
    if _single_header(headers, 'Sec-Fetch-Mode') != 'navigate' or _single_header(headers, 'Sec-Fetch-Dest') != 'document':
        return False
    return (_single_header(headers, 'Sec-Fetch-Site') in ('same-origin', 'none') and
            same_origin(handler))


def browser_authorized(handler, spec):
    if not spec.raw.get('browser') or not same_origin(handler):
        return False
    if local_browser_navigation(handler):
        return True
    key = handler.context.auth.key()
    if not key:
        return True
    authorization = handler.headers.get('Authorization', '')
    if authorization.startswith('Basic '):
        try:
            decoded = base64.b64decode(authorization[6:], validate=True).decode()
            _, password = decoded.split(':', 1)
            return hmac.compare_digest(password.encode(), key.encode())
        except (ValueError, UnicodeError):
            return False
    # Explicit invalid credentials must not fall back to a browser session.
    if authorization:
        return False
    try:
        cookies = SimpleCookie(handler.headers.get('Cookie', ''))
        token = cookies[cookie_name(spec)].value
        expiry, nonce, signature = token.split('.')
        if not time.time() < int(expiry) <= time.time() + TTL:
            return False
        message = f'{spec.key}:{expiry}:{nonce}'.encode()
        expected = hmac.new(key.encode(), message, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except (KeyError, ValueError, CookieError):
        return False


def bearer_is_gateway_key(handler):
    authorization = handler.headers.get('Authorization', '')
    if not authorization.startswith('Bearer '):
        return False
    token = authorization[7:]
    if token.startswith('local-console.'):
        return False
    key = handler.context.auth.key()
    if not key:
        return False
    return hmac.compare_digest(hashlib.sha256(token.encode()).digest(), hashlib.sha256(key.encode()).digest())


def session_cookie(handler, spec):
    key = handler.context.auth.key()
    if not key:
        return None
    expiry, nonce = str(int(time.time()) + TTL), secrets.token_hex(16)
    signature = hmac.new(key.encode(), f'{spec.key}:{expiry}:{nonce}'.encode(), hashlib.sha256).hexdigest()
    secure = '; Secure' if isinstance(handler.connection, ssl.SSLSocket) else ''
    return (f'{cookie_name(spec)}={expiry}.{nonce}.{signature}; Path={spec.prefix}/; '
            f'HttpOnly; SameSite=Strict; Max-Age={TTL}{secure}')


def upstream_headers(handler, spec):
    headers = dict(handler.headers)
    for name in list(headers):
        if name.lower() == 'authorization' and (spec.auth_upstream in ('none', 'bearer') or
                                                (spec.raw.get('browser') and headers[name].startswith('Basic '))):
            del headers[name]
        elif name.lower() == 'cookie':
            # Never expose gateway browser grants to an upstream service.
            value = '; '.join(item.strip() for item in headers[name].split(';')
                              if not item.strip().split('=', 1)[0].startswith(COOKIE_PREFIX))
            if value:
                headers[name] = value
            else:
                del headers[name]
    if spec.auth_upstream == 'bearer':
        headers['Authorization'] = 'Bearer ' + spec.raw['auth']['bearer']
    # Browser upstreams compare Origin with the rewritten upstream Host.
    if spec.raw.get('browser') and same_origin(handler):
        for name in list(headers):
            if name.lower() == 'origin':
                parsed = urlsplit(spec.upstream)
                headers[name] = f'{parsed.scheme}://{parsed.netloc}'
            elif name.lower() == 'sec-fetch-site':
                headers[name] = 'same-origin'
    return headers


class ServiceWriter:
    """Keep redirects and upstream cookies within the mounted service prefix."""
    def __init__(self, writer, spec, cookie=None):
        self.writer, self.spec, self.cookie = writer, spec, cookie

    def __getattr__(self, name):
        return getattr(self.writer, name)

    def map_headers(self, headers):
        pairs = headers.items() if hasattr(headers, 'items') else headers
        result = []
        for name, value in pairs:
            if self.spec.strip_prefix and name.lower() == 'location':
                upstream = self.spec.upstream.rstrip('/')
                parsed = urlsplit(upstream)
                bases = (upstream, '//' + parsed.netloc + parsed.path)
                for upstream_base in bases:
                    if value == upstream_base or value.startswith((upstream_base + '/', upstream_base + '?', upstream_base + '#')):
                        suffix = value[len(upstream_base):]
                        value = self.spec.prefix + (suffix if suffix.startswith('/') else '/' + suffix)
                        break
                else:
                    if value.startswith('/') and not value.startswith('//'):
                        value = self.spec.prefix + value
            elif self.spec.strip_prefix and name.lower() == 'set-cookie':
                try:
                    cookies = SimpleCookie(value)
                    for item in cookies.values():
                        if item.key.startswith(COOKIE_PREFIX):
                            continue
                        item['path'] = self.spec.prefix + (item['path'] or '/')
                        item['domain'] = ''
                        result.append((name, item.OutputString()))
                    continue
                except CookieError:
                    continue
            result.append((name, value))
        if self.cookie:
            result.append(('Set-Cookie', self.cookie))
        return result

    def start(self, status, headers=None):
        pairs = list(headers.items()) if hasattr(headers, 'items') else list(headers or [])
        lengths = [value for name, value in pairs if name.lower() == 'content-length']
        transfer = any(name.lower() == 'transfer-encoding' for name, _ in pairs)
        length = None
        if (len(lengths) == 1 and lengths[0].isascii() and lengths[0].isdigit() and
                not transfer and status >= 200 and status != 204):
            length = int(lengths[0])
        self.writer.start(status, self.map_headers(pairs), content_length=length)


def asset_redirect(handler, proxies):
    """A mounted service page may call site-root URLs such as /assets or /play.

    Redirect those requests back under the service prefix when the Referer is that
    same-origin page. Authentication happens at the destination. Gateway API
    prefixes are dispatched before this fallback, and an existing gateway file
    is never replaced by the redirect.
    """
    if handler.command not in ('GET', 'HEAD', 'POST'):
        return None
    path = urlsplit(handler.path).path
    if not path.startswith('/') or path.startswith('//') or path == '/':
        return None
    referrer = urlsplit(handler.headers.get('Referer', ''))
    if referrer.scheme not in ('http', 'https') or referrer.netloc != handler.headers.get('Host'):
        return None
    for spec in proxies.values():
        prefix = spec.prefix
        if spec.raw.get('browser') and (referrer.path == prefix or referrer.path.startswith(prefix + '/')):
            if path == prefix or path.startswith(prefix + '/'):
                return None
            return prefix + handler.path
    return None
