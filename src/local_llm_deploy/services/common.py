"""Shared HTTP framing and service startup; no inference runtime imports."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import hmac
import json
import os
import signal
import sys
from dataclasses import dataclass
from email import message_from_bytes
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class RequestError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def positive_int(value, name):
    """Accept integer strings for compatibility, never silently truncate floats."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise RequestError(f"'{name}' must be a positive integer")
    try:
        number = int(value)
    except ValueError:
        raise RequestError(f"'{name}' must be a positive integer") from None
    if number <= 0:
        raise RequestError(f"'{name}' must be a positive integer")
    return number


def boolean_field(body, name, default=False):
    value = body.get(name, default)
    if not isinstance(value, bool):
        raise RequestError(f"'{name}' must be a boolean")
    return value


def load_api_key(path):
    if path is None:
        return None
    try:
        with Path(path).open(encoding="utf-8") as stream:
            return stream.readline().strip() or None
    except FileNotFoundError:
        return None


def parse_multipart_form(body: bytes, content_type: str) -> dict[str, str | bytes]:
    """Use the MIME parser on bytes so uploaded audio is never decoded as text."""
    if not content_type.lower().startswith("multipart/form-data"):
        raise RequestError("Content-Type must be multipart/form-data")
    if "\r" in content_type or "\n" in content_type:
        raise RequestError("Invalid Content-Type")
    headers = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n"
    try:
        message = message_from_bytes(headers.encode("ascii") + body, policy=HTTP)
    except (UnicodeError, ValueError):
        raise RequestError("Invalid multipart body") from None
    if not message.is_multipart() or message.defects:
        raise RequestError("Invalid or incomplete multipart body")
    fields = {}
    for part in message.iter_parts():
        if part.defects or part.is_multipart():
            raise RequestError("Invalid multipart field")
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        if name in fields:
            raise RequestError(f"Duplicate multipart field: {name}")
        payload = part.get_payload(decode=True) or b""
        if part.get_filename() is not None or name == "file":
            fields[name] = payload
        else:
            try:
                fields[name] = payload.decode("utf-8")
            except UnicodeError:
                raise RequestError(f"Invalid UTF-8 field: {name}") from None
    return fields


class ServiceHTTPHandler(BaseHTTPRequestHandler):
    """Handlers implement only predict(); this boundary owns errors and auth."""

    model = None
    model_name = ""
    api_key_path = None
    api_key = None
    endpoint = ""
    max_body_bytes = 64 * 1024 * 1024
    request_timeout = 30

    def setup(self):
        super().setup()
        self.connection.settimeout(self.request_timeout)

    def do_GET(self):
        path = urlsplit(self.path).path.rstrip("/")
        if path == "/health":
            ready = self.model is not None
            self._json_response(200 if ready else 503, {"status": "ok" if ready else "loading"})
        else:
            self._error_response(404, "Not found")

    def do_POST(self):
        if urlsplit(self.path).path.rstrip("/") != self.endpoint:
            self._error_response(404, "Not found")
            return
        try:
            if not self._check_auth():
                return
            if self.model is None:
                raise RequestError("Model not loaded", 503)
            self.predict()
        except RequestError as exc:
            self._error_response(exc.status, str(exc))
        except (ValueError, UnicodeError) as exc:
            self._error_response(400, str(exc))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception as exc:
            # Do not echo model internals, local paths or uploaded data to clients.
            sys.stderr.write(f"[{self.model_name}] inference failed: {type(exc).__name__}\n")
            self._error_response(500, "Inference failed", "server_error")

    def predict(self):
        raise NotImplementedError

    def _check_auth(self):
        expected = self.api_key if self.api_key is not None else load_api_key(self.api_key_path)
        if not expected:
            return True
        actual = self.headers.get("Authorization", "")
        if hmac.compare_digest(actual.encode(), f"Bearer {expected}".encode()):
            return True
        self._error_response(401, "Invalid API key")
        return False

    def _read_bytes(self):
        if self.headers.get("Transfer-Encoding"):
            raise RequestError("Transfer-Encoding is unsupported; send Content-Length")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1:
            raise RequestError("A single Content-Length header is required")
        raw_length = lengths[0]
        if not raw_length.isascii() or not raw_length.isdecimal():
            raise RequestError("Invalid Content-Length")
        length = int(raw_length)
        if length == 0:
            raise RequestError("Empty request body")
        if length > self.max_body_bytes:
            raise RequestError("Request body is too large", 413)
        body = self.rfile.read(length)
        if len(body) != length:
            raise RequestError("Incomplete request body")
        return body

    def _read_body(self):
        def reject_constant(value):
            raise RequestError(f"Invalid JSON constant: {value}")

        body = json.loads(self._read_bytes(), parse_constant=reject_constant)
        if not isinstance(body, dict):
            raise RequestError("JSON body must be an object")
        model_name = body.get("model")
        if model_name is not None and not isinstance(model_name, str):
            raise RequestError("'model' must be a string")
        return body

    def _response(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_response(self, code, obj):
        self._response(code, json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8"),
                       "application/json; charset=utf-8")

    def _error_response(self, code, message, error_type="invalid_request_error"):
        self._json_response(code, {"error": {"message": message, "type": error_type}})

    def log_message(self, format, *args):
        pass


def make_handler(handler_class, **attributes):
    """Keep each listener's model and configuration independent of other instances."""
    return type(f"Configured{handler_class.__name__}", (handler_class,), attributes)


@dataclass(frozen=True)
class ServiceSettings:
    paths: Any
    key: str
    alias: str
    model_dir: Path
    host: str
    port: int
    params: dict


def configure_service(argv, *, description, default_model, capability, default_port,
                      environment_prefix):
    from ..config import load_specs, project_paths
    from ..artifacts.paths import resolve_installation

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--project-root")
    parser.add_argument("--port", type=int)
    parser.add_argument("--host")
    parser.add_argument("--model-dir")
    parser.add_argument("--model-name")
    args = parser.parse_args(argv)
    paths = project_paths(args.project_root)
    prefixes = [environment_prefix]
    if environment_prefix.startswith("JINA_"):
        prefixes.append("JINA")

    def environment_value(name):
        return next((os.environ[prefix + "_" + name] for prefix in prefixes
                     if os.environ.get(prefix + "_" + name)), os.environ.get(name))

    model_key = args.model_name or environment_value("MODEL_NAME") or default_model
    specs = load_specs(paths.registry)
    spec = specs.get(model_key)
    if spec is None:
        spec = next((item for item in specs.values() if item.alias == model_key), None)
    if spec is None:
        raise ValueError(f"Unknown registered model: {model_key}")
    if capability not in spec.capabilities:
        raise ValueError(f"Model {spec.key} does not provide {capability}")
    port_value = args.port if args.port is not None else (
        environment_value("PORT") or spec.port or default_port)
    port = positive_int(port_value, "port")
    if port > 65535:
        raise ValueError("Port must be between 1 and 65535")
    host = args.host or environment_value("HOST") or spec.host or "127.0.0.1"
    explicit = (args.model_dir or os.environ.get("LOCAL_LLM_MODEL_DIR")
                or environment_value("MODEL_DIR"))
    installation = resolve_installation(spec, paths, explicit=explicit)
    model_dir = Path(installation.path)
    if not model_dir.is_dir():
        raise ValueError(f"Model directory does not exist: {model_dir}; run ./manage.sh download {spec.key}")
    return ServiceSettings(paths, spec.key, spec.alias, model_dir, host, port, dict(spec.params))


def run_service(handler_class, settings):
    """Load a handler factory, bind and publish under one startup lock."""
    from ..artifacts.paths import installation_lock
    from ..lifecycle.manager import instance_lock
    from ..lifecycle.observe import publish_pid, remove_pid

    owned_record = os.environ.get("LOCAL_LLM_MANAGED_INSTANCE") != "1"
    artifact_guard = installation_lock(settings.paths, settings.model_dir) if owned_record else nullcontext()
    instance_guard = instance_lock(settings.paths, settings.key) if owned_record else nullcontext()
    with artifact_guard, instance_guard:
        explicit_key = os.environ.get("API_KEY") or None
        explicit_file = os.environ.get("API_KEY_FILE")
        if explicit_key and explicit_file:
            raise ValueError("API_KEY and API_KEY_FILE cannot both be set")
        key_path = Path(explicit_file or settings.paths.api_key).expanduser()
        if not key_path.is_absolute():
            key_path = settings.paths.root / key_path
        if explicit_file and not key_path.is_file():
            raise ValueError(f"API key file does not exist: {key_path}")
        # A callable factory keeps expensive model loading inside the startup
        # lock. Manager-started children are already protected by their parent.
        if not isinstance(handler_class, type):
            handler_class = handler_class()
        handler = make_handler(handler_class, api_key_path=key_path,
                               api_key=explicit_key)
        # HTTPServer binds before publishing, so a failed bind never advertises
        # a healthy instance. Failed publication must also close the listener.
        server = ThreadingHTTPServer((settings.host, settings.port), handler)
        try:
            if owned_record:
                publish_pid(settings.paths, settings.key, server.server_port, settings.alias,
                            model_path=settings.model_dir,
                            api_key_file=str(key_path.resolve()) if not explicit_key and key_path.is_file() else None,
                            auth_source='inline' if explicit_key else ('file' if key_path.is_file() else 'none'))
        except BaseException:
            server.server_close()
            raise

    previous_handlers = {}

    def stop(signum, frame):
        raise KeyboardInterrupt

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[sig] = signal.signal(sig, stop)
        print(f"{settings.alias}: http://{settings.host}:{server.server_port}{handler.endpoint}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if owned_record:
            remove_pid(settings.paths, settings.key, expected_pid=os.getpid())
        for sig, previous in previous_handlers.items():
            signal.signal(sig, previous)


def report_startup_error(exc, extra):
    if isinstance(exc, ImportError):
        print(f"Missing {extra} dependencies. Install with: python -m pip install -e '.[{extra}]'", file=sys.stderr)
    else:
        print(f"Unable to start service: {exc}", file=sys.stderr)
    return 1
