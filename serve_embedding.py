#!/usr/bin/env python3
"""
Local LLM Deploy — Embedding 模型服务
加载 jina-embeddings-v5-text-small，提供 OpenAI 兼容的 /v1/embeddings 接口。

用法:
  python3 serve_embedding.py [--port 8004] [--model-dir PATH] [--model-name NAME]

接口:
  POST /v1/embeddings  → 文本向量化（OpenAI 兼容格式）
  GET  /health         → 健康检查
"""
import argparse
import json
import os
import signal
import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
API_KEY_FILE = os.path.join(SCRIPT_DIR, ".api-key")
RUN_DIR = os.path.join(SCRIPT_DIR, "run")
MODELS_JSON = os.path.join(SCRIPT_DIR, "models.json")

TASK_ALIASES = {
    "retrieval.query": "retrieval",
    "retrieval.passage": "retrieval",
    "text-matching": "text-matching",
    "classification": "classification",
    "clustering": "clustering",
    "retrieval": "retrieval",
}

DEFAULT_TASK = "text-matching"
DEFAULT_DIMENSIONS = 1024
BATCH_SIZE = 32


def _log(msg):
    sys.stderr.write(f"[embedding] {msg}\n")
    sys.stderr.flush()


def load_api_key():
    if os.path.isfile(API_KEY_FILE):
        with open(API_KEY_FILE, "r") as f:
            return f.readline().strip().strip("\r\n") or None
    return None


class EmbeddingModel:
    """Wraps jina-embeddings-v5 with task-specific LoRA adapter switching."""

    def __init__(self, model_dir):
        _log(f"加载模型: {model_dir}")
        t0 = time.monotonic()

        import torch
        from transformers import AutoConfig, AutoModel, AutoTokenizer

        self.torch = torch
        self.config = AutoConfig.from_pretrained(model_dir, trust_remote_code=True)
        self.model = AutoModel.from_pretrained(
            model_dir, config=self.config, trust_remote_code=True
        )
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_dir, trust_remote_code=True
        )
        self.task_names = list(self.config.task_names)
        self.max_seq_length = self.config.max_position_embeddings
        self.hidden_size = self.config.hidden_size

        if self.torch.backends.mps.is_available():
            self.device = self.torch.device("mps")
            self.model.to(self.device)
            _log("使用 Apple Silicon MPS 加速")
        elif self.torch.cuda.is_available():
            self.device = self.torch.device("cuda")
            self.model.to(self.device)
            _log("使用 CUDA 加速")
        else:
            self.device = self.torch.device("cpu")
            _log("使用 CPU 推理")

        self.model.eval()
        self._lock = threading.Lock()
        elapsed = time.monotonic() - t0
        _log(f"模型加载完成 ({elapsed:.1f}s), tasks={self.task_names}")

    def encode(self, texts, task=DEFAULT_TASK, dimensions=None, prompt_name="document"):
        import torch
        import torch.nn.functional as F

        adapter_task = TASK_ALIASES.get(task, task)
        if adapter_task not in self.task_names:
            raise ValueError(
                f"Unknown task: {task}. Available: {self.task_names}"
            )

        prefix = "Query: " if prompt_name == "query" else "Document: "
        inputs = [f"{prefix}{t}" for t in texts]

        batch = self.tokenizer(
            inputs,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=self.max_seq_length,
        )
        total_tokens = int(batch["attention_mask"].sum().item())

        with self._lock:
            self.model.set_adapter(adapter_task)
            batch_dev = {k: v.to(self.device) for k, v in batch.items()}
            with torch.no_grad():
                outputs = self.model(**batch_dev)
                hidden = outputs.last_hidden_state
                mask = batch_dev.get("attention_mask")
                if mask is None:
                    pooled = hidden[:, -1]
                else:
                    seq_lens = mask.sum(dim=1) - 1
                    pooled = hidden[
                        torch.arange(hidden.shape[0], device=hidden.device),
                        seq_lens,
                    ]

                if dimensions is not None:
                    pooled = pooled[:, :dimensions]
                embeddings = F.normalize(pooled, p=2, dim=-1)

        return embeddings.cpu().float().numpy(), total_tokens


class EmbeddingHandler(BaseHTTPRequestHandler):
    model: EmbeddingModel = None
    model_name: str = "jina-embeddings-v5-text-small"

    def do_GET(self):
        if self.path == "/health":
            self._json_response(200, {"status": "ok"})
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.rstrip("/").split("?")[0]
        if path == "/v1/embeddings":
            self.handle_embeddings()
        else:
            self.send_error(404)

    def handle_embeddings(self):
        if not self._check_auth():
            return

        try:
            body = self._read_body()
            if body is None:
                return
        except Exception as e:
            self._error_response(400, str(e))
            return

        input_data = body.get("input")
        if input_data is None:
            self._error_response(400, "Missing 'input' field")
            return

        if isinstance(input_data, str):
            texts = [input_data]
        elif isinstance(input_data, list):
            texts = [str(t) for t in input_data]
        else:
            self._error_response(400, "'input' must be a string or array of strings")
            return

        if not texts:
            self._error_response(400, "'input' must not be empty")
            return

        task = body.get("task", DEFAULT_TASK)
        dimensions = body.get("dimensions")
        if dimensions is not None:
            dimensions = int(dimensions)

        prompt_name = "document"
        if task in ("retrieval.query",):
            prompt_name = "query"
        elif body.get("prompt_name") == "query":
            prompt_name = "query"

        try:
            all_embeddings = []
            total_tokens = 0

            for i in range(0, len(texts), BATCH_SIZE):
                batch_texts = texts[i : i + BATCH_SIZE]
                embs, tokens = self.__class__.model.encode(
                    batch_texts,
                    task=task,
                    dimensions=dimensions,
                    prompt_name=prompt_name,
                )
                all_embeddings.extend(embs.tolist())
                total_tokens += tokens

            data = [
                {"object": "embedding", "index": i, "embedding": emb}
                for i, emb in enumerate(all_embeddings)
            ]
            result = {
                "object": "list",
                "model": self.__class__.model_name,
                "data": data,
                "usage": {
                    "prompt_tokens": total_tokens,
                    "total_tokens": total_tokens,
                },
            }
            self._json_response(200, result)

        except ValueError as e:
            self._error_response(400, str(e))
        except Exception as e:
            _log(f"推理错误: {e}")
            self._error_response(500, f"Inference error: {e}")

    def _check_auth(self):
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

    def _read_body(self):
        content_len = int(self.headers.get("Content-Length", 0))
        if content_len == 0:
            self._error_response(400, "Empty request body")
            return None
        raw = self.rfile.read(content_len)
        return json.loads(raw)

    def _json_response(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error_response(self, code, message, error_type="server_error"):
        self._json_response(
            code, {"error": {"message": message, "type": error_type}}
        )

    def log_message(self, format, *args):
        pass


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def write_pid_file(model_name, port):
    os.makedirs(RUN_DIR, exist_ok=True)
    pid_file = os.path.join(RUN_DIR, f"{model_name}.pid")
    with open(pid_file, "w") as f:
        f.write(f"{os.getpid()}\n{port}\njina-embeddings-v5-text-small\n")
    return pid_file


def remove_pid_file(model_name):
    pid_file = os.path.join(RUN_DIR, f"{model_name}.pid")
    if os.path.exists(pid_file):
        os.remove(pid_file)


def get_model_dir_from_json(model_name):
    if not os.path.isfile(MODELS_JSON):
        return None
    with open(MODELS_JSON) as f:
        data = json.load(f)
    cfg = data.get(model_name)
    if not cfg:
        return None
    repo_name = cfg.get("repo_name") or cfg["repo_id"].replace("/", "-")
    return os.path.join(SCRIPT_DIR, "models", repo_name)


def main():
    parser = argparse.ArgumentParser(description="Jina Embedding 服务")
    parser.add_argument("--port", type=int, default=8004)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--model-dir", default=None)
    parser.add_argument("--model-name", default="jina-embed")
    args = parser.parse_args()

    model_dir = args.model_dir
    if not model_dir:
        model_dir = get_model_dir_from_json(args.model_name)
    if not model_dir:
        model_dir = os.path.join(
            SCRIPT_DIR, "models", "jinaai-jina-embeddings-v5-text-small"
        )
    if not os.path.isdir(model_dir):
        print(f"错误: 模型目录不存在: {model_dir}", file=sys.stderr)
        print("请先下载: python3 download_jina_embeddings.py", file=sys.stderr)
        sys.exit(1)

    model = EmbeddingModel(model_dir)
    EmbeddingHandler.model = model
    EmbeddingHandler.model_name = "jina-embeddings-v5-text-small"

    pid_file = write_pid_file(args.model_name, args.port)
    _log(f"PID 文件: {pid_file}")

    def cleanup(signum=None, frame=None):
        _log("正在停止...")
        remove_pid_file(args.model_name)
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    server = ThreadingHTTPServer((args.host, args.port), EmbeddingHandler)

    api_key = load_api_key()
    print(f"========================================")
    print(f"  Embedding 服务: jina-embeddings-v5-text-small")
    print(f"========================================")
    print(f"端口:    {args.port}")
    print(f"监听:    {args.host}")
    print(f"模型:    {model_dir}")
    print(f"Tasks:   {model.task_names}")
    print(f"维度:    {model.hidden_size}")
    print(f"认证:    {'已启用' if api_key else '未启用'}")
    print(f"========================================")
    print(f"接口:    http://{args.host}:{args.port}/v1/embeddings")
    print(f"健康检查: http://{args.host}:{args.port}/health")
    print(f"========================================")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        remove_pid_file(args.model_name)


if __name__ == "__main__":
    main()
