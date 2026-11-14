"""Disconnected clients must not interrupt owned-file registration or cleanup."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from local_llm_deploy.config import ProjectPaths
from local_llm_deploy.mcp_hub.comfy import ComfyBridge, ComfyService, build_comfy_server
from local_llm_deploy.mcp_hub.state import HubState

try:
    import fastmcp
    SDK_AVAILABLE = True
except ImportError:
    SDK_AVAILABLE = False


class TransferBridge:
    def __init__(self):
        self.state, self.error = 'running', None
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.job_started = asyncio.Event()
        self.job_release = asyncio.Event()
        self.blocked = None
        self.block_job = False
        self.path = None
        self.failure = None
        self.job_status = 'completed'

    async def ensure(self):
        pass

    async def close(self):
        pass

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == 'job':
            self.job_started.set()
            if self.block_job:
                await self.job_release.wait()
            return {'status': self.job_status}
        if name in ('fetch_outputs', 'fetch_template', 'set_workflow_slot'):
            key = {'fetch_outputs': 'out_dir', 'fetch_template': 'out_path',
                   'set_workflow_slot': 'workflow_path'}[name]
            self.path = Path(arguments[key])
            target = self.path / 'result.txt' if name == 'fetch_outputs' else self.path
            if name != 'set_workflow_slot':
                target.write_text('partial')
            self.started.set()
            if self.blocked == name:
                await self.release.wait()
            # A cancelled caller must not have removed this in-flight file.
            if not target.is_file():
                raise AssertionError('Caller removed an active transfer file')
            if self.failure:
                raise self.failure
            target.write_text('complete output' if name == 'fetch_outputs' else '{"updated": true}')
            return {'status': 'completed', 'path': str(target)}
        if name == 'nodes':
            expected = {'search': {'action', 'query'}, 'get': {'action', 'name'},
                        'categories': {'action'}, 'types': {'action'}}[arguments['action']]
            if set(arguments) != expected:
                raise AssertionError('Incompatible upstream nodes parameters')
            return {'rows': []}
        return {'ok': True}


class ComfyTransferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temp.name))
        self.state = HubState(self.paths)
        self.device = self.state.create_device('owner', ['comfyui'], ['read', 'generate'])['device']
        self.spec = {'id': 'comfyui', 'name': 'fixture', 'command': 'fixture-comfy', 'args': [],
                     'env': {}, 'url': 'http://127.0.0.1:8188', 'allowed_tools': []}
        self.bridge = TransferBridge()
        self.service = ComfyService(self.spec, self.state, bridge=self.bridge,
                                    actor_provider=lambda: self.device)
        task, _ = self.state.begin_task(self.device['id'], 'comfyui', 'run_workflow', 'fixture')
        self.task = self.state.update_task(task['id'], status='submitted', prompt_id='fixture-prompt')

    async def asyncTearDown(self):
        self.bridge.release.set()
        self.bridge.job_release.set()
        if self.service.pending:
            await asyncio.wait_for(asyncio.gather(*self.service.pending, return_exceptions=True), 2)
        await self.service.close()
        self.temp.cleanup()

    def workflow(self):
        source = self.paths.root / 'source.json'
        source.write_text('{"original": true}')
        return self.state.asset_from_file(self.device['id'], 'comfyui', source)

    async def test_cancelled_download_finishes_once_and_survives_service_recreation(self):
        self.bridge.blocked = 'fetch_outputs'
        first = asyncio.create_task(self.service.fetch_outputs(self.task['id']))
        await asyncio.wait_for(self.bridge.started.wait(), 1)
        directory = self.bridge.path
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual((directory / 'result.txt').read_text(), 'partial')
        second = asyncio.create_task(self.service.fetch_outputs(self.task['id']))
        await asyncio.sleep(0)
        self.assertEqual(len(self.bridge.calls), 1)
        self.bridge.release.set()
        outputs = await asyncio.wait_for(second, 1)
        self.assertFalse(directory.exists())
        asset_id = outputs['assets'][0]['id']
        asset = self.state.get_asset(asset_id, self.device['id'], include_path=True)
        self.assertEqual(Path(asset['path']).read_text(), 'complete output')
        self.assertEqual((await self.service.fetch_outputs(self.task['id']))['assets'][0]['id'], asset_id)
        recreated = ComfyService(self.spec, HubState(self.paths), bridge=self.bridge,
                                actor_provider=lambda: self.device)
        self.assertEqual((await recreated.fetch_outputs(self.task['id']))['assets'][0]['id'], asset_id)
        self.assertEqual((await recreated.job(self.task['id']))['task']['result']['assets'][0]['id'], asset_id)
        self.assertEqual(len(self.bridge.calls), 1)

    async def test_concurrent_job_response_does_not_overwrite_downloaded_assets(self):
        self.bridge.block_job = True
        self.bridge.job_status = 'running'
        pending_job = asyncio.create_task(self.service.job(self.task['id']))
        await asyncio.wait_for(self.bridge.job_started.wait(), 1)
        output = await self.service.fetch_outputs(self.task['id'])
        self.bridge.job_release.set()
        result = await asyncio.wait_for(pending_job, 1)
        self.assertEqual(result['task']['status'], 'completed')
        self.assertEqual(result['task']['result']['assets'], output['assets'])
        self.assertEqual(self.state.get_task(self.task['id'])['result']['assets'], output['assets'])

    async def test_cancelled_template_fetch_registers_file_before_cleanup(self):
        self.bridge.blocked = 'fetch_template'
        waiting = asyncio.create_task(self.service.fetch_template('fixture'))
        await asyncio.wait_for(self.bridge.started.wait(), 1)
        temporary = self.bridge.path
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertTrue(temporary.exists())
        self.bridge.release.set()
        await asyncio.wait_for(asyncio.gather(*self.service.pending), 1)
        self.assertFalse(temporary.exists())
        assets = self.state.list_assets(self.device['id'])
        self.assertEqual(len(assets), 1)
        saved = self.state.get_asset(assets[0]['id'], self.device['id'], include_path=True)
        self.assertEqual(json.loads(Path(saved['path']).read_text()), {'updated': True})

    async def test_cancelled_slot_update_keeps_original_and_registers_finished_copy(self):
        original = self.workflow()
        self.bridge.blocked = 'set_workflow_slot'
        waiting = asyncio.create_task(self.service.set_slot(original['id'], [{'slot': 'prompt', 'value': 'new'}]))
        await asyncio.wait_for(self.bridge.started.wait(), 1)
        temporary = self.bridge.path
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertEqual(json.loads(temporary.read_text()), {'original': True})
        self.bridge.release.set()
        await asyncio.wait_for(asyncio.gather(*self.service.pending), 1)
        self.assertFalse(temporary.exists())
        assets = self.state.list_assets(self.device['id'])
        self.assertEqual(len(assets), 2)
        original_path = self.state.get_asset(original['id'], self.device['id'], include_path=True)['path']
        self.assertEqual(json.loads(Path(original_path).read_text()), {'original': True})

    async def test_failed_fetch_cleans_up_and_allows_retry(self):
        self.bridge.failure = OSError('fake transfer failure')
        with self.assertRaises(OSError):
            await self.service.fetch_outputs(self.task['id'])
        self.assertFalse(self.bridge.path.exists())
        self.assertFalse(self.service.downloads)
        self.assertEqual(self.state.list_assets(self.device['id']), [])
        self.bridge.failure = None
        result = await self.service.fetch_outputs(self.task['id'])
        self.assertEqual(len(result['assets']), 1)
        self.assertEqual(len(self.bridge.calls), 2)

    async def test_upstream_terminal_status_aliases_are_normalized(self):
        for value in (' COMPLETE ', 'Done', 'SuCcEeDeD'):
            self.bridge.job_status = value
            result = await self.service.job(self.task['id'])
            self.assertEqual(result['task']['status'], 'completed')

    @unittest.skipUnless(SDK_AVAILABLE, 'Install MCP Hub dependencies for SDK checks')
    async def test_nodes_wrapper_only_sends_parameters_consumed_by_upstream(self):
        server = build_comfy_server(self.service)
        for arguments, expected in [
                ({'query': 'sampler'}, {'action': 'search', 'query': 'sampler'}),
                ({'action': 'get', 'name': 'KSampler'}, {'action': 'get', 'name': 'KSampler'}),
                ({'action': 'show', 'name': 'KSampler'}, {'action': 'get', 'name': 'KSampler'}),
                ({'action': 'categories'}, {'action': 'categories'}),
                ({'action': 'types'}, {'action': 'types'})]:
            await server.call_tool('nodes', arguments)
            self.assertEqual(self.bridge.calls[-1], ('nodes', expected))

    @unittest.skipUnless(SDK_AVAILABLE, 'Install MCP Hub dependencies for SDK checks')
    async def test_unexpected_file_errors_are_masked_by_sdk(self):
        server = build_comfy_server(self.service)
        with self.assertLogs('fastmcp.server.server', level='ERROR'):
            with self.assertRaisesRegex(Exception, '素材不存在或不属于当前设备'):
                await server.call_tool('validate_workflow', {'workflow_id': 'not-owned'})
        self.bridge.failure = OSError('/private/secret-fixture/workflow.json: token=private-fixture')
        with self.assertLogs('fastmcp.server.server', level='ERROR'):
            with self.assertRaises(Exception) as raised:
                await server.call_tool('fetch_template', {'name': 'fixture'})
        self.assertNotIn('secret-fixture', str(raised.exception))
        self.assertNotIn('private-fixture', str(raised.exception))


@unittest.skipUnless(SDK_AVAILABLE, 'Install MCP Hub dependencies for SDK checks')
class BridgeTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_transfers_outlive_upstream_three_hundred_second_budget(self):
        budgets = {}
        class Client:
            def __init__(self, *args, **kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_args):
                pass
            async def list_tools(self):
                return [SimpleNamespace(name=name) for name in ('upload_file', 'fetch_outputs', 'server_info')]
            async def call_tool(self, name, arguments, *, timeout):
                budgets[name] = timeout
                if name in ('upload_file', 'fetch_outputs') and timeout <= 300:
                    raise TimeoutError('Client expires before upstream transfer deadline')
                return SimpleNamespace(data={'ok': True}, content=[])
        with tempfile.TemporaryDirectory() as directory:
            bridge = ComfyBridge({'command': 'fake', 'url': 'http://127.0.0.1:8188'}, directory)
            with patch('fastmcp.Client', Client), patch('fastmcp.client.transports.StdioTransport', return_value=object()):
                try:
                    for name in ('upload_file', 'fetch_outputs', 'server_info'):
                        self.assertTrue((await bridge.call(name, {}))['ok'])
                finally:
                    await bridge.close()
        self.assertEqual(budgets['upload_file'], 330)
        self.assertEqual(budgets['fetch_outputs'], 330)
        self.assertEqual(budgets['server_info'], 120)


if __name__ == '__main__':
    unittest.main()
