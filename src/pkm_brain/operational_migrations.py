from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable

from .util import now_iso


OperationalMigrationFn = Callable[[sqlite3.Connection], None]
OperationalMigration = tuple[int, str, OperationalMigrationFn]


def _migration_001_create_observations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_observations (
          id TEXT PRIMARY KEY,
          canonical_key TEXT NOT NULL
            CHECK(length(canonical_key) BETWEEN 1 AND 512),
          source_type TEXT NOT NULL
            CHECK(length(source_type) BETWEEN 1 AND 128),
          account_key TEXT NOT NULL
            CHECK(length(account_key) BETWEEN 1 AND 512),
          stream_key TEXT NOT NULL
            CHECK(length(stream_key) BETWEEN 1 AND 512),
          source_key TEXT NOT NULL
            CHECK(length(source_key) BETWEEN 1 AND 1024),
          source_revision TEXT NOT NULL
            CHECK(length(source_revision) BETWEEN 1 AND 1024),
          source_order INTEGER NOT NULL CHECK(source_order >= 0),
          source_updated_at TEXT,
          observed_at TEXT NOT NULL,
          item_kind TEXT NOT NULL
            CHECK(item_kind IN (
              'event', 'commitment', 'waiting', 'follow_up', 'deadline', 'attention'
            )),
          event_kind TEXT NOT NULL
            CHECK(event_kind IN ('upsert', 'cancelled')),
          payload TEXT NOT NULL
            CHECK(
              json_valid(payload)
              AND json_type(payload) = 'object'
              AND length(CAST(payload AS BLOB)) <= 32768
            ),
          content_hash TEXT NOT NULL CHECK(length(content_hash) = 64),
          evidence_refs TEXT NOT NULL DEFAULT '[]'
            CHECK(
              json_valid(evidence_refs)
              AND json_type(evidence_refs) = 'array'
              AND length(CAST(evidence_refs AS BLOB)) <= 16384
            ),
          created_at TEXT NOT NULL,
          UNIQUE(
            source_type, account_key, stream_key, source_key, source_revision
          )
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ops_observations_source
        ON ops_observations(
          source_type, account_key, stream_key, source_key, source_updated_at
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ops_observations_canonical_order
        ON ops_observations(canonical_key, source_updated_at, source_order)
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS ops_observations_immutable_update
        BEFORE UPDATE ON ops_observations
        BEGIN
          SELECT RAISE(ABORT, 'observations are immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS ops_observations_immutable_delete
        BEFORE DELETE ON ops_observations
        BEGIN
          SELECT RAISE(ABORT, 'observations are immutable');
        END
        """
    )


def _migration_002_create_items(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_items (
          id TEXT PRIMARY KEY,
          canonical_key TEXT NOT NULL UNIQUE
            CHECK(length(canonical_key) BETWEEN 1 AND 512),
          source_type TEXT NOT NULL
            CHECK(length(source_type) BETWEEN 1 AND 128),
          account_key TEXT NOT NULL
            CHECK(length(account_key) BETWEEN 1 AND 512),
          stream_key TEXT NOT NULL
            CHECK(length(stream_key) BETWEEN 1 AND 512),
          source_key TEXT NOT NULL
            CHECK(length(source_key) BETWEEN 1 AND 1024),
          item_kind TEXT NOT NULL
            CHECK(item_kind IN (
              'event', 'commitment', 'waiting', 'follow_up', 'deadline', 'attention'
            )),
          state TEXT NOT NULL
            CHECK(state IN ('active', 'resolved', 'dismissed', 'cancelled', 'expired')),
          title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 500),
          details TEXT CHECK(details IS NULL OR length(details) <= 4000),
          owner TEXT NOT NULL
            CHECK(owner IN ('operator', 'other', 'shared', 'unknown')),
          counterparty_entity_id TEXT
            CHECK(
              counterparty_entity_id IS NULL
              OR length(counterparty_entity_id) BETWEEN 1 AND 512
            ),
          project_ref TEXT
            CHECK(project_ref IS NULL OR length(project_ref) BETWEEN 1 AND 512),
          starts_at TEXT,
          due_at TEXT,
          ends_at TEXT,
          source_timezone TEXT
            CHECK(
              source_timezone IS NULL
              OR length(source_timezone) BETWEEN 1 AND 128
            ),
          expires_at TEXT,
          snoozed_until TEXT,
          human_confirmed_at TEXT,
          last_human_action_at TEXT,
          confidence REAL NOT NULL
            CHECK(confidence >= 0.0 AND confidence <= 1.0),
          priority INTEGER NOT NULL DEFAULT 0
            CHECK(priority BETWEEN -100 AND 100),
          reconciliation_method TEXT NOT NULL
            CHECK(length(reconciliation_method) BETWEEN 1 AND 128),
          current_observation_id TEXT NOT NULL
            REFERENCES ops_observations(id) ON DELETE RESTRICT,
          human_override_state TEXT
            CHECK(human_override_state IS NULL OR human_override_state IN (
              'resolved', 'dismissed'
            )),
          human_override_at TEXT,
          human_override_reason TEXT
            CHECK(
              human_override_reason IS NULL
              OR length(human_override_reason) <= 2000
            ),
          metadata TEXT NOT NULL DEFAULT '{}'
            CHECK(
              json_valid(metadata)
              AND json_type(metadata) = 'object'
              AND length(CAST(metadata AS BLOB)) <= 16384
            ),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          state_changed_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ops_items_state_due
        ON ops_items(state, due_at, starts_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ops_items_kind_state
        ON ops_items(item_kind, state, updated_at)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ops_items_source
        ON ops_items(source_type, account_key, stream_key, source_key)
        """
    )


def _migration_003_create_item_events(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_item_events (
          id TEXT PRIMARY KEY,
          item_id TEXT NOT NULL REFERENCES ops_items(id) ON DELETE RESTRICT,
          observation_id TEXT REFERENCES ops_observations(id) ON DELETE RESTRICT,
          event_type TEXT NOT NULL
            CHECK(event_type IN (
              'created', 'updated', 'rescheduled', 'cancelled', 'resolved',
              'expired', 'stale_ignored', 'feedback'
            )),
          actor TEXT NOT NULL
            CHECK(actor IN ('connector', 'deterministic', 'model', 'human')),
          sequence INTEGER NOT NULL CHECK(sequence > 0),
          from_state TEXT
            CHECK(from_state IS NULL OR from_state IN (
              'active', 'resolved', 'dismissed', 'cancelled', 'expired'
            )),
          to_state TEXT NOT NULL
            CHECK(to_state IN (
              'active', 'resolved', 'dismissed', 'cancelled', 'expired'
            )),
          source_type TEXT,
          account_key TEXT,
          stream_key TEXT,
          source_key TEXT,
          source_revision TEXT,
          source_order INTEGER CHECK(source_order IS NULL OR source_order >= 0),
          run_id TEXT NOT NULL CHECK(length(run_id) BETWEEN 1 AND 256),
          reconciliation_version TEXT NOT NULL
            CHECK(length(reconciliation_version) BETWEEN 1 AND 128),
          before_state TEXT NOT NULL DEFAULT '{}'
            CHECK(
              json_valid(before_state)
              AND json_type(before_state) = 'object'
              AND length(CAST(before_state AS BLOB)) <= 65536
            ),
          after_state TEXT NOT NULL DEFAULT '{}'
            CHECK(
              json_valid(after_state)
              AND json_type(after_state) = 'object'
              AND length(CAST(after_state AS BLOB)) <= 65536
            ),
          metadata TEXT NOT NULL DEFAULT '{}'
            CHECK(
              json_valid(metadata)
              AND json_type(metadata) = 'object'
              AND length(CAST(metadata AS BLOB)) <= 65536
            ),
          transition_hash TEXT NOT NULL CHECK(length(transition_hash) = 64),
          idempotency_key TEXT
            CHECK(
              idempotency_key IS NULL
              OR length(idempotency_key) BETWEEN 1 AND 256
            ),
          created_at TEXT NOT NULL,
          UNIQUE(item_id, sequence),
          UNIQUE(item_id, idempotency_key)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ops_item_events_item_created
        ON ops_item_events(item_id, sequence)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ops_item_events_observation
        ON ops_item_events(observation_id)
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS ops_item_events_append_only_update
        BEFORE UPDATE ON ops_item_events
        BEGIN
          SELECT RAISE(ABORT, 'item events are append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS ops_item_events_append_only_delete
        BEFORE DELETE ON ops_item_events
        BEGIN
          SELECT RAISE(ABORT, 'item events are append-only');
        END
        """
    )


def _migration_004_create_source_cursors(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_source_cursors (
          connector_id TEXT NOT NULL
            CHECK(length(connector_id) BETWEEN 1 AND 128),
          source_type TEXT NOT NULL
            CHECK(length(source_type) BETWEEN 1 AND 128),
          account_key TEXT NOT NULL
            CHECK(length(account_key) BETWEEN 1 AND 512),
          stream_key TEXT NOT NULL
            CHECK(length(stream_key) BETWEEN 1 AND 512),
          cursor TEXT CHECK(cursor IS NULL OR length(cursor) <= 8192),
          watermark TEXT CHECK(watermark IS NULL OR length(watermark) <= 1024),
          metadata TEXT NOT NULL DEFAULT '{}'
            CHECK(
              json_valid(metadata)
              AND json_type(metadata) = 'object'
              AND length(CAST(metadata AS BLOB)) <= 524288
            ),
          last_success_at TEXT,
          generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0),
          updated_at TEXT NOT NULL,
          PRIMARY KEY(connector_id, account_key, stream_key)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ops_source_cursors_success
        ON ops_source_cursors(connector_id, last_success_at)
        """
    )


def _migration_005_create_shadow_trial_projections(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_shadow_runs (
          id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 256),
          mode TEXT NOT NULL CHECK(mode IN ('live', 'replay', 'fixture')),
          status TEXT NOT NULL
            CHECK(status IN ('running', 'complete', 'partial', 'failed', 'stopped')),
          requested_sources TEXT NOT NULL DEFAULT '[]'
            CHECK(
              json_valid(requested_sources)
              AND json_type(requested_sources) = 'array'
              AND length(CAST(requested_sources AS BLOB)) <= 4096
            ),
          policy_version TEXT NOT NULL CHECK(length(policy_version) BETWEEN 1 AND 128),
          detector_version TEXT
            CHECK(detector_version IS NULL OR length(detector_version) <= 128),
          started_at TEXT NOT NULL,
          finished_at TEXT,
          coverage TEXT NOT NULL DEFAULT '{}'
            CHECK(
              json_valid(coverage)
              AND json_type(coverage) = 'object'
              AND length(CAST(coverage AS BLOB)) <= 32768
            ),
          usage TEXT NOT NULL DEFAULT '{}'
            CHECK(
              json_valid(usage)
              AND json_type(usage) = 'object'
              AND length(CAST(usage AS BLOB)) <= 16384
            ),
          counts TEXT NOT NULL DEFAULT '{}'
            CHECK(
              json_valid(counts)
              AND json_type(counts) = 'object'
              AND length(CAST(counts AS BLOB)) <= 16384
            ),
          error TEXT CHECK(error IS NULL OR length(error) <= 4000),
          hard_stop_reason TEXT
            CHECK(hard_stop_reason IS NULL OR length(hard_stop_reason) <= 1000),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ops_shadow_runs_started
        ON ops_shadow_runs(started_at DESC)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_shadow_decisions (
          id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 256),
          run_id TEXT NOT NULL
            REFERENCES ops_shadow_runs(id) ON DELETE RESTRICT,
          source_type TEXT NOT NULL CHECK(length(source_type) BETWEEN 1 AND 128),
          account_key TEXT NOT NULL CHECK(length(account_key) BETWEEN 1 AND 512),
          stream_key TEXT NOT NULL CHECK(length(stream_key) BETWEEN 1 AND 512),
          source_key TEXT NOT NULL CHECK(length(source_key) BETWEEN 1 AND 1024),
          source_revision TEXT
            CHECK(source_revision IS NULL OR length(source_revision) <= 1024),
          disposition TEXT NOT NULL
            CHECK(disposition IN ('surfaced', 'suppressed', 'deferred', 'error')),
          reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 1 AND 128),
          item_ids TEXT NOT NULL DEFAULT '[]'
            CHECK(
              json_valid(item_ids)
              AND json_type(item_ids) = 'array'
              AND length(CAST(item_ids AS BLOB)) <= 8192
            ),
          evidence_refs TEXT NOT NULL DEFAULT '[]'
            CHECK(
              json_valid(evidence_refs)
              AND json_type(evidence_refs) = 'array'
              AND length(CAST(evidence_refs AS BLOB)) <= 16384
            ),
          confidence REAL NOT NULL DEFAULT 0.0
            CHECK(confidence >= 0.0 AND confidence <= 1.0),
          metadata TEXT NOT NULL DEFAULT '{}'
            CHECK(
              json_valid(metadata)
              AND json_type(metadata) = 'object'
              AND length(CAST(metadata AS BLOB)) <= 8192
            ),
          created_at TEXT NOT NULL,
          UNIQUE(run_id, source_type, account_key, stream_key, source_key, reason_code)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ops_shadow_decisions_run_disposition
        ON ops_shadow_decisions(run_id, disposition, source_type)
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS ops_shadow_decisions_append_only_update
        BEFORE UPDATE ON ops_shadow_decisions
        BEGIN
          SELECT RAISE(ABORT, 'shadow decisions are append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS ops_shadow_decisions_append_only_delete
        BEFORE DELETE ON ops_shadow_decisions
        BEGIN
          SELECT RAISE(ABORT, 'shadow decisions are append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_handled_assessments (
          id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 256),
          item_id TEXT NOT NULL REFERENCES ops_items(id) ON DELETE RESTRICT,
          run_id TEXT REFERENCES ops_shadow_runs(id) ON DELETE RESTRICT,
          verdict TEXT NOT NULL CHECK(verdict IN (
            'needs_action', 'responded_waiting', 'being_handled', 'fulfilled', 'unknown'
          )),
          supporting_evidence TEXT NOT NULL DEFAULT '[]'
            CHECK(
              json_valid(supporting_evidence)
              AND json_type(supporting_evidence) = 'array'
              AND length(CAST(supporting_evidence AS BLOB)) <= 16384
            ),
          contradicting_evidence TEXT NOT NULL DEFAULT '[]'
            CHECK(
              json_valid(contradicting_evidence)
              AND json_type(contradicting_evidence) = 'array'
              AND length(CAST(contradicting_evidence AS BLOB)) <= 16384
            ),
          sources_checked TEXT NOT NULL DEFAULT '[]'
            CHECK(
              json_valid(sources_checked)
              AND json_type(sources_checked) = 'array'
              AND length(CAST(sources_checked AS BLOB)) <= 4096
            ),
          coverage TEXT NOT NULL DEFAULT '{}'
            CHECK(
              json_valid(coverage)
              AND json_type(coverage) = 'object'
              AND length(CAST(coverage AS BLOB)) <= 8192
            ),
          method_version TEXT NOT NULL CHECK(length(method_version) BETWEEN 1 AND 128),
          policy_version TEXT NOT NULL CHECK(length(policy_version) BETWEEN 1 AND 128),
          confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
          as_of TEXT NOT NULL,
          assessment_hash TEXT NOT NULL UNIQUE CHECK(length(assessment_hash) = 64),
          created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ops_handled_item_as_of
        ON ops_handled_assessments(item_id, as_of DESC, created_at DESC)
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS ops_handled_assessments_append_only_update
        BEFORE UPDATE ON ops_handled_assessments
        BEGIN
          SELECT RAISE(ABORT, 'handled assessments are append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS ops_handled_assessments_append_only_delete
        BEFORE DELETE ON ops_handled_assessments
        BEGIN
          SELECT RAISE(ABORT, 'handled assessments are append-only');
        END
        """
    )


def _migration_006_create_briefing_and_feedback_projections(
    conn: sqlite3.Connection,
) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_briefing_snapshots (
          id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 256),
          run_id TEXT REFERENCES ops_shadow_runs(id) ON DELETE RESTRICT,
          generated_at TEXT NOT NULL,
          as_of TEXT NOT NULL,
          timezone TEXT NOT NULL CHECK(length(timezone) BETWEEN 1 AND 128),
          policy_version TEXT NOT NULL CHECK(length(policy_version) BETWEEN 1 AND 128),
          status TEXT NOT NULL CHECK(status IN ('complete', 'partial', 'unavailable')),
          sections TEXT NOT NULL
            CHECK(
              json_valid(sections)
              AND json_type(sections) = 'object'
              AND length(CAST(sections AS BLOB)) <= 262144
            ),
          coverage TEXT NOT NULL
            CHECK(
              json_valid(coverage)
              AND json_type(coverage) = 'object'
              AND length(CAST(coverage AS BLOB)) <= 32768
            ),
          counts TEXT NOT NULL DEFAULT '{}'
            CHECK(
              json_valid(counts)
              AND json_type(counts) = 'object'
              AND length(CAST(counts AS BLOB)) <= 16384
            ),
          snapshot_hash TEXT NOT NULL UNIQUE CHECK(length(snapshot_hash) = 64),
          expires_at TEXT NOT NULL,
          created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ops_briefing_generated
        ON ops_briefing_snapshots(generated_at DESC)
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS ops_briefing_snapshots_append_only_update
        BEFORE UPDATE ON ops_briefing_snapshots
        BEGIN
          SELECT RAISE(ABORT, 'briefing snapshots are append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS ops_briefing_snapshots_append_only_delete
        BEFORE DELETE ON ops_briefing_snapshots
        BEGIN
          SELECT RAISE(ABORT, 'briefing snapshots are append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_missing_reports (
          id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 256),
          run_id TEXT REFERENCES ops_shadow_runs(id) ON DELETE RESTRICT,
          source_type TEXT CHECK(source_type IS NULL OR length(source_type) <= 128),
          source_ref TEXT CHECK(source_ref IS NULL OR length(source_ref) <= 1024),
          expected_kind TEXT CHECK(expected_kind IS NULL OR expected_kind IN (
            'event', 'commitment', 'waiting', 'follow_up', 'deadline', 'attention'
          )),
          summary TEXT NOT NULL CHECK(length(summary) BETWEEN 1 AND 2000),
          status TEXT NOT NULL DEFAULT 'open'
            CHECK(status IN ('open', 'resolved', 'dismissed')),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ops_missing_reports_status
        ON ops_missing_reports(status, created_at DESC)
        """
    )


def _migration_007_bind_assessments_and_daily_budgets(
    conn: sqlite3.Connection,
) -> None:
    handled_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(ops_handled_assessments)")
    }
    if "observation_id" not in handled_columns:
        conn.execute(
            """
            ALTER TABLE ops_handled_assessments
            ADD COLUMN observation_id TEXT
              REFERENCES ops_observations(id) ON DELETE RESTRICT
            """
        )
    if "source_revision" not in handled_columns:
        conn.execute(
            """
            ALTER TABLE ops_handled_assessments
            ADD COLUMN source_revision TEXT
              CHECK(
                source_revision IS NULL
                OR length(source_revision) BETWEEN 1 AND 1024
              )
            """
        )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ops_handled_observation_revision_as_of
        ON ops_handled_assessments(
          observation_id, source_revision, as_of DESC, created_at DESC
        )
        """
    )

    missing_columns = {
        str(row[1]) for row in conn.execute("PRAGMA table_info(ops_missing_reports)")
    }
    if "idempotency_key" not in missing_columns:
        conn.execute(
            """
            ALTER TABLE ops_missing_reports
            ADD COLUMN idempotency_key TEXT
              CHECK(
                idempotency_key IS NULL
                OR length(idempotency_key) BETWEEN 1 AND 256
              )
            """
        )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ops_missing_idempotency
        ON ops_missing_reports(idempotency_key)
        WHERE idempotency_key IS NOT NULL
        """
    )

    conn.execute("DROP TRIGGER IF EXISTS ops_briefing_snapshots_append_only_delete")
    conn.execute(
        """
        CREATE TRIGGER ops_briefing_snapshots_append_only_delete
        BEFORE DELETE ON ops_briefing_snapshots
        WHEN julianday(OLD.expires_at) IS NULL
          OR julianday(OLD.expires_at) > julianday('now')
        BEGIN
          SELECT RAISE(
            ABORT,
            'briefing snapshots are append-only until expiry'
          );
        END
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_budget_reservations (
          id TEXT PRIMARY KEY CHECK(length(id) BETWEEN 1 AND 256),
          run_id TEXT REFERENCES ops_shadow_runs(id) ON DELETE RESTRICT,
          source_type TEXT NOT NULL CHECK(source_type IN ('calendar', 'gmail')),
          metric TEXT NOT NULL CHECK(metric IN (
            'api_requests', 'api_quota_units', 'detector_calls',
            'detector_input_tokens', 'detector_total_tokens'
          )),
          amount INTEGER NOT NULL CHECK(amount > 0),
          local_day TEXT NOT NULL CHECK(length(local_day) = 10),
          policy_version TEXT NOT NULL CHECK(length(policy_version) BETWEEN 1 AND 128),
          created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ops_budget_day_metric
        ON ops_budget_reservations(local_day, source_type, metric, created_at)
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS ops_budget_reservations_immutable_update
        BEFORE UPDATE ON ops_budget_reservations
        BEGIN
          SELECT RAISE(ABORT, 'budget reservations are immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS ops_budget_reservations_immutable_delete
        BEFORE DELETE ON ops_budget_reservations
        BEGIN
          SELECT RAISE(ABORT, 'budget reservations are immutable');
        END
        """
    )


def _migration_008_expand_source_cursor_metadata(conn: sqlite3.Connection) -> None:
    """Give resumable provider backlogs a bounded cursor-specific envelope."""

    conn.execute("DROP INDEX IF EXISTS idx_ops_source_cursors_success")
    conn.execute("ALTER TABLE ops_source_cursors RENAME TO ops_source_cursors_v7")
    conn.execute(
        """
        CREATE TABLE ops_source_cursors (
          connector_id TEXT NOT NULL
            CHECK(length(connector_id) BETWEEN 1 AND 128),
          source_type TEXT NOT NULL
            CHECK(length(source_type) BETWEEN 1 AND 128),
          account_key TEXT NOT NULL
            CHECK(length(account_key) BETWEEN 1 AND 512),
          stream_key TEXT NOT NULL
            CHECK(length(stream_key) BETWEEN 1 AND 512),
          cursor TEXT CHECK(cursor IS NULL OR length(cursor) <= 8192),
          watermark TEXT CHECK(watermark IS NULL OR length(watermark) <= 1024),
          metadata TEXT NOT NULL DEFAULT '{}'
            CHECK(
              json_valid(metadata)
              AND json_type(metadata) = 'object'
              AND length(CAST(metadata AS BLOB)) <= 524288
            ),
          last_success_at TEXT,
          generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0),
          updated_at TEXT NOT NULL,
          PRIMARY KEY(connector_id, account_key, stream_key)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO ops_source_cursors(
          connector_id, source_type, account_key, stream_key, cursor, watermark,
          metadata, last_success_at, generation, updated_at
        )
        SELECT connector_id, source_type, account_key, stream_key, cursor,
               watermark, metadata, last_success_at, generation, updated_at
        FROM ops_source_cursors_v7
        """
    )
    conn.execute("DROP TABLE ops_source_cursors_v7")
    conn.execute(
        """
        CREATE INDEX idx_ops_source_cursors_success
        ON ops_source_cursors(connector_id, last_success_at)
        """
    )


OPERATIONAL_MIGRATIONS: list[OperationalMigration] = [
    (1, "create_observations", _migration_001_create_observations),
    (2, "create_items", _migration_002_create_items),
    (3, "create_item_events", _migration_003_create_item_events),
    (4, "create_source_cursors", _migration_004_create_source_cursors),
    (5, "create_shadow_trial_projections", _migration_005_create_shadow_trial_projections),
    (
        6,
        "create_briefing_and_feedback_projections",
        _migration_006_create_briefing_and_feedback_projections,
    ),
    (
        7,
        "bind_assessments_and_daily_budgets",
        _migration_007_bind_assessments_and_daily_budgets,
    ),
    (
        8,
        "expand_source_cursor_metadata",
        _migration_008_expand_source_cursor_metadata,
    ),
]
OPERATIONAL_MIGRATION_VERSIONS = tuple(
    version for version, _name, _migration in OPERATIONAL_MIGRATIONS
)


def run_operational_migrations(
    conn: sqlite3.Connection,
    migrations: Iterable[OperationalMigration] | None = None,
) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ops_schema_migrations (
          version INTEGER PRIMARY KEY CHECK(version > 0),
          name TEXT NOT NULL UNIQUE CHECK(length(name) BETWEEN 1 AND 128),
          applied_at TEXT NOT NULL
        )
        """
    )
    applied = {
        int(row["version"] if isinstance(row, sqlite3.Row) else row[0]): str(
            row["name"] if isinstance(row, sqlite3.Row) else row[1]
        )
        for row in conn.execute("SELECT version, name FROM ops_schema_migrations")
    }
    registered = sorted(
        OPERATIONAL_MIGRATIONS if migrations is None else migrations,
        key=lambda candidate: candidate[0],
    )
    for version, name, migration in registered:
        if version in applied:
            if applied[version] != name:
                raise RuntimeError(
                    "operational migration identity mismatch for "
                    f"version {version}: {applied[version]} != {name}"
                )
            continue
        savepoint = f"ops_migration_{int(version)}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            migration(conn)
            conn.execute(
                """
                INSERT INTO ops_schema_migrations(version, name, applied_at)
                VALUES (?, ?, ?)
                """,
                (version, name, now_iso()),
            )
            conn.execute(f"RELEASE {savepoint}")
        except Exception as exc:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
            raise RuntimeError(
                f"operational migration {version} ({name}) failed"
            ) from exc
