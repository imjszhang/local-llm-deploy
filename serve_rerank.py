#!/usr/bin/env python3
"""
Local LLM Deploy — Jina Reranker (MLX) 服务
加载 jina-reranker-v3-mlx，提供 Jina 兼容的 /v1/rerank 接口。

用法:
  python3 serve_rerank.py [--port 8006] [--model-dir PATH] [--model-name NAME]

接口:
  POST /v1/rerank  → 文档重排序（Jina 兼容格式）
  GET  /health     → 健康检查
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
API_KEY_FILE = os.path.join(SCRIPT_DIR, ".api-key")
RUN_DIR = os.path.join(SCRIPT_DIR, "run")
MODELS_JSON = os.path.join(SCRIPT_DIR, "models.json")


def _log(msg: str) -> None:
    sys.stderr.write(f"[rerank] {msg}\n")
    sys.stderr.flush()


def load_api_key() -> str | None:
    if os.path.isfile(API_KEY_FILE):
        with open(API_KEY_FILE) as f:
            return f.readline().strip().strip("\r\n") or None
    return None


class RerankModel:
    """Wraps jina-reranker-v3-mlx MLXReranker."""

    def __init__(self, model_dir: str):
        _log(f"加载模型: {model_dir}")
        t0 = time.monotonic()

        if model_dir not in sys.path:
            sys.path.insert(0, model_dir)

        projector_path = os.path.join(model_dir, "projector.safetensors")
        if not os.path.isfile(projector_path):
            raise FileNotFoundError(f"缺少 projector.safetensors: {projector_path}")

        from rerank import MLXReranker

        self.reranker = MLXReranker(
            model_path=model_dir,
            projector_path=projector_path,
        )
        _log(f"模型就绪 ({time.monotonic() - t0:.1f}s)")

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int | None = None,
        return_embeddings: bool = False,
    ) -> list[dict]:
        return self.reranker.rerank(
            query=query,
            documents=documents,
            top_n=top_n,
            return_embeddings=return_embeddings,
        )


class RerankHandler(BaseHTTPRequestHandler):
    model: RerankModel | None = None
    model_name: str = "jina-reranker-v3"
    max_documents: int = 64

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json_response(200, {"status": "ok"})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        path = self.path.rstrip("/").split("?")[0]
        if path == "/v1/rerank":
            self.handle_rerank()
        else:
            self.send_error(404)

    def handle_rerank(self) -> None:
        if not self._check_auth():
            return

        try:
            body = self._read_body()
            if body is None:
                return
        except Exception as e:
            self._error_response(400, str(e))
            return

        query = body.get("query")
        documents = body.get("documents")
        if not query or not isinstance(query, str):
            self._error_response(400, "Missing or invalid 'query' field")
            return
        if not documents or not isinstance(documents, list):
            self._error_response(400, "Missing or invalid 'documents' field")
            return

        docs = [str(d) for d in documents if d is not None]
        if not docs:
            self._error_response(400, "'documents' must not be empty")
            return
        if len(docs) > self.__class__.max_documents:
            self._error_response(
                400,
                f"Too many documents (max {self.__class__.max_documents})",
            )
            return

        top_n = body.get("top_n")
        if top_n is not None:
            top_n = int(top_n)
            if top_n <= 0:
                self._error_response(400, "'top_n' must be positive")
                return

        return_documents = bool(body.get("return_documents", False))
        return_embeddings = bool(body.get("return_embeddings", False))

        try:
            results = self.__class__.model.rerank(
                query=query,
                documents=docs,
                top_n=top_n,
                return_embeddings=return_embeddings,
            )

            payload_results = []
            for item in results:
                entry = {
                    "index": item["index"],
                    "relevance_score": item["relevance_score"],
                }
                if return_documents:
                    entry["document"] = item["document"]
                if return_embeddings and item.get("embedding") is not None:
                    emb = item["embedding"]
                    entry["embedding"] = (
                        emb.tolist() if hasattr(emb, "tolist") else list(emb)
                    )
                payload_results.append(entry)

            self._json_response(
                200,
                {
                    "model": self.__class__.model_name,
                    "results": payload_results,
                    "usage": {"total_tokens": 0},
                },
            )
        except ValueError as e:
            self._error_response(400, str(e))
        except Exception as e:
            _log(f"推理错误: {e}")
            self._error_response(500, f"Inference error: {e}")

    def _check_auth(self) -> bool:
        expected = load_api_key()
        if not expected:
            return True
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {expected}":
            return True
        self._error_response(
            401, "Invalid API key", error_type="invalid_request_error"
        )
        return False

    def _read_body(self) -> dict | None:
        content_len = int(self.headers.get("Content-Length", 0))
        if content_len == 0:
            self._error_response(400, "Empty request body")
            return None
        raw = self.rfile.read(content_len)
        return json.loads(raw)

    def _json_response(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error_response(
        self, code: int, message: str, error_type: str = "server_error"
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
    repo_name = cfg.get("repo_name") or cfg["repo_id"].replace("/", "-")
    return os.path.join(SCRIPT_DIR, "models", repo_name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Jina Reranker (MLX) 服务")
    parser.add_argument("--port", type=int, default=8006)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--model-dir", default=None)
    parser.add_argument("--model-name", default="jina-rerank-mlx")
    args = parser.parse_args()

    cfg = get_model_config(args.model_name) or {}
    alias = cfg.get("alias") or "jina-reranker-v3"
    max_documents = int((cfg.get("params") or {}).get("max_documents", 64))

    model_dir = args.model_dir or get_model_dir_from_json(args.model_name)
    if not model_dir:
        model_dir = os.path.join(
            SCRIPT_DIR, "models", "jinaai-jina-reranker-v3-mlx"
        )
    if not os.path.isdir(model_dir):
        print(f"错误: 模型目录不存在: {model_dir}", file=sys.stderr)
        print("请先下载: ./manage.sh download jina-rerank-mlx", file=sys.stderr)
        sys.exit(1)

    model = RerankModel(model_dir)
    RerankHandler.model = model
    RerankHandler.model_name = alias
    RerankHandler.max_documents = max_documents

    pid_file = write_pid_file(args.model_name, args.port, alias)
    _log(f"PID 文件: {pid_file}")

    def cleanup(signum=None, frame=None) -> None:
        _log("正在停止...")
        remove_pid_file(args.model_name)
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    server = ThreadingHTTPServer((args.host, args.port), RerankHandler)
    print(f"  Rerank 服务: {alias}")
    print(f"  监听:      http://{args.host}:{args.port}")
    print(f"  接口:      http://{args.host}:{args.port}/v1/rerank")
    print(f"  健康检查:  http://{args.host}:{args.port}/health")
    sys.stdout.flush()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        cleanup()


if __name__ == "__main__":
    main()
