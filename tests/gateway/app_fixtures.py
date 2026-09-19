"""Registered app specs for gateway tests. Not mixed with proxy fixtures."""
from __future__ import annotations

from local_llm_deploy.config import normalize_app


def knowledge_app(upstream, key='knowledge', alias='知识库'):
    spec = normalize_app(key, {
        'type': 'app', 'kind': 'knowledge', 'alias': alias, 'upstream': upstream,
    })
    return {spec.key: spec}


def video_app(home, root='', key='video', alias='视频库', legacy=('/archive',)):
    cfg = {
        'type': 'app', 'kind': 'video', 'alias': alias, 'home': str(home),
        'legacy_prefixes': list(legacy),
    }
    if root:
        cfg['root'] = str(root)
    spec = normalize_app(key, cfg)
    return {spec.key: spec}


def merge_apps(*groups):
    merged = {}
    for group in groups:
        merged.update(group)
    return merged
