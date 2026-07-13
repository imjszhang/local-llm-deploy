#!/usr/bin/env python3
"""
模型推理速度 benchmark — 对比 Ollama 与 llama-server（及自定义后端）

用法:
  ./scripts/bench-models.py --list
  ./scripts/bench-models.py --ollama qwen3.6:27b-mlx --llama qwen36-27b-aggressive
  ./scripts/bench-models.py --auto
  ./scripts/bench-models.py --backend "ds4|openai|http://127.0.0.1:8005|deepseek-v4-flash"
  ./scripts/bench-models.py --ds4 --ollama qwen3.6:27b-mlx
  ./scripts/bench-models.py --ollama qwen3.6:27b-mlx --rounds 5 --json
  ./scripts/bench-models.py --thinking --llama ds4flash
  ./scripts/bench-models.py --thinking --ds4 --rounds 2

环境变量:
  OLLAMA_HOST          Ollama 地址（默认 http://localhost:11434）
  OPENAI_API_KEY/API_KEY  llama-server Bearer token（默认读项目根 .api-key）
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
RUN_DIR = os.path.join(PROJECT_ROOT, "run")
MODELS_JSON = os.path.join(PROJECT_ROOT, "models.json")
API_KEY_FILE = os.path.join(PROJECT_ROOT, ".api-key")
DEFAULT_OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")

BENCHMARK_CASES = [
    ("short_128", "短 prompt / 128 tok", "Explain what machine learning is in simple terms.", 128),
    (
        "medium_256",
        "中等 prompt / 256 tok",
        "Write a detailed explanation of how transformers work in NLP. " * 5,
        256,
    ),
    (
        "long_prefill_64",
        "长 prompt / 64 tok",
        "Summarize the following text in 3 bullet points:\n"
        + ("The history of computing spans decades of innovation. " * 80),
        64,
    ),
]

THINKING_BENCHMARK_CASES = [
    ("short", "短问题", "Explain what machine learning is in simple terms.", 512),
    ("reasoning", "推理题", "9.11 and 9.8, which is greater? Explain step by step.", 512),
]


@dataclass
class Backend:
    label: str
    kind: str  # "ollama" | "llama" | "openai"
    host: str
    model: str
    auth: str | None = None
    note: str = ""

    @property
    def base_url(self) -> str:
        return self.host.rstrip("/")


@dataclass
class RunResult:
    prefill_tps: float = 0.0
    decode_tps: float = 0.0
    prompt_tok: int = 0
    gen_tok: int = 0
    wall_s: float = 0.0
    ttft_ms: float = 0.0
    load_s: float = 0.0
    early_stop: bool = False


@dataclass
class CaseSummary:
    case_id: str
    case_name: str
    num_predict: int
    runs: list[RunResult] = field(default_factory=list)
    stream: RunResult | None = None

    @property
    def decode_last(self) -> float:
        return self.runs[-1].decode_tps if self.runs else 0.0

    @property
    def decode_avg(self) -> float:
        return statistics.mean(r.decode_tps for r in self.runs) if self.runs else 0.0

    @property
    def ttft_ms(self) -> float:
        if self.stream and self.stream.ttft_ms:
            return self.stream.ttft_ms
        return self.runs[-1].ttft_ms if self.runs else 0.0

    @property
    def wall_last(self) -> float:
        return self.runs[-1].wall_s if self.runs else 0.0


@dataclass
class ThinkingRunResult:
    prefill_tps: float = 0.0
    prompt_tok: int = 0
    reasoning_tok: int = 0
    content_tok: int = 0
    total_tok: int = 0
    reasoning_tps: float = 0.0
    content_tps: float = 0.0
    total_tps: float = 0.0
    ttft_reasoning_ms: float = 0.0
    ttft_content_ms: float = 0.0
    wall_s: float = 0.0
    reasoning_pct: float = 0.0
    no_content: bool = False


@dataclass
class ThinkingCaseSummary:
    case_id: str
    case_name: str
    max_tokens: int
    runs: list[ThinkingRunResult] = field(default_factory=list)

    @property
    def reasoning_tps_avg(self) -> float:
        return statistics.mean(r.reasoning_tps for r in self.runs) if self.runs else 0.0

    @property
    def content_tps_avg(self) -> float:
        return statistics.mean(r.content_tps for r in self.runs) if self.runs else 0.0

    @property
    def total_tps_avg(self) -> float:
        return statistics.mean(r.total_tps for r in self.runs) if self.runs else 0.0


def load_api_key() -> str | None:
    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("API_KEY")
    if key:
        return key.strip()
    if os.path.isfile(API_KEY_FILE):
        with open(API_KEY_FILE) as f:
            return f.readline().strip().strip("\r\n") or None
    return None


def load_models_json() -> dict[str, Any]:
    try:
        with open(MODELS_JSON) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def discover_llama_backends() -> list[Backend]:
    auth = load_api_key()
    found: list[Backend] = []
    seen: set[str] = set()
    mj = load_models_json()

    if os.path.isdir(RUN_DIR):
        for fname in sorted(os.listdir(RUN_DIR)):
            if not fname.endswith(".pid"):
                continue
            alias = fname[:-4]
            meta = mj.get(alias, {})
            if meta.get("type") == "embedding":
                continue
            try:
                with open(os.path.join(RUN_DIR, fname)) as f:
                    lines = f.read().strip().split("\n")
                pid = int(lines[0].strip())
                port = int(lines[1].strip()) if len(lines) > 1 else meta.get("default_port", 8001)
                model = lines[2].strip() if len(lines) > 2 else alias
            except (ValueError, IndexError, OSError):
                continue
            if not _pid_alive(pid):
                continue
            key = f"{alias}:{port}"
            if key in seen:
                continue
            seen.add(key)
            quant = meta.get("default_quant", "")
            note = meta.get("full_model_name") or alias
            if quant:
                note = f"{note} ({quant})"
            found.append(
                Backend(
                    label=alias,
                    kind="llama",
                    host=f"http://127.0.0.1:{port}",
                    model=model or alias,
                    auth=auth,
                    note=note,
                )
            )
    return found


def discover_ollama_backends() -> list[Backend]:
    found: list[Backend] = []
    try:
        req = urllib.request.Request(DEFAULT_OLLAMA_HOST + "/api/ps")
        with urllib.request.urlopen(req, timeout=3) as resp:
            ps = json.loads(resp.read())
        models = [m.get("name", "") for m in ps.get("models", []) if m.get("name")]
        if not models:
            req = urllib.request.Request(DEFAULT_OLLAMA_HOST + "/api/tags")
            with urllib.request.urlopen(req, timeout=3) as resp:
                tags = json.loads(resp.read())
            models = [m.get("name", "") for m in tags.get("models", []) if m.get("name")]
    except Exception:
        return found

    for name in models:
        found.append(
            Backend(
                label=name,
                kind="ollama",
                host=DEFAULT_OLLAMA_HOST,
                model=name,
                note="Ollama",
            )
        )
    return found


def resolve_llama_alias(alias: str) -> Backend | None:
    auth = load_api_key()
    mj = load_models_json()
    meta = mj.get(alias, {})
    pid_file = os.path.join(RUN_DIR, f"{alias}.pid")
    port = meta.get("default_port")
    model = alias
    if os.path.isfile(pid_file):
        try:
            with open(pid_file) as f:
                lines = f.read().strip().split("\n")
            pid = int(lines[0].strip())
            if not _pid_alive(pid):
                return None
            port = int(lines[1].strip()) if len(lines) > 1 else port
            model = lines[2].strip() if len(lines) > 2 else alias
        except (ValueError, IndexError, OSError):
            return None
    elif port and _port_open("127.0.0.1", int(port)):
        pass
    else:
        return None

    quant = meta.get("default_quant", "")
    note = meta.get("full_model_name") or alias
    if quant:
        note = f"{note} ({quant})"
    return Backend(
        label=alias,
        kind="llama",
        host=f"http://127.0.0.1:{int(port)}",
        model=model or alias,
        auth=auth,
        note=note,
    )


def _fetch_openai_model_id(host: str, auth: str | None) -> str | None:
    try:
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {auth}"
        req = urllib.request.Request(host.rstrip("/") + "/v1/models", headers=headers)
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
        models = data.get("data") or data.get("models") or []
        if models:
            return models[0].get("id") or models[0].get("name")
    except Exception:
        return None
    return None


def resolve_external_alias(alias: str) -> Backend | None:
    auth = load_api_key()
    mj = load_models_json()
    meta = mj.get(alias)
    if not meta or meta.get("type") != "external":
        return None
    port = meta.get("default_port")
    if not port or not _port_open("127.0.0.1", int(port)):
        return None
    host = f"http://127.0.0.1:{int(port)}"
    model = meta.get("alias") or alias
    api_model = _fetch_openai_model_id(host, auth) or model
    note = meta.get("full_model_name") or alias
    return Backend(
        label=alias,
        kind="openai",
        host=host,
        model=api_model,
        auth=auth,
        note=note,
    )


def resolve_ds4() -> Backend | None:
    b = resolve_external_alias("ds4flash")
    if b is not None:
        return b
    llama = resolve_llama_alias("ds4flash")
    if llama is None:
        return None
    api_model = _fetch_openai_model_id(llama.host, llama.auth) or llama.model
    return Backend(
        label=llama.label,
        kind="openai",
        host=llama.host,
        model=api_model,
        auth=llama.auth,
        note=llama.note,
    )


def parse_backend_spec(spec: str) -> Backend:
    """格式: label|kind|host|model  例如 ds4|openai|http://127.0.0.1:8005|deepseek-v4-flash"""
    parts = spec.split("|", 3)
    if len(parts) != 4:
        raise ValueError(f"无效 --backend 格式: {spec!r}，应为 label|kind|host|model")
    label, kind, host, model = parts
    kind = kind.lower()
    if kind not in ("ollama", "llama", "openai"):
        raise ValueError(f"backend kind 必须是 ollama、llama 或 openai，收到: {kind!r}")
    auth = load_api_key() if kind in ("llama", "openai") else None
    return Backend(label=label, kind=kind, host=host, model=model, auth=auth)


def http_post_json(
    url: str,
    payload: dict[str, Any],
    auth: str | None = None,
    stream: bool = False,
    timeout: float = 600,
) -> tuple[dict[str, Any], float]:
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = f"Bearer {auth}"
    req = urllib.request.Request(url, data=data, headers=headers)
    t0 = time.perf_counter()
    resp = urllib.request.urlopen(req, timeout=timeout)
    if not stream:
        body = json.loads(resp.read())
        return body, time.perf_counter() - t0

    ttft = None
    last: dict[str, Any] = {}
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            break
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue
        last = chunk
        token_text = chunk.get("response") or chunk.get("content")
        if not token_text and chunk.get("choices"):
            choice = chunk["choices"][0]
            token_text = choice.get("text") or (choice.get("delta") or {}).get("content")
        if ttft is None and token_text:
            ttft = time.perf_counter() - t0
    last["_ttft"] = ttft
    last["_wall"] = time.perf_counter() - t0
    return last, last["_wall"]


def bench_ollama(backend: Backend, prompt: str, num_predict: int, stream: bool = False) -> RunResult:
    payload = {
        "model": backend.model,
        "prompt": prompt,
        "stream": stream,
        "think": False,
        "options": {"num_predict": num_predict, "temperature": 0},
    }
    r, wall = http_post_json(backend.base_url + "/api/generate", payload, stream=stream)
    pe = r.get("prompt_eval_count", 0)
    pd = (r.get("prompt_eval_duration") or 0) / 1e9
    ec = r.get("eval_count", 0)
    ed = (r.get("eval_duration") or 0) / 1e9
    ttft = (r.get("_ttft") or 0) * 1000 if stream else (pd * 1000 if pd else 0)
    return RunResult(
        prefill_tps=pe / pd if pd else 0,
        decode_tps=ec / ed if ed else 0,
        prompt_tok=pe,
        gen_tok=ec,
        wall_s=r.get("_wall", wall),
        ttft_ms=ttft,
        load_s=(r.get("load_duration") or 0) / 1e9,
        early_stop=ec < num_predict * 0.8,
    )


def bench_llama(backend: Backend, prompt: str, num_predict: int, stream: bool = False) -> RunResult:
    payload = {
        "prompt": prompt,
        "n_predict": num_predict,
        "temperature": 0,
        "stream": stream,
    }
    r, wall = http_post_json(
        backend.base_url + "/completion",
        payload,
        auth=backend.auth,
        stream=stream,
    )
    if r.get("error"):
        raise RuntimeError(r["error"].get("message") or r["error"])
    t = r.get("timings") or {}
    ec = t.get("predicted_n", 0)
    ed = (t.get("predicted_ms") or 0) / 1000
    pe = t.get("prompt_n", 0)
    pd = (t.get("prompt_ms") or 0) / 1000
    ttft = (r.get("_ttft") or 0) * 1000 if stream else (t.get("prompt_ms") or 0)
    decode_tps = t.get("predicted_per_second", 0) or (ec / ed if ed else 0)
    return RunResult(
        prefill_tps=t.get("prompt_per_second", 0) or (pe / pd if pd else 0),
        decode_tps=decode_tps,
        prompt_tok=pe,
        gen_tok=ec,
        wall_s=r.get("_wall", wall),
        ttft_ms=ttft,
        early_stop=ec < num_predict * 0.8,
    )


def _openai_stream_chunks(
    backend: Backend, prompt: str, num_predict: int
) -> tuple[float | None, float, int, dict[str, Any]]:
    payload = {
        "model": backend.model,
        "prompt": prompt,
        "max_tokens": num_predict,
        "temperature": 0,
        "stream": True,
    }
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if backend.auth:
        headers["Authorization"] = f"Bearer {backend.auth}"
    req = urllib.request.Request(backend.base_url + "/v1/completions", data=data, headers=headers)
    t0 = time.perf_counter()
    ttft = None
    chunks = 0
    last: dict[str, Any] = {}
    resp = urllib.request.urlopen(req, timeout=600)
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            break
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue
        last = chunk
        text = ""
        if chunk.get("choices"):
            text = chunk["choices"][0].get("text") or ""
        if text:
            if ttft is None:
                ttft = time.perf_counter() - t0
            chunks += 1
    wall = time.perf_counter() - t0
    return ttft, wall, chunks, last


def bench_openai(backend: Backend, prompt: str, num_predict: int, stream: bool = False) -> RunResult:
    del stream  # openai 统一用流式采样 TTFT 与 decode
    ttft, wall, chunks, _ = _openai_stream_chunks(backend, prompt, num_predict)
    decode_window = max(wall - (ttft or 0), 0.001)
    ttft_ms = (ttft or 0) * 1000
    return RunResult(
        prefill_tps=0.0,
        decode_tps=chunks / decode_window,
        prompt_tok=0,
        gen_tok=chunks,
        wall_s=wall,
        ttft_ms=ttft_ms,
        early_stop=chunks < num_predict * 0.8,
    )


def bench_thinking(
    backend: Backend,
    prompt: str,
    max_tokens: int,
    reasoning_effort: str = "high",
) -> ThinkingRunResult:
    payload = {
        "model": backend.model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": True,
        "temperature": 0,
        "reasoning_effort": reasoning_effort,
        "thinking": {"type": "enabled"},
    }
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if backend.auth:
        headers["Authorization"] = f"Bearer {backend.auth}"
    req = urllib.request.Request(backend.base_url + "/v1/chat/completions", data=data, headers=headers)
    t0 = time.perf_counter()
    ttft_reasoning = None
    ttft_content = None
    reasoning_chunks = 0
    content_chunks = 0
    timings: dict[str, Any] = {}

    resp = urllib.request.urlopen(req, timeout=600)
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            break
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue
        if chunk.get("timings"):
            timings = chunk["timings"]
        delta = chunk["choices"][0].get("delta", {})
        elapsed = time.perf_counter() - t0
        if delta.get("reasoning_content"):
            reasoning_chunks += 1
            if ttft_reasoning is None:
                ttft_reasoning = elapsed
        if delta.get("content"):
            content_chunks += 1
            if ttft_content is None:
                ttft_content = elapsed

    wall = time.perf_counter() - t0
    total_tok = timings.get("predicted_n", reasoning_chunks + content_chunks)
    if ttft_reasoning is not None and ttft_content is not None and ttft_content > ttft_reasoning:
        reasoning_dur = ttft_content - ttft_reasoning
        content_dur = wall - ttft_content
    elif ttft_reasoning is not None:
        reasoning_dur = wall - ttft_reasoning
        content_dur = 0.0
    else:
        reasoning_dur = 0.0
        content_dur = wall - (ttft_content or 0.0)

    reasoning_dur = max(reasoning_dur, 0.001)
    content_dur = max(content_dur, 0.001)

    reasoning_tok = reasoning_chunks
    content_tok = content_chunks
    if reasoning_chunks + content_chunks != total_tok and total_tok:
        ratio = total_tok / max(reasoning_chunks + content_chunks, 1)
        reasoning_tok = int(reasoning_chunks * ratio)
        content_tok = total_tok - reasoning_tok

    gen_start = ttft_reasoning or ttft_content or 0.0
    return ThinkingRunResult(
        prefill_tps=timings.get("prompt_per_second", 0),
        prompt_tok=timings.get("prompt_n", 0),
        reasoning_tok=reasoning_tok,
        content_tok=content_tok,
        total_tok=total_tok,
        reasoning_tps=reasoning_tok / reasoning_dur if reasoning_tok else 0.0,
        content_tps=content_tok / content_dur if content_tok else 0.0,
        total_tps=timings.get("predicted_per_second", 0) or (total_tok / max(wall - gen_start, 0.001)),
        ttft_reasoning_ms=(ttft_reasoning or 0) * 1000,
        ttft_content_ms=(ttft_content or 0) * 1000,
        wall_s=wall,
        reasoning_pct=reasoning_tok / max(total_tok, 1) * 100,
        no_content=content_tok == 0,
    )


def bench_once(backend: Backend, prompt: str, num_predict: int, stream: bool = False) -> RunResult:
    if backend.kind == "ollama":
        return bench_ollama(backend, prompt, num_predict, stream=stream)
    if backend.kind == "llama":
        return bench_llama(backend, prompt, num_predict, stream=stream)
    if backend.kind == "openai":
        return bench_openai(backend, prompt, num_predict, stream=stream)
    raise ValueError(f"未知 backend kind: {backend.kind}")


def run_backend(backend: Backend, rounds: int, warmup: bool) -> list[CaseSummary]:
    if backend.kind in ("llama", "openai") and not backend.auth:
        print(f"  [warn] {backend.label}: 未找到 API key，请求可能返回 401", file=sys.stderr)

    if warmup:
        try:
            bench_once(backend, "Hello", 16)
        except Exception as exc:
            raise RuntimeError(f"{backend.label} warmup 失败: {exc}") from exc

    summaries: list[CaseSummary] = []
    for case_id, case_name, prompt, num_predict in BENCHMARK_CASES:
        summary = CaseSummary(case_id=case_id, case_name=case_name, num_predict=num_predict)
        for _ in range(rounds):
            summary.runs.append(bench_once(backend, prompt, num_predict))
        if backend.kind == "openai":
            summary.stream = summary.runs[-1] if summary.runs else None
        else:
            try:
                summary.stream = bench_once(backend, prompt, num_predict, stream=True)
            except Exception:
                summary.stream = None
        summaries.append(summary)
    return summaries


def run_thinking_backend(
    backend: Backend,
    rounds: int,
    warmup: bool,
    reasoning_effort: str,
) -> list[ThinkingCaseSummary]:
    if backend.kind not in ("llama", "openai"):
        raise RuntimeError(f"{backend.label}: thinking 模式需要 OpenAI chat 兼容后端（llama/openai）")
    if not backend.auth:
        print(f"  [warn] {backend.label}: 未找到 API key，请求可能返回 401", file=sys.stderr)

    if warmup:
        try:
            bench_thinking(backend, "Hello", 32, reasoning_effort=reasoning_effort)
        except Exception as exc:
            raise RuntimeError(f"{backend.label} warmup 失败: {exc}") from exc

    summaries: list[ThinkingCaseSummary] = []
    for case_id, case_name, prompt, max_tokens in THINKING_BENCHMARK_CASES:
        summary = ThinkingCaseSummary(case_id=case_id, case_name=case_name, max_tokens=max_tokens)
        for _ in range(rounds):
            summary.runs.append(bench_thinking(backend, prompt, max_tokens, reasoning_effort=reasoning_effort))
        summaries.append(summary)
    return summaries


def print_backend_results(backend: Backend, summaries: list[CaseSummary], rounds: int) -> None:
    print(f"\n{'#' * 72}")
    print(f"# {backend.label}")
    if backend.note:
        print(f"# {backend.note}")
    print(f"# {backend.kind} @ {backend.base_url}  model={backend.model}")
    print(f"{'#' * 72}")
    for s in summaries:
        print(f"\n  [{s.case_name}]  target={s.num_predict} tok, rounds={rounds}, ollama_think=false")
        for i, r in enumerate(s.runs, 1):
            stop = " (early stop)" if r.early_stop else ""
            print(
                f"    run{i}: prefill={r.prompt_tok}tok @ {r.prefill_tps:.1f} tok/s | "
                f"gen={r.gen_tok}tok @ {r.decode_tps:.1f} tok/s | "
                f"wall={r.wall_s:.2f}s | ttft≈{r.ttft_ms:.0f}ms{stop}"
            )
        if s.stream:
            r = s.stream
            print(
                f"    stream: TTFT={r.ttft_ms:.0f}ms decode={r.decode_tps:.1f} tok/s wall={r.wall_s:.2f}s"
            )


def print_thinking_results(
    backend: Backend,
    summaries: list[ThinkingCaseSummary],
    rounds: int,
    reasoning_effort: str,
) -> None:
    print(f"\n{'#' * 72}")
    print(f"# {backend.label}")
    if backend.note:
        print(f"# {backend.note}")
    print(f"# thinking @ {backend.base_url}/v1/chat/completions  model={backend.model}")
    print(f"# thinking=enabled, reasoning_effort={reasoning_effort}")
    print(f"{'#' * 72}")
    for s in summaries:
        print(f"\n  [{s.case_name}]  max_tokens={s.max_tokens}, rounds={rounds}")
        for i, r in enumerate(s.runs, 1):
            no_content = " (无 content，max_tokens 不足)" if r.no_content else ""
            print(
                f"    run{i}: prefill={r.prompt_tok}tok @ {r.prefill_tps:.1f} tok/s\n"
                f"         reasoning: {r.reasoning_tok}tok ({r.reasoning_pct:.0f}%) @ {r.reasoning_tps:.1f} tok/s  "
                f"TTFT={r.ttft_reasoning_ms:.0f}ms\n"
                f"         content  : {r.content_tok}tok ({100 - r.reasoning_pct:.0f}%) @ {r.content_tps:.1f} tok/s  "
                f"TTFT={r.ttft_content_ms:.0f}ms\n"
                f"         total    : {r.total_tok}tok @ {r.total_tps:.1f} tok/s  wall={r.wall_s:.1f}s{no_content}"
            )
        print(
            f"  avg: reasoning {s.reasoning_tps_avg:.1f} tok/s | "
            f"content {s.content_tps_avg:.1f} tok/s | total {s.total_tps_avg:.1f} tok/s"
        )

    all_runs = [r for s in summaries for r in s.runs]
    if all_runs:
        print(f"\n  汇总: reasoning {statistics.mean(r.reasoning_tps for r in all_runs):.1f} tok/s | "
              f"content {statistics.mean(r.content_tps for r in all_runs):.1f} tok/s | "
              f"total {statistics.mean(r.total_tps for r in all_runs):.1f} tok/s")
        print(f"        TTFT(reasoning) {statistics.mean(r.ttft_reasoning_ms for r in all_runs):.0f} ms | "
              f"TTFT(content) {statistics.mean(r.ttft_content_ms for r in all_runs):.0f} ms")


def print_comparison(backends: list[Backend], all_summaries: list[list[CaseSummary]]) -> None:
    if len(backends) < 2:
        return
    print(f"\n{'=' * 72}")
    print("对比汇总（各 backend 最后一轮 run 的 decode 速度；Ollama 已关闭 thinking）")
    print(f"{'=' * 72}")

    header = f"{'场景':<24}"
    for b in backends:
        header += f" {b.label[:16]:>16}"
    if len(backends) == 2:
        header += f" {'A/B':>8}"
    print(header)
    print("-" * 72)

    for idx, case in enumerate(BENCHMARK_CASES):
        _, case_name, _, _ = case
        row = f"{case_name:<24}"
        decodes = []
        for summaries in all_summaries:
            r = summaries[idx].runs[-1] if summaries[idx].runs else None
            if r and r.early_stop and r.gen_tok < 8:
                row += f" {'N/A':>14}"
                decodes.append(0.0)
            else:
                d = summaries[idx].decode_last
                decodes.append(d)
                row += f" {d:>14.1f} t/s"
        if len(backends) == 2 and decodes[0] > 0 and decodes[1] > 0:
            row += f" {decodes[0] / decodes[1]:>7.2f}x"
        elif len(backends) == 2:
            row += f" {'—':>8}"
        print(row)

    print()
    for idx, case in enumerate(BENCHMARK_CASES):
        _, case_name, _, _ = case
        row = f"{'TTFT ' + case_name[:18]:<24}"
        for summaries in all_summaries:
            row += f" {summaries[idx].ttft_ms:>14.0f} ms"
        print(row)


def build_report(backends: list[Backend], all_summaries: list[list[CaseSummary]], rounds: int) -> dict[str, Any]:
    report: dict[str, Any] = {
        "rounds": rounds,
        "cases": [{"id": c[0], "name": c[1], "num_predict": c[3]} for c in BENCHMARK_CASES],
        "backends": [],
    }
    for backend, summaries in zip(backends, all_summaries):
        entry = {
            "label": backend.label,
            "kind": backend.kind,
            "host": backend.base_url,
            "model": backend.model,
            "note": backend.note,
            "results": [],
        }
        for s in summaries:
            entry["results"].append(
                {
                    "case_id": s.case_id,
                    "case_name": s.case_name,
                    "num_predict": s.num_predict,
                    "decode_last_tps": round(s.decode_last, 2),
                    "decode_avg_tps": round(s.decode_avg, 2),
                    "ttft_ms": round(s.ttft_ms, 1),
                    "wall_last_s": round(s.wall_last, 2),
                    "runs": [asdict(r) for r in s.runs],
                    "stream": asdict(s.stream) if s.stream else None,
                }
            )
        report["backends"].append(entry)
    return report


def build_thinking_report(
    backends: list[Backend],
    all_summaries: list[list[ThinkingCaseSummary]],
    rounds: int,
    reasoning_effort: str,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "mode": "thinking",
        "rounds": rounds,
        "reasoning_effort": reasoning_effort,
        "cases": [{"id": c[0], "name": c[1], "max_tokens": c[3]} for c in THINKING_BENCHMARK_CASES],
        "backends": [],
    }
    for backend, summaries in zip(backends, all_summaries):
        entry = {
            "label": backend.label,
            "kind": backend.kind,
            "host": backend.base_url,
            "model": backend.model,
            "note": backend.note,
            "results": [],
        }
        for s in summaries:
            entry["results"].append(
                {
                    "case_id": s.case_id,
                    "case_name": s.case_name,
                    "max_tokens": s.max_tokens,
                    "reasoning_tps_avg": round(s.reasoning_tps_avg, 2),
                    "content_tps_avg": round(s.content_tps_avg, 2),
                    "total_tps_avg": round(s.total_tps_avg, 2),
                    "runs": [asdict(r) for r in s.runs],
                }
            )
        report["backends"].append(entry)
    return report


def cmd_list(_args: argparse.Namespace) -> int:
    print("运行中的 llama-server 后端 (run/*.pid):")
    llama = discover_llama_backends()
    if llama:
        for b in llama:
            print(f"  - {b.label:30} {b.base_url}  model={b.model}")
            if b.note:
                print(f"    {b.note}")
    else:
        print("  (无)")

    print(f"\nOllama 模型 ({DEFAULT_OLLAMA_HOST}):")
    ollama = discover_ollama_backends()
    if ollama:
        for b in ollama:
            print(f"  - {b.model}")
    else:
        print("  (无 — 服务未运行或未安装模型)")

    print("\n外部 OpenAI 兼容后端 (models.json type=external):")
    mj = load_models_json()
    found_external = False
    for alias, meta in mj.items():
        if meta.get("type") != "external":
            continue
        b = resolve_external_alias(alias)
        if b:
            found_external = True
            print(f"  - {b.label:30} {b.base_url}  model={b.model}")
            if b.note:
                print(f"    {b.note}")
    if not found_external:
        print("  (无 — 端口未监听)")

    print("\nmodels.json 中其他 alias:")
    for alias, meta in mj.items():
        if meta.get("type") in ("embedding", "external"):
            continue
        port = meta.get("default_port", "?")
        kind = meta.get("type") or "llama-server"
        print(f"  - {alias:30} port={port}  type={kind}")
    return 0


def collect_backends(args: argparse.Namespace) -> list[Backend]:
    backends: list[Backend] = []
    seen: set[str] = set()

    def add(backend: Backend | None) -> None:
        if backend is None:
            return
        key = f"{backend.kind}:{backend.base_url}:{backend.model}"
        if key in seen:
            return
        seen.add(key)
        backends.append(backend)

    if args.auto:
        for b in discover_ollama_backends():
            add(b)
        for b in discover_llama_backends():
            add(b)
        mj = load_models_json()
        for alias, meta in mj.items():
            if meta.get("type") == "external":
                add(resolve_external_alias(alias))
        if not backends:
            raise SystemExit("--auto: 未发现任何运行中的 backend")
        return backends

    if args.ds4:
        b = resolve_ds4()
        if b is None:
            raise SystemExit("ds4flash 未运行（端口 8005 不可达）")
        add(b)

    for model in args.ollama or []:
        add(
            Backend(
                label=model,
                kind="ollama",
                host=DEFAULT_OLLAMA_HOST,
                model=model,
                note="Ollama",
            )
        )

    for alias in args.llama or []:
        b = resolve_llama_alias(alias)
        if b is None:
            raise SystemExit(f"llama 后端未运行或未知 alias: {alias}")
        add(b)

    for spec in args.backend or []:
        add(parse_backend_spec(spec))

    if not backends:
        raise SystemExit("请指定 --ollama、--llama、--ds4、--backend 或 --auto")
    return backends


def collect_thinking_backends(args: argparse.Namespace) -> list[Backend]:
    backends = collect_backends(args)
    chat_backends: list[Backend] = []
    for backend in backends:
        if backend.kind == "ollama":
            raise SystemExit(f"thinking 模式不支持 Ollama 后端: {backend.label}")
        if backend.kind == "llama":
            api_model = _fetch_openai_model_id(backend.host, backend.auth) or backend.model
            chat_backends.append(
                Backend(
                    label=backend.label,
                    kind="openai",
                    host=backend.host,
                    model=api_model,
                    auth=backend.auth,
                    note=backend.note,
                )
            )
        else:
            chat_backends.append(backend)
    return chat_backends


def main() -> int:
    parser = argparse.ArgumentParser(description="对比 Ollama / llama-server / OpenAI 兼容后端推理速度")
    parser.add_argument("--list", action="store_true", help="列出可用 backend")
    parser.add_argument("--auto", action="store_true", help="自动发现所有运行中的 backend")
    parser.add_argument("--ollama", action="append", metavar="MODEL", help="Ollama 模型名，可重复")
    parser.add_argument("--llama", action="append", metavar="ALIAS", help="models.json alias 或 run/*.pid 名，可重复")
    parser.add_argument("--ds4", action="store_true", help="benchmark ds4flash（端口 8005，OpenAI /v1/completions）")
    parser.add_argument(
        "--backend",
        action="append",
        metavar="SPEC",
        help="自定义 backend: label|kind|host|model",
    )
    parser.add_argument("--rounds", type=int, default=3, help="每个场景重复次数（默认 3）")
    parser.add_argument("--no-warmup", action="store_true", help="跳过 warmup 请求")
    parser.add_argument("--json", action="store_true", help="输出 JSON 报告")
    parser.add_argument(
        "--thinking",
        action="store_true",
        help="DeepSeek thinking 模式 benchmark（/v1/chat/completions，分 reasoning/content 测速）",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("high", "max"),
        default="high",
        help="thinking 模式强度（默认 high）",
    )
    args = parser.parse_args()

    if args.list:
        return cmd_list(args)

    rounds = max(1, args.rounds)
    warmup = not args.no_warmup

    if args.thinking:
        backends = collect_thinking_backends(args)
        print("=" * 72)
        print("  Thinking 模式 Benchmark")
        print("=" * 72)
        print(f"  backends         : {', '.join(b.label for b in backends)}")
        print(f"  rounds           : {rounds}")
        print(f"  warmup           : {warmup}")
        print(f"  reasoning_effort : {args.reasoning_effort}")
        print(f"  thinking         : enabled (temperature=0)")
        print("=" * 72)

        all_summaries: list[list[ThinkingCaseSummary]] = []
        for backend in backends:
            try:
                summaries = run_thinking_backend(
                    backend,
                    rounds=rounds,
                    warmup=warmup,
                    reasoning_effort=args.reasoning_effort,
                )
            except Exception as exc:
                print(f"\n[ERROR] {backend.label}: {exc}", file=sys.stderr)
                return 1
            all_summaries.append(summaries)
            if not args.json:
                print_thinking_results(backend, summaries, rounds, args.reasoning_effort)

        if args.json:
            print(
                json.dumps(
                    build_thinking_report(backends, all_summaries, rounds, args.reasoning_effort),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0

    backends = collect_backends(args)
    print("=" * 72)
    print("  模型推理速度 Benchmark")
    print("=" * 72)
    print(f"  backends : {', '.join(b.label for b in backends)}")
    print(f"  rounds   : {rounds}")
    print(f"  warmup   : {warmup}")
    print(f"  ollama   : think=false, temperature=0")
    print("=" * 72)

    all_summaries: list[list[CaseSummary]] = []
    for backend in backends:
        try:
            summaries = run_backend(backend, rounds=rounds, warmup=warmup)
        except Exception as exc:
            print(f"\n[ERROR] {backend.label}: {exc}", file=sys.stderr)
            return 1
        all_summaries.append(summaries)
        if not args.json:
            print_backend_results(backend, summaries, rounds)

    if not args.json:
        print_comparison(backends, all_summaries)

    if args.json:
        print(json.dumps(build_report(backends, all_summaries, rounds), ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
