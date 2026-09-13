"""Model API authentication; knowledge credentials deliberately remain separate."""
from __future__ import annotations

import hmac
import hashlib
import ipaddress
import re
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class BackendAuthError(ValueError):
    pass


@dataclass(frozen=True)
class BackendCredentials:
    key_path: Path = field(repr=False)
    required: bool = False

    def key(self):
        try:
            with self.key_path.open(encoding='utf-8') as stream:
                return stream.readline().strip() or None
        except FileNotFoundError:
            if not self.required:
                return None
        except (OSError, UnicodeError):
            pass
        raise BackendAuthError('Backend API key file is unavailable; configure runtime.api_key_file or restart with API_KEY_FILE')


def backend_credentials(paths, spec=None, observation=None):
    """Resolve references only; never persist an inline secret in runtime state."""
    source = getattr(observation, 'auth_source', None)
    if source == 'inline':
        raise BackendAuthError('Backend uses an inline API_KEY that the gateway cannot recover; restart with API_KEY_FILE')
    observed_file = getattr(observation, 'api_key_file', None)
    runtime = spec.raw.get('runtime', {}) if spec is not None else {}
    selected = observed_file or runtime.get('api_key_file')
    if selected:
        path = Path(selected).expanduser()
        return BackendCredentials(path if path.is_absolute() else paths.root / path, required=True)
    return BackendCredentials(paths.api_key)


class ApiAuth:
    def __init__(self, key_path: Path):
        self.key_path = key_path

    def key(self):
        try:
            with self.key_path.open(encoding='utf-8') as stream:
                return stream.readline().strip() or None
        except FileNotFoundError:
            return None

    def authorized(self, authorization):
        key = self.key()
        if not key:
            return True
        return hmac.compare_digest(str(authorization or '').encode(), f'Bearer {key}'.encode())

    def matches_configured_key(self, authorization):
        """Match a real key only; an unconfigured key is not an auth grant."""
        key = self.key()
        return bool(key and hmac.compare_digest(str(authorization or '').encode(), f'Bearer {key}'.encode()))

    def backend_headers(self, headers, credentials=None):
        result = dict(headers)
        key = credentials.key() if credentials is not None else self.key()
        if key or credentials is not None:
            result = {k: v for k, v in result.items() if k.lower() != 'authorization'}
        if key:
            result['Authorization'] = f'Bearer {key}'
        return result


class ConsoleSessions:
    """Short-lived local browser grants, separate from the shared model API key.

    The real peer, authority and browser origin are checked on every use. These
    grants cannot authorize arbitrary model management or named proxy routes.
    Only token digests are retained, and key rotation invalidates every grant.
    """

    prefix = 'local-console.'

    def __init__(self, auth, *, ttl=8 * 60 * 60, limit=64,
                 clock=time.monotonic, wall_clock=time.time):
        self.auth, self.ttl, self.limit = auth, ttl, limit
        self.clock, self.wall_clock = clock, wall_clock
        self._lock = threading.Lock()
        self._sessions = OrderedDict()

    @staticmethod
    def _header(headers, name):
        values = headers.get_all(name, [])
        return values[0] if len(values) == 1 else None

    @classmethod
    def local_origin(cls, handler, *, issuing=False):
        headers = handler.headers
        # Reverse proxies are intentionally ineligible, even when their final
        # connection to this server comes from a loopback socket.
        if any(name.lower() in ('forwarded', 'via', 'x-real-ip') or
               name.lower().startswith('x-forwarded-') for name in headers):
            return None
        try:
            peer = ipaddress.ip_address(handler.client_address[0])
            if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped:
                peer = peer.ipv4_mapped
            if not peer.is_loopback:
                return None
            authority = cls._header(headers, 'Host')
            if not authority or any(char.isspace() for char in authority):
                return None
            parsed = urlsplit('http://' + authority)
            if (parsed.username is not None or parsed.password is not None or
                    parsed.path or parsed.query or parsed.fragment or '%' in authority):
                return None
            if parsed.hostname != 'localhost' and not ipaddress.ip_address(parsed.hostname).is_loopback:
                return None
            if (80 if parsed.port is None else parsed.port) != handler.server.server_port:
                return None
        except (ValueError, TypeError):
            return None
        if cls._header(headers, 'X-Local-Console') != '1':
            return None
        origin = 'http://' + authority
        origins = headers.get_all('Origin', [])
        if origins:
            if len(origins) != 1 or origins[0] != origin:
                return None
        elif issuing or cls._header(headers, 'Sec-Fetch-Site') != 'same-origin':
            # Same-origin GET/HEAD fetches normally omit Origin. Fetch Metadata
            # plus the required custom header keeps this path browser-only.
            return None
        fetch_sites = headers.get_all('Sec-Fetch-Site', [])
        if fetch_sites and (len(fetch_sites) != 1 or fetch_sites[0] != 'same-origin'):
            return None
        return origin

    @classmethod
    def is_session_authorization(cls, authorization):
        return str(authorization or '').startswith('Bearer ' + cls.prefix)

    def _key_digest(self):
        key = self.auth.key()
        return hashlib.sha256((key or '').encode()).digest()

    def recognizes(self, authorization):
        """Identify an issued grant without reserving independent key names."""
        if not self.is_session_authorization(authorization) or len(authorization) > 135:
            return False
        with self._lock:
            return hashlib.sha256(authorization[7:].encode()).digest() in self._sessions

    def issue(self, origin):
        now, key_digest = self.clock(), self._key_digest()
        token = self.prefix + secrets.token_urlsafe(32)
        expires_at = int((self.wall_clock() + self.ttl) * 1000)
        with self._lock:
            self._prune(now, key_digest)
            while len(self._sessions) >= self.limit:
                self._sessions.popitem(last=False)
            self._sessions[hashlib.sha256(token.encode()).digest()] = (origin, now + self.ttl, key_digest)
        return {'token': token, 'expires_at': expires_at}

    def _prune(self, now, key_digest):
        for digest, (_, expiry, previous_key) in list(self._sessions.items()):
            if expiry <= now or not hmac.compare_digest(previous_key, key_digest):
                del self._sessions[digest]

    def authorized(self, handler, path):
        allowed = ((path == '/monitor-api/v1/snapshot' or path.startswith('/monitor-api/v1/models/')) and
                   handler.command in ('GET', 'HEAD')) or (
                       path == '/v1/chat/completions' and handler.command == 'POST') or (
                       path == '/chat-api/v1/sessions' and handler.command == 'GET') or (
                       re.fullmatch(r'/chat-api/v1/sessions/[A-Za-z0-9][A-Za-z0-9_-]{0,127}', path) is not None
                       and handler.command in ('GET', 'PUT', 'DELETE'))
        if not allowed:
            return False
        origin = self.local_origin(handler)
        authorization = self._header(handler.headers, 'Authorization')
        if not origin or not self.is_session_authorization(authorization):
            return False
        token = authorization[7:]
        if len(token) > 128:
            return False
        try:
            now, key_digest = self.clock(), self._key_digest()
        except (OSError, UnicodeError):
            return False
        with self._lock:
            self._prune(now, key_digest)
            session = self._sessions.get(hashlib.sha256(token.encode()).digest())
        return bool(session and session[0] == origin)
