"""Local inference adapter and HTTP protocol, with optional dependencies loaded on demand."""
from __future__ import annotations

import sys
import threading
import time

from .common import (
    ServiceHTTPHandler, configure_service, make_handler, positive_int,
    run_service, report_startup_error,
)


def _log(message):
    sys.stderr.write("[embedding] " + str(message) + "\n")
    sys.stderr.flush()

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


class EmbeddingHandler(ServiceHTTPHandler):
    endpoint = "/v1/embeddings"
    model_name = "jina-embeddings-v5-text-small"
    default_task = DEFAULT_TASK
    default_dimensions = None

    def predict(self):
        body = self._read_body()
        value = body.get("input")
        if isinstance(value, str):
            texts = [value]
        elif isinstance(value, list) and all(isinstance(item, str) for item in value):
            texts = value
        else:
            raise ValueError("'input' must be a string or array of strings")
        if not texts:
            raise ValueError("'input' must not be empty")
        task = body.get("task", self.default_task)
        if not isinstance(task, str):
            raise ValueError("'task' must be a string")
        adapter_task = TASK_ALIASES.get(task, task)
        if adapter_task not in self.model.task_names:
            raise ValueError(f"Unknown task: {task}. Available: {self.model.task_names}")
        dimensions = body.get("dimensions", self.default_dimensions)
        if dimensions is not None:
            dimensions = positive_int(dimensions, "dimensions")
            if dimensions > self.model.hidden_size:
                raise ValueError(f"'dimensions' must not exceed {self.model.hidden_size}")
        prompt_name = body.get("prompt_name", "document")
        if prompt_name not in ("query", "document"):
            raise ValueError("'prompt_name' must be 'query' or 'document'")
        if task == "retrieval.query":
            prompt_name = "query"
        encoding = body.get("encoding_format", "float")
        if encoding != "float":
            raise ValueError("Only encoding_format='float' is supported")

        all_embeddings = []
        total_tokens = 0
        for index in range(0, len(texts), BATCH_SIZE):
            embeddings, tokens = self.model.encode(
                texts[index:index + BATCH_SIZE], task=task,
                dimensions=dimensions, prompt_name=prompt_name,
            )
            all_embeddings.extend(embeddings.tolist())
            total_tokens += tokens
        self._json_response(200, {
            "object": "list", "model": self.model_name,
            "data": [{"object": "embedding", "index": index, "embedding": vector}
                     for index, vector in enumerate(all_embeddings)],
            "usage": {"prompt_tokens": total_tokens, "total_tokens": total_tokens},
        })


def main(argv=None):
    try:
        settings = configure_service(
            argv, description="Jina Embedding 服务", default_model="jina-embed",
            capability="embedding", default_port=8004, environment_prefix="JINA_EMBED",
        )
        def load_handler():
            model = EmbeddingModel(str(settings.model_dir))
            default_task = settings.params.get("default_task", DEFAULT_TASK)
            if not isinstance(default_task, str) or TASK_ALIASES.get(default_task, default_task) not in model.task_names:
                raise ValueError("Invalid embedding default_task in model configuration")
            dimensions = settings.params.get("dimensions")
            if dimensions is not None:
                dimensions = positive_int(dimensions, "dimensions")
                if dimensions > model.hidden_size:
                    raise ValueError("Configured dimensions exceed the model's hidden size")
            return make_handler(EmbeddingHandler, model=model, model_name=settings.alias,
                                default_task=default_task, default_dimensions=dimensions)

        run_service(load_handler, settings)
        return 0
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        return report_startup_error(exc, "embedding")


if __name__ == "__main__":
    raise SystemExit(main())
