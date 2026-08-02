from __future__ import annotations

import hashlib
import json
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


def _migration_008_create_wiki_page_snapshots(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_page_snapshots (
          id TEXT PRIMARY KEY,
          page_path TEXT NOT NULL,
          before_markdown TEXT,
          after_markdown TEXT,
          before_exists INTEGER NOT NULL DEFAULT 0,
          after_exists INTEGER NOT NULL DEFAULT 0,
          reason TEXT NOT NULL,
          metadata TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wiki_page_snapshots_page
        ON wiki_page_snapshots(page_path, created_at)
        """
    )


def _migration_009_enrich_facts(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "facts"):
        return
    _ensure_column(conn, "facts", "source_spans", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "facts", "evidence_quote", "TEXT")
    _ensure_column(
        conn,
        "facts",
        "extraction_method",
        "TEXT NOT NULL DEFAULT 'legacy'",
    )
    _ensure_column(conn, "facts", "extractor_model", "TEXT")
    _ensure_column(conn, "facts", "effective_at", "TEXT")
    _ensure_column(conn, "facts", "extraction_confidence", "REAL")
    _ensure_column(conn, "facts", "routing_confidence", "REAL")
    _ensure_column(conn, "facts", "truth_confidence", "REAL")
    conn.execute(
        """
        UPDATE facts
        SET truth_confidence = COALESCE(truth_confidence, confidence),
            extraction_method = COALESCE(NULLIF(extraction_method, ''), 'legacy'),
            source_spans = COALESCE(NULLIF(source_spans, ''), '[]')
        WHERE truth_confidence IS NULL
           OR extraction_method IS NULL
           OR extraction_method = ''
           OR source_spans IS NULL
           OR source_spans = ''
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_facts_truth
        ON facts(status, truth_confidence)
        """
    )


def _migration_010_create_cos_actions(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cos_actions (
          id TEXT PRIMARY KEY,
          run_id TEXT REFERENCES wiki_curation_runs(id),
          action_type TEXT NOT NULL,
          status TEXT NOT NULL,
          target_fact_ids TEXT NOT NULL DEFAULT '[]',
          target_page_paths TEXT NOT NULL DEFAULT '[]',
          target_contract_ids TEXT NOT NULL DEFAULT '[]',
          action_features TEXT NOT NULL DEFAULT '{}',
          proposed_by TEXT,
          critic_by TEXT,
          critic_decision TEXT,
          confidence REAL,
          risk_tier TEXT,
          policy_id TEXT,
          policy_version INTEGER,
          policy_decision TEXT,
          autonomy_level TEXT,
          inverse_action_json TEXT NOT NULL DEFAULT '{}',
          evidence_json TEXT NOT NULL DEFAULT '{}',
          applied_state_hash TEXT,
          audit_status TEXT NOT NULL DEFAULT 'unaudited',
          created_at TEXT NOT NULL,
          applied_at TEXT,
          reverted_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cos_actions_status
        ON cos_actions(status, created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cos_actions_run
        ON cos_actions(run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cos_actions_audit
        ON cos_actions(audit_status, applied_at)
        """
    )


def _migration_011_create_cos_policy(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cos_policy (
          id TEXT PRIMARY KEY,
          version INTEGER NOT NULL,
          priority INTEGER NOT NULL,
          match_action_types TEXT NOT NULL DEFAULT '["*"]',
          match_predicate TEXT NOT NULL DEFAULT '{}',
          autonomy_level TEXT NOT NULL,
          critic_required INTEGER NOT NULL DEFAULT 0,
          timeout_allowed INTEGER NOT NULL DEFAULT 0,
          timeout_after_seconds INTEGER,
          audit_sample_rate REAL NOT NULL DEFAULT 0.0,
          demotion_threshold REAL,
          auto_revert_signals TEXT NOT NULL DEFAULT '[]',
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cos_policy_active
        ON cos_policy(active, version, priority)
        """
    )
    created_at = now_iso()
    seed_rows = [
        (
            "policy_v1_l0_noop_canonical",
            1,
            10,
            '["canonicalize_page"]',
            '{"eq":{"deterministic":true},"lte":{"risk_score":0.05},"exists":{"target_page_paths":true}}',
            "L0",
            0,
            0,
            None,
            0.05,
            0.02,
            '["audit_sampled_bad"]',
            1,
            created_at,
        ),
        (
            "policy_v1_l3_truth_resolution",
            1,
            20,
            '["resolve_conflict","fact_supersede"]',
            '{"eq":{"truth_mutation":true}}',
            "L3",
            0,
            0,
            None,
            1.0,
            None,
            "[]",
            1,
            created_at,
        ),
        (
            "policy_v1_l3_all_writes",
            1,
            1000,
            '["*"]',
            "{}",
            "L3",
            0,
            0,
            None,
            1.0,
            None,
            "[]",
            1,
            created_at,
        ),
    ]
    conn.executemany(
        """
        INSERT OR IGNORE INTO cos_policy(
          id, version, priority, match_action_types, match_predicate, autonomy_level,
          critic_required, timeout_allowed, timeout_after_seconds, audit_sample_rate,
          demotion_threshold, auto_revert_signals, active, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        seed_rows,
    )


def _migration_012_create_page_contracts(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS page_contracts (
          id TEXT PRIMARY KEY,
          page_hint TEXT NOT NULL,
          canonical_entity TEXT,
          page_scope TEXT,
          retrieval_purpose TEXT,
          what_belongs_here TEXT,
          what_does_not_belong_here TEXT,
          freshness_policy TEXT,
          related_pages TEXT NOT NULL DEFAULT '[]',
          version INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL DEFAULT 'active',
          created_at TEXT NOT NULL,
          updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_page_contracts_page
        ON page_contracts(page_hint, status)
        """
    )


def _migration_013_create_wiki_page_syntheses(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_page_syntheses (
          id TEXT PRIMARY KEY,
          page_hint TEXT NOT NULL,
          synthesis_markdown TEXT NOT NULL,
          fact_ids TEXT NOT NULL DEFAULT '[]',
          fact_hash TEXT,
          model TEXT,
          prompt_version TEXT,
          generated_at TEXT NOT NULL,
          stale INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wiki_page_syntheses_page
        ON wiki_page_syntheses(page_hint, generated_at)
        """
    )


def _migration_014_extend_open_questions(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "open_questions"):
        return
    _ensure_column(conn, "open_questions", "action_id", "TEXT")
    _ensure_column(
        conn,
        "open_questions",
        "recommended_action",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    _ensure_column(conn, "open_questions", "auto_resolve_after", "TEXT")
    _ensure_column(conn, "open_questions", "risk_tier", "TEXT")
    _ensure_column(conn, "open_questions", "resolver", "TEXT")
    _ensure_column(conn, "open_questions", "decided_by", "TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_open_questions_action
        ON open_questions(action_id)
        """
    )


def _migration_015_create_shared_retrieval_fts(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS retrieval_fts USING fts5(
          kind UNINDEXED,
          target_id UNINDEXED,
          title,
          text,
          heading_path,
          project,
          tags
        )
        """
    )
    conn.execute("DELETE FROM retrieval_fts WHERE kind IN ('chunk', 'fact')")
    if _table_exists(conn, "chunk_fts"):
        conn.execute(
            """
            INSERT INTO retrieval_fts(kind, target_id, title, text, heading_path, project, tags)
            SELECT 'chunk', chunk_id, title, text, heading_path, project, tags
            FROM chunk_fts
            """
        )
    elif _table_exists(conn, "chunks") and _table_exists(conn, "documents"):
        conn.execute(
            """
            INSERT INTO retrieval_fts(kind, target_id, title, text, heading_path, project, tags)
            SELECT 'chunk', c.id, d.title, c.text, c.heading_path,
                   COALESCE(d.project, ''), COALESCE(d.tags, '[]')
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            """
        )
    if _table_exists(conn, "facts"):
        conn.execute(
            """
            INSERT INTO retrieval_fts(kind, target_id, title, text, heading_path, project, tags)
            SELECT 'fact', id, COALESCE(page_hint, entity_key), statement,
                   COALESCE(section_hint, ''), '', COALESCE(source_ids, '[]')
            FROM facts
            WHERE status IN ('active', 'conflicted', 'needs_confirmation')
            """
        )


def _migration_016_context_lineage_fact_target(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "context_lineage_events"):
        return
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_context_lineage_fact
        ON context_lineage_events(target_id, event_type, created_at)
        WHERE target_type = 'fact'
        """
    )


def _migration_017_create_cos_stage_watermarks(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cos_stage_watermarks (
          id TEXT PRIMARY KEY,
          stage TEXT NOT NULL,
          document_id TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          model TEXT,
          prompt_version TEXT NOT NULL,
          status TEXT NOT NULL,
          run_id TEXT,
          processed_at TEXT NOT NULL,
          metadata TEXT NOT NULL DEFAULT '{}',
          UNIQUE(stage, document_id, content_hash, model, prompt_version)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cos_stage_watermarks_stage_doc
        ON cos_stage_watermarks(stage, document_id, status)
        """
    )


def _migration_018_entity_identity(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS entities (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          entity_type TEXT,
          aliases TEXT NOT NULL DEFAULT '[]',
          status TEXT NOT NULL DEFAULT 'active',
          merged_into TEXT,
          description TEXT,
          source_ids TEXT NOT NULL DEFAULT '[]',
          created_at TEXT NOT NULL
        )
        """
    )
    _ensure_column(conn, "entities", "aliases", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "entities", "status", "TEXT NOT NULL DEFAULT 'active'")
    _ensure_column(conn, "entities", "merged_into", "TEXT")
    _ensure_column(conn, "entities", "description", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_status ON entities(status)")
    if not _table_exists(conn, "facts"):
        return
    _ensure_column(conn, "facts", "entity_id", "TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_facts_entity_id_status
        ON facts(entity_id, status, observed_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fact_entities (
          id TEXT PRIMARY KEY,
          fact_id TEXT NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
          entity_id TEXT NOT NULL REFERENCES entities(id),
          is_primary INTEGER NOT NULL DEFAULT 0,
          mention_text TEXT,
          mention_span TEXT,
          mention_kind TEXT,
          resolution_method TEXT,
          confidence REAL,
          created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_fact_entities_fact
        ON fact_entities(fact_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_fact_entities_entity
        ON fact_entities(entity_id)
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_entities_primary
        ON fact_entities(fact_id)
        WHERE is_primary = 1
        """
    )


def _migration_019_fact_entity_mention_kind(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "fact_entities"):
        return
    _ensure_column(conn, "fact_entities", "mention_kind", "TEXT")


def _migration_020_document_source_stats(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "documents"):
        return
    _ensure_column(conn, "documents", "source_mtime_ns", "INTEGER")
    _ensure_column(conn, "documents", "source_size", "INTEGER")


def _migration_021_review_admission(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS review_admissions (
          item_key TEXT PRIMARY KEY,
          item_kind TEXT NOT NULL,
          group_name TEXT NOT NULL,
          state TEXT NOT NULL CHECK(state IN ('admitted', 'deferred')),
          priority INTEGER NOT NULL,
          admission_reason TEXT NOT NULL,
          first_seen_at TEXT NOT NULL,
          admitted_at TEXT,
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_review_admissions_state_priority
        ON review_admissions(state, priority, first_seen_at)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS review_admission_meta (
          singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
          initialized_at TEXT NOT NULL
        )
        """
    )


def _migration_022_temporal_facts(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "facts"):
        return
    _ensure_column(
        conn,
        "facts",
        "temporal_kind",
        "TEXT NOT NULL DEFAULT 'unknown'",
    )
    _ensure_column(conn, "facts", "valid_from", "TEXT")
    _ensure_column(conn, "facts", "valid_to", "TEXT")
    _ensure_column(
        conn,
        "facts",
        "valid_time_precision",
        "TEXT NOT NULL DEFAULT 'unknown'",
    )
    _ensure_column(conn, "facts", "temporal_expression", "TEXT")
    _ensure_column(conn, "facts", "temporal_confidence", "REAL")
    _ensure_column(conn, "facts", "knowledge_to", "TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_facts_valid_time
        ON facts(temporal_kind, valid_from, valid_to, status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_facts_knowledge_time
        ON facts(created_at, knowledge_to, status)
        """
    )
    if _table_exists(conn, "retrieval_fts"):
        conn.execute("DELETE FROM retrieval_fts WHERE kind = 'fact'")
        conn.execute(
            """
            INSERT INTO retrieval_fts(
              kind, target_id, title, text, heading_path, project, tags
            )
            SELECT 'fact', id, COALESCE(page_hint, entity_key),
                   TRIM(statement || ' ' || COALESCE(temporal_expression, '')),
                   COALESCE(section_hint, ''), '', COALESCE(source_ids, '[]')
            FROM facts
            WHERE status IN (
              'active', 'conflicted', 'needs_confirmation', 'superseded',
              'revision_closed'
            )
            """
        )


def _migration_023_fact_revisions(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "facts"):
        return
    _ensure_column(conn, "facts", "assertion_lineage_id", "TEXT")
    _ensure_column(conn, "facts", "revision_of_id", "TEXT")
    _ensure_column(
        conn,
        "facts",
        "revision_number",
        "INTEGER NOT NULL DEFAULT 1",
    )
    _ensure_column(conn, "facts", "revision_status", "TEXT")
    conn.execute(
        """
        UPDATE facts
        SET assertion_lineage_id = id,
            revision_number = 1
        WHERE assertion_lineage_id IS NULL
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_assertion_revision
        ON facts(assertion_lineage_id, revision_number)
        WHERE assertion_lineage_id IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_facts_revision_of
        ON facts(revision_of_id)
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_open_assertion_lineage
        ON facts(assertion_lineage_id)
        WHERE assertion_lineage_id IS NOT NULL AND knowledge_to IS NULL
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_facts_default_assertion_lineage
        AFTER INSERT ON facts
        WHEN NEW.assertion_lineage_id IS NULL
        BEGIN
          UPDATE facts
          SET assertion_lineage_id = NEW.id, revision_number = 1
          WHERE id = NEW.id;
        END
        """
    )


def _migration_024_event_time(conn: sqlite3.Connection) -> None:
    """Add one optional event-time envelope to an ordinary fact row.

    Migration 22 remains readable for predicate-validity compatibility.  Event
    time is deliberately separate: a past occurrence or future plan does not
    make the underlying fact non-current.
    """

    if not _table_exists(conn, "facts"):
        return
    _ensure_column(conn, "facts", "event_time_kind", "TEXT")
    _ensure_column(conn, "facts", "event_start_at", "TEXT")
    _ensure_column(conn, "facts", "event_end_at", "TEXT")
    _ensure_column(conn, "facts", "event_time_precision", "TEXT")
    _ensure_column(conn, "facts", "event_time_expression", "TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_facts_event_time
        ON facts(event_time_kind, event_start_at, event_end_at, status)
        """
    )
    if _table_exists(conn, "retrieval_fts"):
        conn.execute("DELETE FROM retrieval_fts WHERE kind = 'fact'")
        conn.execute(
            """
            INSERT INTO retrieval_fts(
              kind, target_id, title, text, heading_path, project, tags
            )
            SELECT 'fact', id, COALESCE(page_hint, entity_key),
                   TRIM(statement || ' ' || COALESCE(temporal_expression, '') ||
                        ' ' || COALESCE(event_time_expression, '') ||
                        ' ' || COALESCE(event_start_at, '') ||
                        ' ' || COALESCE(event_end_at, '')),
                   COALESCE(section_hint, ''), '', COALESCE(source_ids, '[]')
            FROM facts
            WHERE status IN (
              'active', 'conflicted', 'needs_confirmation', 'superseded',
              'revision_closed'
            )
            """
        )


def _migration_025_gmail_temporal_review_persistence(
    conn: sqlite3.Connection,
) -> None:
    """Persist only complete, non-routable Gmail temporal review projections.

    Runs and artifacts are an append-only evidence ledger.  The mutable head is
    deliberately separate so replay and rollback never rewrite verifier output.
    """

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gmail_temporal_review_runs (
          id TEXT PRIMARY KEY,
          input_key TEXT NOT NULL UNIQUE,
          message_scope_key TEXT NOT NULL,
          pipeline_scope TEXT NOT NULL,
          document_id TEXT NOT NULL REFERENCES documents(id),
          document_content_hash TEXT NOT NULL,
          gmail_account_key TEXT NOT NULL,
          gmail_thread_id TEXT NOT NULL,
          gmail_source_revision TEXT NOT NULL,
          gmail_message_id TEXT NOT NULL,
          message_internal_at TEXT NOT NULL,
          message_start_offset INTEGER NOT NULL,
          message_end_offset INTEGER NOT NULL,
          source_sha256 TEXT NOT NULL,
          source_locator_hash TEXT NOT NULL,
          source_locator_json TEXT NOT NULL,
          projection_version TEXT NOT NULL,
          analysis_fingerprint TEXT NOT NULL,
          batch_plan_fingerprint TEXT NOT NULL,
          ensemble_policy_fingerprint TEXT NOT NULL,
          grouping_policy_fingerprint TEXT NOT NULL,
          projection_fingerprint TEXT NOT NULL,
          projection_sha256 TEXT NOT NULL,
          artifact_set_sha256 TEXT NOT NULL,
          projection_json TEXT NOT NULL,
          complete INTEGER NOT NULL CHECK(complete = 1),
          routable INTEGER NOT NULL CHECK(routable = 0),
          created_at TEXT NOT NULL,
          CHECK(message_start_offset >= 0),
          CHECK(message_end_offset > message_start_offset),
          UNIQUE(id, message_scope_key, pipeline_scope),
          UNIQUE(message_scope_key, pipeline_scope, input_key)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gmail_temporal_review_runs_scope
        ON gmail_temporal_review_runs(message_scope_key, pipeline_scope, created_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gmail_temporal_review_runs_document
        ON gmail_temporal_review_runs(document_id, document_content_hash)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gmail_temporal_review_artifacts (
          id TEXT PRIMARY KEY,
          run_id TEXT NOT NULL REFERENCES gmail_temporal_review_runs(id),
          artifact_kind TEXT NOT NULL CHECK(
            artifact_kind IN (
              'supported_citation', 'uncertainty_sidecar', 'cluster_review'
            )
          ),
          source_artifact_key TEXT NOT NULL,
          candidate_authorization INTEGER NOT NULL CHECK(
            candidate_authorization IN (0, 1)
          ),
          payload_sha256 TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          routable INTEGER NOT NULL CHECK(routable = 0),
          created_at TEXT NOT NULL,
          UNIQUE(run_id, artifact_kind, source_artifact_key)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gmail_temporal_review_artifacts_run
        ON gmail_temporal_review_artifacts(run_id, artifact_kind)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gmail_temporal_review_heads (
          message_scope_key TEXT NOT NULL,
          pipeline_scope TEXT NOT NULL,
          run_id TEXT,
          generation INTEGER NOT NULL CHECK(generation >= 1),
          updated_at TEXT NOT NULL,
          PRIMARY KEY(message_scope_key, pipeline_scope),
          FOREIGN KEY(run_id, message_scope_key, pipeline_scope)
            REFERENCES gmail_temporal_review_runs(
              id, message_scope_key, pipeline_scope
            )
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gmail_temporal_review_heads_run
        ON gmail_temporal_review_heads(run_id)
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_gmail_temporal_review_runs_no_update
        BEFORE UPDATE ON gmail_temporal_review_runs
        BEGIN
          SELECT RAISE(ABORT, 'gmail temporal review runs are immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_gmail_temporal_review_runs_no_delete
        BEFORE DELETE ON gmail_temporal_review_runs
        BEGIN
          SELECT RAISE(ABORT, 'gmail temporal review runs are immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_gmail_temporal_review_artifacts_no_update
        BEFORE UPDATE ON gmail_temporal_review_artifacts
        BEGIN
          SELECT RAISE(ABORT, 'gmail temporal review artifacts are immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_gmail_temporal_review_artifacts_no_delete
        BEFORE DELETE ON gmail_temporal_review_artifacts
        BEGIN
          SELECT RAISE(ABORT, 'gmail temporal review artifacts are immutable');
        END
        """
    )


def _migration_026_gmail_temporal_runner_evidence(
    conn: sqlite3.Connection,
) -> None:
    """Bind review outcomes to the authoritative runner and component evidence."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gmail_temporal_review_executions (
          id TEXT PRIMARY KEY,
          input_key TEXT NOT NULL UNIQUE,
          message_scope_key TEXT NOT NULL,
          pipeline_scope TEXT NOT NULL,
          document_id TEXT NOT NULL REFERENCES documents(id),
          document_content_hash TEXT NOT NULL,
          source_sha256 TEXT NOT NULL,
          source_locator_hash TEXT NOT NULL,
          runner_policy_fingerprint TEXT NOT NULL,
          admission_policy_fingerprint TEXT NOT NULL,
          verifier_policy_fingerprint TEXT NOT NULL,
          sanitizer_version INTEGER NOT NULL CHECK(sanitizer_version >= 1),
          provider TEXT NOT NULL,
          model TEXT NOT NULL,
          reasoning_effort TEXT NOT NULL,
          admission_basis TEXT NOT NULL CHECK(
            admission_basis IN ('fact', 'temporal_rescue', 'not_admitted')
          ),
          disposition TEXT NOT NULL CHECK(
            disposition IN (
              'complete_review_projection', 'no_recognized_expression',
              'no_verification_candidate', 'not_admitted'
            )
          ),
          target_fingerprint TEXT NOT NULL,
          analysis_fingerprint TEXT NOT NULL,
          batch_plan_fingerprint TEXT NOT NULL,
          expression_count INTEGER NOT NULL CHECK(expression_count >= 0),
          batch_count INTEGER NOT NULL CHECK(batch_count >= 0),
          candidate_count INTEGER NOT NULL CHECK(candidate_count >= 0),
          page_count INTEGER NOT NULL CHECK(page_count >= 0),
          request_count INTEGER NOT NULL CHECK(request_count >= 0),
          component_count INTEGER NOT NULL CHECK(component_count IN (0, 3)),
          request_set_sha256 TEXT NOT NULL,
          component_set_sha256 TEXT NOT NULL,
          invocation_attestation TEXT NOT NULL CHECK(
            invocation_attestation = 'self_reported_external_invocation'
          ),
          independent_invocations_verified INTEGER NOT NULL CHECK(
            independent_invocations_verified = 0
          ),
          review_run_id TEXT UNIQUE REFERENCES gmail_temporal_review_runs(id),
          complete INTEGER NOT NULL CHECK(complete = 1),
          routable INTEGER NOT NULL CHECK(routable = 0),
          created_at TEXT NOT NULL,
          CHECK(
            (disposition = 'complete_review_projection'
             AND component_count = 3 AND review_run_id IS NOT NULL)
            OR
            (disposition != 'complete_review_projection'
             AND component_count = 0 AND review_run_id IS NULL)
          ),
          UNIQUE(message_scope_key, pipeline_scope, input_key)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_gmail_temporal_review_executions_scope
        ON gmail_temporal_review_executions(
          message_scope_key, pipeline_scope, created_at
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gmail_temporal_review_components (
          execution_id TEXT NOT NULL
            REFERENCES gmail_temporal_review_executions(id),
          run_ordinal INTEGER NOT NULL CHECK(run_ordinal BETWEEN 1 AND 3),
          invocation_id TEXT NOT NULL,
          started_at TEXT NOT NULL,
          completed_at TEXT NOT NULL,
          artifact_sha256 TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          routable INTEGER NOT NULL CHECK(routable = 0),
          created_at TEXT NOT NULL,
          PRIMARY KEY(execution_id, run_ordinal),
          UNIQUE(execution_id, invocation_id),
          UNIQUE(execution_id, artifact_sha256)
        )
        """
    )
    # Migration 25 could make a production review run current before the
    # authoritative runner receipt existed.  Keep that immutable evidence in
    # the ledger, but never carry its mutable head authority across the v26
    # trust-boundary upgrade.  The NOT EXISTS form also makes a direct,
    # idempotent invocation of this migration preserve already-attested heads.
    conn.execute(
        """
        DELETE FROM gmail_temporal_review_heads
        WHERE pipeline_scope = 'gmail_temporal_review_v1'
          AND run_id IS NOT NULL
          AND NOT EXISTS (
            SELECT 1
            FROM gmail_temporal_review_executions
            WHERE review_run_id = gmail_temporal_review_heads.run_id
              AND message_scope_key =
                    gmail_temporal_review_heads.message_scope_key
              AND pipeline_scope = gmail_temporal_review_heads.pipeline_scope
              AND disposition = 'complete_review_projection'
              AND complete = 1
              AND routable = 0
          )
        """
    )
    for table in (
        "gmail_temporal_review_executions",
        "gmail_temporal_review_components",
    ):
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN
              SELECT RAISE(ABORT, 'gmail temporal runner evidence is immutable');
            END
            """
        )
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN
              SELECT RAISE(ABORT, 'gmail temporal runner evidence is immutable');
            END
            """
        )


def _migration_027_review_resolution_ledger(conn: sqlite3.Connection) -> None:
    """Persist semantic human review decisions across regenerated action IDs."""

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS review_resolutions (
          id TEXT PRIMARY KEY,
          family_key TEXT NOT NULL,
          portable_key TEXT NOT NULL,
          state_fingerprint TEXT NOT NULL,
          group_key TEXT NOT NULL,
          disposition TEXT NOT NULL CHECK(disposition IN ('keep', 'reject')),
          source_item_kind TEXT NOT NULL,
          source_item_id TEXT NOT NULL,
          decision_payload TEXT NOT NULL DEFAULT '{}',
          decided_by TEXT NOT NULL,
          resolved_at TEXT NOT NULL,
          revoked_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_review_resolutions_active_state
        ON review_resolutions(family_key, state_fingerprint)
        WHERE revoked_at IS NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_review_resolutions_portable
        ON review_resolutions(portable_key, resolved_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_review_resolutions_group
        ON review_resolutions(group_key, resolved_at)
        """
    )
    from .review_resolution import backfill_review_resolutions

    backfill_review_resolutions(conn)


def _migration_028_finalize_review_resolution_backfill(
    conn: sqlite3.Connection,
) -> None:
    """Repair the pre-release v27 question backfill and fill legacy fact decisions."""

    from .review_resolution import backfill_review_resolutions

    backfill_review_resolutions(conn)


def _migration_029_rekey_entity_review_resolutions(
    conn: sqlite3.Connection,
) -> None:
    """Move schema-28 decisions onto the entity-aware fact identity.

    Schema 29 makes fact entity attribution material to semantic review identity.
    The ordinary backfill reconstructs confirmed facts, questions, and audit
    decisions under that identity.  Direct Queue decisions have no other durable
    provenance, so the remaining active ledger rows are rekeyed from their source
    action.  The legacy row is retained as revoked history and a deterministic
    successor carries the same human decision under the current identity.
    """

    if not _table_exists(conn, "review_resolutions"):
        return

    from .review_resolution import (
        action_by_id,
        action_review_identity,
        backfill_review_resolutions,
        decoded_fact_row,
        original_question_action,
        synthetic_fact_action,
    )

    _backfill_fact_entity_attribution_snapshots(conn)

    # Reconstruct every source that already has first-class historical
    # provenance before handling direct action decisions.  Besides preserving
    # the richer question plans, this also ensures legacy free-text answers are
    # expanded into their reject-original/keep-manual pair by the current
    # backfill contract.
    backfill_review_resolutions(conn, restore_superseded_on_revoke=False)

    migrated_at = now_iso()
    active_rows = list(
        conn.execute(
            """
            SELECT *
            FROM review_resolutions
            WHERE revoked_at IS NULL
            ORDER BY resolved_at, id
            """
        )
    )
    active_question_counts = {
        str(row["source_item_id"]): int(row["resolution_count"])
        for row in conn.execute(
            """
            SELECT source_item_id, COUNT(*) AS resolution_count
            FROM review_resolutions
            WHERE revoked_at IS NULL AND source_item_kind = 'question'
            GROUP BY source_item_id
            """
        )
    }
    for row in active_rows:
        action = None
        source_kind = str(row["source_item_kind"] or "")
        source_id = str(row["source_item_id"] or "")
        if source_kind in {"action", "audit"}:
            action = action_by_id(conn, source_id)
        elif source_kind == "fact_confirmation" and _table_exists(conn, "facts"):
            fact_row = conn.execute(
                "SELECT * FROM facts WHERE id = ?", (source_id,)
            ).fetchone()
            if fact_row is not None:
                action = synthetic_fact_action(decoded_fact_row(fact_row))
        elif (
            source_kind == "question"
            and active_question_counts.get(source_id) == 1
            and _table_exists(conn, "open_questions")
        ):
            # Multi-resolution alternative questions are deliberately left to
            # backfill_review_resolutions above.  A single residual row can be
            # mapped safely to the reviewed candidate even if old provenance is
            # too sparse for the generic backfill to recreate it.
            question_row = conn.execute(
                "SELECT * FROM open_questions WHERE id = ?", (source_id,)
            ).fetchone()
            if question_row is not None:
                from .review_resolution import decoded_question_row

                action = original_question_action(
                    conn, decoded_question_row(question_row)
                )
        if action is None:
            continue
        identity = action_review_identity(action)
        _rekey_active_review_resolution(
            conn,
            row,
            identity=identity,
            migrated_at=migrated_at,
        )


def _rekey_active_review_resolution(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    identity: dict[str, str],
    migrated_at: str,
) -> None:
    """Rekey one active resolution without violating the partial unique index."""

    old_id = str(row["id"])
    identity_columns = (
        "family_key",
        "portable_key",
        "state_fingerprint",
        "group_key",
    )
    if all(str(row[column]) == identity[column] for column in identity_columns):
        return

    existing = conn.execute(
        """
        SELECT *
        FROM review_resolutions
        WHERE family_key = ? AND state_fingerprint = ?
          AND revoked_at IS NULL
        LIMIT 1
        """,
        (identity["family_key"], identity["state_fingerprint"]),
    ).fetchone()
    if existing is not None and str(existing["id"]) == old_id:
        conn.execute(
            """
            UPDATE review_resolutions
            SET portable_key = ?, group_key = ?
            WHERE id = ? AND revoked_at IS NULL
            """,
            (identity["portable_key"], identity["group_key"], old_id),
        )
        return

    # A current-identity row can already exist when the standard backfill
    # reconstructed this source.  Keep a strictly newer decision.  On equal
    # timestamps, preserve the released ledger row being migrated; its explicit
    # disposition is stronger evidence than a newly derived backfill row.
    if existing is not None and str(existing["resolved_at"] or "") > str(
        row["resolved_at"] or ""
    ):
        conn.execute(
            """
            UPDATE review_resolutions SET revoked_at = ?
            WHERE id = ? AND revoked_at IS NULL
            """,
            (migrated_at, old_id),
        )
        return

    if existing is not None:
        conn.execute(
            """
            UPDATE review_resolutions SET revoked_at = ?
            WHERE id = ? AND revoked_at IS NULL
            """,
            (migrated_at, str(existing["id"])),
        )
    conn.execute(
        """
        UPDATE review_resolutions SET revoked_at = ?
        WHERE id = ? AND revoked_at IS NULL
        """,
        (migrated_at, old_id),
    )

    successor_id = _schema29_resolution_id(old_id, identity)
    decision_payload = _schema29_resolution_payload(
        conn,
        row,
        identity=identity,
        migrated_at=migrated_at,
        seen={old_id},
    )
    successor = conn.execute(
        "SELECT id FROM review_resolutions WHERE id = ?", (successor_id,)
    ).fetchone()
    values = (
        identity["family_key"],
        identity["portable_key"],
        identity["state_fingerprint"],
        identity["group_key"],
        str(row["disposition"]),
        str(row["source_item_kind"]),
        str(row["source_item_id"]),
        json.dumps(decision_payload, sort_keys=True),
        str(row["decided_by"]),
        str(row["resolved_at"]),
    )
    if successor is None:
        conn.execute(
            """
            INSERT INTO review_resolutions(
              id, family_key, portable_key, state_fingerprint, group_key,
              disposition, source_item_kind, source_item_id, decision_payload,
              decided_by, resolved_at, revoked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (successor_id, *values),
        )
    else:
        conn.execute(
            """
            UPDATE review_resolutions
            SET family_key = ?, portable_key = ?, state_fingerprint = ?,
                group_key = ?, disposition = ?, source_item_kind = ?,
                source_item_id = ?, decision_payload = ?, decided_by = ?,
                resolved_at = ?, revoked_at = NULL
            WHERE id = ?
            """,
            (*values, successor_id),
        )


def _backfill_fact_entity_attribution_snapshots(conn: sqlite3.Connection) -> None:
    """Persist recoverable schema-28 attribution and entity-ID provenance."""

    if not all(
        _table_exists(conn, table) for table in ("facts", "fact_entities", "entities")
    ):
        return
    from .fact_entity_attribution import (
        FACT_ENTITY_ATTRIBUTION_SNAPSHOT_KEY,
        FACT_ENTITY_ATTRIBUTION_SNAPSHOT_VERSION,
        fact_entity_attribution_snapshot,
        fact_entity_mentions,
    )

    action_inputs = _schema29_fact_upsert_inputs(conn)
    fact_entity_id_sql = (
        "f.entity_id" if "entity_id" in _table_columns(conn, "facts") else "NULL"
    )
    rows = conn.execute(
        f"""
        SELECT f.id AS fact_id, {fact_entity_id_sql} AS fact_entity_id, f.metadata,
               fe.id AS association_id, fe.is_primary, fe.mention_text,
               fe.mention_kind, e.name AS entity_name, e.entity_type
        FROM facts f
        LEFT JOIN fact_entities fe ON fe.fact_id = f.id
        LEFT JOIN entities e ON e.id = fe.entity_id
        ORDER BY f.id, fe.is_primary DESC, fe.id
        """
    )
    grouped: dict[str, dict[str, object]] = {}
    for row in rows:
        fact_id = str(row["fact_id"])
        item = grouped.setdefault(
            fact_id,
            {
                "entity_id": row["fact_entity_id"],
                "metadata": row["metadata"],
                "mentions": {},
            },
        )
        surface = str(row["mention_text"] or "").strip()
        if not surface:
            continue
        is_primary = bool(row["is_primary"])
        mention: dict[str, object] = {
            "surface": surface,
            "is_primary": is_primary,
        }
        entity_name = str(row["entity_name"] or "").strip()
        if is_primary and entity_name and entity_name.casefold() != surface.casefold():
            mention["entity_identity"] = entity_name
        entity_type = str(row["entity_type"] or "").strip()
        mention_kind = str(row["mention_kind"] or "").strip()
        if entity_type:
            mention["entity_type"] = entity_type
        if mention_kind:
            mention["mention_kind"] = mention_kind
        key = json.dumps(
            mention, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        )
        mentions = item["mentions"]
        if isinstance(mentions, dict):
            mentions.setdefault(key, mention)

    for fact_id, item in grouped.items():
        metadata = _decoded_json_object(item["metadata"])
        if metadata is None:
            continue
        current_input = action_inputs.get(fact_id)
        current_fact = (
            current_input.get("fact") if isinstance(current_input, dict) else None
        )
        existing_snapshot = fact_entity_attribution_snapshot(metadata)
        existing_origin = str((existing_snapshot or {}).get("entity_id_origin") or "")
        if isinstance(current_fact, dict):
            source_mentions = fact_entity_mentions(current_fact)
            recovered_origin = str(current_input.get("entity_id_origin") or "derived")
            recovered_authoritative = bool(
                current_input.get("entity_id_origin_authoritative")
            )
            entity_id_origin = (
                recovered_origin
                if recovered_authoritative
                or existing_origin not in {"derived", "explicit", "unknown"}
                else existing_origin
            )
        else:
            source_mentions = fact_entity_mentions({"metadata": metadata})
            entity_id_origin = (
                existing_origin
                if existing_origin in {"derived", "explicit", "unknown"}
                else "unknown"
            )
        if source_mentions:
            mentions = _schema29_snapshot_mentions(source_mentions)
        elif existing_snapshot is not None:
            mentions = list(existing_snapshot.get("mentions") or [])
        else:
            mention_map = item["mentions"]
            mentions = (
                sorted(
                    mention_map.values(),
                    key=lambda mention: (
                        not bool(mention.get("is_primary")),
                        json.dumps(
                            mention,
                            ensure_ascii=True,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    ),
                )
                if isinstance(mention_map, dict)
                else []
            )
        if (
            not mentions
            and existing_snapshot is None
            and not isinstance(current_fact, dict)
        ):
            continue
        entity_id = str(item.get("entity_id") or "").strip()
        metadata[FACT_ENTITY_ATTRIBUTION_SNAPSHOT_KEY] = {
            "version": FACT_ENTITY_ATTRIBUTION_SNAPSHOT_VERSION,
            "mentions": mentions,
            "entity_id_origin": entity_id_origin,
            **({"entity_id": entity_id} if entity_id else {}),
        }
        conn.execute(
            "UPDATE facts SET metadata = ? WHERE id = ?",
            (json.dumps(metadata, sort_keys=True), fact_id),
        )


def _schema29_fact_upsert_inputs(
    conn: sqlite3.Connection,
) -> dict[str, dict[str, object]]:
    """Recover each head's latest attribution input and ID provenance.

    Target lists can contain superseded history IDs, so only the payload fact's
    own ID is authoritative. Attribution-free updates are skipped, while older
    explicit-ID input remains available to a later mention-only update.
    """

    if not _table_exists(conn, "cos_actions"):
        return {}
    from .fact_entity_attribution import (
        FACT_ENTITY_ID_ORIGINS,
        fact_entity_attribution_input_present,
        fact_entity_attribution_snapshot,
        fact_entity_id_is_explicit,
    )

    result: dict[str, dict[str, object]] = {}
    for row in conn.execute(
        """
        SELECT proposed_by, evidence_json
        FROM cos_actions
        WHERE action_type = 'fact_upsert'
          AND status IN ('applied', 'auto_applied')
        ORDER BY COALESCE(applied_at, created_at) DESC, id DESC
        """
    ):
        if str(row["proposed_by"] or "") == "ui_fact_confirm":
            continue
        evidence = _json_object(row["evidence_json"])
        payload = evidence.get("payload")
        if not isinstance(payload, dict):
            continue
        raw_fact = (
            payload.get("fact") if isinstance(payload.get("fact"), dict) else payload
        )
        if not isinstance(raw_fact, dict):
            continue
        fact = dict(raw_fact)
        payload_id = str(fact.get("id") or "").strip()
        if not payload_id or not fact_entity_attribution_input_present(fact):
            continue
        item = result.setdefault(
            payload_id,
            {
                "fact": fact,
                "entity_id_origin": "derived",
                "entity_id_origin_authoritative": False,
            },
        )
        if bool(item.get("entity_id_origin_authoritative")):
            continue
        snapshot = fact_entity_attribution_snapshot(fact.get("metadata"))
        snapshot_origin = str((snapshot or {}).get("entity_id_origin") or "")
        snapshot_id = str((snapshot or {}).get("entity_id") or "").strip()
        supplied_id = str(fact.get("entity_id") or "").strip()
        if supplied_id:
            item["entity_id_origin"] = (
                "explicit" if fact_entity_id_is_explicit(fact) else "derived"
            )
            item["entity_id_origin_authoritative"] = True
        elif snapshot_origin in FACT_ENTITY_ID_ORIGINS and snapshot_id:
            item["entity_id_origin"] = snapshot_origin
            item["entity_id_origin_authoritative"] = True
    return result


def _schema29_snapshot_mentions(
    mentions: list[dict[str, object]],
) -> list[dict[str, object]]:
    by_key: dict[str, dict[str, object]] = {}
    for mention in mentions:
        is_primary = bool(mention.get("is_primary"))
        snapshot_mention = {
            key: value
            for key, value in {
                "surface": str(mention.get("surface") or "").strip(),
                "entity_identity": (
                    str(mention.get("entity_identity") or "").strip()
                    if is_primary
                    else ""
                ),
                "entity_type": mention.get("entity_type"),
                "mention_kind": mention.get("mention_kind"),
                "is_primary": is_primary,
            }.items()
            if value not in (None, "")
        }
        key = json.dumps(
            snapshot_mention,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        by_key.setdefault(key, snapshot_mention)
    return sorted(
        by_key.values(),
        key=lambda mention: (
            not bool(mention.get("is_primary")),
            json.dumps(
                mention,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ),
        ),
    )


def _schema29_resolution_payload(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    identity: dict[str, str],
    migrated_at: str,
    seen: set[str],
) -> dict[str, object]:
    old_id = str(row["id"])
    decision_payload = _json_object(row["decision_payload"])
    migration_provenance: dict[str, object] = {
        "from_resolution_id": old_id,
        "schema_version": 29,
    }
    superseded_id = str(decision_payload.get("superseded_resolution_id") or "")
    if superseded_id:
        predecessor = conn.execute(
            "SELECT * FROM review_resolutions WHERE id = ?", (superseded_id,)
        ).fetchone()
        predecessor_identity = (
            _schema29_resolution_identity(conn, predecessor)
            if predecessor is not None
            else None
        )
        same_identity = predecessor_identity is not None and all(
            predecessor_identity[key] == identity[key]
            for key in ("family_key", "state_fingerprint")
        )
        migrated_superseded_id = None
        if same_identity:
            migrated_superseded_id = _clone_schema29_resolution_history(
                conn,
                superseded_id,
                identity=predecessor_identity,
                migrated_at=migrated_at,
                seen=seen,
                active=False,
            )
            if migrated_superseded_id:
                decision_payload["superseded_resolution_id"] = migrated_superseded_id
        elif predecessor_identity is not None:
            migrated_superseded_id = _clone_schema29_resolution_history(
                conn,
                superseded_id,
                identity=predecessor_identity,
                migrated_at=migrated_at,
                seen=seen,
                active=True,
            )
            decision_payload.pop("superseded_resolution_id", None)
            migration_provenance["separated_superseded_resolution_id"] = (
                migrated_superseded_id or superseded_id
            )
            migration_provenance["legacy_superseded_resolution_id"] = superseded_id
        else:
            # Never let Undo reactivate an unreachable schema-28 state.
            decision_payload.pop("superseded_resolution_id", None)
            migration_provenance["legacy_superseded_resolution_id"] = superseded_id
    decision_payload["identity_migration"] = migration_provenance
    return decision_payload


def _clone_schema29_resolution_history(
    conn: sqlite3.Connection,
    legacy_id: str,
    *,
    identity: dict[str, str],
    migrated_at: str,
    seen: set[str],
    active: bool,
) -> str | None:
    if not legacy_id or legacy_id in seen:
        return None
    row = conn.execute(
        "SELECT * FROM review_resolutions WHERE id = ?", (legacy_id,)
    ).fetchone()
    if row is None:
        return None
    successor_id = _schema29_resolution_id(legacy_id, identity)
    decision_payload = _schema29_resolution_payload(
        conn,
        row,
        identity=identity,
        migrated_at=migrated_at,
        seen={*seen, legacy_id},
    )
    revoked_at: str | None = None if active else str(row["revoked_at"] or migrated_at)
    if active:
        active_existing = conn.execute(
            """
            SELECT id FROM review_resolutions
            WHERE family_key = ? AND state_fingerprint = ?
              AND revoked_at IS NULL
            LIMIT 1
            """,
            (identity["family_key"], identity["state_fingerprint"]),
        ).fetchone()
        if active_existing is not None and str(active_existing["id"]) != successor_id:
            return str(active_existing["id"])
    values = (
        identity["family_key"],
        identity["portable_key"],
        identity["state_fingerprint"],
        identity["group_key"],
        str(row["disposition"]),
        str(row["source_item_kind"]),
        str(row["source_item_id"]),
        json.dumps(decision_payload, sort_keys=True),
        str(row["decided_by"]),
        str(row["resolved_at"]),
        revoked_at,
    )
    existing = conn.execute(
        "SELECT id FROM review_resolutions WHERE id = ?", (successor_id,)
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO review_resolutions(
              id, family_key, portable_key, state_fingerprint, group_key,
              disposition, source_item_kind, source_item_id, decision_payload,
              decided_by, resolved_at, revoked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (successor_id, *values),
        )
    else:
        conn.execute(
            """
            UPDATE review_resolutions
            SET family_key = ?, portable_key = ?, state_fingerprint = ?,
                group_key = ?, disposition = ?, source_item_kind = ?,
                source_item_id = ?, decision_payload = ?, decided_by = ?,
                resolved_at = ?, revoked_at = ?
            WHERE id = ?
            """,
            (*values, successor_id),
        )
    return successor_id


def _schema29_resolution_identity(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> dict[str, str] | None:
    """Reconstruct a resolution's own identity from durable source state."""

    from .review_resolution import (
        action_by_id,
        action_review_identity,
        decoded_fact_row,
        synthetic_fact_action,
    )

    source_kind = str(row["source_item_kind"] or "")
    source_id = str(row["source_item_id"] or "")
    action = None
    if source_kind in {"action", "audit"}:
        action = action_by_id(conn, source_id)
    elif source_kind == "fact_confirmation" and _table_exists(conn, "facts"):
        fact_row = conn.execute(
            "SELECT * FROM facts WHERE id = ?", (source_id,)
        ).fetchone()
        if fact_row is not None:
            action = synthetic_fact_action(decoded_fact_row(fact_row))
    return action_review_identity(action) if action is not None else None


def _schema29_resolution_id(old_id: str, identity: dict[str, str]) -> str:
    return (
        "resolution_schema29_"
        + hashlib.sha256(
            (
                old_id
                + "\0"
                + identity["family_key"]
                + "\0"
                + identity["state_fingerprint"]
            ).encode("utf-8")
        ).hexdigest()[:24]
    )


def _json_object(value: object) -> dict[str, object]:
    return _decoded_json_object(value) or {}


def _decoded_json_object(value: object) -> dict[str, object] | None:
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return dict(decoded) if isinstance(decoded, dict) else None


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
    (8, "create_wiki_page_snapshots", _migration_008_create_wiki_page_snapshots),
    (9, "enrich_facts", _migration_009_enrich_facts),
    (10, "create_cos_actions", _migration_010_create_cos_actions),
    (11, "create_cos_policy", _migration_011_create_cos_policy),
    (12, "create_page_contracts", _migration_012_create_page_contracts),
    (13, "create_wiki_page_syntheses", _migration_013_create_wiki_page_syntheses),
    (14, "extend_open_questions", _migration_014_extend_open_questions),
    (15, "create_shared_retrieval_fts", _migration_015_create_shared_retrieval_fts),
    (16, "context_lineage_fact_target", _migration_016_context_lineage_fact_target),
    (17, "create_cos_stage_watermarks", _migration_017_create_cos_stage_watermarks),
    (18, "entity_identity", _migration_018_entity_identity),
    (19, "fact_entity_mention_kind", _migration_019_fact_entity_mention_kind),
    (20, "document_source_stats", _migration_020_document_source_stats),
    (21, "review_admission", _migration_021_review_admission),
    (22, "temporal_facts", _migration_022_temporal_facts),
    (23, "fact_revisions", _migration_023_fact_revisions),
    (24, "event_time", _migration_024_event_time),
    (
        25,
        "gmail_temporal_review_persistence",
        _migration_025_gmail_temporal_review_persistence,
    ),
    (
        26,
        "gmail_temporal_runner_evidence",
        _migration_026_gmail_temporal_runner_evidence,
    ),
    (27, "review_resolution_ledger", _migration_027_review_resolution_ledger),
    (
        28,
        "finalize_review_resolution_backfill",
        _migration_028_finalize_review_resolution_backfill,
    ),
    (
        29,
        "rekey_entity_review_resolutions",
        _migration_029_rekey_entity_review_resolutions,
    ),
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
