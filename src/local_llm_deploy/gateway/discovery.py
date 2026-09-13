"""Read-only discovery of managed processes and external HTTP backends."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import socket
import threading
import time
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from .auth import BackendAuthError, backend_credentials


@dataclass(frozen=True)
class Backend:
    key: str
    alias: str
    url: str
    capabilities: tuple = ('chat',)
    backend: str = 'llama_cpp'
    backend_model: str | None = None
    pid: int | None = None
    external: bool = False
    params: dict = field(default_factory=dict)
    endpoints: tuple | None = None
    credentials: object | None = field(default=None, repr=False)

    @property
    def port(self):
        parsed = urlsplit(self.url)
        return parsed.port or (443 if parsed.scheme == 'https' else 80)

    @property
    def ollama(self):
        return self.backend == 'ollama'

    @property
    def names(self):
        return tuple(dict.fromkeys(n for n in (self.key, self.alias, self.backend_model) if n))


def tcp_connect_ok(host, port, timeout=0.2):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class Discovery:
    def __init__(self, paths, specs, settings, *, observe=None, probe=tcp_connect_ok,
                 fetch_json=None, health=None, auth=None, clock=time.monotonic):
        if observe is None:
            from local_llm_deploy.lifecycle.observe import observe_instances
            observe = observe_instances
        from local_llm_deploy.lifecycle.observe import health_ready
        from .auth import ApiAuth
        self.health = health or health_ready
        self.auth = auth or ApiAuth(paths.api_key)
        self.paths, self.specs, self.settings = paths, specs, settings
        self.observe, self.probe, self.clock = observe, probe, clock
        self.fetch_json = fetch_json or self._fetch_json
        self.lock = threading.RLock()
        self._models = None
        self._model_ts = 0
        self._ollama = None
        self._ollama_ts = 0
        self.unavailable = {}

    def update_specs(self, specs):
        with self.lock:
            self.specs = specs
            self._models = None

    def _ollama_credentials(self, spec=None):
        if spec is None or not spec.raw.get('runtime', {}).get('api_key_file'):
            spec = next((candidate for candidate in self.specs.values()
                         if candidate.backend == 'ollama' and candidate.raw.get('runtime', {}).get('api_key_file')), spec)
        return backend_credentials(self.paths, spec)

    def _fetch_json(self, url):
        headers = self.auth.backend_headers({}, self._ollama_credentials())
        with urlopen(Request(url, method='GET', headers=headers), timeout=3) as response:
            # Discovery data is metadata, never an unbounded response.
            data = response.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError('Discovery response too large')
            return json.loads(data)

    def ollama_status(self):
        with self.lock:
            if self._ollama is not None and self.clock() - self._ollama_ts < self.settings.ollama_ttl:
                return self._ollama
            host = self.settings.ollama_host
            result = {'status': 'offline', 'host': host.removeprefix('http://'), 'available': [], 'loaded': []}
            try:
                version = self.fetch_json(host + '/api/version')
                if not isinstance(version, dict):
                    raise ValueError('Invalid Ollama version response')
                result.update(status='running', version=version.get('version', 'unknown'))
            except BackendAuthError as exc:
                result['error'] = str(exc)
                self._ollama, self._ollama_ts = result, self.clock()
                return result
            except (OSError, ValueError, TypeError):
                self._ollama, self._ollama_ts = result, self.clock()
                return result
            for endpoint, output in (('tags', 'available'), ('ps', 'loaded')):
                try:
                    data = self.fetch_json(host + '/api/' + endpoint)
                    if not isinstance(data, dict) or not isinstance(data.get('models', []), list):
                        raise ValueError('Invalid Ollama model response')
                    for model in data.get('models', []):
                        if not isinstance(model, dict):
                            continue
                        details = model.get('details') or {}
                        if not isinstance(details, dict):
                            details = {}
                        item = {'name': model.get('name', ''),
                                'size_gb': round(model.get('size', 0) / 1024**3, 1),
                                'quantization': details.get('quantization_level', ''),
                                'family': details.get('family', ''),
                                'parameter_size': details.get('parameter_size', '')}
                        if output == 'loaded':
                            item['vram_gb'] = round(model.get('size_vram', 0) / 1024**3, 1)
                            item['expires_at'] = model.get('expires_at')
                        result[output].append(item)
                except (OSError, ValueError, TypeError):
                    pass
            self._ollama, self._ollama_ts = result, self.clock()
            return result

    def models(self):
        with self.lock:
            if self._models is not None and self.clock() - self._model_ts < self.settings.discovery_ttl:
                return dict(self._models)
            found = {}
            unavailable = {}
            observed = self.observe(self.paths, self.specs)
            for key, spec in self.specs.items():
                if spec.backend == 'ollama':
                    continue
                item = observed.get(key)
                raw = spec.raw
                external = spec.management == 'external'
                host = spec.host if external else getattr(item, 'host', spec.host)
                host = '127.0.0.1' if host == '0.0.0.0' else ('::1' if host == '::' else host)
                if external:
                    if not self.probe(host, spec.port):
                        continue
                    pid, port = None, spec.port
                elif item is not None and item.status in ('running', 'ready') and item.pid:
                    pid, port = item.pid, item.port
                else:
                    continue
                try:
                    credentials = backend_credentials(self.paths, spec, item if not external else None)
                    headers = self.auth.backend_headers({}, credentials)
                except BackendAuthError as exc:
                    unavailable[key] = str(exc)
                    continue
                health_path = raw.get('health_path') or ('/v1/models' if external else '/health')
                if not self.health(host, port, health_path, timeout=.5,
                                   headers=headers):
                    continue
                authority = f'[{host}]' if ':' in host else host
                found[key] = Backend(key, spec.alias, f'http://{authority}:{port}',
                                     spec.capabilities, spec.backend, raw.get('backend_model'), pid, external,
                                     dict(spec.params), tuple(spec.endpoints), credentials)
            status = self.ollama_status()
            if status.get('error'):
                unavailable.update({key: status['error'] for key, spec in self.specs.items() if spec.backend == 'ollama'})
            available = {m['name'] for m in status.get('available', []) if m.get('name')}
            claimed = set()
            if status.get('status') == 'running':
                for key, spec in self.specs.items():
                    if spec.backend != 'ollama':
                        continue
                    target = spec.backend_model or spec.alias
                    if target not in available:
                        continue
                    try:
                        credentials = self._ollama_credentials(spec)
                        credentials.key()
                    except BackendAuthError as exc:
                        unavailable[key] = str(exc)
                        continue
                    found[key] = Backend(key, spec.alias, self.settings.ollama_host, spec.capabilities,
                                         'ollama', target, external=True, params=dict(spec.params),
                                         endpoints=tuple(spec.endpoints), credentials=credentials)
                    claimed.add(target)
                if self.settings.ollama_auto_discover:
                    for tag in sorted(available - claimed):
                        base = re.sub(r'[^a-zA-Z0-9_-]+', '-', tag).strip('-') or 'ollama-model'
                        key = base
                        n = 2
                        while key in found or key in self.specs:
                            key = f'ollama-{base}-{n}'
                            n += 1
                        found[key] = Backend(key, tag, self.settings.ollama_host, backend='ollama',
                                             backend_model=tag, external=True, credentials=self._ollama_credentials())
            self.unavailable = unavailable
            self._models, self._model_ts = found, self.clock()
            return dict(found)
