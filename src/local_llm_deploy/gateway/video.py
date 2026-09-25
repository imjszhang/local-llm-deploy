"""In-process YouTube/X study video mount. No second HTTP listener."""
from __future__ import annotations

from hmac import compare_digest
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from urllib.parse import parse_qs, urlsplit

from .apps import app_remainder, video_home, video_node, video_root
from .routing import RoutingError


SAFE_VIDEO_ID = re.compile(r'^[A-Za-z0-9_-]{1,32}$')
CORS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, HEAD, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, Authorization',
}
MIME = {
    '.mp4': 'video/mp4',
    '.webm': 'video/webm',
    '.mkv': 'video/x-matroska',
    '.m4a': 'audio/mp4',
    '.mp3': 'audio/mpeg',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.png': 'image/png',
    '.webp': 'image/webp',
    '.html': 'text/html; charset=utf-8',
}


def default_archive_home():
    return str(Path.home() / '.yt-study-archive')


def _expand(value):
    text = str(value or '').strip()
    if not text:
        return ''
    if text.startswith('~/'):
        return str(Path.home() / text[2:])
    return str(Path(text).expanduser())


def _file_config(home):
    path = Path(home) / 'config.json'
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_archive_root(home, explicit=''):
    for candidate in (explicit, os.environ.get('YT_ARCHIVE_ROOT', ''), _file_config(home).get('repoRoot', '')):
        root = _expand(candidate)
        if root and Path(root, 'cli', 'yt.js').is_file():
            return root
    return ''


def resolve_archive_token(home):
    file_token = _file_config(home).get('apiToken', '')
    return str(os.environ.get('YT_ARCHIVE_API_TOKEN') or os.environ.get('API_TOKEN') or file_token or '')


def _bearer(headers):
    header = ''
    if hasattr(headers, 'get'):
        header = headers.get('Authorization') or headers.get('authorization') or ''
    return header[7:].strip() if header.lower().startswith('bearer ') else ''


def require_archive_token(headers, expected):
    if not expected:
        raise RoutingError(401, '未配置 API_TOKEN / apiToken，拒绝写入')
    got = _bearer(headers)
    if not got or not compare_digest(got, expected):
        raise RoutingError(401, '未授权：需要有效的 Bearer token')


def _row(row):
    if row is None:
        return None
    subs = {}
    if row['subs_json']:
        try:
            subs = json.loads(row['subs_json'])
        except ValueError:
            subs = {}
    return {
        'videoId': row['video_id'],
        'platform': row['platform'] or 'youtube',
        'inputUrl': row['input_url'],
        'canonicalUrl': row['canonical_url'],
        'kind': row['kind'],
        'title': row['title'] or '',
        'channel': row['channel'] or '',
        'channelId': row['channel_id'] or '',
        'durationSec': row['duration_sec'],
        'publishedAt': row['published_at'],
        'description': row['description'] or '',
        'videoPath': row['video_path'],
        'thumbPath': row['thumb_path'],
        'subs': subs,
        'quality': row['quality'],
        'bytes': row['bytes'],
        'status': row['status'],
        'error': row['error'],
        'createdAt': row['created_at'],
        'updatedAt': row['updated_at'],
    }


def public_video(video, *, detail=False):
    if not video:
        return None
    item = {
        'videoId': video['videoId'],
        'platform': video['platform'],
        'kind': video['kind'],
        'title': video['title'],
        'channel': video['channel'],
        'durationSec': video['durationSec'],
        'publishedAt': video['publishedAt'],
        'quality': video['quality'],
        'bytes': video['bytes'],
        'status': video['status'],
        'error': video['error'],
        'createdAt': video['createdAt'],
        'updatedAt': video['updatedAt'],
        'canonicalUrl': video['canonicalUrl'],
        'hasMedia': bool(video.get('videoPath')),
        'hasThumb': bool(video.get('thumbPath')),
    }
    if detail:
        item.update({
            'channelId': video.get('channelId') or '',
            'inputUrl': video.get('inputUrl'),
            'description': video.get('description') or '',
            'subs': video.get('subs') or {},
        })
    return item


def _connect(catalog):
    path = Path(catalog)
    if not path.is_file():
        return None
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only = ON')
    except sqlite3.Error:
        pass
    return connection


def _transcript_text(home, video_id):
    path = Path(home) / 'library' / 'videos' / video_id / 'transcript.json'
    if not path.is_file():
        return ''
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return ''
    segments = payload.get('segments') if isinstance(payload, dict) else None
    if not isinstance(segments, list):
        return ''
    return '\n'.join(str(item.get('text') or '') for item in segments if isinstance(item, dict))


def _search_blob(home, video):
    parts = [str(video.get(key) or '') for key in ('title', 'channel', 'description', 'videoId', 'canonicalUrl')]
    parts.append(_transcript_text(home, video['videoId']))
    return '\n'.join(parts).lower()


def list_videos(home, query):
    catalog = Path(home) / 'catalog.db'
    connection = _connect(catalog)
    if connection is None:
        return []
    try:
        rows = connection.execute('SELECT * FROM videos ORDER BY created_at DESC, video_id DESC').fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    videos = [_row(row) for row in rows]
    platform, kind, status = query.get('platform'), query.get('kind'), query.get('status')
    channel = (query.get('channel') or '').lower()
    keyword = (query.get('keyword') or '').lower()
    out = []
    for video in videos:
        if platform and video['platform'] != platform:
            continue
        if kind and video['kind'] != kind:
            continue
        if status and video['status'] != status:
            continue
        if channel and channel not in (video['channel'] or '').lower():
            continue
        if keyword and keyword not in _search_blob(home, video):
            continue
        out.append(video)
    return out


def get_video(home, video_id):
    catalog = Path(home) / 'catalog.db'
    connection = _connect(catalog)
    if connection is None:
        return None
    try:
        row = connection.execute('SELECT * FROM videos WHERE video_id = ?', (video_id,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    return _row(row)


def stats(home):
    result = {
        'total': 0,
        'byStatus': {'queued': 0, 'fetching': 0, 'downloaded': 0, 'failed': 0},
        'byPlatform': {'youtube': 0, 'x': 0},
        'byKind': {'long': 0, 'shorts': 0, 'video': 0},
    }
    catalog = Path(home) / 'catalog.db'
    connection = _connect(catalog)
    if connection is None:
        return result
    try:
        rows = connection.execute(
            'SELECT platform, kind, status, COUNT(*) AS n FROM videos GROUP BY platform, kind, status'
        ).fetchall()
    except sqlite3.Error:
        return result
    finally:
        connection.close()
    for row in rows:
        count = int(row['n'] or 0)
        result['total'] += count
        if row['status']:
            result['byStatus'][row['status']] = result['byStatus'].get(row['status'], 0) + count
        if row['platform']:
            result['byPlatform'][row['platform']] = result['byPlatform'].get(row['platform'], 0) + count
        if row['kind']:
            result['byKind'][row['kind']] = result['byKind'].get(row['kind'], 0) + count
    return result


def resolve_media(home, video, kind):
    relative = video.get('thumbPath') if kind == 'thumb' else video.get('videoPath')
    if not relative:
        return None
    dest = (Path(home) / 'library' / 'videos' / video['videoId']).resolve()
    absolute = (dest / relative).resolve()
    if absolute != dest and not str(absolute).startswith(str(dest) + os.sep):
        return None
    if not absolute.is_file():
        return None
    return absolute


def _json(writer, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode()
    writer.start(status, {'Content-Type': 'application/json; charset=utf-8', **CORS})
    writer.write(body)
    writer.finish()


def _send_file(handler, writer, path, content_type):
    data = path.read_bytes()
    size = len(data)
    extra = {'Accept-Ranges': 'bytes', 'Content-Type': content_type, **CORS}
    range_header = handler.headers.get('Range') if handler is not None else None
    start, end, status = 0, size - 1, 200
    if range_header:
        match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header.strip())
        if not match:
            writer.start(416, {**extra, 'Content-Range': f'bytes */{size}'})
            writer.finish()
            return
        start = int(match.group(1) or 0)
        end = int(match.group(2) or size - 1)
        if start > end or start >= size:
            writer.start(416, {**extra, 'Content-Range': f'bytes */{size}'})
            writer.finish()
            return
        status = 206
        extra['Content-Range'] = f'bytes {start}-{end}/{size}'
    payload = data[start:end + 1]
    writer.start(status, extra)
    writer.write(payload)
    writer.finish()


def _query(remainder):
    parsed = urlsplit(remainder)
    raw = parse_qs(parsed.query, keep_blank_values=True)
    return {key: values[-1] if values else '' for key, values in raw.items()}


def _int(value, default, lo, hi):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


class VideoApp:
    def __init__(self, home, root='', node='node', llm_base='http://127.0.0.1:8888/v1', runner=None,
                 prefix='/video'):
        self.home = Path(_expand(home) or default_archive_home())
        self.root = resolve_archive_root(self.home, root)
        self.node = node or 'node'
        self.prefix = prefix or '/video'
        self.llm_base = (llm_base or 'http://127.0.0.1:8888/v1').rstrip('/')
        self.runner = runner or self._run_cli

    @classmethod
    def from_settings(cls, settings, spec=None, runner=None):
        return cls(
            video_home(spec, settings),
            video_root(spec, settings),
            video_node(spec, settings),
            'http://127.0.0.1:8888/v1',
            runner=runner,
            prefix=spec.prefix if spec else '/video',
        )

    def _run_cli(self, args):
        cli = Path(self.root) / 'cli' / 'yt.js'
        if not cli.is_file():
            raise RoutingError(503, '未配置 YT_ARCHIVE_ROOT / repoRoot，无法写入归档')
        completed = subprocess.run(
            [self.node, str(cli), *args, '--json', '--data-dir', str(self.home)],
            capture_output=True, text=True, timeout=60,
        )
        text = (completed.stdout or '').strip().splitlines()
        payload = {}
        if text:
            try:
                payload = json.loads(text[-1])
            except ValueError as exc:
                raise RoutingError(500, '归档 CLI 返回了无法解析的结果') from exc
        if completed.returncode != 0:
            error = payload.get('error') if isinstance(payload, dict) else {}
            message = error.get('message') if isinstance(error, dict) else None
            raise RoutingError(400, message or '归档写入失败')
        return payload.get('data', payload)

    def dispatch(self, handler, writer, request_path, method, body, headers):
        if method == 'OPTIONS':
            writer.start(204, CORS)
            writer.finish()
            return
        remainder = app_remainder(request_path, self.prefix)
        pathname = remainder.split('?', 1)[0] or '/'
        query = _query(remainder)
        if pathname in ('/', '/index.html'):
            page = Path(self.root) / 'src' / 'index.html' if self.root else Path()
            if not page.is_file():
                raise RoutingError(503, '未配置 YT_ARCHIVE_ROOT / repoRoot，无法提供页面')
            _send_file(handler, writer, page, MIME['.html'])
            return
        if pathname == '/api/v1/health.json':
            counts = stats(self.home)
            _json(writer, 200, {
                'status': 'ok', 'mode': 'local', 'service': 'yt-study-archive',
                'total': counts['total'],
                'llm': {'baseURL': self.llm_base, 'reachable': True, 'status': 200},
            })
            return
        if pathname == '/api/v1/stats.json':
            _json(writer, 200, {'status': 'ok', **stats(self.home)})
            return
        if pathname == '/api/v1/videos.json' and method == 'GET':
            videos = list_videos(self.home, query)
            page = _int(query.get('page'), 1, 1, 10000)
            per_page = _int(query.get('perPage'), 12, 1, 48)
            total = len(videos)
            start = (page - 1) * per_page
            _json(writer, 200, {
                'status': 'ok', 'page': page, 'perPage': per_page,
                'totalItems': total, 'totalPages': max(1, (total + per_page - 1) // per_page),
                'data': [public_video(item) for item in videos[start:start + per_page]],
            })
            return
        if pathname == '/api/v1/videos.json' and method == 'POST':
            require_archive_token(headers, resolve_archive_token(self.home))
            try:
                payload = json.loads(body.decode() if body else b'{}') if body else {}
            except ValueError as exc:
                raise RoutingError(400, 'Invalid JSON body') from exc
            url = str(payload.get('url') or payload.get('inputUrl') or '').strip()
            if not url:
                raise RoutingError(400, 'url is required')
            args = ['add', url]
            if payload.get('quality'):
                args.extend(['--quality', str(payload['quality'])])
            if payload.get('force'):
                args.append('--force')
            result = self.runner(args)
            video = result.get('video') if isinstance(result, dict) else None
            _json(writer, 200, {
                'status': 'ok',
                'video': public_video(video, detail=True) if isinstance(video, dict) and 'videoId' in video else video,
                'existed': bool(result.get('existed')) if isinstance(result, dict) else False,
                'skipped': bool(result.get('skipped')) if isinstance(result, dict) else False,
                'hint': 'node cli/yt.js worker --once',
            })
            return
        video_json = re.fullmatch(r'/api/v1/videos/([^/]+)\.json', pathname)
        if video_json and method == 'GET':
            video_id = video_json.group(1)
            if not SAFE_VIDEO_ID.match(video_id):
                raise RoutingError(400, 'Invalid video id')
            video = get_video(self.home, video_id)
            if not video:
                raise RoutingError(404, f'Video not found: {video_id}')
            _json(writer, 200, {'status': 'ok', 'video': public_video(video, detail=True)})
            return
        retry = re.fullmatch(r'/api/v1/videos/([^/]+)/retry\.json', pathname)
        if retry and method == 'POST':
            require_archive_token(headers, resolve_archive_token(self.home))
            video_id = retry.group(1)
            if not SAFE_VIDEO_ID.match(video_id):
                raise RoutingError(400, 'Invalid video id')
            result = self.runner(['retry', video_id])
            video = result.get('video') if isinstance(result, dict) else None
            _json(writer, 200, {
                'status': 'ok',
                'video': public_video(video, detail=True) if isinstance(video, dict) and 'videoId' in video else video,
                'hint': 'node cli/yt.js worker --once',
            })
            return
        media = re.fullmatch(r'/api/v1/videos/([^/]+)/(media|thumb)', pathname)
        if media and method in ('GET', 'HEAD'):
            video_id, kind = media.group(1), media.group(2)
            if not SAFE_VIDEO_ID.match(video_id):
                raise RoutingError(400, 'Invalid video id')
            video = get_video(self.home, video_id)
            if not video:
                raise RoutingError(404, f'Video not found: {video_id}')
            path = resolve_media(self.home, video, kind)
            if path is None:
                raise RoutingError(404, 'Thumbnail not found' if kind == 'thumb' else 'Media not found')
            _send_file(handler, writer, path, MIME.get(path.suffix.lower(), 'application/octet-stream'))
            return
        raise RoutingError(404, 'Video route not found')
