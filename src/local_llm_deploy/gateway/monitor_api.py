"""Bounded, shared read-only monitor collectors and the versioned display DTO.

Request handlers only read published data and enqueue at most one refresh per
resource. Network and subprocess work never runs under the publication lock.
"""
from __future__ import annotations

import copy
import http.client
import json
import math
import queue
import re
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .auth import BackendAuthError, backend_credentials
from .monitoring import collect_system_info
from .routing import RoutingError

MAX_RESPONSE = 512 * 1024
MAX_OUTPUT = 8192
MAX_SLOTS = 64
MAX_METRICS = 64


def monitor_target(request_path):
    parsed = urlsplit(request_path)
    try:
        query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=16)
    except ValueError:
        raise RoutingError(400, 'Invalid monitor query parameters') from None
    if parsed.path == '/monitor-api/v1/snapshot':
        if query:
            raise RoutingError(400, 'Snapshot does not accept query parameters')
        return None, False
    if (parsed.path.startswith('/monitor-api/v1/models/') and parsed.path.count('/') == 4
            and parsed.path.rsplit('/', 1)[1]):
        if set(query) - {'include_output'} or query.get('include_output', ['0']) not in (['0'], ['1']):
            raise RoutingError(400, 'Invalid monitor detail parameters')
        return parsed.path.rsplit('/', 1)[1], query.get('include_output') == ['1']
    raise RoutingError(404, 'Unknown monitor endpoint')


def finite(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None


def text(value, limit=256):
    return value[:limit] if isinstance(value, str) else None


def issue(code, message):
    return {'code': code, 'message': message}


class CollectionError(Exception):
    def __init__(self, code, message):
        self.issue = issue(code, message)
        super().__init__(message)


class NoRedirect(HTTPRedirectHandler):
    # A configured backend may redirect. Never forward its credentials elsewhere.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_metadata(url, headers, timeout=1.5):
    request = Request(url, method='GET', headers=headers)
    try:
        with build_opener(NoRedirect).open(request, timeout=timeout) as response:
            chunks, size, deadline = [], 0, time.monotonic() + timeout
            read = getattr(response, 'read1', response.read)
            while True:
                if time.monotonic() > deadline:
                    raise CollectionError('backend_unreachable', 'Backend monitoring deadline exceeded')
                chunk = read(min(16384, MAX_RESPONSE + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE:
                    break
            data = b''.join(chunks)
            if len(data) > MAX_RESPONSE:
                raise CollectionError('response_too_large', 'Backend metadata exceeds the size limit')
            return data
    except HTTPError as exc:
        status = exc.code
        exc.close()
        if status in (401, 403):
            raise CollectionError('backend_auth', 'Backend rejected monitoring credentials') from None
        if status in (404, 405, 501):
            raise CollectionError('unsupported', 'Backend does not provide this monitoring endpoint') from None
        raise CollectionError('backend_unready', 'Backend monitoring endpoint is not ready') from None
    except (OSError, URLError, TimeoutError, http.client.HTTPException):
        raise CollectionError('backend_unreachable', 'Backend monitoring endpoint could not be reached') from None


def collect_system():
    data = collect_system_info({}, strict=True)
    return {'cpu': {k: finite(data['cpu'].get(k)) for k in ('user', 'sys', 'idle')},
            'memory': {k: finite(data['memory'].get(k)) for k in
                       ('total_gb', 'used_gb', 'free_gb', 'wired_gb')},
            'load_avg': [finite(n) for n in data.get('load_avg', [])[:3]]}


def collect_process(pid):
    result = subprocess.run(['ps', '-p', str(pid), '-o', '%cpu=', '-o', 'rss='],
                            capture_output=True, text=True, timeout=2)
    values = result.stdout.strip().split()
    if result.returncode or len(values) != 2:
        raise CollectionError('process_unavailable', 'Owned process readings are unavailable')
    try:
        cpu, rss = finite(float(values[0])), finite(float(values[1]))
    except ValueError:
        raise CollectionError('process_unavailable', 'Owned process readings are unavailable') from None
    return {'cpu_pct': cpu, 'rss_gb': rss / 1024**2 if rss is not None else None}


def _section(state='loading', message=None, **values):
    return {'state': state, 'message': message, **values}


def _blank_detail(key, support, now, ollama=False):
    return {'schema_version': 1, 'key': key, 'generated_at': now,
            'health': _section('loading' if support['health'] else 'unsupported'),
            'metrics': _section('loading' if support['metrics'] else 'unsupported', values=[]),
            'slots': _section('loading' if support['slots'] else 'unsupported', items=[]),
            'process': _section('loading' if support['process_stats'] else 'unsupported',
                                cpu_pct=None, rss_gb=None),
            'ollama': _section('loading' if ollama else 'unsupported', loaded=None, size_gb=None, vram_gb=None,
                               quantization=None, expires_at=None, version=None)}


class MonitorAPI:
    def __init__(self, context, *, system_collector=collect_system, process_collector=collect_process,
                 fetch=fetch_metadata, clock=time.monotonic, wall_clock=time.time, workers=3,
                 system_ttl=5, discovery_ttl=10, detail_ttl=3, max_details=128, identity_validator=None):
        self.context = context
        from local_llm_deploy.lifecycle.observe import same_identity
        self.identity_validator = identity_validator or same_identity
        self.system_collector, self.process_collector, self.fetch = system_collector, process_collector, fetch
        self.clock, self.wall_clock = clock, wall_clock
        self.system_ttl, self.discovery_ttl, self.detail_ttl = system_ttl, discovery_ttl, detail_ttl
        self.max_details = max_details
        self.lock = threading.RLock()
        self.cache = {}
        self.inflight = set()
        self.jobs = queue.Queue(maxsize=16)
        self.closed = False
        self.threads = []
        self.workers = workers
        self._remembered_ollama = {}

    def now(self):
        return int(self.wall_clock() * 1000)

    def close(self):
        # Daemon collectors have bounded I/O and need not hold server shutdown.
        with self.lock:
            self.closed = True
            self.cache.clear()

    def _worker(self):
        while True:
            try:
                job = self.jobs.get(timeout=.25)
            except queue.Empty:
                if self.closed:
                    return
                continue
            key, collector = job
            try:
                if not self.closed:
                    self._collect(key, collector)
            finally:
                with self.lock:
                    self.inflight.discard(key)
                self.jobs.task_done()
            if self.closed:
                return

    def _collect(self, key, collector):
        try:
            data = collector()
            error = data.pop('_error', None) if isinstance(data, dict) else None
            success_at = data.pop('_success_at', self.now()) if isinstance(data, dict) else self.now()
        except CollectionError as exc:
            data, error = None, exc.issue
        except Exception:
            # Never return raw exceptions: backend responses, filesystem paths,
            # config values and credentials can appear in exception messages.
            data, error = None, issue('collection_failed', 'Monitoring data could not be collected')
        with self.lock:
            if self.closed:
                return
            previous = self.cache[key]
            if data is not None:
                if isinstance(key, tuple) and previous['data']:
                    for section in ('health', 'metrics', 'slots', 'process', 'ollama'):
                        current, old = data[section], previous['data'][section]
                        if current['state'] == 'error' and old['state'] in ('ready', 'stale'):
                            data[section] = {**old, 'state': 'stale', 'message': current['message']}
                previous['data'] = data
            if error is None:
                previous['success'] = success_at
            previous['error'] = error
            previous['completed'] = self.clock()

    def _request(self, key, ttl, collector):
        with self.lock:
            entry = self.cache.setdefault(key, {'data': None, 'attempt': None, 'success': None,
                                                'error': None, 'completed': float('-inf')})
            if (self.closed or key in self.inflight or
                    self.clock() - entry['completed'] < ttl):
                return
            if not self.threads:
                for number in range(self.workers):
                    thread = threading.Thread(target=self._worker, name=f'monitor-{number}', daemon=True)
                    self.threads.append(thread)
                    thread.start()
            try:
                self.jobs.put_nowait((key, collector))
            except queue.Full:
                return
            entry['attempt'] = self.now()
            self.inflight.add(key)

    def _source(self, key, ttl):
        entry = self.cache.get(key, {})
        success = entry.get('success')
        error = entry.get('error')
        threshold = int(ttl * 3000)
        return {'last_attempt_at': entry.get('attempt'), 'last_success_at': success,
                'stale_after_ms': threshold,
                'stale': bool(error or (success is not None and self.now() - success > threshold)),
                'error': error}

    def _catalog(self):
        self.context.refresh()
        _, state = self.context.catalog_snapshot()
        return {'_error': state['error']}

    def _discovery(self):
        discovery = self.context.discovery
        discovery.models()
        if hasattr(discovery, 'published'):
            published = discovery.published()
        else:
            # Small injected providers used by embedders/tests need no special API.
            published = {'models': discovery.models(), 'ollama': discovery.ollama_status(),
                         'observations': {}, 'availability': {}, 'unavailable': {},
                         'published_at': self.now()}
        error = published.get('error') or published.get('ollama_error')
        if published.get('published_at') is None:
            error = issue('discovery_pending', 'Initial discovery has not completed')
        published['_success_at'] = published.get('published_at')
        published['_error'] = error
        return published

    def snapshot(self):
        self._request('system', self.system_ttl, self.system_collector)
        self._request('catalog', 5, self._catalog)
        self._request('discovery', self.discovery_ttl, self._discovery)
        specs, catalog_state = self.context.catalog_snapshot()
        lanes, budgets = self.context.scheduler.snapshots()
        with self.lock:
            system = copy.deepcopy(self.cache.get('system', {}).get('data'))
            published = self.cache.get('discovery', {}).get('data') or {}
            sources = {'system': self._source('system', self.system_ttl),
                       'catalog': catalog_state,
                       'discovery': self._source('discovery', self.discovery_ttl)}
            rows = self._models(specs, published, budgets)
            diagnostics = []
            for source, status in sources.items():
                if status['error']:
                    diagnostics.append({'code': source + '_' + status['error']['code'], 'severity': 'warning',
                                        'message': status['error']['message'], 'model_key': None})
            for row in rows:
                if row['activity']['uncertain']:
                    diagnostics.append({'code': 'completion_uncertain', 'severity': 'error',
                                        'message': 'Completion is uncertain; confirm backend idle before recovery',
                                        'model_key': row['key']})
                if row['availability']['state'] in ('unauthorized', 'unreachable', 'unready'):
                    diagnostics.append({'code': row['availability']['state'], 'severity': 'warning',
                                        'message': row['availability']['reason'], 'model_key': row['key']})
            generated = self.now()
            return {'schema_version': 1, 'snapshot_id': str(generated), 'generated_at': generated,
                    'sources': sources, 'system': system, 'lanes': lanes, 'models': rows,
                    'diagnostics': diagnostics}

    def _models(self, specs, published, budgets):
        discovered = dict(published.get('models') or {})
        observations = published.get('observations') or {}
        availability = published.get('availability') or {}
        unavailable = published.get('unavailable') or {}
        ollama = published.get('ollama') or {}
        for item in ollama.get('available', []):
            if isinstance(item, dict) and isinstance(item.get('name'), str):
                self._remembered_ollama[item['name']] = item
        # Retain the last successful tags only while discovery is failing. A
        # successful empty list really means the model was removed.
        if ollama.get('status') == 'running' and not published.get('ollama_error'):
            self._remembered_ollama = {item['name']: item for item in ollama.get('available', [])
                                       if isinstance(item, dict) and isinstance(item.get('name'), str)}
        identities = {key: (spec, discovered.get(key)) for key, spec in specs.items()}
        identities.update({key: (None, model) for key, model in discovered.items() if key not in specs})
        claimed = {getattr(model, 'backend_model', None) or getattr(spec, 'backend_model', None) or
                   getattr(spec, 'alias', None) for spec, model in identities.values()
                   if getattr(spec or model, 'backend', None) == 'ollama'}
        for name in sorted(set(self._remembered_ollama) - claimed):
            base = re.sub(r'[^a-zA-Z0-9_-]+', '-', name).strip('-') or 'ollama-model'
            key, counter = base, 2
            while key in identities:
                key, counter = f'ollama-{base}-{counter}', counter + 1
            from .discovery import Backend
            identities[key] = (None, Backend(key, name, self.context.settings.ollama_host,
                                            backend='ollama', backend_model=name, external=True))
        loaded = {m.get('name') for m in ollama.get('loaded', []) if isinstance(m, dict)}
        result = []
        for key, (spec, model) in identities.items():
            info = spec or model
            backend = info.backend
            observation = observations.get(key)
            management = getattr(observation, 'management', None) or getattr(spec, 'management', None)
            management = management if management in ('process', 'launchd', 'external') else 'process'
            if spec is None or backend == 'ollama':
                management = 'external'
            state, reason = 'unknown', 'No owned process observation is available'
            if management == 'external':
                state, reason = 'unmanaged', 'Lifecycle is managed outside this gateway'
            elif observation:
                state = {'ready': 'running', 'running': 'running', 'starting': 'starting',
                         'stale': 'stale', 'stopped': 'stopped'}.get(observation.status, 'unknown')
                reason = 'Process record could not be verified' if state == 'stale' else None
            elif key in discovered and model.pid:
                state, reason = 'running', None
            status = availability.get(key, 'unknown')
            if key in discovered:
                status = 'healthy'
            elif key in unavailable:
                status = 'unauthorized'
            elif backend == 'ollama' and published.get('published_at'):
                status = 'unready' if ollama.get('status') == 'running' else 'unreachable'
                if (published.get('ollama_error') or {}).get('code') == 'backend_auth':
                    status = 'unauthorized'
            messages = {'healthy': None, 'unknown': 'Availability has not been confirmed',
                        'unauthorized': 'Backend monitoring credentials are unavailable or rejected',
                        'unready': 'Backend has not reported ready',
                        'unreachable': 'Backend could not be reached'}
            budget = budgets.get(key) or {}
            uncertain = bool(budget.get('uncertain'))
            routable = key in discovered if published.get('published_at') else None
            if uncertain:
                routable = False
            support = {'health': True, 'metrics': backend == 'llama_cpp', 'slots': backend == 'llama_cpp',
                       'process_stats': bool(observation and observation.pid) or bool(model and model.pid),
                       'output': backend == 'llama_cpp'}
            target = getattr(info, 'backend_model', None) or (info.alias if backend == 'ollama' else None)
            result.append({'key': key, 'alias': info.alias, 'backend': backend, 'backend_model': target,
                           'capabilities': list(info.capabilities), 'registered': spec is not None,
                           'management': management, 'port': getattr(observation, 'port', None) or info.port,
                           'lifecycle': {'state': state, 'reason': reason},
                           'availability': {'state': status, 'reason': messages[status]},
                           'routing': {'available': routable,
                                       'reason': 'Backend completion is uncertain' if uncertain else
                                       (None if routable else 'No ready route is published')},
                           'activity': {'active': budget.get('active_slots', 0), 'waiting': budget.get('waiting', 0),
                                        'uncertain': uncertain, 'scope': 'gateway'},
                           'monitoring_support': support,
                           'budget': {'used': budget.get('used', 0), 'total': budget.get('total', 0)}
                           if 'chat' in info.capabilities and budget else None,
                           'loaded': target in loaded if backend == 'ollama' and ollama.get('status') == 'running'
                           and not published.get('ollama_error') else None,
                           'endpoint': '/api/' + quote(key, safe='') + '/v1/'})
        return sorted(result, key=lambda row: row['key'])

    def detail(self, key, include_output=False):
        snapshot = self.snapshot()
        row = next((row for row in snapshot['models'] if row['key'] == key), None)
        if row is None:
            raise RoutingError(404, 'Unknown monitor model')
        cache_key = ('detail', key, include_output)
        with self.lock:
            # Bound per-model caches even if a registry repeatedly changes keys.
            detail_keys = [name for name in self.cache if isinstance(name, tuple)]
            if cache_key not in self.cache and len(detail_keys) >= self.max_details:
                candidates = [name for name in detail_keys if name not in self.inflight]
                if candidates:
                    oldest = min(candidates, key=lambda name: self.cache[name]['completed'])
                    self.cache.pop(oldest, None)
        self._request(cache_key, self.detail_ttl, lambda: self._detail(row, include_output))
        with self.lock:
            entry = self.cache[cache_key]
            data = copy.deepcopy(entry['data']) or _blank_detail(key, row['monitoring_support'], self.now(), row['backend'] == 'ollama')
            data['source'] = copy.deepcopy(self._source(cache_key, self.detail_ttl))
            if data['source']['stale'] and not data['source']['error']:
                for section in ('health', 'metrics', 'slots', 'process', 'ollama'):
                    if data[section]['state'] == 'ready':
                        data[section]['state'] = 'stale'
            if entry['error'] and entry['data'] is None:
                for section in ('health', 'metrics', 'slots', 'process'):
                    if data[section]['state'] == 'loading':
                        data[section].update(state='error', message=entry['error']['message'])
            return data

    def _detail(self, row, include_output):
        specs, _ = self.context.catalog_snapshot()
        spec = specs.get(row['key'])
        with self.lock:
            published = self.cache.get('discovery', {}).get('data') or {}
            model = (published.get('models') or {}).get(row['key'])
            observation = (published.get('observations') or {}).get(row['key'])
            ollama = copy.deepcopy(published.get('ollama') or {})
            ollama_parts = published.get('ollama_parts') or {}
        data = _blank_detail(row['key'], row['monitoring_support'], self.now(), row['backend'] == 'ollama')
        errors = []
        if row['backend'] == 'ollama':
            state = 'ready' if ollama.get('status') == 'running' else 'error'
            message = None if state == 'ready' else 'Ollama metadata is unavailable'
            loaded = next((m for m in ollama.get('loaded', []) if m.get('name') == row['backend_model']), None)
            available = next((m for m in ollama.get('available', []) if m.get('name') == row['backend_model']), {})
            info = loaded or available
            data['health'] = _section(state, message)
            metadata_ready = state == 'ready' or bool(ollama_parts.get('ps') or ollama_parts.get('tags'))
            data['ollama'] = _section('ready' if metadata_ready else state, None if metadata_ready else message,
                                      version=text(ollama.get('version')),
                                      loaded=bool(loaded) if state == 'ready' or ollama_parts.get('ps') else None,
                                      size_gb=finite(info.get('size_gb')), vram_gb=finite(info.get('vram_gb')),
                                      quantization=text(info.get('quantization')), expires_at=text(info.get('expires_at')))
            if state != 'ready':
                data['_error'] = issue('ollama_unavailable', message)
            return data
        try:
            credentials = getattr(model, 'credentials', None) or backend_credentials(self.context.paths, spec, observation)
            headers = self.context.auth.backend_headers({}, credentials)
        except BackendAuthError:
            for name in ('health', 'metrics', 'slots'):
                if data[name]['state'] != 'unsupported':
                    data[name].update(state='unauthorized', message='Backend monitoring credentials are unavailable')
            data['_error'] = issue('backend_auth', 'Backend monitoring credentials are unavailable')
            return data
        if model:
            base = model.url
        elif spec:
            host = getattr(observation, 'host', None) or spec.host
            host = '127.0.0.1' if host == '0.0.0.0' else ('::1' if host == '::' else host)
            port = getattr(observation, 'port', None) or spec.port
            base = 'http://' + (f'[{host}]' if ':' in host else host) + ':' + str(port)
        else:
            raise CollectionError('model_unavailable', 'Model discovery is unavailable')
        health_path = (spec.raw.get('health_path') if spec else None) or (
            '/v1/models' if row['management'] == 'external' else '/health')
        deadline = time.monotonic() + 5
        for name, endpoint, parser in (('health', health_path, self._health),
                                       ('metrics', '/metrics', self._metrics),
                                       ('slots', '/slots', lambda value: self._slots(value, include_output))):
            if data[name]['state'] == 'unsupported':
                continue
            try:
                if time.monotonic() >= deadline:
                    raise CollectionError('detail_budget', 'Monitoring collection budget exceeded')
                values = parser(self.fetch(base + endpoint, headers))
                data[name].update(state='ready', message=None, **values)
            except CollectionError as exc:
                state = {'unsupported': 'unsupported', 'backend_auth': 'unauthorized'}.get(exc.issue['code'], 'error')
                data[name].update(state=state, message=exc.issue['message'])
                if state != 'unsupported':
                    errors.append(exc.issue)
            except (ValueError, TypeError, UnicodeError, OverflowError):
                data[name].update(state='error', message='Backend returned invalid monitoring data')
                errors.append(issue('invalid_metadata', 'Backend returned invalid monitoring data'))
        pid = getattr(observation, 'pid', None) or getattr(model, 'pid', None)
        if pid:
            try:
                identity = getattr(observation, 'identity', None)
                if identity is None or not self.identity_validator(identity):
                    raise ValueError('Process identity changed')
                values = self.process_collector(pid)
                if not self.identity_validator(identity):
                    raise ValueError('Process identity changed during collection')
                data['process'].update(state='ready', cpu_pct=finite(values.get('cpu_pct')),
                                       rss_gb=finite(values.get('rss_gb')))
            except Exception:
                data['process'].update(state='error', message='Owned process readings are unavailable')
                errors.append(issue('process_unavailable', 'Owned process readings are unavailable'))
        if errors:
            data['_error'] = errors[0]
        return data

    @staticmethod
    def _health(raw):
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            # Successful health endpoints may legitimately contain plain text.
            return {}
        if isinstance(value, dict) and (value.get('status') in ('loading', 'starting', 'error', 'unhealthy')
                                        or value.get('error')):
            raise CollectionError('backend_unready', 'Backend has not reported ready')
        return {}

    @staticmethod
    def _metrics(raw):
        values = []
        for line in raw.decode('utf-8').splitlines():
            match = re.fullmatch(r'([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^\n]*\})?\s+(\S+)(?:\s+\S+)?', line)
            if not match:
                continue
            try:
                value = finite(float(match.group(2)))
            except ValueError:
                continue
            name = match.group(1)
            # Labels are intentionally omitted: they may contain model paths or text.
            unit = 'tokens/s' if name.rsplit(':', 1)[-1] in (
                'prompt_tokens_seconds', 'predicted_tokens_seconds', 'tokens_per_second') else (
                'seconds' if name.endswith(('_seconds', '_seconds_total')) else (
                'bytes' if name.endswith(('_bytes', '_bytes_total')) else 'count'))
            values.append({'name': name[:128], 'value': value, 'unit': unit})
            if len(values) >= MAX_METRICS:
                break
        if not values:
            raise CollectionError('invalid_metadata', 'Backend returned no supported metrics')
        return {'values': values}

    @staticmethod
    def _slots(raw, include_output):
        slots = json.loads(raw)
        if not isinstance(slots, list):
            raise ValueError('Invalid slots')
        result = []
        for value in slots[:MAX_SLOTS]:
            if not isinstance(value, dict):
                continue
            output = value.get('generated_text') or value.get('generated') or value.get('output') or value.get('content')
            reasoning = value.get('reasoning_content') or value.get('reasoning')
            next_token = value.get('next_token')
            if isinstance(next_token, list):
                next_token = next_token[0] if next_token else {}
            next_token = next_token if isinstance(next_token, dict) else {}
            item = {'id': text(value.get('id'), 64) if isinstance(value.get('id'), str) else value.get('id') if type(value.get('id')) is int else len(result),
                    'busy': bool(value.get('is_processing')),
                    'decoded': finite(next_token.get('n_decoded', value.get('n_decoded'))),
                    'limit': finite((value.get('params') or {}).get('n_predict')) if
                    isinstance(value.get('params'), dict) else finite(value.get('n_predict')),
                    'prompt_tokens': finite(value.get('n_prompt_tokens'))}
            if include_output:
                item['generated'], item['reasoning'] = text(output, MAX_OUTPUT) or '', text(reasoning, MAX_OUTPUT) or ''
            result.append(item)
        return {'items': result}
