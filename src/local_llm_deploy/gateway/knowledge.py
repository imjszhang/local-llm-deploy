"""Knowledge collector path mapping. No model credentials or inference policy."""
from __future__ import annotations

KNOWLEDGE_PREFIX = '/knowledge'


def is_knowledge_path(path):
    pathname = path.split('?', 1)[0]
    return pathname == KNOWLEDGE_PREFIX or pathname.startswith(KNOWLEDGE_PREFIX + '/')


def knowledge_redirect_location(path):
    query = '?' + path.split('?', 1)[1] if '?' in path else ''
    return KNOWLEDGE_PREFIX + '/' + query


def knowledge_backend_url(path, base_url):
    if not is_knowledge_path(path):
        raise ValueError('Not a knowledge path')
    if path.split('?', 1)[0] == KNOWLEDGE_PREFIX:
        return None
    return base_url.rstrip('/') + path[len(KNOWLEDGE_PREFIX):]
