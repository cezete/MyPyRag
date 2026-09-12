"""Docling-native structured chunking and atomic artifact publication."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from mypyrag.config import Config
from mypyrag.model import Manifest, now
from mypyrag.storage import atomic_json, atomic_text, sync_directory

CHUNK_SCHEMA_VERSION = 1
CHUNKER_SCHEMA_VERSION = "mypyrag-structured-v1"
TABLE_STRATEGY_VERSION = "docling-table-cells-row-v1"
CHUNK_UUID_NAMESPACE = uuid.UUID("66f84d9f-c790-5c94-9fe6-4a93e83cd87d")


def text_hash(text: str) -> str:
    """Hash the exact UTF-8 bytes stored in an artifact."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_filename_stem(index: int, chunk_count: int) -> str:
    """Return the stable one-based filename stem for a complete chunk set."""
    if index < 1 or chunk_count < index:
        raise ValueError("Chunk filename index must be within the complete set")
    return f"{index:0{max(4, len(str(chunk_count)))}d}"


@dataclass(frozen=True)
class ChunkingSpec:
    strategy: str
    implementation_version: str
    max_tokens: int
    tokenizer: str
    context_serialization: str
    table_strategy: str
    fingerprint: str

    @classmethod
    def create(cls, config: Config) -> ChunkingSpec:
        canonical = {
            "strategy": config.chunker,
            "implementation_version": CHUNKER_SCHEMA_VERSION,
            "max_tokens": config.chunk_max_tokens,
            "tokenizer": config.chunk_tokenizer,
            "context_serialization": "docling-hybrid-contextualize-v1",
            "table_strategy": TABLE_STRATEGY_VERSION,
        }
        fingerprint = text_hash(
            json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        return cls(
            strategy=config.chunker,
            implementation_version=CHUNKER_SCHEMA_VERSION,
            max_tokens=config.chunk_max_tokens,
            tokenizer=config.chunk_tokenizer,
            context_serialization="docling-hybrid-contextualize-v1",
            table_strategy=TABLE_STRATEGY_VERSION,
            fingerprint=fingerprint,
        )

    def manifest_dict(self) -> dict[str, Any]:
        return asdict(self)

    def artifact_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "max_tokens": self.max_tokens,
            "tokenizer": self.tokenizer,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class TableMetadata:
    table_index: int
    row_index: int
    column_names: list[str]
    key_fields: dict[str, str]
    header_reconstructed: bool
    part_index: int = 1
    part_count: int = 1


@dataclass(frozen=True)
class RawChunk:
    source_type: str
    text: str
    embedding_text: str
    headings: list[str]
    structural_path: list[str]
    page_numbers: list[int]
    docling_references: list[str]
    table: TableMetadata | None = None


class StructuredChunker(Protocol):
    @property
    def spec(self) -> ChunkingSpec: ...

    def chunks(self, document_json: Path) -> list[RawChunk]: ...


def _item_metadata(items: list[Any]) -> tuple[list[int], list[str]]:
    pages = sorted({p.page_no for item in items for p in getattr(item, "prov", [])})
    refs = list(dict.fromkeys(item.self_ref for item in items if getattr(item, "self_ref", None)))
    return pages, refs


class DoclingHybridChunker:
    """Adapter around Docling HybridChunker plus row-preserving table handling."""

    def __init__(self, config: Config, tokenizer: Any | None = None) -> None:
        self.config = config
        self.spec = ChunkingSpec.create(config)
        self._tokenizer = tokenizer

    def _build(self) -> Any:
        from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
        from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer

        tokenizer = self._tokenizer or HuggingFaceTokenizer.from_pretrained(
            model_name=self.config.chunk_tokenizer,
            max_tokens=self.config.chunk_max_tokens,
        )
        # Peer merging can combine a table item with adjacent prose. Keeping item
        # boundaries prevents that prose from being lost when table chunks are replaced.
        return HybridChunker(tokenizer=tokenizer, merge_peers=False)

    def chunks(self, document_json: Path) -> list[RawChunk]:
        from docling_core.types.doc.document import DoclingDocument
        from docling_core.types.doc.items.table.table import TableItem

        document = DoclingDocument.load_from_json(document_json)
        hybrid = self._build()
        result: list[RawChunk] = []
        table_indexes = {table.self_ref: index for index, table in enumerate(document.tables)}
        emitted_tables: set[str] = set()
        for chunk in hybrid.chunk(document):
            items = list(chunk.meta.doc_items)
            headings = list(chunk.meta.headings or [])
            referenced_tables = [item for item in items if isinstance(item, TableItem)]
            if referenced_tables:
                for table in referenced_tables:
                    if table.self_ref not in emitted_tables:
                        result.extend(
                            self._table_rows(
                                document,
                                table,
                                table_indexes[table.self_ref],
                                headings,
                                hybrid.tokenizer,
                            )
                        )
                        emitted_tables.add(table.self_ref)
                continue
            text = chunk.text.strip()
            embedding = hybrid.contextualize(chunk).strip()
            if not text or not embedding:
                continue
            pages, refs = _item_metadata(items)
            result.append(
                RawChunk(
                    source_type="text",
                    text=text,
                    embedding_text=embedding,
                    headings=headings,
                    structural_path=headings,
                    page_numbers=pages,
                    docling_references=refs,
                )
            )
        result.extend(
            row
            for table_index, table in enumerate(document.tables)
            if table.self_ref not in emitted_tables
            for row in self._table_rows(
                document, table, table_index, [], hybrid.tokenizer
            )
        )
        return result

    @staticmethod
    def _caption(document: Any, table: Any) -> str | None:
        values: list[str] = []
        for reference in table.captions:
            try:
                item = reference.resolve(document)
                if text := getattr(item, "text", "").strip():
                    values.append(text)
            except (AttributeError, KeyError, ValueError):
                continue
        return " | ".join(values) or None

    @staticmethod
    def _grid(table: Any) -> tuple[list[list[str]], list[int]]:
        data = table.data
        grid = [["" for _ in range(data.num_cols)] for _ in range(data.num_rows)]
        header_rows: set[int] = set()
        cells = sorted(
            data.table_cells,
            key=lambda c: (c.start_row_offset_idx, c.start_col_offset_idx),
        )
        for cell in cells:
            if cell.column_header:
                header_rows.update(range(cell.start_row_offset_idx, cell.end_row_offset_idx))
            for row in range(cell.start_row_offset_idx, min(cell.end_row_offset_idx, data.num_rows)):
                for col in range(cell.start_col_offset_idx, min(cell.end_col_offset_idx, data.num_cols)):
                    if not grid[row][col]:
                        grid[row][col] = cell.text.strip()
        return grid, sorted(header_rows)

    @staticmethod
    def _key_fields(columns: list[str], values: list[str]) -> dict[str, str]:
        keys = ("address", "identifier", "name", "function", "command", "opcode", "register", "code", "number", "id")
        result: dict[str, str] = {}
        for column, value in zip(columns, values, strict=True):
            normalized = " ".join(column.casefold().replace("_", " ").split())
            matched = next((key for key in keys if key in normalized.split() or normalized.endswith(key)), None)
            if not matched or not value:
                continue
            selected = value
            if matched in {"function", "command", "name"}:
                prefix = value.split(".", 1)[0].strip()
                if prefix and len(prefix.split()) == 1:
                    selected = prefix
            result[matched] = selected
        return result

    def _table_rows(
        self, document: Any, table: Any, table_index: int, headings: list[str], tokenizer: Any
    ) -> list[RawChunk]:
        grid, header_rows = self._grid(table)
        if not grid:
            return []
        header_reconstructed = bool(header_rows)
        if header_rows:
            columns = [
                " / ".join(dict.fromkeys(grid[row][col] for row in header_rows if grid[row][col]))
                or f"column_{col + 1}"
                for col in range(table.data.num_cols)
            ]
        else:
            columns = [f"column_{col + 1}" for col in range(table.data.num_cols)]
        caption = self._caption(document, table)
        context = [*headings, *([caption] if caption else [])]
        pages, refs = _item_metadata([table])
        result: list[RawChunk] = []
        for row_index, values in enumerate(grid):
            if row_index in header_rows or not any(values):
                continue
            lines = [f"{column}: {value}" for column, value in zip(columns, values, strict=True)]
            key_fields = self._key_fields(columns, values)
            key_lines = [f"{column}: {value}" for column, value in zip(columns, values, strict=True) if any(value == v for v in key_fields.values())]
            parts = self._split_row(lines, context, key_lines, tokenizer)
            for part_index, part in enumerate(parts, 1):
                embedding_lines = [*context, *key_lines, *[line for line in part if line not in key_lines]]
                metadata = TableMetadata(
                    table_index=table_index,
                    row_index=row_index,
                    column_names=columns,
                    key_fields=key_fields,
                    header_reconstructed=header_reconstructed,
                    part_index=part_index,
                    part_count=len(parts),
                )
                result.append(
                    RawChunk(
                        source_type="table_row",
                        text="\n".join(part).strip(),
                        embedding_text="\n".join(embedding_lines).strip(),
                        headings=headings,
                        structural_path=headings,
                        page_numbers=pages,
                        docling_references=refs,
                        table=metadata,
                    )
                )
        return result

    def _split_row(
        self, lines: list[str], context: list[str], key_lines: list[str], tokenizer: Any
    ) -> list[list[str]]:
        prefix = "\n".join([*context, *key_lines])
        parts: list[list[str]] = []
        current: list[str] = []
        for line in lines:
            candidate = [*current, line]
            embedding = "\n".join([prefix, *[item for item in candidate if item not in key_lines]])
            if current and tokenizer.count_tokens(embedding) > self.config.chunk_max_tokens:
                parts.append(current)
                current = []
            embedding = "\n".join([prefix, *([] if line in key_lines else [line])])
            if tokenizer.count_tokens(embedding) <= self.config.chunk_max_tokens:
                current.append(line)
                continue
            label, separator, value = line.partition(": ")
            pieces = self._split_value(label + separator, value, prefix, tokenizer)
            if current:
                parts.append(current)
            parts.extend([[piece] for piece in pieces[:-1]])
            current = [pieces[-1]] if pieces else []
        if current:
            parts.append(current)
        return parts or [lines]

    def _split_value(self, label: str, value: str, prefix: str, tokenizer: Any) -> list[str]:
        words = value.split()
        if not words:
            return [label.rstrip()]
        result: list[str] = []
        current: list[str] = []
        for word in words:
            candidate = label + " ".join([*current, word])
            if current and tokenizer.count_tokens(f"{prefix}\n{candidate}") > self.config.chunk_max_tokens:
                result.append(label + " ".join(current))
                current = [word]
            else:
                current.append(word)
        if current:
            result.append(label + " ".join(current))
        return result


class ChunkArtifactWriter:
    """Build and validate a complete staging set, then swap it into place."""

    def write(
        self, directory: Path, manifest: Manifest, spec: ChunkingSpec, chunks: list[RawChunk]
    ) -> int:
        cleaned = [chunk for chunk in chunks if chunk.text.strip() and chunk.embedding_text.strip()]
        parent = directory.resolve()
        final = (directory / "CHUNKS").resolve()
        if final.parent != parent or final.is_symlink():
            raise ValueError("CHUNKS must be a non-symlink child of the document directory")
        staging = directory / f".CHUNKS.{uuid.uuid4().hex}.tmp"
        backup = directory / f".CHUNKS.{uuid.uuid4().hex}.old"
        staging.mkdir()
        try:
            created = now()
            identifiers: set[str] = set()
            for index, raw in enumerate(cleaned, 1):
                body_hash = text_hash(raw.text)
                chunk_id = str(
                    uuid.uuid5(
                        CHUNK_UUID_NAMESPACE,
                        f"{manifest.document_id}:{spec.fingerprint}:{index}:{body_hash}",
                    )
                )
                if chunk_id in identifiers:
                    raise ValueError("Duplicate deterministic chunk_id")
                identifiers.add(chunk_id)
                record = {
                    "schema_version": CHUNK_SCHEMA_VERSION,
                    "document_id": manifest.document_id,
                    "chunk_id": chunk_id,
                    "chunk_index": index,
                    "source_type": raw.source_type,
                    "text": raw.text,
                    "embedding_text": raw.embedding_text,
                    "text_sha256": body_hash,
                    "embedding_text_sha256": text_hash(raw.embedding_text),
                    "original_filename": manifest.original_filename,
                    "source_relative_path": manifest.source_relative_path,
                    "document_type": manifest.source_type,
                    "headings": raw.headings,
                    "structural_path": raw.structural_path,
                    "page_numbers": raw.page_numbers,
                    "docling_references": raw.docling_references,
                    "chunking": spec.artifact_dict(),
                    "table": asdict(raw.table) if raw.table else None,
                    "created_at": created,
                }
                stem = chunk_filename_stem(index, len(cleaned))
                atomic_text(staging / f"{stem}.txt", raw.embedding_text)
                atomic_json(staging / f"{stem}.json", record)
            self._validate(staging, len(cleaned))
            if final.exists():
                os.replace(final, backup)
            try:
                os.replace(staging, final)
                sync_directory(directory)
            except Exception:
                if backup.exists() and not final.exists():
                    os.replace(backup, final)
                raise
            if backup.exists():
                shutil.rmtree(backup)
            return len(cleaned)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    @staticmethod
    def _validate(staging: Path, expected: int) -> None:
        json_files = sorted(staging.glob("*.json"))
        text_files = sorted(staging.glob("*.txt"))
        if len(json_files) != expected or len(text_files) != expected:
            raise ValueError("Incomplete chunk artifact set")
        ids: set[str] = set()
        for index, (json_path, text_path) in enumerate(zip(json_files, text_files, strict=True), 1):
            with json_path.open(encoding="utf-8") as stream:
                record = json.load(stream)
            if (
                record["schema_version"] != CHUNK_SCHEMA_VERSION
                or record["chunk_index"] != index
                or json_path.stem != text_path.stem
                or int(json_path.stem) != index
            ):
                raise ValueError("Chunk filename/index mismatch")
            # Universal-newline reading would turn CRLF embedded in source-derived
            # text into LF on Windows and create a false content/hash mismatch.
            with text_path.open(encoding="utf-8", newline="") as stream:
                text = stream.read()
            if (
                not record["text"].strip()
                or not text.strip()
                or text != record["embedding_text"]
                or text_hash(record["text"]) != record["text_sha256"]
                or text_hash(text) != record["embedding_text_sha256"]
            ):
                raise ValueError("Chunk text artifact validation failed")
            if record["chunk_id"] in ids:
                raise ValueError("Duplicate chunk_id")
            ids.add(record["chunk_id"])
