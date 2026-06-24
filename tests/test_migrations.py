from __future__ import annotations

import sqlite3
from pathlib import Path

from pkm_brain.db import connection, init_db
from pkm_brain.migrations import run_migrations


EXPECTED_MIGRATIONS = list(range(1, 17))


def test_fresh_db_applies_registered_migrations(tmp_path: Path) -> None:
    db_path = tmp_path / "brain.sqlite"

    init_db(db_path)

    with connection(db_path) as conn:
        versions = [
            row["version"]
            for row in conn.execute("SELECT version FROM schema_migrations")
        ]
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(documents)")}
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(documents)")}
        lineage_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(context_lineage_events)")
        }
        retrieval_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(retrieval_events)")
        }
        fact_columns = {row["name"] for row in conn.execute("PRAGMA table_info(facts)")}
        question_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(open_questions)")
        }
        wiki_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(wiki_pages)")
        }
        item_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(wiki_change_items)")
        }
        snapshot_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(wiki_page_snapshots)")
        }
        action_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(cos_actions)")
        }
        policy_count = conn.execute("SELECT COUNT(*) FROM cos_policy").fetchone()[0]
        contract_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(page_contracts)")
        }
        synthesis_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(wiki_page_syntheses)")
        }
        retrieval_fts_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(retrieval_fts)")
        }

    assert versions == EXPECTED_MIGRATIONS
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
    assert {"target_type", "target_id", "event_type", "metadata"}.issubset(
        lineage_columns
    )
    assert "citation_snapshots" in retrieval_columns
    assert "cited_chunk_ids" not in retrieval_columns
    assert {
        "statement",
        "entity_key",
        "status",
        "metadata",
        "source_spans",
        "truth_confidence",
        "routing_confidence",
        "extraction_confidence",
    }.issubset(fact_columns)
    assert {
        "question",
        "fact_ids",
        "options",
        "status",
        "action_id",
        "recommended_action",
        "auto_resolve_after",
    }.issubset(question_columns)
    assert {"managed", "fact_ids"}.issubset(wiki_columns)
    assert "status" in item_columns
    assert {
        "page_path",
        "before_markdown",
        "after_markdown",
        "reason",
        "metadata",
    }.issubset(snapshot_columns)
    assert {"action_type", "policy_version", "applied_state_hash"}.issubset(
        action_columns
    )
    assert policy_count >= 1
    assert {"page_hint", "page_scope", "version", "status"}.issubset(contract_columns)
    assert {"page_hint", "synthesis_markdown", "fact_hash", "stale"}.issubset(
        synthesis_columns
    )
    assert {"kind", "target_id", "title", "text"}.issubset(retrieval_fts_columns)


def test_migrations_rerun_is_noop(tmp_path: Path) -> None:
    db_path = tmp_path / "brain.sqlite"
    init_db(db_path)
    init_db(db_path)

    with connection(db_path) as conn:
        versions = [
            row["version"]
            for row in conn.execute("SELECT version FROM schema_migrations")
        ]

    assert versions == EXPECTED_MIGRATIONS


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
        versions = [
            row["version"]
            for row in conn.execute("SELECT version FROM schema_migrations")
        ]
        row = conn.execute(
            "SELECT origin_node_id, logical_source_key FROM documents WHERE id = 'doc_legacy'"
        ).fetchone()
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(documents)")}

    assert versions == EXPECTED_MIGRATIONS
    assert row["origin_node_id"] == "<local>"
    assert row["logical_source_key"] == "/tmp/legacy.md"
    assert "idx_documents_origin_logical" in indexes


def test_documents_sensitivity_column_migration_drops_column_and_preserves_rows() -> (
    None
):
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
    row = conn.execute(
        "SELECT id, title, status FROM documents WHERE id = 'doc_sensitive'"
    ).fetchone()
    versions = [
        row["version"] for row in conn.execute("SELECT version FROM schema_migrations")
    ]

    assert "sensitivity" not in columns
    assert dict(row) == {
        "id": "doc_sensitive",
        "title": "Legacy Sensitive",
        "status": "active",
    }
    assert versions == EXPECTED_MIGRATIONS


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

    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(retrieval_events)")
    }
    versions = [
        row["version"] for row in conn.execute("SELECT version FROM schema_migrations")
    ]
    count = conn.execute("SELECT COUNT(*) FROM retrieval_events").fetchone()[0]

    assert versions == EXPECTED_MIGRATIONS
    assert "citation_snapshots" in columns
    assert "cited_chunk_ids" not in columns
    assert count == 0


def test_cos_migrations_backfill_legacy_facts_questions_and_shared_fts() -> None:
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
        [(version,) for version in range(1, 9)],
    )
    conn.execute(
        """
        CREATE TABLE facts (
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
        INSERT INTO facts(
          id, statement, entity_key, page_hint, section_hint, source_ids,
          observed_at, confidence, status, metadata, created_at
        ) VALUES (
          'fact_legacy', 'Legacy fact statement', 'concept:test:summary',
          'concepts/test.md', 'Summary', '["document:doc_legacy"]',
          '2026-05-20T00:00:00+00:00', 0.7, 'active', '{}',
          '2026-05-20T00:00:00+00:00'
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE open_questions (
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
        INSERT INTO open_questions(
          id, kind, page_hint, fact_ids, question, status, created_at
        ) VALUES (
          'question_legacy', 'conflict', 'concepts/test.md',
          '["fact_legacy"]', 'Which fact is correct?', 'open',
          '2026-05-20T00:00:00+00:00'
        )
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE chunk_fts USING fts5(
          chunk_id UNINDEXED,
          title,
          text,
          heading_path,
          project,
          tags
        )
        """
    )
    conn.execute(
        """
        INSERT INTO chunk_fts(chunk_id, title, text, heading_path, project, tags)
        VALUES ('chunk_legacy', 'Legacy Chunk', 'legacy chunk text', '', '', '[]')
        """
    )

    run_migrations(conn)

    fact = conn.execute("SELECT * FROM facts WHERE id = 'fact_legacy'").fetchone()
    question = conn.execute(
        "SELECT * FROM open_questions WHERE id = 'question_legacy'"
    ).fetchone()
    retrieval_rows = [
        dict(row)
        for row in conn.execute(
            "SELECT kind, target_id, title, text FROM retrieval_fts ORDER BY kind, target_id"
        )
    ]
    versions = [
        row["version"] for row in conn.execute("SELECT version FROM schema_migrations")
    ]

    assert versions == EXPECTED_MIGRATIONS
    assert fact["source_spans"] == "[]"
    assert fact["extraction_method"] == "legacy"
    assert fact["truth_confidence"] == 0.7
    assert question["action_id"] is None
    assert question["recommended_action"] == "{}"
    assert {"kind": "chunk", "target_id": "chunk_legacy", "title": "Legacy Chunk", "text": "legacy chunk text"} in retrieval_rows
    assert {"kind": "fact", "target_id": "fact_legacy", "title": "concepts/test.md", "text": "Legacy fact statement"} in retrieval_rows


def test_wiki_change_item_status_migration_defaults_existing_items_to_pending() -> None:
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
        [(1,), (2,), (3,), (4,), (5,), (6,)],
    )
    conn.execute(
        """
        CREATE TABLE wiki_change_batches (
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
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE wiki_change_items (
          id TEXT PRIMARY KEY,
          batch_id TEXT NOT NULL,
          order_index INTEGER NOT NULL,
          target_path TEXT NOT NULL,
          operation TEXT NOT NULL,
          section_name TEXT,
          proposed_markdown TEXT NOT NULL,
          rationale TEXT NOT NULL,
          source_ids TEXT NOT NULL DEFAULT '[]',
          confidence REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO wiki_change_batches(
          id, title, rationale, author, source, status, confidence, source_ids, created_at
        ) VALUES (
          'batch_legacy', 'Legacy', 'Legacy rationale', 'agent', 'test',
          'proposed', 0.8, '[]', '2026-05-20T00:00:00+00:00'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO wiki_change_items(
          id, batch_id, order_index, target_path, operation, proposed_markdown,
          rationale, source_ids, confidence
        ) VALUES (
          'item_legacy', 'batch_legacy', 0, 'concepts/test.md', 'create_page',
          '# Test', 'Create page', '[]', 0.8
        )
        """
    )

    run_migrations(conn)

    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(wiki_change_items)")
    }
    row = conn.execute(
        "SELECT id, status FROM wiki_change_items WHERE id = 'item_legacy'"
    ).fetchone()
    indexes = {
        row["name"] for row in conn.execute("PRAGMA index_list(wiki_change_items)")
    }
    versions = [
        row["version"] for row in conn.execute("SELECT version FROM schema_migrations")
    ]

    assert "status" in columns
    assert dict(row) == {"id": "item_legacy", "status": "pending"}
    assert "idx_wiki_change_items_status_target" in indexes
    assert "idx_wiki_change_items_batch_status" in indexes
    assert versions == EXPECTED_MIGRATIONS


def test_init_db_preflights_legacy_wiki_change_items_before_schema_indexes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "brain.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE wiki_change_batches (
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
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE wiki_change_items (
              id TEXT PRIMARY KEY,
              batch_id TEXT NOT NULL,
              order_index INTEGER NOT NULL,
              target_path TEXT NOT NULL,
              operation TEXT NOT NULL,
              section_name TEXT,
              proposed_markdown TEXT NOT NULL,
              rationale TEXT NOT NULL,
              source_ids TEXT NOT NULL DEFAULT '[]',
              confidence REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO wiki_change_batches(
              id, title, rationale, author, source, status, confidence, source_ids, created_at
            ) VALUES (
              'batch_legacy', 'Legacy', 'Legacy rationale', 'agent', 'test',
              'proposed', 0.8, '[]', '2026-05-20T00:00:00+00:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO wiki_change_items(
              id, batch_id, order_index, target_path, operation, proposed_markdown,
              rationale, source_ids, confidence
            ) VALUES (
              'item_legacy', 'batch_legacy', 0, 'concepts/test.md', 'create_page',
              '# Test', 'Create page', '[]', 0.8
            )
            """
        )

    init_db(db_path)

    with connection(db_path) as conn:
        row = conn.execute(
            "SELECT id, status FROM wiki_change_items WHERE id = 'item_legacy'"
        ).fetchone()
        indexes = {
            row["name"] for row in conn.execute("PRAGMA index_list(wiki_change_items)")
        }
        versions = [
            row["version"]
            for row in conn.execute("SELECT version FROM schema_migrations")
        ]

    assert dict(row) == {"id": "item_legacy", "status": "pending"}
    assert "idx_wiki_change_items_status_target" in indexes
    assert versions == EXPECTED_MIGRATIONS


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

    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    versions = [
        row["version"] for row in conn.execute("SELECT version FROM schema_migrations")
    ]

    assert "should_rollback" not in tables
    assert versions == []
