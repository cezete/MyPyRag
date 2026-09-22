"""Command-line entry points for ingestion, indexing, migrations, and search."""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections import Counter
from pathlib import Path
from typing import Any

from mypyrag.config import Config
from mypyrag.converter import DoclingAdapter
from mypyrag.model import MaxStage
from mypyrag.pipeline import Pipeline
from mypyrag.storage import load_manifest

log = logging.getLogger(__name__)


def _services(config: Config) -> tuple[Any, Any, Any]:
    from mypyrag.database import MigrationManager, PostgresDatabase, PostgresIndexStore
    from mypyrag.indexing import IndexingService
    from mypyrag.ollama import OllamaEmbeddingProvider

    config.require_services()
    database = PostgresDatabase(config)
    MigrationManager(database).require_current()
    provider = OllamaEmbeddingProvider(config)
    store = PostgresIndexStore(database)
    return IndexingService(config, provider, store), provider, store


def _index_store(config: Config) -> Any:
    from mypyrag.database import MigrationManager, PostgresDatabase, PostgresIndexStore

    config.require_services()
    database = PostgresDatabase(config)
    MigrationManager(database).require_current()
    return PostgresIndexStore(database)


def _pipeline(config: Config, *, with_indexing: bool | None = None) -> Pipeline:
    enabled = config.max_stage == MaxStage.INDEXED if with_indexing is None else with_indexing
    indexer = _services(config)[0] if enabled else None
    return Pipeline(config, DoclingAdapter(), indexer=indexer)


def _status(config: Config) -> int:
    counts: Counter[str] = Counter()
    invalid = 0
    for root in (config.in_dir, config.done_dir, config.error_dir):
        if not root.exists():
            continue
        for directory in sorted(root.iterdir()):
            if directory.is_symlink() or not directory.is_dir():
                continue
            if not (directory / "manifest.json").exists():
                if (directory / "receipt.json").exists():
                    print(f"PENDING_RECEIPT {directory}")
                    invalid += 1
                continue
            try:
                manifest = load_manifest(directory)
                counts[manifest.current_state] += 1
                print(
                    f"{manifest.current_state:12} {manifest.document_id[:12]} "
                    f"{manifest.original_filename} universe={manifest.universe} "
                    f"last={manifest.last_successful_state} chunks={manifest.chunk_count}"
                    + (f" error={manifest.last_error}" if manifest.last_error else "")
                    + f" [{directory}]"
                )
            except Exception:
                log.exception("Invalid manifest: %s", directory)
                invalid += 1
    print(
        f"Total: {sum(counts.values())}; invalid/pending: {invalid}; "
        + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    )
    return 1 if invalid else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mypyrag")
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help="Resolve .env and configured directories here (default: cwd)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("watch")
    process = commands.add_parser("process")
    process.add_argument("file", type=Path)
    commands.add_parser("status")
    resume = commands.add_parser("resume")
    resume.add_argument("document_directory", type=Path)
    commands.add_parser("retry-errors")
    commands.add_parser("mcp")
    set_universe = commands.add_parser("set-universe")
    set_universe.add_argument("document_directory", type=Path)
    set_universe.add_argument("universe")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--universe", required=True)
    search.add_argument("--limit", type=int)
    search.add_argument("--json", action="store_true", dest="as_json")
    list_universes = commands.add_parser("list-universes")
    list_universes.add_argument("--json", action="store_true", dest="as_json")
    database = commands.add_parser("db")
    database_commands = database.add_subparsers(dest="db_command", required=True)
    database_commands.add_parser("migrate")
    database_commands.add_parser("status")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        config = Config.load(args.base_dir)
        logging.basicConfig(
            level=config.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
        )
        if args.command == "status":
            return _status(config)
        if args.command == "mcp":
            from mypyrag.mcp_server import run

            return run(config)
        if args.command == "db":
            from mypyrag.database import MigrationManager, PostgresDatabase

            config.require_services()
            migrations = MigrationManager(PostgresDatabase(config))
            if args.db_command == "migrate":
                applied = migrations.migrate()
                print("Applied migrations: " + (", ".join(map(str, applied)) or "none"))
            print(json.dumps(migrations.status(), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "search":
            from mypyrag.indexing import SearchService
            from mypyrag.reranking import create_reranker

            _indexer, provider, store = _services(config)
            started = time.monotonic()
            hits = SearchService(config, provider, store, create_reranker(config)).search(
                args.query, args.universe, args.limit
            )
            if args.as_json:
                print(
                    json.dumps(
                        [
                            {
                                "rank": rank,
                                "cosine_similarity": hit.cosine_similarity,
                                "cosine_distance": hit.cosine_distance,
                                "rerank_score": hit.rerank_score,
                                "chunk_id": hit.chunk_id,
                                "document_id": hit.document_id,
                                "original_filename": hit.original_filename,
                                "universe": hit.universe,
                                "source_type": hit.source_type,
                                "headings": hit.headings,
                                "structural_path": hit.structural_path,
                                "page_numbers": hit.page_numbers,
                                "text": hit.text,
                            }
                            for rank, hit in enumerate(hits, 1)
                        ],
                        ensure_ascii=False,
                    )
                )
            elif not hits:
                print("No results.")
            else:
                for rank, hit in enumerate(hits, 1):
                    context = hit.structural_path or hit.headings
                    pages = ",".join(map(str, hit.page_numbers)) or "n.a."
                    print(
                        f"{rank}. similarity={hit.cosine_similarity:.6f} "
                        f"rerank={hit.rerank_score if hit.rerank_score is not None else 'disabled'} "
                        f"chunk={hit.chunk_id} document={hit.document_id[:12]} "
                        f"file={hit.original_filename} universe={hit.universe} "
                        f"source={hit.source_type} path={context} pages={pages}\n{hit.text}\n"
                    )
            log.info(
                "Search universe=%s limit=%s hits=%d elapsed_seconds=%.3f",
                args.universe,
                args.limit or config.search_default_limit,
                len(hits),
                time.monotonic() - started,
            )
            return 0
        if args.command == "list-universes":
            summaries = _index_store(config).list_universes()
            if args.as_json:
                print(
                    json.dumps(
                        [
                            {
                                "universe": item.universe,
                                "documents": item.documents,
                                "chunks": item.chunks,
                            }
                            for item in summaries
                        ],
                        ensure_ascii=False,
                    )
                )
            elif summaries:
                print(f"{'UNIVERSE':<32} {'DOCUMENTS':>10} {'CHUNKS':>10}")
                for item in summaries:
                    print(f"{item.universe:<32} {item.documents:>10} {item.chunks:>10}")
            else:
                print("No indexed universes.")
            return 0
        pipeline = _pipeline(
            config,
            with_indexing=(
                config.max_stage == MaxStage.INDEXED
                and args.command in {"watch", "process", "resume", "retry-errors"}
            ),
        )
        if args.command == "set-universe":
            old, new = pipeline.set_universe(args.document_directory, args.universe)
            print(f"Universe updated: {old} \N{RIGHTWARDS ARROW} {new}")
            return 0
        if args.command == "resume":
            return 0 if pipeline.resume(args.document_directory) else 1
        if args.command == "retry-errors":
            return 0 if pipeline.retry_errors() else 1
        if args.command == "process":
            return 0 if pipeline.process(args.file) else 1
        while True:
            pipeline.cycle()
            time.sleep(config.poll_interval_seconds)
    except KeyboardInterrupt:
        return 130
    except Exception:
        log.exception("MyPyRag failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
