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
        lineage_columns = {row["name"] for row in conn.execute("PRAGMA table_info(context_lineage_events)")}
        retrieval_columns = {row["name"] for row in conn.execute("PRAGMA table_info(retrieval_events)")}

    assert versions == [1, 2, 3, 4, 5]
    assert {
        "id",
        "source_type",
        "title",
        "source_path",
        "raw_path",
        "content_hash",
        "origin_node_id",
        "logical_source_key",
        "created_at",
        "ingested_at",
        "project",
        "tags",
        "version",
        "status",
    }.issubset(columns)
    assert "idx_documents_origin_logical" in indexes
    assert {"target_type", "target_id", "event_type", "metadata"}.issubset(lineage_columns)
    assert "citation_snapshots" in retrieval_columns
    assert "cited_chunk_ids" not in retrieval_columns


def test_migrations_rerun_is_noop(tmp_path: Path) -> None:
    db_path = tmp_path / "brain.sqlite"
    init_db(db_path)
    init_db(db_path)

    with connection(db_path) as conn:
        versions = [row["version"] for row in conn.execute("SELECT version FROM schema_migrations")]

    assert versions == [1, 2, 3, 4, 5]


def test_init_db_migrates_pre_sync_documents_table(tmp_path: Path) -> None:
    db_path = tmp_path / "brain.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE documents (
              id TEXT PRIMARY KEY,
              source_type TEXT NOT NULL,
              title TEXT NOT NULL,
              source_path TEXT NOT NULL,
              raw_path TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              created_at TEXT NOT NULL,
              ingested_at TEXT NOT NULL,
              project TEXT,
              tags TEXT NOT NULL DEFAULT '[]',
              version INTEGER NOT NULL DEFAULT 1,
              status TEXT NOT NULL DEFAULT 'active'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO documents(
              id, source_type, title, source_path, raw_path, content_hash,
              created_at, ingested_at, tags
            ) VALUES (
              'doc_legacy', 'note', 'Legacy', '/tmp/legacy.md',
              '/tmp/raw.md', 'abc', '2026-05-18T00:00:00Z',
              '2026-05-18T00:00:00Z', '[]'
            )
            """
        )

    init_db(db_path)

    with connection(db_path) as conn:
        versions = [row["version"] for row in conn.execute("SELECT version FROM schema_migrations")]
        row = conn.execute(
            "SELECT origin_node_id, logical_source_key FROM documents WHERE id = 'doc_legacy'"
        ).fetchone()
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(documents)")}

    assert versions == [1, 2, 3, 4, 5]
    assert row["origin_node_id"] == "<local>"
    assert row["logical_source_key"] == "/tmp/legacy.md"
    assert "idx_documents_origin_logical" in indexes


def test_documents_sensitivity_column_migration_drops_column_and_preserves_rows() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE schema_migrations (
          version INTEGER PRIMARY KEY,
          applied_at TEXT NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, '2026-05-20T00:00:00+00:00')",
        [(1,), (2,), (3,), (4,)],
    )
    conn.execute(
        """
        CREATE TABLE documents (
          id TEXT PRIMARY KEY,
          source_type TEXT NOT NULL,
          title TEXT NOT NULL,
          source_path TEXT NOT NULL,
          raw_path TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          origin_node_id TEXT,
          logical_source_key TEXT,
          created_at TEXT NOT NULL,
          ingested_at TEXT NOT NULL,
          project TEXT,
          tags TEXT NOT NULL DEFAULT '[]',
          sensitivity TEXT NOT NULL DEFAULT 'normal',
          version INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL DEFAULT 'active'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO documents(
          id, source_type, title, source_path, raw_path, content_hash,
          origin_node_id, logical_source_key, created_at, ingested_at,
          project, tags, sensitivity, version, status
        ) VALUES (
          'doc_sensitive', 'markdown_note', 'Legacy Sensitive', '/tmp/source.md',
          '/tmp/raw.md', 'abc', '<local>', '/tmp/source.md',
          '2026-05-20T00:00:00+00:00', '2026-05-20T00:00:00+00:00',
          NULL, '[]', 'normal', 1, 'active'
        )
        """
    )

    run_migrations(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(documents)")}
    row = conn.execute("SELECT id, title, status FROM documents WHERE id = 'doc_sensitive'").fetchone()
    versions = [row["version"] for row in conn.execute("SELECT version FROM schema_migrations")]

    assert "sensitivity" not in columns
    assert dict(row) == {"id": "doc_sensitive", "title": "Legacy Sensitive", "status": "active"}
    assert versions == [1, 2, 3, 4, 5]


def test_retrieval_events_migration_drops_legacy_cited_chunk_ids() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE schema_migrations (
          version INTEGER PRIMARY KEY,
          applied_at TEXT NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, '2026-05-20T00:00:00+00:00')",
        [(1,), (2,), (3,)],
    )
    conn.execute(
        """
        CREATE TABLE retrieval_events (
          id TEXT PRIMARY KEY,
          query TEXT NOT NULL,
          timestamp TEXT NOT NULL,
          caller TEXT NOT NULL,
          returned_chunk_ids TEXT NOT NULL DEFAULT '[]',
          selected_chunk_ids TEXT NOT NULL DEFAULT '[]',
          cited_chunk_ids TEXT NOT NULL DEFAULT '[]',
          debug TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO retrieval_events(
          id, query, timestamp, caller, returned_chunk_ids, selected_chunk_ids, cited_chunk_ids, debug
        ) VALUES ('retrieval_legacy', 'q', '2026-05-20T00:00:00+00:00', 'test', '[]', '[]', '["chunk_old"]', '{}')
        """
    )

    run_migrations(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(retrieval_events)")}
    versions = [row["version"] for row in conn.execute("SELECT version FROM schema_migrations")]
    count = conn.execute("SELECT COUNT(*) FROM retrieval_events").fetchone()[0]

    assert versions == [1, 2, 3, 4, 5]
    assert "citation_snapshots" in columns
    assert "cited_chunk_ids" not in columns
    assert count == 0


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
