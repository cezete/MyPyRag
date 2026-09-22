from dataclasses import replace

from fastapi.testclient import TestClient

from mypyrag.config import Config
from mypyrag.indexing import (
    SearchDiagnostics,
    SearchHit,
    SearchResult,
    SourceSummary,
    UniverseSummary,
)
from mypyrag.service import create_app


class FakeSearchService:
    def __init__(self):
        self.calls = []

    def search_detailed(self, query, universe=None, limit=None, path_prefix=None):
        self.calls.append((query, universe, limit, path_prefix))
        hits = [SearchHit(
                "chunk-id",
                "d" * 64,
                "manual.html",
                "retro",
                "html",
                "CHROUT writes a character.",
                ["CHROUT"],
                ["KERNAL", "CHROUT"],
                [12],
                0.125,
                7,
                "docs/IN/retro/c64/manual.html",
                2.25,
            )
        ]
        diagnostics = SearchDiagnostics(
            True, "test-model", 20, limit or 5, 1, 1, 0, 0.01, 0.02, 0.03, 0.06
        )
        return SearchResult(hits, diagnostics)


class FailingRerankSearchService(FakeSearchService):
    def search_detailed(self, query, universe=None, limit=None, path_prefix=None):
        from mypyrag.reranking import RerankingError

        raise RerankingError("inference failed")


class FakeStore:
    def __init__(self, *, healthy=True):
        self.healthy = healthy
        self.source_calls = []

    def health(self):
        if not self.healthy:
            raise RuntimeError("offline")

    def list_universes(self):
        return [UniverseSummary("retro", 1, 10)]

    def list_sources(self, universe=None, path_prefix=None, status=None):
        self.source_calls.append((universe, path_prefix, status))
        return [
            SourceSummary(
                "d" * 64,
                "docs/IN/retro/c64/manual.html",
                "manual.html",
                "retro",
                "ready",
                10,
                None,
            )
        ]


def make_client(tmp_path, monkeypatch, *, healthy=True):
    for key in list(__import__("os").environ):
        if key.startswith("MYPYRAG_"):
            monkeypatch.delenv(key)
    config = replace(Config.load(tmp_path), api_token="secret")
    search = FakeSearchService()
    store = FakeStore(healthy=healthy)
    app = create_app(config, search_service=search, store=store)
    return TestClient(app), search, store


def test_health_is_public_and_reports_database(tmp_path, monkeypatch):
    client, _search, _store = make_client(tmp_path, monkeypatch)
    assert client.get("/health").json() == {
        "status": "ok",
        "service": "mypyrag",
        "database": "ok",
        "search": "ready",
    }
    failed, _search, _store = make_client(tmp_path, monkeypatch, healthy=False)
    assert failed.get("/health").status_code == 503


def test_health_uses_readiness_probe(tmp_path, monkeypatch):
    _client, search, store = make_client(tmp_path, monkeypatch)
    config = replace(Config.load(tmp_path), api_token="secret")

    def unavailable():
        raise RuntimeError("embedding service offline")

    app = create_app(
        config,
        search_service=search,
        store=store,
        readiness_probe=unavailable,
    )
    response = TestClient(app).get("/health")
    assert response.status_code == 503
    assert response.json()["detail"]["search"] == "not_ready"


def test_protected_endpoints_require_exact_bearer_token(tmp_path, monkeypatch):
    client, _search, _store = make_client(tmp_path, monkeypatch)
    for path in ("/search", "/universes", "/sources"):
        response = (
            client.post(path, json={"query": "test"})
            if path == "/search"
            else client.get(path)
        )
        assert response.status_code == 401
        response = (
            client.post(path, json={"query": "test"}, headers={"Authorization": "Bearer bad"})
            if path == "/search"
            else client.get(path, headers={"Authorization": "Bearer bad"})
        )
        assert response.status_code == 401


def test_search_response_and_filters(tmp_path, monkeypatch):
    client, search, _store = make_client(tmp_path, monkeypatch)
    response = client.post(
        "/search",
        headers={"Authorization": "Bearer secret"},
        json={"query": "What is CHROUT?", "universe": "retro", "path_prefix": "c64", "top_k": 6},
    )
    assert response.status_code == 200
    payload = response.json()
    assert search.calls == [("What is CHROUT?", "retro", 6, "c64")]
    assert payload["results"][0]["score"] == 0.875
    assert payload["results"][0]["rerank_score"] == 2.25
    assert payload["results"][0]["chunk_index"] == 7
    assert payload["results"][0]["metadata"]["title"] == "CHROUT"
    assert payload["metadata"]["rerank_applied"] is True
    assert payload["metadata"]["candidate_top_k"] == 20


def test_universes_and_sources(tmp_path, monkeypatch):
    client, _search, store = make_client(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer secret"}
    assert client.get("/universes", headers=headers).json() == {"universes": ["retro"]}
    response = client.get(
        "/sources?universe=retro&path_prefix=c64&status=ready", headers=headers
    )
    assert response.status_code == 200
    assert store.source_calls == [("retro", "c64", "ready")]
    assert response.json()["sources"][0]["status"] == "ready"


def test_reranker_failure_is_an_explicit_503(tmp_path, monkeypatch):
    _client, _search, store = make_client(tmp_path, monkeypatch)
    config = replace(Config.load(tmp_path), api_token="secret")
    app = create_app(config, search_service=FailingRerankSearchService(), store=store)
    response = TestClient(app).post(
        "/search",
        headers={"Authorization": "Bearer secret"},
        json={"query": "test", "universe": "retro"},
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Reranker unavailable"}
