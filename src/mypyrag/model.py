"""Versioned manifest and explicit stage-one transitions."""

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
    schema_version: int = 1
    current_state: State = State.RECEIVED
    last_successful_state: State = State.RECEIVED
    failed_stage: str | None = None
    last_error: str | None = None
    attempt_count: int = 0
    docling_version: str | None = None
    embedding_model: str | None = None
    embedding_vector_size: int | None = None
    chunking: dict[str, Any] | None = None
    chunk_count: int = 0
    indexed_point_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manifest":
        result = cls(**data)
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
            State.CONVERTED: {State.ERROR},
            State.ERROR: {State.CONVERTING, State.ERROR},
        }
        if target not in allowed.get(self.current_state, set()):
            raise ValueError(f"Invalid transition: {self.current_state} -> {target}")
        if target == State.CONVERTING and self.last_successful_state != State.RECEIVED:
            raise ValueError("Conversion requires last_successful_state=RECEIVED")
        self.current_state = target
        if target == State.CONVERTED:
            self.last_successful_state = target
            self.failed_stage = None
            self.last_error = None
        self.updated_at = now()
