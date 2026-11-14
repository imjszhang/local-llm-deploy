"""Root-key-only management for the independent MCP Hub.

This module belongs to the gateway process, so configuration and lifecycle
controls remain available while the Hub is stopped. No client credentials are
forwarded to the Hub's loopback-only internal API.
"""
from __future__ import annotations

import copy
import http.client
import json
import re
import threading
import time
import uuid
from urllib.parse import urlsplit

from .routing import RoutingError


PREFIX = '/mcp-admin-api/v1'
MAX_BODY_BYTES = 256 * 1024
MAX_INTERNAL_BYTES = 2 * 1024 * 1024
IDENTIFIER = re.compile(r'^[A-Za-z0-9_-]{1,128}$')


def is_admin_path(path):
    return path == '/mcp-admin-api' or path.startswith('/mcp-admin-api/')


def authorize(handler):
    """Authenticate before reading a body, including Expect: 100-continue."""
    if (len(handler.headers.get_all('Authorization', [])) != 1 or
            not handler.context.auth.matches_configured_key(handler.headers.get('Authorization'))):
        raise RoutingError(401, 'MCP Hub 管理需要已配置的网关管理员密钥')
    hosts = handler.headers.get_all('Host', [])
    origins = handler.headers.get_all('Origin', [])
    if len(hosts) != 1 or len(origins) > 1:
        raise RoutingError(403, 'MCP Hub 管理请求来源无效')
    try:
        authority = urlsplit('http://' + hosts[0])
        if (not authority.hostname or authority.username or authority.password or
                authority.path or authority.query or authority.fragment or
                any(char.isspace() for char in hosts[0])):
            raise ValueError
        authority.port  # Reject malformed ports before comparing origins.
    except ValueError:
        raise RoutingError(403, 'MCP Hub 管理请求来源无效') from None
    if origins and origins[0] != 'http://' + hosts[0]:
        raise RoutingError(403, 'MCP Hub 管理只接受同源浏览器请求')
    if handler.headers.get('Sec-Fetch-Site', 'none') not in ('same-origin', 'none'):
        raise RoutingError(403, 'MCP Hub 管理只接受同源浏览器请求')
    length = handler._content_length(MAX_BODY_BYTES)
    if handler.command in ('GET', 'HEAD', 'DELETE') and length:
        raise RoutingError(400, '此管理请求不能包含正文')
    if handler.command in ('POST', 'PUT'):
        if handler.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
            raise RoutingError(415, '管理请求需要 application/json')
        if not length:
            raise RoutingError(400, '管理请求需要 JSON 正文')


def _payload(handler):
    try:
        value = json.loads(handler._body(MAX_BODY_BYTES) or b'{}')
    except (ValueError, UnicodeError):
        raise RoutingError(400, '管理请求不是有效 JSON') from None
    if not isinstance(value, dict):
        raise RoutingError(400, '管理请求正文必须是对象')
    return value


def _fields(value, required, optional=()):
    if set(value) - set(required) - set(optional) or set(required) - set(value):
        raise RoutingError(400, '管理请求字段不完整或包含未知字段')


def _json(writer, status, payload):
    writer.start(status, {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store'})
    writer.write(json.dumps(payload, ensure_ascii=False).encode())
    writer.finish()


class MCPAdminAPI:
    def __init__(self, context, *, store=None, controller=None, state=None, internal_request=None):
        self.context = context
        self._store, self._controller, self._state = store, controller, state
        self._internal_request = internal_request
        self._lock = threading.RLock()
        self._operations = {}
        self._active_operation = None

    def _components(self):
        # Lazy construction keeps the existing gateway independent of Hub state.
        with self._lock:
            if self._store is None:
                from local_llm_deploy.mcp_hub.config import HubConfigStore
                self._store = HubConfigStore(self.context.paths)
            if self._controller is None:
                from local_llm_deploy.mcp_hub.controller import HubController
                self._controller = HubController(self.context.paths)
            if self._state is None:
                from local_llm_deploy.mcp_hub.state import HubState
                self._state = HubState(self.context.paths)
        return self._store, self._controller, self._state

    def _scrub(self, value):
        text = str(value)
        secrets = []
        try:
            secrets.append(self.context.auth.key())
            if self._state is not None:
                secrets.append(self._state.internal_key())
            if self._store is not None:
                def collect(item):
                    if isinstance(item, dict):
                        for key, child in item.items():
                            if key == 'args' and isinstance(child, list):
                                secrets.extend(arg for arg in child if isinstance(arg, str) and len(arg) >= 4)
                            elif key == 'env' and isinstance(child, dict):
                                secrets.extend(value for value in child.values() if isinstance(value, str) and len(value) >= 4)
                            elif any(part in key.lower() for part in ('key', 'token', 'secret', 'password', 'auth')) and isinstance(child, str):
                                secrets.append(child)
                            else:
                                collect(child)
                    elif isinstance(item, list):
                        for child in item:
                            collect(child)
                collect(self._store.load())
        except (OSError, ValueError):
            pass
        for secret in sorted({item for item in secrets if isinstance(item, str) and item}, key=len, reverse=True):
            text = text.replace(secret, '[REDACTED]')
        text = re.sub(r'(?i)\bBearer\s+[^\s\"\'<>]+', 'Bearer [REDACTED]', text)
        text = re.sub(r'\bmcp_hub_[A-Za-z0-9_-]+', '[REDACTED]', text)
        text = re.sub(r'\bsk-[A-Za-z0-9_-]{16,}', '[REDACTED]', text)
        text = re.sub(r'(?i)(--?(?:api[-_]?key|token|secret|password)\s+)[^\s\"\']+', r'\1[REDACTED]', text)
        text = re.sub(r'(?i)((?:api[_-]?key|token|secret|password|authorization)[\"\']?\s*[:=]\s*[\"\']?)[^\s\"\'&,}]+', r'\1[REDACTED]', text)
        return text

    def _internal(self, runtime, target, method='GET'):
        if runtime.get('status') not in ('starting', 'running', 'ready', 'unhealthy'):
            raise RoutingError(503, 'MCP Hub 未运行；可在此页面编辑配置或启动')
        port = runtime.get('port')
        if type(port) is not int or not 1 <= port <= 65535:
            raise RoutingError(503, 'MCP Hub 运行地址不可用')
        if self._internal_request is not None:
            return self._internal_request(runtime, target, method)
        host = '::1' if runtime.get('host') in ('::', '::1') else '127.0.0.1'
        timeout = 15 if method == 'POST' else 8 if target.endswith('/tools') else 2
        connection = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            headers = {'X-Hub-Internal-Key': self._state.internal_key(), 'Accept': 'application/json'}
            body = None
            if method == 'POST':
                body = b'{}'
                headers['Content-Type'] = 'application/json'
            connection.request(method, target, body, headers)
            response = connection.getresponse()
            data = response.read(MAX_INTERNAL_BYTES + 1)
            if response.status != 200 or len(data) > MAX_INTERNAL_BYTES:
                raise RoutingError(503, 'MCP Hub 内部接口暂不可用')
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (OSError, ValueError, http.client.HTTPException):
            raise RoutingError(503, 'MCP Hub 暂不可达；管理页面仍可编辑配置和重启') from None
        finally:
            connection.close()

    def snapshot(self, handler):
        store, controller, state = self._components()
        runtime = dict(controller.status())
        try:
            config = store.load()
        except ValueError as exc:
            config = None
            runtime['config_error'] = self._scrub(exc)
        try:
            live = self._internal(runtime, '/internal/status')
            runtime.update(available=True, services=live.get('services', []))
            for key in ('tool_count', 'started_at', 'active_connections', 'applied_revision'):
                if key in live:
                    runtime[key] = live[key]
        except RoutingError as exc:
            runtime.update(available=False, services=[], error=str(exc))
        hostname = urlsplit('http://' + handler.headers['Host']).hostname
        if ':' in hostname:
            hostname = '[' + hostname + ']'
        services = (config or {}).get('services', {})
        if isinstance(services, list):
            service_ids = [item.get('id') for item in services if isinstance(item, dict) and item.get('enabled', True)]
        else:
            service_ids = [key for key, item in services.items() if isinstance(item, dict) and item.get('enabled', True)]
        base = (config or {}).get('listen', {}).get('public_url') or f'http://{hostname}:{runtime.get("port", 8899)}'
        connections = [{'service_id': key, 'url': f'{base.rstrip("/")}/mcp/{key}',
                        'transport': 'streamable-http'} for key in service_ids if key and IDENTIFIER.fullmatch(key)]
        return {'config': config, 'runtime': runtime, 'devices': state.list_devices(),
                'tasks': state.list_tasks(limit=50), 'connections': connections}

    def action(self, action):
        if action not in ('start', 'stop', 'restart'):
            raise RoutingError(400, '不支持的 MCP Hub 操作')
        _store, controller, _state = self._components()
        with self._lock:
            if self._active_operation is not None:
                raise RoutingError(409, '另一个 MCP Hub 操作正在执行')
            operation = {'id': uuid.uuid4().hex, 'action': action, 'status': 'running',
                         'created_at': int(time.time() * 1000)}
            self._operations[operation['id']] = operation
            self._active_operation = operation['id']
            while len(self._operations) > 100:
                self._operations.pop(next(iter(self._operations)))
            result = copy.deepcopy(operation)

        def run():
            try:
                value = getattr(controller, action)()
                update = {'status': 'succeeded', 'result': value}
            except Exception as exc:
                update = {'status': 'failed', 'error': self._scrub(exc)}
            with self._lock:
                operation.update(update, finished_at=int(time.time() * 1000))
                self._active_operation = None
        threading.Thread(target=run, name='mcp-hub-management', daemon=True).start()
        return {'operation': result}

    def dispatch(self, handler, writer):
        authorize(handler)
        target = urlsplit(handler.path)
        if target.query or target.fragment or '%' in target.path or not target.path.startswith(PREFIX + '/'):
            raise RoutingError(404, '未知 MCP Hub 管理接口')
        path = target.path[len(PREFIX):]
        method = handler.command
        store, controller, state = self._components()
        try:
            if path == '/snapshot' and method in ('GET', 'HEAD'):
                payload = self.snapshot(handler)
            elif path == '/config' and method == 'PUT':
                body = _payload(handler)
                _fields(body, ('config', 'expected_revision'))
                if type(body['expected_revision']) is not int:
                    raise RoutingError(400, 'expected_revision 必须是整数')
                payload = {'config': store.save(body['config'], expected_revision=body['expected_revision'])}
            elif path == '/actions' and method == 'POST':
                body = _payload(handler)
                _fields(body, ('action',))
                _json(writer, 202, self.action(body['action']))
                return
            elif path.startswith('/operations/') and method in ('GET', 'HEAD'):
                ident = path.removeprefix('/operations/')
                with self._lock:
                    operation = self._operations.get(ident)
                    if operation is None:
                        raise RoutingError(404, '管理操作不存在或网关已重启')
                    payload = {'operation': copy.deepcopy(operation)}
            elif path == '/logs' and method in ('GET', 'HEAD'):
                payload = {'logs': self._scrub(controller.logs(limit=200))[-256 * 1024:]}
            elif path == '/devices' and method == 'POST':
                body = _payload(handler)
                _fields(body, ('name', 'services', 'permissions'))
                payload = state.create_device(body['name'], body['services'], body['permissions'])
                _json(writer, 201, payload)
                return
            elif path.startswith('/devices/') and method == 'DELETE':
                ident = path.removeprefix('/devices/')
                if not IDENTIFIER.fullmatch(ident):
                    raise RoutingError(400, '设备 ID 无效')
                state.revoke_device(ident)
                payload = {'revoked': True, 'id': ident}
            elif match := re.fullmatch(r'/services/([A-Za-z0-9_-]{1,128})/(tools|probe)', path):
                ident, operation = match.groups()
                if (operation == 'tools' and method not in ('GET', 'HEAD') or
                        operation == 'probe' and method != 'POST'):
                    raise RoutingError(405, '此管理接口不支持该方法')
                if operation == 'probe':
                    _fields(_payload(handler), ())
                payload = self._internal(controller.status(), f'/internal/services/{ident}/{operation}',
                                         'POST' if operation == 'probe' else 'GET')
            else:
                raise RoutingError(404, '未知 MCP Hub 管理接口或请求方法')
        except ValueError as exc:
            status = 409 if type(exc).__name__ == 'HubConfigConflict' else 400
            raise RoutingError(status, self._scrub(exc)) from None
        _json(writer, 200, payload)
