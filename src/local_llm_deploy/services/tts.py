"""Single-model Qwen3-TTS Base service; inference dependencies stay isolated."""
from __future__ import annotations

import base64
import binascii
import io
import tempfile
import threading
import wave
from pathlib import Path

from .common import (RequestError, ServiceHTTPHandler, configure_service, make_handler,
                     report_startup_error, run_service)


LANGUAGES = {"auto", "Chinese", "English", "Japanese", "Korean", "German", "French",
             "Russian", "Portuguese", "Spanish", "Italian"}


def validate_request(body):
    if body.get("stream", False) is not False:
        raise RequestError("Streaming audio is not supported")
    if body.get("response_format", "wav") != "wav":
        raise RequestError("Only response_format=wav is supported")
    for key, limit in (("input", 2000), ("ref_text", 2000)):
        value = body.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise RequestError(f"'{key}' must contain 1..{limit} characters")
    language = body.get("language", "English")
    if not isinstance(language, str) or language not in LANGUAGES:
        raise RequestError("Unsupported language")
    encoded = body.get("ref_audio_base64")
    if not isinstance(encoded, str) or len(encoded) > 16 * 1024 * 1024:
        raise RequestError("A base64 WAV reference is required (maximum 12 MiB)")
    try:
        audio = base64.b64decode(encoded, validate=True)
        with wave.open(io.BytesIO(audio), "rb") as wav:
            frames, rate = wav.getnframes(), wav.getframerate()
            if not 8000 <= rate <= 48000 or wav.getnchannels() not in (1, 2):
                raise RequestError("Reference must be mono/stereo PCM WAV at 8..48 kHz")
            if wav.getsampwidth() != 2 or not 1 <= frames / rate <= 30:
                raise RequestError("Reference must be 16-bit PCM WAV, 1..30 seconds")
            if len(wav.readframes(frames)) != frames * wav.getnchannels() * 2:
                raise RequestError("Truncated reference WAV")
    except (binascii.Error, wave.Error, EOFError, ValueError) as exc:
        if isinstance(exc, RequestError):
            raise
        raise RequestError("Invalid base64 PCM WAV reference") from None
    return audio, language


class TTSModel:
    def __init__(self, model_dir):
        from mlx_audio.tts.utils import load_model
        self._model = load_model(str(model_dir))
        if self._model.tokenizer is None or self._model.speech_tokenizer is None:
            raise RuntimeError("Text/speech tokenizer failed to load")
        if getattr(self._model.config, "tts_model_type", None) != "base":
            raise ValueError("This service requires a Qwen3-TTS Base model")
        self._lock = threading.Lock()

    def synthesize(self, text, ref_audio, ref_text, language):
        import numpy as np
        import soundfile as sf
        chunks, sample_rate = [], None
        with self._lock:
            for result in self._model.generate(text=text, ref_audio=ref_audio, ref_text=ref_text,
                                               lang_code=language, max_tokens=4096):
                rate = int(result.sample_rate)
                if sample_rate is not None and rate != sample_rate:
                    raise RuntimeError("Inconsistent generated sample rates")
                sample_rate = rate
                chunk = np.asarray(result.audio, dtype=np.float32).reshape(-1)
                if not np.isfinite(chunk).all():
                    raise RuntimeError("Non-finite generated audio")
                chunks.append(chunk)
            if not chunks or sum(x.size for x in chunks) == 0:
                raise RuntimeError("No generated audio")
            output = io.BytesIO()
            sf.write(output, np.concatenate(chunks), sample_rate, format="WAV", subtype="PCM_16")
            return output.getvalue()


class TTSHandler(ServiceHTTPHandler):
    endpoint = "/v1/audio/speech"
    max_body_bytes = 17 * 1024 * 1024

    def predict(self):
        body = self._read_body()
        if body.get("model") not in (None, self.model_name, getattr(self, "model_key", self.model_name)):
            raise RequestError("Unknown model", 404)
        audio, language = validate_request(body)
        with tempfile.TemporaryDirectory(prefix="local-tts-") as directory:
            reference = Path(directory) / "reference.wav"
            reference.write_bytes(audio)
            output = self.model.synthesize(body["input"], str(reference), body["ref_text"], language)
        self._response(200, output, "audio/wav")


def main(argv=None):
    try:
        settings = configure_service(argv, description="Qwen3-TTS Base (MLX)",
                                     default_model="qwen3-tts-1.7b-base", capability="tts",
                                     default_port=8008, environment_prefix="TTS")
        def load_handler():
            model = TTSModel(settings.model_dir)
            return make_handler(TTSHandler, model=model, model_name=settings.alias, model_key=settings.key)
        run_service(load_handler, settings)
        return 0
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        return report_startup_error(exc, "tts")


if __name__ == "__main__":
    raise SystemExit(main())
