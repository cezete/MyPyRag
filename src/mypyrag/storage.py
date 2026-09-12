"""Durable local storage primitives; never overwrite a source document."""

import hashlib
import json
import os
import re
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any

from mypyrag.model import Manifest


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_text(path: Path, text: str) -> None:
    """Write exact UTF-8 text through a same-directory atomic replacement."""
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        # newline="" preserves the caller's exact text bytes on every platform.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def sync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def load_manifest(directory: Path) -> Manifest:
    with (directory / "manifest.json").open(encoding="utf-8") as stream:
        return Manifest.from_dict(json.load(stream))


def save_manifest(directory: Path, manifest: Manifest) -> None:
    atomic_json(directory / "manifest.json", manifest.to_dict())


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_name(stem: str, document_id: str) -> str:
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_-")[:80] or "document"
    return f"{stem}--{document_id[:8]}"


def source_path(directory: Path, relative: str) -> Path:
    path = directory / relative
    resolved = path.resolve()
    source = (directory / "source").resolve()
    if path.is_symlink() or source != resolved.parent or source.parent != directory.resolve():
        raise ValueError("Manifest source path must be a regular file inside source/")
    if not path.is_file():
        raise ValueError(f"Missing source: {relative}")
    return path


def signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


class StabilityTracker:
    def __init__(self, seconds: int) -> None:
        self.seconds = seconds
        self.observations: dict[Path, tuple[tuple[int, int], float]] = {}

    def ready(self, path: Path, observed_at: float | None = None) -> bool:
        moment = time.monotonic() if observed_at is None else observed_at
        current = signature(path)
        previous = self.observations.get(path)
        if previous is None or previous[0] != current:
            self.observations[path] = (current, moment)
            return self.seconds == 0
        return moment - previous[1] >= self.seconds

    def prune(self, paths: set[Path]) -> None:
        self.observations = {p: v for p, v in self.observations.items() if p in paths}
