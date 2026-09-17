from dataclasses import replace

import pytest

from mypyrag.config import Config
from mypyrag.indexing import SearchHit, SearchService
from mypyrag.reranking import RerankScores, create_reranker


class FakeProvider:
    def __init__(self):
        self.inputs = []

    def validate_model(self):
        return "digest"

    def embed(self, texts):
        self.inputs.extend(texts)
        return [[1.0] + [0.0] * 767 for _ in texts]


class FakeStore:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    def search(self, vector, universe, limit, path_prefix=None):
        self.calls.append((universe, limit, path_prefix))
        return self.hits[:limit]


class FakeReranker:
    model_name = "fake-cross-encoder"
    max_length = 512

    def __init__(self, scores, *, truncated=0, fail=False):
        self.scores = scores
        self.truncated = truncated
        self.fail = fail
        self.calls = []

    def score(self, query, documents):
        self.calls.append((query, documents))
        if self.fail:
            raise RuntimeError("inference failed")
        return RerankScores(self.scores[: len(documents)], self.truncated)


def hit(index, *, universe="retro.c64", text=None, score=None):
    distance = 1.0 - (score if score is not None else 0.9 - index / 100)
    return SearchHit(
        f"chunk-{index}",
        "d" * 64,
        "C64_Programmers_Reference_Guide.pdf",
        universe,
        "table_row",
        text or f"body-{index}",
        ["ABBREVIATIONS FOR BASIC KEYWORDS"],
        ["Reference", "ABBREVIATIONS FOR BASIC KEYWORDS"],
        [394],
        distance,
        index,
        "docs/IN/retro/c64/manual.pdf",
    )


@pytest.fixture
def config(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("MYPYRAG_") and key != "MYPYRAG_RUN_RERANKER_SMOKE":
            monkeypatch.delenv(key)
    return Config.load(tmp_path)


def test_reranker_reorders_and_keeps_scores_metadata_and_compact_context(config):
    candidates = [hit(1, text="?", score=0.95), hit(2, text="PRINT displays output", score=0.7)]
    store = FakeStore(candidates)
    reranker = FakeReranker([-2.0, 7.0], truncated=1)
    outcome = SearchService(config, FakeProvider(), store, reranker).search_detailed(
        "What does PRINT do?", "retro.c64"
    )
    assert [item.chunk_id for item in outcome.hits] == ["chunk-2", "chunk-1"]
    assert [item.rerank_score for item in outcome.hits] == [7.0, -2.0]
    assert [item.cosine_similarity for item in outcome.hits] == pytest.approx([0.7, 0.95])
    assert outcome.hits[0].text == "PRINT displays output"
    assert outcome.hits[0].page_numbers == [394]
    assert store.calls == [("retro.c64", 20, None)]
    document = reranker.calls[0][1][0]
    assert document == "?\n\nContext: Reference > ABBREVIATIONS FOR BASIC KEYWORDS"
    assert outcome.diagnostics.truncated_count == 1


def test_limits_filters_empty_and_ties(config):
    candidates = [hit(index) for index in range(1, 21)]
    store = FakeStore(candidates)
    tied = FakeReranker([1.0] * 20)
    service = SearchService(config, FakeProvider(), store, tied)
    outcome = service.search_detailed("query", "retro.c64", path_prefix="c64")
    assert len(outcome.hits) == 5
    assert [item.chunk_id for item in outcome.hits] == [f"chunk-{index}" for index in range(1, 6)]
    assert store.calls == [("retro.c64", 20, "c64")]

    configured = replace(config, search_candidate_top_k=3, search_default_limit=2)
    store = FakeStore(candidates)
    outcome = SearchService(configured, FakeProvider(), store, FakeReranker([1.0] * 7)).search_detailed(
        "query", "retro.c64", 7
    )
    assert store.calls == [("retro.c64", 7, None)]
    assert len(outcome.hits) == 7

    empty_store = FakeStore([])
    unused = FakeReranker([])
    empty = SearchService(config, FakeProvider(), empty_store, unused).search_detailed(
        "query", "retro.c64"
    )
    assert empty.hits == []
    assert unused.calls == []


def test_disabled_mode_does_not_initialize_or_score(config):
    disabled = replace(config, search_rerank_enabled=False)
    assert create_reranker(disabled) is None
    store = FakeStore([hit(1), hit(2)])
    outcome = SearchService(disabled, FakeProvider(), store).search_detailed("query", "retro.c64")
    assert [item.chunk_id for item in outcome.hits] == ["chunk-1", "chunk-2"]
    assert all(item.rerank_score is None for item in outcome.hits)
    assert outcome.diagnostics.rerank_applied is False


def test_boolean_property_parsing(tmp_path, monkeypatch):
    monkeypatch.setenv("MYPYRAG_SEARCH_RERANK_ENABLED", "false")
    assert Config.load(tmp_path).search_rerank_enabled is False
    monkeypatch.setenv("MYPYRAG_SEARCH_RERANK_ENABLED", "sometimes")
    with pytest.raises(ValueError, match="expected true or false"):
        Config.load(tmp_path)


def test_enabled_missing_or_failed_reranker_is_visible(config):
    store = FakeStore([hit(1)])
    with pytest.raises(RuntimeError, match="not initialized"):
        SearchService(config, FakeProvider(), store).search("query", "retro.c64")
    with pytest.raises(RuntimeError, match="inference failed"):
        SearchService(config, FakeProvider(), store, FakeReranker([], fail=True)).search(
            "query", "retro.c64"
        )


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"search_candidate_top_k": 0}, "invalid integer"),
        ({"search_default_limit": 21, "search_candidate_top_k": 20}, "CANDIDATE_TOP_K"),
        ({"search_rerank_batch_size": 0}, "invalid integer"),
        ({"search_rerank_enabled": "false"}, "must be true or false"),
    ],
)
def test_invalid_rerank_config(config, changes, match):
    with pytest.raises((TypeError, ValueError), match=match):
        replace(config, **changes)


@pytest.mark.integration
def test_real_cross_encoder_smoke_when_enabled(config):
    import os

    if os.getenv("MYPYRAG_RUN_RERANKER_SMOKE") != "1":
        pytest.skip("set MYPYRAG_RUN_RERANKER_SMOKE=1 to load the real cross-encoder")
    reranker = create_reranker(config)
    assert reranker is not None
    scores = reranker.score("What does BASIC PRINT do?", ["PRINT writes output.", "Disk drive setup."])
    assert len(scores.values) == 2
    assert scores.values[0] > scores.values[1]
