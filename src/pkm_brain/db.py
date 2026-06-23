from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, TypeVar


SQLITE_BUSY_TIMEOUT_SECONDS = 1.0
SQLITE_BUSY_TIMEOUT_MS = int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)
SQLITE_LOCK_RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0, 4.0, 8.0)
T = TypeVar("T")

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS documents (
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
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'active'
);

CREATE INDEX IF NOT EXISTS idx_documents_content_hash
ON documents(content_hash);

CREATE INDEX IF NOT EXISTS idx_documents_origin_logical
ON documents(origin_node_id, logical_source_key);

CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  chunk_index INTEGER NOT NULL,
  corpus_type TEXT NOT NULL DEFAULT 'raw',
  text TEXT NOT NULL,
  heading_path TEXT,
  start_offset INTEGER,
  end_offset INTEGER,
  token_count INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_corpus_type ON chunks(corpus_type);

CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
  chunk_id UNINDEXED,
  title,
  text,
  heading_path,
  project,
  tags
);

CREATE TABLE IF NOT EXISTS entities (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  entity_type TEXT,
  source_ids TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relations (
  id TEXT PRIMARY KEY,
  subject_id TEXT NOT NULL,
  predicate TEXT NOT NULL,
  object_id TEXT NOT NULL,
  source_ids TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
  id TEXT PRIMARY KEY,
  memory_type TEXT NOT NULL,
  scope TEXT NOT NULL,
  content TEXT NOT NULL,
  confidence REAL NOT NULL,
  source_ids TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_seen_at TEXT,
  reviewed_at TEXT,
  review_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope);

CREATE TABLE IF NOT EXISTS wiki_pages (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  page_type TEXT NOT NULL,
  status TEXT NOT NULL,
  path TEXT NOT NULL,
  source_ids TEXT NOT NULL DEFAULT '[]',
  related TEXT NOT NULL DEFAULT '[]',
  tags TEXT NOT NULL DEFAULT '[]',
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS wiki_change_batches (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  rationale TEXT NOT NULL,
  author TEXT NOT NULL,
  source TEXT NOT NULL,
  status TEXT NOT NULL,
  confidence REAL NOT NULL,
  source_ids TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  reviewed_at TEXT,
  applied_at TEXT,
  error TEXT
);

CREATE INDEX IF NOT EXISTS idx_wiki_change_batches_status
ON wiki_change_batches(status, created_at);

CREATE TABLE IF NOT EXISTS wiki_change_items (
  id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES wiki_change_batches(id) ON DELETE CASCADE,
  order_index INTEGER NOT NULL,
  target_path TEXT NOT NULL,
  operation TEXT NOT NULL,
  section_name TEXT,
  proposed_markdown TEXT NOT NULL,
  rationale TEXT NOT NULL,
  source_ids TEXT NOT NULL DEFAULT '[]',
  confidence REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_wiki_change_items_batch_id
ON wiki_change_items(batch_id);

CREATE INDEX IF NOT EXISTS idx_wiki_change_items_status_target
ON wiki_change_items(status, target_path);

CREATE TABLE IF NOT EXISTS wiki_interviews (
  id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES wiki_change_batches(id) ON DELETE CASCADE,
  questions TEXT NOT NULL DEFAULT '[]',
  answers TEXT NOT NULL DEFAULT '[]',
  disposition TEXT NOT NULL,
  provider TEXT,
  model TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wiki_interviews_batch_id
ON wiki_interviews(batch_id);

CREATE TABLE IF NOT EXISTS ingestion_runs (
  id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  documents_discovered INTEGER NOT NULL DEFAULT 0,
  documents_changed INTEGER NOT NULL DEFAULT 0,
  documents_skipped INTEGER NOT NULL DEFAULT 0,
  chunks_created INTEGER NOT NULL DEFAULT 0,
  embeddings_created INTEGER NOT NULL DEFAULT 0,
  errors TEXT NOT NULL DEFAULT '[]',
  warnings TEXT NOT NULL DEFAULT '[]'
);

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
);

CREATE INDEX IF NOT EXISTS idx_sync_runs_peer_status
ON sync_runs(peer_node_id, status, finished_at);

CREATE TABLE IF NOT EXISTS automation_runs (
  id TEXT PRIMARY KEY,
  job_name TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '{}',
  error TEXT
);

CREATE INDEX IF NOT EXISTS idx_automation_runs_job_status
ON automation_runs(job_name, status, finished_at);

CREATE TABLE IF NOT EXISTS retrieval_events (
  id TEXT PRIMARY KEY,
  query TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  caller TEXT NOT NULL,
  returned_chunk_ids TEXT NOT NULL DEFAULT '[]',
  selected_chunk_ids TEXT NOT NULL DEFAULT '[]',
  citation_snapshots TEXT NOT NULL DEFAULT '[]',
  debug TEXT NOT NULL DEFAULT '{}'
);

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
);

CREATE INDEX IF NOT EXISTS idx_context_lineage_target
ON context_lineage_events(target_type, target_id, event_type, created_at);

CREATE INDEX IF NOT EXISTS idx_context_lineage_retrieval
ON context_lineage_events(retrieval_event_id);

CREATE INDEX IF NOT EXISTS idx_context_lineage_agent_session
ON context_lineage_events(agent_session_id, event_type);

CREATE TABLE IF NOT EXISTS agent_sessions (
  id TEXT PRIMARY KEY,
  summary TEXT NOT NULL,
  files_touched TEXT NOT NULL DEFAULT '[]',
  commands_run TEXT NOT NULL DEFAULT '[]',
  outcome TEXT NOT NULL,
  unresolved_issues TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capture_sources (
  id TEXT PRIMARY KEY,
  source_kind TEXT NOT NULL,
  agent TEXT NOT NULL,
  session_id TEXT NOT NULL,
  source_path TEXT NOT NULL,
  source_hash TEXT NOT NULL,
  source_mtime REAL,
  source_size INTEGER,
  captured_path TEXT,
  captured_at TEXT,
  status TEXT NOT NULL,
  error TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_capture_sources_agent_session
ON capture_sources(agent, session_id);
"""


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    return json.loads(value)


def is_sqlite_locked_error(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return True
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def retry_sqlite_lock(operation: Callable[[], T]) -> T:
    for delay in SQLITE_LOCK_RETRY_DELAYS_SECONDS:
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not is_sqlite_locked_error(exc):
                raise
            time.sleep(delay)
    # One final attempt lets the original sqlite3 error surface if the lock
    # outlives all configured backoff windows.
    return operation()


class RetryingConnection(sqlite3.Connection):
    """SQLite connection that backs off on transient writer lock contention.

    PKM Brain has several LaunchAgents that can write to the same local SQLite
    database. WAL plus SQLite's busy timeout handle normal contention; this
    retry layer covers short lock races that still surface as SQLITE_BUSY or
    SQLITE_LOCKED.
    """

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        return retry_sqlite_lock(
            lambda: super(RetryingConnection, self).execute(sql, parameters)
        )

    def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor:
        return retry_sqlite_lock(
            lambda: super(RetryingConnection, self).executemany(sql, parameters)
        )

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        return retry_sqlite_lock(
            lambda: super(RetryingConnection, self).executescript(sql_script)
        )

    def commit(self) -> None:
        return retry_sqlite_lock(lambda: super(RetryingConnection, self).commit())


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS, factory=RetryingConnection
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connection(db_path) as conn:
        # Existing pre-sync databases need these columns before SCHEMA creates
        # idx_documents_origin_logical.
        documents_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents'"
        ).fetchone()
        if documents_table:
            ensure_column(conn, "documents", "origin_node_id", "TEXT")
            ensure_column(conn, "documents", "logical_source_key", "TEXT")
        wiki_change_items_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='wiki_change_items'"
        ).fetchone()
        if wiki_change_items_table:
            ensure_column(
                conn, "wiki_change_items", "status", "TEXT NOT NULL DEFAULT 'pending'"
            )
        conn.executescript(SCHEMA)
        ensure_column(conn, "memories", "reviewed_at", "TEXT")
        ensure_column(conn, "memories", "review_reason", "TEXT")
        from .migrations import run_migrations

        run_migrations(conn)


def ensure_column(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def rows(
    conn: sqlite3.Connection, query: str, params: Iterable[Any] = ()
) -> list[sqlite3.Row]:
    return list(conn.execute(query, tuple(params)))
