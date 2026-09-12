import json
import re
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer
from test_pipeline import FakeConverter, put

from mypyrag.chunker import (
    ChunkArtifactWriter,
    ChunkingSpec,
    DoclingHybridChunker,
    RawChunk,
    chunk_filename_stem,
)
from mypyrag.config import Config
from mypyrag.model import MaxStage, State
from mypyrag.pipeline import Pipeline
from mypyrag.storage import atomic_json, load_manifest


@pytest.fixture
def config(tmp_path, monkeypatch):
    import os

    for key in os.environ:
        if key.startswith("MYPYRAG_"):
            monkeypatch.delenv(key)
    return replace(Config.load(tmp_path), file_stable_seconds=0)


class WordTokenizer(BaseTokenizer):
    max_tokens: int = 512

    def count_tokens(self, text: str) -> int:
        return len(re.findall(r"\w+|[^\w\s]", text, re.UNICODE))

    def get_max_tokens(self) -> int:
        return self.max_tokens

    def get_tokenizer(self):
        return self

    def encode(self, text: str):
        return re.findall(r"\w+|[^\w\s]", text, re.UNICODE)


@pytest.fixture
def chunked_pipeline(config):
    configured = replace(config, max_stage=MaxStage.CHUNKED)
    chunker = DoclingHybridChunker(
        configured, WordTokenizer(max_tokens=configured.chunk_max_tokens)
    )
    return Pipeline(configured, FakeConverter(), chunker)


def test_converted_to_chunked_e2e_and_idempotence(chunked_pipeline):
    assert chunked_pipeline.process(put(chunked_pipeline, text="Useful semantic content."))
    [(directory, manifest)] = chunked_pipeline.manifests()
    assert manifest.current_state == manifest.last_successful_state == State.CHUNKED
    assert manifest.chunk_count == 1
    assert manifest.chunking["fingerprint"]
    record = json.loads((directory / "CHUNKS/0001.json").read_text(encoding="utf-8"))
    assert (directory / "CHUNKS/0001.txt").read_text(encoding="utf-8") == record["embedding_text"]
    assert record["text"] == "Useful semantic content."
    assert record["chunk_index"] == 1
    first_id = record["chunk_id"]
    assert chunked_pipeline.cycle()
    assert json.loads((directory / "CHUNKS/0001.json").read_text())["chunk_id"] == first_id
    assert len(list((directory / "CHUNKS").glob("*.json"))) == manifest.chunk_count


def test_converted_limit_never_calls_chunker(config):
    class ForbiddenChunker:
        @property
        def spec(self):
            raise AssertionError("chunker was consulted")

        def chunks(self, document_json):
            raise AssertionError("chunker was called")

    pipeline = Pipeline(config, FakeConverter(), ForbiddenChunker())
    assert pipeline.process(put(pipeline))
    [(directory, manifest)] = pipeline.manifests()
    assert manifest.current_state == State.CONVERTED
    assert list((directory / "CHUNKS").iterdir()) == []


def test_corrupt_docling_json_is_document_error_and_next_document_continues(chunked_pipeline):
    first = chunked_pipeline._register(put(chunked_pipeline, "a.md", "first"))
    second = chunked_pipeline._register(put(chunked_pipeline, "b.md", "second"))
    for directory in (first, second):
        manifest = load_manifest(directory)
        manifest.transition(State.CONVERTING)
        manifest.transition(State.CONVERTED)
        atomic_json(directory / "manifest.json", manifest.to_dict())
    (first / "JSON/document.json").write_text("broken", encoding="utf-8")
    FakeConverter().convert(second / "source/b.md", second / "JSON/document.json")
    assert not chunked_pipeline.cycle()
    states = {m.original_filename: m for _, m in chunked_pipeline.manifests()}
    assert states["a.md"].current_state == State.ERROR
    assert states["a.md"].last_successful_state == State.CONVERTED
    assert states["a.md"].failed_stage == "CHUNKING"
    assert states["b.md"].current_state == State.CHUNKED


def make_document(path: Path, *, table: bool = False, long_value: bool = False) -> None:
    from docling_core.types.doc import ProvenanceItem
    from docling_core.types.doc.document import DoclingDocument, TableCell, TableData
    from docling_core.types.doc.labels import DocItemLabel

    document = DoclingDocument(name="manual")
    document.add_title("Machine Manual")
    document.add_heading("Kernel calls", level=1)
    provenance = ProvenanceItem(
        page_no=3, bbox={"l": 0, "t": 1, "r": 1, "b": 0}, charspan=(0, 1)
    )
    document.add_text(label=DocItemLabel.TEXT, text="Introduction text", prov=provenance)
    if table:
        value = "SCINIT. " + ("Initialize video hardware safely " * 12 if long_value else "Initialize VIC")
        cells = [
            TableCell(start_row_offset_idx=0, end_row_offset_idx=1, start_col_offset_idx=0, end_col_offset_idx=1, text="Address", column_header=True),
            TableCell(start_row_offset_idx=0, end_row_offset_idx=1, start_col_offset_idx=1, end_col_offset_idx=2, text="Function", column_header=True),
            TableCell(start_row_offset_idx=1, end_row_offset_idx=2, start_col_offset_idx=0, end_col_offset_idx=1, text="$FF81"),
            TableCell(start_row_offset_idx=1, end_row_offset_idx=2, start_col_offset_idx=1, end_col_offset_idx=2, text=value),
        ]
        document.add_table(
            TableData(num_rows=2, num_cols=2, table_cells=cells), prov=provenance
        )
    atomic_json(path, document.export_to_dict())


def test_hybrid_context_and_table_row_reconstruction(config, tmp_path):
    path = tmp_path / "document.json"
    make_document(path, table=True)
    chunker = DoclingHybridChunker(config, WordTokenizer())
    chunks = chunker.chunks(path)
    text_chunk = next(chunk for chunk in chunks if chunk.source_type == "text")
    assert text_chunk.text == "Introduction text"
    assert "Machine Manual" in text_chunk.embedding_text
    assert "Kernel calls" in text_chunk.embedding_text
    assert text_chunk.page_numbers == [3]
    assert text_chunk.docling_references
    table_chunks = [chunk for chunk in chunks if chunk.source_type == "table_row"]
    assert len(table_chunks) == 1
    row = table_chunks[0]
    assert "Address: $FF81" in row.text
    assert "Function: SCINIT. Initialize VIC" in row.text
    assert row.table.column_names == ["Address", "Function"]
    assert row.table.key_fields == {"address": "$FF81", "function": "SCINIT"}
    assert row.table.header_reconstructed
    assert row.page_numbers == [3]
    assert all("Address: $FF81" not in chunk.text for chunk in chunks if chunk.source_type == "text")


def test_long_table_row_splits_only_inside_row_and_repeats_keys(config, tmp_path):
    configured = replace(config, chunk_max_tokens=28)
    path = tmp_path / "document.json"
    make_document(path, table=True, long_value=True)
    rows = [
        chunk
        for chunk in DoclingHybridChunker(configured, WordTokenizer(max_tokens=28)).chunks(path)
        if chunk.source_type == "table_row"
    ]
    assert len(rows) > 1
    assert [row.table.part_index for row in rows] == list(range(1, len(rows) + 1))
    assert all(row.table.part_count == len(rows) for row in rows)
    assert all(row.table.key_fields["address"] == "$FF81" for row in rows)
    assert all("Address: $FF81" in row.embedding_text for row in rows)


def test_artifact_ids_hashes_width_and_fingerprint(config, tmp_path):
    source = tmp_path / "x.md"
    source.write_text("x")
    pipeline = Pipeline(config, FakeConverter())
    directory = pipeline._register(source)
    manifest = load_manifest(directory)
    raw = RawChunk("text", "body", "Heading\nbody", [], [], [], [])
    writer = ChunkArtifactWriter()
    spec = ChunkingSpec.create(config)
    assert writer.write(directory, manifest, spec, [raw]) == 1
    first = json.loads((directory / "CHUNKS/0001.json").read_text())
    assert first["text_sha256"] != first["embedding_text_sha256"]
    assert manifest.document_id not in first["embedding_text"]
    assert first["chunk_id"] not in first["embedding_text"]
    writer.write(directory, manifest, spec, [raw])
    assert json.loads((directory / "CHUNKS/0001.json").read_text())["chunk_id"] == first["chunk_id"]
    changed = ChunkingSpec.create(replace(config, chunk_max_tokens=511))
    writer.write(directory, manifest, changed, [raw])
    assert json.loads((directory / "CHUNKS/0001.json").read_text())["chunk_id"] != first["chunk_id"]


def test_filename_width_grows_with_chunk_count():
    assert chunk_filename_stem(1, 9) == "0001"
    assert chunk_filename_stem(9999, 9999) == "9999"
    assert chunk_filename_stem(10000, 10000) == "10000"


def test_artifact_validation_preserves_embedded_crlf(config, tmp_path):
    source = tmp_path / "x.md"
    source.write_text("x")
    pipeline = Pipeline(config, FakeConverter())
    directory = pipeline._register(source)
    manifest = load_manifest(directory)
    raw = RawChunk("text", "first\r\nsecond", "Heading\r\nfirst\r\nsecond", [], [], [], [])
    writer = ChunkArtifactWriter()

    assert writer.write(directory, manifest, ChunkingSpec.create(config), [raw]) == 1
    artifact = directory / "CHUNKS/0001.txt"
    assert artifact.read_bytes() == raw.embedding_text.encode("utf-8")
    record = json.loads((directory / "CHUNKS/0001.json").read_text(encoding="utf-8"))
    assert record["embedding_text"] == raw.embedding_text


def test_failed_publish_restores_previous_complete_set(config, tmp_path):
    source = tmp_path / "x.md"
    source.write_text("x")
    pipeline = Pipeline(config, FakeConverter())
    directory = pipeline._register(source)
    manifest = load_manifest(directory)
    writer = ChunkArtifactWriter()
    spec = ChunkingSpec.create(config)
    old = RawChunk("text", "old", "old", [], [], [], [])
    new = RawChunk("text", "new", "new", [], [], [], [])
    writer.write(directory, manifest, spec, [old])
    real_replace = __import__("os").replace

    def fail_staging_publish(source_path, destination_path):
        if Path(source_path).name.endswith(".tmp") and Path(destination_path).name == "CHUNKS":
            raise OSError("publish interrupted")
        return real_replace(source_path, destination_path)

    with patch("mypyrag.chunker.os.replace", side_effect=fail_staging_publish), pytest.raises(OSError):
        writer.write(directory, manifest, spec, [new])
    assert (directory / "CHUNKS/0001.txt").read_text() == "old"
    assert len(list((directory / "CHUNKS").iterdir())) == 2
