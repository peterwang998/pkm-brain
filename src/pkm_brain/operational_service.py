from __future__ import annotations

import fcntl
import os
import stat
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .operational_db import init_operational_db
from .operational_state import (
    OperationalObservation,
    ReconciliationResult,
    SourceCursorUpdate,
    SourceUnitResult,
    reconcile_observation,
    reconcile_source_unit,
    record_item_feedback,
    save_source_cursor,
)
from .paths import BrainPaths, local_node_id
from .sync_config import load_sync_config


OPERATIONAL_WRITER_LOCK_TIMEOUT_SECONDS = 5.0
OPERATIONAL_WRITER_LOCK_POLL_SECONDS = 0.05
PRIVATE_FILE_MODE = 0o600


class OperationalWriteRefusedError(RuntimeError):
    """The current process or topology is not authorized to mutate operations."""


class OperationalWriterBusyError(RuntimeError):
    """Another authorized mutation holds the bounded operational write lease."""


@dataclass(frozen=True)
class OperationalAuthorityStatus:
    role: str
    node_id: str | None
    configured: bool
    can_write: bool
    reason: str


_HOME_LOCKS_GUARD = threading.Lock()
_HOME_LOCKS: dict[Path, threading.RLock] = {}


def operational_authority_status(paths: BrainPaths) -> OperationalAuthorityStatus:
    """Resolve writer authority from disk every time; never cache topology state."""

    quarantine = paths.restore_quarantine_file
    if quarantine.exists() or quarantine.is_symlink():
        return OperationalAuthorityStatus(
            role="quarantined_restore",
            node_id=None,
            configured=True,
            can_write=False,
            reason="restored_home_requires_activation",
        )
    sync_path = paths.sync_config_file
    if sync_path.is_symlink():
        return OperationalAuthorityStatus(
            role="invalid",
            node_id=None,
            configured=True,
            can_write=False,
            reason="sync_config_symlink_refused",
        )
    if not sync_path.exists():
        node_marker = paths.local_node_id_file
        if node_marker.exists() or node_marker.is_symlink():
            return OperationalAuthorityStatus(
                role="invalid",
                node_id=None,
                configured=True,
                can_write=False,
                reason="sync_config_missing_for_configured_node",
            )
        return OperationalAuthorityStatus(
            role="single",
            node_id=local_node_id(paths),
            configured=False,
            can_write=True,
            reason="implicit_single",
        )
    try:
        config = load_sync_config(paths)
    except Exception as exc:
        return OperationalAuthorityStatus(
            role="invalid",
            node_id=None,
            configured=True,
            can_write=False,
            reason=f"invalid_sync_config:{type(exc).__name__}",
        )
    if config.brain_home.expanduser().resolve() != paths.home:
        return OperationalAuthorityStatus(
            role=config.role,
            node_id=config.node_id,
            configured=True,
            can_write=False,
            reason="configured_home_mismatch",
        )
    node_path = paths.local_node_id_file
    if node_path.is_symlink() or not node_path.is_file():
        return OperationalAuthorityStatus(
            role=config.role,
            node_id=config.node_id,
            configured=True,
            can_write=False,
            reason="local_node_identity_missing",
        )
    try:
        actual_node_id = node_path.read_text(encoding="utf-8").strip()
    except OSError:
        actual_node_id = ""
    if not actual_node_id or actual_node_id != config.node_id:
        return OperationalAuthorityStatus(
            role=config.role,
            node_id=config.node_id,
            configured=True,
            can_write=False,
            reason="local_node_identity_mismatch",
        )
    if config.role != "primary":
        return OperationalAuthorityStatus(
            role=config.role,
            node_id=config.node_id,
            configured=True,
            can_write=False,
            reason="secondary_is_read_only",
        )
    return OperationalAuthorityStatus(
        role="primary",
        node_id=config.node_id,
        configured=True,
        can_write=True,
        reason="configured_primary",
    )


def _home_mutation_lock(paths: BrainPaths) -> threading.RLock:
    home = paths.home.resolve()
    with _HOME_LOCKS_GUARD:
        return _HOME_LOCKS.setdefault(home, threading.RLock())


def _lock_path_matches_descriptor(path: Path, descriptor: int) -> bool:
    try:
        descriptor_stat = os.fstat(descriptor)
        path_stat = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISREG(descriptor_stat.st_mode)
        and stat.S_ISREG(path_stat.st_mode)
        and descriptor_stat.st_dev == path_stat.st_dev
        and descriptor_stat.st_ino == path_stat.st_ino
    )


@contextmanager
def _cross_process_mutation_lock(
    paths: BrainPaths,
    *,
    timeout_seconds: float = OPERATIONAL_WRITER_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    lock_path = paths.operational_writer_lock_file
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if lock_path.is_symlink():
        raise OperationalWriteRefusedError(
            f"operational writer lock must not be a symlink: {lock_path}"
        )
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, PRIVATE_FILE_MODE)
    except OSError as exc:
        raise OperationalWriteRefusedError(
            f"operational writer lock cannot be opened: {lock_path}"
        ) from exc
    try:
        handle = os.fdopen(descriptor, "a+", encoding="utf-8")
    except Exception:
        os.close(descriptor)
        raise
    try:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OperationalWriteRefusedError(
                f"operational writer lock is not a regular file: {lock_path}"
            )
        os.fchmod(handle.fileno(), PRIVATE_FILE_MODE)
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise OperationalWriterBusyError(
                        "timed out waiting for the operational writer lease"
                    ) from exc
                time.sleep(OPERATIONAL_WRITER_LOCK_POLL_SECONDS)
        if not _lock_path_matches_descriptor(lock_path, handle.fileno()):
            raise OperationalWriteRefusedError(
                "operational writer lock path changed while acquiring the lease"
            )
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


class OperationalService:
    """The only production-facing mutation boundary for the operational store."""

    def __init__(
        self,
        paths: BrainPaths,
        *,
        writer_guard: Callable[[], None] | None = None,
    ) -> None:
        self.paths = paths
        self._writer_guard = writer_guard
        self._mutation_lock = _home_mutation_lock(paths)

    def authority_status(self) -> OperationalAuthorityStatus:
        return operational_authority_status(self.paths)

    def _require_writer_guard(self) -> None:
        if self._writer_guard is None:
            raise OperationalWriteRefusedError(
                "operational mutation requires an active daemon writer lease"
            )
        try:
            self._writer_guard()
        except OperationalWriteRefusedError:
            raise
        except Exception as exc:
            raise OperationalWriteRefusedError(
                "operational daemon writer lease is not active"
            ) from exc

    def _require_authority(self) -> OperationalAuthorityStatus:
        status = self.authority_status()
        if not status.can_write:
            raise OperationalWriteRefusedError(
                f"operational mutation refused: {status.reason}"
            )
        return status

    @contextmanager
    def mutation_lease(self) -> Iterator[OperationalAuthorityStatus]:
        """Hold one bounded mutation lease and revalidate authority inside it."""

        self._require_writer_guard()
        self._require_authority()
        acquired = self._mutation_lock.acquire(
            timeout=OPERATIONAL_WRITER_LOCK_TIMEOUT_SECONDS
        )
        if not acquired:
            raise OperationalWriterBusyError(
                "timed out waiting for the in-process operational writer lease"
            )
        try:
            self._require_writer_guard()
            with _cross_process_mutation_lock(self.paths):
                self._require_writer_guard()
                yield self._require_authority()
        finally:
            self._mutation_lock.release()

    @contextmanager
    def quiesce_mutations(self) -> Iterator[None]:
        """Drain active mutations before the daemon releases its writer lease."""

        acquired = self._mutation_lock.acquire(
            timeout=OPERATIONAL_WRITER_LOCK_TIMEOUT_SECONDS
        )
        if not acquired:
            raise OperationalWriterBusyError(
                "timed out waiting to quiesce operational mutations"
            )
        try:
            yield
        finally:
            self._mutation_lock.release()

    def initialize(self) -> None:
        with self.mutation_lease():
            init_operational_db(self.paths.ops_sqlite_path)

    def reconcile_observation(
        self,
        observation: OperationalObservation,
        *,
        processed_at: str | None = None,
        run_id: str | None = None,
    ) -> ReconciliationResult:
        with self.mutation_lease():
            return reconcile_observation(
                self.paths.ops_sqlite_path,
                observation,
                processed_at=processed_at,
                run_id=run_id,
            )

    def reconcile_source_unit(
        self,
        observations: Sequence[OperationalObservation],
        *,
        cursor_update: SourceCursorUpdate | None = None,
        processed_at: str | None = None,
        run_id: str | None = None,
    ) -> SourceUnitResult:
        with self.mutation_lease():
            return reconcile_source_unit(
                self.paths.ops_sqlite_path,
                observations,
                cursor_update=cursor_update,
                processed_at=processed_at,
                run_id=run_id,
            )

    def record_item_feedback(
        self,
        item_id: str,
        decision: str,
        *,
        note: str | None = None,
        snoozed_until: str | None = None,
        idempotency_key: str | None = None,
        created_at: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        with self.mutation_lease():
            return record_item_feedback(
                self.paths.ops_sqlite_path,
                item_id,
                decision,
                note=note,
                snoozed_until=snoozed_until,
                idempotency_key=idempotency_key,
                created_at=created_at,
                run_id=run_id,
            )

    def save_source_cursor(
        self,
        connector_id: str,
        account_key: str,
        stream_key: str,
        *,
        source_type: str,
        cursor: str | None,
        watermark: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        last_success_at: str | None = None,
        expected_cursor: str | None = None,
        expected_generation: int | None = None,
        enforce_expected_cursor: bool = True,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        with self.mutation_lease():
            return save_source_cursor(
                self.paths.ops_sqlite_path,
                connector_id,
                account_key,
                stream_key,
                source_type=source_type,
                cursor=cursor,
                watermark=watermark,
                metadata=metadata,
                last_success_at=last_success_at,
                expected_cursor=expected_cursor,
                expected_generation=expected_generation,
                enforce_expected_cursor=enforce_expected_cursor,
                updated_at=updated_at,
            )
