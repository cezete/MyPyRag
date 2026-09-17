"""Cross-encoder lifecycle and bounded CPU inference for search reranking."""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from mypyrag.config import Config

log = logging.getLogger(__name__)


class RerankingError(RuntimeError):
    """A visible runtime failure for bounded cross-encoder inference."""


@dataclass(frozen=True)
class RerankScores:
    values: list[float]
    truncated_count: int


class Reranker(Protocol):
    model_name: str
    max_length: int

    def score(self, query: str, documents: list[str]) -> RerankScores: ...


class CrossEncoderReranker:
    """One process-wide model instance with fail-fast overload handling."""

    def __init__(self, config: Config) -> None:
        # Imports stay service-only: the worker never constructs this class.
        import torch
        from sentence_transformers import CrossEncoder

        torch.set_num_threads(config.search_rerank_threads)
        torch.set_num_interop_threads(1)
        self.model_name = config.search_rerank_model
        self.batch_size = config.search_rerank_batch_size
        self._slots = threading.BoundedSemaphore(config.search_rerank_max_concurrency)
        log.info(
            "Loading reranker model=%s device=%s threads=%d max_concurrency=%d",
            self.model_name,
            config.search_rerank_device,
            config.search_rerank_threads,
            config.search_rerank_max_concurrency,
        )
        self._model = CrossEncoder(self.model_name, device=config.search_rerank_device)
        configured_max = getattr(self._model, "max_seq_length", None)
        tokenizer_max = getattr(self._model.tokenizer, "model_max_length", None)
        raw_max = configured_max if configured_max is not None else tokenizer_max
        if not isinstance(raw_max, (int, str)):
            raise TypeError(f"Reranker reported invalid tokenizer maximum: {raw_max!r}")
        self.max_length = int(raw_max)
        if self.max_length <= 0 or self.max_length > 1_000_000:
            raise RuntimeError(f"Reranker reported invalid tokenizer maximum: {self.max_length}")
        log.info("Reranker ready model=%s max_length=%d", self.model_name, self.max_length)

    def score(self, query: str, documents: list[str]) -> RerankScores:
        if not documents:
            return RerankScores([], 0)
        if not self._slots.acquire(blocking=False):
            raise RerankingError("Reranker is busy; concurrent inference limit reached")
        try:
            truncated = sum(self._would_truncate(query, document) for document in documents)
            raw = self._model.predict(
                [(query, document) for document in documents],
                batch_size=self.batch_size,
                show_progress_bar=False,
            )
            values = [float(value) for value in raw]
            if len(values) != len(documents) or not all(math.isfinite(value) for value in values):
                raise RerankingError("Reranker returned an invalid score vector")
            return RerankScores(values, truncated)
        finally:
            self._slots.release()

    def _would_truncate(self, query: str, document: str) -> bool:
        encoded: dict[str, Any] = self._model.tokenizer(
            query,
            document,
            add_special_tokens=True,
            truncation=False,
        )
        return len(encoded["input_ids"]) > self.max_length


def create_reranker(config: Config) -> Reranker | None:
    if not config.search_rerank_enabled:
        log.info("Reranking disabled; cross-encoder will not be loaded")
        return None
    return CrossEncoderReranker(config)
