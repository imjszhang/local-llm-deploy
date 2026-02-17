#!/usr/bin/env python3
"""
前端静态服务 + API 代理
自动读取 .api-key 并代理请求到 llama-server，无需手动输入密钥
"""
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
LLAMA_BASE = os.environ.get("LLAMA_API_BASE", "http://127.0.0.1:8001")
# 大 prompt + thinking 场景：7k prompt~281s，生成 8.31 tok/s，20k prompt+10k 生成约 35min
API_PROXY_TIMEOUT = int(os.environ.get("API_PROXY_TIMEOUT", "3600"))
# 监控接口超时：llama-server 推理时 /metrics /slots 会阻塞，短超时快速释放线程避免服务卡死
MONITOR_PROXY_TIMEOUT = int(os.environ.get("MONITOR_PROXY_TIMEOUT", "8"))


def load_api_key():
    if os.path.isfile(API_KEY_FILE):
        with open(API_KEY_FILE, "r") as f:
            return f.readline().strip().strip("\r\n") or None
    return None


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

    def proxy_request(self, method):
        path = self.path[4:]  # strip /api
        url = LLAMA_BASE.rstrip("/") + path
        # 监控接口推理时易阻塞，使用短超时
        monitor_paths = ("health", "metrics", "slots")
        timeout = MONITOR_PROXY_TIMEOUT if path.lstrip("/").split("?")[0] in monitor_paths else API_PROXY_TIMEOUT
        if self.raw_requestline and b"?" in self.raw_requestline:
            # preserve query string
            pass
        else:
            # parse query from path
            if "?" in path:
                url = LLAMA_BASE.rstrip("/") + path

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

    def log_message(self, format, *args):
        if not self.path.startswith("/api/"):
            super().log_message(format, *args)


def main():
    port = int(os.environ.get("UI_PORT", "8888"))
    api_key = load_api_key()
    print(f"前端服务: http://localhost:{port}/")
    print(f"  监控: http://localhost:{port}/monitor.html")
    print(f"API 代理: /api/* -> {LLAMA_BASE}")
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
