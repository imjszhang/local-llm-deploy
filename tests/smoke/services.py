"""Explicit live smoke runner. Importing/discovering this module sends no requests."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.request
import uuid


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run small, serial requests against explicitly selected live services")
    parser.add_argument("--proxy", type=int, default=8888)
    parser.add_argument("--jina", type=int, default=8004)
    parser.add_argument("--rerank", type=int, help="Include direct and proxy Rerank checks")
    parser.add_argument("--whisper", type=int, help="Include ASR; requires --audio")
    parser.add_argument("--audio", type=Path, help="Short local audio sample for Whisper")
    parser.add_argument("--chat-model", help="Include one regular and one streamed chat request via the gateway")
    parser.add_argument("--embedding-model", default="jina-embeddings-v5-text-small")
    parser.add_argument("--rerank-model", default="jina-reranker-v3")
    parser.add_argument("--whisper-model", default="whisper-large-v3")
    parser.add_argument("--no-auth", action="store_true")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args(argv)
    if args.whisper and not args.audio:
        parser.error("--whisper requires --audio pointing to a short local sample")
    if args.audio and not args.audio.is_file():
        parser.error("--audio does not exist")
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("API_KEY")
    key_path = args.project_root / ".api-key"
    if not key and key_path.is_file() and not args.no_auth:
        with key_path.open() as stream:
            key = stream.readline().strip()
    auth = {} if args.no_auth or not key else {"Authorization": f"Bearer {key}"}
    embedding_url = os.environ.get("BASE_JINA") or f"http://127.0.0.1:{args.jina}"
    proxy_url = os.environ.get("BASE_PROXY") or f"http://127.0.0.1:{args.proxy}"
    failures = []
    passed = 0

    def check(name, base, endpoint, body=None, *, headers=None, validate=None, raw=False):
        nonlocal passed
        hdrs = dict(auth)
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        try:
            req = urllib.request.Request(base + endpoint, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=180) as response:
                payload = response.read(4 * 1024 * 1024)
                result = payload if raw else json.loads(payload)
            if validate:
                validate(result)
            passed += 1
            print(f"[OK] {name}", flush=True)
        except Exception as exc:
            failures.append(name)
            print(f"[FAIL] {name}: {type(exc).__name__}", file=sys.stderr, flush=True)

    def embeddings(count, dimensions=None):
        def validate(result):
            assert result["object"] == "list"
            assert len(result["data"]) == count
            assert [entry["index"] for entry in result["data"]] == list(range(count))
            for entry in result["data"]:
                assert entry["embedding"] and all(isinstance(x, (int, float)) for x in entry["embedding"])
                if dimensions:
                    assert len(entry["embedding"]) == dimensions
            assert result["usage"]["total_tokens"] > 0
        return validate

    check("Embedding health", embedding_url, "/health", validate=lambda body: require(body["status"] == "ok"))
    for text, task, dimensions in (("测试文本", "text-matching", None),
                                   (["文本1", "文本2"], "retrieval.query", 256),
                                   (["文档段落"], "retrieval.passage", None)):
        body = {"model": args.embedding_model, "input": text, "task": task}
        if dimensions:
            body["dimensions"] = dimensions
        check(f"Embedding {task}", embedding_url, "/v1/embeddings", body,
              validate=embeddings(1 if isinstance(text, str) else len(text), dimensions))
    check("Gateway model discovery", proxy_url, "/api/models")
    check("Gateway embeddings", proxy_url, "/v1/embeddings",
          {"model": args.embedding_model, "input": ["代理测试"]}, validate=embeddings(1))

    if args.rerank:
        body = {"model": args.rerank_model, "query": "北京", "documents": ["北京是中国首都", "苹果是一种水果"],
                "top_n": 1, "return_documents": True}

        def validate_rerank(result):
            assert len(result["results"]) == 1
            item = result["results"][0]
            assert item["index"] in (0, 1) and isinstance(item["relevance_score"], (int, float))
            assert "document" in item

        check("Rerank direct", f"http://127.0.0.1:{args.rerank}", "/v1/rerank", body, validate=validate_rerank)
        check("Rerank gateway", proxy_url, "/v1/rerank", body, validate=validate_rerank)

    if args.whisper:
        audio = args.audio.read_bytes()
        for response_format in ("json", "text", "verbose_json"):
            boundary = uuid.uuid4().hex
            fields = {"model": args.whisper_model, "response_format": response_format}
            body = f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="sample.wav"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + audio + b"\r\n"
            for name, value in fields.items():
                body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            body += f"--{boundary}--\r\n".encode()
            headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}

            def validate_whisper(result):
                if response_format == "text":
                    assert isinstance(result.decode("utf-8"), str)
                else:
                    assert isinstance(result["text"], str)
                    if response_format == "verbose_json":
                        assert isinstance(result["segments"], list)
            for base, description in ((f"http://127.0.0.1:{args.whisper}", "direct"), (proxy_url, "gateway")):
                check(f"Whisper {description} {response_format}", base, "/v1/audio/transcriptions", body,
                      headers=headers, raw=response_format == "text", validate=validate_whisper)

    if args.chat_model:
        body = {"model": args.chat_model, "messages": [{"role": "user", "content": "Say hello."}], "max_tokens": 16}
        check("Chat JSON", proxy_url, "/v1/chat/completions", body,
              validate=lambda result: require(bool(result["choices"])))
        check("Chat SSE", proxy_url, "/v1/chat/completions", {**body, "stream": True}, raw=True,
              validate=lambda result: require(b"data:" in result and b"[DONE]" in result))
    print(f"Passed {passed}; failed {len(failures)}")
    return 1 if failures else 0


def require(condition):
    if not condition:
        raise AssertionError("Response contract mismatch")


if __name__ == "__main__":
    raise SystemExit(main())
