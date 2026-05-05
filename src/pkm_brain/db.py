from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator


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
  created_at TEXT NOT NULL,
  ingested_at TEXT NOT NULL,
  project TEXT,
  tags TEXT NOT NULL DEFAULT '[]',
  sensitivity TEXT NOT NULL DEFAULT 'normal',
  version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'active'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_content_hash
ON documents(content_hash);

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
  last_seen_at TEXT
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

CREATE TABLE IF NOT EXISTS retrieval_events (
  id TEXT PRIMARY KEY,
  query TEXT NOT NULL,
  timestamp TEXT NOT NULL,
  caller TEXT NOT NULL,
  returned_chunk_ids TEXT NOT NULL DEFAULT '[]',
  selected_chunk_ids TEXT NOT NULL DEFAULT '[]',
  cited_chunk_ids TEXT NOT NULL DEFAULT '[]',
  debug TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS agent_sessions (
  id TEXT PRIMARY KEY,
  summary TEXT NOT NULL,
  files_touched TEXT NOT NULL DEFAULT '[]',
  commands_run TEXT NOT NULL DEFAULT '[]',
  outcome TEXT NOT NULL,
  unresolved_issues TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
"""


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    return json.loads(value)


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
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
        conn.executescript(SCHEMA)


def rows(conn: sqlite3.Connection, query: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return list(conn.execute(query, tuple(params)))
