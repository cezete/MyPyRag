"""Versioned manifest and explicit pipeline transitions."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class State(StrEnum):
    RECEIVED = "RECEIVED"
    CONVERTING = "CONVERTING"
    CONVERTED = "CONVERTED"
    CHUNKING = "CHUNKING"
    CHUNKED = "CHUNKED"
    INDEXING = "INDEXING"
    INDEXED = "INDEXED"
    DONE = "DONE"
    ERROR = "ERROR"


class MaxStage(StrEnum):
    CONVERTED = "CONVERTED"
    CHUNKED = "CHUNKED"
    INDEXED = "INDEXED"


SUCCESS_ORDER = {
    State.RECEIVED: 0,
    State.CONVERTED: 1,
    State.CHUNKED: 2,
    State.INDEXED: 3,
    State.DONE: 4,
}


def reached(success: State, maximum: MaxStage) -> bool:
    return SUCCESS_ORDER[success] >= SUCCESS_ORDER[State(maximum.value)]


def now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Manifest:
    document_id: str
    original_filename: str
    source_relative_path: str
    detected_mime_type: str
    source_type: str
    source_sha256: str
    source_size_bytes: int
    received_at: str
    updated_at: str
    universe: str = "n.a."
    source_path: str | None = None
    schema_version: int = 1
    current_state: State = State.RECEIVED
    last_successful_state: State = State.RECEIVED
    failed_stage: str | None = None
    last_error: str | None = None
    attempt_count: int = 0
    docling_version: str | None = None
    embedding_model: str | None = None
    embedding_model_digest: str | None = None
    embedding_vector_size: int | None = None
    embedding_normalization: str | None = None
    indexed_at: str | None = None
    postgres_target: str | None = None
    database_schema_version: int | None = None
    chunking: dict[str, Any] | None = None
    chunk_count: int = 0
    indexed_point_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manifest":
        # Schema v1 manifests from the first two stages did not contain universe.
        # Treat it logically as n.a. without rewriting the file on a read.
        values = {"universe": "n.a.", **data}
        result = cls(**values)
        if result.schema_version != 1:
            raise ValueError("Unsupported manifest schema_version")
        result.current_state = State(result.current_state)
        result.last_successful_state = State(result.last_successful_state)
        if result.last_successful_state not in SUCCESS_ORDER:
            raise ValueError("last_successful_state must be a completed state")
        if (
            len(result.document_id) != 64
            or any(c not in "0123456789abcdef" for c in result.document_id)
            or result.source_sha256 != result.document_id
        ):
            raise ValueError("Invalid SHA-256 document identity")
        if result.source_size_bytes < 0 or result.attempt_count < 0:
            raise ValueError("Negative manifest size or attempt_count")
        return result

    def transition(self, target: State) -> None:
        allowed = {
            State.RECEIVED: {State.CONVERTING, State.ERROR},
            State.CONVERTING: {State.CONVERTING, State.CONVERTED, State.ERROR},
            State.CONVERTED: {State.CHUNKING, State.ERROR},
            State.CHUNKING: {State.CHUNKING, State.CHUNKED, State.ERROR},
            State.CHUNKED: {State.INDEXING, State.ERROR},
            State.INDEXING: {State.INDEXING, State.INDEXED, State.ERROR},
            State.INDEXED: {State.DONE, State.ERROR},
            State.DONE: set(),
            State.ERROR: {State.CONVERTING, State.CHUNKING, State.INDEXING, State.ERROR},
        }
        if target not in allowed.get(self.current_state, set()):
            raise ValueError(f"Invalid transition: {self.current_state} -> {target}")
        if target == State.CONVERTING and self.last_successful_state != State.RECEIVED:
            raise ValueError("Conversion requires last_successful_state=RECEIVED")
        if target == State.CHUNKING and self.last_successful_state != State.CONVERTED:
            raise ValueError("Chunking requires last_successful_state=CONVERTED")
        if target == State.INDEXING and self.last_successful_state != State.CHUNKED:
            raise ValueError("Indexing requires last_successful_state=CHUNKED")
        if target == State.DONE and self.last_successful_state != State.INDEXED:
            raise ValueError("DONE requires last_successful_state=INDEXED")
        self.current_state = target
        if target in {State.CONVERTED, State.CHUNKED, State.INDEXED, State.DONE}:
            self.last_successful_state = target
            self.failed_stage = None
            self.last_error = None
        self.updated_at = now()
