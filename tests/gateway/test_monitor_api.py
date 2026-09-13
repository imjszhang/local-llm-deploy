"""Versioned monitor contracts use temporary credentials and fake backends only."""
from __future__ import annotations

import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from local_llm_deploy.config import ProjectPaths, normalize_models
from local_llm_deploy.gateway.app import GatewayContext, create_server
from local_llm_deploy.gateway.discovery import Backend, Discovery
from local_llm_deploy.gateway.monitor_api import CollectionError, MonitorAPI, collect_system, fetch_metadata
from local_llm_deploy.gateway.settings import GatewaySettings

SYSTEM = {'cpu': {'user': 0, 'sys': 3, 'idle': 97},
          'memory': {'total_gb': 64, 'used_gb': 12, 'free_gb': 52, 'wired_gb': 2}, 'load_avg': [1, 2, 3]}


class FakeDiscovery:
    def __init__(self, specs):
        self.specs, self.calls = specs, 0
        self.models_value = {'chat': Backend('chat', 'Chat fixture', 'http://127.0.0.1:9001', pid=123),
                             'embed': Backend('embed', 'Embedding', 'http://127.0.0.1:9002',
                                              capabilities=('embedding',), backend='transformers_embedding', pid=234)}
        self.value = {'models': self.models_value,
                      'ollama': {'status': 'running', 'version': 'fixture', 'available': [{'name': 'tag:latest'}], 'loaded': []},
                      'observations': {'chat': SimpleNamespace(status='running', pid=123, port=9001, host='127.0.0.1', identity=SimpleNamespace(pid=123)),
                                       'embed': SimpleNamespace(status='ready', pid=234, port=9002, host='127.0.0.1', identity=SimpleNamespace(pid=234)),
                                       'stale': SimpleNamespace(status='stale', pid=None, port=9003)},
                      'availability': {'chat': 'healthy', 'embed': 'healthy'},
                      'unavailable': {}, 'published_at': 1_800_000_000_000}

    def models(self):
        self.calls += 1
        return dict(self.models_value)

    def published(self):
        return copy.deepcopy(self.value)

    def ollama_status(self):
        return copy.deepcopy(self.value['ollama'])


class MonitorFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name))
        self.paths.static.mkdir()
        (self.paths.static / 'monitor.html').write_text('monitor fixture')
        self.specs = normalize_models({'chat': {'alias': 'Chat fixture'}, 'embed': {'type': 'embedding'},
                                      'offline': {}, 'stale': {},
                                      'ollama': {'type': 'ollama', 'ollama_model': 'tag:latest'}})
        self.discovery = FakeDiscovery(self.specs)
        self.context = GatewayContext(self.paths, specs=self.specs, discovery=self.discovery)
        self.context.monitor_api.close()
        self.clock = [100.]
        self.fetches = []
        def fetch(url, headers):
            self.fetches.append(url)
            if url.endswith('/metrics'):
                return b'llamacpp:tokens_predicted_total 12\nllamacpp:nan NaN\n'
            if url.endswith('/slots'):
                return json.dumps([{'id': 0, 'is_processing': True, 'next_token': [{'n_decoded': 7}],
                                    'n_prompt_tokens': 20, 'params': {'n_predict': 100},
                                    'generated': '<script>fixture</script>' + 'x' * 9000}]).encode()
            return b'{"status":"ok"}'
        self.monitor = MonitorAPI(self.context, system_collector=lambda: copy.deepcopy(SYSTEM), fetch=fetch,
                                  process_collector=lambda pid: {'cpu_pct': 0, 'rss_gb': .5}, identity_validator=lambda identity: True,
                                  clock=lambda: self.clock[0], wall_clock=lambda: 1_800_000_000 + self.clock[0])
        self.context.monitor_api = self.monitor
        self.addCleanup(self.monitor.close)

    def settle(self):
        deadline = time.monotonic() + 3
        while self.monitor.jobs.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(self.monitor.jobs.unfinished_tasks, 0, 'Collector did not finish')

    def snapshot(self):
        self.monitor.snapshot()
        self.settle()
        return self.monitor.snapshot()

    def detail(self, key, output=False):
        self.monitor.detail(key, output)
        self.settle()
        return self.monitor.detail(key, output)


class MonitorContractTests(MonitorFixture):
    def test_full_catalog_identity_lifecycle_availability_and_scoped_activity(self):
        first = self.monitor.snapshot()
        self.assertEqual({m['key'] for m in first['models']}, set(self.specs))
        data = self.snapshot()
        rows = {m['key']: m for m in data['models']}
        self.assertEqual(data['schema_version'], 1)
        self.assertGreater(data['generated_at'], 1_000_000_000_000)
        self.assertEqual(set(data['lanes']), {'chat', 'embed', 'rerank', 'asr'})
        self.assertEqual(rows['offline']['lifecycle']['state'], 'unknown')
        self.assertEqual(rows['stale']['lifecycle']['state'], 'stale')
        self.assertEqual(rows['chat']['availability']['state'], 'healthy')
        self.assertEqual(rows['chat']['lifecycle']['state'], 'running')
        self.assertFalse(rows['ollama']['loaded'])
        self.assertEqual(rows['ollama']['lifecycle']['state'], 'unmanaged')
        self.assertFalse(rows['ollama']['routing']['available'])
        self.assertEqual(rows['embed']['activity']['scope'], 'gateway')
        self.assertFalse(rows['embed']['monitoring_support']['slots'])
        self.assertNotIn(str(self.paths.root), json.dumps(data))
        self.assertEqual(data['system']['cpu']['user'], 0)

    def test_uncertain_retains_reservation_and_blocks_route(self):
        ticket = self.context.scheduler.submit('chat', 'chat', tokens=30)
        self.assertTrue(ticket.acquire())
        ticket.quarantine('private fixture recovery reason')
        data = self.snapshot()
        row = next(m for m in data['models'] if m['key'] == 'chat')
        self.assertEqual(row['activity'], {'active': 1, 'waiting': 0, 'uncertain': True, 'scope': 'gateway'})
        self.assertEqual(row['budget']['used'], 30)
        self.assertFalse(row['routing']['available'])
        self.assertEqual(data['lanes']['chat']['queue_depth'], 1)
        self.assertEqual(self.context.scheduler.snapshots()[1]['chat']['used'], 30)
        self.assertNotIn('private fixture', json.dumps(data))
        self.assertIn('completion_uncertain', {d['code'] for d in data['diagnostics']})

    def test_first_failure_is_null_then_failure_retains_stale_success(self):
        self.monitor.system_collector = lambda: (_ for _ in ()).throw(ValueError('/private/key=fixture'))
        data = self.snapshot()
        self.assertIsNone(data['system'])
        self.assertIsNone(data['sources']['system']['last_success_at'])
        self.assertIsNotNone(data['sources']['system']['last_attempt_at'])
        self.assertNotIn('fixture', json.dumps(data['sources']))
        self.monitor.system_collector = lambda: copy.deepcopy(SYSTEM)
        self.clock[0] += 6
        data = self.snapshot()
        success = data['sources']['system']['last_success_at']
        self.monitor.system_collector = lambda: (_ for _ in ()).throw(OSError('secret'))
        self.clock[0] += 6
        data = self.snapshot()
        self.assertEqual(data['system'], SYSTEM)
        self.assertTrue(data['sources']['system']['stale'])
        self.assertEqual(data['sources']['system']['last_success_at'], success)
        self.assertGreater(data['sources']['system']['last_attempt_at'], success)

    def test_llama_details_finite_metrics_bounded_opt_in_output(self):
        self.snapshot()
        data = self.detail('chat')
        self.assertEqual(data['metrics']['state'], 'ready')
        self.assertIsNone(data['metrics']['values'][1]['value'])
        self.assertEqual(data['slots']['items'][0]['decoded'], 7)
        self.assertNotIn('generated', data['slots']['items'][0])
        self.assertEqual(data['process']['cpu_pct'], 0)
        output = self.detail('chat', True)
        self.assertEqual(len(output['slots']['items'][0]['generated']), 8192)
        self.assertTrue(output['slots']['items'][0]['generated'].startswith('<script>'))
        json.dumps(output, allow_nan=False)
        self.assertNotIn('generated', self.detail('chat')['slots']['items'][0])
        self.assertEqual(list(self.paths.root.iterdir()), [self.paths.static])

    def test_service_details_never_probe_unsupported_endpoints(self):
        self.snapshot()
        data = self.detail('embed')
        self.assertEqual(self.fetches, ['http://127.0.0.1:9002/health'])
        self.assertEqual(data['metrics']['state'], 'unsupported')
        self.assertEqual(data['slots']['state'], 'unsupported')
        self.assertEqual(data['health']['state'], 'ready')
        self.assertEqual(data['process']['rss_gb'], .5)

    def test_failed_detail_section_retains_readings_and_marks_stale(self):
        self.snapshot()
        original = self.detail('chat')
        self.monitor.fetch = lambda *args: (_ for _ in ()).throw(CollectionError('backend_unreachable', 'Unavailable'))
        self.clock[0] += 4
        failed = self.detail('chat')
        self.assertEqual(failed['metrics']['values'], original['metrics']['values'])
        self.assertEqual(failed['metrics']['state'], 'stale')
        self.assertTrue(failed['source']['stale'])
        self.assertEqual(failed['source']['last_success_at'], original['source']['last_success_at'])

    def test_ollama_is_metadata_only_and_offline_discovered_tags_remain_visible(self):
        self.discovery.value['ollama']['available'].append({'name': 'extra:latest'})
        self.snapshot()
        data = self.detail('extra-latest')
        self.assertEqual(data['ollama']['state'], 'ready')
        self.assertFalse(data['ollama']['loaded'])
        self.assertEqual(self.fetches, [])
        self.discovery.value['ollama'] = {'status': 'offline', 'available': [], 'loaded': []}
        self.discovery.value['ollama_error'] = {'code': 'ollama_unavailable', 'message': 'Ollama unavailable'}
        self.clock[0] += 11
        rows = {m['key']: m for m in self.snapshot()['models']}
        self.assertIn('extra-latest', rows)
        self.assertEqual(rows['extra-latest']['availability']['state'], 'unreachable')
        self.assertIsNone(rows['extra-latest']['loaded'])

    def test_system_collection_failure_does_not_reuse_legacy_healthy_defaults(self):
        with patch('local_llm_deploy.gateway.monitoring.subprocess.run', return_value=SimpleNamespace(returncode=1, stdout='')):
            with self.assertRaises(ValueError):
                collect_system()
        with patch('local_llm_deploy.gateway.monitoring.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='invalid top output')):
            with self.assertRaises(ValueError):
                collect_system()

    def test_registry_refresh_error_preserves_catalog_with_sanitized_diagnostic(self):
        self.context._spec_loader = lambda *args: (_ for _ in ()).throw(ValueError('private config payload'))
        self.context._loaded_at = float('-inf')
        data = self.snapshot()
        self.assertEqual(set(self.specs), {m['key'] for m in data['models']})
        self.assertTrue(data['sources']['catalog']['stale'])
        self.assertEqual(data['sources']['catalog']['error']['code'], 'registry_invalid')
        self.assertNotIn('private config', json.dumps(data))

    def test_reused_pid_is_never_reported_as_owned_process(self):
        self.snapshot()
        self.monitor.identity_validator = lambda identity: False
        self.monitor.process_collector = lambda pid: self.fail('Unverified PID was collected')
        data = self.detail('chat')
        self.assertEqual(data['process']['state'], 'error')
        self.assertIsNone(data['process']['rss_gb'])

    def test_process_identity_change_during_collection_discards_readings(self):
        self.snapshot()
        values = iter([True, False])
        self.monitor.identity_validator = lambda identity: next(values)
        data = self.detail('chat')
        self.assertEqual(data['process']['state'], 'error')
        self.assertIsNone(data['process']['rss_gb'])

    def test_metric_rate_units_and_ollama_version_failure_are_independent(self):
        metrics = self.monitor._metrics(b'llamacpp:predicted_tokens_seconds 42\nllamacpp:prompt_seconds_total 3\n')
        self.assertEqual([item['unit'] for item in metrics['values']], ['tokens/s', 'seconds'])
        self.discovery.value['ollama'] = {'status': 'offline', 'available': [{'name': 'tag:latest'}],
                                          'loaded': [{'name': 'tag:latest', 'size_gb': 7.5}]}
        self.discovery.value['ollama_parts'] = {'tags': True, 'ps': True}
        self.discovery.value['ollama_error'] = {'code': 'ollama_unavailable', 'message': 'Version unavailable'}
        self.snapshot()
        data = self.detail('ollama')
        self.assertEqual(data['health']['state'], 'error')
        self.assertEqual(data['ollama']['state'], 'ready')
        self.assertTrue(data['ollama']['loaded'])
        self.assertEqual(data['ollama']['size_gb'], 7.5)
        self.assertIsNone(data['ollama']['version'])

    def test_missing_backend_credentials_are_sanitized_and_never_probed(self):
        self.specs['chat'].raw['runtime'] = {'api_key_file': '/missing/private-fixture.key'}
        self.snapshot()
        data = self.detail('chat')
        self.assertEqual(data['health']['state'], 'unauthorized')
        self.assertEqual(self.fetches, [])
        self.assertNotIn('private-fixture', json.dumps(data))


class MonitorHTTPTests(MonitorFixture):
    def setUp(self):
        super().setUp()
        self.paths.api_key.write_text('fixture-api-key')
        self.server = create_server(self.context, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, path, method='GET', *, auth=True, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        request_headers = {'Authorization': 'Bearer fixture-api-key'} if auth else {}
        request_headers.update(headers or {})
        conn.request(method, path, body=body, headers=request_headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_auth_head_namespace_and_old_public_contracts(self):
        for path in ('/monitor-api/v1/snapshot', '/monitor-api/v1/models/chat', '/%6donitor-api/v1/snapshot'):
            self.assertEqual(self.request(path, auth=False)[0], 401)
            self.assertEqual(self.request(path)[0], 200)
            status, headers, body = self.request(path, 'HEAD')
            self.assertEqual(status, 200)
            self.assertEqual(body, b'')
            self.assertEqual(headers['Cache-Control'], 'no-store')
        status, _, raw = self.request('/api/models', auth=False)
        self.assertEqual(status, 200)
        self.assertTrue({'models', 'ollama', 'lanes', 'global'} <= json.loads(raw).keys())
        self.assertEqual(self.request('/monitor.html', auth=False)[0], 200)

    def test_wrong_methods_paths_and_queries_are_json_errors(self):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'TRACE', 'CONNECT'):
            status, headers, raw = self.request('/monitor-api/v1/snapshot', method)
            self.assertEqual(status, 405, method)
            self.assertEqual(headers['Content-Type'], 'application/json')
            self.assertIn('error', json.loads(raw))
        for path in ('/monitor-api', '/monitor-api/v2/snapshot', '/monitor-api/v1/models',
                     '/monitor-api/v1/models/missing', '/monitor-api/v1/models/chat/extra'):
            self.assertEqual(self.request(path)[0], 404, path)
        for path in ('/monitor-api/v1/models/%252f', '/monitor-api/v1/models/%GG',
                     '/monitor-api/v1/../snapshot', '/monitor-api/v1/models/chat?include_output=2',
                     '/monitor-api/v1/models/chat?include_output=1&include_output=0',
                     '/monitor-api/v1/snapshot?key=fixture'):
            self.assertEqual(self.request(path)[0], 400, path)
        self.assertEqual(self.request('/monitor-api/v1/snapshot', body='ignored')[0], 400)

    def test_expect_rejects_before_accepting_body(self):
        for target, auth, expected in (('/monitor-api/v1/snapshot', False, b'401'),
                                       ('/monitor-api/v1/snapshot', True, b'405'),
                                       ('/monitor-api/v1/missing', True, b'405')):
            with socket.create_connection(('127.0.0.1', self.server.server_port), timeout=2) as client:
                request = f'POST {target} HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\nExpect: 100-continue\r\n'
                if auth:
                    request += 'Authorization: Bearer fixture-api-key\r\n'
                client.sendall((request + '\r\n').encode())
                response = client.recv(4096)
                self.assertIn(expected, response.split(b'\r\n', 1)[0])
                self.assertNotIn(b'100 Continue', response)


class MonitorMetadataHTTPTests(MonitorFixture):
    def test_http_errors_redirects_size_limit_and_availability_are_distinct(self):
        mode, seen = ['ready'], []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                if mode[0] == 'redirect':
                    self.send_response(302)
                    self.send_header('Location', '/should-not-follow')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                codes = {'unauthorized': 401, 'unsupported': 404, 'unready': 503}
                payload = b'x' * (512 * 1024 + 1) if mode[0] == 'oversize' else b'{"status":"ok"}'
                self.send_response(codes.get(mode[0], 200))
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        endpoint = f'http://127.0.0.1:{server.server_port}/health'
        self.assertEqual(fetch_metadata(endpoint, {}), b'{"status":"ok"}')
        for state, code in (('unauthorized', 'backend_auth'), ('unsupported', 'unsupported'),
                            ('unready', 'backend_unready'), ('oversize', 'response_too_large'),
                            ('redirect', 'backend_unready')):
            mode[0] = state
            with self.assertRaises(CollectionError) as error:
                fetch_metadata(endpoint, {'Authorization': 'Bearer fixture-only'})
            self.assertEqual(error.exception.issue['code'], code)
        self.assertNotIn('/should-not-follow', seen)
        specs = normalize_models({'remote': {'external_backend': True, 'default_port': server.server_port}})
        discovery = Discovery(self.paths, specs, GatewaySettings(discovery_ttl=0), observe=lambda *args: {},
                              fetch_json=lambda url: {'version': 'fixture', 'models': []})
        for state, expected in (('unauthorized', 'unauthorized'), ('unready', 'unready'), ('ready', 'healthy')):
            mode[0] = state
            models = discovery.models()
            self.assertEqual(discovery.published()['availability']['remote'], expected)
            self.assertEqual('remote' in models, state == 'ready')


class MonitorConcurrencyTests(MonitorFixture):
    def test_many_clients_share_collectors_and_reads_remain_prompt(self):
        started, release = threading.Event(), threading.Event()
        count = []
        def collector():
            count.append(1)
            started.set()
            release.wait(3)
            return copy.deepcopy(SYSTEM)
        self.monitor.system_collector = collector
        self.addCleanup(release.set)
        self.monitor.snapshot()
        self.assertTrue(started.wait(1))
        before = time.monotonic()
        threads = [threading.Thread(target=self.monitor.snapshot) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(1)
            self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - before, .75)
        self.assertEqual(len(count), 1)
        self.assertEqual(self.discovery.calls, 1)
        release.set()
        self.settle()

    def test_many_model_details_have_bounded_workers_queue_and_cache(self):
        self.context.specs = normalize_models({f'model-{number}': {'type': 'embedding'} for number in range(160)})
        started, release = threading.Event(), threading.Event()
        def fetch(*args):
            started.set()
            release.wait(3)
            return b'{"status":"ok"}'
        self.monitor.fetch = fetch
        self.addCleanup(release.set)
        self.snapshot()
        for number in range(160):
            self.monitor.detail(f'model-{number}')
        self.assertTrue(started.wait(1))
        self.assertLessEqual(self.monitor.jobs.qsize(), 16)
        self.assertEqual(len(self.monitor.threads), 3)
        self.assertLessEqual(len(self.monitor.inflight), 19)
        self.assertLessEqual(len([key for key in self.monitor.cache if isinstance(key, tuple)]), 128)
        release.set()
        self.settle()

    def test_discovery_publication_does_not_wait_on_probes_and_keeps_route(self):
        started, release = threading.Event(), threading.Event()
        observations = {'chat': SimpleNamespace(status='ready', pid=123, port=9001)}
        block = [False]
        def health(*args, **kwargs):
            if block[0]:
                started.set()
                release.wait(3)
            return True
        discovery = Discovery(self.paths, self.specs, GatewaySettings(discovery_ttl=0),
                              observe=lambda *args: observations, health=health,
                              fetch_json=lambda url: {'version': 'fixture', 'models': []})
        self.context.discovery = discovery
        self.assertIn('chat', discovery.models())
        block[0] = True
        worker = threading.Thread(target=discovery.models)
        worker.start()
        self.addCleanup(worker.join, 4)
        self.addCleanup(release.set)
        self.assertTrue(started.wait(1))
        before = time.monotonic()
        self.assertIn('chat', discovery.published()['models'])
        route = self.context.resolve('/v1/chat/completions', 'POST', b'{"model":"chat"}', {})
        self.assertEqual(route.backend.key, 'chat')
        self.assertLess(time.monotonic() - before, .1)
        release.set()
        worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_cold_discovery_does_not_make_concurrent_first_inference_fail(self):
        started, release = threading.Event(), threading.Event()
        def health(*args, **kwargs):
            started.set()
            release.wait(2)
            return True
        discovery = Discovery(self.paths, self.specs, GatewaySettings(), health=health,
                              observe=lambda *args: {'chat': SimpleNamespace(status='ready', pid=123, port=9001)},
                              fetch_json=lambda url: {'version': 'fixture', 'models': []})
        self.context.discovery = discovery
        producer = threading.Thread(target=discovery.models)
        producer.start()
        self.addCleanup(producer.join, 3)
        self.addCleanup(release.set)
        self.assertTrue(started.wait(1))
        routes = []
        consumer = threading.Thread(target=lambda: routes.append(self.context.resolve(
            '/v1/chat/completions', 'POST', b'{"model":"chat"}', {})))
        consumer.start()
        consumer.join(.03)
        self.assertTrue(consumer.is_alive())
        release.set()
        producer.join(1)
        consumer.join(1)
        self.assertEqual(routes[0].backend.key, 'chat')

    def test_new_registered_key_evicts_conflicting_auto_discovered_route(self):
        discovery = Discovery(self.paths, {}, GatewaySettings(), observe=lambda *args: {},
                              fetch_json=lambda url: {'version': 'fixture', 'models': [{'name': 'dynamic:latest'}]})
        self.assertEqual(discovery.models()['dynamic-latest'].backend, 'ollama')
        specs = normalize_models({'dynamic-latest': {'external_backend': True, 'default_port': 9123,
                                                     'alias': 'registered replacement'}})
        discovery.update_specs(specs)
        self.assertNotIn('dynamic-latest', discovery.published()['models'])

    def test_registry_changes_evict_old_routes_and_old_generation_cannot_publish(self):
        started, release = threading.Event(), threading.Event()
        block = [False]
        def health(*args, **kwargs):
            if block[0]:
                started.set()
                release.wait(2)
            return True
        discovery = Discovery(self.paths, self.specs, GatewaySettings(discovery_ttl=0), health=health,
                              observe=lambda *args: {'chat': SimpleNamespace(status='ready', pid=123, port=9001)},
                              fetch_json=lambda url: {'version': 'fixture', 'models': []})
        self.assertIn('chat', discovery.models())
        block[0] = True
        producer = threading.Thread(target=discovery.models)
        producer.start()
        self.addCleanup(producer.join, 3)
        self.addCleanup(release.set)
        self.assertTrue(started.wait(1))
        discovery.update_specs(normalize_models({'new': {'alias': 'new model'}}))
        self.assertNotIn('chat', discovery.published()['models'])
        release.set()
        producer.join(1)
        self.assertNotIn('chat', discovery.published()['models'])

    def test_slow_registry_loader_does_not_hold_catalog_lock(self):
        started, release = threading.Event(), threading.Event()
        def loader(*args):
            started.set()
            release.wait(3)
            return self.specs
        self.context._spec_loader = loader
        self.context._loaded_at = float('-inf')
        worker = threading.Thread(target=self.context.refresh)
        worker.start()
        self.addCleanup(worker.join, 4)
        self.addCleanup(release.set)
        self.assertTrue(started.wait(1))
        before = time.monotonic()
        specs, _ = self.context.catalog_snapshot()
        self.assertEqual(set(specs), set(self.specs))
        self.context.refresh()
        self.context.scheduler.snapshots()
        self.assertLess(time.monotonic() - before, .1)
        release.set()
        worker.join(1)


if __name__ == '__main__':
    unittest.main()
