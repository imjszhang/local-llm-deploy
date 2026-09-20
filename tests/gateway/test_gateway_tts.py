import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import wave
from http.server import BaseHTTPRequestHandler
from local_llm_deploy.config import ProjectPaths, normalize_models
from local_llm_deploy.gateway.app import GatewayContext, create_server
from local_llm_deploy.gateway.discovery import Backend
from local_llm_deploy.gateway.settings import GatewaySettings
from local_llm_deploy.observability import AccessLogger, BoundedCapture
from tests.gateway.test_gateway_contract import start_server, stop_server

buf = io.BytesIO()
with wave.open(buf, 'wb') as wav:
    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
    wav.writeframes(b'\x00\xff' * 240)
WAV = buf.getvalue()

class AudioBackend(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        time.sleep(.08)  # Cross multiple gateway keepalive intervals.
        self.send_response(200); self.send_header('Content-Type', 'audio/wav')
        self.send_header('Content-Length', str(len(WAV))); self.end_headers()
        self.wfile.write(WAV)
    def log_message(self, *args): pass

class TTSGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name)
        (root / '.api-key').write_text('test-key')
        self.upstream = start_server(AudioBackend)
        self.specs = normalize_models({'tts': {'type':'tts', 'alias':'voice', 'default_for':['tts']}})
        models = {'tts': Backend('tts','voice',f'http://127.0.0.1:{self.upstream.server_port}',('tts',),'mlx_tts')}
        class Discovery:
            def models(self): return models
            def ollama_status(self): return {'status':'offline'}
        self.models = models
        self.ctx = GatewayContext(ProjectPaths(root), specs=self.specs, discovery=Discovery(),
                                  settings=GatewaySettings(keepalive=.01), apps={})
        self.server = create_server(self.ctx, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
    def tearDown(self):
        stop_server(self.server); stop_server(self.upstream); self.tmp.cleanup()
    def request(self, body, auth=True):
        conn = http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        headers = {'Content-Type':'application/json'}
        if auth: headers['Authorization']='Bearer test-key'
        conn.request('POST','/v1/audio/speech',json.dumps(body),headers)
        r=conn.getresponse(); result=(r.status,r.getheader('Content-Type'),r.read());conn.close();return result
    def test_binary_default_and_alias(self):
        for body in ({'input':'hello'}, {'model':'voice','input':'hello'}):
            status,kind,data=self.request(body)
            self.assertEqual((status,kind,data),(200,'audio/wav',WAV))
        lanes,_=self.ctx.scheduler.snapshots();self.assertEqual(lanes['tts']['active'],0)
    def test_rejections(self):
        self.assertEqual(self.request({'input':'hello'},False)[0],401)
        self.assertEqual(self.request({'model':'unknown'})[0],404)
        self.assertEqual(self.request({'stream':True})[0],400)
        self.assertEqual(self.request({'stream':'false'})[0],400)
        self.models.clear();self.assertEqual(self.request({'model':'tts'})[0],503)
    def test_no_audio_payload_logging(self):
        p=Path(self.tmp.name)/'access.jsonl';logger=AccessLogger(str(p),log_body=True)
        capture=BoundedCapture(1000);capture.append(WAV)
        logger.write(request_id='id',path='/v1/audio/speech',method='POST',model='tts',
                     capability='tts',elapsed=1,status=200,body=b'secret-reference',response=capture)
        row=json.loads(p.read_text());self.assertNotIn('body',row);self.assertNotIn('response_body',row)
    def test_tts_lane_and_settings(self):
        s=GatewaySettings.from_env({'TTS_LANE_CONCURRENT':'2','DEFAULT_TTS_MODEL':'voice'})
        self.assertEqual(s.tts_concurrent,2);self.assertEqual(s.defaults['tts'],'voice')
        first=self.ctx.scheduler.submit('tts','tts');second=self.ctx.scheduler.submit('tts','tts')
        self.assertTrue(first.acquire(.1));self.assertFalse(second.acquire(.02))
        first.release();self.assertTrue(second.acquire(.1));second.release()
