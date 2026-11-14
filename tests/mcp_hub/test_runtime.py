"""Wire-level MCP and HTTP asset tests; never call the real ComfyUI."""
import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
import tempfile
import unittest

try:
    import httpx2
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    from local_llm_deploy.mcp_hub.server import HubRuntime
    from local_llm_deploy.mcp_hub.comfy import ComfyService, READ_TOOLS, WRITE_TOOLS
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False

from local_llm_deploy.config import ProjectPaths
from local_llm_deploy.mcp_hub.config import default_config


class FakeBridge:
    def __init__(self):
        self.state, self.error = 'stopped', None
        self.tools = sorted(READ_TOOLS | WRITE_TOOLS)
        self.submits = 0

    async def ensure(self):
        self.state = 'running'

    async def close(self):
        self.state = 'stopped'

    async def call(self, name, arguments):
        if name == 'search_templates':
            return {'rows': [{'name': 'fake-local', 'api': False}]}
        if name == 'run_workflow':
            self.submits += 1
            await asyncio.sleep(.02)
            return {'prompt_id': 'fake-prompt-' + str(self.submits), 'status': 'queued'}
        if name == 'job':
            return {'status': 'completed'}
        if name == 'fetch_outputs':
            (Path(arguments['out_dir']) / 'result.txt').write_text('verified output')
            return {'status': 'completed'}
        if name == 'validate_workflow':
            return {'valid': isinstance(json.loads(Path(arguments['workflow_path']).read_text()), dict)}
        if name == 'fetch_template':
            Path(arguments['out_path']).write_text('{"nodes": []}')
            return {'path': arguments['out_path'], 'local_check': {'runnable': True}}
        return {'ok': True}


@unittest.skipUnless(SDK_AVAILABLE, 'Install requirements/mcp-hub.txt for MCP integration tests')
class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = ProjectPaths(Path(self.temp.name))
        config = default_config()
        config['services'] = [{'id': 'comfyui', 'name': 'Comfy', 'kind': 'comfyui', 'enabled': True,
                               'url': 'http://127.0.0.1:8188', 'command': '/fake', 'args': [], 'env': {}, 'allowed_tools': []}]
        self.bridge = FakeBridge()
        self.runtime = HubRuntime(self.paths, config, comfy_factory=lambda spec, state: ComfyService(spec, state, bridge=self.bridge))
        self.owner = self.runtime.state.create_device('owner', ['comfyui'], ['read', 'generate'])
        self.reader = self.runtime.state.create_device('reader', ['comfyui'], ['read'])
        self.other = self.runtime.state.create_device('other', ['comfyui'], ['read', 'generate'])
        self.app = self.runtime.app()
        self.stack = AsyncExitStack()
        self.started = asyncio.Event()
        self.stopping = asyncio.Event()
        async def serve_lifespan():
            async with self.runtime.lifespan(None):
                self.started.set()
                await self.stopping.wait()
        self.lifespan_task = asyncio.create_task(serve_lifespan())
        await asyncio.wait_for(self.started.wait(), 5)
        self.http = await self.stack.enter_async_context(httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app), base_url='http://localhost:8899', trust_env=False))

    async def asyncTearDown(self):
        await self.stack.aclose()
        self.stopping.set()
        await self.lifespan_task

    def headers(self, who=None):
        return {'Authorization': 'Bearer ' + (who or self.owner)['token']}

    def client(self, who=None, endpoint='comfyui', mode='legacy'):
        def factory(**kwargs):
            return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app), trust_env=False, **kwargs)
        transport = StreamableHttpTransport('http://localhost:8899/mcp/' + endpoint,
                                            headers=self.headers(who), httpx_client_factory=factory)
        return Client(transport, mode=mode)

    async def upload(self, who=None, data=b'{"nodes": []}'):
        response = await self.http.put('/assets?service=comfyui&filename=workflow.json', content=data, headers=self.headers(who))
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def test_http_boundary_and_internal_control(self):
        self.assertEqual((await self.http.get('/health')).status_code, 200)
        self.assertEqual((await self.http.get('/assets')).status_code, 401)
        self.assertEqual((await self.http.get('/assets', headers={**self.headers(), 'Origin': 'https://evil.invalid'})).status_code, 403)
        self.assertEqual((await self.http.get('/assets', headers={**self.headers(), 'Host': 'evil.invalid'})).status_code, 403)
        self.assertEqual((await self.http.get('/internal/status')).status_code, 403)
        internal = {'X-Hub-Internal-Key': self.runtime.internal_key}
        self.assertEqual((await self.http.get('/internal/status', headers=internal)).status_code, 200)
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app, client=('192.168.0.23', 9999)),
                                      base_url='http://localhost:8899') as remote:
            self.assertEqual((await remote.get('/internal/status', headers=internal)).status_code, 403)

    async def test_owned_assets_range_and_revocation(self):
        asset = await self.upload(data=b'abcdef')
        response = await self.http.get(asset['download_path'], headers={**self.headers(), 'Range': 'bytes=1-3'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b'bcd')
        self.assertEqual((await self.http.get(asset['download_path'], headers=self.headers(self.other))).status_code, 400)
        self.assertEqual((await self.http.put('/assets?service=comfyui&filename=x', content=b'x', headers=self.headers(self.reader))).status_code, 400)
        self.runtime.state.revoke_device(self.owner['device']['id'])
        self.assertEqual((await self.http.get(asset['download_path'], headers=self.headers())).status_code, 401)

    async def test_sdk_tool_discovery_and_generate_scope(self):
        async with self.client(self.reader) as client:
            names = {tool.name for tool in await client.list_tools()}
            self.assertIn('search_templates', names)
            self.assertNotIn('run_workflow', names)
            response = await client.call_tool('search_templates', {'query': 'fake'})
            self.assertEqual(response.data['rows'][0]['name'], 'fake-local')
            with self.assertRaises(Exception):
                await client.call_tool('run_workflow', {'workflow_id': 'unknown'})
        self.assertEqual(self.bridge.submits, 0)

    async def test_submit_poll_download_and_deduplicate(self):
        asset = await self.upload()
        async with self.client() as client:
            check = await client.call_tool('validate_workflow', {'workflow_id': asset['id']})
            self.assertTrue(check.data['valid'])
            result = await client.call_tool('run_workflow', {'workflow_id': asset['id'], 'request_id': 'same-request'})
            task_id = result.data['task']['id']
            repeated = await client.call_tool('run_workflow', {'workflow_id': asset['id'], 'request_id': 'same-request'})
            self.assertTrue(repeated.data['reused'])
            self.assertEqual(self.bridge.submits, 1)
            with self.assertRaises(Exception):
                await client.call_tool('run_workflow', {
                    'workflow_id': asset['id'], 'request_id': 'same-request', 'confirm_spend': True})
            self.assertEqual(self.bridge.submits, 1)
            status = await client.call_tool('job', {'task_id': task_id})
            self.assertEqual(status.data['task']['status'], 'completed')
            outputs = await client.call_tool('fetch_outputs', {'task_id': task_id})
            output = outputs.data['assets'][0]
            response = await self.http.get(output['download_path'], headers=self.headers())
            self.assertEqual(response.content, b'verified output')
        async with self.client(self.other) as other:
            with self.assertRaises(Exception):
                await other.call_tool('job', {'task_id': task_id})
            with self.assertRaises(Exception):
                await other.call_tool('run_workflow', {'workflow_id': asset['id']})
        self.assertEqual(self.bridge.submits, 1)

    async def test_modern_and_aggregate_endpoint(self):
        async with self.client(endpoint='default', mode='2026-07-28') as client:
            tools = {tool.name for tool in await client.list_tools()}
            self.assertIn('comfyui_search_templates', tools)
            response = await client.call_tool('comfyui_search_templates', {})
            self.assertTrue(response.data['rows'])

    async def test_disconnected_submit_is_recorded(self):
        service = self.runtime.services['comfyui']
        service.actor_provider = lambda: self.owner['device']
        pending = asyncio.create_task(service.submit('run_workflow', {'workflow_path': '/fake'}, 'disconnect'))
        await asyncio.sleep(.005)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        await asyncio.sleep(.04)
        self.assertEqual(self.runtime.state.list_tasks()[0]['status'], 'submitted')
        self.assertEqual(self.bridge.submits, 1)


if __name__ == '__main__':
    unittest.main()
