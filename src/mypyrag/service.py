"""Authenticated HTTP API for querying already-ready RAG documents."""

from __future__ import annotations

import argparse
import hmac
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from mypyrag.config import Config
from mypyrag.indexing import SearchService
from mypyrag.reranking import RerankingError

log = logging.getLogger(__name__)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    universe: str | None = None
    path_prefix: str | None = None
    top_k: int | None = None


def _runtime(config: Config) -> tuple[SearchService, Any, Callable[[], None]]:
    from mypyrag.database import MigrationManager, PostgresDatabase, PostgresIndexStore
    from mypyrag.ollama import OllamaEmbeddingProvider
    from mypyrag.reranking import create_reranker

    config.require_services()
    database = PostgresDatabase(config)
    MigrationManager(database).require_current()
    store = PostgresIndexStore(database)
    reranker = create_reranker(config)
    provider = OllamaEmbeddingProvider(config)

    def readiness() -> None:
        store.health()
        provider.health()

    return SearchService(config, provider, store, reranker), store, readiness


def create_app(
    config: Config | None = None,
    *,
    search_service: SearchService | None = None,
    store: Any | None = None,
    readiness_probe: Callable[[], None] | None = None,
) -> FastAPI:
    selected_config = config or Config.load()
    if search_service is None or store is None:
        search_service, store, readiness_probe = _runtime(selected_config)

    app = FastAPI(title="MyPyRag", version="1.0.0")
    bearer = HTTPBearer(auto_error=False)

    def authorize(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),  # noqa: B008
    ) -> None:
        valid = (
            credentials is not None
            and credentials.scheme.lower() == "bearer"
            and bool(selected_config.api_token)
            and hmac.compare_digest(credentials.credentials, selected_config.api_token)
        )
        if not valid:
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.get("/health")
    def health() -> dict[str, str]:
        try:
            if readiness_probe is None:
                store.health()
            else:
                readiness_probe()
        except Exception as exc:
            log.warning("Database health check failed: %s", exc)
            raise HTTPException(
                status_code=503,
                detail={"status": "degraded", "service": "mypyrag", "search": "not_ready"},
            ) from exc
        return {"status": "ok", "service": "mypyrag", "database": "ok", "search": "ready"}

    @app.post("/search", dependencies=[Depends(authorize)])
    def search(request: SearchRequest) -> dict[str, Any]:
        try:
            outcome = search_service.search_detailed(
                request.query, request.universe, request.top_k, request.path_prefix
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RerankingError as exc:
            log.exception("Reranking failed")
            raise HTTPException(status_code=503, detail="Reranker unavailable") from exc
        except Exception as exc:
            log.exception("Search failed")
            raise HTTPException(status_code=503, detail="Search backend unavailable") from exc
        return {
            "query": request.query,
            "metadata": {
                "rerank_applied": outcome.diagnostics.rerank_applied,
                "rerank_model": outcome.diagnostics.model,
                "candidate_top_k": outcome.diagnostics.candidate_top_k,
                "result_top_k": outcome.diagnostics.result_top_k,
                "candidate_count": outcome.diagnostics.candidate_count,
                "result_count": outcome.diagnostics.result_count,
                "truncated_count": outcome.diagnostics.truncated_count,
                "timings_ms": {
                    "embedding": round(outcome.diagnostics.embedding_seconds * 1000, 3),
                    "retrieval": round(outcome.diagnostics.retrieval_seconds * 1000, 3),
                    "reranking": round(outcome.diagnostics.rerank_seconds * 1000, 3),
                    "total": round(outcome.diagnostics.total_seconds * 1000, 3),
                },
            },
            "results": [
                {
                    "score": hit.cosine_similarity,
                    "rerank_score": hit.rerank_score,
                    "text": hit.text,
                    "source_path": hit.source_path,
                    "chunk_index": hit.chunk_index,
                    "metadata": {
                        "universe": hit.universe,
                        "path_prefix": request.path_prefix,
                        "title": hit.headings[-1] if hit.headings else None,
                        "document_id": hit.document_id,
                        "chunk_id": hit.chunk_id,
                        "source_type": hit.source_type,
                        "page_numbers": hit.page_numbers,
                        "structural_path": hit.structural_path,
                    },
                }
                for hit in outcome.hits
            ],
        }

    @app.get("/universes", dependencies=[Depends(authorize)])
    def universes() -> dict[str, list[str]]:
        return {"universes": [item.universe for item in store.list_universes()]}

    @app.get("/sources", dependencies=[Depends(authorize)])
    def sources(
        universe: str | None = None,
        path_prefix: str | None = None,
        status: Annotated[
            Literal["queued", "processing", "ready", "failed"] | None, Query()
        ] = None,
    ) -> dict[str, Any]:
        try:
            selected_universe = (
                selected_config.validate_universe(universe) if universe is not None else None
            )
            selected_prefix = path_prefix.strip().strip("/\\") if path_prefix else None
            if path_prefix is not None and not selected_prefix:
                raise ValueError("Path prefix must not be empty")
            items = store.list_sources(selected_universe, selected_prefix, status)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"sources": [item.__dict__ for item in items]}

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mypyrag-service")
    parser.add_argument("--base-dir", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    config = Config.load(args.base_dir)
    config.require_api_token()
    logging.basicConfig(
        level=config.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    uvicorn.run(create_app(config), host=config.service_host, port=config.service_port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
