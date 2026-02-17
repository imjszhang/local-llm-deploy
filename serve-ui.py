#!/usr/bin/env python3
"""
Local LLM Deploy — 前端静态服务 + 多模型 API 代理
自动读取 .api-key，支持多个 llama-server 后端实例的路由代理

路由规则:
  /api/models            → 返回运行中的模型列表（从 run/*.pid 读取）
  /api/<model-name>/*    → 代理到该模型对应的后端端口
  /api/*                 → 代理到默认（第一个运行中的）后端
"""
import json
import os
import socket
import sys
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


def load_api_key():
    if os.path.isfile(API_KEY_FILE):
        with open(API_KEY_FILE, "r") as f:
            return f.readline().strip().strip("\r\n") or None
    return None


def get_running_models():
    """从 run/*.pid 读取运行中的模型列表，返回 {name: {pid, port}}"""
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
            # 检查进程是否还在运行
            try:
                os.kill(pid, 0)
            except OSError:
                os.remove(fpath)
                continue
            name = fname[:-4]  # strip .pid
            models[name] = {"pid": pid, "port": port}
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


class ProxyHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_GET(self):
        if self.path.startswith("/api/"):
            self.proxy_request("GET")
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/"):
            self.proxy_request("POST")
        else:
            super().do_POST()

    def resolve_backend(self, api_path):
        """解析 API 路径，返回 (backend_url, remaining_path)"""
        # /api/models → 特殊端点，返回模型列表
        if api_path.lstrip("/") == "models":
            return None, "models"

        # /api/<model-name>/... → 路由到指定模型
        models = get_running_models()
        parts = api_path.lstrip("/").split("/", 1)
        if len(parts) >= 1 and parts[0] in models:
            model_name = parts[0]
            port = models[model_name]["port"]
            remaining = "/" + parts[1] if len(parts) > 1 else "/"
            return f"http://127.0.0.1:{port}", remaining

        # /api/... → 默认后端
        port = get_default_port()
        return f"http://127.0.0.1:{port}", api_path

    def proxy_request(self, method):
        api_path = self.path[4:]  # strip /api

        backend_url, remaining_path = self.resolve_backend(api_path)

        # 特殊端点: /api/models
        if backend_url is None and remaining_path == "models":
            self.handle_models_endpoint()
            return

        url = backend_url.rstrip("/") + remaining_path

        monitor_paths = ("health", "metrics", "slots")
        clean_path = remaining_path.lstrip("/").split("?")[0]
        timeout = MONITOR_PROXY_TIMEOUT if clean_path in monitor_paths else API_PROXY_TIMEOUT

        headers = {}
        for k, v in self.headers.items():
            if k.lower() not in ("host", "connection"):
                headers[k] = v

        api_key = load_api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            if method == "GET":
                req = urllib.request.Request(url, headers=headers, method="GET")
            else:
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len) if content_len else None
                req = urllib.request.Request(url, data=body, headers=headers, method="POST")
                if body:
                    req.add_header("Content-Type", self.headers.get("Content-Type", "application/json"))

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
                    self.wfile.write(("%x\r\n" % len(chunk)).encode())
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(e.read())
        except urllib.error.URLError as e:
            if isinstance(getattr(e, "reason", None), socket.timeout) or "timed out" in str(e).lower():
                self.send_response(504)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write('{"error":"推理中，监控接口被阻塞，请稍后刷新"}'.encode("utf-8"))
            else:
                self.send_error(502, str(e))
        except Exception as e:
            self.send_error(502, str(e))

    def handle_models_endpoint(self):
        """返回运行中的模型列表"""
        models = get_running_models()
        result = []
        for name, info in models.items():
            result.append({"name": name, "port": info["port"], "pid": info["pid"]})
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        if not self.path.startswith("/api/"):
            super().log_message(format, *args)


def main():
    port = int(os.environ.get("UI_PORT", "8888"))
    api_key = load_api_key()
    models = get_running_models()

    print(f"前端服务: http://localhost:{port}/")
    print(f"  监控: http://localhost:{port}/monitor.html")
    print()
    print("API 代理路由:")
    print(f"  /api/models → 模型列表")
    if models:
        for name, info in models.items():
            print(f"  /api/{name}/* → http://127.0.0.1:{info['port']}")
        first_name = next(iter(models))
        print(f"  /api/* → http://127.0.0.1:{models[first_name]['port']} (默认: {first_name})")
    else:
        default_port = int(os.environ.get("LLAMA_PORT", "8001"))
        print(f"  /api/* → http://127.0.0.1:{default_port} (无运行中模型，使用默认端口)")
    print()
    if api_key:
        print("  认证: 已从 .api-key 加载")
    else:
        print("  认证: 未启用（无 .api-key）")
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
