"""Environment overrides .env; relative paths resolve against the selected base directory."""

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from urllib.parse import urlparse

from dotenv import dotenv_values

from mypyrag.model import MaxStage
from mypyrag.universe import UniversePolicy


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
    universe_max_depth: int = 8
    universe_max_length: int = 128
    universe_segment_max_length: int = 32
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
    search_candidate_top_k: int = 20
    search_rerank_enabled: bool = True
    search_rerank_model: str = "cross-encoder/ms-marco-MiniLM-L6-v2"
    search_rerank_device: str = "cpu"
    search_rerank_batch_size: int = 8
    search_rerank_max_concurrency: int = 1
    search_rerank_threads: int = 2
    service_host: str = "127.0.0.1"
    service_port: int = 8765
    api_token: str = ""
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8766
    mcp_path: str = "/mcp"
    mcp_rag_service_url: str = "http://127.0.0.1:8765"
    mcp_connect_timeout_seconds: int = 10
    mcp_read_timeout_seconds: int = 180
    mcp_health_timeout_seconds: int = 5
    mcp_access_token: str = ""
    mcp_allowed_hosts: str = ""
    mcp_allowed_origins: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.max_stage, MaxStage):
            raise TypeError("MYPYRAG_MAX_STAGE must be CONVERTED, CHUNKED or INDEXED")
        for name in (
            "poll_interval_seconds",
            "file_stable_seconds",
            "chunk_max_tokens",
            "universe_max_depth",
            "universe_max_length",
            "universe_segment_max_length",
            "embedding_vector_size",
            "embedding_timeout_seconds",
            "embedding_batch_size",
            "postgres_port",
            "postgres_connect_timeout_seconds",
            "search_default_limit",
            "search_max_limit",
            "search_candidate_top_k",
            "search_rerank_batch_size",
            "search_rerank_max_concurrency",
            "search_rerank_threads",
            "service_port",
            "mcp_port",
            "mcp_connect_timeout_seconds",
            "mcp_read_timeout_seconds",
            "mcp_health_timeout_seconds",
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
        if self.universe_segment_max_length > self.universe_max_length:
            raise ValueError(
                "MYPYRAG_UNIVERSE_SEGMENT_MAX_LENGTH must not exceed universe maximum length"
            )
        if self.search_default_limit > self.search_max_limit:
            raise ValueError("MYPYRAG_SEARCH_DEFAULT_LIMIT must not exceed the maximum")
        if self.search_default_limit > self.search_candidate_top_k:
            raise ValueError(
                "MYPYRAG_SEARCH_DEFAULT_LIMIT must not exceed MYPYRAG_SEARCH_CANDIDATE_TOP_K"
            )
        if not isinstance(self.search_rerank_enabled, bool):
            raise TypeError("MYPYRAG_SEARCH_RERANK_ENABLED must be true or false")
        if not self.search_rerank_model:
            raise ValueError("MYPYRAG_SEARCH_RERANK_MODEL must not be empty")
        if not self.search_rerank_device:
            raise ValueError("MYPYRAG_SEARCH_RERANK_DEVICE must not be empty")
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
        if not self.service_host:
            raise ValueError("MYPYRAG_SERVICE_HOST must not be empty")
        if self.service_port > 65535:
            raise ValueError("MYPYRAG_SERVICE_PORT must be at most 65535")
        if not self.mcp_host:
            raise ValueError("MYPYRAG_MCP_HOST must not be empty")
        if self.mcp_port > 65535:
            raise ValueError("MYPYRAG_MCP_PORT must be at most 65535")
        if not self.mcp_path.startswith("/") or self.mcp_path == "/" or self.mcp_path.endswith("/"):
            raise ValueError("MYPYRAG_MCP_PATH must start with '/', must not be '/', and must not end with '/'")
        parsed_rag_url = urlparse(self.mcp_rag_service_url)
        if parsed_rag_url.scheme not in {"http", "https"} or not parsed_rag_url.netloc:
            raise ValueError("MYPYRAG_MCP_RAG_SERVICE_URL: expected HTTP(S) URL")
        if parsed_rag_url.query or parsed_rag_url.fragment:
            raise ValueError("MYPYRAG_MCP_RAG_SERVICE_URL must not contain a query or fragment")

    @classmethod
    def load(cls, base: Path | None = None) -> "Config":
        base = (base or Path.cwd()).resolve()
        values = {**dotenv_values(base / ".env"), **os.environ}

        def get(name: str, default: str) -> str:
            key = f"MYPYRAG_{name.upper()}"
            value = values.get(key, default)
            if value is None or (
                not value.strip()
                and name
                not in {
                    "postgres_password",
                    "api_token",
                    "mcp_access_token",
                    "mcp_allowed_hosts",
                    "mcp_allowed_origins",
                }
            ):
                raise ValueError(f"{key} must not be empty")
            return value.strip()

        defaults = cls(base / "docs/IN", base / "docs/DONE", base / "docs/ERROR")
        kwargs: dict[str, object] = {}
        for name in cls.__dataclass_fields__:
            default = getattr(defaults, name)
            value = get(name, str(default))
            if name.endswith("_dir"):
                kwargs[name] = (base / value).resolve()
            elif isinstance(default, bool):
                normalized = value.casefold()
                if normalized not in {"true", "false"}:
                    raise ValueError(f"MYPYRAG_{name.upper()}: expected true or false")
                kwargs[name] = normalized == "true"
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

    @property
    def universe_policy(self) -> UniversePolicy:
        return UniversePolicy(
            max_depth=self.universe_max_depth,
            max_length=self.universe_max_length,
            segment_max_length=self.universe_segment_max_length,
        )

    def validate_universe(self, value: str) -> str:
        return self.universe_policy.validate(value)

    def require_services(self) -> None:
        missing = []
        for name in ("postgres_host", "postgres_database", "postgres_user", "postgres_password"):
            if not str(getattr(self, name)).strip():
                missing.append(f"MYPYRAG_{name.upper()}")
        if missing:
            raise ValueError("Missing configuration required for indexing/search: " + ", ".join(missing))

    def require_api_token(self) -> None:
        if not self.api_token:
            raise ValueError("MYPYRAG_API_TOKEN must be configured for the HTTP service")

    def require_mcp_tokens(self) -> None:
        missing = []
        if not self.api_token:
            missing.append("MYPYRAG_API_TOKEN (backend service token)")
        if not self.mcp_access_token:
            missing.append("MYPYRAG_MCP_ACCESS_TOKEN")
        if missing:
            raise ValueError("Missing configuration required for the MCP server: " + ", ".join(missing))

    @staticmethod
    def _csv_values(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    @property
    def effective_mcp_allowed_hosts(self) -> list[str]:
        configured = self._csv_values(self.mcp_allowed_hosts)
        if configured:
            return configured
        return [f"127.0.0.1:{self.mcp_port}", f"localhost:{self.mcp_port}"]

    @property
    def effective_mcp_allowed_origins(self) -> list[str]:
        return self._csv_values(self.mcp_allowed_origins)
