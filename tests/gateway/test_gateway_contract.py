"""HTTP contracts recorded against the legacy gateway before extraction."""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeBackend(BaseHTTPRequestHandler):
    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        payload = json.dumps({'path': self.path, 'body': body.decode(),
                              'authorization': self.headers.get('Authorization')}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def start_server(handler):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def stop_server(server):
    server.shutdown()
    server.server_close()


class GatewayContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'static').mkdir()
        (self.root / 'static' / 'monitor.html').write_text('monitor-fixture')
        self.backend = start_server(FakeBackend)
        self.port = self.backend.server_port
        self.config = {
            'chat': {'type': 'chat', 'alias': 'chat-alias', 'default_port': self.port},
            'embed': {'type': 'embedding', 'alias': 'embed-alias', 'default_port': self.port},
            'asr': {'type': 'asr', 'alias': 'asr-alias', 'default_port': self.port},
        }
        self.models = {k: {'pid': None, 'port': self.port, 'model': v['alias']}
                       for k, v in self.config.items()}
        self.gateway = self.make_gateway()

    def make_gateway(self):
        baseline = os.environ.get('LOCAL_LLM_TEST_BASELINE_COMMIT')
        if baseline:
            return self.make_baseline_gateway(baseline)
        from local_llm_deploy.config import ProjectPaths, normalize_models
        from local_llm_deploy.gateway.app import GatewayContext, create_server
        from local_llm_deploy.gateway.discovery import Backend
        from local_llm_deploy.gateway.settings import GatewaySettings
        specs = normalize_models(self.config)
        models = {k: Backend(k, v['model'], f'http://127.0.0.1:{self.port}',
                             specs[k].capabilities, specs[k].backend)
                  for k, v in self.models.items()}
        class FakeDiscovery:
            def models(self):
                return models
            def ollama_status(self):
                return {'status': 'offline'}
        settings = GatewaySettings(knowledge_url=f'http://127.0.0.1:{self.port}/collector')
        self.context = GatewayContext(ProjectPaths(self.root), specs=specs,
                                      discovery=FakeDiscovery(), settings=settings)
        server = create_server(self.context, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def make_baseline_gateway(self, revision):
        """Opt-in comparison of identical contracts against this repo's old source."""
        if not re.fullmatch(r'[0-9a-f]{40}', revision):
            raise ValueError('LOCAL_LLM_TEST_BASELINE_COMMIT must be a full commit')
        repository = Path(__file__).resolve().parents[2]
        source = subprocess.check_output(['git', '-C', str(repository), 'show', revision + ':serve-ui.py'])
        filename = self.root / 'baseline_gateway.py'
        filename.write_bytes(source)
        loader = importlib.util.spec_from_file_location('baseline_gateway', filename)
        legacy = importlib.util.module_from_spec(loader)
        loader.loader.exec_module(legacy)
        legacy.STATIC_DIR = str(self.root / 'static')
        legacy.API_KEY_FILE = str(self.root / '.api-key')
        legacy.ACCESS_LOG_FILE = None
        legacy.OLLAMA_AUTO_DISCOVER = False
        legacy.KNOWLEDGE_COLLECTOR_URL = f'http://127.0.0.1:{self.port}/collector'
        legacy._load_models_json = lambda: self.config
        legacy.get_running_models = lambda: self.models
        legacy.get_ollama_status = lambda: {'status': 'offline'}
        return start_server(legacy.ProxyHandler)

    def tearDown(self):
        stop_server(self.gateway)
        stop_server(self.backend)
        self.tmp.cleanup()

    def request(self, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.gateway.server_port, timeout=3)
        if isinstance(body, dict):
            body = json.dumps(body)
            headers = {'Content-Type': 'application/json', **(headers or {})}
        conn.request('POST' if body is not None else 'GET', path, body, headers or {})
        resp = conn.getresponse()
        result = (resp.status, dict(resp.getheaders()), resp.read())
        conn.close()
        return result

    def test_chat_alias_and_json_body(self):
        status, headers, payload = self.request('/v1/chat/completions', {'model': 'chat-alias', 'messages': []})
        self.assertEqual(status, 200)
        result = json.loads(payload)
        self.assertEqual(result['path'], '/v1/chat/completions')
        self.assertEqual(json.loads(result['body'])['model'], 'chat-alias')

    def test_embedding_and_named_proxy(self):
        for path in ('/v1/embeddings', '/api/embed/v1/embeddings'):
            status, _, payload = self.request(path, {'model': 'embed-alias', 'input': ['文本']})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(payload)['path'], '/v1/embeddings')

    def test_models_and_monitor_fields(self):
        status, _, payload = self.request('/v1/models')
        self.assertEqual(status, 200)
        names = {m['id'] for m in json.loads(payload)['data']}
        self.assertTrue({'chat', 'chat-alias', 'embed', 'embed-alias'} <= names)
        status, _, payload = self.request('/api/models')
        data = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertTrue({'models', 'ollama', 'lanes', 'global'} <= data.keys())
        self.assertTrue({'chat', 'embed', 'asr'} <= data['lanes'].keys())

    def test_knowledge_rewrite_and_client_credentials(self):
        status, headers, _ = self.request('/knowledge?x=1')
        self.assertEqual(status, 301)
        self.assertEqual(headers['Location'], '/knowledge/?x=1')
        status, _, payload = self.request('/knowledge/items?x=1', headers={'Authorization': 'Bearer knowledge-test'})
        data = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(data['path'], '/collector/items?x=1')
        self.assertEqual(data['authorization'], 'Bearer knowledge-test')

    def test_static_monitor(self):
        status, _, payload = self.request('/monitor.html')
        self.assertEqual(status, 200)
        self.assertEqual(payload, b'monitor-fixture')


if __name__ == '__main__':
    unittest.main()
