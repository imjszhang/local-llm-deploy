"""Protocol tests use injected CPU fakes and never read the deployment's state."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from local_llm_deploy.config import ProjectPaths
from local_llm_deploy.services.common import (
    ServiceSettings, configure_service, make_handler, run_service,
)
from local_llm_deploy.services.embedding import EmbeddingHandler
from local_llm_deploy.services.rerank import RerankHandler, RerankModel
from local_llm_deploy.services.whisper import WhisperHandler, WhisperModel


class Vectors(list):
    def tolist(self):
        return list(self)


class FakeEmbedding:
    task_names = ["retrieval", "text-matching", "classification", "clustering"]
    hidden_size = 4

    def __init__(self):
        self.calls = []

    def encode(self, texts, **kwargs):
        self.calls.append((texts, kwargs))
        return Vectors([[0.5] * (kwargs["dimensions"] or 4) for _ in texts]), len(texts) * 2


class FakeRerank:
    def rerank(self, query, documents, top_n, return_embeddings):
        order = list(reversed(range(len(documents))))[:top_n]
        return [{"index": i, "relevance_score": float(i) / 10,
                 "document": documents[i], "embedding": Vectors([1, 2])}
                for i in order]


class FakeWhisper:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def transcribe(self, path, **kwargs):
        self.calls.append((path, Path(path).read_bytes(), kwargs))
        if self.fail:
            raise RuntimeError("an internal model detail")
        return {"text": "  你好，世界  ", "language": "zh", "duration": 1.2,
                "segments": [{"start": 0.0, "end": 1.2, "text": "你好，世界"}]}


@contextmanager
def service(handler, model, **attributes):
    with tempfile.TemporaryDirectory() as directory:
        key_path = Path(directory) / ".api-key"
        key_path.write_text("test-key\n")
        configured = make_handler(handler, model=model, api_key_path=key_path, **attributes)
        server = ThreadingHTTPServer(("127.0.0.1", 0), configured)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


def request(server, endpoint, body=None, *, headers=None, auth=True, method="POST"):
    hdrs = {"Authorization": "Bearer test-key"} if auth else {}
    if headers:
        hdrs.update(headers)
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    try:
        connection.request(method, endpoint, body=body, headers=hdrs)
        response = connection.getresponse()
        raw = response.read()
        payload = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw.decode()
        return response.status, payload
    finally:
        connection.close()


def multipart(audio=b"\x00\xff\x80audio\r\n\r\n", **fields):
    boundary = "LocalLLMProtocolBoundary"
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="test.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode() + audio + b"\r\n"]
    for key, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), {"Content-Type": f'multipart/form-data; boundary="{boundary}"'}


class ServiceContractTests(unittest.TestCase):
    def test_embedding_batch_task_dimensions_and_usage(self):
        model = FakeEmbedding()
        with service(EmbeddingHandler, model, model_name="configured-alias") as server:
            status, result = request(server, "/v1/embeddings/?x=1", {
                "input": ["text"] * 35, "task": "retrieval.query", "dimensions": "2",
            })
        self.assertEqual(status, 200)
        self.assertEqual(result["model"], "configured-alias")
        self.assertEqual(len(result["data"]), 35)
        self.assertEqual(result["data"][-1], {"object": "embedding", "index": 34, "embedding": [0.5, 0.5]})
        self.assertEqual(result["usage"], {"prompt_tokens": 70, "total_tokens": 70})
        self.assertEqual([len(texts) for texts, _ in model.calls], [32, 3])
        self.assertTrue(all(kwargs["prompt_name"] == "query" for _, kwargs in model.calls))

    def test_invalid_embedding_fields_are_json_errors_and_service_survives(self):
        model = FakeEmbedding()
        invalid = [None, [], [1], True, 1.5, {}, "invalid", 0, -1]
        with service(EmbeddingHandler, model) as server:
            for value in invalid:
                if value is None:
                    continue  # None intentionally selects the full model dimension.
                with self.subTest(dimensions=value):
                    status, result = request(server, "/v1/embeddings", {"input": "test", "dimensions": value})
                    self.assertEqual(status, 400)
                    self.assertIn("error", result)
            for body in ([1], {"input": [1]}, {"input": []}, {"input": "x", "task": {}},
                         {"input": "x", "dimensions": 5}, {"input": "x", "prompt_name": []}):
                self.assertEqual(request(server, "/v1/embeddings", body)[0], 400)
            self.assertEqual(request(server, "/v1/embeddings", {"input": "test"})[0], 200)
        self.assertEqual(len(model.calls), 1)

    def test_auth_health_and_loading_status(self):
        for handler, model in ((EmbeddingHandler, FakeEmbedding()), (RerankHandler, FakeRerank()),
                               (WhisperHandler, FakeWhisper())):
            with self.subTest(service=handler.__name__), service(handler, model) as server:
                self.assertEqual(request(server, "/health/?check=1", auth=False, method="GET"), (200, {"status": "ok"}))
                self.assertEqual(request(server, handler.endpoint, {}, auth=False)[0], 401)
                self.assertEqual(request(server, "/absent", method="GET")[0], 404)
            with service(handler, None) as server:
                self.assertEqual(request(server, "/health", method="GET")[0], 503)
                self.assertEqual(request(server, handler.endpoint, {})[0], 503)

    def test_body_framing_errors(self):
        with service(EmbeddingHandler, FakeEmbedding(), max_body_bytes=80) as server:
            for raw in (b"null", b"[]", b"1", b"{bad}", b'{"input":"x","dimensions":NaN}'):
                self.assertEqual(request(server, "/v1/embeddings", raw)[0], 400)
            self.assertEqual(request(server, "/v1/embeddings", b"x" * 81)[0], 413)
            self.assertEqual(request(server, "/v1/embeddings", b"", headers={"Content-Length": "-1"})[0], 400)
            self.assertEqual(request(server, "/v1/embeddings", b"", headers={"Content-Length": "bad"})[0], 400)
            self.assertEqual(request(server, "/v1/embeddings", b"", headers={"Transfer-Encoding": "chunked"})[0], 400)

    def test_rerank_contract_top_n_and_optional_output(self):
        with service(RerankHandler, FakeRerank(), model_name="custom-rerank") as server:
            status, result = request(server, "/v1/rerank", {
                "query": "query", "documents": ["first", "second"], "top_n": "1",
                "return_documents": True, "return_embeddings": True,
            })
            self.assertEqual(status, 200)
            self.assertEqual(result, {"model": "custom-rerank", "results": [
                {"index": 1, "relevance_score": 0.1, "document": "second", "embedding": [1, 2]}
            ], "usage": {"total_tokens": 0}})
            _, result = request(server, "/v1/rerank", {"query": "query", "documents": ["first"]})
            self.assertEqual(set(result["results"][0]), {"index", "relevance_score"})

    def test_rerank_rejects_invalid_types_without_dropping_connection(self):
        with service(RerankHandler, FakeRerank(), max_documents=2) as server:
            bodies = [{"top_n": value} for value in (True, [], {}, "bad", 0, -2, 1.3)]
            bodies += [{"return_documents": "false"}, {"return_embeddings": 1}, {"query": []},
                       {"documents": [1]}, {"documents": []}, {"documents": ["a", "b", "c"]}]
            for invalid in bodies:
                with self.subTest(body=invalid):
                    body = {"query": "q", "documents": ["a"], **invalid}
                    status, result = request(server, "/v1/rerank", body)
                    self.assertEqual(status, 400)
                    self.assertIn("error", result)

    def test_whisper_binary_audio_and_response_formats(self):
        model = FakeWhisper()
        audio = bytes(range(256)) + b"\r\n\r\n\xff"
        with service(WhisperHandler, model) as server:
            for response_format in ("json", "text", "verbose_json"):
                with self.subTest(format=response_format):
                    body, headers = multipart(audio, response_format=response_format, language="zh", task="transcribe")
                    status, result = request(server, "/v1/audio/transcriptions", body, headers=headers)
                    self.assertEqual(status, 200)
                    if response_format == "text":
                        self.assertEqual(result, "你好，世界")
                    else:
                        self.assertEqual(result["text"], "你好，世界")
                        if response_format == "verbose_json":
                            self.assertEqual(result["duration"], 1.2)
                            self.assertEqual(result["task"], "transcribe")
                            self.assertEqual(len(result["segments"]), 1)
                    path, received_audio, kwargs = model.calls[-1]
                    self.assertEqual(received_audio, audio)
                    self.assertFalse(Path(path).exists())
                    self.assertEqual(kwargs["word_timestamps"], response_format == "verbose_json")

    def test_whisper_failure_cleans_temporary_audio_and_hides_internal_details(self):
        model = FakeWhisper(fail=True)
        with service(WhisperHandler, model) as server:
            body, headers = multipart()
            status, result = request(server, "/v1/audio/transcriptions", body, headers=headers)
            self.assertEqual(status, 500)
            self.assertEqual(result["error"]["message"], "Inference failed")
            self.assertFalse(Path(model.calls[0][0]).exists())

    def test_whisper_invalid_multipart_and_options(self):
        model = FakeWhisper()
        with service(WhisperHandler, model) as server:
            self.assertEqual(request(server, "/v1/audio/transcriptions", {})[0], 400)
            for fields in ({"task": "bad"}, {"response_format": "srt"}):
                body, headers = multipart(**fields)
                self.assertEqual(request(server, "/v1/audio/transcriptions", body, headers=headers)[0], 400)
            body, headers = multipart()
            self.assertEqual(request(server, "/v1/audio/transcriptions", body[:-8], headers=headers)[0], 400)
        self.assertEqual(model.calls, [])

    def test_handlers_do_not_share_model_configuration(self):
        with service(EmbeddingHandler, FakeEmbedding(), model_name="one") as first:
            with service(EmbeddingHandler, FakeEmbedding(), model_name="two") as second:
                self.assertEqual(request(first, "/v1/embeddings", {"input": "x"})[1]["model"], "one")
                self.assertEqual(request(second, "/v1/embeddings", {"input": "x"})[1]["model"], "two")


class ServiceLifecycleTests(unittest.TestCase):
    def test_configuration_uses_shared_registry_paths_and_explicit_precedence(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            paths = ProjectPaths(Path(directory))
            paths.registry.write_text(json.dumps({"custom": {
                "type": "embedding", "alias": "custom-alias", "repo_id": "org/model",
                "default_port": 8123, "host": "127.0.0.2", "params": {"dimensions": 4},
            }}))
            default_path = paths.models / "org-model"
            default_path.mkdir(parents=True)
            arguments = ["--project-root", directory, "--model-name", "custom-alias"]
            options = dict(description="Test service", default_model="unused", capability="embedding",
                           default_port=8004, environment_prefix="JINA_EMBED")
            settings = configure_service(arguments, **options)
            self.assertEqual((settings.key, settings.alias, settings.model_dir, settings.port, settings.host),
                             ("custom", "custom-alias", default_path, 8123, "127.0.0.2"))
            self.assertEqual(settings.params, {"dimensions": 4})
            custom_path = paths.root / "custom-installation"
            custom_path.mkdir()
            with patch.dict(os.environ, {"JINA_EMBED_PORT": "8234", "JINA_EMBED_HOST": "127.0.0.3",
                                         "LOCAL_LLM_MODEL_DIR": str(custom_path)}):
                settings = configure_service(arguments, **options)
                self.assertEqual((settings.port, settings.host, settings.model_dir), (8234, "127.0.0.3", custom_path))
                settings = configure_service(arguments + ["--port", "8345", "--host", "127.0.0.4",
                                                           "--model-dir", str(default_path)], **options)
                self.assertEqual((settings.port, settings.host, settings.model_dir), (8345, "127.0.0.4", default_path))
            with self.assertRaisesRegex(ValueError, "does not provide"):
                configure_service(arguments, **{**options, "capability": "asr"})
            with self.assertRaisesRegex(ValueError, "Unknown registered model"):
                configure_service(["--project-root", directory, "--model-name", "absent"], **options)

    def test_config_rejects_invalid_port_before_loading_models(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            paths = ProjectPaths(Path(directory))
            paths.registry.write_text(json.dumps({"fake": {"type": "embedding", "repo_id": "org/model"}}))
            (paths.models / "org-model").mkdir(parents=True)
            for value in ("-1", "0", "65536"):
                with self.subTest(port=value), self.assertRaises(ValueError):
                    configure_service(["--project-root", directory, "--port", value], description="test",
                                      default_model="fake", capability="embedding", default_port=8004,
                                      environment_prefix="JINA_EMBED")

    def test_imports_do_not_load_heavy_dependencies(self):
        result = subprocess.run([sys.executable, "-c", "import sys; import local_llm_deploy.services.embedding; "
            "import local_llm_deploy.services.rerank; import local_llm_deploy.services.whisper; "
            "assert not {'torch','transformers','mlx','mlx_whisper','rerank'}.intersection(sys.modules)"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_binding_failure_does_not_publish_or_remove_state(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            paths = ProjectPaths(Path(directory))
            settings = ServiceSettings(paths, "fake", "alias", paths.models, "127.0.0.1", occupied.getsockname()[1], {})
            with patch("local_llm_deploy.lifecycle.observe.publish_pid") as publish, patch("local_llm_deploy.lifecycle.observe.remove_pid") as remove:
                with self.assertRaises(OSError):
                    run_service(make_handler(EmbeddingHandler, model=FakeEmbedding()), settings)
                publish.assert_not_called()
                remove.assert_not_called()

    def test_ready_record_is_published_after_bind_and_removed_on_exit(self):
        import fcntl
        from local_llm_deploy.lifecycle.manager import instance_lock
        from local_llm_deploy.lifecycle.types import LifecycleError

        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            settings = ServiceSettings(paths, "fake", "alias", paths.models, "127.0.0.1", 0, {})

            def load_handler():
                with (paths.run / ".artifacts.lock").open("a") as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with self.assertRaises(LifecycleError):
                    with instance_lock(paths, "fake"):
                        pass
                return make_handler(EmbeddingHandler, model=FakeEmbedding())

            def running(server):
                with (paths.run / ".artifacts.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(lock, fcntl.LOCK_UN)
                with instance_lock(paths, "fake"):
                    pass  # stop can acquire the lock after ready publication.
                lines = (paths.run / "fake.pid").read_text().splitlines()
                self.assertEqual(int(lines[0]), os.getpid())
                self.assertEqual(int(lines[1]), server.server_port)
                self.assertTrue(json.loads(lines[3])["ready"])
                self.assertEqual(json.loads(lines[3])["model_path"], str(settings.model_dir.resolve()))
                with socket.create_connection(server.server_address, timeout=1):
                    pass
                raise KeyboardInterrupt

            with patch.dict(os.environ, {"LOCAL_LLM_MANAGED_INSTANCE": "0"}):
                with patch.object(ThreadingHTTPServer, "serve_forever", running):
                    run_service(load_handler, settings)
            self.assertFalse((paths.run / "fake.pid").exists())

    def test_failed_model_load_publishes_no_ready_record(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            settings = ServiceSettings(paths, "fake", "alias", paths.models, "127.0.0.1", 0, {})

            def load_handler():
                raise ImportError("fake missing runtime")

            with patch.dict(os.environ, {"LOCAL_LLM_MANAGED_INSTANCE": "0"}):
                with self.assertRaises(ImportError):
                    run_service(load_handler, settings)
            self.assertFalse((paths.run / "fake.pid").exists())

    def test_failed_publication_closes_listener_and_preserves_existing_record(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            paths.run.mkdir()
            record = paths.run / "fake.pid"
            record.write_text("existing ownership record")
            settings = ServiceSettings(paths, "fake", "alias", paths.models, "127.0.0.1", 0, {})
            listeners = []

            def listener(*args, **kwargs):
                server = ThreadingHTTPServer(*args, **kwargs)
                listeners.append(server)
                return server

            with patch.dict(os.environ, {"LOCAL_LLM_MANAGED_INSTANCE": "0"}, clear=True):
                with patch("local_llm_deploy.services.common.ThreadingHTTPServer", listener):
                    with patch("local_llm_deploy.lifecycle.observe.publish_pid", side_effect=ValueError("already owned")):
                        with self.assertRaisesRegex(ValueError, "already owned"):
                            run_service(make_handler(EmbeddingHandler, model=FakeEmbedding()), settings)
            self.assertEqual(record.read_text(), "existing ownership record")
            self.assertEqual(listeners[0].socket.fileno(), -1)

    def test_managed_start_never_writes_or_removes_parent_record(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            settings = ServiceSettings(paths, "fake", "alias", paths.models, "127.0.0.1", 0, {})
            with patch.dict(os.environ, {"LOCAL_LLM_MANAGED_INSTANCE": "1"}):
                with patch.object(ThreadingHTTPServer, "serve_forever", side_effect=KeyboardInterrupt):
                    with patch("local_llm_deploy.lifecycle.observe.publish_pid") as publish, patch("local_llm_deploy.lifecycle.observe.remove_pid") as remove:
                        run_service(make_handler(EmbeddingHandler, model=FakeEmbedding()), settings)
                        publish.assert_not_called()
                        remove.assert_not_called()

    def test_explicit_missing_auth_file_fails_before_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            settings = ServiceSettings(paths, "fake", "alias", paths.models, "127.0.0.1", 0, {})
            with patch.dict(os.environ, {"LOCAL_LLM_MANAGED_INSTANCE": "1", "API_KEY_FILE": "missing-key"}, clear=True):
                with patch("local_llm_deploy.services.common.ThreadingHTTPServer") as listener:
                    with self.assertRaisesRegex(ValueError, "API key file does not exist"):
                        run_service(make_handler(EmbeddingHandler, model=FakeEmbedding()), settings)
                    listener.assert_not_called()

    def test_authentication_overrides_are_explicit_and_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            (paths.root / "custom.key").write_text("alternate-test-key")
            settings = ServiceSettings(paths, "fake", "alias", paths.models, "127.0.0.1", 0, {})

            def running(server):
                self.assertEqual(server.RequestHandlerClass.api_key_path, paths.root / "custom.key")
                self.assertIsNone(server.RequestHandlerClass.api_key)
                raise KeyboardInterrupt

            with patch.dict(os.environ, {"LOCAL_LLM_MANAGED_INSTANCE": "1", "API_KEY_FILE": "custom.key"}, clear=True):
                with patch.object(ThreadingHTTPServer, "serve_forever", running):
                    run_service(make_handler(EmbeddingHandler, model=FakeEmbedding()), settings)
                with patch.dict(os.environ, {"API_KEY": "explicit-test-key"}):
                    with self.assertRaisesRegex(ValueError, "cannot both be set"):
                        run_service(make_handler(EmbeddingHandler, model=FakeEmbedding()), settings)

    def test_rerank_and_whisper_adapters_serialize_inference(self):
        for adapter_type in (RerankModel, WhisperModel):
            with self.subTest(adapter=adapter_type.__name__):
                started = threading.Event()
                release = threading.Event()
                calls = []

                class Runtime:
                    def predict(self, *args, **kwargs):
                        calls.append(1)
                        started.set()
                        if len(calls) == 1:
                            release.wait(2)
                        return []
                    rerank = predict
                    transcribe = predict

                model = adapter_type.__new__(adapter_type)
                model._lock = threading.Lock()
                if adapter_type is RerankModel:
                    model.reranker = Runtime()
                    predict = lambda: model.rerank("query", ["document"])
                else:
                    model._mlx_whisper = Runtime()
                    model.model_dir = "fake"
                    model.default_language = "zh"
                    model.default_task = "transcribe"
                    predict = lambda: model.transcribe("fake.audio")
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(predict)
                    self.assertTrue(started.wait(1))
                    second = pool.submit(predict)
                    time.sleep(0.02)
                    self.assertEqual(len(calls), 1)
                    release.set()
                    first.result(timeout=2)
                    second.result(timeout=2)
                self.assertEqual(len(calls), 2)


class FFmpegLauncherTests(unittest.TestCase):
    def test_launcher_uses_selected_python_and_exec_preserves_arguments_and_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "fake ffmpeg"
            executable.write_text(f"#!{sys.executable}\nimport json, os, sys\nprint(json.dumps({{'argv': sys.argv[1:], 'pid': os.getpid()}}))\nraise SystemExit(7)\n")
            executable.chmod(0o755)
            (root / "imageio_ffmpeg.py").write_text(f"def get_ffmpeg_exe():\n    return {str(executable)!r}\n")
            launcher = Path(__file__).resolve().parents[2] / "tools/ffmpeg"
            env = {**os.environ, "LOCAL_LLM_FFMPEG_PYTHON": sys.executable, "PYTHONPATH": directory}
            arguments = ["-i", "file with spaces.wav", "-metadata", "title=你好", "-"]
            with subprocess.Popen([str(launcher), *arguments], cwd=directory, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 7, stderr)
                self.assertEqual(json.loads(stdout), {"argv": arguments, "pid": process.pid})

    def test_launcher_rejects_recursive_executable_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = Path(__file__).resolve().parents[2] / "tools/ffmpeg"
            (root / "imageio_ffmpeg.py").write_text(f"def get_ffmpeg_exe():\n    return {str(launcher)!r}\n")
            env = {**os.environ, "LOCAL_LLM_FFMPEG_PYTHON": sys.executable, "PYTHONPATH": directory}
            result = subprocess.run([str(launcher), "-version"], env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertIn("points back", result.stderr)

    def test_whisper_sets_its_own_ffmpeg_python_before_warmup(self):
        from types import SimpleNamespace
        calls = []

        def transcribe(*args, **kwargs):
            calls.append(os.environ.get("LOCAL_LLM_FFMPEG_PYTHON"))
            return {"text": ""}

        with patch.dict(os.environ, {"LOCAL_LLM_FFMPEG_PYTHON": "old-python"}):
            with patch.dict(sys.modules, {"mlx_whisper": SimpleNamespace(transcribe=transcribe)}):
                WhisperModel("fake-model")
            self.assertEqual(calls, [sys.executable])


class ServiceSmokeRunnerTests(unittest.TestCase):
    def test_live_runner_checks_all_protocols_without_exposing_key(self):
        from tests.smoke.services import main as smoke_main
        from local_llm_deploy.services.common import parse_multipart_form

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            (root / ".api-key").write_text("never-print-this-key")
            audio = root / "short.wav"
            audio.write_bytes(bytes(range(256)))
            seen = []

            def fake_urlopen(req, timeout):
                seen.append(req)
                self.assertEqual(req.get_header("Authorization"), "Bearer never-print-this-key")
                self.assertGreater(timeout, 0)
                if req.full_url.endswith("/health"):
                    result = {"status": "ok"}
                elif req.full_url.endswith("/api/models"):
                    result = {"models": []}
                elif req.full_url.endswith("/v1/embeddings"):
                    body = json.loads(req.data)
                    count = 1 if isinstance(body["input"], str) else len(body["input"])
                    dimensions = body.get("dimensions", 4)
                    result = {"object": "list", "data": [{"index": i, "embedding": [0.5] * dimensions}
                                                          for i in range(count)], "usage": {"total_tokens": 2}}
                elif req.full_url.endswith("/v1/rerank"):
                    result = {"results": [{"index": 0, "document": "test", "relevance_score": 0.5}]}
                elif req.full_url.endswith("/v1/audio/transcriptions"):
                    fields = parse_multipart_form(req.data, req.get_header("Content-type"))
                    self.assertEqual(fields["file"], audio.read_bytes())
                    if fields["response_format"] == "text":
                        return io.BytesIO("你好".encode())
                    result = {"text": "你好", "segments": []}
                elif req.full_url.endswith("/v1/chat/completions"):
                    if json.loads(req.data).get("stream"):
                        return io.BytesIO(b'data: {"choices":[{}]}\n\ndata: [DONE]\n\n')
                    result = {"choices": [{"message": {"content": "Hello"}}]}
                else:
                    self.fail(f"Unexpected smoke endpoint: {req.full_url}")
                return io.BytesIO(json.dumps(result).encode())

            output = io.StringIO()
            with patch("urllib.request.urlopen", fake_urlopen), patch("sys.stdout", output), patch("sys.stderr", output):
                code = smoke_main(["--project-root", directory, "--jina", "1", "--proxy", "2",
                                   "--rerank", "3", "--whisper", "4", "--audio", str(audio), "--chat-model", "fake"])
            self.assertEqual(code, 0, output.getvalue())
            self.assertEqual(len(seen), 16)
            self.assertNotIn("never-print-this-key", output.getvalue())

    def test_live_runner_returns_failure_for_bad_response_contract(self):
        from tests.smoke.services import main as smoke_main

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            with patch("urllib.request.urlopen", side_effect=lambda *args, **kwargs: io.BytesIO(b"{}")):
                with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                    self.assertEqual(smoke_main(["--project-root", directory, "--no-auth"]), 1)


if __name__ == "__main__":
    unittest.main()
