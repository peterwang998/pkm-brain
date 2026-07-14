from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from pkm_brain.operational_db import (
    OPERATIONAL_BUSY_TIMEOUT_MS,
    OPERATIONAL_DIRECTORY_MODE,
    OPERATIONAL_FILE_MODE,
    OperationalStoreUnavailableError,
    connect_operational,
    init_operational_db,
    operational_connection,
)
from pkm_brain.operational_migrations import (
    OPERATIONAL_MIGRATIONS,
    run_operational_migrations,
)
from pkm_brain.paths import BrainPaths


EXPECTED_OPERATIONAL_MIGRATIONS = [1, 2, 3, 4, 5, 6, 7, 8, 9]
EXPECTED_OPERATIONAL_TABLES = {
    "ops_schema_migrations",
    "ops_observations",
    "ops_items",
    "ops_item_events",
    "ops_source_cursors",
    "ops_shadow_runs",
    "ops_shadow_decisions",
    "ops_handled_assessments",
    "ops_briefing_snapshots",
    "ops_missing_reports",
    "ops_budget_reservations",
    "ops_suppression_rules",
    "ops_meeting_packets",
}


def test_operational_database_has_a_separate_brain_home_path(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")

    assert paths.sqlite_path == paths.home / "db" / "brain.sqlite"
    assert paths.ops_sqlite_path == paths.home / "db" / "ops.sqlite"
    assert paths.ops_sqlite_path != paths.sqlite_path


def test_operational_migrations_are_independent_and_idempotent(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")

    init_operational_db(paths.ops_sqlite_path)
    init_operational_db(paths.ops_sqlite_path)

    with operational_connection(paths.ops_sqlite_path) as conn:
        versions = [
            row["version"]
            for row in conn.execute(
                "SELECT version FROM ops_schema_migrations ORDER BY version"
            )
        ]
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert versions == EXPECTED_OPERATIONAL_MIGRATIONS
    assert EXPECTED_OPERATIONAL_TABLES.issubset(tables)
    assert "documents" not in tables
    assert "facts" not in tables
    assert "cos_actions" not in tables
    assert not paths.sqlite_path.exists()


def test_cursor_metadata_expansion_preserves_v7_rows_and_other_bounds(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ops-v7.sqlite"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        run_operational_migrations(conn, migrations=OPERATIONAL_MIGRATIONS[:7])
        conn.execute(
            """
            INSERT INTO ops_source_cursors(
              connector_id, source_type, account_key, stream_key,
              cursor, metadata, generation, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "gmail",
                "gmail",
                "gmail.personal",
                "mailbox",
                "history-7",
                '{"coverage_status":"partial"}',
                3,
                "2026-07-13T15:00:00+00:00",
            ),
        )
        run_operational_migrations(conn)

        row = conn.execute(
            "SELECT cursor, metadata, generation FROM ops_source_cursors"
        ).fetchone()
        cursor_sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name = 'ops_source_cursors'"
            ).fetchone()[0]
        )
        item_sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name = 'ops_items'"
            ).fetchone()[0]
        )
        observation_sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name = 'ops_observations'"
            ).fetchone()[0]
        )
    finally:
        conn.close()

    assert tuple(row) == ("history-7", '{"coverage_status":"partial"}', 3)
    assert "<= 524288" in cursor_sql
    assert "<= 16384" in item_sql
    assert "<= 32768" in observation_sql


def test_operational_connection_uses_wal_busy_timeout_and_foreign_keys(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ops.sqlite"
    init_operational_db(db_path)

    conn = connect_operational(db_path)
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    finally:
        conn.close()

    assert journal_mode == "wal"
    assert busy_timeout == OPERATIONAL_BUSY_TIMEOUT_MS
    assert foreign_keys == 1


def test_operational_connection_rolls_back_failed_short_transaction(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ops.sqlite"
    init_operational_db(db_path)

    with pytest.raises(RuntimeError, match="abort transaction"):
        with operational_connection(db_path, write=True) as conn:
            conn.execute(
                """
                INSERT INTO ops_source_cursors(
                  connector_id, source_type, account_key, stream_key, updated_at
                ) VALUES (
                  'calendar', 'google_calendar', 'me', 'primary',
                  '2026-07-13T10:00:00+00:00'
                )
                """
            )
            raise RuntimeError("abort transaction")

    with operational_connection(db_path, write=False) as conn:
        count = conn.execute("SELECT COUNT(*) FROM ops_source_cursors").fetchone()[0]

    assert count == 0


def test_runtime_open_never_creates_or_accepts_an_uninitialized_store(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing" / "ops.sqlite"

    with pytest.raises(OperationalStoreUnavailableError, match="unavailable"):
        connect_operational(missing)
    assert not missing.exists()

    empty = tmp_path / "empty.sqlite"
    empty.touch()
    with pytest.raises(OperationalStoreUnavailableError, match="not initialized"):
        connect_operational(empty)
    assert empty.stat().st_size == 0

    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(OperationalStoreUnavailableError, match="integrity/schema"):
        connect_operational(corrupt)


def test_initialization_requires_exact_post_migration_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "ops.sqlite"
    init_operational_db(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO ops_schema_migrations(version, name, applied_at)
            VALUES (99, 'unknown_future_migration', '2026-07-13T00:00:00+00:00')
            """
        )

    with pytest.raises(OperationalStoreUnavailableError, match="newer"):
        init_operational_db(db_path)


def test_initialization_refuses_a_non_operational_sqlite_database(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "brain.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE facts(id TEXT PRIMARY KEY)")

    with pytest.raises(OperationalStoreUnavailableError, match="non-operational"):
        init_operational_db(db_path)

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "facts" in tables
    assert not any(name.startswith("ops_") for name in tables)


def test_operational_store_enforces_owner_only_permissions(tmp_path: Path) -> None:
    db_path = tmp_path / "private-db" / "ops.sqlite"
    previous_umask = os.umask(0)
    try:
        init_operational_db(db_path)
        with operational_connection(db_path, write=True) as conn:
            conn.execute(
                """
                INSERT INTO ops_source_cursors(
                  connector_id, source_type, account_key, stream_key, updated_at
                ) VALUES (
                  'calendar', 'google_calendar', 'account-1', 'primary', ?
                )
                """,
                ("2026-07-13T15:00:00+00:00",),
            )
            assert stat.S_IMODE(db_path.stat().st_mode) == OPERATIONAL_FILE_MODE
            for sidecar in (Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
                assert sidecar.exists()
                assert stat.S_IMODE(sidecar.stat().st_mode) == OPERATIONAL_FILE_MODE
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(db_path.parent.stat().st_mode) == OPERATIONAL_DIRECTORY_MODE
    assert stat.S_IMODE(db_path.stat().st_mode) == OPERATIONAL_FILE_MODE
