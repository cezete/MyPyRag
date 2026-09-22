"""Small explicit adapter for Ollama's documented /api/tags and /api/embed APIs."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from mypyrag.config import Config
from mypyrag.indexing import validate_vectors

log = logging.getLogger(__name__)


class OllamaEmbeddingProvider:
    def __init__(self, config: Config, client: httpx.Client | None = None) -> None:
        self.config = config
        self.client = client or httpx.Client(
            base_url=config.ollama_url.rstrip("/"), timeout=config.embedding_timeout_seconds
        )

    def health(self) -> None:
        """Cheap readiness check: Ollama responds and the configured model is present."""
        response = self.client.get(
            "/api/tags", timeout=min(5, self.config.embedding_timeout_seconds)
        )
        response.raise_for_status()
        models = response.json().get("models", [])
        match = next(
            (
                model
                for model in models
                if model.get("name") == self.config.embedding_model
                or model.get("model") == self.config.embedding_model
            ),
            None,
        )
        if match is None:
            raise ValueError(f"Ollama model is not installed: {self.config.embedding_model}")
        digest = str(match.get("digest", ""))
        if self.config.embedding_model_digest and digest != self.config.embedding_model_digest:
            raise ValueError(f"Ollama model digest mismatch for {self.config.embedding_model}")

    def validate_model(self) -> str:
        response = self.client.get("/api/tags")
        response.raise_for_status()
        models = response.json().get("models", [])
        match = next(
            (
                model
                for model in models
                if model.get("name") == self.config.embedding_model
                or model.get("model") == self.config.embedding_model
            ),
            None,
        )
        if match is None:
            raise ValueError(f"Ollama model is not installed: {self.config.embedding_model}")
        digest = str(match.get("digest", ""))
        if self.config.embedding_model_digest and digest != self.config.embedding_model_digest:
            raise ValueError(
                f"Ollama model digest mismatch for {self.config.embedding_model}: "
                f"expected {self.config.embedding_model_digest}, got {digest or '<missing>'}"
            )
        control = self.embed(["MyPyRag embedding dimension check"])
        validate_vectors(control, 1, self.config.embedding_vector_size)
        log.info(
            "Ollama embedding model verified model=%s dimensions=%d",
            self.config.embedding_model,
            self.config.embedding_vector_size,
        )
        return digest

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        response = self.client.post(
            "/api/embed",
            json={"model": self.config.embedding_model, "input": texts, "truncate": False},
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list):
            raise TypeError("Ollama /api/embed response has no embeddings array")
        try:
            return [[float(value) for value in vector] for vector in embeddings]
        except (TypeError, ValueError) as exc:
            raise ValueError("Ollama returned a malformed embedding vector") from exc
