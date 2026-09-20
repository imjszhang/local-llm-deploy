import base64
import io
import json
from pathlib import Path
import unittest
import wave

from local_llm_deploy.services.tts import TTSHandler, validate_request
from local_llm_deploy.services.common import RequestError
from tests.services.test_services_contract import service


def payload():
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
        wav.writeframes(b'\0\0' * 16000)
    return {'input': 'A deployment test.', 'ref_text': 'Reference sentence.',
            'ref_audio_base64': base64.b64encode(buf.getvalue()).decode(), 'response_format': 'wav'}


class FakeTTS:
    def synthesize(self, text, ref_audio, ref_text, language):
        self.path = ref_audio
        self.audio = Path(ref_audio).read_bytes()
        return self.audio


class TTSContract(unittest.TestCase):
    def test_validation(self):
        validate_request(payload())
        for field, value in [('stream', True), ('response_format', 'mp3'), ('language', []),
                             ('input', ''), ('ref_text', ''), ('ref_audio_base64', 'bad!')]:
            with self.subTest(field=field), self.assertRaises(RequestError):
                validate_request({**payload(), field: value})

    def test_truncated_wave(self):
        data = payload()
        data['ref_audio_base64'] = base64.b64encode(base64.b64decode(data['ref_audio_base64'])[:-8]).decode()
        with self.assertRaises(RequestError): validate_request(data)

    def test_http_wave_and_cleanup(self):
        fake = FakeTTS()
        from http.client import HTTPConnection
        with service(TTSHandler, fake) as server:
            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            connection.request('POST', '/v1/audio/speech', json.dumps(payload()),
                               {'Authorization': 'Bearer test-key', 'Content-Type': 'application/json'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader('Content-Type'), 'audio/wav')
            self.assertEqual(response.read(), fake.audio)
            self.assertFalse(Path(fake.path).exists())
            connection.close()
            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            connection.request('POST', '/v1/audio/speech', json.dumps(payload()))
            response = connection.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            connection.close()


class TTSDeployment(unittest.TestCase):
    def test_installation_requires_speech_tokenizer_and_all_shards(self):
        import tempfile
        from local_llm_deploy.config import normalize_models
        from local_llm_deploy.artifacts.paths import weights_complete
        spec = normalize_models({'tts': {'type': 'tts'}})['tts']
        self.assertEqual(spec.endpoints, ('/v1/audio/speech',))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('config.json', 'tokenizer_config.json', 'vocab.json', 'merges.txt',
                         'model.safetensors', 'speech_tokenizer/config.json'):
                file = root / name; file.parent.mkdir(parents=True, exist_ok=True); file.touch()
            self.assertFalse(weights_complete(spec, root))
            (root / 'speech_tokenizer/model.safetensors').touch()
            self.assertTrue(weights_complete(spec, root))
            (root / 'model.safetensors.index.json').write_text(json.dumps({'weight_map': {'a':'missing.safetensors'}}))
            self.assertFalse(weights_complete(spec, root))

    def test_launch_plan_uses_isolated_module(self):
        import tempfile
        from local_llm_deploy.config import normalize_models, ProjectPaths
        from local_llm_deploy.backends.builders import build_service
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = normalize_models({'tts': {'type': 'tts', 'repo_id': 'example/model',
                    'default_port': 8008, 'runtime': {'python': '.venv-tts/bin/python'}}})['tts']
            plan = build_service(spec, ProjectPaths(root), env={}, validate=False)
            self.assertEqual(plan.argv[:3], (str(root.resolve() / '.venv-tts/bin/python'), '-m', 'local_llm_deploy.services.tts'))
            self.assertEqual(plan.port, 8008)
            self.assertEqual(plan.env["HF_HUB_OFFLINE"], "1")
