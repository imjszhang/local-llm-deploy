"""Model API authentication; knowledge credentials deliberately remain separate."""
from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from pathlib import Path


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

    def backend_headers(self, headers, credentials=None):
        result = dict(headers)
        key = credentials.key() if credentials is not None else self.key()
        if key or credentials is not None:
            result = {k: v for k, v in result.items() if k.lower() != 'authorization'}
        if key:
            result['Authorization'] = f'Bearer {key}'
        return result
