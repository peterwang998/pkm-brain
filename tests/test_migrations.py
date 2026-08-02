from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pkm_brain.db import connection, init_db
from pkm_brain.migrations import MIGRATIONS, run_migrations


EXPECTED_MIGRATIONS = list(range(1, 30))
REVIEW_RESOLUTION_MIGRATION = next(
    migration for migration in MIGRATIONS if migration[0] == 27
)
FINAL_REVIEW_BACKFILL_MIGRATION = next(
    migration for migration in MIGRATIONS if migration[0] == 28
)
ENTITY_REVIEW_REKEY_MIGRATION = next(
    migration for migration in MIGRATIONS if migration[0] == 29
)


def schema_28_fact_review_identity(fact: dict[str, object]) -> dict[str, str]:
    """Reproduce the released schema-28 fact identity (before attribution)."""

    from pkm_brain.review_resolution import (
        FACT_TEMPORAL_FIELDS,
        canonical_payload,
        canonical_repeatable_rows,
        canonical_source_spans,
        decoded_json,
        digest,
        mapping,
        normalize_text,
        stable_strings,
        temporal_value_present,
    )

    statement = normalize_text(fact.get("statement"))
    source_ids = stable_strings(decoded_json(fact.get("source_ids"), []))
    source_spans = canonical_source_spans(decoded_json(fact.get("source_spans"), []))
    quote = normalize_text(fact.get("evidence_quote") or fact.get("quote"))
    temporal = {
        field: canonical_payload(fact.get(field))
        for field in FACT_TEMPORAL_FIELDS
        if temporal_value_present(fact.get(field))
    }
    raw_event_time = mapping(fact.get("event_time"))
    event_time = {
        "kind": fact.get("event_time_kind") or raw_event_time.get("kind"),
        "start_at": fact.get("event_start_at") or raw_event_time.get("start_at"),
        "end_at": fact.get("event_end_at") or raw_event_time.get("end_at"),
        "precision": fact.get("event_time_precision")
        or raw_event_time.get("precision"),
        "expression": fact.get("event_time_expression")
        or raw_event_time.get("expression"),
    }
    event_time = {
        key: canonical_payload(value)
        for key, value in event_time.items()
        if temporal_value_present(value)
    }
    if event_time:
        temporal["event_time"] = event_time
    metadata = mapping(fact.get("metadata"))
    event_references = canonical_repeatable_rows(
        metadata.get("temporal_references")
        or metadata.get("event_references")
        or fact.get("temporal_references")
        or []
    )
    family_state = {"kind": "fact", "statement": statement}
    evidence_state = {
        "source_ids": source_ids,
        "source_spans": source_spans,
        "evidence_quote": quote,
    }
    state = {
        **family_state,
        **evidence_state,
        "temporal": temporal,
        "event_references": event_references,
    }
    source_group = evidence_state if any(evidence_state.values()) else family_state
    return {
        "family_key": f"fact:{digest(family_state)}",
        "portable_key": f"fact:{digest({**family_state, **evidence_state})}",
        "state_fingerprint": digest(state),
        "group_key": f"source:{digest(source_group)}",
    }


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
        entity_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(entities)")
        }
        fact_entity_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(fact_entities)")
        }
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
        watermark_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(cos_stage_watermarks)")
        }
        temporal_run_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(gmail_temporal_review_runs)")
        }
        temporal_artifact_columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(gmail_temporal_review_artifacts)"
            )
        }
        temporal_head_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(gmail_temporal_review_heads)")
        }
        temporal_execution_columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(gmail_temporal_review_executions)"
            )
        }
        temporal_component_columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(gmail_temporal_review_components)"
            )
        }
        review_resolution_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(review_resolutions)")
        }
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert versions == EXPECTED_MIGRATIONS
    assert "relations" not in tables
    assert "events" not in tables
    assert "review_admissions" in tables
    assert "review_admission_meta" in tables
    assert "review_resolutions" in tables
    assert {
        "id",
        "family_key",
        "portable_key",
        "state_fingerprint",
        "group_key",
        "disposition",
        "source_item_kind",
        "source_item_id",
        "decision_payload",
        "decided_by",
        "resolved_at",
        "revoked_at",
    } == review_resolution_columns
    assert {
        "id",
        "source_type",
        "title",
        "source_path",
        "raw_path",
        "content_hash",
        "origin_node_id",
        "logical_source_key",
        "source_mtime_ns",
        "source_size",
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
        "entity_id",
        "status",
        "metadata",
        "source_spans",
        "truth_confidence",
        "routing_confidence",
        "extraction_confidence",
        "temporal_kind",
        "valid_from",
        "valid_to",
        "valid_time_precision",
        "temporal_expression",
        "temporal_confidence",
        "knowledge_to",
        "assertion_lineage_id",
        "revision_of_id",
        "revision_number",
        "revision_status",
        "event_time_kind",
        "event_start_at",
        "event_end_at",
        "event_time_precision",
        "event_time_expression",
    }.issubset(fact_columns)
    assert {"aliases", "status", "merged_into", "description"}.issubset(entity_columns)
    assert {
        "fact_id",
        "entity_id",
        "is_primary",
        "mention_text",
        "mention_span",
        "mention_kind",
        "resolution_method",
        "confidence",
    }.issubset(fact_entity_columns)
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
    assert {
        "stage",
        "document_id",
        "content_hash",
        "model",
        "prompt_version",
        "status",
    }.issubset(watermark_columns)
    assert {
        "input_key",
        "message_scope_key",
        "pipeline_scope",
        "document_id",
        "document_content_hash",
        "gmail_account_key",
        "gmail_thread_id",
        "gmail_source_revision",
        "gmail_message_id",
        "message_internal_at",
        "message_start_offset",
        "message_end_offset",
        "source_sha256",
        "source_locator_hash",
        "grouping_policy_fingerprint",
        "projection_sha256",
        "artifact_set_sha256",
        "projection_json",
        "complete",
        "routable",
    }.issubset(temporal_run_columns)
    assert {
        "run_id",
        "artifact_kind",
        "source_artifact_key",
        "candidate_authorization",
        "payload_sha256",
        "payload_json",
        "routable",
    }.issubset(temporal_artifact_columns)
    assert {
        "message_scope_key",
        "pipeline_scope",
        "run_id",
        "generation",
        "updated_at",
    }.issubset(temporal_head_columns)
    assert {
        "input_key",
        "message_scope_key",
        "pipeline_scope",
        "document_id",
        "source_sha256",
        "runner_policy_fingerprint",
        "admission_policy_fingerprint",
        "verifier_policy_fingerprint",
        "disposition",
        "target_fingerprint",
        "request_set_sha256",
        "component_set_sha256",
        "invocation_attestation",
        "review_run_id",
        "complete",
        "routable",
    }.issubset(temporal_execution_columns)
    assert {
        "execution_id",
        "run_ordinal",
        "invocation_id",
        "artifact_sha256",
        "payload_json",
        "routable",
    }.issubset(temporal_component_columns)


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
    assert {
        "kind": "chunk",
        "target_id": "chunk_legacy",
        "title": "Legacy Chunk",
        "text": "legacy chunk text",
    } in retrieval_rows
    assert {
        "kind": "fact",
        "target_id": "fact_legacy",
        "title": "concepts/test.md",
        "text": "Legacy fact statement",
    } in retrieval_rows


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


def test_init_db_preflights_legacy_entities_before_schema_indexes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "brain.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE entities (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              entity_type TEXT,
              source_ids TEXT NOT NULL DEFAULT '[]',
              created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO entities(id, name, entity_type, source_ids, created_at)
            VALUES ('entity_legacy', 'Legacy Entity', NULL, '[]', '2026-06-01T00:00:00+00:00')
            """
        )

    init_db(db_path)

    with connection(db_path) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(entities)")}
        row = conn.execute(
            """
            SELECT id, aliases, status, merged_into, description
            FROM entities
            WHERE id = 'entity_legacy'
            """
        ).fetchone()
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(entities)")}
        versions = [
            row["version"]
            for row in conn.execute("SELECT version FROM schema_migrations")
        ]

    assert {"aliases", "status", "merged_into", "description"} <= columns
    assert dict(row) == {
        "id": "entity_legacy",
        "aliases": "[]",
        "status": "active",
        "merged_into": None,
        "description": None,
    }
    assert "idx_entities_status" in indexes
    assert versions == EXPECTED_MIGRATIONS


def test_migration_adds_mention_kind_to_existing_fact_entities_table() -> None:
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
        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, '2026-06-29T00:00:00+00:00')",
        [(version,) for version in range(1, 19)],
    )
    conn.execute(
        """
        CREATE TABLE facts (
          id TEXT PRIMARY KEY,
          statement TEXT NOT NULL,
          entity_key TEXT NOT NULL,
          source_ids TEXT NOT NULL DEFAULT '[]',
          confidence REAL NOT NULL,
          status TEXT NOT NULL,
          metadata TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE entities (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          entity_type TEXT,
          aliases TEXT NOT NULL DEFAULT '[]',
          status TEXT NOT NULL DEFAULT 'active',
          source_ids TEXT NOT NULL DEFAULT '[]',
          created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE fact_entities (
          id TEXT PRIMARY KEY,
          fact_id TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
          entity_id TEXT NOT NULL REFERENCES entities(id),
          is_primary INTEGER NOT NULL DEFAULT 0,
          mention_text TEXT,
          mention_span TEXT,
          resolution_method TEXT,
          confidence REAL,
          created_at TEXT NOT NULL
        )
        """
    )

    run_migrations(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(fact_entities)")}
    versions = [
        row["version"] for row in conn.execute("SELECT version FROM schema_migrations")
    ]

    assert "mention_kind" in columns
    assert versions == EXPECTED_MIGRATIONS


def test_temporal_fact_migration_is_additive_and_does_not_guess_valid_time() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE facts (
          id TEXT PRIMARY KEY,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          effective_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO facts(id, status, created_at, effective_at)
        VALUES ('fact_legacy', 'active', '2026-01-02T00:00:00+00:00', '2026-01-01')
        """
    )

    temporal_migration = next(row for row in MIGRATIONS if row[0] == 22)
    run_migrations(conn, [temporal_migration])

    row = conn.execute(
        """
        SELECT temporal_kind, valid_from, valid_to, valid_time_precision,
               temporal_expression, temporal_confidence, knowledge_to
        FROM facts
        WHERE id = 'fact_legacy'
        """
    ).fetchone()
    assert dict(row) == {
        "temporal_kind": "unknown",
        "valid_from": None,
        "valid_to": None,
        "valid_time_precision": "unknown",
        "temporal_expression": None,
        "temporal_confidence": None,
        "knowledge_to": None,
    }


def test_fact_revision_migration_backfills_lineage_and_enforces_one_open_revision() -> (
    None
):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE facts (
          id TEXT PRIMARY KEY,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          knowledge_to TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO facts(id, status, created_at, knowledge_to)
        VALUES ('fact_legacy', 'active', '2026-01-02T00:00:00+00:00', NULL)
        """
    )

    revision_migration = next(row for row in MIGRATIONS if row[0] == 23)
    run_migrations(conn, [revision_migration])

    row = conn.execute(
        """
        SELECT assertion_lineage_id, revision_of_id, revision_number,
               revision_status
        FROM facts
        WHERE id = 'fact_legacy'
        """
    ).fetchone()
    assert dict(row) == {
        "assertion_lineage_id": "fact_legacy",
        "revision_of_id": None,
        "revision_number": 1,
        "revision_status": None,
    }
    indexes = {row["name"] for row in conn.execute("PRAGMA index_list(facts)")}
    assert {
        "idx_facts_assertion_revision",
        "idx_facts_revision_of",
        "idx_facts_open_assertion_lineage",
    }.issubset(indexes)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO facts(
              id, status, created_at, knowledge_to, assertion_lineage_id,
              revision_of_id, revision_number
            ) VALUES (
              'fact_2', 'active', '2026-02-02T00:00:00+00:00', NULL,
              'fact_legacy', 'fact_legacy', 2
            )
            """
        )

    conn.execute(
        """
        UPDATE facts
        SET knowledge_to = '2026-02-02T00:00:00+00:00',
            revision_status = status,
            status = 'revision_closed'
        WHERE id = 'fact_legacy'
        """
    )
    conn.execute(
        """
        INSERT INTO facts(
          id, status, created_at, knowledge_to, assertion_lineage_id,
          revision_of_id, revision_number
        ) VALUES (
          'fact_2', 'active', '2026-02-02T00:00:00+00:00', NULL,
          'fact_legacy', 'fact_legacy', 2
        )
        """
    )
    assert (
        conn.execute(
            """
        SELECT id
        FROM facts
        WHERE assertion_lineage_id = 'fact_legacy' AND knowledge_to IS NULL
        """
        ).fetchone()["id"]
        == "fact_2"
    )


def test_event_time_migration_is_additive_nullable_and_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE facts (
          id TEXT PRIMARY KEY,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO facts(id, status, created_at)
        VALUES ('fact_legacy', 'active', '2026-01-02T00:00:00+00:00')
        """
    )

    event_time_migration = next(row for row in MIGRATIONS if row[0] == 24)
    run_migrations(conn, [event_time_migration])
    run_migrations(conn, [event_time_migration])

    row = conn.execute(
        """
        SELECT event_time_kind, event_start_at, event_end_at,
               event_time_precision, event_time_expression
        FROM facts
        WHERE id = 'fact_legacy'
        """
    ).fetchone()
    assert dict(row) == {
        "event_time_kind": None,
        "event_start_at": None,
        "event_end_at": None,
        "event_time_precision": None,
        "event_time_expression": None,
    }
    assert "idx_facts_event_time" in {
        index["name"] for index in conn.execute("PRAGMA index_list(facts)")
    }


def test_runner_evidence_migration_retires_unattested_production_head_only() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("CREATE TABLE documents(id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO documents(id) VALUES ('doc-gmail-legacy')")
    persistence_migration = next(row for row in MIGRATIONS if row[0] == 25)
    runner_migration = next(row for row in MIGRATIONS if row[0] == 26)
    run_migrations(conn, [persistence_migration])

    run_columns = (
        "id",
        "input_key",
        "message_scope_key",
        "pipeline_scope",
        "document_id",
        "document_content_hash",
        "gmail_account_key",
        "gmail_thread_id",
        "gmail_source_revision",
        "gmail_message_id",
        "message_internal_at",
        "message_start_offset",
        "message_end_offset",
        "source_sha256",
        "source_locator_hash",
        "source_locator_json",
        "projection_version",
        "analysis_fingerprint",
        "batch_plan_fingerprint",
        "ensemble_policy_fingerprint",
        "grouping_policy_fingerprint",
        "projection_fingerprint",
        "projection_sha256",
        "artifact_set_sha256",
        "projection_json",
        "complete",
        "routable",
        "created_at",
    )
    common = (
        "doc-gmail-legacy",
        "a" * 64,
        "personal@example.test",
        "thread-legacy",
        "b" * 64,
        "message-legacy",
        "2026-07-22T12:00:00+00:00",
        0,
        10,
        "c" * 64,
        "d" * 64,
        "{}",
        "gmail_temporal_review_projection_v1",
        "analysis-v1",
        "batch-v1",
        "ensemble-v1",
        "grouping-v1",
        "projection-v1",
        "e" * 64,
        "f" * 64,
        "{}",
        1,
        0,
        "2026-07-22T19:00:00+00:00",
    )
    fixtures = (
        (
            "run-production-legacy",
            "input-production-legacy",
            "scope-production-legacy",
            "gmail_temporal_review_v1",
            *common,
        ),
        (
            "run-review-legacy",
            "input-review-legacy",
            "scope-review-legacy",
            "gmail-temporal-review/test",
            *common,
        ),
    )
    conn.executemany(
        f"INSERT INTO gmail_temporal_review_runs({', '.join(run_columns)}) "
        f"VALUES ({', '.join('?' for _ in run_columns)})",
        fixtures,
    )
    conn.executemany(
        """
        INSERT INTO gmail_temporal_review_heads(
          message_scope_key, pipeline_scope, run_id, generation, updated_at
        ) VALUES (?, ?, ?, 1, '2026-07-22T19:00:00+00:00')
        """,
        (
            (
                "scope-production-legacy",
                "gmail_temporal_review_v1",
                "run-production-legacy",
            ),
            (
                "scope-review-legacy",
                "gmail-temporal-review/test",
                "run-review-legacy",
            ),
        ),
    )

    run_migrations(conn, [runner_migration])
    runner_migration[2](conn)

    heads = [
        tuple(row)
        for row in conn.execute(
            """
            SELECT message_scope_key, pipeline_scope, run_id
            FROM gmail_temporal_review_heads
            ORDER BY message_scope_key
            """
        )
    ]
    assert heads == [
        (
            "scope-review-legacy",
            "gmail-temporal-review/test",
            "run-review-legacy",
        )
    ]
    assert (
        conn.execute("SELECT COUNT(*) FROM gmail_temporal_review_runs").fetchone()[0]
        == 2
    )
    assert [
        row["version"] for row in conn.execute("SELECT version FROM schema_migrations")
    ] == [25, 26]


def test_review_resolution_migration_backfills_confirmed_facts_idempotently(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "brain.sqlite"
    init_db(db_path)

    with connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, source_ids, source_spans,
              evidence_quote, confidence, status, confirmed_by_user,
              metadata, created_at, last_seen_at, temporal_kind, valid_from,
              valid_time_precision, event_time_kind, event_start_at,
              event_time_precision
            ) VALUES (
              'fact_confirmed', 'The launch is scheduled for August 3.',
              'event:launch', '["document:launch"]',
              '[{"chunk_id":"chunk_launch","start":0,"end":45}]',
              'The launch is scheduled for August 3.', 0.94, 'active', 1,
              '{}', '2026-07-30T12:00:00+00:00',
              '2026-07-30T12:00:00+00:00', 'unknown', NULL, 'unknown',
              'scheduled_for', '2026-08-03T16:00:00+00:00', 'minute'
            )
            """
        )
        conn.execute("DROP TABLE review_resolutions")
        conn.execute("DELETE FROM schema_migrations WHERE version = 27")

        run_migrations(conn, [REVIEW_RESOLUTION_MIGRATION])

        first = conn.execute(
            """
            SELECT disposition, source_item_kind, source_item_id, decided_by,
                   state_fingerprint, revoked_at
            FROM review_resolutions
            """
        ).fetchall()
        REVIEW_RESOLUTION_MIGRATION[2](conn)
        second = conn.execute(
            """
            SELECT disposition, source_item_kind, source_item_id, decided_by,
                   state_fingerprint, revoked_at
            FROM review_resolutions
            """
        ).fetchall()

    assert len(first) == 1
    assert first[0]["disposition"] == "keep"
    assert first[0]["source_item_kind"] == "fact_confirmation"
    assert first[0]["source_item_id"] == "fact_confirmed"
    assert first[0]["decided_by"] == "human"
    assert first[0]["revoked_at"] is None
    assert [tuple(row) for row in second] == [tuple(first[0])]


def test_review_resolution_migration_coerces_legacy_question_answer_json(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "brain.sqlite"
    init_db(db_path)
    legacy_answers = ["null", "[]", '"accepted"', "42", "true"]

    with connection(db_path) as conn:
        for index, answer in enumerate(legacy_answers):
            action_id = f"action_legacy_answer_{index}"
            question_id = f"question_legacy_answer_{index}"
            page_hint = f"concepts/legacy-answer-{index}.md"
            conn.execute(
                """
                INSERT INTO cos_actions(
                  id, action_type, status, target_fact_ids, target_page_paths,
                  target_contract_ids, action_features, inverse_action_json,
                  evidence_json, audit_status, created_at, applied_at
                ) VALUES (?, 'canonicalize_page', 'applied', '[]', ?, '[]',
                          '{}', '{}', ?, 'unaudited', ?, ?)
                """,
                (
                    action_id,
                    json.dumps([page_hint]),
                    json.dumps({"payload": {"page_hint": page_hint}}),
                    f"2026-07-30T12:0{index}:00+00:00",
                    f"2026-07-30T12:0{index}:00+00:00",
                ),
            )
            conn.execute(
                """
                INSERT INTO open_questions(
                  id, kind, question, status, answer, created_at, answered_at,
                  action_id, decided_by
                ) VALUES (?, 'legacy_review', 'Keep this action?', 'answered',
                          ?, ?, ?, ?, 'human')
                """,
                (
                    question_id,
                    answer,
                    f"2026-07-30T12:0{index}:00+00:00",
                    f"2026-07-30T12:1{index}:00+00:00",
                    action_id,
                ),
            )

        conn.execute("DROP TABLE review_resolutions")
        conn.execute("DELETE FROM schema_migrations WHERE version = 27")
        run_migrations(conn, [REVIEW_RESOLUTION_MIGRATION])
        rows = list(
            conn.execute(
                """
                SELECT source_item_id, disposition, decision_payload
                FROM review_resolutions
                ORDER BY source_item_id
                """
            )
        )
        REVIEW_RESOLUTION_MIGRATION[2](conn)
        count_after_repeat = conn.execute(
            "SELECT COUNT(*) FROM review_resolutions"
        ).fetchone()[0]

    assert len(rows) == len(legacy_answers)
    assert all(row["disposition"] == "keep" for row in rows)
    assert all(json.loads(row["decision_payload"]) == {} for row in rows)
    assert count_after_repeat == len(legacy_answers)


def test_review_resolution_migration_recovers_null_decider_human_selection(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "brain.sqlite"
    init_db(db_path)
    original_fact = {
        "id": "fact_candidate",
        "statement": "The candidate state is correct.",
        "entity_key": "concepts:candidate:summary",
        "source_ids": ["document:candidate"],
        "source_spans": [{"source_id": "document:candidate", "start": 0, "end": 31}],
        "evidence_quote": "The candidate state is correct.",
    }

    with connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO cos_actions(
              id, action_type, status, target_fact_ids, target_page_paths,
              target_contract_ids, action_features, proposed_by,
              inverse_action_json, evidence_json, audit_status, created_at,
              applied_at
            ) VALUES (
              'action_original', 'fact_upsert', 'applied', '["fact_candidate"]',
              '["concepts/candidate.md"]', '[]', '{}', 'extractor', '{}', ?,
              'unaudited', '2026-07-30T12:00:00+00:00',
              '2026-07-30T12:01:00+00:00'
            )
            """,
            (json.dumps({"payload": {"fact": original_fact}}, sort_keys=True),),
        )
        conn.execute(
            """
            INSERT INTO cos_actions(
              id, action_type, status, target_fact_ids, target_page_paths,
              target_contract_ids, action_features, proposed_by,
              inverse_action_json, evidence_json, audit_status, created_at,
              applied_at
            ) VALUES (
              'action_selection', 'resolve_conflict', 'applied',
              '["fact_candidate","fact_existing"]',
              '["concepts/candidate.md"]', '[]',
              '{"human_confirmed":true}', 'question_answer', '{}',
              '{"payload":{"question_id":"question_selection"}}',
              'unaudited', '2026-07-30T12:02:00+00:00',
              '2026-07-30T12:02:00+00:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, fact_ids, question, options, status, answer, context,
              created_at, answered_at, action_id, decided_by
            ) VALUES (
              'question_selection', 'fact_conflict_review',
              '["fact_candidate","fact_existing"]', 'Which fact is true?',
              '[]', 'answered', ?, ?, '2026-07-30T12:00:00+00:00',
              '2026-07-30T12:02:00+00:00', 'action_selection', NULL
            )
            """,
            (
                json.dumps(
                    {
                        "selected_fact_ids": ["fact_candidate"],
                        "selected_fact_id": "fact_candidate",
                        "superseded_fact_ids": ["fact_existing"],
                        "answer": "",
                    },
                    sort_keys=True,
                ),
                json.dumps({"candidate_action_id": "action_original"}),
            ),
        )
        conn.execute("DROP TABLE review_resolutions")
        conn.execute("DELETE FROM schema_migrations WHERE version = 27")
        run_migrations(conn, [REVIEW_RESOLUTION_MIGRATION])
        resolution = conn.execute(
            """
            SELECT disposition, source_item_id, decided_by, decision_payload,
                   family_key, state_fingerprint
            FROM review_resolutions
            WHERE source_item_kind = 'question'
            """
        ).fetchone()
        original_row = conn.execute(
            "SELECT * FROM cos_actions WHERE id = 'action_original'"
        ).fetchone()

    from pkm_brain.review_resolution import action_review_identity, decoded_action_row

    expected = action_review_identity(decoded_action_row(original_row))
    assert resolution["disposition"] == "keep"
    assert resolution["source_item_id"] == "question_selection"
    assert resolution["decided_by"] == "human"
    assert json.loads(resolution["decision_payload"])["backfill_provenance"] == (
        "legacy_human_resolution"
    )
    assert resolution["family_key"] == expected["family_key"]
    assert resolution["state_fingerprint"] == expected["state_fingerprint"]


def test_review_resolution_migration_recovers_null_decider_human_dismissal(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "brain.sqlite"
    init_db(db_path)
    with connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO cos_actions(
              id, action_type, status, target_fact_ids, target_page_paths,
              target_contract_ids, action_features, proposed_by,
              inverse_action_json, evidence_json, audit_status, created_at
            ) VALUES (
              'action_dismissed', 'canonicalize_page', 'rejected', '[]',
              '["concepts/dismissed.md"]', '[]', '{}', 'synthesizer', '{}', ?,
              'unaudited', '2026-07-30T12:00:00+00:00'
            )
            """,
            (
                json.dumps(
                    {
                        "payload": {"page_hint": "concepts/dismissed.md"},
                        "human_review": {
                            "decision": "reject",
                            "reason": "human rejected queue item",
                            "decided_at": "2026-07-30T12:01:00+00:00",
                        },
                    },
                    sort_keys=True,
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, question, options, status, answer, context, created_at,
              answered_at, action_id, decided_by
            ) VALUES (
              'question_dismissed', 'topology_review', 'Keep this page?', '[]',
              'dismissed', '{"reason":"human rejected queue item"}', '{}',
              '2026-07-30T12:00:00+00:00',
              '2026-07-30T12:01:00+00:00', 'action_dismissed', NULL
            )
            """
        )
        conn.execute("DROP TABLE review_resolutions")
        conn.execute("DELETE FROM schema_migrations WHERE version = 27")
        run_migrations(conn, [REVIEW_RESOLUTION_MIGRATION])
        resolution = conn.execute(
            """
            SELECT disposition, source_item_id, decided_by
            FROM review_resolutions
            WHERE source_item_kind = 'question'
            """
        ).fetchone()

    assert tuple(resolution) == ("reject", "question_dismissed", "human")


def test_review_resolution_backfill_rejects_original_and_keeps_manual_answer(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "brain.sqlite"
    init_db(db_path)
    with connection(db_path) as conn:
        for fact_id, statement, source_id, confirmed in (
            (
                "fact_free_text_original",
                "The original answer is stale.",
                "document:free-text-original",
                0,
            ),
            (
                "fact_free_text_manual",
                "The human supplied the current answer.",
                "manual:question:question_free_text_backfill",
                1,
            ),
        ):
            conn.execute(
                """
                INSERT INTO facts(
                  id, statement, entity_key, page_hint, source_ids, source_spans,
                  evidence_quote, confidence, status, confirmed_by_user,
                  metadata, created_at, last_seen_at
                ) VALUES (?, ?, 'concepts:free-text:summary',
                          'concepts/free-text.md', ?, '[]', ?, 1.0, 'active', ?,
                          ?, '2026-07-31T12:00:00+00:00',
                          '2026-07-31T12:01:00+00:00')
                """,
                (
                    fact_id,
                    statement,
                    json.dumps([source_id]),
                    statement,
                    confirmed,
                    json.dumps(
                        {
                            "question_id": "question_free_text_backfill"
                            if confirmed
                            else None
                        },
                        sort_keys=True,
                    ),
                ),
            )
        conn.execute(
            """
            INSERT INTO cos_actions(
              id, action_type, status, target_fact_ids, target_page_paths,
              target_contract_ids, action_features, proposed_by,
              inverse_action_json, evidence_json, audit_status, created_at,
              applied_at
            ) VALUES (
              'action_free_text_result', 'fact_supersede', 'applied',
              '["fact_free_text_original"]', '["concepts/free-text.md"]', '[]',
              '{"human_confirmed":true}', 'question_answer', '{}',
              '{"payload":{"question_id":"question_free_text_backfill"}}',
              'unaudited', '2026-07-31T12:01:00+00:00',
              '2026-07-31T12:01:00+00:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, fact_ids, question, options, status, answer, context,
              created_at, answered_at, action_id, decided_by
            ) VALUES (
              'question_free_text_backfill', 'conflict',
              '["fact_free_text_original"]', 'What is true?',
              '[{"fact_id":"fact_free_text_original"}]', 'answered', ?, '{}',
              '2026-07-31T12:00:00+00:00',
              '2026-07-31T12:01:00+00:00', 'action_free_text_result', NULL
            )
            """,
            (
                json.dumps(
                    {
                        "selected_fact_id": "fact_free_text_manual",
                        "answer": "The human supplied the current answer.",
                    },
                    sort_keys=True,
                ),
            ),
        )
        conn.execute("DROP TABLE review_resolutions")
        conn.execute("DELETE FROM schema_migrations WHERE version = 27")
        run_migrations(conn, [REVIEW_RESOLUTION_MIGRATION])
        active = [
            tuple(row)
            for row in conn.execute(
                """
                SELECT disposition, source_item_kind, source_item_id
                FROM review_resolutions
                WHERE revoked_at IS NULL
                ORDER BY source_item_kind, source_item_id
                """
            )
        ]

    assert active == [
        ("keep", "fact_confirmation", "fact_free_text_manual"),
        ("reject", "question", "question_free_text_backfill"),
    ]


@pytest.mark.parametrize(
    ("decision", "replacement_proposer", "expected_disposition"),
    [
        ("route", "ui_queue_route", "keep"),
        ("new_page", "ui_queue_route", "keep"),
        ("supports", "ui_queue_supports_existing", "reject"),
        ("supports_existing", "ui_queue_supports_existing", "reject"),
        ("merge_evidence", "ui_queue_supports_existing", "reject"),
        ("temporal_update", "ui_queue_temporal_update", "reject"),
        ("updates", "ui_queue_temporal_update", "reject"),
        ("current_state", "ui_queue_temporal_update", "reject"),
    ],
)
def test_review_resolution_migration_records_replacement_semantics(
    tmp_path: Path,
    decision: str,
    replacement_proposer: str,
    expected_disposition: str,
) -> None:
    db_path = tmp_path / "brain.sqlite"
    init_db(db_path)
    original_fact = {
        "id": "fact_original",
        "statement": f"Original candidate for {decision}.",
        "entity_key": "concepts:original:summary",
        "source_ids": [f"document:{decision}"],
        "source_spans": [{"source_id": f"document:{decision}", "start": 0, "end": 40}],
        "evidence_quote": f"Original candidate for {decision}.",
    }
    replacement_fact = {**original_fact, "id": "fact_replacement"}
    if decision in {"route", "new_page"}:
        replacement_fact["page_hint"] = "concepts/approved-route.md"
    else:
        replacement_fact.update(
            {
                "statement": f"Replacement result for {decision}.",
                "evidence_quote": f"Replacement result for {decision}.",
            }
        )
    original_evidence = {
        "payload": {"fact": original_fact},
        "human_review": {
            "decision": "reject",
            "reason": "replaced by human decision",
            "decided_at": "2026-07-30T12:02:00+00:00",
        },
    }
    answer = {"decision": decision}
    if decision in {"route", "new_page"}:
        answer["old_action_id"] = "action_original"
        answer["new_action_id"] = "action_replacement"

    with connection(db_path) as conn:
        for action_id, fact, proposer, evidence in (
            ("action_original", original_fact, "extractor", original_evidence),
            (
                "action_replacement",
                replacement_fact,
                replacement_proposer,
                {"payload": {"fact": replacement_fact}},
            ),
        ):
            conn.execute(
                """
                INSERT INTO cos_actions(
                  id, action_type, status, target_fact_ids, target_page_paths,
                  target_contract_ids, action_features, proposed_by,
                  inverse_action_json, evidence_json, audit_status, created_at,
                  applied_at
                ) VALUES (?, 'fact_upsert', ?, ?, '["concepts/original.md"]',
                          '[]', '{"human_confirmed":true}', ?, '{}', ?,
                          'unaudited', '2026-07-30T12:00:00+00:00',
                          '2026-07-30T12:02:00+00:00')
                """,
                (
                    action_id,
                    "rejected" if action_id == "action_original" else "applied",
                    json.dumps([fact["id"]]),
                    proposer,
                    json.dumps(evidence, sort_keys=True),
                ),
            )
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, fact_ids, question, options, status, answer, context,
              recommended_action, created_at, answered_at, action_id, decided_by
            ) VALUES (
              'question_replacement', 'fact_conflict_review', '["fact_existing"]',
              'How should this candidate be handled?', '[]', 'answered', ?, ?, ?,
              '2026-07-30T12:00:00+00:00',
              '2026-07-30T12:02:00+00:00', 'action_replacement', NULL
            )
            """,
            (
                json.dumps(answer, sort_keys=True),
                json.dumps(
                    {
                        "candidate_action_id": "action_original",
                        "action_id": "action_original",
                    },
                    sort_keys=True,
                ),
                json.dumps(
                    {"action_type": "fact_upsert", "payload": {"fact": original_fact}},
                    sort_keys=True,
                ),
            ),
        )
        conn.execute("DROP TABLE review_resolutions")
        conn.execute("DELETE FROM schema_migrations WHERE version = 27")
        run_migrations(conn, [REVIEW_RESOLUTION_MIGRATION])
        resolution = conn.execute(
            """
            SELECT disposition, family_key, state_fingerprint
            FROM review_resolutions
            WHERE source_item_kind = 'question'
            """
        ).fetchone()
        original_row = conn.execute(
            "SELECT * FROM cos_actions WHERE id = 'action_original'"
        ).fetchone()
        replacement_row = conn.execute(
            "SELECT * FROM cos_actions WHERE id = 'action_replacement'"
        ).fetchone()

    from pkm_brain.review_resolution import action_review_identity, decoded_action_row

    original = action_review_identity(decoded_action_row(original_row))
    replacement = action_review_identity(decoded_action_row(replacement_row))
    expected = replacement if expected_disposition == "keep" else original
    assert resolution["disposition"] == expected_disposition
    assert resolution["family_key"] == expected["family_key"]
    assert resolution["state_fingerprint"] == expected["state_fingerprint"]
    if expected_disposition == "reject":
        assert resolution["state_fingerprint"] != replacement["state_fingerprint"]


def test_review_resolution_migration_skips_human_result_without_original_identity(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "brain.sqlite"
    init_db(db_path)
    with connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO cos_actions(
              id, action_type, status, target_fact_ids, target_page_paths,
              target_contract_ids, action_features, proposed_by,
              inverse_action_json, evidence_json, audit_status, created_at,
              applied_at
            ) VALUES (
              'action_result_only', 'resolve_conflict', 'applied',
              '["fact_manual"]', '[]', '[]', '{"human_confirmed":true}',
              'question_answer', '{}',
              '{"payload":{"question_id":"question_unmatched"}}',
              'unaudited', '2026-07-30T12:00:00+00:00',
              '2026-07-30T12:01:00+00:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, question, options, status, answer, context, created_at,
              answered_at, action_id, decided_by
            ) VALUES (
              'question_unmatched', 'conflict', 'What is true?', '[]',
              'answered', ?, '{}', '2026-07-30T12:00:00+00:00',
              '2026-07-30T12:01:00+00:00', 'action_result_only', NULL
            )
            """,
            (
                json.dumps(
                    {"selected_fact_id": "fact_manual", "answer": "Manual truth"},
                    sort_keys=True,
                ),
            ),
        )
        conn.execute("DROP TABLE review_resolutions")
        conn.execute("DELETE FROM schema_migrations WHERE version = 27")
        run_migrations(conn, [REVIEW_RESOLUTION_MIGRATION])
        question_resolution_count = conn.execute(
            """
            SELECT COUNT(*) FROM review_resolutions
            WHERE source_item_kind = 'question'
            """
        ).fetchone()[0]

    assert question_resolution_count == 0


def test_final_review_backfill_repairs_v27_replacement_and_preserves_valid_live(
    tmp_path: Path,
) -> None:
    from pkm_brain.review_resolution import (
        decoded_action_row,
        record_review_resolution,
    )

    db_path = tmp_path / "brain.sqlite"
    init_db(db_path)
    original_fact = {
        "id": "fact_v27_original",
        "statement": "The original v27 candidate.",
        "entity_key": "concepts:v27:summary",
        "source_ids": ["document:v27-original"],
        "source_spans": [{"source_id": "document:v27-original", "start": 0, "end": 27}],
        "evidence_quote": "The original v27 candidate.",
    }
    replacement_fact = {
        **original_fact,
        "id": "fact_v27_replacement",
        "page_hint": "concepts/v27.md",
    }
    valid_payload = {"page_hint": "concepts/valid-live.md"}
    with connection(db_path) as conn:
        for action_id, status, proposer, fact in (
            ("action_v27_original", "rejected", "extractor", original_fact),
            (
                "action_v27_replacement",
                "applied",
                "ui_queue_route",
                replacement_fact,
            ),
        ):
            evidence = {"payload": {"fact": fact}}
            if action_id == "action_v27_original":
                evidence["human_review"] = {
                    "decision": "reject",
                    "reason": "replaced by explicit UI route decision",
                    "decided_at": "2026-07-31T12:01:00+00:00",
                }
            conn.execute(
                """
                INSERT INTO cos_actions(
                  id, action_type, status, target_fact_ids, target_page_paths,
                  target_contract_ids, action_features, proposed_by,
                  inverse_action_json, evidence_json, audit_status, created_at,
                  applied_at
                ) VALUES (?, 'fact_upsert', ?, ?, '["concepts/v27.md"]', '[]',
                          '{}', ?, '{}', ?, 'unaudited',
                          '2026-07-31T12:00:00+00:00', ?)
                """,
                (
                    action_id,
                    status,
                    json.dumps([fact["id"]]),
                    proposer,
                    json.dumps(evidence, sort_keys=True),
                    "2026-07-31T12:01:00+00:00" if status == "applied" else None,
                ),
            )
        conn.execute(
            """
            INSERT INTO cos_actions(
              id, action_type, status, target_fact_ids, target_page_paths,
              target_contract_ids, action_features, proposed_by,
              inverse_action_json, evidence_json, audit_status, created_at,
              applied_at
            ) VALUES (
              'action_valid_live', 'canonicalize_page', 'applied', '[]',
              '["concepts/valid-live.md"]', '[]', '{}', 'synthesizer', '{}', ?,
              'unaudited', '2026-07-31T12:00:00+00:00',
              '2026-07-31T12:01:00+00:00'
            )
            """,
            (json.dumps({"payload": valid_payload}, sort_keys=True),),
        )
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, question, options, status, answer, context,
              recommended_action, created_at, answered_at, action_id, decided_by
            ) VALUES (
              'question_v27_route', 'fact_conflict_review', 'Where should it go?',
              '[]', 'answered', ?, ?, ?, '2026-07-31T12:00:00+00:00',
              '2026-07-31T12:01:00+00:00', 'action_v27_replacement', 'human'
            )
            """,
            (
                json.dumps(
                    {
                        "decision": "route",
                        "old_action_id": "action_v27_original",
                        "new_action_id": "action_v27_replacement",
                    },
                    sort_keys=True,
                ),
                json.dumps({"candidate_action_id": "action_v27_original"}),
                json.dumps(
                    {"action_type": "fact_upsert", "payload": {"fact": original_fact}},
                    sort_keys=True,
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, question, options, status, answer, context, created_at,
              answered_at, action_id, decided_by
            ) VALUES (
              'question_valid_live', 'topology_review', 'Keep this page?', '[]',
              'answered', '{"decision":"apply_action","action_id":"action_valid_live"}',
              '{}', '2026-07-31T12:00:00+00:00',
              '2026-07-31T12:01:00+00:00', 'action_valid_live', 'human'
            )
            """
        )
        original_action = decoded_action_row(
            conn.execute(
                "SELECT * FROM cos_actions WHERE id = 'action_v27_original'"
            ).fetchone()
        )
        valid_action = decoded_action_row(
            conn.execute(
                "SELECT * FROM cos_actions WHERE id = 'action_valid_live'"
            ).fetchone()
        )
        wrong, _ = record_review_resolution(
            conn,
            original_action,
            disposition="reject",
            source_item_kind="question",
            source_item_id="question_v27_route",
            resolution_id="resolution_wrong_v27",
        )
        valid, _ = record_review_resolution(
            conn,
            valid_action,
            disposition="keep",
            source_item_kind="question",
            source_item_id="question_valid_live",
            resolution_id="resolution_valid_live",
        )
        conn.execute("DELETE FROM schema_migrations WHERE version = 28")

        run_migrations(conn, [FINAL_REVIEW_BACKFILL_MIGRATION])
        first = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, disposition, source_item_id, revoked_at
                FROM review_resolutions
                WHERE source_item_kind = 'question'
                ORDER BY id
                """
            )
        ]
        FINAL_REVIEW_BACKFILL_MIGRATION[2](conn)
        second = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, disposition, source_item_id, revoked_at
                FROM review_resolutions
                WHERE source_item_kind = 'question'
                ORDER BY id
                """
            )
        ]

    wrong_row = next(row for row in first if row["id"] == wrong["id"])
    valid_row = next(row for row in first if row["id"] == valid["id"])
    repaired = [
        row
        for row in first
        if row["source_item_id"] == "question_v27_route" and row["revoked_at"] is None
    ]
    assert wrong_row["revoked_at"] is not None
    assert valid_row == {
        "id": "resolution_valid_live",
        "disposition": "keep",
        "source_item_id": "question_valid_live",
        "revoked_at": None,
    }
    assert len(repaired) == 1
    assert repaired[0]["disposition"] == "keep"
    assert second == first


def test_review_resolution_migration_backfills_opposite_exact_sibling_decisions(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "brain.sqlite"
    init_db(db_path)
    fact = {
        "id": "fact_launch",
        "statement": "The launch is scheduled for August 3.",
        "entity_key": "event:launch",
        "source_ids": ["document:launch"],
        "source_spans": [{"chunk_id": "chunk_launch", "start": 0, "end": 45}],
        "evidence_quote": "The launch is scheduled for August 3.",
        "temporal_kind": "unknown",
        "valid_time_precision": "unknown",
        "event_time_kind": "scheduled_for",
        "event_start_at": "2026-08-03T16:00:00+00:00",
        "event_time_precision": "minute",
    }
    decisions = (
        (
            "action_exact_keep",
            "fact_launch_first",
            "2026-07-31T12:00:00+00:00",
            "sampled_ok",
            {"ui_marked_ok": True},
        ),
        (
            "action_exact_reject",
            "fact_launch_second",
            "2026-07-31T12:01:00+00:00",
            "sampled_bad",
            {"ui_rejected_current_fact": True},
        ),
    )

    with connection(db_path) as conn:
        for action_id, target_fact_id, decided_at, audit_status, metadata in decisions:
            evidence = {
                "payload": {"fact": fact},
                "audits": [
                    {
                        "status": audit_status,
                        "metadata": metadata,
                        "at": decided_at,
                    }
                ],
            }
            conn.execute(
                """
                INSERT INTO cos_actions(
                  id, action_type, status, target_fact_ids, target_page_paths,
                  target_contract_ids, action_features, inverse_action_json,
                  evidence_json, audit_status, created_at, applied_at
                ) VALUES (?, 'fact_upsert', 'applied', ?, '[]', '[]', '{}',
                          '{}', ?, ?, ?, ?)
                """,
                (
                    action_id,
                    json.dumps([target_fact_id]),
                    json.dumps(evidence, sort_keys=True),
                    audit_status,
                    decided_at,
                    decided_at,
                ),
            )

        conn.execute("DROP TABLE review_resolutions")
        conn.execute("DELETE FROM schema_migrations WHERE version = 27")
        run_migrations(conn, [REVIEW_RESOLUTION_MIGRATION])

        first = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, family_key, state_fingerprint, disposition,
                       source_item_id, resolved_at, revoked_at
                FROM review_resolutions
                ORDER BY resolved_at, id
                """
            )
        ]
        REVIEW_RESOLUTION_MIGRATION[2](conn)
        second = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, family_key, state_fingerprint, disposition,
                       source_item_id, resolved_at, revoked_at
                FROM review_resolutions
                ORDER BY resolved_at, id
                """
            )
        ]
        active_count = conn.execute(
            "SELECT COUNT(*) FROM review_resolutions WHERE revoked_at IS NULL"
        ).fetchone()[0]

    assert len(first) == 2
    assert first[0]["id"] != first[1]["id"]
    assert first[0]["family_key"] == first[1]["family_key"]
    assert first[0]["state_fingerprint"] == first[1]["state_fingerprint"]
    assert [row["disposition"] for row in first] == ["keep", "reject"]
    assert first[0]["revoked_at"] == "2026-07-31T12:01:00+00:00"
    assert first[1]["revoked_at"] is None
    assert active_count == 1
    assert second == first


def test_entity_review_rekey_migration_preserves_direct_keep_and_reject(
    tmp_path: Path,
) -> None:
    from pkm_brain.review_resolution import (
        action_review_identity,
        active_resolution_for_action,
    )

    db_path = tmp_path / "brain.sqlite"
    init_db(db_path)
    fixtures = (
        (
            "keep",
            "The Apollo launch is scheduled for August 3.",
            "event:apollo-launch",
            "entity_apollo_launch",
            "document:apollo-launch",
        ),
        (
            "reject",
            "The Gemini launch is scheduled for August 7.",
            "event:gemini-launch",
            "entity_gemini_launch",
            "document:gemini-launch",
        ),
    )
    actions: dict[str, dict[str, object]] = {}

    with connection(db_path) as conn:
        for index, (
            disposition,
            statement,
            entity_key,
            entity_id,
            source_id,
        ) in enumerate(fixtures):
            action_id = f"action_schema28_direct_{disposition}"
            fact_id = f"fact_schema28_direct_{disposition}"
            fact = {
                "id": fact_id,
                "statement": statement,
                "entity_key": entity_key,
                "entity_id": entity_id,
                "entity_mentions": [
                    {
                        "surface": "launch",
                        "entity_identity": statement.split(" launch", 1)[0]
                        .removeprefix("The ")
                        .strip(),
                        "entity_type": "event",
                        "is_primary": True,
                    },
                    {
                        "surface": "launch team",
                        "entity_type": "organization",
                        "is_primary": False,
                    },
                ],
                "source_ids": [source_id],
                "source_spans": [
                    {
                        "source_id": source_id,
                        "chunk_id": f"chunk_{disposition}",
                        "start": 0,
                        "end": len(statement),
                    }
                ],
                "evidence_quote": statement,
                "temporal_kind": "unknown",
                "valid_time_precision": "unknown",
                "event_time_kind": "scheduled_for",
                "event_start_at": f"2026-08-{3 + (index * 4):02d}T16:00:00+00:00",
                "event_time_precision": "minute",
                "metadata": {},
            }
            action: dict[str, object] = {
                "id": action_id,
                "action_type": "fact_upsert",
                "status": "applied" if disposition == "keep" else "rejected",
                "target_fact_ids": [fact_id],
                "target_page_paths": [f"events/{disposition}-launch.md"],
                "target_contract_ids": [],
                "action_features": {"truth_mutation": True},
                "evidence_json": {"payload": {"fact": fact}},
                "created_at": f"2026-07-31T12:0{index}:00+00:00",
                "applied_at": (
                    f"2026-07-31T12:0{index}:30+00:00"
                    if disposition == "keep"
                    else None
                ),
            }
            actions[disposition] = action
            conn.execute(
                """
                INSERT INTO cos_actions(
                  id, action_type, status, target_fact_ids, target_page_paths,
                  target_contract_ids, action_features, proposed_by,
                  inverse_action_json, evidence_json, audit_status, created_at,
                  applied_at
                ) VALUES (?, 'fact_upsert', ?, ?, ?, '[]', ?, 'extractor',
                          '{}', ?, 'unaudited', ?, ?)
                """,
                (
                    action_id,
                    action["status"],
                    json.dumps(action["target_fact_ids"]),
                    json.dumps(action["target_page_paths"]),
                    json.dumps(action["action_features"], sort_keys=True),
                    json.dumps(action["evidence_json"], sort_keys=True),
                    action["created_at"],
                    action["applied_at"],
                ),
            )
            legacy_identity = schema_28_fact_review_identity(fact)
            current_identity = action_review_identity(action)
            assert (
                legacy_identity["state_fingerprint"]
                != current_identity["state_fingerprint"]
            )
            legacy_resolution_id = f"resolution_schema28_direct_{disposition}"
            decision_payload = {"decision": disposition}
            if disposition == "reject":
                decision_payload["superseded_resolution_id"] = (
                    "resolution_schema28_reject_previous_keep"
                )
            conn.execute(
                """
                INSERT INTO review_resolutions(
                  id, family_key, portable_key, state_fingerprint, group_key,
                  disposition, source_item_kind, source_item_id,
                  decision_payload, decided_by, resolved_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'action', ?, ?, 'human', ?, NULL)
                """,
                (
                    legacy_resolution_id,
                    legacy_identity["family_key"],
                    legacy_identity["portable_key"],
                    legacy_identity["state_fingerprint"],
                    legacy_identity["group_key"],
                    disposition,
                    action_id,
                    json.dumps(decision_payload, sort_keys=True),
                    f"2026-07-31T12:1{index}:00+00:00",
                ),
            )
            if disposition == "reject":
                conn.execute(
                    """
                    INSERT INTO review_resolutions(
                      id, family_key, portable_key, state_fingerprint, group_key,
                      disposition, source_item_kind, source_item_id,
                      decision_payload, decided_by, resolved_at, revoked_at
                    ) VALUES (
                      'resolution_schema28_reject_previous_keep', ?, ?, ?, ?,
                      'keep', 'action', ?, '{"decision":"keep"}', 'human',
                      '2026-07-31T12:09:00+00:00',
                      '2026-07-31T12:11:00+00:00'
                    )
                    """,
                    (
                        legacy_identity["family_key"],
                        legacy_identity["portable_key"],
                        legacy_identity["state_fingerprint"],
                        legacy_identity["group_key"],
                        action_id,
                    ),
                )
                # Simulate a partially upgraded process having derived an
                # opposite current-key row before schema 29 was recorded.  The
                # released active Reject is authoritative on an equal timestamp,
                # and migration must satisfy the partial unique index without
                # letting the rejected issue resurface.
                conn.execute(
                    """
                    INSERT INTO review_resolutions(
                      id, family_key, portable_key, state_fingerprint, group_key,
                      disposition, source_item_kind, source_item_id,
                      decision_payload, decided_by, resolved_at, revoked_at
                    ) VALUES (
                      'resolution_schema29_derived_keep', ?, ?, ?, ?, 'keep',
                      'action', ?, '{"derived":true}', 'human', ?, NULL
                    )
                    """,
                    (
                        current_identity["family_key"],
                        current_identity["portable_key"],
                        current_identity["state_fingerprint"],
                        current_identity["group_key"],
                        action_id,
                        f"2026-07-31T12:1{index}:00+00:00",
                    ),
                )

        conn.execute("DELETE FROM schema_migrations WHERE version = 29")
        run_migrations(conn, [ENTITY_REVIEW_REKEY_MIGRATION])

        first = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, family_key, portable_key, state_fingerprint,
                       group_key, disposition, source_item_kind, source_item_id,
                       decision_payload, decided_by, resolved_at, revoked_at
                FROM review_resolutions
                ORDER BY id
                """
            )
        ]
        active_count = conn.execute(
            "SELECT COUNT(*) FROM review_resolutions WHERE revoked_at IS NULL"
        ).fetchone()[0]
        active_index_count = conn.execute(
            """
            SELECT COUNT(*) FROM pragma_index_list('review_resolutions')
            WHERE name = 'idx_review_resolutions_active_state' AND partial = 1
            """
        ).fetchone()[0]
        for disposition, action in actions.items():
            active = active_resolution_for_action(conn, action)
            assert active is not None
            assert active["disposition"] == disposition
            assert active["source_item_kind"] == "action"
            assert active["source_item_id"] == action["id"]
            assert active["decided_by"] == "human"
            migration = active["decision_payload"]["identity_migration"]
            assert migration == {
                "from_resolution_id": f"resolution_schema28_direct_{disposition}",
                "schema_version": 29,
            }
            if disposition == "reject":
                predecessor_id = active["decision_payload"]["superseded_resolution_id"]
                assert predecessor_id != "resolution_schema28_reject_previous_keep"
                predecessor = conn.execute(
                    """
                    SELECT family_key, state_fingerprint, disposition, revoked_at
                    FROM review_resolutions WHERE id = ?
                    """,
                    (predecessor_id,),
                ).fetchone()
                assert tuple(predecessor) == (
                    active["family_key"],
                    active["state_fingerprint"],
                    "keep",
                    "2026-07-31T12:11:00+00:00",
                )

        ENTITY_REVIEW_REKEY_MIGRATION[2](conn)
        second = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, family_key, portable_key, state_fingerprint,
                       group_key, disposition, source_item_kind, source_item_id,
                       decision_payload, decided_by, resolved_at, revoked_at
                FROM review_resolutions
                ORDER BY id
                """
            )
        ]

    assert len(first) == 7
    assert active_count == 2
    assert active_index_count == 1
    assert all(
        row["revoked_at"] is not None
        for row in first
        if row["id"].startswith("resolution_schema28_")
    )
    derived = next(
        row for row in first if row["id"] == "resolution_schema29_derived_keep"
    )
    assert derived["revoked_at"] is not None
    assert second == first


def test_entity_review_rekey_migration_replays_manual_answer_pair(
    tmp_path: Path,
) -> None:
    from pkm_brain.review_resolution import (
        active_resolution_for_action,
        synthetic_fact_action,
    )

    db_path = tmp_path / "brain.sqlite"
    init_db(db_path)
    question_id = "question_schema28_manual_answer"
    original_fact = {
        "id": "fact_schema28_original_answer",
        "statement": "The launch owner is the legacy operations team.",
        "entity_key": "event:launch",
        "entity_id": "entity_launch",
        "entity_mentions": [
            {
                "surface": "launch",
                "entity_identity": "Launch",
                "entity_type": "event",
                "is_primary": True,
            }
        ],
        "source_ids": ["document:launch-owner"],
        "source_spans": [
            {
                "source_id": "document:launch-owner",
                "chunk_id": "chunk_launch_owner",
                "start": 0,
                "end": 48,
            }
        ],
        "evidence_quote": "The launch owner is the legacy operations team.",
        "metadata": {},
    }
    original_action: dict[str, object] = {
        "id": "action_schema28_original_answer",
        "action_type": "fact_upsert",
        "status": "rejected",
        "target_fact_ids": [original_fact["id"]],
        "target_page_paths": ["events/launch.md"],
        "target_contract_ids": [],
        "action_features": {},
        "evidence_json": {"payload": {"fact": original_fact}},
        "created_at": "2026-07-31T13:00:00+00:00",
    }
    manual_statement = "The launch owner is Peter."

    with connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO entities(
              id, name, entity_type, aliases, status, source_ids, created_at
            ) VALUES (
              'entity_launch', 'Launch Event', 'event', '[]', 'active',
              '["document:launch-owner"]', '2026-07-31T13:00:00+00:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO cos_actions(
              id, action_type, status, target_fact_ids, target_page_paths,
              target_contract_ids, action_features, proposed_by,
              inverse_action_json, evidence_json, audit_status, created_at
            ) VALUES (?, 'fact_upsert', 'rejected', ?, ?, '[]', '{}',
                      'extractor', '{}', ?, 'unaudited', ?)
            """,
            (
                original_action["id"],
                json.dumps(original_action["target_fact_ids"]),
                json.dumps(original_action["target_page_paths"]),
                json.dumps(original_action["evidence_json"], sort_keys=True),
                original_action["created_at"],
            ),
        )
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, entity_id, page_hint, source_ids,
              source_spans, evidence_quote, confidence, status,
              confirmed_by_user, metadata, created_at, last_seen_at
            ) VALUES (
              'fact_schema28_manual_answer', ?, 'event:launch', 'entity_launch',
              'events/launch.md', ?, '[]', ?, 1.0, 'active', 1, ?,
              '2026-07-31T13:01:00+00:00',
              '2026-07-31T13:01:00+00:00'
            )
            """,
            (
                manual_statement,
                json.dumps([f"manual:question:{question_id}"]),
                manual_statement,
                json.dumps(
                    {"question_id": question_id},
                    sort_keys=True,
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO fact_entities(
              id, fact_id, entity_id, is_primary, mention_text, mention_span,
              mention_kind, resolution_method, confidence, created_at
            ) VALUES (
              'fact_entity_schema28_manual', 'fact_schema28_manual_answer',
              'entity_launch', 1, 'launch', NULL, 'event', 'exact', 1.0,
              '2026-07-31T13:01:00+00:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, fact_ids, question, options, status, answer, context,
              created_at, answered_at, action_id, decided_by
            ) VALUES (
              ?, 'fact_conflict_review', '["fact_schema28_original_answer"]',
              'Who owns the launch?', '[]', 'answered', ?, ?,
              '2026-07-31T13:00:00+00:00',
              '2026-07-31T13:01:00+00:00', ?, 'human'
            )
            """,
            (
                question_id,
                json.dumps(
                    {
                        "decision": "manual_answer",
                        "selected_fact_id": "fact_schema28_manual_answer",
                        "answer": manual_statement,
                    },
                    sort_keys=True,
                ),
                json.dumps(
                    {"candidate_action_id": original_action["id"]}, sort_keys=True
                ),
                original_action["id"],
            ),
        )
        legacy_identity = schema_28_fact_review_identity(original_fact)
        conn.execute(
            """
            INSERT INTO review_resolutions(
              id, family_key, portable_key, state_fingerprint, group_key,
              disposition, source_item_kind, source_item_id, decision_payload,
              decided_by, resolved_at, revoked_at
            ) VALUES (
              'resolution_schema28_manual_reject', ?, ?, ?, ?, 'reject',
              'question', ?, '{"decision":"manual_answer"}', 'human',
              '2026-07-31T13:01:00+00:00', NULL
            )
            """,
            (
                legacy_identity["family_key"],
                legacy_identity["portable_key"],
                legacy_identity["state_fingerprint"],
                legacy_identity["group_key"],
                question_id,
            ),
        )

        conn.execute("DELETE FROM schema_migrations WHERE version = 29")
        run_migrations(conn, [ENTITY_REVIEW_REKEY_MIGRATION])

        original_resolution = active_resolution_for_action(conn, original_action)
        manual_fact_row = conn.execute(
            "SELECT * FROM facts WHERE id = 'fact_schema28_manual_answer'"
        ).fetchone()
        manual_fact = dict(manual_fact_row)
        manual_fact["source_ids"] = json.loads(manual_fact["source_ids"])
        manual_fact["source_spans"] = json.loads(manual_fact["source_spans"])
        manual_fact["metadata"] = json.loads(manual_fact["metadata"])
        assert manual_fact["metadata"]["entity_attribution_snapshot"] == {
            "version": 1,
            "entity_id": "entity_launch",
            "entity_id_origin": "unknown",
            "mentions": [
                {
                    "surface": "launch",
                    "entity_identity": "Launch Event",
                    "entity_type": "event",
                    "mention_kind": "event",
                    "is_primary": True,
                }
            ],
        }
        manual_resolution = active_resolution_for_action(
            conn, synthetic_fact_action(manual_fact)
        )
        legacy_reject = conn.execute(
            """
            SELECT revoked_at FROM review_resolutions
            WHERE id = 'resolution_schema28_manual_reject'
            """
        ).fetchone()
        first = [
            tuple(row)
            for row in conn.execute(
                """
                SELECT family_key, state_fingerprint, disposition, revoked_at
                FROM review_resolutions
                ORDER BY id
                """
            )
        ]
        ENTITY_REVIEW_REKEY_MIGRATION[2](conn)
        second = [
            tuple(row)
            for row in conn.execute(
                """
                SELECT family_key, state_fingerprint, disposition, revoked_at
                FROM review_resolutions
                ORDER BY id
                """
            )
        ]

    assert original_resolution is not None
    assert original_resolution["disposition"] == "reject"
    assert manual_resolution is not None
    assert manual_resolution["disposition"] == "keep"
    assert legacy_reject["revoked_at"] is not None
    assert second == first


def test_entity_review_rekey_separates_cross_entity_supersession_chain(
    tmp_path: Path,
) -> None:
    from pkm_brain.review_resolution import (
        action_review_identity,
        active_resolution_for_action,
    )

    db_path = tmp_path / "brain.sqlite"
    init_db(db_path)
    actions: list[dict[str, object]] = []
    with connection(db_path) as conn:
        for index, (label, entity_id) in enumerate(
            (("Apollo", "entity_apollo"), ("Gemini", "entity_gemini"))
        ):
            fact = {
                "id": f"fact_{label.lower()}",
                "statement": "The launch review is scheduled.",
                "entity_id": entity_id,
                "entity_mentions": [
                    {
                        "surface": "launch",
                        "entity_identity": label,
                        "entity_type": "event",
                        "is_primary": True,
                    }
                ],
                "source_ids": ["document:shared-launch-review"],
                "source_spans": [],
                "evidence_quote": "The launch review is scheduled.",
                "metadata": {},
            }
            action = {
                "id": f"action_{label.lower()}",
                "action_type": "fact_upsert",
                "status": "applied" if index == 0 else "rejected",
                "target_fact_ids": [fact["id"]],
                "target_page_paths": [],
                "target_contract_ids": [],
                "action_features": {},
                "evidence_json": {"payload": {"fact": fact}},
                "created_at": f"2026-07-31T14:0{index}:00+00:00",
            }
            actions.append(action)
            conn.execute(
                """
                INSERT INTO cos_actions(
                  id, action_type, status, target_fact_ids, target_page_paths,
                  target_contract_ids, action_features, proposed_by,
                  inverse_action_json, evidence_json, audit_status, created_at
                ) VALUES (?, 'fact_upsert', ?, ?, '[]', '[]', '{}',
                          'extractor', '{}', ?, 'unaudited', ?)
                """,
                (
                    action["id"],
                    action["status"],
                    json.dumps(action["target_fact_ids"]),
                    json.dumps(action["evidence_json"], sort_keys=True),
                    action["created_at"],
                ),
            )

        legacy = schema_28_fact_review_identity(
            actions[0]["evidence_json"]["payload"]["fact"]  # type: ignore[index]
        )
        conn.execute(
            """
            INSERT INTO review_resolutions(
              id, family_key, portable_key, state_fingerprint, group_key,
              disposition, source_item_kind, source_item_id, decision_payload,
              decided_by, resolved_at, revoked_at
            ) VALUES (
              'resolution_cross_apollo', ?, ?, ?, ?, 'keep', 'action',
              'action_apollo', '{}', 'human',
              '2026-07-31T14:10:00+00:00',
              '2026-07-31T14:11:00+00:00'
            )
            """,
            (
                legacy["family_key"],
                legacy["portable_key"],
                legacy["state_fingerprint"],
                legacy["group_key"],
            ),
        )
        conn.execute(
            """
            INSERT INTO review_resolutions(
              id, family_key, portable_key, state_fingerprint, group_key,
              disposition, source_item_kind, source_item_id, decision_payload,
              decided_by, resolved_at, revoked_at
            ) VALUES (
              'resolution_cross_gemini', ?, ?, ?, ?, 'reject', 'action',
              'action_gemini', ?, 'human',
              '2026-07-31T14:11:00+00:00', NULL
            )
            """,
            (
                legacy["family_key"],
                legacy["portable_key"],
                legacy["state_fingerprint"],
                legacy["group_key"],
                json.dumps({"superseded_resolution_id": "resolution_cross_apollo"}),
            ),
        )

        conn.execute("DELETE FROM schema_migrations WHERE version = 29")
        run_migrations(conn, [ENTITY_REVIEW_REKEY_MIGRATION])

        apollo = active_resolution_for_action(conn, actions[0])
        gemini = active_resolution_for_action(conn, actions[1])
        assert apollo is not None and apollo["disposition"] == "keep"
        assert gemini is not None and gemini["disposition"] == "reject"
        assert "superseded_resolution_id" not in gemini["decision_payload"]
        assert (
            gemini["decision_payload"]["identity_migration"][
                "separated_superseded_resolution_id"
            ]
            == apollo["id"]
        )
        assert (
            action_review_identity(actions[0])["state_fingerprint"]
            != action_review_identity(actions[1])["state_fingerprint"]
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM review_resolutions WHERE revoked_at IS NULL"
            ).fetchone()[0]
            == 2
        )


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
