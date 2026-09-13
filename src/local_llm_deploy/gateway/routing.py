"""Pure capability and protocol routing. Explicit model mistakes never fall back."""
from __future__ import annotations

from dataclasses import dataclass
from email import message_from_bytes
from email.policy import HTTP
import json
import re
from urllib.parse import unquote, urlsplit


ENDPOINTS = {
    '/v1/chat/completions': ('chat', 'chat'),
    '/v1/completions': ('chat', 'chat'),
    '/v1/responses': ('chat', 'responses'),
    '/v1/messages': ('chat', 'messages'),
    '/v1/embeddings': ('embedding', None),
    '/v1/rerank': ('rerank', None),
    '/v1/audio/transcriptions': ('asr', None),
}
NATIVE_OLLAMA = {
    '/api/chat': ('chat', 'ollama'),
    '/api/generate': ('chat', 'ollama'),
    '/api/embed': ('embedding', None),
    '/api/embeddings': ('embedding', None),
}
NATIVE_LLAMA = {
    '/completion': ('chat', 'chat', '/v1/completions'),
    '/infill': ('chat', 'chat', '/v1/completions'),
    '/embedding': ('embedding', None, '/v1/embeddings'),
    '/reranking': ('rerank', None, '/v1/rerank'),
    '/v1/reranking': ('rerank', None, '/v1/rerank'),
}
READ_ONLY = frozenset(('/health', '/metrics', '/slots', '/props', '/models', '/v1/models'))
LLAMA_METADATA = frozenset(('/tokenize', '/detokenize', '/apply-template'))
OLLAMA_OPERATIONS = {
    '/api/version': ('GET', 'HEAD'), '/api/tags': ('GET', 'HEAD'), '/api/ps': ('GET', 'HEAD'),
    '/api/show': ('POST',), '/api/create': ('POST',), '/api/copy': ('POST',),
    '/api/pull': ('POST',), '/api/push': ('POST',), '/api/delete': ('DELETE',),
}


class RoutingError(Exception):
    def __init__(self, status, message, code='invalid_request_error'):
        super().__init__(message)
        self.status, self.code = status, code


def normalize_proxy_path(path):
    """Decode API paths once, before authentication and endpoint classification.

    Backends decode paths too. Residual escapes and path components that a
    backend might normalize differently cannot be safely treated as metadata.
    The query is opaque and never participates in endpoint selection.
    """
    parsed = urlsplit(path)
    if re.search(r'%(?![0-9a-fA-F]{2})', parsed.path):
        raise RoutingError(400, 'Invalid URL path encoding')
    try:
        decoded = unquote(parsed.path, encoding='utf-8', errors='strict')
    except UnicodeDecodeError:
        raise RoutingError(400, 'Invalid URL path encoding') from None
    if ('%' in decoded or '\\' in decoded or '//' in decoded or
            any(ord(char) < 32 or ord(char) == 127 for char in decoded) or
            any(part in ('.', '..') for part in decoded.split('/'))):
        raise RoutingError(400, 'Ambiguous URL path')
    return decoded + ('?' + parsed.query if parsed.query else '')


def validate_passthrough(model, endpoint, method):
    methods = ('GET', 'HEAD') if endpoint in READ_ONLY else None
    if model.ollama:
        methods = OLLAMA_OPERATIONS.get(endpoint, methods)
        if re.fullmatch(r'/api/blobs/sha256:[0-9a-fA-F]{64}', endpoint):
            methods = ('HEAD', 'POST')
    elif model.backend == 'llama_cpp' and endpoint in LLAMA_METADATA:
        methods = ('POST',)
    if methods is None:
        raise RoutingError(404, f'Unknown backend endpoint: {endpoint}')
    if method not in methods:
        raise RoutingError(405, 'Method not allowed for backend endpoint')


@dataclass(frozen=True)
class Route:
    backend: object
    url: str
    body: bytes | None
    capability: str | None = None
    protocol: str | None = None
    stream: bool = False


def json_object(body):
    if not body:
        raise RoutingError(400, 'Empty request body')
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise RoutingError(400, 'Invalid JSON body') from None
    if not isinstance(value, dict):
        raise RoutingError(400, 'Request body must be a JSON object')
    if value.get('model') is not None and not isinstance(value['model'], str):
        raise RoutingError(400, 'model must be a string')
    if 'stream' in value and not isinstance(value['stream'], bool):
        raise RoutingError(400, 'stream must be a boolean')
    return value


def multipart_model(body, content_type):
    if not content_type.lower().startswith('multipart/form-data'):
        raise RoutingError(400, 'Content-Type must be multipart/form-data')
    message = message_from_bytes(f'Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n'.encode() + (body or b''), policy=HTTP)
    if not message.is_multipart():
        raise RoutingError(400, 'Invalid multipart body')
    for part in message.iter_parts():
        if part.get_param('name', header='content-disposition') == 'model':
            if part.get_filename():
                raise RoutingError(400, 'model must be a text field')
            return (part.get_payload(decode=True) or b'').decode('utf-8', errors='replace')
    return None


class Router:
    def __init__(self, specs, settings):
        self.specs, self.settings = specs, settings

    def _default(self, capability):
        if capability in self.settings.defaults:
            return self.settings.defaults[capability]
        matches = [s.key for s in self.specs.values() if capability in s.raw.get('default_for', [])]
        if len(matches) == 1:
            return matches[0]
        raise RoutingError(400, f'model is required; configure an explicit default for {capability}')

    def select(self, models, requested, capability, unavailable=None):
        requested = requested or self._default(capability)
        matches = [m for m in models.values() if requested in m.names]
        if len(matches) > 1:
            raise RoutingError(409, f'Ambiguous model: {requested}')
        if not matches:
            known = next((s.key for s in self.specs.values() if requested in (s.key, s.alias, s.backend_model)), None)
            raise RoutingError(503 if known else 404,
                               (unavailable or {}).get(known, f'Model unavailable: {requested}') if known else f'Unknown model: {requested}')
        model = matches[0]
        if capability not in model.capabilities:
            raise RoutingError(400, f'Model {requested} does not support {capability}')
        return model

    def resolve(self, path, method, body, headers, models, unavailable=None):
        parsed = urlsplit(normalize_proxy_path(path))
        endpoint = parsed.path.rstrip('/')
        query = '?' + parsed.query if parsed.query else ''
        named = None
        native_ollama = False
        if endpoint.startswith('/api/'):
            suffix = endpoint[4:]
            first, _, rest = suffix.lstrip('/').partition('/')
            if first == 'ollama':
                native_ollama = True
                endpoint = '/' + rest
            elif first in models or first in self.specs:
                named = first
                endpoint = '/' + rest
            else:
                endpoint = suffix
            if endpoint.lstrip('/') in {p[4:] for p in ENDPOINTS}:
                endpoint = '/v1/' + endpoint.lstrip('/')
        if endpoint in NATIVE_OLLAMA:
            capability, protocol = NATIVE_OLLAMA[endpoint]
        elif endpoint in NATIVE_LLAMA:
            capability, protocol, _ = NATIVE_LLAMA[endpoint]
        else:
            capability, protocol = ENDPOINTS.get(endpoint, (None, None))
        if capability:
            if method != 'POST':
                raise RoutingError(405, 'Inference endpoints require POST')
            if capability == 'asr':
                requested = multipart_model(body, headers.get('Content-Type', ''))
                data = None
                stream = False
            else:
                data = json_object(body)
                requested = data.get('model')
                stream = data.get('stream', protocol == 'ollama')
            model = self.select(models, named or requested, capability, unavailable)
            if (native_ollama or endpoint in NATIVE_OLLAMA) and not model.ollama:
                raise RoutingError(400, 'The selected model is not provided by Ollama')
            if endpoint in NATIVE_LLAMA and model.backend != 'llama_cpp':
                raise RoutingError(400, 'The selected model is not provided by llama.cpp')
            if named and requested and requested not in model.names:
                raise RoutingError(400, 'Path model and body model disagree')
            allowed = model.endpoints
            if allowed is None:
                from local_llm_deploy.domain import ModelSpec
                allowed = ModelSpec(model.key, model.alias, model.capabilities, model.backend,
                                    'external' if model.external else 'managed', {}).endpoints
            if endpoint in NATIVE_OLLAMA and model.ollama:
                allowed = tuple(allowed) + tuple(NATIVE_OLLAMA)
            declared_endpoint = NATIVE_LLAMA[endpoint][2] if endpoint in NATIVE_LLAMA else endpoint
            if declared_endpoint not in allowed:
                raise RoutingError(400, f'Model {model.key} does not support endpoint {endpoint}')
            if data is not None and model.backend_model:
                data['model'] = model.backend_model
                body = json.dumps(data, ensure_ascii=False).encode()
            return Route(model, model.url.rstrip('/') + endpoint + query, body, capability, protocol, stream)
        # Explicit, authenticated pass-through remains available for backend
        # health/metrics/model metadata and native Ollama operations.
        if native_ollama:
            from .discovery import Backend
            credentials = next((model.credentials for model in models.values()
                                if model.ollama and model.url == self.settings.ollama_host and model.credentials is not None), None)
            backend = Backend('ollama', 'Ollama', self.settings.ollama_host, backend='ollama',
                              external=True, credentials=credentials)
            validate_passthrough(backend, endpoint, method)
            return Route(backend, backend.url + endpoint + query, body)
        if named:
            model = models.get(named)
            if not model:
                raise RoutingError(503, (unavailable or {}).get(named, f'Model unavailable: {named}'))
            validate_passthrough(model, endpoint, method)
            return Route(model, model.url + endpoint + query, body)
        if parsed.path.startswith('/api/'):
            model = self.select(models, None, 'chat', unavailable)
            validate_passthrough(model, endpoint, method)
            return Route(model, model.url + endpoint + query, body)
        raise RoutingError(404, f'Unknown endpoint: {parsed.path}')
