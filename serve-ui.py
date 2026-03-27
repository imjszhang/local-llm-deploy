#!/usr/bin/env python3
"""
Local LLM Deploy — 前端静态服务 + 多模型 API 代理 + 推理请求队列
自动读取 .api-key，支持多个 llama-server 后端实例的路由代理

路由规则:
  /v1/models             → OpenAI 标准模型列表
  /v1/chat/completions   → 按请求体 model 字段路由到对应后端（推荐）
  /v1/embeddings         → 按请求体 model 字段路由到 embedding 后端
  /api/models            → 返回运行中的模型列表（从 run/*.pid 读取）
  /api/<model-name>/*    → 代理到该模型对应的后端端口
  /api/*                 → 代理到默认（第一个运行中的）后端

推理请求队列（KV 预算感知）:
  每模型按 KV token 预算控制并发（短请求可多路并行，长请求自动串行）。
  全局跨模型并发上限防止统一内存带宽被打满。
  排队期间对流式请求发送 SSE keepalive 保持连接。
"""
import errno
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
EMBEDDING_PATHS = frozenset({
    "v1/embeddings", "embeddings",
})
MAX_QUEUE_DEPTH = int(os.environ.get("MAX_QUEUE_DEPTH", "5"))
QUEUE_KEEPALIVE_SEC = int(os.environ.get("QUEUE_KEEPALIVE_SEC", "5"))
MAX_GLOBAL_CONCURRENT = int(os.environ.get("MAX_GLOBAL_CONCURRENT", "3"))
KV_CHARS_PER_TOKEN = float(os.environ.get("KV_CHARS_PER_TOKEN", "2.5"))
MODELS_JSON = os.path.join(SCRIPT_DIR, "models.json")
ACCESS_LOG_FILE = os.environ.get("SERVE_UI_ACCESS_LOG", "").strip() or None
LOG_BODY = os.environ.get("SERVE_UI_LOG_BODY", "").strip().lower() in ("1", "true", "yes")

_access_log_lock = threading.Lock()


def _log(msg):
    sys.stderr.write(f"[serve-ui] {msg}\n")
    sys.stderr.flush()


def _is_client_disconnected(exc):
    """客户端已关闭连接时，再往 socket 写会触发这些错误；不应再 send_error。"""
    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
        return True
    if isinstance(exc, OSError) and exc.errno in (errno.EPIPE, errno.ECONNRESET):
        return True
    return False


def _parse_body_summary(body, kind="infer"):
    """从 body 解析摘要字段，不抛异常。kind: 'infer' | 'embed'"""
    summary = {}
    if not body:
        return summary
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        summary["parse_error"] = True
        return summary
    if not isinstance(data, dict):
        return summary
    if data.get("model") is not None:
        summary["model"] = data["model"]
    if kind == "infer":
        summary["stream"] = data.get("stream", False)
        messages = data.get("messages")
        if isinstance(messages, list):
            summary["n_messages"] = len(messages)
        if "max_tokens" in data:
            summary["max_tokens"] = data["max_tokens"]
        if "temperature" in data:
            summary["temperature"] = data["temperature"]
    else:
        inp = data.get("input")
        if isinstance(inp, list):
            summary["input_count"] = len(inp)
        elif isinstance(inp, str):
            summary["input_len"] = len(inp)
    return summary


def _log_request_summary(kind, path, method, client_ip, model_name, body_summary, full_body=None):
    """记录请求摘要到 stderr。JSONL 在响应完成后由 _log_request_and_response 写入。"""
    parts = [f"[{kind}]", client_ip, "→", model_name, f"path={path}"]
    for k, v in body_summary.items():
        parts.append(f"{k}={v}")
    _log(" ".join(str(p) for p in parts))


def _log_request_and_response(
    kind, path, method, client_ip, model_name, body_summary,
    full_body=None, response_body=None,
):
    """仅在配置了 ACCESS_LOG_FILE 时写入一行 JSONL，含请求信息与 response_body。"""
    if not ACCESS_LOG_FILE:
        return
    ts = time.time()
    record = {
        "ts": ts,
        "kind": kind,
        "path": path,
        "method": method,
        "client_ip": client_ip,
        "model_name": model_name,
        "body_summary": body_summary,
    }
    if full_body is not None and LOG_BODY:
        record["body"] = full_body
    if response_body is not None:
        if isinstance(response_body, bytes):
            record["response_body"] = response_body.decode("utf-8", errors="replace")
        else:
            record["response_body"] = response_body
    else:
        record["response_body"] = None
    try:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with _access_log_lock:
            with open(ACCESS_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError as e:
        _log(f"access_log write failed: {e}")


def load_api_key():
    if os.path.isfile(API_KEY_FILE):
        with open(API_KEY_FILE, "r") as f:
            return f.readline().strip().strip("\r\n") or None
    return None


def _load_models_json():
    """Load models.json, cached per-process with 30s TTL."""
    now = time.monotonic()
    cache = getattr(_load_models_json, "_cache", None)
    if cache and now - cache[1] < 30:
        return cache[0]
    try:
        with open(MODELS_JSON) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    _load_models_json._cache = (data, now)
    return data


def _get_model_params(model_name):
    """Return params dict for a model from models.json, or empty dict."""
    data = _load_models_json()
    model = data.get(model_name)
    if model and isinstance(model, dict):
        return model.get("params", {})
    return {}


def estimate_kv_tokens(body, model_name):
    """Estimate KV token usage from request body.

    Returns (estimated_kv, max_tokens_used) where estimated_kv is
    prompt_estimate + max_tokens and max_tokens_used is the generation
    cap taken from the request or model config fallback.
    """
    prompt_chars = 0
    max_tokens = 0
    if body:
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            data = {}
        if isinstance(data, dict):
            max_tokens = data.get("max_tokens") or 0
            messages = data.get("messages")
            if isinstance(messages, list):
                for msg in messages:
                    content = msg.get("content") if isinstance(msg, dict) else None
                    if isinstance(content, str):
                        prompt_chars += len(content)
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and isinstance(part.get("text"), str):
                                prompt_chars += len(part["text"])
    if not max_tokens:
        params = _get_model_params(model_name)
        max_tokens = params.get("n_predict", 32768)
    prompt_tokens_est = int(prompt_chars / KV_CHARS_PER_TOKEN) if prompt_chars else 0
    return prompt_tokens_est + max_tokens, max_tokens


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


# ── 推理请求队列（KV 预算感知） ──────────────────────────────────


class ModelBudgetGate:
    """Per-model KV budget pool with concurrency control.

    Replaces the old Semaphore(1)-based InferenceGate.  Allows up to
    ``max_slots`` concurrent requests as long as the sum of their
    estimated KV token usage stays within ``total_budget``.
    """

    def __init__(self, model_name, ctx_size=131072, max_slots=1,
                 kv_budget_ratio=0.9):
        self.model_name = model_name
        self.total_budget = int(ctx_size * kv_budget_ratio)
        self.max_slots = max(1, max_slots)
        self._condition = threading.Condition()
        self._active_slots = 0
        self._used_budget = 0
        self._queue_depth = 0

    @property
    def active_slots(self):
        return self._active_slots

    @property
    def used_budget(self):
        return self._used_budget

    @property
    def queue_depth(self):
        return self._queue_depth

    def enter_queue(self):
        with self._condition:
            if self._queue_depth >= MAX_QUEUE_DEPTH:
                return False
            self._queue_depth += 1
            return True

    def leave_queue(self):
        with self._condition:
            self._queue_depth = max(0, self._queue_depth - 1)

    def _can_acquire(self, estimated_kv):
        if self._active_slots >= self.max_slots:
            return False
        if self._used_budget + estimated_kv <= self.total_budget:
            return True
        # First request always allowed to avoid deadlock
        return self._active_slots == 0

    def acquire(self, estimated_kv, timeout=None):
        """Try to acquire a slot with the given KV budget.

        Returns True if acquired, False on timeout.
        """
        with self._condition:
            if self._can_acquire(estimated_kv):
                self._active_slots += 1
                self._used_budget += estimated_kv
                return True
            if timeout is not None and timeout <= 0:
                return False
            deadline = (time.monotonic() + timeout) if timeout else None
            while True:
                remaining = None
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                self._condition.wait(timeout=remaining)
                if self._can_acquire(estimated_kv):
                    self._active_slots += 1
                    self._used_budget += estimated_kv
                    return True

    def acquire_nonblocking(self, estimated_kv):
        """Non-blocking acquire. Returns True if immediately available."""
        with self._condition:
            if self._can_acquire(estimated_kv):
                self._active_slots += 1
                self._used_budget += estimated_kv
                return True
            return False

    def release(self, estimated_kv):
        with self._condition:
            self._used_budget = max(0, self._used_budget - estimated_kv)
            self._active_slots = max(0, self._active_slots - 1)
            self._condition.notify_all()

    def budget_snapshot(self):
        """Return a dict describing current budget state."""
        with self._condition:
            return {
                "total": self.total_budget,
                "used": self._used_budget,
                "active_slots": self._active_slots,
                "max_slots": self.max_slots,
                "queue_depth": self._queue_depth,
            }


class GlobalBudgetGate:
    """Cross-model global concurrency limiter."""

    def __init__(self, max_concurrent):
        self._semaphore = threading.Semaphore(max(1, max_concurrent))
        self._max = max(1, max_concurrent)
        self._lock = threading.Lock()
        self._active = 0

    @property
    def active(self):
        return self._active

    @property
    def max_concurrent(self):
        return self._max

    def acquire(self, timeout=None):
        ok = self._semaphore.acquire(timeout=timeout)
        if ok:
            with self._lock:
                self._active += 1
        return ok

    def acquire_nonblocking(self):
        ok = self._semaphore.acquire(blocking=False)
        if ok:
            with self._lock:
                self._active += 1
        return ok

    def release(self):
        with self._lock:
            self._active = max(0, self._active - 1)
        self._semaphore.release()

    def snapshot(self):
        with self._lock:
            return {"active": self._active, "max": self._max}


_gates_lock = threading.Lock()
_inference_gates: dict = {}
_global_gate = GlobalBudgetGate(MAX_GLOBAL_CONCURRENT)


def get_inference_gate(model_name):
    with _gates_lock:
        if model_name not in _inference_gates:
            params = _get_model_params(model_name)
            ctx_size = params.get("ctx_size", 131072)
            max_slots = params.get("max_concurrent", 1)
            kv_ratio = params.get("kv_budget_ratio", 0.9)
            _inference_gates[model_name] = ModelBudgetGate(
                model_name, ctx_size=ctx_size,
                max_slots=max_slots, kv_budget_ratio=kv_ratio,
            )
        return _inference_gates[model_name]


def get_global_gate():
    return _global_gate


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
        elif clean_path in EMBEDDING_PATHS and model_name:
            body_summary = _parse_body_summary(body, "embed")
            full_body = body.decode("utf-8", errors="replace") if (LOG_BODY and body) else None
            _log_request_summary("embed", self.path, method, self.client_address[0], model_name, body_summary, full_body)
            _log(f"[embed] {self.client_address[0]} → {model_name}")
            capture = bool(ACCESS_LOG_FILE)
            resp_body = self._forward_request(url, method, body, API_PROXY_TIMEOUT, capture_response=capture)
            _log_request_and_response(
                "embed", self.path, method, self.client_address[0], model_name,
                body_summary, full_body, resp_body,
            )
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
                self._send_error_safe(503, "No running models")
                return
            url = backend_url.rstrip("/") + self.path
            self._gated_inference(url, method, body, model_name)
        elif clean_path in EMBEDDING_PATHS:
            model_name, backend_url = self._resolve_model_from_body(body)
            if not backend_url:
                self._send_error_safe(503, "No running embedding models")
                return
            url = backend_url.rstrip("/") + "/v1/embeddings"
            body_summary = _parse_body_summary(body, "embed")
            full_body = body.decode("utf-8", errors="replace") if (LOG_BODY and body) else None
            _log_request_summary("embed", self.path, method, self.client_address[0], model_name, body_summary, full_body)
            _log(f"[embed] {self.client_address[0]} → {model_name}")
            capture = bool(ACCESS_LOG_FILE)
            resp_body = self._forward_request(url, method, body, API_PROXY_TIMEOUT, capture_response=capture)
            _log_request_and_response(
                "embed", self.path, method, self.client_address[0], model_name,
                body_summary, full_body, resp_body,
            )
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
        g_gate = get_global_gate()
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

        body_summary = _parse_body_summary(body, "infer")
        full_body = body.decode("utf-8", errors="replace") if (LOG_BODY and body) else None
        _log_request_summary("infer", self.path, method, client_ip, model_name, body_summary, full_body)

        est_kv, _ = estimate_kv_tokens(body, model_name)

        is_stream = False
        if body:
            try:
                is_stream = json.loads(body).get("stream", False)
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass

        t0 = time.monotonic()
        try:
            # Fast path: try both gates non-blocking
            got_global = g_gate.acquire_nonblocking()
            got_model = got_global and gate.acquire_nonblocking(est_kv)

            if got_global and got_model:
                snap = gate.budget_snapshot()
                _log(
                    f"[budget] {client_ip} → {model_name} "
                    f"est={est_kv} used={snap['used']}/{snap['total']} "
                    f"slots={snap['active_slots']}/{snap['max_slots']} → ALLOW"
                )
                try:
                    _log(f"[infer] {client_ip} → {model_name}")
                    capture = bool(ACCESS_LOG_FILE)
                    if is_stream:
                        self._send_stream_headers()
                        resp_body = self._forward_with_keepalive(url, method, body, capture_response=capture)
                    else:
                        resp_body = self._forward_request(url, method, body, API_PROXY_TIMEOUT, capture_response=capture)
                    _log_request_and_response(
                        "infer", self.path, method, client_ip, model_name,
                        body_summary, full_body, resp_body,
                    )
                finally:
                    gate.release(est_kv)
                    g_gate.release()
                    _log(
                        f"[infer] {client_ip} → {model_name} "
                        f"done ({time.monotonic() - t0:.1f}s)"
                    )
                return

            if got_global:
                g_gate.release()

            # Slow path: queue wait
            snap = gate.budget_snapshot()
            _log(
                f"[budget] {client_ip} → {model_name} "
                f"est={est_kv} used={snap['used']}/{snap['total']} "
                f"slots={snap['active_slots']}/{snap['max_slots']} → QUEUE"
            )
            _log(
                f"[queue] {client_ip} 排队等待 {model_name} "
                f"(depth={gate.queue_depth})"
            )

            if is_stream:
                self._queued_stream(gate, g_gate, est_kv, url, method, body, client_ip, model_name, body_summary, full_body)
            else:
                self._queued_block(gate, g_gate, est_kv, url, method, body, client_ip, model_name, body_summary, full_body)

            _log(
                f"[infer] {client_ip} → {model_name} "
                f"done ({time.monotonic() - t0:.1f}s, queued)"
            )
        finally:
            gate.leave_queue()

    def _queued_stream(self, gate, g_gate, est_kv, url, method, body,
                       client_ip, model_name, body_summary=None, full_body=None):
        """Streaming request: send headers + keepalive while queued, then relay."""
        self._send_stream_headers()

        # Wait for model gate (with keepalive)
        while not gate.acquire(est_kv, timeout=QUEUE_KEEPALIVE_SEC):
            try:
                self._write_chunk(b": keepalive\n\n")
            except (BrokenPipeError, ConnectionResetError, OSError):
                _log(f"[queue] {client_ip} 断开，取消排队 {model_name}")
                return

        # Got model gate; now acquire global (with keepalive)
        while not g_gate.acquire(timeout=QUEUE_KEEPALIVE_SEC):
            try:
                self._write_chunk(b": keepalive\n\n")
            except (BrokenPipeError, ConnectionResetError, OSError):
                _log(f"[queue] {client_ip} 断开，取消排队 {model_name}")
                gate.release(est_kv)
                return

        try:
            _log(f"[infer] {client_ip} → {model_name} (queued)")
            capture = bool(ACCESS_LOG_FILE)
            resp_body = self._forward_with_keepalive(url, method, body, capture_response=capture)
            _log_request_and_response(
                "infer", self.path, method, client_ip, model_name,
                body_summary or {}, full_body, resp_body,
            )
        finally:
            gate.release(est_kv)
            g_gate.release()

    def _queued_block(self, gate, g_gate, est_kv, url, method, body,
                      client_ip, model_name, body_summary=None, full_body=None):
        """Non-streaming request: block until budget available."""
        if not gate.acquire(est_kv, timeout=API_PROXY_TIMEOUT):
            self.send_response(504)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            err = json.dumps(
                {"error": {"message": "队列等待超时", "type": "server_error"}},
                ensure_ascii=False,
            )
            self.wfile.write(err.encode("utf-8"))
            return

        if not g_gate.acquire(timeout=API_PROXY_TIMEOUT):
            gate.release(est_kv)
            self.send_response(504)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            err = json.dumps(
                {"error": {"message": "全局队列等待超时", "type": "server_error"}},
                ensure_ascii=False,
            )
            self.wfile.write(err.encode("utf-8"))
            return

        try:
            _log(f"[infer] {client_ip} → {model_name} (queued)")
            capture = bool(ACCESS_LOG_FILE)
            resp_body = self._forward_request(url, method, body, API_PROXY_TIMEOUT, capture_response=capture)
            _log_request_and_response(
                "infer", self.path, method, client_ip, model_name,
                body_summary or {}, full_body, resp_body,
            )
        finally:
            gate.release(est_kv)
            g_gate.release()

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

    def _send_error_safe(self, code, message=""):
        """向客户端发送错误页；若对端已断开则静默结束，避免 BrokenPipe 链式异常。"""
        try:
            self.send_error(code, message)
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            if not _is_client_disconnected(e):
                raise

    def _forward_request(self, url, method, body, timeout, capture_response=False):
        """Forward request and relay full response (headers + body).
        If capture_response is True, returns the response body bytes; otherwise returns None."""
        out = [] if capture_response else None
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
                    if out is not None:
                        out.append(chunk)
                self.wfile.write(b"0\r\n\r\n")
            return b"".join(out) if out is not None else None
        except urllib.error.HTTPError as e:
            try:
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                err_body = e.read()
                self.wfile.write(err_body)
            except (BrokenPipeError, ConnectionResetError, OSError) as w:
                if _is_client_disconnected(w):
                    return None
                raise
            if out is not None:
                return err_body
            return None
        except urllib.error.URLError as e:
            try:
                if (
                    isinstance(getattr(e, "reason", None), socket.timeout)
                    or "timed out" in str(e).lower()
                ):
                    self.send_response(504)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(
                        '{"error":"推理中，监控接口被阻塞，请稍后刷新"}'.encode(
                            "utf-8"
                        )
                    )
                else:
                    self._send_error_safe(502, str(e))
            except (BrokenPipeError, ConnectionResetError, OSError) as w:
                if _is_client_disconnected(w):
                    return None
                raise
            return None
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            if _is_client_disconnected(e):
                return None
            raise
        except Exception as e:
            self._send_error_safe(502, str(e))
            return None

    def _forward_with_keepalive(self, url, method, body, capture_response=False):
        """Forward to backend with keepalive during long prompt processing.
        Stream headers must already be sent before calling this method.
        Uses a reader thread so the main thread can send keepalive while
        the backend is processing the prompt (no data flowing yet).
        If capture_response is True, returns the concatenated response body bytes."""
        req = self._build_backend_request(url, method, body)
        data_q = queue.Queue()
        out = [] if capture_response else None

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
                    if out is not None:
                        out.append(payload)
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
                    chunk_data = f"data: {json.dumps(err_json)}\n\ndata: [DONE]\n\n".encode()
                    self._write_chunk(chunk_data)
                    if out is not None:
                        out.append(chunk_data)
                    break
                elif msg_type == "error":
                    err = {
                        "error": {"message": payload, "type": "server_error"}
                    }
                    chunk_data = f"data: {json.dumps(err)}\n\ndata: [DONE]\n\n".encode()
                    self._write_chunk(chunk_data)
                    if out is not None:
                        out.append(chunk_data)
                    break
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            if _is_client_disconnected(e):
                _log("[infer] 客户端断开")
            else:
                raise
        finally:
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        return b"".join(out) if out is not None else None

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
        """返回运行中的模型列表，包含队列与 KV 预算状态"""
        models = get_running_models()
        result = []
        for name, info in models.items():
            gate = _inference_gates.get(name)
            entry = {
                "name": name,
                "model": info.get("model", name),
                "port": info["port"],
                "pid": info["pid"],
                "queue": gate.queue_depth if gate else 0,
            }
            if gate:
                entry["budget"] = gate.budget_snapshot()
            result.append(entry)
        g_snap = get_global_gate().snapshot()
        payload = {
            "models": result,
            "global": g_snap,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
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
    print(f"全局并发上限: {MAX_GLOBAL_CONCURRENT}，KV 粗算系数: {KV_CHARS_PER_TOKEN} chars/tok")
    if models:
        for name in models:
            g = get_inference_gate(name)
            print(f"  {name}: max_slots={g.max_slots}, budget={g.total_budget} tok")
    if api_key:
        print("认证: 已从 .api-key 加载")
    else:
        print("认证: 未启用（无 .api-key）")
    if ACCESS_LOG_FILE:
        print(f"Access log: {ACCESS_LOG_FILE}")
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
