"""Versioned migrations and short, document-scoped PostgreSQL transactions."""

from __future__ import annotations

import importlib.resources
import logging
from datetime import UTC, datetime
from typing import Any

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from mypyrag.config import Config
from mypyrag.indexing import IndexedChunk, SearchHit
from mypyrag.model import Manifest

SCHEMA_VERSION = 1
log = logging.getLogger(__name__)


class PostgresDatabase:
    def __init__(self, config: Config) -> None:
        self.config = config

    def connect(self, *, autocommit: bool = False) -> psycopg.Connection[Any]:
        connection = psycopg.connect(
            host=self.config.postgres_host,
            port=self.config.postgres_port,
            dbname=self.config.postgres_database,
            user=self.config.postgres_user,
            password=self.config.postgres_password,
            sslmode=self.config.postgres_sslmode,
            connect_timeout=self.config.postgres_connect_timeout_seconds,
            autocommit=autocommit,
            row_factory=dict_row,
        )
        register_vector(connection)
        return connection

    def logical_target(self) -> str:
        return (
            f"postgresql://{self.config.postgres_host}:{self.config.postgres_port}/"
            f"{self.config.postgres_database}/{self.config.postgres_schema}"
        )


class MigrationManager:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def migrate(self) -> list[int]:
        schema = sql.Identifier(self.database.config.postgres_schema)
        applied: list[int] = []
        with self.database.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("mypyrag-migrations",))
            cursor.execute(
                "SELECT installed_version FROM pg_available_extensions WHERE name = 'vector'"
            )
            row = cursor.fetchone()
            if row is None or row["installed_version"] is None:
                raise RuntimeError(
                    "pgvector extension is not active in the target database; "
                    "infrastructure setup is required"
                )
            cursor.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(schema))
            cursor.execute(
                sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {}.schema_migrations ("
                    "version integer PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
                ).format(schema)
            )
            cursor.execute(sql.SQL("SELECT version FROM {}.schema_migrations").format(schema))
            existing = {item["version"] for item in cursor.fetchall()}
            migrations = importlib.resources.files("mypyrag.migrations")
            for entry in sorted(migrations.iterdir(), key=lambda path: path.name):
                if not entry.name.endswith(".sql"):
                    continue
                version = int(entry.name.split("_", 1)[0])
                if version in existing:
                    continue
                template = entry.read_text(encoding="utf-8")
                cursor.execute(sql.SQL(template).format(schema=schema))
                cursor.execute(
                    sql.SQL("INSERT INTO {}.schema_migrations (version) VALUES (%s)").format(
                        schema
                    ),
                    (version,),
                )
                applied.append(version)
        return applied

    def status(self) -> dict[str, Any]:
        schema_name = self.database.config.postgres_schema
        schema = sql.Identifier(schema_name)
        with self.database.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            extension = cursor.fetchone()
            cursor.execute(
                "SELECT to_regclass(%s) AS table_name", (f"{schema_name}.schema_migrations",)
            )
            migration_table = cursor.fetchone()
            versions: list[int] = []
            if migration_table and migration_table["table_name"] is not None:
                cursor.execute(
                    sql.SQL("SELECT version FROM {}.schema_migrations ORDER BY version").format(
                        schema
                    )
                )
                versions = [item["version"] for item in cursor.fetchall()]
            cursor.execute(
                "SELECT format_type(a.atttypid, a.atttypmod) AS type "
                "FROM pg_attribute a WHERE a.attrelid = to_regclass(%s) "
                "AND a.attname = 'embedding' AND NOT a.attisdropped",
                (f"{schema_name}.chunks",),
            )
            vector_column = cursor.fetchone()
            cursor.execute(
                "SELECT indexdef FROM pg_indexes WHERE schemaname=%s AND tablename='chunks' "
                "AND indexdef LIKE '%%USING hnsw%%vector_cosine_ops%%'",
                (schema_name,),
            )
            hnsw = cursor.fetchone()
        return {
            "vector_version": extension["extversion"] if extension else None,
            "schema_versions": versions,
            "vector_column": vector_column["type"] if vector_column else None,
            "hnsw_cosine_index": hnsw is not None,
        }

    def require_current(self) -> dict[str, Any]:
        status = self.status()
        if (
            status["schema_versions"] != list(range(1, SCHEMA_VERSION + 1))
            or status["vector_column"] != "vector(768)"
            or not status["hnsw_cosine_index"]
        ):
            raise RuntimeError("Database schema is not current; run 'mypyrag db migrate'")
        return status


class PostgresIndexStore:
    def __init__(self, database: PostgresDatabase) -> None:
        self.database = database

    def logical_target(self) -> str:
        return self.database.logical_target()

    def replace_document(
        self, manifest: Manifest, universe: str, chunks: list[IndexedChunk], model_digest: str
    ) -> int:
        schema = sql.Identifier(self.database.config.postgres_schema)
        indexed_at = datetime.now(UTC)
        fingerprint = str((manifest.chunking or {}).get("fingerprint", ""))
        if not fingerprint:
            raise ValueError("Manifest has no chunking fingerprint")
        log.info(
            "PostgreSQL replace begin document_id=%s universe=%s chunks=%d",
            manifest.document_id[:12],
            universe,
            len(chunks),
        )
        deleted = 0
        with self.database.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "INSERT INTO {}.documents (document_id, original_filename, source_relative_path, "
                    "source_sha256, document_type, universe, docling_version, chunking_fingerprint, "
                    "chunk_count, embedding_model, embedding_model_digest, embedding_vector_size, "
                    "indexed_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (document_id) DO UPDATE SET original_filename=EXCLUDED.original_filename, "
                    "source_relative_path=EXCLUDED.source_relative_path, source_sha256=EXCLUDED.source_sha256, "
                    "document_type=EXCLUDED.document_type, universe=EXCLUDED.universe, "
                    "docling_version=EXCLUDED.docling_version, chunking_fingerprint=EXCLUDED.chunking_fingerprint, "
                    "chunk_count=EXCLUDED.chunk_count, embedding_model=EXCLUDED.embedding_model, "
                    "embedding_model_digest=EXCLUDED.embedding_model_digest, "
                    "embedding_vector_size=EXCLUDED.embedding_vector_size, indexed_at=EXCLUDED.indexed_at, "
                    "updated_at=EXCLUDED.updated_at"
                ).format(schema),
                (
                    manifest.document_id,
                    manifest.original_filename,
                    manifest.source_relative_path,
                    manifest.source_sha256,
                    manifest.source_type,
                    universe,
                    manifest.docling_version,
                    fingerprint,
                    len(chunks),
                    self.database.config.embedding_model,
                    model_digest,
                    self.database.config.embedding_vector_size,
                    indexed_at,
                    indexed_at,
                ),
            )
            cursor.execute(
                sql.SQL("DELETE FROM {}.chunks WHERE document_id = %s").format(schema),
                (manifest.document_id,),
            )
            deleted = cursor.rowcount
            insert = sql.SQL(
                "INSERT INTO {}.chunks (chunk_id,document_id,chunk_index,universe,source_type,text,"
                "embedding_text,text_sha256,embedding_text_sha256,embedding_input_sha256,"
                "original_filename,document_type,headings,structural_path,page_numbers,"
                "docling_references,table_metadata,chunking_metadata,embedding_model,"
                "embedding_model_digest,embedding_vector_size,embedding,created_at,indexed_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            ).format(schema)
            rows = []
            for item in chunks:
                artifact = item.artifact
                rows.append(
                    (
                        artifact.chunk_id,
                        artifact.document_id,
                        artifact.chunk_index,
                        universe,
                        artifact.source_type,
                        artifact.text,
                        artifact.embedding_text,
                        artifact.text_sha256,
                        artifact.embedding_text_sha256,
                        artifact.embedding_input_sha256,
                        artifact.original_filename,
                        artifact.document_type,
                        Jsonb(artifact.headings),
                        Jsonb(artifact.structural_path),
                        Jsonb(artifact.page_numbers),
                        Jsonb(artifact.docling_references),
                        Jsonb(artifact.table_metadata) if artifact.table_metadata else None,
                        Jsonb(artifact.chunking_metadata),
                        self.database.config.embedding_model,
                        model_digest,
                        self.database.config.embedding_vector_size,
                        Vector(item.embedding),
                        artifact.created_at,
                        indexed_at,
                    )
                )
            cursor.executemany(insert, rows)
            cursor.execute(
                sql.SQL(
                    "SELECT count(*) AS count, count(DISTINCT chunk_id) AS unique_ids, "
                    "bool_and(universe=%s) AS same_universe FROM {}.chunks WHERE document_id=%s"
                ).format(schema),
                (universe, manifest.document_id),
            )
            check = cursor.fetchone()
            if (
                check is None
                or check["count"] != len(chunks)
                or check["unique_ids"] != len(chunks)
                or not check["same_universe"]
            ):
                raise RuntimeError("PostgreSQL chunk count/identity/universe verification failed")
        log.info(
            "PostgreSQL replace committed document_id=%s deleted=%d inserted=%d verified=%d",
            manifest.document_id[:12],
            deleted,
            len(chunks),
            len(chunks),
        )
        return len(chunks)

    def search(self, vector: list[float], universe: str, limit: int) -> list[SearchHit]:
        schema = sql.Identifier(self.database.config.postgres_schema)
        with self.database.connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "SELECT chunk_id::text, document_id, original_filename, universe, source_type, "
                    "text, headings, structural_path, page_numbers, embedding <=> %s AS cosine_distance "
                    "FROM {}.chunks WHERE universe = %s ORDER BY embedding <=> %s LIMIT %s"
                ).format(schema),
                (Vector(vector), universe, Vector(vector), limit),
            )
            return [SearchHit(**row) for row in cursor.fetchall()]
