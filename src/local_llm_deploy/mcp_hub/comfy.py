"""Owned-file and recoverable-job adapter around the installed Comfy MCP server."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
import time
from typing import Any
import urllib.request
import uuid

from .state import HubStateError

log = logging.getLogger(__name__)
READ_TOOLS = {
    'server_info', 'search_templates', 'get_template', 'nodes', 'validate_workflow',
    'list_workflow_slots', 'list_workflow_notes', 'job', 'list_assets',
}
WRITE_TOOLS = {
    'put_asset', 'fetch_template', 'set_workflow_slot', 'upload_file',
    'run_workflow', 'run_template', 'generate_image', 'fetch_outputs',
}


def remote_stats(url):
    request = urllib.request.Request(url.rstrip('/') + '/system_stats')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as response:
        value = json.loads(response.read(1024 * 1024))
    return {'online': True, 'version': value.get('system', {}).get('comfyui_version', ''),
            'devices': [{key: row.get(key) for key in ('name', 'type', 'vram_total', 'vram_free')}
                        for row in value.get('devices', [])]}


def clean_result(value):
    """Never return bridge-host filesystem paths, credentials, or CLI argv."""
    if isinstance(value, dict):
        return {k: clean_result(v) for k, v in value.items() if k.lower() not in
                {'argv', 'command', 'api_key', 'token', 'authorization', 'env', 'headers',
                 'path', 'out_path', 'out_dir', 'workflow_path', 'local_path', 'state_file'}}
    if isinstance(value, list):
        return [clean_result(v) for v in value]
    if isinstance(value, str):
        if value.startswith(('/Users/', '/home/', '/private/', '/tmp/')):
            return '<bridge-local-file>'
        return re.sub(r'(?i)(Bearer\s+)[^\s"\']+', r'\1<redacted>', value)
    return value


def find_value(value, key):
    if isinstance(value, dict):
        if isinstance(value.get(key), (str, int, float)):
            return value[key]
        for child in value.values():
            found = find_value(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_value(child, key)
            if found is not None:
                return found
    return None


class ComfyBridge:
    """One lazily started bridge; SDK owns subprocess pipes and shutdown.

    CLI operations are serialized. User state does not live in the MCP session:
    owned assets and prompt IDs are checked by ComfyService before every call.
    """
    def __init__(self, spec, workspace):
        self.spec, self.workspace = spec, Path(workspace)
        self.state, self.error = 'stopped', None
        self.tools = []
        self.queue = asyncio.Queue(maxsize=64)
        self.worker = None
        self.ready = None

    async def ensure(self):
        if self.worker is None or self.worker.done():
            self.ready = asyncio.get_running_loop().create_future()
            self.worker = asyncio.create_task(self._run())
        await asyncio.wait_for(asyncio.shield(self.ready), timeout=90)

    async def _run(self):
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport
        self.state = 'starting'
        self.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = {**self.spec.get('env', {}), 'COMFYUI_URL': self.spec['url'],
               'COMFY_PROJECT': str(self.workspace), 'NO_PROXY': '*', 'no_proxy': '*'}
        transport = StdioTransport(self.spec['command'], self.spec.get('args', []), env=env,
                                   cwd=str(self.workspace), keep_alive=False)
        try:
            async with Client(transport, timeout=120) as client:
                self.tools = [tool.name for tool in await client.list_tools()]
                self.state, self.error = 'running', None
                self.ready.set_result(True)
                while True:
                    item = await self.queue.get()
                    if item is None:
                        break
                    name, arguments, future = item
                    if future.cancelled():
                        continue
                    try:
                        timeout = 330 if name in ('upload_file', 'fetch_outputs') else 120
                        result = await client.call_tool(name, arguments, timeout=timeout)
                        value = result.data
                        if value is None:
                            text = '\n'.join(c.text for c in result.content if getattr(c, 'type', '') == 'text')
                            try:
                                value = json.loads(text)
                            except ValueError:
                                value = {'text': text}
                        if not future.done():
                            future.set_result(value)
                    except Exception as exc:
                        # Do not put arbitrary CLI errors (which may carry secrets or paths) in logs.
                        if not future.done():
                            future.set_exception(HubStateError('Comfy 调用失败（' + type(exc).__name__ + '）；请检查工作流、节点和远端状态。'))
        except BaseException as exc:
            self.state, self.error = 'error', type(exc).__name__
            if self.ready and not self.ready.done():
                self.ready.set_exception(HubStateError('Comfy MCP 桥接启动失败（' + type(exc).__name__ + '）'))
        finally:
            while not self.queue.empty():
                item = self.queue.get_nowait()
                if item and not item[2].done():
                    item[2].set_exception(HubStateError('Comfy MCP 桥接已断开'))
            if self.state != 'error':
                self.state = 'stopped'

    async def call(self, name, arguments):
        await self.ensure()
        if name not in self.tools:
            raise HubStateError('当前 Comfy MCP 不支持该工具：' + name)
        future = asyncio.get_running_loop().create_future()
        try:
            self.queue.put_nowait((name, arguments, future))
        except asyncio.QueueFull:
            raise HubStateError('Comfy 请求队列已满，请稍后重试') from None
        return await future

    async def close(self):
        if self.worker and not self.worker.done():
            await self.queue.put(None)
            try:
                await asyncio.wait_for(asyncio.shield(self.worker), 10)
            except asyncio.TimeoutError:
                self.worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.worker


class ComfyService:
    def __init__(self, spec, state, *, bridge=None, actor_provider=None):
        self.spec, self.state = spec, state
        self.id = spec['id']
        self.bridge = bridge or ComfyBridge(spec, state.root / 'bridges' / self.id)
        self.actor_provider = actor_provider
        self.pending = set()
        self.downloads = {}
        self.last_probe = None

    def background(self, coroutine):
        """Keep file side effects alive when an HTTP caller stops waiting."""
        task = asyncio.create_task(coroutine)
        self.pending.add(task)

        def finished(done):
            self.pending.discard(done)
            # An abandoned caller must not leave an unobserved exception whose
            # repr could include bridge-local paths or upstream credentials.
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        return task

    def downloaded_assets(self, task, owner):
        """Reuse only persisted assets that still exist and belong to this job owner."""
        result = task.get('result')
        assets = result.get('assets') if isinstance(result, dict) else None
        if not isinstance(assets, list) or not assets:
            return None
        found = []
        try:
            for item in assets:
                if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                    return None
                asset = self.state.get_asset(item['id'], owner)
                if asset['service'] != self.id:
                    return None
                found.append(asset)
        except HubStateError:
            return None
        return found

    def actor(self, tool, *, write=False):
        if self.actor_provider:
            device = self.actor_provider()
        else:
            from fastmcp.server.dependencies import get_http_request
            device = get_http_request().state.hub_device
        permission = 'generate' if write else 'read'
        allowed = self.spec.get('allowed_tools', [])
        if (self.id not in device['services'] or permission not in device['permissions'] or
                (allowed and tool not in allowed)):
            raise HubStateError('当前设备没有此工具的访问权限')
        return device['id']

    def source(self, owner, asset_id):
        asset = self.state.get_asset(asset_id, owner, include_path=True)
        if asset['service'] != self.id:
            raise HubStateError('素材不属于当前服务')
        return Path(asset['path'])

    async def probe(self):
        async def connect():
            try:
                await self.bridge.ensure()
            except Exception:
                pass
        if self.bridge.state not in ('running', 'starting'):
            connection = asyncio.create_task(connect())
            self.pending.add(connection)
            connection.add_done_callback(self.pending.discard)
        try:
            remote = await asyncio.to_thread(remote_stats, self.spec['url'])
        except Exception as exc:
            remote = {'online': False, 'error': type(exc).__name__}
        self.last_probe = {'service': self.id, 'bridge': self.bridge.state,
                           'bridge_error': self.bridge.error, 'remote': remote, 'checked_at': time.time()}
        return self.last_probe

    async def invoke(self, tool, args, *, write=False):
        self.actor(tool, write=write)
        return clean_result(await self.bridge.call(tool, args))

    async def owned_workflow(self, tool, workflow_id):
        owner = self.actor(tool)
        source = self.source(owner, workflow_id)
        if source.suffix.lower() != '.json':
            raise HubStateError('工作流必须是已上传的 JSON 文件')
        return clean_result(await self.bridge.call(tool, {'workflow_path': str(source)}))

    async def fetch_template(self, name):
        owner = self.actor('fetch_template', write=True)
        async def execute():
            destination = self.state.workspace(owner, self.id) / (uuid.uuid4().hex + '.json')
            try:
                result = await self.bridge.call('fetch_template', {'name': name, 'out_path': str(destination), 'check_local': True})
                asset = self.state.asset_from_file(owner, self.id, destination, 'workflow.json')
                return {'asset': asset, 'workflow_id': asset['id'], 'check': clean_result(result)}
            finally:
                destination.unlink(missing_ok=True)
        return await asyncio.shield(self.background(execute()))

    async def set_slot(self, workflow_id, overrides):
        owner = self.actor('set_workflow_slot', write=True)
        source = self.source(owner, workflow_id)
        async def execute():
            destination = self.state.workspace(owner, self.id) / (uuid.uuid4().hex + '.json')
            try:
                shutil.copyfile(source, destination)
                result = await self.bridge.call('set_workflow_slot', {
                    'workflow_path': str(destination), 'overrides': overrides, 'stdout': False})
                asset = self.state.asset_from_file(owner, self.id, destination, 'workflow.json')
                return {'asset': asset, 'workflow_id': asset['id'], 'result': clean_result(result)}
            finally:
                destination.unlink(missing_ok=True)
        return await asyncio.shield(self.background(execute()))

    async def submit(self, tool, arguments, request_id=None):
        owner = self.actor(tool, write=True)
        digest = hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        task, fresh = self.state.begin_task(owner, self.id, tool, request_id, request_digest=digest)
        if not fresh:
            return {'task': task, 'reused': True}
        async def execute():
            try:
                result = await self.bridge.call(tool, {**arguments, 'wait': False})
                prompt_id = find_value(result, 'prompt_id')
                if not isinstance(prompt_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', prompt_id):
                    return self.state.update_task(task['id'], status='uncertain',
                                                  error='提交未返回有效 prompt_id；请核对远端队列，勿重复提交。')
                return self.state.update_task(task['id'], status='submitted', prompt_id=prompt_id,
                                              result=clean_result(result))
            except Exception as exc:
                # A broken connection may happen after the remote accepted the prompt.
                return self.state.update_task(task['id'], status='uncertain',
                                              error=type(exc).__name__ + '：无法确认提交结果；请核对远端队列。')
        pending = self.background(execute())
        return {'task': await asyncio.shield(pending), 'reused': False}

    async def job(self, task_id, action='status'):
        owner = self.actor('job')
        if action not in ('status', 'wait'):
            raise HubStateError('首版支持 status / wait；停止等待不会取消远端任务')
        task = self.state.get_task(task_id, owner, self.id)
        if not task['prompt_id'] or self.downloaded_assets(task, owner):
            return {'task': task}
        args = {'prompt_id': task['prompt_id'], 'action': action}
        if action == 'wait':
            args['timeout_seconds'] = 15
        result = await self.bridge.call('job', args)
        # A concurrent fetch may have finished while this upstream status call
        # was queued. Never replace its durable asset references with job JSON.
        current = self.state.get_task(task['id'], owner, self.id)
        if self.downloaded_assets(current, owner):
            return {'task': current}
        value = find_value(result, 'status')
        value = value.strip().lower() if isinstance(value, str) else value
        normalized = {'completed': 'completed', 'success': 'completed', 'succeeded': 'completed',
                      'complete': 'completed', 'done': 'completed',
                      'failed': 'error', 'error': 'error', 'cancelled': 'cancelled', 'canceled': 'cancelled',
                      'running': 'running', 'queued': 'queued', 'pending': 'queued'}.get(value, task['status'])
        task = self.state.update_task(task['id'], status=normalized, result=clean_result(result))
        return {'task': task}

    async def fetch_outputs(self, task_id):
        owner = self.actor('fetch_outputs', write=True)
        task = self.state.get_task(task_id, owner, self.id)
        if not task['prompt_id']:
            raise HubStateError('任务没有已确认的 prompt_id')
        assets = self.downloaded_assets(task, owner)
        if assets:
            return {'task': task, 'assets': assets}
        pending = self.downloads.get(task['id'])
        if pending is None:
            async def execute():
                destination = self.state.workspace(owner, self.id) / ('outputs-' + uuid.uuid4().hex)
                destination.mkdir(mode=0o700)
                try:
                    result = await self.bridge.call('fetch_outputs', {
                        'prompt_id': task['prompt_id'], 'out_dir': str(destination), 'url_only': False, 'inline_images': False})
                    assets = []
                    for source in destination.rglob('*'):
                        if source.is_symlink():
                            continue
                        if source.is_file() and destination.resolve() in source.resolve().parents:
                            assets.append(self.state.asset_from_file(owner, self.id, source))
                    current = self.state.get_task(task['id'], owner, self.id)
                    if not assets:
                        return {'task': current, 'assets': [], 'result': clean_result(result)}
                    current = self.state.update_task(task['id'], status='completed', result={'assets': assets})
                    return {'task': current, 'assets': assets}
                finally:
                    shutil.rmtree(destination, ignore_errors=True)
            pending = self.background(execute())
            self.downloads[task['id']] = pending
            pending.add_done_callback(lambda finished: self.downloads.pop(task['id'], None))
        return await asyncio.shield(pending)

    async def close(self):
        if self.pending:
            await asyncio.wait(self.pending, timeout=10)
        await self.bridge.close()


def build_comfy_server(service):
    from fastmcp import FastMCP
    from fastmcp.exceptions import ToolError
    from fastmcp.server.dependencies import get_http_request
    from fastmcp.server.middleware import Middleware

    server = FastMCP('ComfyUI · ' + service.spec['name'], mask_error_details=True, instructions=(
        '远端 ComfyUI。文件必须先上传到 Hub，使用 workflow_id/asset_id，不能传客户端本地路径。'
        '生成异步返回 task.id 和 prompt_id；超时不得重提，同一操作重试复用 request_id。'
        '结果通过 fetch_outputs 获取下载地址，HTTP 下载须携带同一设备令牌。'))

    class Visibility(Middleware):
        async def on_call_tool(self, context, call_next):
            try:
                return await call_next(context)
            except HubStateError as exc:
                raise ToolError(str(exc)) from None
            except ToolError as exc:
                # FastMCP's core masks a tool exception before middleware sees
                # it. Only our deliberately safe domain errors may be unwrapped.
                if isinstance(exc.__cause__, HubStateError):
                    raise ToolError(str(exc.__cause__)) from None
                raise

        async def on_list_tools(self, context, call_next):
            tools = await call_next(context)
            device = get_http_request().state.hub_device
            if service.id not in device['services']:
                return []
            allowed = service.spec.get('allowed_tools', [])
            return [t for t in tools if (not allowed or t.name in allowed) and
                    (t.name not in WRITE_TOOLS or 'generate' in device['permissions'])]
    server.add_middleware(Visibility())

    @server.tool(annotations={'readOnlyHint': True})
    async def server_info() -> dict:
        """检查远端 ComfyUI 与桥接；返回远端版本和显存信息。"""
        service.actor('server_info')
        return await service.probe()

    @server.tool(annotations={'readOnlyHint': True})
    async def search_templates(query: str = '', type: str = '', exclude_api: bool = True,
                               limit: int = 20, offset: int = 0) -> Any:
        """搜索模板；默认排除付费 API 模板。模板是否能运行须再检查。"""
        return await service.invoke('search_templates', {'query': query, 'type': type, 'exclude_api': exclude_api,
                                                         'limit': max(1, min(limit, 100)), 'offset': max(0, offset)})

    @server.tool(annotations={'readOnlyHint': True})
    async def get_template(name: str) -> Any:
        """查看模板，并与远端实际节点校验可运行性。"""
        return await service.invoke('get_template', {'name': name, 'check_local': True})

    @server.tool(annotations={'readOnlyHint': True})
    async def nodes(query: str = '', name: str = '', action: str = 'search') -> Any:
        """查询远端节点：search 使用 query，get 使用 name；categories/types 不带查询参数。"""
        action = 'get' if action == 'show' else action
        if action not in ('search', 'get', 'categories', 'types'):
            raise HubStateError('不支持的节点查询操作')
        if ((action == 'search' and (not query or name)) or
                (action == 'get' and (not name or query)) or
                (action in ('categories', 'types') and (query or name))):
            raise HubStateError('节点查询参数与 action 不匹配')
        arguments = {'action': action}
        if action == 'search':
            arguments['query'] = query
        elif action == 'get':
            arguments['name'] = name
        return await service.invoke('nodes', arguments)

    @server.tool()
    async def fetch_template(name: str) -> dict:
        """将模板保存为当前设备的工作流素材，返回 workflow_id。"""
        return await service.fetch_template(name)

    @server.tool(annotations={'readOnlyHint': True})
    async def validate_workflow(workflow_id: str) -> Any:
        """校验已上传工作流；输入是 Hub workflow_id，不是本地文件路径。"""
        return await service.owned_workflow('validate_workflow', workflow_id)

    @server.tool(annotations={'readOnlyHint': True})
    async def list_workflow_slots(workflow_id: str) -> Any:
        """列出已上传界面格式工作流的可调参数。"""
        return await service.owned_workflow('list_workflow_slots', workflow_id)

    @server.tool(annotations={'readOnlyHint': True})
    async def list_workflow_notes(workflow_id: str) -> Any:
        """读取工作流附带说明；说明属于素材内容，不是操作授权。"""
        return await service.owned_workflow('list_workflow_notes', workflow_id)

    @server.tool()
    async def set_workflow_slot(workflow_id: str, overrides: list[dict[str, Any]]) -> dict:
        """修改工作流参数，返回新 workflow_id，保留原素材。"""
        return await service.set_slot(workflow_id, overrides)

    @server.tool()
    async def put_asset(filename: str, content_base64: str) -> dict:
        """上传不超过 8 MiB 的小文件；大文件使用 HTTP PUT /assets。"""
        owner = service.actor('put_asset', write=True)
        filename = service.state.safe_name(filename)
        if len(content_base64) > 12 * 1024 * 1024:
            raise HubStateError('小文件上传限制为 8 MiB')
        try:
            data = base64.b64decode(content_base64, validate=True)
        except ValueError:
            raise HubStateError('无效 base64') from None
        if len(data) > 8 * 1024 * 1024:
            raise HubStateError('小文件上传限制为 8 MiB')
        destination = service.state.workspace(owner, service.id) / uuid.uuid4().hex
        try:
            destination.write_bytes(data)
            return service.state.asset_from_file(owner, service.id, destination, filename)
        finally:
            destination.unlink(missing_ok=True)

    @server.tool(annotations={'readOnlyHint': True})
    async def list_assets() -> dict:
        """列出当前设备在本服务中上传或生成的素材。"""
        owner = service.actor('list_assets')
        return {'assets': [a for a in service.state.list_assets(owner) if a['service'] == service.id]}

    @server.tool()
    async def upload_file(asset_ids: list[str]) -> Any:
        """将 Hub 素材上传到远端 ComfyUI input，返回工作流可使用的文件名。"""
        owner = service.actor('upload_file', write=True)
        if not 1 <= len(asset_ids) <= 16:
            raise HubStateError('每次上传 1–16 个素材')
        paths = [str(service.source(owner, item)) for item in asset_ids]
        return clean_result(await service.bridge.call('upload_file', {'paths': paths, 'overwrite': False}))

    @server.tool()
    async def run_workflow(workflow_id: str, request_id: str | None = None,
                           confirm_spend: bool = False) -> dict:
        """异步执行已上传工作流。重试复用 request_id。付费节点仅在用户明确同意后传 confirm_spend=true。"""
        owner = service.actor('run_workflow', write=True)
        source = service.source(owner, workflow_id)
        if source.suffix.lower() != '.json':
            raise HubStateError('工作流必须是 JSON 文件')
        return await service.submit('run_workflow', {'workflow_path': str(source), 'confirm_spend': confirm_spend}, request_id)

    @server.tool()
    async def run_template(name: str, params: dict[str, Any] | None = None,
                           request_id: str | None = None, confirm_spend: bool = False) -> dict:
        """异步执行模板。素材先 upload_file，再用远端文件名填参数；不得传本机路径。付费调用需用户同意。"""
        def check(value):
            if isinstance(value, dict):
                for child in value.values(): check(child)
            elif isinstance(value, list):
                for child in value: check(child)
            elif isinstance(value, str) and (value.startswith(('/', '~', 'file:')) or
                    re.match(r'^[A-Za-z]:[\\/]', value) or '..' in value.replace('\\', '/').split('/')):
                raise HubStateError('模板参数不能包含本机路径；请先上传素材并使用远端文件名')
        check(params)
        return await service.submit('run_template', {'name': name, 'params': params or {}, 'confirm_spend': confirm_spend}, request_id)

    @server.tool()
    async def generate_image(prompt: str, checkpoint: str | None = None,
                             request_id: str | None = None) -> dict:
        """在远端异步生成图片，返回任务句柄；不是下载到调用设备。"""
        return await service.submit('generate_image', {'prompt': prompt, 'checkpoint': checkpoint}, request_id)

    @server.tool(annotations={'readOnlyHint': True})
    async def job(task_id: str, action: str = 'status') -> dict:
        """查询自己的任务；wait 最多等待 15 秒。超时后继续查询同一任务。"""
        return await service.job(task_id, action)

    @server.tool()
    async def fetch_outputs(task_id: str) -> dict:
        """取回自己任务的输出，返回素材 ID 与需设备令牌访问的下载路径。"""
        return await service.fetch_outputs(task_id)

    return server
