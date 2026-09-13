"""Local inference adapter and HTTP protocol, with optional dependencies loaded on demand."""
from __future__ import annotations

import os
import sys
import threading
import time

from .common import (
    ServiceHTTPHandler, configure_service, make_handler, positive_int,
    boolean_field, run_service, report_startup_error,
)


def _log(message):
    sys.stderr.write("[rerank] " + str(message) + "\n")
    sys.stderr.flush()

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

        self._lock = threading.Lock()
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
        with self._lock:
            return self.reranker.rerank(
                query=query,
                documents=documents,
                top_n=top_n,
                return_embeddings=return_embeddings,
            )


class RerankHandler(ServiceHTTPHandler):
    endpoint = "/v1/rerank"
    model_name = "jina-reranker-v3"
    max_documents = 64

    def predict(self):
        body = self._read_body()
        query = body.get("query")
        documents = body.get("documents")
        if not isinstance(query, str) or not query:
            raise ValueError("Missing or invalid 'query' field")
        if not isinstance(documents, list) or not documents:
            raise ValueError("Missing or invalid 'documents' field")
        if any(not isinstance(item, str) for item in documents):
            raise ValueError("'documents' must be an array of strings")
        if len(documents) > self.max_documents:
            raise ValueError(f"Too many documents (max {self.max_documents})")
        top_n = body.get("top_n")
        if top_n is not None:
            top_n = positive_int(top_n, "top_n")
        return_documents = boolean_field(body, "return_documents")
        return_embeddings = boolean_field(body, "return_embeddings")
        results = self.model.rerank(query=query, documents=documents, top_n=top_n,
                                    return_embeddings=return_embeddings)
        payload = []
        for item in results:
            entry = {"index": item["index"], "relevance_score": item["relevance_score"]}
            if return_documents:
                entry["document"] = item.get("document", documents[item["index"]])
            if return_embeddings and item.get("embedding") is not None:
                vector = item["embedding"]
                entry["embedding"] = vector.tolist() if hasattr(vector, "tolist") else list(vector)
            payload.append(entry)
        self._json_response(200, {"model": self.model_name, "results": payload,
                                  "usage": {"total_tokens": 0}})


def main(argv=None):
    try:
        settings = configure_service(
            argv, description="Jina Reranker (MLX) 服务", default_model="jina-rerank-mlx",
            capability="rerank", default_port=8006, environment_prefix="JINA_RERANK",
        )
        max_documents = positive_int(settings.params.get("max_documents", 64), "max_documents")
        def load_handler():
            model = RerankModel(str(settings.model_dir))
            return make_handler(RerankHandler, model=model, model_name=settings.alias,
                                max_documents=max_documents)

        run_service(load_handler, settings)
        return 0
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        return report_startup_error(exc, "rerank")


if __name__ == "__main__":
    raise SystemExit(main())
