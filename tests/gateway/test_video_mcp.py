"""Wire-level MCP, asset safety and download interoperability on the real gateway."""
import json
from pathlib import Path
import sqlite3
import unittest

from tests.gateway import test_gateway_video as fixtures


class VideoMcpTests(unittest.TestCase):
    setUp = fixtures.VideoGatewayTests.setUp
    tearDown = fixtures.VideoGatewayTests.tearDown
    request = fixtures.VideoGatewayTests.request
    ident = 'dQw4w9WgXcQ'
    endpoint = '/video/mcp'

    def rpc(self, method, params=None, extra=None):
        headers = {'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25', **(extra or {})}
        status, payload, headers = self.request(self.endpoint, {
            'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {},
        }, headers)
        return status, json.loads(payload) if payload else None, headers

    def call(self, name, args=None):
        status, payload, _ = self.rpc('tools/call', {'name': name, 'arguments': args or {}})
        self.assertEqual(status, 200)
        return payload['result']

    def seed_text(self):
        dest = self.home / 'library' / 'videos' / self.ident
        (dest / 'subs').mkdir(exist_ok=True)
        (dest / 'subs' / 'zh.vtt').write_text('WEBVTT\n\n00:00.000 --> 00:01.000\n学习视频', encoding='utf-8')
        (dest / 'transcript.json').write_text(json.dumps({'segments': [{'text': '独特台词', 'start': 0, 'end': 1}]}), encoding='utf-8')
        (dest / 'transcript.zh.md').write_text('中文文稿内容', encoding='utf-8')
        conn = sqlite3.connect(self.home / 'catalog.db')
        conn.execute('UPDATE videos SET subs_json=?', (json.dumps({'zh': 'subs/zh.vtt'}),))
        conn.commit()
        conn.close()
        return dest

    def test_lifecycle_and_tools(self):
        status, payload, _ = self.rpc('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                                                    'clientInfo': {'name': 'test', 'version': '1'}})
        self.assertEqual(status, 200)
        self.assertEqual(payload['result']['protocolVersion'], '2025-11-25')
        self.assertIn('tools', payload['result']['capabilities'])
        status, data, _ = self.request(self.endpoint, {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                                      {'Accept': 'application/json, text/event-stream'})
        self.assertEqual((status, data), (202, b''))
        _, payload, _ = self.rpc('tools/list')
        self.assertEqual(len(payload['result']['tools']), 6)
        self.assertTrue(all(t['annotations']['readOnlyHint'] for t in payload['result']['tools']))
        self.assertEqual(self.rpc('ping')[1]['result'], {})
        self.assertEqual(self.rpc('resources/list')[1]['error']['code'], -32601)
        self.assertEqual(self.rpc('tools/call', {'name': 'delete_video'})[1]['error']['code'], -32602)
        self.assertEqual(self.call('video_library_stats')['structuredContent']['total'], 1)
        legacy = self.rpc('tools/call', {'name': 'video_library_stats'}, {'MCP-Protocol-Version': '2025-03-26'})[1]['result']
        self.assertNotIn('structuredContent', legacy)

    def test_search_assets_read_and_download(self):
        dest = self.seed_text()
        result = self.call('search_videos', {'keyword': '独特台词', 'perPage': 1})['structuredContent']
        self.assertEqual(result['total'], 1)
        self.assertIsNone(result['nextPage'])
        self.assertEqual(self.call('search_videos', {'keyword': 'not-found'})['structuredContent']['total'], 0)
        detail = self.call('get_video', {'videoId': self.ident})['structuredContent']
        self.assertNotIn(str(self.home), json.dumps(detail))
        self.assertEqual(len(detail['assets']), 4)
        text = self.call('read_video_text', {'videoId': self.ident, 'assetId': 'manuscript-zh', 'limit': 2})['structuredContent']
        self.assertEqual((text['text'], text['nextOffset']), ('中文', 2))
        rest = self.call('read_video_text', {'videoId': self.ident, 'assetId': 'manuscript-zh', 'offset': 2})['structuredContent']
        self.assertEqual(rest['text'], '文稿内容')
        (dest / 'video.zh.burn.mp4').write_bytes(b'zh-burn')
        download = self.call('get_video_download', {'videoId': self.ident, 'variant': 'burn'})['structuredContent']
        self.assertEqual(download['bytes'], 7)
        self.assertIn(f'127.0.0.1:{self.gateway.server_port}/video/', download['downloadUrl'])
        from urllib.parse import urlsplit
        parsed = urlsplit(download['downloadUrl'])
        status, data, headers = self.request(parsed.path + '?' + parsed.query, headers={'Range': 'bytes=3-'})
        self.assertEqual((status, data), (206, b'burn'))
        self.assertTrue(headers['Content-Disposition'].startswith('attachment;'))
        asset = next(a for a in detail['assets'] if a['assetId'] == 'manuscript-zh')
        status, data, _ = self.request(urlsplit(asset['downloadUrl']).path)
        self.assertEqual((status, data.decode()), (200, '中文文稿内容'))
        status, data, headers = self.request(urlsplit(asset['downloadUrl']).path, method='HEAD')
        self.assertEqual(data, b'')
        self.assertEqual(int(headers['Content-Length']), len('中文文稿内容'.encode()))

    def test_validation_and_path_safety(self):
        self.seed_text()
        for name, args in [
            ('search_videos', {'perPage': True}), ('search_videos', {'page': 0}),
            ('search_videos', {'unknown': 'x'}), ('get_video', {'videoId': '../bad'}),
            ('get_video', {'videoId': 'missing'}), ('get_video_download', {'videoId': self.ident, 'variant': 'soft'}),
            ('read_video_text', {'videoId': self.ident, 'assetId': '../../config.json'}),
            ('read_video_text', {'videoId': self.ident, 'assetId': 'video-source'}),
        ]:
            self.assertTrue(self.call(name, args)['isError'])
        dest = self.home / 'library' / 'videos' / self.ident
        (dest / 'transcript.zh.md').unlink()
        (dest / 'transcript.zh.md').symlink_to(self.home / 'config.json')
        items = self.call('list_video_assets', {'videoId': self.ident})['structuredContent']['assets']
        self.assertNotIn('manuscript-zh', [a['assetId'] for a in items])
        self.assertEqual(self.request(f'/video/api/v1/videos/{self.ident}/assets/manuscript-zh')[0], 404)

    def test_transport_rejects_invalid_requests(self):
        self.assertEqual(self.request(self.endpoint)[0], 405)
        self.assertEqual(self.request(self.endpoint, method='DELETE')[0], 405)
        self.assertEqual(self.rpc('ping', extra={'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.rpc('ping', extra={'Host': f'evil.example:{self.gateway.server_port}'})[0], 403)
        self.assertEqual(self.rpc('ping', extra={'MCP-Protocol-Version': 'invalid'})[0], 400)
        self.assertEqual(self.rpc('ping', extra={'Accept': 'application/json'})[0], 406)
        status, data, _ = self.request(self.endpoint, '{bad', {'Content-Type': 'application/json',
                                                            'Accept': 'application/json, text/event-stream'})
        self.assertEqual((status, json.loads(data)['error']['code']), (400, -32700))
        self.assertEqual(self.rpc('initialize')[1]['error']['code'], -32602)
        status, payload, _ = self.request('/video/api/v1/mcp.json')
        self.assertEqual(status, 200)
        info = json.loads(payload)
        self.assertTrue(info['readOnly'])
        self.assertEqual(len(info['tools']), 6)
        self.assertEqual(info['endpoint'], f'http://127.0.0.1:{self.gateway.server_port}/video/mcp')


if __name__ == '__main__':
    unittest.main()
