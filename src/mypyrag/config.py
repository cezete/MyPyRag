"""Environment overrides .env; relative paths resolve against the selected base directory."""

import logging
import os
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
    ollama_url: str = "http://192.168.3.32:11434"
    embedding_model: str = "nomic-embed-text:latest"
    embedding_vector_size: int = 768
    embedding_model_digest: str = "0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f"
    qdrant_url: str = "http://192.168.3.4:6333"
    qdrant_collection: str = "mypyrag_nomic_embed_text_768_v1"
    qdrant_distance: str = "COSINE"
    qdrant_vector_size: int = 768

    def __post_init__(self) -> None:
        if not isinstance(self.max_stage, MaxStage):
            raise TypeError("MYPYRAG_MAX_STAGE must be CONVERTED, CHUNKED or INDEXED")
        if self.max_stage != MaxStage.CONVERTED:
            raise ValueError(
                f"MYPYRAG_MAX_STAGE={self.max_stage} is not implemented; use CONVERTED"
            )
        for name in (
            "poll_interval_seconds",
            "file_stable_seconds",
            "chunk_max_tokens",
            "embedding_vector_size",
            "qdrant_vector_size",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or value < (0 if name == "file_stable_seconds" else 1):
                raise ValueError(f"MYPYRAG_{name.upper()}: invalid integer range")
        if self.log_level not in logging.getLevelNamesMapping():
            raise ValueError("MYPYRAG_LOG_LEVEL: unknown logging level")
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
        for field in ("ollama_url", "qdrant_url"):
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
            if value is None or not value.strip():
                raise ValueError(f"{key} must not be empty")
            return value.strip()

        defaults = cls(base / "docs/IN", base / "docs/DONE", base / "docs/ERROR")
        kwargs: dict[str, object] = {}
        for name in cls.__dataclass_fields__:
            default = getattr(defaults, name)
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
