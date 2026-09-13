"""Read-only discovery of managed processes and external HTTP backends."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import copy
import re
import socket
import threading
import time
from urllib.request import Request, build_opener
from urllib.error import HTTPError
from urllib.parse import urlsplit

from .auth import BackendAuthError, backend_credentials


def urlopen(request, *, timeout):
    """Metadata redirects must not forward backend credentials to another host."""
    from .monitor_api import NoRedirect
    return build_opener(NoRedirect).open(request, timeout=timeout)


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
        self._detailed_health = health is None
        self.auth = auth or ApiAuth(paths.api_key)
        self.paths, self.specs, self.settings = paths, specs, settings
        self.observe, self.probe, self.clock = observe, probe, clock
        self.fetch_json = fetch_json or self._fetch_json
        self.lock = threading.RLock()
        self._ready = threading.Condition(self.lock)
        self._models = None
        self._model_ts = 0
        self._ollama = None
        self._ollama_ts = 0
        self.unavailable = {}
        self._refreshing = False
        self._ollama_refreshing = False
        self._observations = {}
        self._availability = {}
        self._generation = 0
        self._published_at = None
        self._ollama_attempt_at = None
        self._ollama_success_at = None
        self._ollama_error = None
        self._model_error = None
        self._probe_cursor = 0
        self._ollama_parts = {}

    def update_specs(self, specs):
        with self.lock:
            if self._models is not None:
                self._models = {key: model for key, model in self._models.items()
                                if (key not in self.specs and key not in specs) or
                                (key in self.specs and key in specs and specs[key].raw == self.specs[key].raw)}
            self.specs = specs
            self._model_ts = float("-inf")
            self._generation += 1

    def _ollama_credentials(self, spec=None):
        if spec is None or not spec.raw.get('runtime', {}).get('api_key_file'):
            spec = next((candidate for candidate in self.specs.values()
                         if candidate.backend == 'ollama' and candidate.raw.get('runtime', {}).get('api_key_file')), spec)
        return backend_credentials(self.paths, spec)

    def _fetch_json(self, url):
        headers = self.auth.backend_headers({}, self._ollama_credentials())
        try:
            response = urlopen(Request(url, method='GET', headers=headers), timeout=1)
        except HTTPError as exc:
            if exc.code in (401, 403):
                exc.close()
                raise BackendAuthError('Ollama rejected backend credentials') from None
            exc.close()
            raise
        with response:
            # Discovery data is metadata, never an unbounded response.
            chunks, size, deadline = [], 0, time.monotonic() + 1
            read = getattr(response, 'read1', response.read)
            while True:
                if time.monotonic() > deadline:
                    raise ValueError('Discovery response deadline exceeded')
                chunk = read(min(16384, 2 * 1024 * 1024 + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > 2 * 1024 * 1024:
                    break
            data = b''.join(chunks)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError('Discovery response too large')
            return json.loads(data)

    def published(self):
        """Copy the last completed discovery; never probe or wait for a collector."""
        with self.lock:
            return {'models': dict(self._models or {}), 'observations': dict(self._observations),
                    'availability': dict(self._availability), 'unavailable': dict(self.unavailable),
                    'ollama': copy.deepcopy(self._ollama), 'published_at': self._published_at,
                    'error': self._model_error,
                    'ollama_attempt_at': self._ollama_attempt_at,
                    'ollama_success_at': self._ollama_success_at,
                    'ollama_error': self._ollama_error, 'ollama_parts': dict(self._ollama_parts)}

    def ollama_status(self):
        with self.lock:
            if self._ollama_refreshing or (self._ollama is not None and
                                           self.clock() - self._ollama_ts < self.settings.ollama_ttl):
                return self._ollama or {'status': 'offline', 'available': [], 'loaded': []}
            self._ollama_refreshing = True
            self._ollama_attempt_at = int(time.time() * 1000)
        try:
            result, error, parts = self._collect_ollama()
            with self.lock:
                self._ollama, self._ollama_ts = result, self.clock()
                self._ollama_error = error
                self._ollama_parts = parts
                if error is None:
                    self._ollama_success_at = int(time.time() * 1000)
            return result
        finally:
            with self.lock:
                self._ollama_refreshing = False

    def _collect_ollama(self):
        host = self.settings.ollama_host
        parts, error, deadline = {}, None, time.monotonic() + 4
        result = {'status': 'offline', 'host': host.removeprefix('http://'), 'available': [], 'loaded': []}
        try:
            version = self.fetch_json(host + '/api/version')
            if not isinstance(version, dict):
                raise ValueError('Invalid Ollama version response')
            result.update(status='running', version=version.get('version', 'unknown'))
            parts['version'] = True
        except BackendAuthError as exc:
            result['error'] = str(exc)
            return result, {'code': 'backend_auth', 'message': 'Ollama credentials are unavailable'}, parts
        except (OSError, ValueError, TypeError):
            error = {'code': 'ollama_unavailable', 'message': 'Ollama version discovery is unavailable'}
        for endpoint, output in (('tags', 'available'), ('ps', 'loaded')):
            try:
                if time.monotonic() >= deadline:
                    raise ValueError('Ollama collection deadline exceeded')
                data = self.fetch_json(host + '/api/' + endpoint)
                if not isinstance(data, dict) or not isinstance(data.get('models', []), list):
                    raise ValueError('Invalid Ollama model response')
                for model in data.get('models', []):
                    if not isinstance(model, dict) or not isinstance(model.get('name'), str) or not model['name']:
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
                parts[endpoint] = True
            except (OSError, ValueError, TypeError):
                error = {'code': 'ollama_partial', 'message': 'Some Ollama metadata is unavailable'}
        return result, error, parts


    def models(self):
        with self._ready:
            if self._refreshing and self._models is None:
                # Only the initial routing snapshot needs a bounded wait. The
                # condition releases the lock; monitor HTTP reads never call this.
                self._ready.wait_for(lambda: not self._refreshing, timeout=10)
            if self._refreshing or (self._models is not None and
                                    self.clock() - self._model_ts < self.settings.discovery_ttl):
                return dict(self._models or {})
            self._refreshing = True
            specs, generation = self.specs, self._generation
        try:
            found, unavailable, observed, availability, error = self._collect_models(specs)
            with self.lock:
                if generation == self._generation:
                    self.unavailable = unavailable
                    self._models, self._model_ts = found, self.clock()
                    self._observations, self._availability = observed, availability
                    self._published_at = int(time.time() * 1000)
                    self._model_error = error
                return dict(self._models or {})
        finally:
            with self.lock:
                self._refreshing = False
                self._ready.notify_all()

    def _collect_models(self, specs):
        with self.lock:
            found = {key: model for key, model in (self._models or {}).items() if model.backend != 'ollama'}
            unavailable = {key: value for key, value in self.unavailable.items()
                           if key in specs and specs[key].backend != 'ollama'}
            availability = {key: value for key, value in self._availability.items() if key in specs}
        error = None
        observed = self.observe(self.paths, specs)
        deadline = time.monotonic() + 5
        candidates = [(key, spec) for key, spec in specs.items() if spec.backend != 'ollama']
        offset = self._probe_cursor % len(candidates) if candidates else 0
        candidates = candidates[offset:] + candidates[:offset]
        for key, spec in candidates:
            if time.monotonic() > deadline:
                error = {'code': 'discovery_budget', 'message': 'Discovery budget exceeded; some models have not been checked'}
                break
            self._probe_cursor += 1
            found.pop(key, None)
            unavailable.pop(key, None)
            availability.pop(key, None)
            item = observed.get(key)
            raw = spec.raw
            external = spec.management == 'external'
            host = spec.host if external else getattr(item, 'host', spec.host)
            host = '127.0.0.1' if host == '0.0.0.0' else ('::1' if host == '::' else host)
            if external:
                if not self.probe(host, spec.port):
                    availability[key] = 'unreachable'
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
                availability[key] = 'unauthorized'
                continue
            health_path = raw.get('health_path') or ('/v1/models' if external else '/health')
            availability[key] = self._health_state(host, port, health_path, headers)
            if availability[key] != 'healthy':
                continue
            authority = f'[{host}]' if ':' in host else host
            found[key] = Backend(key, spec.alias, f'http://{authority}:{port}',
                                 spec.capabilities, spec.backend, raw.get('backend_model'), pid, external,
                                 dict(spec.params), tuple(spec.endpoints), credentials)
        status = self.ollama_status()
        if status.get('error'):
            unavailable.update({key: status['error'] for key, spec in specs.items() if spec.backend == 'ollama'})
        available = {m['name'] for m in status.get('available', []) if m.get('name')}
        claimed = set()
        if status.get('status') == 'running':
            for key, spec in specs.items():
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
                    while key in found or key in specs:
                        key = f'ollama-{base}-{n}'
                        n += 1
                    found[key] = Backend(key, tag, self.settings.ollama_host, backend='ollama',
                                         backend_model=tag, external=True, credentials=self._ollama_credentials())
        return found, unavailable, observed, availability, error

    def _health_state(self, host, port, path, headers):
        if not self._detailed_health:
            return 'healthy' if self.health(host, port, path, timeout=.5, headers=headers) else 'unready'
        from .monitor_api import CollectionError, MonitorAPI, fetch_metadata
        authority = f'[{host}]' if ':' in host else host
        try:
            raw = fetch_metadata(f'http://{authority}:{port}{path}', headers, timeout=.5)
            MonitorAPI._health(raw)
            return 'healthy'
        except CollectionError as exc:
            return {'backend_auth': 'unauthorized', 'backend_unreachable': 'unreachable'}.get(
                exc.issue['code'], 'unready')
