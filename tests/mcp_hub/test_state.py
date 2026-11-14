import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from local_llm_deploy.mcp_hub.state import HubState, HubStateError


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = HubState(Path(self.temp.name))

    def test_grant_scope_and_revocation_survive_restart(self):
        result = self.state.create_device('剪辑电脑', ['comfyui'], ['read'])
        token = result['token']
        second = HubState(Path(self.temp.name))
        self.assertEqual(second.authenticate(token, 'comfyui')['name'], '剪辑电脑')
        self.assertIsNone(second.authenticate(token, 'other'))
        self.assertIsNone(second.authenticate(token, 'comfyui', 'generate'))
        self.assertNotIn(token, second.db_path.read_bytes().decode('latin1'))
        second.revoke_device(result['device']['id'])
        self.assertIsNone(self.state.authenticate(token))

    def test_request_id_deduplicates_concurrent_submit(self):
        def submit(_):
            return self.state.begin_task('owner', 'comfyui', 'run_workflow', 'request-1')
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(submit, range(4)))
        self.assertEqual(sum(fresh for _, fresh in results), 1)
        self.assertEqual(len({task['id'] for task, _ in results}), 1)
        task = results[0][0]
        self.state.update_task(task['id'], prompt_id='prompt1', status='submitted')
        self.assertEqual(self.state.get_task('prompt1', 'owner')['id'], task['id'])
        with self.assertRaises(HubStateError):
            self.state.get_task('prompt1', 'stranger')
        with self.assertRaises(HubStateError):
            self.state.begin_task('owner', 'comfyui', 'run_template', 'request-1')

    def test_files_are_owned_and_symlinks_rejected(self):
        source = Path(self.temp.name) / 'input.json'
        source.write_text('{"workflow": true}')
        asset = self.state.asset_from_file('owner', 'comfyui', source)
        self.assertNotIn('path', asset)
        with self.assertRaises(HubStateError):
            self.state.get_asset(asset['id'], 'stranger')
        with self.assertRaises(HubStateError):
            self.state.asset_from_file('owner', 'comfyui', source, '../escape')
        target = Path(self.state.get_asset(asset['id'], 'owner', include_path=True)['path'])
        target.unlink()
        target.symlink_to(source)
        with self.assertRaises(HubStateError):
            self.state.get_asset(asset['id'], 'owner')

    def test_crash_does_not_automatically_resubmit(self):
        task, _ = self.state.begin_task('owner', 'comfyui', 'run_workflow', 'request-2')
        self.state.recover_submitting()
        existing, fresh = self.state.begin_task('owner', 'comfyui', 'run_workflow', 'request-2')
        self.assertFalse(fresh)
        self.assertEqual(existing['status'], 'uncertain')
        self.assertEqual(existing['id'], task['id'])


if __name__ == '__main__':
    unittest.main()
