from __future__ import annotations

import sqlite3
from pathlib import Path

from pkm_brain.db import connection, init_db
from pkm_brain.migrations import run_migrations


def test_fresh_db_applies_registered_migrations(tmp_path: Path) -> None:
    db_path = tmp_path / "brain.sqlite"

    init_db(db_path)

    with connection(db_path) as conn:
        versions = [row["version"] for row in conn.execute("SELECT version FROM schema_migrations")]
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(documents)")}
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(documents)")}

    assert versions == [1, 2]
    assert "origin_node_id" in columns
    assert "logical_source_key" in columns
    assert "idx_documents_origin_logical" in indexes


def test_migrations_rerun_is_noop(tmp_path: Path) -> None:
    db_path = tmp_path / "brain.sqlite"
    init_db(db_path)
    init_db(db_path)

    with connection(db_path) as conn:
        versions = [row["version"] for row in conn.execute("SELECT version FROM schema_migrations")]

    assert versions == [1, 2]


def test_failed_migration_rolls_back_that_migration() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE documents(id TEXT PRIMARY KEY)")

    def failing_migration(inner: sqlite3.Connection) -> None:
        inner.execute("CREATE TABLE should_rollback(id TEXT PRIMARY KEY)")
        raise ValueError("boom")

    try:
        run_migrations(conn, [(99, "failing", failing_migration)])
    except RuntimeError:
        pass

    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    versions = [row["version"] for row in conn.execute("SELECT version FROM schema_migrations")]

    assert "should_rollback" not in tables
    assert versions == []
