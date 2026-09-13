"""Serve the committed monitor build through Python, without a frontend runtime."""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import http.client
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from local_llm_deploy.config import ProjectPaths
from local_llm_deploy.gateway.app import GatewayContext, create_server


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class AssetReferences(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'script' and values.get('src'):
            self.paths.append(values['src'])
        if tag == 'link' and values.get('rel') in ('stylesheet', 'modulepreload'):
            self.paths.append(values.get('href', ''))


class NoDiscovery:
    def models(self):
        raise AssertionError('Static files must not probe inference services')

    def ollama_status(self):
        raise AssertionError('Static files must not probe Ollama')


class MonitorStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.paths = ProjectPaths(Path(cls.tmp.name))
        # Copy the real committed publication, never substitute fixture HTML.
        publication = Path(os.environ.get('LOCAL_LLM_TEST_STATIC_DIR', str(PROJECT_ROOT / 'static')))
        shutil.copytree(publication, cls.paths.static)
        if not (cls.paths.static / 'index.html').is_file():
            shutil.copy2(PROJECT_ROOT / 'static' / 'index.html', cls.paths.static / 'index.html')
        cls.paths.api_key.write_text('static-test-fixture-key', encoding='utf-8')
        cls.context = GatewayContext(cls.paths, specs={}, discovery=NoDiscovery())
        cls.addClassCleanup(cls.context.monitor_api.close)
        cls.server = create_server(cls.context, host='127.0.0.1', port=0)
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    def request(self, path, method='GET', headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.headers, response.read()
        finally:
            connection.close()

    def test_real_html_and_existing_root_entry_are_public_and_revalidated(self):
        expected = (self.paths.static / 'monitor.html').read_bytes()
        self.assertIn(b'id="app"', expected, 'Publish the Vue build before validating static deployment')
        self.assertNotIn(b'@vite/client', expected)
        for path in ('/', '/index.html', '/monitor.html'):
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertIn('text/html', headers['Content-Type'])
                self.assertEqual(headers['Cache-Control'], 'no-cache')
                if path == '/monitor.html':
                    self.assertEqual(body, expected)
                else:
                    self.assertIn(b'/monitor.html', body)
                status, head_headers, head_body = self.request(path, 'HEAD')
                self.assertEqual(status, 200)
                self.assertEqual(head_body, b'')
                self.assertEqual(int(head_headers['Content-Length']), len(body))
                self.assertEqual(head_headers['Cache-Control'], 'no-cache')

    def test_html_asset_references_match_manifest_and_served_bytes(self):
        manifest = json.loads((self.paths.static / 'monitor-manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['schema_version'], 1)
        entries = {item['path']: item for item in manifest['files']}
        self.assertEqual(manifest['entry'], 'monitor.html')
        parser = AssetReferences()
        parser.feed((self.paths.static / 'monitor.html').read_text(encoding='utf-8'))
        self.assertTrue(any(path.endswith('.js') for path in parser.paths))
        self.assertTrue(any(path.endswith('.css') for path in parser.paths))
        for path in parser.paths:
            with self.subTest(path=path):
                self.assertRegex(path, r'^/monitor-assets/[A-Za-z0-9_-]+-[A-Za-z0-9_-]{8,}\.(?:js|css)$')
                self.assertIn(path.lstrip('/'), entries)
                entry = entries[path.lstrip('/')]
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertEqual(len(body), entry['bytes'])
                self.assertEqual(hashlib.sha256(body).hexdigest(), entry['sha256'])
                self.assertEqual(body, (self.paths.static / path.lstrip('/')).read_bytes())
                self.assertEqual(headers['Cache-Control'], 'public, max-age=31536000, immutable')
                self.assertIn('javascript' if path.endswith('.js') else 'text/css', headers['Content-Type'])
                status, head_headers, head_body = self.request(path, 'HEAD')
                self.assertEqual(status, 200)
                self.assertEqual(head_body, b'')
                self.assertEqual(int(head_headers['Content-Length']), len(body))
                self.assertEqual(head_headers['Cache-Control'], headers['Cache-Control'])
                status, cached_headers, cached_body = self.request(path, headers={'If-Modified-Since': headers['Last-Modified']})
                self.assertEqual(status, 304)
                self.assertEqual(cached_body, b'')
                self.assertEqual(cached_headers['Cache-Control'], headers['Cache-Control'])

    def test_unknown_monitor_api_is_json_and_missing_asset_is_not_immutable(self):
        auth = {'Authorization': 'Bearer static-test-fixture-key'}
        for path in ('/monitor-api/v1/absent', '/monitor-api/v2/snapshot'):
            status, headers, body = self.request(path, headers=auth)
            self.assertEqual(status, 404)
            self.assertEqual(headers['Content-Type'], 'application/json')
            self.assertIn('error', json.loads(body))
            self.assertNotIn(b'<!DOCTYPE', body)
        status, headers, _ = self.request('/monitor-assets/missing-00000000.js')
        self.assertEqual(status, 404)
        self.assertNotIn('immutable', headers.get('Cache-Control', ''))

    def test_python_serves_publication_without_node_or_any_subprocess(self):
        parser = AssetReferences()
        parser.feed((self.paths.static / 'monitor.html').read_text(encoding='utf-8'))
        self.assertTrue(parser.paths)
        with patch('subprocess.Popen', side_effect=AssertionError('Static serving attempted a subprocess')) as process:
            for path in ('/', '/monitor.html', *parser.paths):
                self.assertEqual(self.request(path)[0], 200)
                self.assertEqual(self.request(path, 'HEAD')[0], 200)
            process.assert_not_called()


if __name__ == '__main__':
    unittest.main()
