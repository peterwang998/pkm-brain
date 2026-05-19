from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable

from .util import now_iso


MigrationFn = Callable[[sqlite3.Connection], None]
Migration = tuple[int, str, MigrationFn]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migration_001_add_origin_identity(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "documents", "origin_node_id", "TEXT")
    _ensure_column(conn, "documents", "logical_source_key", "TEXT")
    conn.execute(
        """
        UPDATE documents
        SET origin_node_id = COALESCE(origin_node_id, '<local>'),
            logical_source_key = COALESCE(logical_source_key, source_path)
        WHERE origin_node_id IS NULL OR logical_source_key IS NULL
        """
    )
    conn.execute("DROP INDEX IF EXISTS idx_documents_content_hash")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_content_hash ON documents(content_hash)")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_documents_origin_logical
        ON documents(origin_node_id, logical_source_key)
        """
    )


def _migration_002_create_sync_runs(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_runs (
          id TEXT PRIMARY KEY,
          peer_node_id TEXT NOT NULL,
          direction TEXT NOT NULL,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          status TEXT NOT NULL,
          files_pulled INTEGER NOT NULL DEFAULT 0,
          files_pushed INTEGER NOT NULL DEFAULT 0,
          bytes_pulled INTEGER NOT NULL DEFAULT 0,
          bytes_pushed INTEGER NOT NULL DEFAULT 0,
          primary_ingest_run_id TEXT,
          remote_ingest_status TEXT,
          errors TEXT NOT NULL DEFAULT '[]'
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sync_runs_peer_status
        ON sync_runs(peer_node_id, status, finished_at)
        """
    )


MIGRATIONS: list[Migration] = [
    (1, "add_origin_identity", _migration_001_add_origin_identity),
    (2, "create_sync_runs", _migration_002_create_sync_runs),
]


def run_migrations(conn: sqlite3.Connection, migrations: Iterable[Migration] | None = None) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version INTEGER PRIMARY KEY,
          applied_at TEXT NOT NULL
        )
        """
    )
    applied = {row["version"] if isinstance(row, sqlite3.Row) else row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    for version, name, fn in sorted(migrations or MIGRATIONS, key=lambda migration: migration[0]):
        if version in applied:
            continue
        savepoint = f"migration_{version}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            fn(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, now_iso()),
            )
            conn.execute(f"RELEASE {savepoint}")
        except Exception as exc:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
            raise RuntimeError(f"migration {version} ({name}) failed") from exc
