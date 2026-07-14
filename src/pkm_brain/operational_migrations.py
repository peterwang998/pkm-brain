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
              AND length(CAST(metadata AS BLOB)) <= 16384
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


OPERATIONAL_MIGRATIONS: list[OperationalMigration] = [
    (1, "create_observations", _migration_001_create_observations),
    (2, "create_items", _migration_002_create_items),
    (3, "create_item_events", _migration_003_create_item_events),
    (4, "create_source_cursors", _migration_004_create_source_cursors),
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
