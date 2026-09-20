#!/usr/bin/env python3
"""Exercise the local TTS service without exposing its API key in shell arguments."""
import argparse
import base64
import io
import json
from pathlib import Path
import time
import urllib.request
import wave

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--reference', type=Path, required=True)
parser.add_argument('--reference-text', required=True)
parser.add_argument('--text', default='This is a local speech synthesis test. The model is now running on this Mac.')
parser.add_argument('--language', default='English')
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--port', type=int, default=8888)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
body = {'model': 'qwen3-tts', 'input': args.text, 'ref_text': args.reference_text,
        'ref_audio_base64': base64.b64encode(args.reference.read_bytes()).decode(),
        'language': args.language, 'response_format': 'wav'}
headers = {'Content-Type': 'application/json'}
key_path = root / '.api-key'
if key_path.is_file():
    headers['Authorization'] = 'Bearer ' + key_path.read_text().splitlines()[0].strip()
request = urllib.request.Request(f'http://127.0.0.1:{args.port}/v1/audio/speech',
                                 json.dumps(body).encode(), headers)
started = time.monotonic()
with urllib.request.urlopen(request, timeout=600) as response:
    if response.headers.get_content_type() != 'audio/wav':
        raise RuntimeError('Service did not return WAV')
    audio = response.read()
with wave.open(io.BytesIO(audio), 'rb') as wav:
    duration = wav.getnframes() / wav.getframerate()
    if duration <= 0:
        raise RuntimeError('Empty generated audio')
    report = {'sample_rate': wav.getframerate(), 'channels': wav.getnchannels(),
              'duration_seconds': duration, 'elapsed_seconds': round(time.monotonic()-started, 3),
              'output': str(args.output.resolve()), 'synthetic': True}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_bytes(audio)
print(json.dumps(report, indent=2))
