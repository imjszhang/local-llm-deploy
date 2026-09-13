"""Local inference adapter and HTTP protocol, with optional dependencies loaded on demand."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

from .common import (
    ServiceHTTPHandler, configure_service, make_handler,
    run_service, report_startup_error,
)
from .common import parse_multipart_form


def _log(message):
    sys.stderr.write("[whisper] " + str(message) + "\n")
    sys.stderr.flush()

class WhisperModel:
    """Wraps mlx-whisper with a single model directory and inference lock."""

    def __init__(self, model_dir: str, *, language: str = "zh", task: str = "transcribe"):
        self.model_dir = model_dir
        self.default_language = language
        self.default_task = task
        self._lock = threading.Lock()
        _log(f"加载模型: {model_dir}")
        t0 = time.monotonic()
        # The compatibility ffmpeg launcher must use this model's environment,
        # including a staged replacement or a configured runtime.python.
        os.environ["LOCAL_LLM_FFMPEG_PYTHON"] = sys.executable
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


class WhisperHandler(ServiceHTTPHandler):
    endpoint = "/v1/audio/transcriptions"
    model_name = "whisper-large-v3"
    default_language = "zh"
    default_task = "transcribe"
    default_response_format = "json"

    def predict(self):
        fields = parse_multipart_form(self._read_bytes(), self.headers.get("Content-Type", ""))
        file_data = fields.get("file")
        if not isinstance(file_data, bytes) or not file_data:
            raise ValueError("Missing or invalid 'file' field")
        for field in ("model", "language", "task", "response_format"):
            if field in fields and not isinstance(fields[field], str):
                raise ValueError(f"'{field}' must be a text field")
        language = fields.get("language") or self.default_language
        task = fields.get("task") or self.default_task
        response_format = fields.get("response_format") or self.default_response_format
        if task not in ("transcribe", "translate"):
            raise ValueError("'task' must be 'transcribe' or 'translate'")
        if response_format not in ("json", "text", "verbose_json"):
            raise ValueError("'response_format' must be json, text or verbose_json")
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".audio", delete=False) as audio:
                audio.write(file_data)
                tmp_path = audio.name
            result = self.model.transcribe(tmp_path, language=language, task=task,
                                           word_timestamps=response_format == "verbose_json")
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except FileNotFoundError:
                    pass
        text = (result.get("text") or "").strip()
        if response_format == "text":
            self._response(200, text.encode("utf-8"), "text/plain; charset=utf-8")
        elif response_format == "verbose_json":
            self._json_response(200, {
                "task": task, "language": result.get("language") or language,
                "duration": result.get("duration"), "text": text,
                "segments": result.get("segments") or [],
            })
        else:
            self._json_response(200, {"text": text})


def main(argv=None):
    try:
        settings = configure_service(
            argv, description="Whisper ASR (MLX) 服务", default_model="whisper-large-v3",
            capability="asr", default_port=8007, environment_prefix="WHISPER",
        )
        language = settings.params.get("language") or "zh"
        task = settings.params.get("task") or "transcribe"
        response_format = settings.params.get("response_format") or "json"
        if not isinstance(language, str):
            raise ValueError("Configured language must be a string")
        if task not in ("transcribe", "translate"):
            raise ValueError("Invalid Whisper task in model configuration")
        if response_format not in ("json", "text", "verbose_json"):
            raise ValueError("Invalid Whisper response_format in model configuration")
        def load_handler():
            model = WhisperModel(str(settings.model_dir), language=language, task=task)
            return make_handler(WhisperHandler, model=model, model_name=settings.alias,
                                default_language=language, default_task=task,
                                default_response_format=response_format)

        run_service(load_handler, settings)
        return 0
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        return report_startup_error(exc, "whisper")


if __name__ == "__main__":
    raise SystemExit(main())
