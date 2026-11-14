"""Independent authenticated Streamable HTTP MCP Hub (optional runtime deps)."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import hmac
import ipaddress
import json
import logging
from pathlib import Path
import socket
import time
from urllib.parse import urlsplit
import uuid

from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route
import uvicorn

from local_llm_deploy.config import project_paths
from .config import HubConfigStore
from .state import HubState, HubStateError, MAX_ASSET_BYTES
from .comfy import ComfyService, READ_TOOLS, WRITE_TOOLS, build_comfy_server

log = logging.getLogger('mcp-hub')
MAX_MCP_BODY = 16 * 1024 * 1024


class Boundary:
    """Authenticate each HTTP request, before the SDK creates a transport.

    Host/Origin protection and authentication also cover upload/download routes.
    Device grants are re-read per request so revocation applies immediately.
    """
    def __init__(self, app, runtime):
        self.app, self.runtime = app, runtime
        self.hosts = {'localhost', '127.0.0.1', '::1', socket.gethostname().lower()}
        try:
            self.hosts.update(row[4][0].split('%')[0] for row in socket.getaddrinfo(socket.gethostname(), None))
        except OSError:
            pass
        self.hosts.update(runtime.config.get('allowed_hosts', []))
        public = runtime.config['listen'].get('public_url')
        if public:
            self.hosts.add(urlsplit(public).hostname)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        headers = {}
        for key, value in scope['headers']:
            key = key.decode('latin1').lower()
            headers.setdefault(key, []).append(value.decode('latin1'))
        async def reject(code, message):
            response = JSONResponse({'error': message}, code, headers={'Cache-Control': 'no-store'})
            await response(scope, receive, send)
        if any(len(headers.get(k, [])) > 1 for k in ('host', 'origin', 'authorization', 'content-length', 'x-hub-internal-key')):
            return await reject(400, 'Duplicate request headers')
        authority = headers.get('host', [''])[0]
        try:
            host = urlsplit('http://' + authority)
            names = self.hosts | {scope.get('server', ('', 0))[0]}
            if (host.hostname not in names or host.username or host.password or host.path or host.query or host.fragment
                    or any(c.isspace() for c in authority)):
                raise ValueError
            host.port
        except ValueError:
            return await reject(403, 'Untrusted Host')
        origin = headers.get('origin', [''])[0]
        trusted = set(self.runtime.config.get('allowed_origins', [])) | {scope.get('scheme', 'http') + '://' + authority}
        if origin and origin not in trusted:
            return await reject(403, 'Untrusted Origin')

        async def cors_send(message):
            if message['type'] == 'http.response.start':
                message['headers'] = list(message.get('headers', [])) + [(b'cache-control', b'no-store')]
                if origin:
                    message['headers'] += [(b'access-control-allow-origin', origin.encode()),
                                           (b'vary', b'Origin'),
                                           (b'access-control-expose-headers', b'Mcp-Session-Id, MCP-Protocol-Version, Content-Disposition')]
            await send(message)

        path = scope['path']
        if path.startswith('/internal/'):
            try:
                local = ipaddress.ip_address(scope.get('client', ('', 0))[0]).is_loopback
            except ValueError:
                local = False
            key = headers.get('x-hub-internal-key', [''])[0]
            if not local or not hmac.compare_digest(key, self.runtime.internal_key):
                return await reject(403, 'Internal endpoint')
        elif path == '/health':
            pass
        elif scope['method'] == 'OPTIONS' and origin:
            response = JSONResponse({}, 200, headers={
                'Access-Control-Allow-Methods': 'GET, HEAD, POST, PUT, DELETE, OPTIONS',
                'Access-Control-Allow-Headers': 'Authorization, Content-Type, Accept, MCP-Protocol-Version, Mcp-Session-Id',
                'Access-Control-Max-Age': '600'})
            return await response(scope, receive, cors_send)
        else:
            authorization = headers.get('authorization', [''])[0]
            token = authorization[7:] if authorization.startswith('Bearer ') else ''
            service_id = path.split('/')[2] if path.startswith('/mcp/') and len(path.split('/')) == 3 else None
            if service_id == 'default':
                service_id = None
            device = self.runtime.state.authenticate(token, service_id)
            if not device:
                return await reject(401, 'Valid device token and service access required')
            scope.setdefault('state', {})['hub_device'] = device
        limit = MAX_ASSET_BYTES if path == '/assets' and scope['method'] == 'PUT' else MAX_MCP_BODY
        try:
            length = int(headers.get('content-length', ['0'])[0])
            if length < 0 or length > limit:
                return await reject(413, 'Request body too large')
        except ValueError:
            return await reject(400, 'Invalid Content-Length')
        # MCP messages are small JSON documents. Bound chunked requests too,
        # before handing any of them to the protocol engine.
        if path.startswith('/mcp/') and scope['method'] == 'POST':
            body = bytearray()
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                body.extend(message.get('body', b''))
                if len(body) > MAX_MCP_BODY:
                    return await reject(413, 'MCP request body too large')
                if not message.get('more_body'):
                    break
            delivered = False
            async def buffered():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {'type': 'http.request', 'body': bytes(body), 'more_body': False}
                return await receive()
            return await self.app(scope, buffered, cors_send)
        return await self.app(scope, receive, cors_send)


class MCPDispatch:
    def __init__(self, apps):
        self.apps = apps

    async def __call__(self, scope, receive, send):
        key = scope['path'].removeprefix('/mcp/').rstrip('/')
        app = self.apps.get(key)
        if app is None:
            return await JSONResponse({'error': 'Unknown MCP service'}, 404)(scope, receive, send)
        # Keep the full path and all SDK middleware, including request context.
        return await app(scope, receive, send)


class HubRuntime:
    def __init__(self, paths, config=None, *, comfy_factory=ComfyService):
        self.paths = paths
        self.config = config if config is not None else HubConfigStore(paths).load()
        self.state = HubState(paths)
        self.internal_key = self.state.internal_key()
        self.started = time.time()
        self.services, self.servers, self.apps = {}, {}, {}
        self.background = set()
        self.upload_slots = asyncio.Semaphore(4)
        aggregate = FastMCP('Local MCP Hub', instructions='按服务命名空间提供工具；每台设备只看到获授权的服务。')
        for spec in self.config['services']:
            if not spec['enabled']:
                continue
            if spec['kind'] == 'comfyui':
                service = comfy_factory(spec, self.state)
                self.services[spec['id']] = service
                server = build_comfy_server(service)
            else:
                from .generic import build_generic_server
                workspace = self.state.root / 'bridges' / spec['id']
                server = build_generic_server(spec, workspace=workspace)
            self.servers[spec['id']] = server
            self.apps[spec['id']] = server.http_app(path='/mcp/' + spec['id'], stateless_http=True,
                                                   host_origin_protection=False)
            aggregate.mount(server, namespace=spec['id'])
        self.apps['default'] = aggregate.http_app(path='/mcp/default', stateless_http=True,
                                                   host_origin_protection=False)

    def spawn(self, coroutine):
        async def safe():
            try:
                await coroutine
            except Exception as exc:
                log.info('Background connection unavailable (%s)', type(exc).__name__)
        task = asyncio.create_task(safe())
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    @asynccontextmanager
    async def lifespan(self, app):
        self.state.recover_submitting()
        async with AsyncExitStack() as stack:
            for child in self.apps.values():
                await stack.enter_async_context(child.router.lifespan_context(child))
            for service in self.services.values():
                self.spawn(service.bridge.ensure())
            try:
                yield
            finally:
                for service in self.services.values():
                    await service.close()
                for task in list(self.background):
                    task.cancel()
                if self.background:
                    await asyncio.gather(*self.background, return_exceptions=True)

    async def health(self, request):
        return JSONResponse({'status': 'ready', 'service': 'mcp-hub'})

    async def internal_status(self, request):
        rows = []
        for spec in self.config['services']:
            service = self.services.get(spec['id'])
            status = 'disabled' if not spec['enabled'] else service.bridge.state if service else 'ready'
            exposed = READ_TOOLS | WRITE_TOOLS
            if spec.get('allowed_tools'):
                exposed = exposed.intersection(spec['allowed_tools'])
            rows.append({'id': spec['id'], 'name': spec['name'], 'kind': spec['kind'], 'status': status,
                         'available': status in ('ready', 'running'),
                         'tool_count': len(exposed) if service else None,
                         'bridge': status if service else None, 'remote': (service.last_probe or {}).get('remote') if service else None})
        return JSONResponse({'available': True, 'applied_revision': self.config['revision'],
                             'started_at': self.started, 'services': rows})

    async def internal_tools(self, request):
        key = request.path_params['service']
        server = self.servers.get(key)
        if not server:
            return JSONResponse({'error': 'Service disabled or not found'}, 404)
        try:
            tools = await asyncio.wait_for(server.list_tools(run_middleware=False), 6)
            spec = next(item for item in self.config['services'] if item['id'] == key)
            allowed = spec.get('allowed_tools', [])
            if allowed:
                tools = [tool for tool in tools if tool.name in allowed]
            return JSONResponse({'tools': [{'name': t.name, 'description': t.description,
                                           'inputSchema': t.parameters} for t in tools]})
        except Exception as exc:
            return JSONResponse({'error': 'Tool discovery unavailable: ' + type(exc).__name__}, 503)

    async def internal_probe(self, request):
        key = request.path_params['service']
        service = self.services.get(key)
        if service:
            return JSONResponse(await service.probe())
        return await self.internal_tools(request)

    def check_service(self, device, service, *, write=False):
        if service not in self.servers or service not in device['services'] or (write and 'generate' not in device['permissions']):
            raise HubStateError('当前设备没有此服务权限')

    async def assets(self, request):
        device = request.state.hub_device
        if request.method == 'GET':
            assets = [item for item in self.state.list_assets(device['id']) if item['service'] in device['services']
                      and item['service'] in self.servers]
            return JSONResponse({'assets': assets})
        service = request.query_params.get('service', '')
        self.check_service(device, service, write=True)
        filename = self.state.safe_name(request.query_params.get('filename', ''))
        destination = self.state.workspace(device['id'], service) / ('upload-' + uuid.uuid4().hex)
        async with self.upload_slots:
            try:
                length = 0
                with destination.open('xb') as output:
                    async for chunk in request.stream():
                        length += len(chunk)
                        if length > MAX_ASSET_BYTES:
                            return JSONResponse({'error': 'File exceeds 512 MiB'}, 413)
                        output.write(chunk)
                asset = await asyncio.to_thread(self.state.asset_from_file, device['id'], service, destination, filename)
                return JSONResponse(asset, 201)
            finally:
                destination.unlink(missing_ok=True)

    async def download(self, request):
        device = request.state.hub_device
        asset = self.state.get_asset(request.path_params['asset'], device['id'], include_path=True)
        self.check_service(device, asset['service'])
        return FileResponse(asset['path'], media_type=asset['mime_type'], filename=asset['name'],
                            headers={'X-Content-Type-Options': 'nosniff'})

    async def tasks(self, request):
        device = request.state.hub_device
        return JSONResponse({'tasks': [t for t in self.state.list_tasks(owner=device['id'])
                                       if t['service'] in device['services'] and t['service'] in self.servers]})

    def app(self):
        async def state_error(request, exc):
            return JSONResponse({'error': str(exc)}, 400)
        app = Starlette(routes=[
            Route('/health', self.health, methods=['GET', 'HEAD']),
            Route('/internal/status', self.internal_status),
            Route('/internal/services/{service}/tools', self.internal_tools),
            Route('/internal/services/{service}/probe', self.internal_probe, methods=['POST']),
            Route('/assets', self.assets, methods=['GET', 'PUT']),
            Route('/assets/{asset}', self.download, methods=['GET', 'HEAD']),
            Route('/tasks', self.tasks),
            Route('/mcp/{service}', MCPDispatch(self.apps), methods=['GET', 'POST', 'DELETE']),
        ], lifespan=self.lifespan, exception_handlers={HubStateError: state_error})
        return Boundary(app, self)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Independent LAN MCP Hub')
    parser.add_argument('--project-root')
    parser.add_argument('--host')
    parser.add_argument('--port', type=int)
    args = parser.parse_args(argv)
    paths = project_paths(args.project_root)
    runtime = HubRuntime(paths)
    host = args.host or runtime.config['listen']['host']
    port = args.port or runtime.config['listen']['port']
    # One process owns the task records and bridge lifecycle. Do not add workers.
    uvicorn.run(runtime.app(), host=host, port=port, proxy_headers=False, access_log=False,
                log_level='info', timeout_graceful_shutdown=20)


if __name__ == '__main__':
    main()
