"""Universe syntax and the filesystem-backed ingestion catalog."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

SEGMENT_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*\Z", re.ASCII)


def is_link_like(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


class UniversePolicy:
    def __init__(self, max_depth: int = 8, max_length: int = 128, segment_max_length: int = 32):
        self.max_depth = max_depth
        self.max_length = max_length
        self.segment_max_length = segment_max_length

    def from_parts(self, parts: Iterable[str], *, context: str = "universe path") -> str:
        segments = tuple(parts)
        if not segments:
            raise ValueError(f"Unclassified {context}: place the file under IN/<universe>/")
        if len(segments) > self.max_depth:
            raise ValueError(
                f"Invalid {context} {'/'.join(segments)!r}: depth exceeds "
                f"{self.max_depth} segments"
            )
        for segment in segments:
            if len(segment) > self.segment_max_length:
                raise ValueError(
                    f"Invalid {context} {'/'.join(segments)!r}: segment {segment!r} exceeds "
                    f"{self.segment_max_length} characters"
                )
            if not SEGMENT_PATTERN.fullmatch(segment):
                raise ValueError(
                    f"Invalid {context} {'/'.join(segments)!r}: segment {segment!r} must match "
                    "[a-z0-9][a-z0-9_-]*"
                )
        value = ".".join(segments)
        self.validate(value, context=context)
        return value

    def validate(self, value: str, *, context: str = "universe") -> str:
        if not value:
            raise ValueError(f"Invalid {context} {value!r}: value is empty")
        if value == "n.a.":
            raise ValueError(f"Invalid {context} {value!r}: n.a. is not indexable")
        if len(value) > self.max_length:
            raise ValueError(
                f"Invalid {context} {value!r}: length exceeds {self.max_length} characters"
            )
        segments = value.split(".")
        if len(segments) > self.max_depth:
            raise ValueError(
                f"Invalid {context} {value!r}: depth exceeds {self.max_depth} segments"
            )
        for segment in segments:
            if not segment:
                raise ValueError(f"Invalid {context} {value!r}: empty segment")
            if len(segment) > self.segment_max_length:
                raise ValueError(
                    f"Invalid {context} {value!r}: segment {segment!r} exceeds "
                    f"{self.segment_max_length} characters"
                )
            if not SEGMENT_PATTERN.fullmatch(segment):
                raise ValueError(
                    f"Invalid {context} {value!r}: segment {segment!r} must match "
                    "[a-z0-9][a-z0-9_-]*"
                )
        return value

    def catalog_directory(self, in_dir: Path, universe: str) -> Path:
        canonical = self.validate(universe)
        root = in_dir.resolve()
        current = in_dir
        for segment in canonical.split("."):
            current = current / segment
            if is_link_like(current):
                raise ValueError(f"Universe {universe!r} uses symlink directory: {current}")
            if not current.is_dir():
                raise ValueError(
                    f"Universe {universe!r} is not present in the ingestion catalog; "
                    f"create directory {in_dir.joinpath(*canonical.split('.'))}"
                )
            resolved = current.resolve()
            if not resolved.is_relative_to(root):
                raise ValueError(f"Universe {universe!r} escapes the IN directory")
            if (current / "manifest.json").exists() or (current / "receipt.json").exists():
                raise ValueError(f"Universe {universe!r} points into a document work directory")
        return current
