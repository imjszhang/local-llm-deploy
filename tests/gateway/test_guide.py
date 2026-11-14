"""The public guide explains apps, services and model APIs without secrets."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from local_llm_deploy.config import ProjectPaths, normalize_app, normalize_models, normalize_proxy
from local_llm_deploy.gateway.app import GatewayContext, create_server
from local_llm_deploy.gateway.discovery import Backend
from local_llm_deploy.gateway.guide import build_guide, render_guide, wants_html
from local_llm_deploy.gateway.settings import GatewaySettings


class GuideContentTests(unittest.TestCase):
    def test_explains_current_entries_and_hides_secrets(self):
        specs = normalize_models({
            'chat': {'type': 'chat', 'alias': '对话', 'default_port': 59999, 'default_for': ['chat']},
            'offline': {'type': 'embedding', 'alias': '向量', 'default_port': 59998},
        })
        proxy = normalize_proxy('civ2', {
            'type': 'proxy', 'alias': 'civ2agent', 'upstream': 'http://10.9.8.7:47321',
            'auth': {'upstream': 'bearer', 'bearer': 'secret-token-value'},
            'home_query': 'token=secret-token-value', 'websocket': True,
        })
        app = normalize_app('knowledge', {'type': 'app', 'kind': 'knowledge', 'alias': '知识库', 'upstream': 'http://10.9.8.7:9'})
        discovered = {'chat': Backend('chat', '对话', 'http://127.0.0.1:59999', ('chat',), 'llama_cpp')}
        payload = build_guide({app.key: app}, {proxy.key: proxy}, specs, discovered)
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn('10.9.8.7', text)
        self.assertNotIn('secret-token-value', text)
        self.assertNotIn('47321', text)
        self.assertNotIn('59999', text)
        service = payload['services'][0]
        self.assertEqual(service['path'], '/services/civ2/')
        self.assertIn('WebSocket', service['summary'])
        chat = next(item for item in payload['models'] if item['key'] == 'chat')
        offline = next(item for item in payload['models'] if item['key'] == 'offline')
        self.assertTrue(chat['routable'])
        self.assertFalse(offline['routable'])
        self.assertIn('/v1/chat/completions', [item['path'] for item in chat['endpoints']])
        self.assertIn('默认用于对话', chat['summary'])
        paths = {item['path'] for item in payload['interfaces']}
        self.assertTrue({'/api/guide', '/v1/models', '/v1/chat/completions', '/v1/embeddings'} <= paths)
        page = render_guide(payload).decode()
        self.assertIn('知识库', page)
        self.assertIn('/services/civ2/', page)
        self.assertNotIn('secret-token-value', page)

    def test_browser_accept_selects_html(self):
        self.assertTrue(wants_html('text/html,application/xhtml+xml'))
        self.assertFalse(wants_html('application/json'))
        self.assertFalse(wants_html('application/json, text/html;q=0.8'))
        self.assertFalse(wants_html(None))


class GuideHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / 'static').mkdir()
        (root / '.api-key').write_text('gateway-key\n')
        specs = normalize_models({'chat': {'type': 'chat', 'alias': '对话', 'default_port': 59999}})
        proxy = normalize_proxy('civ2', {
            'type': 'proxy', 'alias': 'civ2agent', 'upstream': 'http://10.9.8.7:47321',
            'home_query': 'token=secret-token-value',
        })
        app = normalize_app('knowledge', {'type': 'app', 'kind': 'knowledge', 'alias': '知识库', 'upstream': 'http://10.9.8.7:9'})
        discovered = {'chat': Backend('chat', '对话', 'http://127.0.0.1:59999', specs['chat'].capabilities, 'llama_cpp')}

        class FakeDiscovery:
            def models(self):
                return discovered

            def ollama_status(self):
                return {'status': 'offline'}

        context = GatewayContext(
            ProjectPaths(root), specs=specs, proxies={proxy.key: proxy}, apps={app.key: app},
            discovery=FakeDiscovery(), settings=GatewaySettings(),
        )
        self.server = create_server(context, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, accept=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        headers = {'Accept': accept} if accept else {}
        conn.request(method, path, headers=headers)
        resp = conn.getresponse()
        result = (resp.status, resp.getheader('Content-Type'), resp.read())
        conn.close()
        return result

    def test_public_json_and_html_hide_the_upstream(self):
        status, content_type, body = self.request('GET', '/api/guide')
        self.assertEqual(status, 200)
        self.assertIn('application/json', content_type)
        payload = json.loads(body)
        self.assertEqual(payload['apps'][0]['path'], '/knowledge/')
        self.assertIn('对话补全', json.dumps(payload, ensure_ascii=False))
        self.assertNotIn(b'10.9.8.7', body)
        self.assertNotIn(b'secret-token-value', body)
        status, content_type, body = self.request('GET', '/api/guide', 'text/html')
        self.assertEqual(status, 200)
        self.assertIn('text/html', content_type)
        self.assertIn('本地网关说明'.encode(), body)
        self.assertNotIn(b'secret-token-value', body)
        self.assertEqual(self.request('POST', '/api/guide')[0], 405)
        self.assertEqual(self.request('GET', '/v1/models')[0], 401)
