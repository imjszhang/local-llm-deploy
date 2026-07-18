#!/usr/bin/env python3
"""
Local LLM Deploy — Whisper ASR (MLX) 服务
加载 mlx-community Whisper 模型，提供 OpenAI 兼容的 /v1/audio/transcriptions 接口。

用法:
  python3 serve_whisper.py [--port 8007] [--model-dir PATH] [--model-name NAME]

接口:
  POST /v1/audio/transcriptions  → 语音转写（OpenAI 兼容 multipart）
  GET  /health                   → 健康检查
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tempfile
import threading
import time
from email import message_from_bytes
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
API_KEY_FILE = os.path.join(SCRIPT_DIR, ".api-key")
RUN_DIR = os.path.join(SCRIPT_DIR, "run")
MODELS_JSON = os.path.join(SCRIPT_DIR, "models.json")


def _log(msg: str) -> None:
    sys.stderr.write(f"[whisper] {msg}\n")
    sys.stderr.flush()


def load_api_key() -> str | None:
    if os.path.isfile(API_KEY_FILE):
        with open(API_KEY_FILE) as f:
            return f.readline().strip().strip("\r\n") or None
    return None


def parse_multipart_form(body: bytes, content_type: str) -> dict[str, str | bytes]:
    """Parse multipart/form-data into field name -> str or bytes."""
    if not content_type.lower().startswith("multipart/form-data"):
        raise ValueError("Content-Type must be multipart/form-data")
    header_block = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    msg = message_from_bytes(header_block + body, policy=HTTP)
    fields: dict[str, str | bytes] = {}
    if not msg.is_multipart():
        return fields
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            fields[name] = ""
            continue
        filename = part.get_filename()
        if filename:
            fields[name] = payload
        else:
            fields[name] = payload.decode("utf-8", errors="replace")
    return fields


class WhisperModel:
    """Wraps mlx-whisper with a single model directory and inference lock."""

    def __init__(self, model_dir: str, *, language: str = "zh", task: str = "transcribe"):
        self.model_dir = model_dir
        self.default_language = language
        self.default_task = task
        self._lock = threading.Lock()
        _log(f"加载模型: {model_dir}")
        t0 = time.monotonic()
        import mlx_whisper

        self._mlx_whisper = mlx_whisper
        # Warm up model weights
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            self._write_silent_wav(tmp_path, duration_sec=0.1)
            mlx_whisper.transcribe(
                tmp_path,
                path_or_hf_repo=model_dir,
                language=language,
                task=task,
                verbose=False,
            )
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        _log(f"模型就绪 ({time.monotonic() - t0:.1f}s)")

    @staticmethod
    def _write_silent_wav(path: str, duration_sec: float = 0.1) -> None:
        import struct
        import wave

        rate = 16000
        nframes = max(1, int(rate * duration_sec))
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(struct.pack("<h", 0) * nframes)

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str | None = None,
        task: str | None = None,
        word_timestamps: bool = False,
    ) -> dict:
        lang = language or self.default_language
        tsk = task or self.default_task
        with self._lock:
            return self._mlx_whisper.transcribe(
                audio_path,
                path_or_hf_repo=self.model_dir,
                language=lang,
                task=tsk,
                word_timestamps=word_timestamps,
                verbose=False,
            )


class WhisperHandler(BaseHTTPRequestHandler):
    model: WhisperModel | None = None
    model_name: str = "whisper-large-v3"
    default_language: str = "zh"
    default_task: str = "transcribe"
    default_response_format: str = "json"

    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/health":
            self._json_response(200, {"status": "ok"})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        path = self.path.rstrip("/").split("?")[0]
        if path == "/v1/audio/transcriptions":
            self.handle_transcription()
        else:
            self.send_error(404)

    def handle_transcription(self) -> None:
        if not self._check_auth():
            return
        if self.model is None:
            self._error_response(503, "Model not loaded")
            return

        content_len = int(self.headers.get("Content-Length", 0))
        if content_len == 0:
            self._error_response(400, "Empty request body")
            return

        content_type = self.headers.get("Content-Type", "")
        body = self.rfile.read(content_len)
        try:
            fields = parse_multipart_form(body, content_type)
        except ValueError as e:
            self._error_response(400, str(e))
            return

        file_data = fields.get("file")
        if not isinstance(file_data, (bytes, bytearray)) or not file_data:
            self._error_response(400, "Missing or invalid 'file' field")
            return

        language = str(fields.get("language") or self.default_language)
        task = str(fields.get("task") or self.default_task)
        response_format = str(
            fields.get("response_format") or self.default_response_format
        )
        word_timestamps = response_format == "verbose_json"

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as tmp:
                tmp.write(file_data)
                tmp_path = tmp.name
            result = self.model.transcribe(
                tmp_path,
                language=language,
                task=task,
                word_timestamps=word_timestamps,
            )
        except Exception as e:
            _log(f"转写失败: {e}")
            self._error_response(500, f"Transcription failed: {e}")
            return
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

        text = (result.get("text") or "").strip()
        if response_format == "text":
            body_out = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body_out)))
            self.end_headers()
            self.wfile.write(body_out)
            return
        if response_format == "verbose_json":
            payload = {
                "task": task,
                "language": result.get("language") or language,
                "duration": result.get("duration"),
                "text": text,
                "segments": result.get("segments") or [],
            }
            self._json_response(200, payload)
            return
        self._json_response(200, {"text": text})

    def _check_auth(self) -> bool:
        expected = load_api_key()
        if not expected:
            return True
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {expected}":
            return True
        self._error_response(401, "Invalid API key", error_type="invalid_request_error")
        return False

    def _json_response(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error_response(
        self, code: int, message: str, error_type: str = "invalid_request_error"
    ) -> None:
        self._json_response(code, {"error": {"message": message, "type": error_type}})

    def log_message(self, format, *args) -> None:
        pass


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def write_pid_file(model_name: str, port: int, alias: str) -> str:
    os.makedirs(RUN_DIR, exist_ok=True)
    pid_file = os.path.join(RUN_DIR, f"{model_name}.pid")
    with open(pid_file, "w") as f:
        f.write(f"{os.getpid()}\n{port}\n{alias}\n")
    return pid_file


def remove_pid_file(model_name: str) -> None:
    pid_file = os.path.join(RUN_DIR, f"{model_name}.pid")
    if os.path.exists(pid_file):
        os.remove(pid_file)


def get_model_config(model_name: str) -> dict | None:
    if not os.path.isfile(MODELS_JSON):
        return None
    with open(MODELS_JSON) as f:
        data = json.load(f)
    return data.get(model_name)


def get_model_dir_from_json(model_name: str) -> str | None:
    cfg = get_model_config(model_name)
    if not cfg:
        return None
    rn = cfg.get("repo_name") or cfg["repo_id"].replace("/", "-")
    return os.path.join(SCRIPT_DIR, "models", rn)


def main() -> None:
    parser = argparse.ArgumentParser(description="Whisper ASR (MLX) 服务")
    parser.add_argument("--port", type=int, default=8007)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--model-dir", default=None)
    parser.add_argument("--model-name", default="whisper-large-v3")
    args = parser.parse_args()

    cfg = get_model_config(args.model_name) or {}
    params = cfg.get("params") or {}
    alias = cfg.get("alias") or "whisper-large-v3"
    language = str(params.get("language") or "zh")
    task = str(params.get("task") or "transcribe")
    response_format = str(params.get("response_format") or "json")

    model_dir = args.model_dir or get_model_dir_from_json(args.model_name)
    if not model_dir:
        model_dir = os.path.join(
            SCRIPT_DIR, "models", "mlx-community-whisper-large-v3-mlx"
        )
    if not os.path.isdir(model_dir):
        print(f"错误: 模型目录不存在: {model_dir}", file=sys.stderr)
        print(f"请先下载: ./manage.sh download {args.model_name}", file=sys.stderr)
        sys.exit(1)

    model = WhisperModel(model_dir, language=language, task=task)
    WhisperHandler.model = model
    WhisperHandler.model_name = alias
    WhisperHandler.default_language = language
    WhisperHandler.default_task = task
    WhisperHandler.default_response_format = response_format

    pid_file = write_pid_file(args.model_name, args.port, alias)
    _log(f"PID 文件: {pid_file}")

    def cleanup(signum=None, frame=None) -> None:
        _log("正在停止...")
        remove_pid_file(args.model_name)
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    server = ThreadingHTTPServer((args.host, args.port), WhisperHandler)
    print(f"  Whisper 服务: {alias}")
    print(f"  监听:      http://{args.host}:{args.port}")
    print(f"  接口:      http://{args.host}:{args.port}/v1/audio/transcriptions")
    print(f"  健康检查:  http://{args.host}:{args.port}/health")
    sys.stdout.flush()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        cleanup()


if __name__ == "__main__":
    main()
