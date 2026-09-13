from contextlib import nullcontext

from psycopg import sql

from mypyrag.config import Config
from mypyrag.database import MigrationManager


class FakeCursor:
    def __init__(self):
        self.queries = []
        self._one = iter(
            [
                {"installed_version": "0.8.1"},
                {"present": True},
                {"table_name": "mypyrag.schema_migrations"},
            ]
        )

    def execute(self, query, parameters=None):
        rendered = query.as_string(None) if isinstance(query, sql.Composable) else query
        self.queries.append((rendered, parameters))

    def fetchone(self):
        return next(self._one)

    def fetchall(self):
        return [{"version": 1}, {"version": 2}]


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return nullcontext(self._cursor)


class FakeDatabase:
    def __init__(self, config, cursor):
        self.config = config
        self._cursor = cursor

    def connect(self):
        return nullcontext(FakeConnection(self._cursor))


def test_migrate_does_not_request_create_for_existing_catalog(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("MYPYRAG_"):
            monkeypatch.delenv(key)
    config = Config.load(tmp_path)
    cursor = FakeCursor()
    database = FakeDatabase(config, cursor)
    assert MigrationManager(database).migrate() == []
    statements = [query for query, _parameters in cursor.queries]
    assert not any(query.startswith("CREATE SCHEMA") for query in statements)
    assert not any(query.startswith("CREATE TABLE") for query in statements)
