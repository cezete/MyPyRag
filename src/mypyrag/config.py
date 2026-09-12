"""Environment overrides .env; relative paths resolve against the selected base directory."""

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from urllib.parse import urlparse

from dotenv import dotenv_values

from mypyrag.model import MaxStage


@dataclass(frozen=True)
class Config:
    in_dir: Path
    done_dir: Path
    error_dir: Path
    max_stage: MaxStage = MaxStage.CONVERTED
    poll_interval_seconds: int = 10
    file_stable_seconds: int = 10
    log_level: str = "INFO"
    docling_json_filename: str = "document.json"
    chunker: str = "hybrid"
    chunk_max_tokens: int = 512
    chunk_tokenizer: str = "nomic-ai/nomic-embed-text-v1.5"
    allowed_universes: tuple[str, ...] = (
        "retro",
        "minecraft",
        "java",
        "python",
        "personal_notes",
    )
    ollama_url: str = "http://192.168.3.32:11434"
    embedding_model: str = "nomic-embed-text:latest"
    embedding_vector_size: int = 768
    embedding_model_digest: str = "0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f"
    embedding_timeout_seconds: int = 120
    embedding_batch_size: int = 16
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5436
    postgres_database: str = "mypyrag"
    postgres_user: str = "mypyrag_app"
    postgres_password: str = ""
    postgres_schema: str = "mypyrag"
    postgres_sslmode: str = "disable"
    postgres_connect_timeout_seconds: int = 10
    search_default_limit: int = 5
    search_max_limit: int = 50

    def __post_init__(self) -> None:
        if not isinstance(self.max_stage, MaxStage):
            raise TypeError("MYPYRAG_MAX_STAGE must be CONVERTED, CHUNKED or INDEXED")
        for name in (
            "poll_interval_seconds",
            "file_stable_seconds",
            "chunk_max_tokens",
            "embedding_vector_size",
            "embedding_timeout_seconds",
            "embedding_batch_size",
            "postgres_port",
            "postgres_connect_timeout_seconds",
            "search_default_limit",
            "search_max_limit",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or value < (0 if name == "file_stable_seconds" else 1):
                raise ValueError(f"MYPYRAG_{name.upper()}: invalid integer range")
        if self.log_level not in logging.getLevelNamesMapping():
            raise ValueError("MYPYRAG_LOG_LEVEL: unknown logging level")
        if self.chunker != "hybrid":
            raise ValueError("MYPYRAG_CHUNKER: only 'hybrid' is supported")
        if self.chunk_max_tokens > 2048:
            raise ValueError("MYPYRAG_CHUNK_MAX_TOKENS must not exceed the 2048-token context")
        name = self.docling_json_filename
        if not name or name in {".", ".."} or any(c in name for c in '/\\:<>"|?*'):
            raise ValueError("MYPYRAG_DOCLING_JSON_FILENAME must be a plain filename")
        if not name.endswith(".json") or name.endswith((" ", ".")):
            raise ValueError("MYPYRAG_DOCLING_JSON_FILENAME must end in .json")
        if PureWindowsPath(name).is_reserved():
            raise ValueError("MYPYRAG_DOCLING_JSON_FILENAME is a reserved Windows filename")
        roots = [self.in_dir.resolve(), self.done_dir.resolve(), self.error_dir.resolve()]
        for i, root in enumerate(roots):
            for other in roots[i + 1 :]:
                if root == other or root in other.parents or other in root.parents:
                    raise ValueError("IN, DONE and ERROR must be separate, non-nested directories")
        normalized = tuple(dict.fromkeys(value.strip().lower() for value in self.allowed_universes))
        if any(not value for value in normalized):
            raise ValueError("MYPYRAG_ALLOWED_UNIVERSES must not contain empty entries")
        if self.max_stage == MaxStage.INDEXED and not normalized:
            raise ValueError("MYPYRAG_ALLOWED_UNIVERSES must not be empty for indexing")
        object.__setattr__(self, "allowed_universes", normalized)
        if self.search_default_limit > self.search_max_limit:
            raise ValueError("MYPYRAG_SEARCH_DEFAULT_LIMIT must not exceed the maximum")
        if self.embedding_vector_size != 768:
            raise ValueError("MYPYRAG_EMBEDDING_VECTOR_SIZE must be 768")
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", self.postgres_schema):
            raise ValueError("MYPYRAG_POSTGRES_SCHEMA must be a safe lowercase SQL identifier")
        if self.postgres_sslmode not in {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}:
            raise ValueError("MYPYRAG_POSTGRES_SSLMODE is invalid")
        for field in ("ollama_url",):
            parsed = urlparse(getattr(self, field))
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(f"MYPYRAG_{field.upper()}: expected HTTP(S) URL")

    @classmethod
    def load(cls, base: Path | None = None) -> "Config":
        base = (base or Path.cwd()).resolve()
        values = {**dotenv_values(base / ".env"), **os.environ}

        def get(name: str, default: str) -> str:
            key = f"MYPYRAG_{name.upper()}"
            value = values.get(key, default)
            if value is None or (not value.strip() and name != "postgres_password"):
                raise ValueError(f"{key} must not be empty")
            return value.strip()

        defaults = cls(base / "docs/IN", base / "docs/DONE", base / "docs/ERROR")
        kwargs: dict[str, object] = {}
        for name in cls.__dataclass_fields__:
            default = getattr(defaults, name)
            if name == "allowed_universes":
                raw = get(name, ",".join(default))
                parts = raw.split(",")
                if any(not part.strip() for part in parts):
                    raise ValueError("MYPYRAG_ALLOWED_UNIVERSES must not contain empty entries")
                kwargs[name] = tuple(part.strip() for part in parts)
                continue
            value = get(name, str(default))
            if name.endswith("_dir"):
                kwargs[name] = (base / value).resolve()
            elif isinstance(default, int):
                try:
                    kwargs[name] = int(value)
                except ValueError as exc:
                    raise ValueError(
                        f"MYPYRAG_{name.upper()}: expected integer, got {value!r}"
                    ) from exc
            elif name == "max_stage":
                try:
                    kwargs[name] = MaxStage(value)
                except ValueError as exc:
                    raise ValueError(
                        "MYPYRAG_MAX_STAGE: expected CONVERTED, CHUNKED or INDEXED"
                    ) from exc
            else:
                kwargs[name] = value
        return cls(**kwargs)  # type: ignore[arg-type]

    def normalize_universe(self, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized or normalized == "n.a." or normalized not in self.allowed_universes:
            allowed = ", ".join(self.allowed_universes)
            raise ValueError(
                f"Universe {value!r} is not allowed; run 'mypyrag set-universe "
                f"<document-directory> <universe>'. Allowed: {allowed}"
            )
        return normalized

    def require_services(self) -> None:
        missing = []
        for name in ("postgres_host", "postgres_database", "postgres_user", "postgres_password"):
            if not str(getattr(self, name)).strip():
                missing.append(f"MYPYRAG_{name.upper()}")
        if not self.allowed_universes:
            missing.append("MYPYRAG_ALLOWED_UNIVERSES")
        if missing:
            raise ValueError("Missing configuration required for indexing/search: " + ", ".join(missing))
