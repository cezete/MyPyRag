import json
import math
from dataclasses import replace
from unittest.mock import patch

import httpx
import pytest
from test_pipeline import FakeConverter, put

from mypyrag.chunker import ChunkArtifactWriter, ChunkingSpec, RawChunk
from mypyrag.config import Config
from mypyrag.indexing import (
    ChunkArtifactReader,
    IndexingService,
    SearchHit,
    SearchService,
    input_hash,
    normalize_embedding_text,
    validate_vectors,
)
from mypyrag.model import MaxStage, State
from mypyrag.ollama import OllamaEmbeddingProvider
from mypyrag.pipeline import Pipeline
from mypyrag.storage import atomic_json, load_manifest, save_manifest


class FakeProvider:
    def __init__(self, *, dimensions=768, fail=False):
        self.dimensions = dimensions
        self.fail = fail
        self.validations = 0
        self.inputs = []

    def validate_model(self):
        self.validations += 1
        if self.fail:
            raise RuntimeError("ollama unavailable")
        return "digest"

    def embed(self, texts):
        self.inputs.extend(texts)
        return [[float(index == 0) for index in range(self.dimensions)] for _ in texts]


class FakeStore:
    def __init__(self):
        self.replacements = []
        self.searches = []
        self.hits = []

    def replace_document(self, manifest, universe, chunks, model_digest):
        self.replacements.append((manifest.document_id, universe, chunks, model_digest))
        return len(chunks)

    def search(self, vector, universe, limit):
        self.searches.append((vector, universe, limit))
        return self.hits

    def logical_target(self):
        return "configured-postgresql"


@pytest.fixture
def stage_config(tmp_path, monkeypatch):
    import os

    for key in os.environ:
        if key.startswith("MYPYRAG_"):
            monkeypatch.delenv(key)
    return replace(
        Config.load(tmp_path),
        max_stage=MaxStage.INDEXED,
        file_stable_seconds=0,
    )


def make_chunked(config, text="first\r\nsecond"):
    pipeline = Pipeline(replace(config, max_stage=MaxStage.CONVERTED), FakeConverter())
    directory = pipeline._register(put(pipeline, text="source"))
    assert directory is not None
    manifest = load_manifest(directory)
    manifest.transition(State.CONVERTING)
    manifest.docling_version = "fake-1"
    manifest.transition(State.CONVERTED)
    writer = ChunkArtifactWriter()
    spec = ChunkingSpec.create(config)
    manifest.chunk_count = writer.write(
        directory,
        manifest,
        spec,
        [RawChunk("text", "body", text, ["Heading"], ["Heading"], [1], ["#/texts/0"])],
    )
    manifest.chunking = spec.manifest_dict()
    manifest.transition(State.CHUNKING)
    manifest.transition(State.CHUNKED)
    save_manifest(directory, manifest)
    return directory


def test_new_and_legacy_manifest_universe(stage_config):
    pipeline = Pipeline(stage_config, FakeConverter())
    directory = pipeline._register(put(pipeline))
    assert directory is not None
    payload = json.loads((directory / "manifest.json").read_text())
    assert payload["universe"] == "n.a."
    payload.pop("universe")
    atomic_json(directory / "manifest.json", payload)
    before = (directory / "manifest.json").read_bytes()
    assert load_manifest(directory).universe == "n.a."
    assert (directory / "manifest.json").read_bytes() == before


def test_legacy_allowed_universes_environment_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("MYPYRAG_ALLOWED_UNIVERSES", "old,value")
    assert not hasattr(Config.load(tmp_path), "allowed_universes")


def test_set_universe_updates_atomically_and_validates_state(stage_config):
    directory = make_chunked(stage_config)
    pipeline = Pipeline(stage_config, FakeConverter())
    (stage_config.in_dir / "retro").mkdir()
    (stage_config.in_dir / "python").mkdir()
    with patch("mypyrag.storage.os.replace", wraps=__import__("os").replace) as replace_call:
        assert pipeline.set_universe(directory, "retro") == ("n.a.", "retro")
        assert replace_call.called
    assert load_manifest(directory).universe == "retro"
    before = (directory / "manifest.json").read_bytes()
    with pytest.raises(ValueError, match="not present"):
        pipeline.set_universe(directory, "unknown")
    assert (directory / "manifest.json").read_bytes() == before
    manifest = load_manifest(directory)
    manifest.transition(State.INDEXING)
    save_manifest(directory, manifest)
    with pytest.raises(ValueError, match="reindexing"):
        pipeline.set_universe(directory, "python")


def test_set_universe_creates_legacy_missing_key(stage_config):
    directory = make_chunked(stage_config)
    path = directory / "manifest.json"
    payload = json.loads(path.read_text())
    payload.pop("universe")
    atomic_json(path, payload)
    (stage_config.in_dir / "retro").mkdir()
    Pipeline(stage_config, FakeConverter()).set_universe(directory, "retro")
    assert json.loads(path.read_text())["universe"] == "retro"


@pytest.mark.parametrize(
    "source,expected",
    [("a\r\nb", "a\nb"), ("a\rb", "a\nb"), ("a\nb", "a\nb"), ("a  \tb", "a  \tb")],
)
def test_embedding_normalization(source, expected):
    assert normalize_embedding_text(source) == expected
    assert input_hash(expected) == __import__("hashlib").sha256(expected.encode()).hexdigest()


def test_chunk_reader_uses_json_only_and_preserves_original(stage_config):
    directory = make_chunked(stage_config)
    manifest = load_manifest(directory)
    original = (directory / "CHUNKS/0001.json").read_bytes()
    (directory / "CHUNKS/0001.txt").unlink()
    [chunk] = ChunkArtifactReader().read(directory, manifest)
    assert chunk.embedding_text == "first\nsecond"
    assert chunk.embedding_text_sha256 != chunk.embedding_input_sha256
    assert (directory / "CHUNKS/0001.json").read_bytes() == original


@pytest.mark.parametrize("mutation,match", [
    ({"document_id": "0" * 64}, "document_id"),
    ({"chunk_id": "bad"}, "valid UUID"),
    ({"chunk_index": 2}, "contiguous"),
    ({"text_sha256": "0" * 64}, "text_sha256"),
])
def test_chunk_reader_rejects_corruption_before_network(stage_config, mutation, match):
    directory = make_chunked(stage_config)
    manifest = load_manifest(directory)
    manifest.universe = "retro"
    save_manifest(directory, manifest)
    path = directory / "CHUNKS/0001.json"
    payload = json.loads(path.read_text())
    payload.update(mutation)
    atomic_json(path, payload)
    provider, store = FakeProvider(), FakeStore()
    with pytest.raises(ValueError, match=match):
        IndexingService(stage_config, provider, store).index(directory, manifest)
    assert provider.validations == 0
    assert store.replacements == []


def test_invalid_universe_stops_before_embedding_and_database(stage_config):
    directory = make_chunked(stage_config)
    provider, store = FakeProvider(), FakeStore()
    with pytest.raises(ValueError, match="not indexable"):
        IndexingService(stage_config, provider, store).index(directory, load_manifest(directory))
    assert provider.validations == 0
    assert store.replacements == []


def test_vector_validation():
    validate_vectors([[0.0] * 768], 1, 768)
    with pytest.raises(ValueError, match="dimensions"):
        validate_vectors([[0.0] * 767], 1, 768)
    with pytest.raises(ValueError, match="NaN"):
        validate_vectors([[math.nan] * 768], 1, 768)
    with pytest.raises(ValueError, match="count mismatch"):
        validate_vectors([], 1, 768)


def test_ollama_adapter_checks_tags_digest_and_embedding(stage_config):
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": stage_config.embedding_model,
                            "digest": stage_config.embedding_model_digest,
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"embeddings": [[0.0] * 768]})

    client = httpx.Client(
        base_url=stage_config.ollama_url,
        transport=httpx.MockTransport(handler),
    )
    provider = OllamaEmbeddingProvider(stage_config, client)
    assert provider.validate_model() == stage_config.embedding_model_digest
    assert requests == [("GET", "/api/tags"), ("POST", "/api/embed")]


def test_migration_template_quotes_schema_and_preserves_json_default():
    import importlib.resources

    from psycopg import sql

    template = importlib.resources.files("mypyrag.migrations").joinpath(
        "001_initial.sql"
    ).read_text(encoding="utf-8")
    rendered = sql.SQL(template).format(schema=sql.Identifier("mypyrag")).as_string()
    assert 'CREATE TABLE "mypyrag".documents' in rendered
    assert "DEFAULT '{}'::jsonb" in rendered
    assert "vector(768) NOT NULL" in rendered
    assert "USING hnsw (embedding vector_cosine_ops)" in rendered


def test_full_chunked_to_done_flow_and_cleanup(stage_config):
    directory = make_chunked(stage_config)
    manifest = load_manifest(directory)
    manifest.universe = "retro"
    save_manifest(directory, manifest)
    provider, store = FakeProvider(), FakeStore()
    pipeline = Pipeline(
        stage_config,
        FakeConverter(),
        indexer=IndexingService(stage_config, provider, store),
    )
    assert pipeline.cycle()
    [(_, done)] = pipeline.manifests()
    done_dir = stage_config.done_dir / directory.name
    assert done.current_state == done.last_successful_state == State.DONE
    assert done.indexed_point_count == 1
    assert done.embedding_normalization
    assert done.embedding_model_digest == "digest"
    assert not list((done_dir / "CHUNKS").glob("*.txt"))
    assert len(list((done_dir / "CHUNKS").glob("*.json"))) == 1
    assert provider.inputs == ["first\nsecond"]
    assert len(store.replacements) == 1


def test_index_failure_preserves_artifacts_and_chunked_success_state(stage_config):
    directory = make_chunked(stage_config)
    manifest = load_manifest(directory)
    manifest.universe = "retro"
    save_manifest(directory, manifest)
    pipeline = Pipeline(
        stage_config,
        FakeConverter(),
        indexer=IndexingService(stage_config, FakeProvider(fail=True), FakeStore()),
    )
    assert not pipeline.cycle()
    [(error_dir, failed)] = pipeline.manifests()
    assert error_dir.parent == stage_config.error_dir
    assert failed.current_state == State.ERROR
    assert failed.last_successful_state == State.CHUNKED
    assert failed.failed_stage == "INDEXING"
    assert list((error_dir / "CHUNKS").glob("*.txt"))
    assert list((error_dir / "CHUNKS").glob("*.json"))


def test_search_validation_and_forwarding(stage_config):
    provider, store = FakeProvider(), FakeStore()
    store.hits = [
        SearchHit("c", "d" * 64, "x.md", "retro", "text", "body", [], [], [], 0.25)
    ]
    service = SearchService(stage_config, provider, store)
    assert service.search("query", "retro", 1)[0].cosine_similarity == 0.75
    assert store.searches[0][1:] == ("retro", 1)
    with pytest.raises(ValueError, match="empty"):
        service.search("  ", "retro")
    with pytest.raises(ValueError, match="between"):
        service.search("query", "retro", 0)
    with pytest.raises(ValueError, match="must match"):
        service.search("query", "RETRO", 1)


@pytest.mark.integration
def test_postgres_migrations_and_schema_when_enabled(tmp_path):
    import os

    if os.getenv("MYPYRAG_RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("set MYPYRAG_RUN_POSTGRES_INTEGRATION=1 with PostgreSQL credentials")
    from mypyrag.database import MigrationManager, PostgresDatabase

    config = Config.load(tmp_path)
    config.require_services()
    manager = MigrationManager(PostgresDatabase(config))
    manager.migrate()
    assert manager.migrate() == []
    status = manager.require_current()
    assert status["vector_column"] == "vector(768)"
    assert status["hnsw_cosine_index"]


@pytest.mark.integration
def test_real_ollama_smoke_when_enabled(tmp_path):
    import os

    if os.getenv("MYPYRAG_RUN_OLLAMA_SMOKE") != "1":
        pytest.skip("set MYPYRAG_RUN_OLLAMA_SMOKE=1 to use the configured Ollama service")
    config = Config.load(tmp_path)
    provider = OllamaEmbeddingProvider(config)
    assert provider.validate_model() == config.embedding_model_digest
    vectors = provider.embed(["MyPyRag integration smoke test"])
    validate_vectors(vectors, 1, 768)
