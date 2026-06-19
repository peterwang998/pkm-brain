from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pkm_brain import db as db_module


def test_connect_sets_wal_busy_timeout_and_foreign_keys(tmp_path: Path) -> None:
    db_path = tmp_path / "brain.sqlite"

    conn = db_module.connect(db_path)
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    finally:
        conn.close()

    assert journal_mode == "wal"
    assert busy_timeout == db_module.SQLITE_BUSY_TIMEOUT_MS
    assert foreign_keys == 1


def test_retry_sqlite_lock_uses_five_backoff_delays(monkeypatch: pytest.MonkeyPatch) -> None:
    delays = (0.01, 0.02, 0.04, 0.08, 0.16)
    slept: list[float] = []
    attempts = 0

    monkeypatch.setattr(db_module, "SQLITE_LOCK_RETRY_DELAYS_SECONDS", delays)
    monkeypatch.setattr(db_module.time, "sleep", slept.append)

    def locked_operation() -> None:
        nonlocal attempts
        attempts += 1
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        db_module.retry_sqlite_lock(locked_operation)

    assert attempts == len(delays) + 1
    assert slept == list(delays)


def test_retrying_connection_waits_for_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "brain.sqlite"
    with sqlite3.connect(db_path) as setup:
        setup.execute("PRAGMA journal_mode=WAL")
        setup.execute("CREATE TABLE items(id TEXT PRIMARY KEY)")

    retry_delays = (0.01, 0.02, 0.04, 0.08, 0.16)
    slept: list[float] = []
    monkeypatch.setattr(db_module, "SQLITE_BUSY_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(db_module, "SQLITE_BUSY_TIMEOUT_MS", 1)
    monkeypatch.setattr(db_module, "SQLITE_LOCK_RETRY_DELAYS_SECONDS", retry_delays)

    conn = db_module.connect(db_path)
    locker = sqlite3.connect(db_path, timeout=0.001)
    try:
        locker.execute("BEGIN IMMEDIATE")
        locker.execute("INSERT INTO items(id) VALUES ('held')")

        def release_lock(delay: float) -> None:
            slept.append(delay)
            locker.commit()

        monkeypatch.setattr(db_module.time, "sleep", release_lock)

        conn.execute("INSERT INTO items(id) VALUES ('retry')")
        conn.commit()

        rows = [row["id"] for row in conn.execute("SELECT id FROM items ORDER BY id")]
    finally:
        locker.close()
        conn.close()

    assert slept == [retry_delays[0]]
    assert rows == ["held", "retry"]
