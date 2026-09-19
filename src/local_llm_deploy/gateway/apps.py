"""Registered first-class gateway apps. Separate from /services/ proxies."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from local_llm_deploy.domain import AppSpec


def pathname(path):
    return path.split('?', 1)[0]


def matches_prefix(path, prefix):
    current = pathname(path)
    return current == prefix or current.startswith(prefix + '/')


def match_app(path, apps):
    for spec in (apps or {}).values():
        if matches_prefix(path, spec.prefix):
            return spec, 'primary'
        for legacy in spec.legacy_prefixes:
            if matches_prefix(path, legacy):
                return spec, 'legacy'
    return None, None


def slash_redirect(path, prefix):
    query = '?' + path.split('?', 1)[1] if '?' in path else ''
    return prefix + '/' + query


def rewrite_legacy(path, legacy, prefix):
    parsed = urlsplit(path)
    rest = parsed.path[len(legacy):] or '/'
    query = ('?' + parsed.query) if parsed.query else ''
    if rest == '/':
        return prefix + '/' + query
    return prefix + rest + query


def app_remainder(path, prefix):
    parsed = urlsplit(path)
    rest = parsed.path[len(prefix):] or '/'
    return rest + (('?' + parsed.query) if parsed.query else '')


def knowledge_backend_url(path, base_url, prefix):
    if not matches_prefix(path, prefix):
        raise ValueError('Not a knowledge path')
    if pathname(path) == prefix:
        return None
    return base_url.rstrip('/') + path[len(prefix):]


def knowledge_upstream(spec: AppSpec, settings, environ=None):
    env = os.environ if environ is None else environ
    override = str(env.get('KNOWLEDGE_COLLECTOR_URL') or '').strip()
    if override:
        return override.rstrip('/')
    return (spec.upstream or getattr(settings, 'knowledge_url', '')).rstrip('/')


def knowledge_timeout(spec: AppSpec, settings):
    if spec.timeout:
        return spec.timeout
    return getattr(settings, 'knowledge_timeout', 30)


def video_home(spec: AppSpec | None, settings, environ=None):
    env = os.environ if environ is None else environ
    return (env.get('YT_ARCHIVE_HOME') or (spec.home if spec else '') or
            getattr(settings, 'archive_home', '') or str(Path.home() / '.yt-study-archive'))


def video_root(spec: AppSpec | None, settings, environ=None):
    env = os.environ if environ is None else environ
    return env.get('YT_ARCHIVE_ROOT') or (spec.root if spec else '') or getattr(settings, 'archive_root', '') or ''


def video_node(spec: AppSpec | None, settings, environ=None):
    env = os.environ if environ is None else environ
    return env.get('YT_ARCHIVE_NODE') or (spec.node if spec else '') or getattr(settings, 'archive_node', '') or 'node'
