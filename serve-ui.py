#!/usr/bin/env python3
"""
Local LLM Deploy — 前端静态服务 + 多模型 API 代理 + 推理请求队列
自动读取 .api-key，支持多个 llama-server 后端实例的路由代理

路由规则:
  /v1/models             → OpenAI 标准模型列表
  /v1/chat/completions   → 按请求体 model 字段路由到对应后端（推荐）
  /api/models            → 返回运行中的模型列表（从 run/*.pid 读取）
  /api/<model-name>/*    → 代理到该模型对应的后端端口
  /api/*                 → 代理到默认（第一个运行中的）后端

推理请求队列:
  推理接口按模型串行处理，防止请求互相取消。
  排队期间对流式请求发送 SSE keepalive 保持连接。
"""
import json
import os
import queue
import socket
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(SCRIPT_DIR, "static")
API_KEY_FILE = os.path.join(SCRIPT_DIR, ".api-key")
RUN_DIR = os.path.join(SCRIPT_DIR, "run")
API_PROXY_TIMEOUT = int(os.environ.get("API_PROXY_TIMEOUT", "3600"))
MONITOR_PROXY_TIMEOUT = int(os.environ.get("MONITOR_PROXY_TIMEOUT", "8"))

INFERENCE_PATHS = frozenset({
    "v1/chat/completions", "v1/completions",
    "chat/completions", "completions",
})
MAX_QUEUE_DEPTH = int(os.environ.get("MAX_QUEUE_DEPTH", "5"))
QUEUE_KEEPALIVE_SEC = int(os.environ.get("QUEUE_KEEPALIVE_SEC", "5"))


def _log(msg):
    sys.stderr.write(f"[serve-ui] {msg}\n")
    sys.stderr.flush()


def load_api_key():
    if os.path.isfile(API_KEY_FILE):
        with open(API_KEY_FILE, "r") as f:
            return f.readline().strip().strip("\r\n") or None
    return None


def get_running_models():
    """从 run/*.pid 读取运行中的模型列表，返回 {name: {pid, port, model}}"""
    models = {}
    if not os.path.isdir(RUN_DIR):
        return models
    for fname in os.listdir(RUN_DIR):
        if not fname.endswith(".pid"):
            continue
        fpath = os.path.join(RUN_DIR, fname)
        try:
            with open(fpath) as f:
                lines = f.read().strip().split("\n")
            pid = int(lines[0].strip())
            port = int(lines[1].strip()) if len(lines) > 1 else 8001
            name = fname[:-4]
            model = lines[2].strip() if len(lines) > 2 else name
            if not model:
                model = name
            try:
                os.kill(pid, 0)
            except OSError:
                os.remove(fpath)
                continue
            models[name] = {"pid": pid, "port": port, "model": model}
        except (ValueError, IndexError, FileNotFoundError):
            continue
    return models


def get_default_port():
    """获取默认后端端口（第一个运行中的模型，或 8001）"""
    models = get_running_models()
    if models:
        first = next(iter(models.values()))
        return first["port"]
    return int(os.environ.get("LLAMA_PORT", "8001"))


# ── 推理请求队列 ──────────────────────────────────────────────


class InferenceGate:
    """Per-model concurrency gate: semaphore(1) + queue depth tracking."""

    def __init__(self):
        self.semaphore = threading.Semaphore(1)
        self._lock = threading.Lock()
        self._depth = 0

    @property
    def queue_depth(self):
        return self._depth

    def enter_queue(self):
        """Try to enter the queue. Returns False if full."""
        with self._lock:
            if self._depth >= MAX_QUEUE_DEPTH:
                return False
            self._depth += 1
            return True

    def leave_queue(self):
        with self._lock:
            self._depth = max(0, self._depth - 1)


_gates_lock = threading.Lock()
_inference_gates: dict = {}


def get_inference_gate(model_name):
    with _gates_lock:
        if model_name not in _inference_gates:
            _inference_gates[model_name] = InferenceGate()
        return _inference_gates[model_name]


# ── HTTP Handler ──────────────────────────────────────────────


class ProxyHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def end_headers(self):
        if not self.path.startswith(("/api/", "/v1/")):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_GET(self):
        if self.path.startswith("/api/"):
            self.proxy_request("GET")
        elif self.path.startswith("/v1/"):
            self.openai_request("GET")
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/"):
            self.proxy_request("POST")
        elif self.path.startswith("/v1/"):
            self.openai_request("POST")
        else:
            super().do_POST()

    def resolve_backend(self, api_path):
        """解析 API 路径，返回 (backend_url, remaining_path, model_name)"""
        if api_path.lstrip("/") == "models":
            return None, "models", None

        models = get_running_models()
        parts = api_path.lstrip("/").split("/", 1)
        if len(parts) >= 1 and parts[0] in models:
            model_name = parts[0]
            port = models[model_name]["port"]
            remaining = "/" + parts[1] if len(parts) > 1 else "/"
            return f"http://127.0.0.1:{port}", remaining, model_name

        if models:
            default_name = next(iter(models))
            return (
                f"http://127.0.0.1:{models[default_name]['port']}",
                api_path,
                default_name,
            )
        port = int(os.environ.get("LLAMA_PORT", "8001"))
        return f"http://127.0.0.1:{port}", api_path, None

    # ── 请求路由 ──

    def proxy_request(self, method):
        api_path = self.path[4:]  # strip /api
        backend_url, remaining_path, model_name = self.resolve_backend(api_path)

        if backend_url is None and remaining_path == "models":
            self.handle_models_endpoint()
            return

        url = backend_url.rstrip("/") + remaining_path
        clean_path = remaining_path.lstrip("/").split("?")[0]

        body = None
        if method == "POST":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len else None

        if clean_path in INFERENCE_PATHS and model_name:
            self._gated_inference(url, method, body, model_name)
        else:
            monitor_paths = ("health", "metrics", "slots")
            timeout = (
                MONITOR_PROXY_TIMEOUT
                if clean_path in monitor_paths
                else API_PROXY_TIMEOUT
            )
            self._forward_request(url, method, body, timeout)

    # ── OpenAI 兼容路由 (/v1/*) ──

    def _check_auth(self):
        """校验 Bearer token，若 .api-key 存在则要求客户端携带。
        返回 True 表示通过（或无需认证），False 表示已返回 401。"""
        expected = load_api_key()
        if not expected:
            return True
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {expected}":
            return True
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        err = json.dumps(
            {"error": {"message": "Invalid API key", "type": "invalid_request_error"}},
        )
        self.wfile.write(err.encode("utf-8"))
        return False

    def openai_request(self, method):
        """处理 /v1/* 路由，通过请求体 model 字段路由到对应后端"""
        if not self._check_auth():
            return
        clean_path = self.path.lstrip("/").split("?")[0]

        if clean_path == "v1/models":
            self.handle_openai_models()
            return

        body = None
        if method == "POST":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len else None

        if clean_path in INFERENCE_PATHS:
            model_name, backend_url = self._resolve_model_from_body(body)
            if not backend_url:
                self.send_error(503, "No running models")
                return
            url = backend_url.rstrip("/") + self.path
            self._gated_inference(url, method, body, model_name)
        else:
            models = get_running_models()
            if models:
                default_name = next(iter(models))
                backend_url = (
                    f"http://127.0.0.1:{models[default_name]['port']}"
                )
            else:
                port = int(os.environ.get("LLAMA_PORT", "8001"))
                backend_url = f"http://127.0.0.1:{port}"
            url = backend_url.rstrip("/") + self.path
            self._forward_request(url, method, body, API_PROXY_TIMEOUT)

    def _resolve_model_from_body(self, body):
        """从请求体的 model 字段匹配运行中后端，返回 (model_name, backend_url)。
        匹配顺序：alias 精确匹配 → 短名精确匹配 → 默认第一个。"""
        models = get_running_models()
        if not models:
            return None, None

        requested = None
        if body:
            try:
                requested = json.loads(body).get("model")
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        if requested:
            for name, info in models.items():
                if info.get("model") == requested:
                    return name, f"http://127.0.0.1:{info['port']}"
            if requested in models:
                info = models[requested]
                return requested, f"http://127.0.0.1:{info['port']}"

        default_name = next(iter(models))
        return (
            default_name,
            f"http://127.0.0.1:{models[default_name]['port']}",
        )

    # ── 推理门控 ──

    def _gated_inference(self, url, method, body, model_name):
        gate = get_inference_gate(model_name)
        client_ip = self.client_address[0]

        if not gate.enter_queue():
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "30")
            self.end_headers()
            err = json.dumps(
                {"error": {"message": "推理队列已满，请稍后重试", "type": "server_error"}},
                ensure_ascii=False,
            )
            self.wfile.write(err.encode("utf-8"))
            return

        is_stream = False
        if body:
            try:
                is_stream = json.loads(body).get("stream", False)
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        t0 = time.monotonic()
        try:
            # Fast path: no contention
            if gate.semaphore.acquire(blocking=False):
                try:
                    _log(f"[infer] {client_ip} → {model_name}")
                    if is_stream:
                        self._send_stream_headers()
                        self._forward_with_keepalive(url, method, body)
                    else:
                        self._forward_request(url, method, body, API_PROXY_TIMEOUT)
                finally:
                    gate.semaphore.release()
                    _log(
                        f"[infer] {client_ip} → {model_name} "
                        f"done ({time.monotonic() - t0:.1f}s)"
                    )
                return

            # Slow path: queue wait
            _log(
                f"[queue] {client_ip} 排队等待 {model_name} "
                f"(depth={gate.queue_depth})"
            )

            if is_stream:
                self._queued_stream(gate, url, method, body, client_ip, model_name)
            else:
                self._queued_block(gate, url, method, body, client_ip, model_name)

            _log(
                f"[infer] {client_ip} → {model_name} "
                f"done ({time.monotonic() - t0:.1f}s, queued)"
            )
        finally:
            gate.leave_queue()

    def _queued_stream(self, gate, url, method, body, client_ip, model_name):
        """Streaming request: send headers + keepalive while queued, then relay."""
        self._send_stream_headers()

        while not gate.semaphore.acquire(timeout=QUEUE_KEEPALIVE_SEC):
            try:
                self._write_chunk(b": keepalive\n\n")
            except (BrokenPipeError, ConnectionResetError, OSError):
                _log(f"[queue] {client_ip} 断开，取消排队 {model_name}")
                return

        try:
            _log(f"[infer] {client_ip} → {model_name} (queued)")
            self._forward_with_keepalive(url, method, body)
        finally:
            gate.semaphore.release()

    def _queued_block(self, gate, url, method, body, client_ip, model_name):
        """Non-streaming request: block until semaphore available."""
        if not gate.semaphore.acquire(timeout=API_PROXY_TIMEOUT):
            self.send_response(504)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            err = json.dumps(
                {"error": {"message": "队列等待超时", "type": "server_error"}},
                ensure_ascii=False,
            )
            self.wfile.write(err.encode("utf-8"))
            return
        try:
            _log(f"[infer] {client_ip} → {model_name} (queued)")
            self._forward_request(url, method, body, API_PROXY_TIMEOUT)
        finally:
            gate.semaphore.release()

    # ── 转发与保活 ──

    def _build_backend_request(self, url, method, body):
        headers = {}
        for k, v in self.headers.items():
            if k.lower() not in ("host", "connection"):
                headers[k] = v
        api_key = load_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if method == "GET":
            return urllib.request.Request(url, headers=headers, method="GET")
        req = urllib.request.Request(
            url, data=body, headers=headers, method="POST"
        )
        if body:
            req.add_header(
                "Content-Type",
                self.headers.get("Content-Type", "application/json"),
            )
        return req

    def _send_stream_headers(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _write_chunk(self, data):
        self.wfile.write(("%x\r\n" % len(data)).encode())
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _forward_request(self, url, method, body, timeout):
        """Forward request and relay full response (headers + body)."""
        try:
            req = self._build_backend_request(url, method, body)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() not in ("transfer-encoding", "content-length"):
                        self.send_header(k, v)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    self._write_chunk(chunk)
                self.wfile.write(b"0\r\n\r\n")
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(e.read())
        except urllib.error.URLError as e:
            if (
                isinstance(getattr(e, "reason", None), socket.timeout)
                or "timed out" in str(e).lower()
            ):
                self.send_response(504)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    '{"error":"推理中，监控接口被阻塞，请稍后刷新"}'.encode("utf-8")
                )
            else:
                self.send_error(502, str(e))
        except Exception as e:
            self.send_error(502, str(e))

    def _forward_with_keepalive(self, url, method, body):
        """Forward to backend with keepalive during long prompt processing.
        Stream headers must already be sent before calling this method.
        Uses a reader thread so the main thread can send keepalive while
        the backend is processing the prompt (no data flowing yet)."""
        req = self._build_backend_request(url, method, body)
        data_q = queue.Queue()

        def reader():
            try:
                with urllib.request.urlopen(req, timeout=API_PROXY_TIMEOUT) as resp:
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        data_q.put(("data", chunk))
                data_q.put(("done", None))
            except urllib.error.HTTPError as e:
                try:
                    err_body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    err_body = ""
                data_q.put(("http_error", (e.code, err_body)))
            except Exception as e:
                data_q.put(("error", str(e)))

        t = threading.Thread(target=reader, daemon=True)
        t.start()

        try:
            while True:
                try:
                    msg_type, payload = data_q.get(timeout=QUEUE_KEEPALIVE_SEC)
                except queue.Empty:
                    self._write_chunk(b": keepalive\n\n")
                    continue

                if msg_type == "data":
                    self._write_chunk(payload)
                elif msg_type == "done":
                    break
                elif msg_type == "http_error":
                    code, err_body = payload
                    try:
                        err_json = json.loads(err_body)
                    except (json.JSONDecodeError, ValueError):
                        err_json = {
                            "error": {
                                "message": f"Backend error {code}",
                                "type": "server_error",
                            }
                        }
                    self._write_chunk(
                        f"data: {json.dumps(err_json)}\n\ndata: [DONE]\n\n".encode()
                    )
                    break
                elif msg_type == "error":
                    err = {
                        "error": {"message": payload, "type": "server_error"}
                    }
                    self._write_chunk(
                        f"data: {json.dumps(err)}\n\ndata: [DONE]\n\n".encode()
                    )
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            _log("[infer] 客户端断开")
        finally:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    # ── 端点处理 ──

    def handle_openai_models(self):
        """返回 OpenAI 标准格式的 /v1/models 模型列表"""
        models = get_running_models()
        data = []
        created = int(time.time())
        for name, info in models.items():
            alias = info.get("model", name)
            data.append(
                {
                    "id": alias,
                    "object": "model",
                    "created": created,
                    "owned_by": "local",
                }
            )
            if name != alias:
                data.append(
                    {
                        "id": name,
                        "object": "model",
                        "created": created,
                        "owned_by": "local",
                    }
                )
        result = {"object": "list", "data": data}
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_models_endpoint(self):
        """返回运行中的模型列表，包含队列状态"""
        models = get_running_models()
        result = []
        for name, info in models.items():
            gate = _inference_gates.get(name)
            result.append(
                {
                    "name": name,
                    "model": info.get("model", name),
                    "port": info["port"],
                    "pid": info["pid"],
                    "queue": gate.queue_depth if gate else 0,
                }
            )
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        if not self.path.startswith(("/api/", "/v1/")):
            super().log_message(format, *args)


def main():
    port = int(os.environ.get("UI_PORT", "8888"))
    api_key = load_api_key()
    models = get_running_models()

    print(f"前端服务: http://localhost:{port}/")
    print(f"  监控: http://localhost:{port}/monitor.html")
    print(f"  Chat 流式: http://localhost:{port}/chat.html")
    print()
    print("API 代理路由:")
    print(f"  /api/models → 模型列表")
    if models:
        for name, info in models.items():
            print(f"  /api/{name}/* → http://127.0.0.1:{info['port']}")
        first_name = next(iter(models))
        print(
            f"  /api/* → http://127.0.0.1:{models[first_name]['port']}"
            f" (默认: {first_name})"
        )
    else:
        default_port = int(os.environ.get("LLAMA_PORT", "8001"))
        print(
            f"  /api/* → http://127.0.0.1:{default_port}"
            f" (无运行中模型，使用默认端口)"
        )
    print()
    print(f"OpenAI 兼容: http://localhost:{port}/v1  (通过 model 字段自动路由)")
    print(f"推理队列: 最大排队 {MAX_QUEUE_DEPTH}，保活间隔 {QUEUE_KEEPALIVE_SEC}s")
    if api_key:
        print("认证: 已从 .api-key 加载")
    else:
        print("认证: 未启用（无 .api-key）")
    print()

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadingHTTPServer(("", port), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
