"""In-process /video/ mount does not need a second HTTP listener."""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from local_llm_deploy.config import ProjectPaths, normalize_models
from local_llm_deploy.gateway.app import GatewayContext, create_server
from local_llm_deploy.gateway.discovery import Backend
from local_llm_deploy.gateway.settings import GatewaySettings
from local_llm_deploy.gateway.apps import match_app, rewrite_legacy, slash_redirect
from tests.gateway.app_fixtures import video_app


def seed_catalog(home, videos):
    catalog = Path(home) / 'catalog.db'
    connection = sqlite3.connect(catalog)
    connection.execute("""CREATE TABLE videos (
        video_id TEXT PRIMARY KEY,
        platform TEXT NOT NULL DEFAULT 'youtube',
        input_url TEXT NOT NULL,
        canonical_url TEXT NOT NULL,
        kind TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        channel TEXT NOT NULL DEFAULT '',
        channel_id TEXT NOT NULL DEFAULT '',
        duration_sec INTEGER,
        published_at TEXT,
        description TEXT NOT NULL DEFAULT '',
        video_path TEXT,
        thumb_path TEXT,
        subs_json TEXT,
        quality TEXT NOT NULL DEFAULT '720',
        bytes INTEGER,
        sha256 TEXT,
        status TEXT NOT NULL,
        error TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    )""")
    for video in videos:
        connection.execute(
            """INSERT INTO videos (video_id,platform,input_url,canonical_url,kind,title,channel,
               video_path,thumb_path,quality,bytes,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            video,
        )
    connection.commit()
    connection.close()


class VideoGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / 'video-home'
        self.home.mkdir()
        self.repo = self.root / 'yt-study-archive'
        (self.repo / 'src').mkdir(parents=True)
        (self.repo / 'cli').mkdir()
        (self.repo / 'cli' / 'yt.js').write_text('#!/usr/bin/env node\n', encoding='utf-8')
        (self.repo / 'src' / 'index.html').write_text('<html>Study Video</html>', encoding='utf-8')
        (self.root / 'static').mkdir()
        (self.root / '.api-key').write_text('service-key\n')
        (self.home / 'config.json').write_text(json.dumps({
            'apiToken': 'tok',
            'repoRoot': str(self.repo),
        }), encoding='utf-8')
        dest = self.home / 'library' / 'videos' / 'dQw4w9WgXcQ'
        dest.mkdir(parents=True)
        (dest / 'video.mp4').write_bytes(b'media-bytes-xx')
        seed_catalog(self.home, [(
            'dQw4w9WgXcQ', 'youtube', 'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
            'https://www.youtube.com/watch?v=dQw4w9WgXcQ', 'long', 'Fixture', 'Channel',
            'video.mp4', None, '720', 14, 'downloaded', 1, 1,
        )])
        calls = []

        def runner(args):
            calls.append(args)
            return {
                'video': {
                    'videoId': 'newid123456', 'platform': 'youtube', 'kind': 'long',
                    'title': '', 'channel': '', 'durationSec': None, 'publishedAt': None,
                    'quality': '720', 'bytes': None, 'status': 'queued', 'error': None,
                    'createdAt': 2, 'updatedAt': 2, 'canonicalUrl': args[1],
                    'videoPath': None, 'thumbPath': None, 'channelId': '',
                    'inputUrl': args[1], 'description': '', 'subs': {},
                },
                'existed': False, 'skipped': False,
            }

        self.calls = calls
        settings = GatewaySettings(
            api_timeout=1, client_write_timeout=1, max_body_bytes=4096,
            archive_home=str(self.home), archive_root=str(self.repo),
        )
        specs = normalize_models({'chat': {'alias': 'friendly-chat'}})
        class FakeDiscovery:
            def models(self):
                return {'chat': Backend('chat', 'friendly-chat', 'http://127.0.0.1:9', ('chat',), 'llama_cpp')}
            def ollama_status(self):
                return {'status': 'offline'}
        from local_llm_deploy.gateway.video import VideoApp
        apps = video_app(self.home, self.repo)
        self.context = GatewayContext(
            ProjectPaths(self.root), settings=settings, specs=specs, discovery=FakeDiscovery(),
            apps=apps,
            video=VideoApp(str(self.home), str(self.repo), runner=runner, prefix=apps['video'].prefix),
        )
        self.gateway = create_server(self.context, port=0)
        threading.Thread(target=self.gateway.serve_forever, daemon=True).start()

    def tearDown(self):
        self.gateway.shutdown()
        self.gateway.server_close()
        self.tmp.cleanup()

    def request(self, path, body=None, headers=None, method=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.gateway.server_port, timeout=3)
        headers = dict(headers or {})
        if isinstance(body, dict):
            body = json.dumps(body)
            headers = {'Content-Type': 'application/json', **headers}
        connection.request(method or ('POST' if body is not None else 'GET'), path, body, headers)
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def test_path_helpers(self):
        spec = next(iter(self.context.apps.values()))
        self.assertEqual(match_app('/video', self.context.apps)[0], spec)
        self.assertEqual(match_app('/video/api/v1/health.json', self.context.apps)[1], 'primary')
        self.assertIsNone(match_app('/knowledge/', self.context.apps)[0])
        self.assertEqual(match_app('/archive', self.context.apps)[1], 'legacy')
        self.assertEqual(match_app('/archive/api/v1/health.json', self.context.apps)[1], 'legacy')
        self.assertEqual(slash_redirect('/video?x=1', spec.prefix), '/video/?x=1')
        self.assertEqual(rewrite_legacy('/archive?x=1', '/archive', spec.prefix), '/video/?x=1')
        self.assertEqual(
            rewrite_legacy('/archive/api/v1/health.json', '/archive', spec.prefix),
            '/video/api/v1/health.json',
        )

    def test_page_health_list_media_and_token(self):
        status, _, headers = self.request('/video?x=1')
        self.assertEqual(status, 301)
        self.assertEqual(headers['Location'], '/video/?x=1')

        status, payload, _ = self.request('/video/')
        self.assertEqual(status, 200)
        self.assertIn(b'Study Video', payload)

        status, payload, _ = self.request('/video/api/v1/health.json')
        self.assertEqual(status, 200)
        health = json.loads(payload)
        self.assertEqual(health['service'], 'yt-study-archive')
        self.assertEqual(health['total'], 1)
        self.assertTrue(health['llm']['reachable'])

        status, payload, _ = self.request('/video/api/v1/videos.json')
        listed = json.loads(payload)
        self.assertEqual(status, 200)
        self.assertEqual(listed['totalItems'], 1)
        self.assertEqual(listed['data'][0]['videoId'], 'dQw4w9WgXcQ')

        status, payload, _ = self.request('/video/api/v1/videos/dQw4w9WgXcQ/media', headers={'Range': 'bytes=0-4'})
        self.assertEqual(status, 206)
        self.assertEqual(payload, b'media')

        status, _, _ = self.request('/video/api/v1/videos.json', {'url': 'https://youtu.be/dQw4w9WgXcQ'})
        self.assertEqual(status, 401)

        status, payload, _ = self.request(
            '/video/api/v1/videos.json',
            {'url': 'https://youtu.be/aaaaaaaaaaa'},
            {'Authorization': 'Bearer tok'},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)['video']['videoId'], 'newid123456')
        self.assertEqual(self.calls, [['add', 'https://youtu.be/aaaaaaaaaaa']])

    def test_legacy_archive_redirects(self):
        status, _, headers = self.request('/archive?x=1')
        self.assertEqual(status, 301)
        self.assertEqual(headers['Location'], '/video/?x=1')

        status, _, headers = self.request('/archive/api/v1/health.json')
        self.assertEqual(status, 301)
        self.assertEqual(headers['Location'], '/video/api/v1/health.json')

    def test_no_model_key_required(self):
        status, _, _ = self.request('/video/api/v1/stats.json')
        self.assertEqual(status, 200)

    def test_keyword_matches_transcript_json(self):
        dest = self.home / 'library' / 'videos' / 'dQw4w9WgXcQ'
        (dest / 'transcript.json').write_text(json.dumps({
            'source': 'subs', 'lang': 'zh-Hans',
            'segments': [{'start': 0, 'end': 1, 'text': '口播里的独特词'}],
        }), encoding='utf-8')
        status, payload, _ = self.request('/video/api/v1/videos.json?keyword=%E7%8B%AC%E7%89%B9%E8%AF%8D')
        self.assertEqual(status, 200)
        body = json.loads(payload)
        self.assertEqual(body['totalItems'], 1)
        self.assertEqual(body['data'][0]['videoId'], 'dQw4w9WgXcQ')
        status, payload, _ = self.request('/video/api/v1/videos.json?keyword=%E6%B2%A1%E6%9C%89%E8%BF%99%E5%8F%A5')
        self.assertEqual(json.loads(payload)['totalItems'], 0)

    def test_unregistered_video_is_not_mounted(self):
        self.context.apps = {}
        status, _, _ = self.request('/video/')
        self.assertEqual(status, 404)
        status, _, _ = self.request('/archive/')
        self.assertEqual(status, 404)


if __name__ == '__main__':
    unittest.main()
