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
