"""Canonical chunk validation, embedding orchestration, and search contracts."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from mypyrag.chunker import CHUNK_SCHEMA_VERSION, text_hash
from mypyrag.config import Config
from mypyrag.model import Manifest
from mypyrag.reranking import Reranker, RerankingError

NORMALIZATION_VERSION = "newlines-crlf-cr-to-lf-v1"
log = logging.getLogger(__name__)


def normalize_embedding_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def input_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChunkInput:
    chunk_id: uuid.UUID
    document_id: str
    chunk_index: int
    source_type: str
    text: str
    embedding_text: str
    text_sha256: str
    embedding_text_sha256: str
    embedding_input_sha256: str
    original_filename: str
    document_type: str
    headings: list[str]
    structural_path: list[str]
    page_numbers: list[int]
    docling_references: list[str]
    table_metadata: dict[str, Any] | None
    chunking_metadata: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class IndexedChunk:
    artifact: ChunkInput
    embedding: list[float]


@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    document_id: str
    original_filename: str
    universe: str
    source_type: str
    text: str
    headings: list[str]
    structural_path: list[str]
    page_numbers: list[int]
    cosine_distance: float
    chunk_index: int = 0
    source_path: str = ""
    rerank_score: float | None = None

    @property
    def cosine_similarity(self) -> float:
        return 1.0 - self.cosine_distance


@dataclass(frozen=True)
class UniverseSummary:
    universe: str
    documents: int
    chunks: int


@dataclass(frozen=True)
class SourceSummary:
    document_id: str
    source_path: str
    original_filename: str
    universe: str
    status: str
    chunk_count: int
    last_error: str | None


@dataclass(frozen=True)
class SearchDiagnostics:
    rerank_applied: bool
    model: str | None
    candidate_top_k: int
    result_top_k: int
    candidate_count: int
    result_count: int
    truncated_count: int
    embedding_seconds: float
    retrieval_seconds: float
    rerank_seconds: float
    total_seconds: float


@dataclass(frozen=True)
class SearchResult:
    hits: list[SearchHit]
    diagnostics: SearchDiagnostics


class EmbeddingProvider(Protocol):
    def validate_model(self) -> str: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class IndexStore(Protocol):
    def replace_document(
        self, manifest: Manifest, universe: str, chunks: list[IndexedChunk], model_digest: str
    ) -> int: ...

    def search(
        self, vector: list[float], universe: str | None, limit: int, path_prefix: str | None = None
    ) -> list[SearchHit]: ...

    def list_universes(self) -> list[UniverseSummary]: ...

    def logical_target(self) -> str: ...


class ChunkArtifactReader:
    def read(self, directory: Path, manifest: Manifest) -> list[ChunkInput]:
        chunks_dir = directory / "CHUNKS"
        if chunks_dir.is_symlink() or chunks_dir.resolve().parent != directory.resolve():
            raise ValueError("CHUNKS must be a non-symlink document child")
        paths = sorted(chunks_dir.glob("*.json"))
        if len(paths) != manifest.chunk_count:
            raise ValueError(
                f"CHUNKS: expected {manifest.chunk_count} JSON files, found {len(paths)}"
            )
        expected_fingerprint = (manifest.chunking or {}).get("fingerprint")
        result: list[ChunkInput] = []
        identifiers: set[uuid.UUID] = set()
        indices: set[int] = set()
        for path in paths:
            try:
                with path.open(encoding="utf-8", newline="") as stream:
                    record = json.load(stream)
            except Exception as exc:
                raise ValueError(f"{path}: invalid chunk JSON: {exc}") from exc
            field = partial(self._required, record, path=path)
            if field("schema_version") != CHUNK_SCHEMA_VERSION:
                raise ValueError(f"{path}: unsupported schema_version")
            if field("document_id") != manifest.document_id:
                raise ValueError(f"{path}: document_id does not match manifest")
            try:
                chunk_id = uuid.UUID(str(field("chunk_id")))
            except ValueError as exc:
                raise ValueError(f"{path}: chunk_id is not a valid UUID") from exc
            if chunk_id in identifiers:
                raise ValueError(f"{path}: duplicate chunk_id")
            identifiers.add(chunk_id)
            index = field("chunk_index")
            if not isinstance(index, int) or index < 1 or index in indices:
                raise ValueError(f"{path}: chunk_index must be a unique positive integer")
            indices.add(index)
            text = field("text")
            original_embedding_text = field("embedding_text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"{path}: text must not be empty")
            if not isinstance(original_embedding_text, str) or not original_embedding_text.strip():
                raise ValueError(f"{path}: embedding_text must not be empty")
            if field("text_sha256") != text_hash(text):
                raise ValueError(f"{path}: text_sha256 mismatch")
            if field("embedding_text_sha256") != text_hash(original_embedding_text):
                raise ValueError(f"{path}: embedding_text_sha256 mismatch")
            chunking = field("chunking")
            if not isinstance(chunking, dict) or chunking.get("fingerprint") != expected_fingerprint:
                raise ValueError(f"{path}: chunking fingerprint does not match manifest")
            if field("original_filename") != manifest.original_filename:
                raise ValueError(f"{path}: original_filename does not match manifest")
            if field("document_type") != manifest.source_type:
                raise ValueError(f"{path}: document_type does not match manifest")
            normalized = normalize_embedding_text(original_embedding_text)
            result.append(
                ChunkInput(
                    chunk_id=chunk_id,
                    document_id=manifest.document_id,
                    chunk_index=index,
                    source_type=str(field("source_type")),
                    text=text,
                    embedding_text=normalized,
                    text_sha256=field("text_sha256"),
                    embedding_text_sha256=field("embedding_text_sha256"),
                    embedding_input_sha256=input_hash(normalized),
                    original_filename=manifest.original_filename,
                    document_type=manifest.source_type,
                    headings=self._list(record, "headings", path),
                    structural_path=self._list(record, "structural_path", path),
                    page_numbers=self._list(record, "page_numbers", path),
                    docling_references=self._list(record, "docling_references", path),
                    table_metadata=record.get("table"),
                    chunking_metadata=chunking,
                    created_at=str(field("created_at")),
                )
            )
        if indices != set(range(1, manifest.chunk_count + 1)):
            raise ValueError("CHUNKS: chunk_index values must be contiguous from 1")
        return sorted(result, key=lambda item: item.chunk_index)

    @staticmethod
    def _required(record: dict[str, Any], name: str, path: Path) -> Any:
        if name not in record:
            raise ValueError(f"{path}: missing field {name}")
        return record[name]

    @staticmethod
    def _list(record: dict[str, Any], name: str, path: Path) -> list[Any]:
        value = record.get(name, [])
        if not isinstance(value, list):
            raise TypeError(f"{path}: {name} must be an array")
        return value


def validate_vectors(vectors: list[list[float]], expected_count: int, dimensions: int) -> None:
    if len(vectors) != expected_count:
        raise ValueError(f"Embedding count mismatch: expected {expected_count}, got {len(vectors)}")
    for index, vector in enumerate(vectors, 1):
        if len(vector) != dimensions:
            raise ValueError(
                f"Embedding {index} has {len(vector)} dimensions; expected {dimensions}"
            )
        if not all(math.isfinite(value) for value in vector):
            raise ValueError(f"Embedding {index} contains NaN or Infinity")


class IndexingService:
    def __init__(
        self,
        config: Config,
        provider: EmbeddingProvider,
        store: IndexStore,
        reader: ChunkArtifactReader | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.store = store
        self.reader = reader or ChunkArtifactReader()

    def index(self, directory: Path, manifest: Manifest) -> tuple[int, str, float]:
        universe = self.config.validate_universe(manifest.universe)
        chunks = self.reader.read(directory, manifest)
        started = time.monotonic()
        digest = self.provider.validate_model()
        vectors: list[list[float]] = []
        for offset in range(0, len(chunks), self.config.embedding_batch_size):
            batch = chunks[offset : offset + self.config.embedding_batch_size]
            log.info(
                "Embedding document_id=%s universe=%s batch=%d size=%d",
                manifest.document_id[:12],
                universe,
                offset // self.config.embedding_batch_size + 1,
                len(batch),
            )
            embedded = self.provider.embed([chunk.embedding_text for chunk in batch])
            validate_vectors(embedded, len(batch), self.config.embedding_vector_size)
            vectors.extend(embedded)
        validate_vectors(vectors, len(chunks), self.config.embedding_vector_size)
        indexed = [IndexedChunk(chunk, vector) for chunk, vector in zip(chunks, vectors, strict=True)]
        count = self.store.replace_document(manifest, universe, indexed, digest)
        elapsed = time.monotonic() - started
        log.info(
            "Embedding/index complete document_id=%s file=%s chunks=%d elapsed_seconds=%.3f",
            manifest.document_id[:12],
            manifest.original_filename,
            count,
            elapsed,
        )
        return count, digest, elapsed


class SearchService:
    def __init__(
        self,
        config: Config,
        provider: EmbeddingProvider,
        store: IndexStore,
        reranker: Reranker | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.store = store
        self.reranker = reranker

    def search(
        self,
        query: str,
        universe: str | None = None,
        limit: int | None = None,
        path_prefix: str | None = None,
    ) -> list[SearchHit]:
        return self.search_detailed(query, universe, limit, path_prefix).hits

    def search_detailed(
        self,
        query: str,
        universe: str | None = None,
        limit: int | None = None,
        path_prefix: str | None = None,
    ) -> SearchResult:
        started = time.monotonic()
        normalized_query = normalize_embedding_text(query)
        if not normalized_query.strip():
            raise ValueError("Search query must not be empty")
        selected_universe = self.config.validate_universe(universe) if universe else None
        selected_prefix = path_prefix.strip().strip("/\\") if path_prefix else None
        if path_prefix is not None and not selected_prefix:
            raise ValueError("Path prefix must not be empty")
        selected_limit = self.config.search_default_limit if limit is None else limit
        if selected_limit < 1 or selected_limit > self.config.search_max_limit:
            raise ValueError(
                f"Search limit must be between 1 and {self.config.search_max_limit}"
            )
        candidate_limit = max(self.config.search_candidate_top_k, selected_limit)
        self.provider.validate_model()
        embedding_started = time.monotonic()
        vectors = self.provider.embed([normalized_query])
        validate_vectors(vectors, 1, self.config.embedding_vector_size)
        embedding_seconds = time.monotonic() - embedding_started
        retrieval_started = time.monotonic()
        if selected_prefix is None:
            candidates = self.store.search(vectors[0], selected_universe, candidate_limit)
        else:
            candidates = self.store.search(
                vectors[0], selected_universe, candidate_limit, selected_prefix
            )
        retrieval_seconds = time.monotonic() - retrieval_started
        rerank_seconds = 0.0
        truncated_count = 0
        rerank_applied = self.config.search_rerank_enabled
        model = self.config.search_rerank_model if rerank_applied else None
        if rerank_applied and candidates:
            if self.reranker is None:
                raise RerankingError("Reranking is enabled but the reranker is not initialized")
            rerank_started = time.monotonic()
            try:
                scored = self.reranker.score(
                    normalized_query, [self._rerank_document(hit) for hit in candidates]
                )
            except RerankingError:
                raise
            except Exception as exc:
                raise RerankingError("Reranker inference failed") from exc
            if len(scored.values) != len(candidates):
                raise RerankingError("Reranker score count does not match candidate count")
            rerank_seconds = time.monotonic() - rerank_started
            truncated_count = scored.truncated_count
            ranked = sorted(
                (
                    (score, index, SearchHit(**{**hit.__dict__, "rerank_score": score}))
                    for index, (hit, score) in enumerate(
                        zip(candidates, scored.values, strict=True)
                    )
                ),
                key=lambda item: (-item[0], item[1], item[2].chunk_id),
            )
            hits = [hit for _score, _index, hit in ranked[:selected_limit]]
        else:
            hits = candidates[:selected_limit]
        total_seconds = time.monotonic() - started
        diagnostics = SearchDiagnostics(
            rerank_applied=rerank_applied,
            model=model,
            candidate_top_k=candidate_limit,
            result_top_k=selected_limit,
            candidate_count=len(candidates),
            result_count=len(hits),
            truncated_count=truncated_count,
            embedding_seconds=embedding_seconds,
            retrieval_seconds=retrieval_seconds,
            rerank_seconds=rerank_seconds,
            total_seconds=total_seconds,
        )
        log.info(
            "Search universe=%s candidate_top_k=%d result_top_k=%d candidates=%d results=%d "
            "rerank_applied=%s model=%s truncated=%d embedding_seconds=%.3f "
            "retrieval_seconds=%.3f rerank_seconds=%.3f total_seconds=%.3f",
            selected_universe,
            candidate_limit,
            selected_limit,
            len(candidates),
            len(hits),
            rerank_applied,
            model,
            truncated_count,
            embedding_seconds,
            retrieval_seconds,
            rerank_seconds,
            total_seconds,
        )
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "Search ranking before=%s after=%s",
                [hit.chunk_id for hit in candidates],
                [hit.chunk_id for hit in hits],
            )
        return SearchResult(hits, diagnostics)

    @staticmethod
    def _rerank_document(hit: SearchHit) -> str:
        context = list(dict.fromkeys([*hit.structural_path, *hit.headings]))
        if not context:
            return hit.text
        return f"{hit.text}\n\nContext: {' > '.join(context)}"
