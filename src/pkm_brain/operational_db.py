from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from .operational_migrations import (
    OPERATIONAL_MIGRATION_VERSIONS,
    run_operational_migrations,
)


OPERATIONAL_BUSY_TIMEOUT_SECONDS = 0.25
OPERATIONAL_BUSY_TIMEOUT_MS = int(OPERATIONAL_BUSY_TIMEOUT_SECONDS * 1000)
OPERATIONAL_LOCK_RETRY_DELAYS_SECONDS = (0.05, 0.1, 0.2, 0.4, 0.8)
OPERATIONAL_DIRECTORY_MODE = 0o700
OPERATIONAL_FILE_MODE = 0o600
T = TypeVar("T")


class OperationalStoreUnavailableError(RuntimeError):
    """The explicitly bootstrapped operational store is unavailable."""


def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return True
    message = str(exc).casefold()
    return "database is locked" in message or "database table is locked" in message


def _retry_locked(operation: Callable[[], T]) -> T:
    for delay in OPERATIONAL_LOCK_RETRY_DELAYS_SECONDS:
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not _is_locked_error(exc):
                raise
            time.sleep(delay)
    return operation()


def _operational_files(db_path: Path) -> tuple[Path, Path, Path]:
    return (
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
    )


def _secure_operational_permissions(db_path: Path) -> None:
    if db_path.parent.exists():
        os.chmod(db_path.parent, OPERATIONAL_DIRECTORY_MODE)
    for path in _operational_files(db_path):
        try:
            if path.is_symlink():
                raise OperationalStoreUnavailableError(
                    f"operational store path must not be a symlink: {path}"
                )
            os.chmod(path, OPERATIONAL_FILE_MODE)
        except FileNotFoundError:
            continue


def _require_operational_db(db_path: Path) -> None:
    if db_path.is_symlink() or not db_path.is_file():
        raise OperationalStoreUnavailableError(
            "operational store is unavailable; explicit initialization or restore is "
            f"required: {db_path}"
        )


def _prepare_operational_db(db_path: Path) -> None:
    if db_path.is_symlink():
        raise OperationalStoreUnavailableError(
            f"operational store path must not be a symlink: {db_path}"
        )
    if db_path.exists() and not db_path.is_file():
        raise OperationalStoreUnavailableError(
            f"operational store is not a regular file: {db_path}"
        )
    if db_path.is_file() and db_path.stat().st_size > 0:
        read_only_uri = f"{db_path.absolute().as_uri()}?mode=ro"
        try:
            with sqlite3.connect(read_only_uri, uri=True) as existing:
                tables = {
                    str(row[0])
                    for row in existing.execute(
                        """
                        SELECT name FROM sqlite_master
                        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                        """
                    )
                }
        except sqlite3.DatabaseError as exc:
            raise OperationalStoreUnavailableError(
                f"operational store bootstrap target is not a valid database: {db_path}"
            ) from exc
        if tables and "ops_schema_migrations" not in tables:
            raise OperationalStoreUnavailableError(
                "refusing to initialize operational tables in a non-operational "
                f"database: {db_path}"
            )
    db_path.parent.mkdir(
        mode=OPERATIONAL_DIRECTORY_MODE,
        parents=True,
        exist_ok=True,
    )
    os.chmod(db_path.parent, OPERATIONAL_DIRECTORY_MODE)
    try:
        descriptor = os.open(
            db_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            OPERATIONAL_FILE_MODE,
        )
    except FileExistsError:
        if not db_path.is_file():
            raise OperationalStoreUnavailableError(
                f"operational store is not a regular file: {db_path}"
            ) from None
    else:
        os.close(descriptor)
    _secure_operational_permissions(db_path)


class OperationalConnection(sqlite3.Connection):
    """A short-lived SQLite connection with bounded lock backoff."""

    _operational_db_path: Path | None = None

    def _secure_files(self) -> None:
        if self._operational_db_path is not None:
            _secure_operational_permissions(self._operational_db_path)

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        cursor = _retry_locked(
            lambda: super(OperationalConnection, self).execute(sql, parameters)
        )
        self._secure_files()
        return cursor

    def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor:
        cursor = _retry_locked(
            lambda: super(OperationalConnection, self).executemany(sql, parameters)
        )
        self._secure_files()
        return cursor

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        cursor = _retry_locked(
            lambda: super(OperationalConnection, self).executescript(sql_script)
        )
        self._secure_files()
        return cursor

    def commit(self) -> None:
        _retry_locked(lambda: super(OperationalConnection, self).commit())
        self._secure_files()


def connect_operational(
    db_path: Path,
    *,
    require_initialized: bool = True,
    configure_wal: bool = False,
) -> OperationalConnection:
    """Open an existing store without ever creating a replacement database."""

    _require_operational_db(db_path)
    database_uri = f"{db_path.absolute().as_uri()}?mode=rw"
    try:
        conn = sqlite3.connect(
            database_uri,
            uri=True,
            timeout=OPERATIONAL_BUSY_TIMEOUT_SECONDS,
            factory=OperationalConnection,
        )
    except sqlite3.DatabaseError as exc:
        raise OperationalStoreUnavailableError(
            f"operational store cannot be opened: {db_path}"
        ) from exc
    conn._operational_db_path = db_path
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"PRAGMA busy_timeout={OPERATIONAL_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        if require_initialized:
            try:
                versions = tuple(
                    int(row[0])
                    for row in conn.execute(
                        "SELECT version FROM ops_schema_migrations ORDER BY version"
                    )
                )
            except sqlite3.OperationalError as exc:
                raise OperationalStoreUnavailableError(
                    "operational store is not initialized; explicit initialization "
                    f"or restore is required: {db_path}"
                ) from exc
            if versions != OPERATIONAL_MIGRATION_VERSIONS:
                raise OperationalStoreUnavailableError(
                    "operational store schema is incomplete or newer than this "
                    f"runtime: found {versions}, expected "
                    f"{OPERATIONAL_MIGRATION_VERSIONS}"
                )
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            expected_tables = {
                "ops_schema_migrations",
                "ops_observations",
                "ops_items",
                "ops_item_events",
                "ops_source_cursors",
            }
            if not expected_tables.issubset(tables):
                raise OperationalStoreUnavailableError(
                    "operational store schema tables are incomplete; explicit "
                    f"initialization or restore is required: {db_path}"
                )
        if configure_wal:
            journal_mode = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        else:
            journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
        if journal_mode.casefold() != "wal":
            raise OperationalStoreUnavailableError(
                f"operational store is not in WAL mode: {db_path}"
            )
        _secure_operational_permissions(db_path)
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise OperationalStoreUnavailableError(
            f"operational store failed integrity/schema checks: {db_path}"
        ) from exc
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def operational_connection(
    db_path: Path,
    *,
    write: bool = False,
    require_initialized: bool = True,
    configure_wal: bool = False,
) -> Iterator[OperationalConnection]:
    conn = connect_operational(
        db_path,
        require_initialized=require_initialized,
        configure_wal=configure_wal,
    )
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if write:
            conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
        _secure_operational_permissions(db_path)


def init_operational_db(db_path: Path) -> None:
    """Explicitly bootstrap or migrate the private operational store."""

    _prepare_operational_db(db_path)
    with operational_connection(
        db_path,
        write=True,
        require_initialized=False,
        configure_wal=True,
    ) as conn:
        run_operational_migrations(conn)
    # Initialization is successful only if the strict runtime open accepts the
    # exact migration set and physical schema.
    with operational_connection(db_path, write=False):
        pass
    _secure_operational_permissions(db_path)
