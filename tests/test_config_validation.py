"""Invalid JSON types must fail as configuration errors before coercion or hashing."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from local_llm_deploy.config import ConfigError, ProjectPaths, normalize_models
from local_llm_deploy.registry import Registry, main as registry_main


class ConfigValidationTests(unittest.TestCase):
    def assert_invalid(self, config):
        with self.assertRaises(ConfigError) as error:
            normalize_models({'model': config})
        self.assertIn('model', str(error.exception))

    def test_known_scalar_fields_reject_containers_and_coercions(self):
        fields = ('type', 'backend', 'management', 'alias', 'backend_model', 'ollama_model',
                  'repo_id', 'repo_name', 'default_quant', 'host', 'external_host',
                  'download_source', 'download_target_mode', 'revision', 'hf_revision',
                  'model_path', 'cpp_dir', 'engine_profile', 'engine', 'mmproj',
                  'chat_template_file', 'health_path', 'full_model_name', 'startup_hint')
        for field in fields:
            for value in ([], {}, False, 42):
                with self.subTest(field=field, value=value):
                    self.assert_invalid({field: value})

    def test_known_containers_reject_wrong_shapes(self):
        for field in ('params', 'runtime', 'quants'):
            for value in (None, [], False, 2, 'text'):
                with self.subTest(field=field, value=value):
                    self.assert_invalid({field: value})
        for field in ('capabilities', 'default_for', 'endpoints'):
            for value in ({}, 'chat', False, [['chat']], [None]):
                with self.subTest(field=field, value=value):
                    self.assert_invalid({field: value})
        for value in ('false', 0, [], {}):
            with self.subTest(external_backend=value):
                self.assert_invalid({'external_backend': value})

    def test_quant_names_and_patterns_validate_before_path_usage(self):
        for bad in ({'quants': {'Q4': []}}, {'quants': {'Q4': {'pattern': {}}}},
                    {'quants': {'Q4': {'patterns': [None]}}}, {'download_allow_patterns': [2]},
                    {'quants': {'Q4': {'size_gb': 'large'}}}):
            with self.subTest(config=bad):
                self.assert_invalid(bad)

    def test_inference_parameters_reject_wrong_scalar_types(self):
        for field in ('ctx_size', 'max_concurrent', 'max_documents', 'n_predict', 'dimensions'):
            for value in ('5', [], {}, True, 1.2):
                with self.subTest(field=field, value=value):
                    self.assert_invalid({'params': {field: value}})
        for field in ('temp', 'top_p', 'repeat_penalty', 'kv_budget_ratio'):
            for value in ('0.5', [], {}, True, float('nan'), float('inf')):
                with self.subTest(field=field, value=value):
                    self.assert_invalid({'params': {field: value}})
        for field in ('language', 'task', 'response_format', 'default_task'):
            for value in ([], {}, False, 42):
                with self.subTest(field=field, value=value):
                    self.assert_invalid({'params': {field: value}})

    def test_runtime_known_fields_are_checked_before_construction(self):
        for field in ('python', 'cpp_dir', 'api_key_file', 'ready_path', 'management'):
            for value in ([], {}, False, 3):
                with self.subTest(field=field, value=value):
                    self.assert_invalid({'runtime': {field: value}})
        for value in (None, True, '180', {}, [], float('inf'), 10 ** 1000):
            with self.subTest(timeout_type=type(value).__name__):
                self.assert_invalid({'runtime': {'ready_timeout': value}})
        self.assert_invalid({'runtime': {'ready_path': None}})
        self.assert_invalid({'runtime': {'run_at_load': 'yes'}})
        self.assert_invalid({'runtime': {'keep_alive': []}})

    def test_optional_nulls_extensions_and_valid_pattern_forms_survive(self):
        raw = {'alias': None, 'repo_name': None, 'endpoints': None, 'backend': 'custom-adapter',
               'params': {'temp': 0, 'n_predict': -1, 'dimensions': None, 'extension': {'x': []}},
               'quants': {'Q4': {'pattern': ['*.gguf'], 'size_gb': 1.2}},
               'runtime': {'python': None, 'keep_alive': {'SuccessfulExit': False}},
               'download_allow_patterns': '*.json', 'extension': {'opaque': [None, {'x': True}]}}
        spec = normalize_models({'model': raw})['model']
        self.assertEqual(spec.alias, 'model')
        self.assertEqual(spec.raw['extension'], raw['extension'])
        self.assertEqual(spec.raw['params']['n_predict'], -1)
        self.assertEqual(spec.raw['runtime']['keep_alive'], {'SuccessfulExit': False})

    def test_invalid_identity_json_reports_error_and_retains_cached_registry(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = ProjectPaths(Path(temporary))
            paths.registry.write_text(json.dumps({'model': {'alias': 'good'}}))
            registry = Registry(paths)
            registry.load_raw()
            paths.registry.write_text(json.dumps({'model': {'backend_model': []}}))
            with self.assertRaises(ConfigError):
                registry.load_raw(refresh=True)
            self.assertEqual(registry.load_raw()['model']['alias'], 'good')
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                self.assertEqual(registry_main(['validate'], paths=paths), 1)
            self.assertIn('backend_model', output.getvalue())
            self.assertNotIn('Traceback', output.getvalue())


if __name__ == '__main__':
    unittest.main()
