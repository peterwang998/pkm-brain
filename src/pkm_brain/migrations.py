from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable

from .util import now_iso


MigrationFn = Callable[[sqlite3.Connection], None]
Migration = tuple[int, str, MigrationFn]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row["name"] if isinstance(row, sqlite3.Row) else row[1]
        for row in conn.execute(f"PRAGMA table_info({table})")
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    )


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_content_hash ON documents(content_hash)"
    )
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


def _migration_003_create_context_lineage_events(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS context_lineage_events (
          id TEXT PRIMARY KEY,
          target_type TEXT NOT NULL,
          target_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          retrieval_event_id TEXT,
          agent_session_id TEXT,
          query TEXT,
          weight REAL NOT NULL DEFAULT 0.0,
          metadata TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_context_lineage_target
        ON context_lineage_events(target_type, target_id, event_type, created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_context_lineage_retrieval
        ON context_lineage_events(retrieval_event_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_context_lineage_agent_session
        ON context_lineage_events(agent_session_id, event_type)
        """
    )


def _migration_004_recreate_retrieval_events_with_snapshots(
    conn: sqlite3.Connection,
) -> None:
    conn.execute("DROP TABLE IF EXISTS retrieval_events")
    conn.execute(
        """
        CREATE TABLE retrieval_events (
          id TEXT PRIMARY KEY,
          query TEXT NOT NULL,
          timestamp TEXT NOT NULL,
          caller TEXT NOT NULL,
          returned_chunk_ids TEXT NOT NULL DEFAULT '[]',
          selected_chunk_ids TEXT NOT NULL DEFAULT '[]',
          citation_snapshots TEXT NOT NULL DEFAULT '[]',
          debug TEXT NOT NULL DEFAULT '{}'
        )
        """
    )


def _migration_005_drop_documents_sensitivity(conn: sqlite3.Connection) -> None:
    if "sensitivity" in _table_columns(conn, "documents"):
        conn.execute("ALTER TABLE documents DROP COLUMN sensitivity")


def _migration_006_create_wiki_fact_curation(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS facts (
          id TEXT PRIMARY KEY,
          statement TEXT NOT NULL,
          entity_key TEXT NOT NULL,
          page_hint TEXT,
          section_hint TEXT,
          source_ids TEXT NOT NULL DEFAULT '[]',
          observed_at TEXT,
          confidence REAL NOT NULL,
          status TEXT NOT NULL,
          supersedes_id TEXT,
          conflict_group_id TEXT,
          confirmed_by_user INTEGER NOT NULL DEFAULT 0,
          metadata TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          last_seen_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_facts_entity_status
        ON facts(entity_key, status, observed_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_facts_page_status
        ON facts(page_hint, status, observed_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS open_questions (
          id TEXT PRIMARY KEY,
          kind TEXT NOT NULL,
          entity_key TEXT,
          page_hint TEXT,
          fact_ids TEXT NOT NULL DEFAULT '[]',
          question TEXT NOT NULL,
          options TEXT NOT NULL DEFAULT '[]',
          status TEXT NOT NULL,
          answer TEXT,
          context TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          answered_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_open_questions_status
        ON open_questions(status, created_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_curation_runs (
          id TEXT PRIMARY KEY,
          source_packet_id TEXT,
          group_by TEXT,
          status TEXT NOT NULL,
          summary TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wiki_curation_runs_packet
        ON wiki_curation_runs(source_packet_id, created_at)
        """
    )
    if _table_exists(conn, "wiki_pages"):
        _ensure_column(conn, "wiki_pages", "managed", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "wiki_pages", "fact_ids", "TEXT NOT NULL DEFAULT '[]'")


def _migration_007_add_wiki_change_item_status(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "wiki_change_items"):
        return
    _ensure_column(
        conn, "wiki_change_items", "status", "TEXT NOT NULL DEFAULT 'pending'"
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wiki_change_items_status_target
        ON wiki_change_items(status, target_path)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wiki_change_items_batch_status
        ON wiki_change_items(batch_id, status)
        """
    )


MIGRATIONS: list[Migration] = [
    (1, "add_origin_identity", _migration_001_add_origin_identity),
    (2, "create_sync_runs", _migration_002_create_sync_runs),
    (3, "create_context_lineage_events", _migration_003_create_context_lineage_events),
    (
        4,
        "recreate_retrieval_events_with_snapshots",
        _migration_004_recreate_retrieval_events_with_snapshots,
    ),
    (5, "drop_documents_sensitivity", _migration_005_drop_documents_sensitivity),
    (6, "create_wiki_fact_curation", _migration_006_create_wiki_fact_curation),
    (7, "add_wiki_change_item_status", _migration_007_add_wiki_change_item_status),
]


def run_migrations(
    conn: sqlite3.Connection, migrations: Iterable[Migration] | None = None
) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version INTEGER PRIMARY KEY,
          applied_at TEXT NOT NULL
        )
        """
    )
    applied = {
        row["version"] if isinstance(row, sqlite3.Row) else row[0]
        for row in conn.execute("SELECT version FROM schema_migrations")
    }
    for version, name, fn in sorted(
        migrations or MIGRATIONS, key=lambda migration: migration[0]
    ):
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
