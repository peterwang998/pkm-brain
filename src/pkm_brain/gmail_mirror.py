from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .google_normalization import NormalizedGmailMessage, NormalizedGmailThread


GMAIL_MIRROR_SCHEMA_VERSION = 1
GMAIL_MIRROR_SCHEMA_NAME = "create_gmail_mirror"
GMAIL_MIRROR_DIRECTORY_MODE = 0o700
GMAIL_MIRROR_FILE_MODE = 0o600
GMAIL_MIRROR_BUSY_TIMEOUT_MS = 5_000
GMAIL_MIRROR_STREAM = "mailbox"
GMAIL_MIRROR_MAX_PENDING_IDS = 10_000
GMAIL_MIRROR_MAX_PENDING_IDS_BYTES = 500 * 1024
GMAIL_MIRROR_MAX_RAW_PAYLOAD_BYTES = 64 * 1024 * 1024
GMAIL_MIRROR_MAX_NORMALIZED_PAYLOAD_BYTES = 8 * 1024 * 1024
GMAIL_MIRROR_QUARANTINE_RETRY_BASE_SECONDS = 600
GMAIL_MIRROR_QUARANTINE_RETRY_MAX_SECONDS = 86_400
GMAIL_MIRROR_QUEUE_STATES = {
    "pending",
    "processing",
    "completed",
    "deferred",
    "failed",
    "superseded",
}
GMAIL_MIRROR_FINISH_STATES = {"completed", "deferred", "failed"}


class GmailMirrorError(RuntimeError):
    """The durable Gmail mirror cannot safely complete the requested operation."""


class GmailMirrorSecurityError(GmailMirrorError):
    """The mirror path or one of its SQLite files is not private and regular."""


class GmailMirrorConflictError(GmailMirrorError):
    """An immutable provider revision was reused with different content."""


class GmailMirrorGenerationConflict(GmailMirrorError):
    """A stale sync or triage worker attempted to mutate newer mirror state."""


@dataclass(frozen=True)
class GmailMirrorCheckpoint:
    account_key: str
    stream_key: str
    history_id: str | None
    mode: str
    coverage_complete: bool
    reset_required: bool
    continuation_page_token: str | None
    baseline_history_id: str | None
    pending_thread_ids: tuple[str, ...]
    continuation_history_id: str | None
    generation: int
    last_sequence: int
    last_success_at: str | None
    updated_at: str


@dataclass(frozen=True)
class GmailMirrorCheckpointUpdate:
    account_key: str
    history_id: str | None
    mode: str
    coverage_complete: bool
    reset_required: bool
    continuation_page_token: str | None
    baseline_history_id: str | None
    pending_thread_ids: tuple[str, ...]
    continuation_history_id: str | None
    expected_generation: int | None
    last_success_at: str | None
    updated_at: str
    stream_key: str = GMAIL_MIRROR_STREAM

    @classmethod
    def from_fetch_result(
        cls,
        account_key: str,
        result: Any,
        *,
        previous: GmailMirrorCheckpoint | None,
        updated_at: str,
        stream_key: str = GMAIL_MIRROR_STREAM,
    ) -> GmailMirrorCheckpointUpdate:
        """Translate one GmailThreadReader result without coupling to its module."""

        coverage_complete = bool(result.coverage_complete)
        mode = str(result.mode)
        reset_required = bool(
            not coverage_complete
            and (
                result.reset_required
                or (
                    previous is not None
                    and bool(getattr(previous, "reset_required", False))
                )
            )
        )
        if coverage_complete:
            history_id = _optional_text(result.next_history_id, "next_history_id", 256)
        elif mode == "full":
            history_id = None
        else:
            history_id = previous.history_id if previous is not None else None
        return cls(
            account_key=account_key,
            stream_key=stream_key,
            history_id=history_id,
            mode=mode,
            coverage_complete=coverage_complete,
            reset_required=reset_required,
            continuation_page_token=(
                None if coverage_complete else result.continuation_page_token
            ),
            baseline_history_id=(
                None if coverage_complete else result.baseline_history_id
            ),
            pending_thread_ids=(
                () if coverage_complete else tuple(result.pending_thread_ids)
            ),
            continuation_history_id=(
                None if coverage_complete else result.continuation_history_id
            ),
            expected_generation=(previous.generation if previous is not None else None),
            last_success_at=(updated_at if coverage_complete else None),
            updated_at=updated_at,
        ).validated()

    def validated(self) -> GmailMirrorCheckpointUpdate:
        account_key = _identifier(self.account_key, "account_key", 512)
        stream_key = _identifier(self.stream_key, "stream_key", 512)
        mode = self.mode.strip().casefold()
        if mode not in {"full", "incremental"}:
            raise ValueError("Gmail mirror checkpoint mode must be full or incremental")
        history_id = _optional_text(self.history_id, "history_id", 256)
        continuation_page_token = _optional_text(
            self.continuation_page_token,
            "continuation_page_token",
            8_192,
        )
        baseline_history_id = _optional_text(
            self.baseline_history_id,
            "baseline_history_id",
            256,
        )
        continuation_history_id = _optional_text(
            self.continuation_history_id,
            "continuation_history_id",
            256,
        )
        pending_thread_ids = _pending_thread_ids(self.pending_thread_ids)
        if self.coverage_complete:
            if history_id is None:
                raise ValueError("a complete Gmail mirror checkpoint requires history_id")
            if any(
                (
                    continuation_page_token,
                    baseline_history_id,
                    continuation_history_id,
                    pending_thread_ids,
                )
            ):
                raise ValueError(
                    "a complete Gmail mirror checkpoint cannot retain continuation state"
                )
        if mode == "full" and continuation_page_token and baseline_history_id is None:
            raise ValueError("a partial full Gmail mirror checkpoint requires its baseline")
        if mode == "full" and (pending_thread_ids or continuation_history_id):
            raise ValueError("full Gmail mirror checkpoints cannot carry history backlog")
        if mode == "incremental" and baseline_history_id is not None:
            raise ValueError("incremental Gmail mirror checkpoints cannot carry a baseline")
        if pending_thread_ids and continuation_history_id is None:
            raise ValueError("pending Gmail thread IDs require continuation_history_id")
        if self.expected_generation is not None and (
            isinstance(self.expected_generation, bool) or self.expected_generation < 1
        ):
            raise ValueError("expected_generation must be a positive integer or null")
        updated_at = _canonical_timestamp(self.updated_at, "updated_at")
        last_success_at = _optional_timestamp(
            self.last_success_at,
            "last_success_at",
        )
        return GmailMirrorCheckpointUpdate(
            account_key=account_key,
            stream_key=stream_key,
            history_id=history_id,
            mode=mode,
            coverage_complete=bool(self.coverage_complete),
            reset_required=bool(self.reset_required),
            continuation_page_token=continuation_page_token,
            baseline_history_id=baseline_history_id,
            pending_thread_ids=pending_thread_ids,
            continuation_history_id=continuation_history_id,
            expected_generation=self.expected_generation,
            last_success_at=last_success_at,
            updated_at=updated_at,
        )


@dataclass(frozen=True)
class GmailMirrorThreadInput:
    thread: NormalizedGmailThread
    raw_payload: Mapping[str, Any]


@dataclass(frozen=True)
class GmailMirrorQuarantineInput:
    thread_id: str
    source_revision: str | None
    stage: str
    error: str
    payload_sha256: str
    retry_attempt: bool = False
    parser_version: str | None = None


@dataclass(frozen=True)
class GmailMirrorQuarantineRetry:
    thread_id: str
    retry_count: int
    next_retry_at: str | None
    last_retry_at: str | None
    last_parser_version: str | None


@dataclass(frozen=True)
class GmailMirrorRevision:
    account_key: str
    thread_id: str
    source_revision: str
    mirror_sequence: int
    tombstoned: bool
    thread: NormalizedGmailThread | None
    raw_payload: Mapping[str, Any] | None
    stored_at: str


@dataclass(frozen=True)
class GmailMirrorTriageItem:
    account_key: str
    thread_id: str
    source_revision: str
    mirror_sequence: int
    tombstoned: bool
    thread: NormalizedGmailThread | None
    state: str
    generation: int
    attempt_count: int
    enqueued_at: str
    updated_at: str
    available_at: str | None
    lease_expires_at: str | None
    detector_version: str | None
    policy_version: str | None
    last_error: str | None


@dataclass(frozen=True)
class GmailMirrorSyncResult:
    checkpoint: GmailMirrorCheckpoint
    inserted_revisions: int
    current_updates: int
    tombstones: int
    queued: int
    superseded: int
    quarantined: int


class GmailMirrorStore:
    """Owner-only, provider-faithful Gmail mirror and independent triage queue."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.expanduser().absolute()

    def initialize(self) -> None:
        _prepare_mirror_file(self.db_path)
        with self._connect(require_initialized=False) as conn:
            existing_tables = {
                str(row[0])
                for row in conn.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    """
                )
            }
            if existing_tables and "gmail_mirror_schema_migrations" not in existing_tables:
                raise GmailMirrorError(
                    "refusing to initialize Gmail mirror tables in another database"
                )
            conn.executescript(_SCHEMA_SQL)
            _ensure_quarantine_retry_columns(conn)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_gmail_mirror_quarantine_retry
                ON gmail_mirror_quarantine(
                  account_key, resolved_at, next_retry_at, thread_id
                )
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO gmail_mirror_schema_migrations(
                  version, name, applied_at
                ) VALUES (?, ?, ?)
                """,
                (
                    GMAIL_MIRROR_SCHEMA_VERSION,
                    GMAIL_MIRROR_SCHEMA_NAME,
                    _now_iso(),
                ),
            )
        _secure_mirror_files(self.db_path)
        with self._connect() as conn:
            _verify_schema(conn)

    def get_checkpoint(
        self,
        account_key: str,
        stream_key: str = GMAIL_MIRROR_STREAM,
    ) -> GmailMirrorCheckpoint | None:
        account = _identifier(account_key, "account_key", 512)
        stream = _identifier(stream_key, "stream_key", 512)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM gmail_mirror_sync_state
                WHERE account_key = ? AND stream_key = ?
                """,
                (account, stream),
            ).fetchone()
        return _checkpoint_from_row(row) if row is not None else None

    def apply_sync_unit(
        self,
        update: GmailMirrorCheckpointUpdate,
        threads: Sequence[GmailMirrorThreadInput],
        *,
        missing_thread_ids: Sequence[str] = (),
        quarantined_threads: Sequence[GmailMirrorQuarantineInput] = (),
        quarantine_retry: bool = False,
        parser_version: str | None = None,
    ) -> GmailMirrorSyncResult:
        update = update.validated()
        prepared_threads = _validated_thread_inputs(threads)
        missing = _validated_missing_thread_ids(missing_thread_ids)
        provider_quarantines = _validated_quarantine_inputs(quarantined_threads)
        parser = _optional_text(parser_version, "parser_version", 256)
        provider_quarantines = tuple(
            replace(
                failure,
                retry_attempt=bool(quarantine_retry or failure.retry_attempt),
                parser_version=failure.parser_version or parser,
            )
            for failure in provider_quarantines
        )
        accepted_ids = {
            item.thread.thread_id.strip()
            for item in prepared_threads
            if isinstance(item.thread.thread_id, str)
            and item.thread.thread_id.strip()
            and len(item.thread.thread_id.strip()) <= 2_000
            and "\x00" not in item.thread.thread_id
        }
        overlap = set(missing).intersection(accepted_ids)
        if overlap:
            raise ValueError(
                "a Gmail mirror sync unit cannot both retain and tombstone a thread"
            )
        inserted_revisions = 0
        current_updates = 0
        tombstones = 0
        queued = 0
        superseded = 0
        quarantined = 0
        with self._transaction() as conn:
            current_state = conn.execute(
                """
                SELECT * FROM gmail_mirror_sync_state
                WHERE account_key = ? AND stream_key = ?
                """,
                (update.account_key, update.stream_key),
            ).fetchone()
            current_generation = (
                int(current_state["generation"]) if current_state is not None else None
            )
            if current_generation != update.expected_generation:
                raise GmailMirrorGenerationConflict(
                    "Gmail mirror checkpoint changed concurrently: "
                    f"expected generation {update.expected_generation!r}, "
                    f"found {current_generation!r}"
                )
            last_sequence = (
                int(current_state["last_sequence"]) if current_state is not None else 0
            )
            for failure in provider_quarantines:
                quarantined += _record_quarantine(
                    conn,
                    account_key=update.account_key,
                    failure=failure,
                    updated_at=update.updated_at,
                )
            for item in prepared_threads:
                counters_before = (
                    last_sequence,
                    inserted_revisions,
                    current_updates,
                    queued,
                    superseded,
                )
                conn.execute("SAVEPOINT gmail_mirror_thread")
                try:
                    thread_id = _identifier(
                        item.thread.thread_id,
                        "thread_id",
                        2_000,
                    )
                    if not isinstance(item.raw_payload, Mapping):
                        raise ValueError("raw Gmail thread payload must be an object")
                    raw_id = str(item.raw_payload.get("id") or "").strip()
                    if raw_id != thread_id:
                        raise ValueError(
                            "raw and normalized Gmail thread IDs do not match"
                        )
                    source_revision = gmail_mirror_source_revision(item.thread)
                    normalized_json = _bounded_json(
                        item.thread.as_dict(),
                        "normalized Gmail thread",
                        GMAIL_MIRROR_MAX_NORMALIZED_PAYLOAD_BYTES,
                    )
                    raw_json = _bounded_json(
                        dict(item.raw_payload),
                        "raw Gmail thread",
                        GMAIL_MIRROR_MAX_RAW_PAYLOAD_BYTES,
                    )
                    content_sha256 = _revision_hash(
                        tombstoned=False,
                        normalized_json=normalized_json,
                        raw_json=raw_json,
                    )
                    existing_revision = _revision_row(
                        conn,
                        update.account_key,
                        thread_id,
                        source_revision,
                    )
                    current_pointer = _current_pointer_row(
                        conn,
                        update.account_key,
                        thread_id,
                    )
                    pointer_changed = (
                        current_pointer is None
                        or str(current_pointer["current_revision"]) != source_revision
                        or bool(current_pointer["tombstoned"])
                    )
                    if existing_revision is not None:
                        _assert_revision_content(
                            existing_revision,
                            content_sha256=content_sha256,
                            tombstoned=False,
                        )
                    if pointer_changed:
                        last_sequence += 1
                        if existing_revision is None:
                            conn.execute(
                                """
                                INSERT INTO gmail_mirror_thread_revisions(
                                  account_key, thread_id, source_revision,
                                  first_seen_sequence, tombstoned, normalized_payload,
                                  raw_payload, content_sha256, provider_updated_at, stored_at
                                ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                                """,
                                (
                                    update.account_key,
                                    thread_id,
                                    source_revision,
                                    last_sequence,
                                    normalized_json,
                                    raw_json,
                                    content_sha256,
                                    item.thread.updated_at,
                                    update.updated_at,
                                ),
                            )
                            inserted_revisions += 1
                        _upsert_current_pointer(
                            conn,
                            account_key=update.account_key,
                            thread_id=thread_id,
                            source_revision=source_revision,
                            mirror_sequence=last_sequence,
                            tombstoned=False,
                            updated_at=update.updated_at,
                        )
                        current_updates += 1
                        superseded += _supersede_older_queue_items(
                            conn,
                            account_key=update.account_key,
                            thread_id=thread_id,
                            source_revision=source_revision,
                            updated_at=update.updated_at,
                        )
                        queued += _enqueue_revision(
                            conn,
                            account_key=update.account_key,
                            thread_id=thread_id,
                            source_revision=source_revision,
                            mirror_sequence=last_sequence,
                            updated_at=update.updated_at,
                        )
                    _resolve_quarantines(
                        conn,
                        account_key=update.account_key,
                        thread_id=thread_id,
                        updated_at=update.updated_at,
                    )
                except (GmailMirrorConflictError, TypeError, ValueError) as exc:
                    conn.execute("ROLLBACK TO SAVEPOINT gmail_mirror_thread")
                    conn.execute("RELEASE SAVEPOINT gmail_mirror_thread")
                    (
                        last_sequence,
                        inserted_revisions,
                        current_updates,
                        queued,
                        superseded,
                    ) = counters_before
                    isolated = _quarantine_from_thread_input(item, exc)
                    isolated = replace(
                        isolated,
                        retry_attempt=bool(quarantine_retry),
                        parser_version=parser,
                    )
                    quarantined += _record_quarantine(
                        conn,
                        account_key=update.account_key,
                        failure=isolated,
                        updated_at=update.updated_at,
                    )
                    continue
                conn.execute("RELEASE SAVEPOINT gmail_mirror_thread")
            tombstone_marker = _checkpoint_marker(update)
            for thread_id in missing:
                source_revision = gmail_mirror_tombstone_revision(
                    thread_id,
                    tombstone_marker,
                )
                content_sha256 = _revision_hash(
                    tombstoned=True,
                    normalized_json=None,
                    raw_json=None,
                )
                existing_revision = _revision_row(
                    conn,
                    update.account_key,
                    thread_id,
                    source_revision,
                )
                current_pointer = _current_pointer_row(
                    conn,
                    update.account_key,
                    thread_id,
                )
                pointer_changed = (
                    current_pointer is None
                    or str(current_pointer["current_revision"]) != source_revision
                    or not bool(current_pointer["tombstoned"])
                )
                if existing_revision is not None:
                    _assert_revision_content(
                        existing_revision,
                        content_sha256=content_sha256,
                        tombstoned=True,
                    )
                if not pointer_changed:
                    _resolve_quarantines(
                        conn,
                        account_key=update.account_key,
                        thread_id=thread_id,
                        updated_at=update.updated_at,
                    )
                    continue
                last_sequence += 1
                if existing_revision is None:
                    conn.execute(
                        """
                        INSERT INTO gmail_mirror_thread_revisions(
                          account_key, thread_id, source_revision,
                          first_seen_sequence, tombstoned, normalized_payload,
                          raw_payload, content_sha256, provider_updated_at, stored_at
                        ) VALUES (?, ?, ?, ?, 1, NULL, NULL, ?, NULL, ?)
                        """,
                        (
                            update.account_key,
                            thread_id,
                            source_revision,
                            last_sequence,
                            content_sha256,
                            update.updated_at,
                        ),
                    )
                    inserted_revisions += 1
                _upsert_current_pointer(
                    conn,
                    account_key=update.account_key,
                    thread_id=thread_id,
                    source_revision=source_revision,
                    mirror_sequence=last_sequence,
                    tombstoned=True,
                    updated_at=update.updated_at,
                )
                current_updates += 1
                tombstones += 1
                superseded += _supersede_older_queue_items(
                    conn,
                    account_key=update.account_key,
                    thread_id=thread_id,
                    source_revision=source_revision,
                    updated_at=update.updated_at,
                )
                queued += _enqueue_revision(
                    conn,
                    account_key=update.account_key,
                    thread_id=thread_id,
                    source_revision=source_revision,
                    mirror_sequence=last_sequence,
                    updated_at=update.updated_at,
                )
                _resolve_quarantines(
                    conn,
                    account_key=update.account_key,
                    thread_id=thread_id,
                    updated_at=update.updated_at,
                )
            checkpoint = _save_checkpoint(
                conn,
                update,
                generation=(current_generation or 0) + 1,
                last_sequence=last_sequence,
                previous_last_success_at=(
                    str(current_state["last_success_at"])
                    if current_state is not None and current_state["last_success_at"]
                    else None
                ),
            )
        return GmailMirrorSyncResult(
            checkpoint=checkpoint,
            inserted_revisions=inserted_revisions,
            current_updates=current_updates,
            tombstones=tombstones,
            queued=queued,
            superseded=superseded,
            quarantined=quarantined,
        )

    def get_current_revision(
        self,
        account_key: str,
        thread_id: str,
    ) -> GmailMirrorRevision | None:
        account = _identifier(account_key, "account_key", 512)
        thread = _identifier(thread_id, "thread_id", 2_000)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT r.*, c.current_sequence AS mirror_sequence
                FROM gmail_mirror_threads c
                JOIN gmail_mirror_thread_revisions r
                  ON r.account_key = c.account_key
                 AND r.thread_id = c.thread_id
                 AND r.source_revision = c.current_revision
                WHERE c.account_key = ? AND c.thread_id = ?
                """,
                (account, thread),
            ).fetchone()
        return _revision_from_row(row) if row is not None else None

    def get_revision(
        self,
        account_key: str,
        thread_id: str,
        source_revision: str,
    ) -> GmailMirrorRevision | None:
        account = _identifier(account_key, "account_key", 512)
        thread = _identifier(thread_id, "thread_id", 2_000)
        revision = _identifier(source_revision, "source_revision", 2_000)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT r.*, r.first_seen_sequence AS mirror_sequence
                FROM gmail_mirror_thread_revisions r
                WHERE account_key = ? AND thread_id = ? AND source_revision = ?
                """,
                (account, thread, revision),
            ).fetchone()
        return _revision_from_row(row) if row is not None else None

    def list_pending_triage(
        self,
        account_key: str,
        *,
        limit: int = 200,
        as_of: str | None = None,
    ) -> tuple[GmailMirrorTriageItem, ...]:
        account = _identifier(account_key, "account_key", 512)
        bounded_limit = _positive_limit(limit)
        timestamp = _canonical_timestamp(as_of or _now_iso(), "as_of")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT q.*, r.tombstoned, r.normalized_payload
                FROM gmail_mirror_triage_queue q
                JOIN gmail_mirror_thread_revisions r
                  ON r.account_key = q.account_key
                 AND r.thread_id = q.thread_id
                 AND r.source_revision = q.source_revision
                JOIN gmail_mirror_threads c
                  ON c.account_key = q.account_key
                 AND c.thread_id = q.thread_id
                 AND c.current_revision = q.source_revision
                WHERE q.account_key = ?
                  AND q.state IN ('pending', 'deferred', 'failed')
                  AND (q.available_at IS NULL OR q.available_at <= ?)
                ORDER BY q.mirror_sequence, q.thread_id
                LIMIT ?
                """,
                (account, timestamp, bounded_limit),
            ).fetchall()
        return tuple(_triage_from_row(row) for row in rows)

    def triage_counts(self, account_key: str) -> dict[str, int]:
        """Return bounded queue health without loading any mirrored message content."""

        account = _identifier(account_key, "account_key", 512)
        timestamp = _now_iso()
        counts = {state: 0 for state in sorted(GMAIL_MIRROR_QUEUE_STATES)}
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT state, COUNT(*) AS item_count
                FROM gmail_mirror_triage_queue
                WHERE account_key = ?
                GROUP BY state
                """,
                (account,),
            ).fetchall()
            quarantine_row = conn.execute(
                """
                SELECT COUNT(DISTINCT thread_id) AS item_count,
                       COUNT(DISTINCT CASE
                         WHEN next_retry_at IS NULL OR next_retry_at <= ?
                         THEN thread_id END
                       ) AS due_count
                FROM gmail_mirror_quarantine
                WHERE account_key = ? AND resolved_at IS NULL
                """,
                (timestamp, account),
            ).fetchone()
        for row in rows:
            state = str(row["state"])
            if state not in counts:
                raise GmailMirrorError(f"Gmail mirror queue contains invalid state: {state}")
            counts[state] = int(row["item_count"])
        counts["quarantined_count"] = int(quarantine_row["item_count"] or 0)
        counts["quarantine_retry_due_count"] = int(
            quarantine_row["due_count"] or 0
        )
        counts["quarantine_retry_deferred_count"] = max(
            0,
            counts["quarantined_count"]
            - counts["quarantine_retry_due_count"],
        )
        counts["backlog_count"] = sum(
            counts[state] for state in ("pending", "processing", "deferred", "failed")
        ) + counts["quarantined_count"]
        counts["total_count"] = sum(
            counts[state] for state in GMAIL_MIRROR_QUEUE_STATES
        ) + counts["quarantined_count"]
        return counts

    def quarantine_counts(self, account_key: str) -> dict[str, int]:
        """Return content-free provider isolation health for one account."""

        account = _identifier(account_key, "account_key", 512)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT thread_id) AS total_count,
                       COUNT(DISTINCT CASE WHEN resolved_at IS NULL
                         THEN thread_id END) AS unresolved_count
                FROM gmail_mirror_quarantine
                WHERE account_key = ?
                """,
                (account,),
            ).fetchone()
        assert row is not None
        return {
            "total_count": int(row["total_count"] or 0),
            "unresolved_count": int(row["unresolved_count"] or 0),
        }

    def list_due_quarantine_retries(
        self,
        account_key: str,
        *,
        parser_version: str,
        as_of: str | None = None,
        limit: int = 10,
    ) -> tuple[GmailMirrorQuarantineRetry, ...]:
        """Return bounded, content-free retry state grouped by provider thread."""

        account = _identifier(account_key, "account_key", 512)
        parser = _identifier(parser_version, "parser_version", 256)
        timestamp = _canonical_timestamp(as_of or _now_iso(), "as_of")
        bounded_limit = _positive_limit(limit)
        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH retry_threads AS (
                  SELECT thread_id,
                         MAX(retry_count) AS retry_count,
                         MIN(next_retry_at) AS next_retry_at,
                         MAX(last_retry_at) AS last_retry_at,
                         MAX(last_parser_version) AS last_parser_version
                  FROM gmail_mirror_quarantine
                  WHERE account_key = ? AND resolved_at IS NULL
                  GROUP BY thread_id
                )
                SELECT * FROM retry_threads
                WHERE next_retry_at IS NULL OR next_retry_at <= ?
                   OR last_parser_version IS NULL
                   OR last_parser_version != ?
                ORDER BY COALESCE(next_retry_at, ''), thread_id
                LIMIT ?
                """,
                (account, timestamp, parser, bounded_limit),
            ).fetchall()
        return tuple(
            GmailMirrorQuarantineRetry(
                thread_id=str(row["thread_id"]),
                retry_count=int(row["retry_count"] or 0),
                next_retry_at=(
                    str(row["next_retry_at"])
                    if row["next_retry_at"] is not None
                    else None
                ),
                last_retry_at=(
                    str(row["last_retry_at"])
                    if row["last_retry_at"] is not None
                    else None
                ),
                last_parser_version=(
                    str(row["last_parser_version"])
                    if row["last_parser_version"] is not None
                    else None
                ),
            )
            for row in rows
        )

    def claim_pending_triage(
        self,
        account_key: str,
        *,
        limit: int = 200,
        claimed_at: str | None = None,
        lease_seconds: int = 900,
        detector_version: str | None = None,
        policy_version: str | None = None,
    ) -> tuple[GmailMirrorTriageItem, ...]:
        account = _identifier(account_key, "account_key", 512)
        bounded_limit = _positive_limit(limit)
        if (
            isinstance(lease_seconds, bool)
            or lease_seconds <= 0
            or lease_seconds > 86_400
        ):
            raise ValueError("lease_seconds must be between 1 and 86400")
        timestamp = _canonical_timestamp(claimed_at or _now_iso(), "claimed_at")
        detector_context = _optional_text(
            detector_version,
            "detector_version",
            256,
        )
        policy_context = _optional_text(policy_version, "policy_version", 256)
        if (detector_context is None) != (policy_context is None):
            raise ValueError(
                "detector_version and policy_version must be provided together"
            )
        lease_expires_at = (
            _parse_timestamp(timestamp) + timedelta(seconds=lease_seconds)
        ).replace(microsecond=0).isoformat()
        claimed: list[GmailMirrorTriageItem] = []
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE gmail_mirror_triage_queue
                SET state = 'deferred', available_at = ?, lease_expires_at = NULL,
                    last_error = COALESCE(last_error, 'triage lease expired'),
                    generation = generation + 1, updated_at = ?
                WHERE account_key = ? AND state = 'processing'
                  AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                """,
                (timestamp, timestamp, account, timestamp),
            )
            if detector_context is None:
                candidates = conn.execute(
                    """
                    SELECT q.account_key, q.thread_id, q.source_revision, q.generation
                    FROM gmail_mirror_triage_queue q
                    JOIN gmail_mirror_threads c
                      ON c.account_key = q.account_key
                     AND c.thread_id = q.thread_id
                     AND c.current_revision = q.source_revision
                    WHERE q.account_key = ?
                      AND q.state IN ('pending', 'deferred', 'failed')
                      AND (q.available_at IS NULL OR q.available_at <= ?)
                    ORDER BY q.mirror_sequence, q.thread_id
                    LIMIT ?
                    """,
                    (account, timestamp, bounded_limit),
                ).fetchall()
            else:
                candidates = conn.execute(
                    """
                    SELECT q.account_key, q.thread_id, q.source_revision, q.generation
                    FROM gmail_mirror_triage_queue q
                    JOIN gmail_mirror_threads c
                      ON c.account_key = q.account_key
                     AND c.thread_id = q.thread_id
                     AND c.current_revision = q.source_revision
                    WHERE q.account_key = ? AND (
                      (
                        q.state IN ('pending', 'deferred', 'failed')
                        AND (q.available_at IS NULL OR q.available_at <= ?)
                      )
                      OR
                      (
                        q.state = 'completed'
                        AND (
                          q.detector_version IS NOT ?
                          OR q.policy_version IS NOT ?
                        )
                      )
                    )
                    ORDER BY q.mirror_sequence, q.thread_id
                    LIMIT ?
                    """,
                    (
                        account,
                        timestamp,
                        detector_context,
                        policy_context,
                        bounded_limit,
                    ),
                ).fetchall()
            for candidate in candidates:
                cursor = conn.execute(
                    """
                    UPDATE gmail_mirror_triage_queue
                    SET state = 'processing', attempt_count = attempt_count + 1,
                        generation = generation + 1, available_at = NULL,
                        lease_expires_at = ?, updated_at = ?
                    WHERE account_key = ? AND thread_id = ? AND source_revision = ?
                      AND generation = ?
                      AND state IN ('pending', 'deferred', 'failed', 'completed')
                    """,
                    (
                        lease_expires_at,
                        timestamp,
                        account,
                        str(candidate["thread_id"]),
                        str(candidate["source_revision"]),
                        int(candidate["generation"]),
                    ),
                )
                if cursor.rowcount != 1:
                    raise GmailMirrorGenerationConflict(
                        "Gmail triage item changed while it was being claimed"
                    )
                row = _triage_row(
                    conn,
                    account,
                    str(candidate["thread_id"]),
                    str(candidate["source_revision"]),
                )
                assert row is not None
                claimed.append(_triage_from_row(row))
        return tuple(claimed)

    def finish_triage(
        self,
        account_key: str,
        thread_id: str,
        source_revision: str,
        *,
        expected_generation: int,
        state: str,
        updated_at: str | None = None,
        detector_version: str | None = None,
        policy_version: str | None = None,
        available_at: str | None = None,
        error: str | None = None,
    ) -> GmailMirrorTriageItem:
        account = _identifier(account_key, "account_key", 512)
        thread = _identifier(thread_id, "thread_id", 2_000)
        revision = _identifier(source_revision, "source_revision", 2_000)
        if isinstance(expected_generation, bool) or expected_generation < 1:
            raise ValueError("expected_generation must be a positive integer")
        normalized_state = state.strip().casefold()
        if normalized_state not in GMAIL_MIRROR_FINISH_STATES:
            raise ValueError("triage state must be completed, deferred, or failed")
        timestamp = _canonical_timestamp(updated_at or _now_iso(), "updated_at")
        detector = _optional_text(detector_version, "detector_version", 256)
        policy = _optional_text(policy_version, "policy_version", 256)
        next_available = _optional_timestamp(available_at, "available_at")
        last_error = _optional_text(error, "error", 4_000)
        if normalized_state == "completed" and (detector is None or policy is None):
            raise ValueError("completed triage requires detector and policy versions")
        if normalized_state == "deferred" and next_available is None:
            raise ValueError("deferred triage requires available_at")
        if normalized_state == "failed" and last_error is None:
            raise ValueError("failed triage requires an error")
        if normalized_state != "deferred" and next_available is not None:
            raise ValueError("available_at is only valid for deferred triage")
        with self._transaction() as conn:
            current = _current_pointer_row(conn, account, thread)
            if current is None or str(current["current_revision"]) != revision:
                raise GmailMirrorGenerationConflict(
                    "Gmail triage result is no longer the current thread revision"
                )
            cursor = conn.execute(
                """
                UPDATE gmail_mirror_triage_queue
                SET state = ?, generation = generation + 1,
                    available_at = ?, lease_expires_at = NULL,
                    detector_version = ?, policy_version = ?, last_error = ?,
                    updated_at = ?
                WHERE account_key = ? AND thread_id = ? AND source_revision = ?
                  AND generation = ? AND state = 'processing'
                """,
                (
                    normalized_state,
                    next_available,
                    detector,
                    policy,
                    last_error,
                    timestamp,
                    account,
                    thread,
                    revision,
                    expected_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise GmailMirrorGenerationConflict(
                    "Gmail triage item changed concurrently or its lease is not active"
                )
            row = _triage_row(conn, account, thread, revision)
            assert row is not None
            result = _triage_from_row(row)
        return result

    @contextmanager
    def _connect(
        self,
        *,
        require_initialized: bool = True,
    ) -> Iterator[sqlite3.Connection]:
        _assert_private_mirror(self.db_path)
        uri = f"{self.db_path.as_uri()}?mode=rw"
        try:
            conn = sqlite3.connect(
                uri,
                uri=True,
                timeout=GMAIL_MIRROR_BUSY_TIMEOUT_MS / 1_000,
            )
        except sqlite3.DatabaseError as exc:
            raise GmailMirrorError(f"Gmail mirror cannot be opened: {self.db_path}") from exc
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout={GMAIL_MIRROR_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA foreign_keys=ON")
            mode = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0])
            if mode.casefold() != "wal":
                raise GmailMirrorError("Gmail mirror must use WAL journal mode")
            if require_initialized:
                _verify_schema(conn)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
            _secure_mirror_files(self.db_path)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn


def gmail_mirror_source_revision(thread: NormalizedGmailThread) -> str:
    if thread.source_revision and thread.source_revision.strip():
        return _identifier(thread.source_revision, "source_revision", 2_000)
    payload = json.dumps(
        thread.as_dict(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "gmail-normalized-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def gmail_mirror_tombstone_revision(thread_id: str, checkpoint_marker: str) -> str:
    thread = _identifier(thread_id, "thread_id", 2_000)
    marker = _identifier(checkpoint_marker, "checkpoint_marker", 2_000)
    payload = json.dumps([thread, marker], ensure_ascii=True, separators=(",", ":"))
    return "gmail-tombstone-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validated_thread_inputs(
    values: Sequence[GmailMirrorThreadInput],
) -> tuple[GmailMirrorThreadInput, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("Gmail mirror thread inputs must be a sequence")
    output: list[GmailMirrorThreadInput] = []
    for value in values:
        if not isinstance(value, GmailMirrorThreadInput):
            raise ValueError("Gmail mirror thread inputs must be GmailMirrorThreadInput")
        output.append(value)
    return tuple(output)


def _validated_quarantine_inputs(
    values: Sequence[GmailMirrorQuarantineInput],
) -> tuple[GmailMirrorQuarantineInput, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("quarantined_threads must be a sequence")
    output: list[GmailMirrorQuarantineInput] = []
    for value in values:
        if not isinstance(value, GmailMirrorQuarantineInput):
            raise ValueError(
                "quarantined_threads must contain GmailMirrorQuarantineInput values"
            )
        thread_id = _safe_quarantine_thread_id(value.thread_id)
        source_revision = _safe_optional_quarantine_text(
            value.source_revision,
            maximum=2_000,
        )
        stage = _safe_quarantine_text(value.stage, maximum=128, fallback="provider")
        error = _safe_quarantine_text(
            value.error,
            maximum=1_000,
            fallback="provider thread could not be normalized",
        )
        parser_version = _safe_optional_quarantine_text(
            value.parser_version,
            maximum=256,
        )
        try:
            payload_sha256 = _sha256_identifier(value.payload_sha256)
        except ValueError:
            payload_sha256 = hashlib.sha256(
                _canonical_json(
                    [thread_id, source_revision, stage, error, "invalid-payload-digest"]
                ).encode("utf-8")
            ).hexdigest()
        output.append(
            GmailMirrorQuarantineInput(
                thread_id=thread_id,
                source_revision=source_revision,
                stage=stage,
                error=error,
                payload_sha256=payload_sha256,
                retry_attempt=bool(value.retry_attempt),
                parser_version=parser_version,
            )
        )
    return tuple(output)


def _safe_quarantine_thread_id(value: Any) -> str:
    if isinstance(value, str):
        normalized = value.strip()
        if normalized and len(normalized) <= 2_000 and "\x00" not in normalized:
            return normalized
    return "invalid-thread-" + hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def _safe_optional_quarantine_text(value: Any, *, maximum: int) -> str | None:
    if value is None:
        return None
    normalized = str(value).replace("\x00", " ").strip()
    return normalized[:maximum] or None


def _safe_quarantine_text(value: Any, *, maximum: int, fallback: str) -> str:
    return _safe_optional_quarantine_text(value, maximum=maximum) or fallback


def _validated_missing_thread_ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("missing_thread_ids must be a sequence of thread IDs")
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        thread_id = _identifier(value, "thread_id", 2_000)
        if thread_id in seen:
            raise ValueError("missing_thread_ids cannot contain duplicates")
        seen.add(thread_id)
        output.append(thread_id)
    return tuple(output)


def _quarantine_from_thread_input(
    item: GmailMirrorThreadInput,
    error: Exception,
) -> GmailMirrorQuarantineInput:
    raw_thread_id = item.thread.thread_id
    thread_id = (
        raw_thread_id.strip()
        if isinstance(raw_thread_id, str)
        and raw_thread_id.strip()
        and len(raw_thread_id.strip()) <= 2_000
        and "\x00" not in raw_thread_id
        else "invalid-thread-"
        + hashlib.sha256(repr(raw_thread_id).encode("utf-8")).hexdigest()
    )
    raw_revision = str(item.thread.source_revision or "").strip()
    source_revision = (
        raw_revision
        if raw_revision and len(raw_revision) <= 2_000 and "\x00" not in raw_revision
        else None
    )
    try:
        if not isinstance(item.raw_payload, Mapping):
            raise TypeError("raw payload is not a mapping")
        encoded = _canonical_json(dict(item.raw_payload)).encode("utf-8")
    except (TypeError, ValueError):
        encoded = _canonical_json(
            {
                "thread_id": thread_id,
                "source_revision": source_revision,
                "payload": "not-json-serializable",
            }
        ).encode("utf-8")
    detail = f"{type(error).__name__}: {error}".replace("\x00", " ").strip()
    return GmailMirrorQuarantineInput(
        thread_id=thread_id,
        source_revision=source_revision,
        stage="mirror",
        error=(detail or type(error).__name__)[:1_000],
        payload_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def _record_quarantine(
    conn: sqlite3.Connection,
    *,
    account_key: str,
    failure: GmailMirrorQuarantineInput,
    updated_at: str,
) -> int:
    fingerprint = hashlib.sha256(
        _canonical_json(
            [
                failure.thread_id,
                failure.source_revision,
                failure.stage,
                failure.error,
                failure.payload_sha256,
            ]
        ).encode("utf-8")
    ).hexdigest()
    retry_count = 0
    last_retry_at: str | None = None
    next_retry_at = _quarantine_next_retry_at(updated_at, retry_count=0)
    if failure.retry_attempt:
        row = conn.execute(
            """
            SELECT MAX(retry_count) AS retry_count
            FROM gmail_mirror_quarantine
            WHERE account_key = ? AND thread_id = ? AND resolved_at IS NULL
            """,
            (account_key, failure.thread_id),
        ).fetchone()
        retry_count = int(row["retry_count"] or 0) + 1
        last_retry_at = updated_at
        next_retry_at = _quarantine_next_retry_at(
            updated_at,
            retry_count=retry_count,
        )
        conn.execute(
            """
            UPDATE gmail_mirror_quarantine
            SET retry_count = ?, next_retry_at = ?, last_retry_at = ?,
                last_parser_version = COALESCE(?, last_parser_version)
            WHERE account_key = ? AND thread_id = ? AND resolved_at IS NULL
            """,
            (
                retry_count,
                next_retry_at,
                last_retry_at,
                failure.parser_version,
                account_key,
                failure.thread_id,
            ),
        )
    conn.execute(
        """
        INSERT INTO gmail_mirror_quarantine(
          account_key, thread_id, source_revision, failure_fingerprint,
          stage, error, payload_sha256, occurrence_count,
          retry_count, first_seen_at, last_seen_at, next_retry_at,
          last_retry_at, last_parser_version, resolved_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT(account_key, thread_id, failure_fingerprint) DO UPDATE SET
          occurrence_count = gmail_mirror_quarantine.occurrence_count + 1,
          last_seen_at = excluded.last_seen_at,
          retry_count = CASE
            WHEN gmail_mirror_quarantine.resolved_at IS NOT NULL
              THEN excluded.retry_count
            ELSE MAX(gmail_mirror_quarantine.retry_count, excluded.retry_count)
          END,
          next_retry_at = CASE
            WHEN gmail_mirror_quarantine.resolved_at IS NOT NULL
              THEN excluded.next_retry_at
            WHEN excluded.last_retry_at IS NOT NULL
              THEN excluded.next_retry_at
            ELSE COALESCE(
              gmail_mirror_quarantine.next_retry_at,
              excluded.next_retry_at
            )
          END,
          last_retry_at = CASE
            WHEN gmail_mirror_quarantine.resolved_at IS NOT NULL
              THEN excluded.last_retry_at
            ELSE COALESCE(
              excluded.last_retry_at,
              gmail_mirror_quarantine.last_retry_at
            )
          END,
          last_parser_version = COALESCE(
            excluded.last_parser_version,
            gmail_mirror_quarantine.last_parser_version
          ),
          resolved_at = NULL
        """,
        (
            account_key,
            failure.thread_id,
            failure.source_revision,
            fingerprint,
            failure.stage,
            failure.error,
            failure.payload_sha256,
            retry_count,
            updated_at,
            updated_at,
            next_retry_at,
            last_retry_at,
            failure.parser_version,
        ),
    )
    return 1


def _quarantine_next_retry_at(updated_at: str, *, retry_count: int) -> str:
    exponent = min(max(0, int(retry_count)), 20)
    delay = min(
        GMAIL_MIRROR_QUARANTINE_RETRY_MAX_SECONDS,
        GMAIL_MIRROR_QUARANTINE_RETRY_BASE_SECONDS * (2**exponent),
    )
    return (
        _parse_timestamp(updated_at) + timedelta(seconds=delay)
    ).replace(microsecond=0).isoformat()


def _resolve_quarantines(
    conn: sqlite3.Connection,
    *,
    account_key: str,
    thread_id: str,
    updated_at: str,
) -> None:
    conn.execute(
        """
        UPDATE gmail_mirror_quarantine
        SET resolved_at = ?
        WHERE account_key = ? AND thread_id = ? AND resolved_at IS NULL
        """,
        (updated_at, account_key, thread_id),
    )


def _sha256_identifier(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("payload_sha256 must be a string")
    normalized = value.strip().casefold()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError("payload_sha256 must be a lowercase SHA-256 digest")
    return normalized


def _pending_thread_ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("pending_thread_ids must be a sequence of thread IDs")
    if len(values) > GMAIL_MIRROR_MAX_PENDING_IDS:
        raise ValueError("pending_thread_ids exceeds its durable count bound")
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        thread_id = _identifier(value, "pending thread ID", 256)
        if thread_id not in seen:
            output.append(thread_id)
            seen.add(thread_id)
    encoded = _canonical_json(output)
    if len(encoded.encode("utf-8")) > GMAIL_MIRROR_MAX_PENDING_IDS_BYTES:
        raise ValueError("pending_thread_ids exceeds its durable byte bound")
    return tuple(output)


def _revision_row(
    conn: sqlite3.Connection,
    account_key: str,
    thread_id: str,
    source_revision: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM gmail_mirror_thread_revisions
        WHERE account_key = ? AND thread_id = ? AND source_revision = ?
        """,
        (account_key, thread_id, source_revision),
    ).fetchone()


def _current_pointer_row(
    conn: sqlite3.Connection,
    account_key: str,
    thread_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM gmail_mirror_threads
        WHERE account_key = ? AND thread_id = ?
        """,
        (account_key, thread_id),
    ).fetchone()


def _assert_revision_content(
    row: sqlite3.Row,
    *,
    content_sha256: str,
    tombstoned: bool,
) -> None:
    if str(row["content_sha256"]) != content_sha256 or bool(
        row["tombstoned"]
    ) != tombstoned:
        raise GmailMirrorConflictError(
            "immutable Gmail revision has conflicting content: "
            f"{row['thread_id']}@{row['source_revision']}"
        )


def _upsert_current_pointer(
    conn: sqlite3.Connection,
    *,
    account_key: str,
    thread_id: str,
    source_revision: str,
    mirror_sequence: int,
    tombstoned: bool,
    updated_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO gmail_mirror_threads(
          account_key, thread_id, current_revision, current_sequence,
          tombstoned, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_key, thread_id) DO UPDATE SET
          current_revision = excluded.current_revision,
          current_sequence = excluded.current_sequence,
          tombstoned = excluded.tombstoned,
          updated_at = excluded.updated_at
        """,
        (
            account_key,
            thread_id,
            source_revision,
            mirror_sequence,
            int(tombstoned),
            updated_at,
        ),
    )


def _supersede_older_queue_items(
    conn: sqlite3.Connection,
    *,
    account_key: str,
    thread_id: str,
    source_revision: str,
    updated_at: str,
) -> int:
    cursor = conn.execute(
        """
        UPDATE gmail_mirror_triage_queue
        SET state = 'superseded', generation = generation + 1,
            lease_expires_at = NULL, available_at = NULL, updated_at = ?
        WHERE account_key = ? AND thread_id = ? AND source_revision != ?
          AND state IN ('pending', 'processing', 'deferred', 'failed')
        """,
        (updated_at, account_key, thread_id, source_revision),
    )
    return max(0, int(cursor.rowcount))


def _enqueue_revision(
    conn: sqlite3.Connection,
    *,
    account_key: str,
    thread_id: str,
    source_revision: str,
    mirror_sequence: int,
    updated_at: str,
) -> int:
    existing = conn.execute(
        """
        SELECT state FROM gmail_mirror_triage_queue
        WHERE account_key = ? AND thread_id = ? AND source_revision = ?
        """,
        (account_key, thread_id, source_revision),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO gmail_mirror_triage_queue(
              account_key, thread_id, source_revision, mirror_sequence,
              state, generation, attempt_count, enqueued_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', 1, 0, ?, ?)
            """,
            (
                account_key,
                thread_id,
                source_revision,
                mirror_sequence,
                updated_at,
                updated_at,
            ),
        )
        return 1
    # This function is only called after the current pointer changes. A provider
    # revision can legitimately become current again after a transient 404 or
    # restore, so every historical queue state must be reactivated with the new
    # mirror ordering rather than silently retaining its old completed/failed
    # position.
    conn.execute(
        """
        UPDATE gmail_mirror_triage_queue
        SET mirror_sequence = ?, state = 'pending', generation = generation + 1,
            attempt_count = 0, enqueued_at = ?, updated_at = ?,
            available_at = NULL, lease_expires_at = NULL,
            detector_version = NULL, policy_version = NULL, last_error = NULL
        WHERE account_key = ? AND thread_id = ? AND source_revision = ?
        """,
        (
            mirror_sequence,
            updated_at,
            updated_at,
            account_key,
            thread_id,
            source_revision,
        ),
    )
    return 1


def _save_checkpoint(
    conn: sqlite3.Connection,
    update: GmailMirrorCheckpointUpdate,
    *,
    generation: int,
    last_sequence: int,
    previous_last_success_at: str | None,
) -> GmailMirrorCheckpoint:
    last_success_at = update.last_success_at or previous_last_success_at
    pending_json = _canonical_json(list(update.pending_thread_ids))
    conn.execute(
        """
        INSERT INTO gmail_mirror_sync_state(
          account_key, stream_key, history_id, mode, coverage_complete,
          reset_required, continuation_page_token, baseline_history_id,
          pending_thread_ids, continuation_history_id, generation,
          last_sequence, last_success_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(account_key, stream_key) DO UPDATE SET
          history_id = excluded.history_id,
          mode = excluded.mode,
          coverage_complete = excluded.coverage_complete,
          reset_required = excluded.reset_required,
          continuation_page_token = excluded.continuation_page_token,
          baseline_history_id = excluded.baseline_history_id,
          pending_thread_ids = excluded.pending_thread_ids,
          continuation_history_id = excluded.continuation_history_id,
          generation = excluded.generation,
          last_sequence = excluded.last_sequence,
          last_success_at = excluded.last_success_at,
          updated_at = excluded.updated_at
        """,
        (
            update.account_key,
            update.stream_key,
            update.history_id,
            update.mode,
            int(update.coverage_complete),
            int(update.reset_required),
            update.continuation_page_token,
            update.baseline_history_id,
            pending_json,
            update.continuation_history_id,
            generation,
            last_sequence,
            last_success_at,
            update.updated_at,
        ),
    )
    row = conn.execute(
        """
        SELECT * FROM gmail_mirror_sync_state
        WHERE account_key = ? AND stream_key = ?
        """,
        (update.account_key, update.stream_key),
    ).fetchone()
    assert row is not None
    return _checkpoint_from_row(row)


def _checkpoint_marker(update: GmailMirrorCheckpointUpdate) -> str:
    value = (
        update.history_id
        or update.continuation_history_id
        or update.baseline_history_id
        or update.continuation_page_token
        or update.updated_at
    )
    return f"{update.mode}:{value}"


def _checkpoint_from_row(row: sqlite3.Row) -> GmailMirrorCheckpoint:
    try:
        pending_raw = json.loads(str(row["pending_thread_ids"]))
    except json.JSONDecodeError as exc:
        raise GmailMirrorError("Gmail mirror checkpoint backlog is corrupt") from exc
    if not isinstance(pending_raw, list):
        raise GmailMirrorError("Gmail mirror checkpoint backlog is not an array")
    pending = _pending_thread_ids(tuple(pending_raw))
    return GmailMirrorCheckpoint(
        account_key=str(row["account_key"]),
        stream_key=str(row["stream_key"]),
        history_id=str(row["history_id"]) if row["history_id"] is not None else None,
        mode=str(row["mode"]),
        coverage_complete=bool(row["coverage_complete"]),
        reset_required=bool(row["reset_required"]),
        continuation_page_token=(
            str(row["continuation_page_token"])
            if row["continuation_page_token"] is not None
            else None
        ),
        baseline_history_id=(
            str(row["baseline_history_id"])
            if row["baseline_history_id"] is not None
            else None
        ),
        pending_thread_ids=pending,
        continuation_history_id=(
            str(row["continuation_history_id"])
            if row["continuation_history_id"] is not None
            else None
        ),
        generation=int(row["generation"]),
        last_sequence=int(row["last_sequence"]),
        last_success_at=(
            str(row["last_success_at"]) if row["last_success_at"] is not None else None
        ),
        updated_at=str(row["updated_at"]),
    )


def _revision_hash(
    *,
    tombstoned: bool,
    normalized_json: str | None,
    raw_json: str | None,
) -> str:
    payload = _canonical_json(
        {
            "tombstoned": tombstoned,
            "normalized": normalized_json,
            "raw": raw_json,
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _revision_from_row(row: sqlite3.Row) -> GmailMirrorRevision:
    tombstoned = bool(row["tombstoned"])
    normalized_payload = row["normalized_payload"]
    raw_payload = row["raw_payload"]
    if tombstoned:
        if normalized_payload is not None or raw_payload is not None:
            raise GmailMirrorError("Gmail tombstone unexpectedly contains message content")
        thread = None
        raw = None
    else:
        if normalized_payload is None or raw_payload is None:
            raise GmailMirrorError("live Gmail revision is missing mirrored content")
        thread = normalized_gmail_thread_from_dict(
            _json_object(str(normalized_payload), "normalized Gmail revision")
        )
        raw = _json_object(str(raw_payload), "raw Gmail revision")
    return GmailMirrorRevision(
        account_key=str(row["account_key"]),
        thread_id=str(row["thread_id"]),
        source_revision=str(row["source_revision"]),
        mirror_sequence=int(row["mirror_sequence"]),
        tombstoned=tombstoned,
        thread=thread,
        raw_payload=raw,
        stored_at=str(row["stored_at"]),
    )


def _triage_row(
    conn: sqlite3.Connection,
    account_key: str,
    thread_id: str,
    source_revision: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT q.*, r.tombstoned, r.normalized_payload
        FROM gmail_mirror_triage_queue q
        JOIN gmail_mirror_thread_revisions r
          ON r.account_key = q.account_key
         AND r.thread_id = q.thread_id
         AND r.source_revision = q.source_revision
        WHERE q.account_key = ? AND q.thread_id = ? AND q.source_revision = ?
        """,
        (account_key, thread_id, source_revision),
    ).fetchone()


def _triage_from_row(row: sqlite3.Row) -> GmailMirrorTriageItem:
    tombstoned = bool(row["tombstoned"])
    normalized_payload = row["normalized_payload"]
    if tombstoned:
        thread = None
    else:
        if normalized_payload is None:
            raise GmailMirrorError("live Gmail triage item has no normalized thread")
        thread = normalized_gmail_thread_from_dict(
            _json_object(str(normalized_payload), "normalized Gmail triage item")
        )
    return GmailMirrorTriageItem(
        account_key=str(row["account_key"]),
        thread_id=str(row["thread_id"]),
        source_revision=str(row["source_revision"]),
        mirror_sequence=int(row["mirror_sequence"]),
        tombstoned=tombstoned,
        thread=thread,
        state=str(row["state"]),
        generation=int(row["generation"]),
        attempt_count=int(row["attempt_count"]),
        enqueued_at=str(row["enqueued_at"]),
        updated_at=str(row["updated_at"]),
        available_at=(
            str(row["available_at"]) if row["available_at"] is not None else None
        ),
        lease_expires_at=(
            str(row["lease_expires_at"])
            if row["lease_expires_at"] is not None
            else None
        ),
        detector_version=(
            str(row["detector_version"])
            if row["detector_version"] is not None
            else None
        ),
        policy_version=(
            str(row["policy_version"]) if row["policy_version"] is not None else None
        ),
        last_error=(str(row["last_error"]) if row["last_error"] is not None else None),
    )


def normalized_gmail_thread_from_dict(
    value: Mapping[str, Any],
) -> NormalizedGmailThread:
    """Rehydrate the normalizer's stable snapshot contract from mirrored JSON."""

    expected_thread_keys = {
        "thread_id",
        "history_id",
        "source_revision",
        "subject",
        "created_at",
        "updated_at",
        "message_class",
        "messages",
        "body_chars",
        "attachment_count",
        "quoted_chars_removed",
        "truncated",
    }
    _exact_keys(value, expected_thread_keys, "normalized Gmail thread")
    raw_messages = value.get("messages")
    if not isinstance(raw_messages, list):
        raise GmailMirrorError("normalized Gmail thread messages must be an array")
    messages = tuple(_normalized_message_from_dict(item) for item in raw_messages)
    thread_id = _required_json_string(value, "thread_id", 2_000)
    if any(message.thread_id != thread_id for message in messages):
        raise GmailMirrorError("normalized Gmail message belongs to another thread")
    body_chars = _json_integer(value, "body_chars", minimum=0)
    attachment_count = _json_integer(value, "attachment_count", minimum=0)
    quoted_chars_removed = _json_integer(value, "quoted_chars_removed", minimum=0)
    if body_chars != sum(len(message.body) for message in messages):
        raise GmailMirrorError("normalized Gmail thread body_chars is inconsistent")
    if attachment_count != sum(message.attachment_count for message in messages):
        raise GmailMirrorError("normalized Gmail attachment_count is inconsistent")
    if quoted_chars_removed != sum(
        message.quoted_chars_removed for message in messages
    ):
        raise GmailMirrorError("normalized Gmail quoted-history count is inconsistent")
    return NormalizedGmailThread(
        thread_id=thread_id,
        history_id=_optional_json_string(value, "history_id", 256),
        source_revision=_optional_json_string(value, "source_revision", 2_000),
        subject=_optional_json_string(value, "subject", 10_000),
        created_at=_optional_json_string(value, "created_at", 128),
        updated_at=_optional_json_string(value, "updated_at", 128),
        message_class=_required_json_string(value, "message_class", 128),
        messages=messages,
        body_chars=body_chars,
        attachment_count=attachment_count,
        quoted_chars_removed=quoted_chars_removed,
        truncated=_json_boolean(value, "truncated"),
    )


def _normalized_message_from_dict(value: Any) -> NormalizedGmailMessage:
    if not isinstance(value, Mapping):
        raise GmailMirrorError("normalized Gmail message must be an object")
    expected_message_keys = {
        "message_id",
        "thread_id",
        "internal_date",
        "timestamp",
        "from_addresses",
        "to_addresses",
        "cc_addresses",
        "subject",
        "date_header",
        "internet_message_id",
        "in_reply_to",
        "references",
        "label_ids",
        "outgoing",
        "operator_authored",
        "body",
        "body_kind",
        "attachment_count",
        "quoted_chars_removed",
        "truncated",
    }
    _exact_keys(value, expected_message_keys, "normalized Gmail message")
    return NormalizedGmailMessage(
        message_id=_required_json_string(value, "message_id", 2_000),
        thread_id=_required_json_string(value, "thread_id", 2_000),
        internal_date=_optional_json_string(value, "internal_date", 128),
        timestamp=_optional_json_string(value, "timestamp", 128),
        from_addresses=_json_string_tuple(value, "from_addresses", 1_000),
        to_addresses=_json_string_tuple(value, "to_addresses", 1_000),
        cc_addresses=_json_string_tuple(value, "cc_addresses", 1_000),
        subject=_optional_json_string(value, "subject", 10_000),
        date_header=_optional_json_string(value, "date_header", 10_000),
        internet_message_id=_optional_json_string(
            value,
            "internet_message_id",
            10_000,
        ),
        in_reply_to=_optional_json_string(value, "in_reply_to", 10_000),
        references=_json_string_tuple(value, "references", 10_000),
        label_ids=_json_string_tuple(value, "label_ids", 1_000),
        outgoing=_json_boolean(value, "outgoing"),
        operator_authored=_json_boolean(value, "operator_authored"),
        body=_required_json_string(value, "body", GMAIL_MIRROR_MAX_NORMALIZED_PAYLOAD_BYTES, allow_empty=True),
        body_kind=_optional_json_string(value, "body_kind", 128),
        attachment_count=_json_integer(value, "attachment_count", minimum=0),
        quoted_chars_removed=_json_integer(
            value,
            "quoted_chars_removed",
            minimum=0,
        ),
        truncated=_json_boolean(value, "truncated"),
    )


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected.difference(actual))
        unknown = sorted(actual.difference(expected))
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise GmailMirrorError(f"{label} schema mismatch: {'; '.join(details)}")


def _required_json_string(
    value: Mapping[str, Any],
    key: str,
    maximum: int,
    *,
    allow_empty: bool = False,
) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str):
        raise GmailMirrorError(f"normalized Gmail {key} must be a string")
    if (not candidate and not allow_empty) or len(candidate) > maximum:
        raise GmailMirrorError(f"normalized Gmail {key} is outside its bound")
    return candidate


def _optional_json_string(
    value: Mapping[str, Any],
    key: str,
    maximum: int,
) -> str | None:
    candidate = value.get(key)
    if candidate is None:
        return None
    if not isinstance(candidate, str) or not candidate or len(candidate) > maximum:
        raise GmailMirrorError(f"normalized Gmail {key} is invalid")
    return candidate


def _json_string_tuple(
    value: Mapping[str, Any],
    key: str,
    maximum: int,
) -> tuple[str, ...]:
    candidate = value.get(key)
    if not isinstance(candidate, list) or any(
        not isinstance(item, str) or not item or len(item) > maximum
        for item in candidate
    ):
        raise GmailMirrorError(f"normalized Gmail {key} must be a string array")
    return tuple(candidate)


def _json_boolean(value: Mapping[str, Any], key: str) -> bool:
    candidate = value.get(key)
    if not isinstance(candidate, bool):
        raise GmailMirrorError(f"normalized Gmail {key} must be a boolean")
    return candidate


def _json_integer(value: Mapping[str, Any], key: str, *, minimum: int) -> int:
    candidate = value.get(key)
    if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < minimum:
        raise GmailMirrorError(f"normalized Gmail {key} must be an integer")
    return candidate


def _json_object(value: str, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise GmailMirrorError(f"{label} contains invalid JSON") from exc
    if not isinstance(payload, dict):
        raise GmailMirrorError(f"{label} must be an object")
    return payload


def _prepare_mirror_file(db_path: Path) -> None:
    parent = db_path.parent
    if parent.is_symlink():
        raise GmailMirrorSecurityError(
            f"Gmail mirror directory must not be a symlink: {parent}"
        )
    if not parent.exists():
        parent.mkdir(parents=True, mode=GMAIL_MIRROR_DIRECTORY_MODE)
    _assert_private_directory(parent)
    if db_path.is_symlink():
        raise GmailMirrorSecurityError(
            f"Gmail mirror database must not be a symlink: {db_path}"
        )
    if db_path.exists() and not db_path.is_file():
        raise GmailMirrorSecurityError(
            f"Gmail mirror database is not a regular file: {db_path}"
        )
    if not db_path.exists():
        descriptor = os.open(
            db_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            GMAIL_MIRROR_FILE_MODE,
        )
        os.close(descriptor)
    _assert_private_file(db_path)


def _assert_private_mirror(db_path: Path) -> None:
    _assert_private_directory(db_path.parent)
    _assert_private_file(db_path)
    for path in _mirror_files(db_path)[1:]:
        if path.exists() or path.is_symlink():
            _assert_private_file(path)


def _assert_private_directory(path: Path) -> os.stat_result:
    try:
        value = os.stat(path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise GmailMirrorSecurityError(
            f"Gmail mirror directory is missing: {path}"
        ) from exc
    if not stat.S_ISDIR(value.st_mode):
        raise GmailMirrorSecurityError(f"Gmail mirror path is not a directory: {path}")
    if value.st_uid != os.getuid() or stat.S_IMODE(value.st_mode) & 0o077:
        raise GmailMirrorSecurityError(
            f"Gmail mirror directory must be owner-only: {path}"
        )
    return value


def _assert_private_file(path: Path) -> os.stat_result:
    try:
        value = os.stat(path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise GmailMirrorSecurityError(f"Gmail mirror file is missing: {path}") from exc
    if not stat.S_ISREG(value.st_mode):
        raise GmailMirrorSecurityError(
            f"Gmail mirror path is not a regular file: {path}"
        )
    if value.st_uid != os.getuid() or stat.S_IMODE(value.st_mode) & 0o077:
        raise GmailMirrorSecurityError(
            f"Gmail mirror file must be owner-only: {path}"
        )
    return value


def _secure_mirror_files(db_path: Path) -> None:
    for path in _mirror_files(db_path):
        if path.is_symlink():
            raise GmailMirrorSecurityError(
                f"Gmail mirror SQLite file must not be a symlink: {path}"
            )
        try:
            os.chmod(path, GMAIL_MIRROR_FILE_MODE)
        except FileNotFoundError:
            continue


def _mirror_files(db_path: Path) -> tuple[Path, Path, Path]:
    return (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm"))


def _verify_schema(conn: sqlite3.Connection) -> None:
    try:
        rows = conn.execute(
            "SELECT version, name FROM gmail_mirror_schema_migrations ORDER BY version"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise GmailMirrorError("Gmail mirror is not initialized") from exc
    versions = tuple((int(row[0]), str(row[1])) for row in rows)
    expected = ((GMAIL_MIRROR_SCHEMA_VERSION, GMAIL_MIRROR_SCHEMA_NAME),)
    if versions != expected:
        raise GmailMirrorError(
            f"Gmail mirror schema is incompatible: found {versions}, expected {expected}"
        )
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if not _REQUIRED_TABLES.issubset(tables):
        raise GmailMirrorError("Gmail mirror schema tables are incomplete")


def _ensure_quarantine_retry_columns(conn: sqlite3.Connection) -> None:
    """Upgrade the pre-release schema-one quarantine table in place."""

    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(gmail_mirror_quarantine)").fetchall()
    }
    additions = {
        "retry_count": "INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0)",
        "next_retry_at": "TEXT",
        "last_retry_at": "TEXT",
        "last_parser_version": (
            "TEXT CHECK(last_parser_version IS NULL "
            "OR length(last_parser_version) <= 256)"
        ),
    }
    for name, definition in additions.items():
        if name not in columns:
            conn.execute(
                f"ALTER TABLE gmail_mirror_quarantine ADD COLUMN {name} {definition}"
            )


def _identifier(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or "\x00" in normalized:
        raise ValueError(f"{label} is invalid")
    return normalized


def _optional_text(value: Any, label: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _identifier(value, label, maximum)


def _canonical_timestamp(value: str, label: str) -> str:
    parsed = _parse_timestamp(value, label)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _optional_timestamp(value: str | None, label: str) -> str | None:
    return _canonical_timestamp(value, label) if value is not None else None


def _parse_timestamp(value: str, label: str = "timestamp") -> datetime:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError(f"{label} must be a bounded timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed


def _positive_limit(value: int) -> int:
    if isinstance(value, bool) or not 1 <= value <= 1_000:
        raise ValueError("limit must be between 1 and 1000")
    return value


def _bounded_json(value: Any, label: str, maximum_bytes: int) -> str:
    try:
        encoded = _canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON-serializable") from exc
    if len(encoded.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{label} exceeds its byte bound")
    return encoded


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_REQUIRED_TABLES = {
    "gmail_mirror_schema_migrations",
    "gmail_mirror_sync_state",
    "gmail_mirror_thread_revisions",
    "gmail_mirror_threads",
    "gmail_mirror_triage_queue",
    "gmail_mirror_quarantine",
}


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS gmail_mirror_schema_migrations (
  version INTEGER PRIMARY KEY CHECK(version > 0),
  name TEXT NOT NULL UNIQUE CHECK(length(name) BETWEEN 1 AND 128),
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gmail_mirror_sync_state (
  account_key TEXT NOT NULL CHECK(length(account_key) BETWEEN 1 AND 512),
  stream_key TEXT NOT NULL CHECK(length(stream_key) BETWEEN 1 AND 512),
  history_id TEXT CHECK(history_id IS NULL OR length(history_id) <= 256),
  mode TEXT NOT NULL CHECK(mode IN ('full', 'incremental')),
  coverage_complete INTEGER NOT NULL CHECK(coverage_complete IN (0, 1)),
  reset_required INTEGER NOT NULL CHECK(reset_required IN (0, 1)),
  continuation_page_token TEXT
    CHECK(
      continuation_page_token IS NULL
      OR length(continuation_page_token) <= 8192
    ),
  baseline_history_id TEXT
    CHECK(baseline_history_id IS NULL OR length(baseline_history_id) <= 256),
  pending_thread_ids TEXT NOT NULL DEFAULT '[]'
    CHECK(
      json_valid(pending_thread_ids)
      AND json_type(pending_thread_ids) = 'array'
      AND length(CAST(pending_thread_ids AS BLOB)) <= 512000
    ),
  continuation_history_id TEXT
    CHECK(
      continuation_history_id IS NULL
      OR length(continuation_history_id) <= 256
    ),
  generation INTEGER NOT NULL CHECK(generation > 0),
  last_sequence INTEGER NOT NULL CHECK(last_sequence >= 0),
  last_success_at TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(account_key, stream_key)
);

CREATE TABLE IF NOT EXISTS gmail_mirror_thread_revisions (
  account_key TEXT NOT NULL CHECK(length(account_key) BETWEEN 1 AND 512),
  thread_id TEXT NOT NULL CHECK(length(thread_id) BETWEEN 1 AND 2000),
  source_revision TEXT NOT NULL CHECK(length(source_revision) BETWEEN 1 AND 2000),
  first_seen_sequence INTEGER NOT NULL CHECK(first_seen_sequence > 0),
  tombstoned INTEGER NOT NULL CHECK(tombstoned IN (0, 1)),
  normalized_payload TEXT,
  raw_payload TEXT,
  content_sha256 TEXT NOT NULL
    CHECK(length(content_sha256) = 64 AND content_sha256 GLOB '[0-9a-f]*'),
  provider_updated_at TEXT,
  stored_at TEXT NOT NULL,
  PRIMARY KEY(account_key, thread_id, source_revision),
  UNIQUE(account_key, first_seen_sequence),
  CHECK(
    (tombstoned = 1 AND normalized_payload IS NULL AND raw_payload IS NULL)
    OR
    (
      tombstoned = 0
      AND normalized_payload IS NOT NULL
      AND raw_payload IS NOT NULL
      AND json_valid(normalized_payload)
      AND json_type(normalized_payload) = 'object'
      AND json_valid(raw_payload)
      AND json_type(raw_payload) = 'object'
    )
  )
);

CREATE TABLE IF NOT EXISTS gmail_mirror_threads (
  account_key TEXT NOT NULL CHECK(length(account_key) BETWEEN 1 AND 512),
  thread_id TEXT NOT NULL CHECK(length(thread_id) BETWEEN 1 AND 2000),
  current_revision TEXT NOT NULL CHECK(length(current_revision) BETWEEN 1 AND 2000),
  current_sequence INTEGER NOT NULL CHECK(current_sequence > 0),
  tombstoned INTEGER NOT NULL CHECK(tombstoned IN (0, 1)),
  updated_at TEXT NOT NULL,
  PRIMARY KEY(account_key, thread_id),
  UNIQUE(account_key, current_sequence),
  FOREIGN KEY(account_key, thread_id, current_revision)
    REFERENCES gmail_mirror_thread_revisions(
      account_key, thread_id, source_revision
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS gmail_mirror_triage_queue (
  account_key TEXT NOT NULL CHECK(length(account_key) BETWEEN 1 AND 512),
  thread_id TEXT NOT NULL CHECK(length(thread_id) BETWEEN 1 AND 2000),
  source_revision TEXT NOT NULL CHECK(length(source_revision) BETWEEN 1 AND 2000),
  mirror_sequence INTEGER NOT NULL CHECK(mirror_sequence > 0),
  state TEXT NOT NULL
    CHECK(
      state IN (
        'pending', 'processing', 'completed', 'deferred', 'failed', 'superseded'
      )
    ),
  generation INTEGER NOT NULL CHECK(generation > 0),
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
  enqueued_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  available_at TEXT,
  lease_expires_at TEXT,
  detector_version TEXT
    CHECK(detector_version IS NULL OR length(detector_version) <= 256),
  policy_version TEXT
    CHECK(policy_version IS NULL OR length(policy_version) <= 256),
  last_error TEXT CHECK(last_error IS NULL OR length(last_error) <= 4000),
  PRIMARY KEY(account_key, thread_id, source_revision),
  UNIQUE(account_key, mirror_sequence),
  FOREIGN KEY(account_key, thread_id, source_revision)
    REFERENCES gmail_mirror_thread_revisions(
      account_key, thread_id, source_revision
    ) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS gmail_mirror_quarantine (
  account_key TEXT NOT NULL CHECK(length(account_key) BETWEEN 1 AND 512),
  thread_id TEXT NOT NULL CHECK(length(thread_id) BETWEEN 1 AND 2000),
  source_revision TEXT
    CHECK(source_revision IS NULL OR length(source_revision) BETWEEN 1 AND 2000),
  failure_fingerprint TEXT NOT NULL
    CHECK(
      length(failure_fingerprint) = 64
      AND failure_fingerprint GLOB '[0-9a-f]*'
    ),
  stage TEXT NOT NULL CHECK(length(stage) BETWEEN 1 AND 128),
  error TEXT NOT NULL CHECK(length(error) BETWEEN 1 AND 1000),
  payload_sha256 TEXT NOT NULL
    CHECK(length(payload_sha256) = 64 AND payload_sha256 GLOB '[0-9a-f]*'),
  occurrence_count INTEGER NOT NULL CHECK(occurrence_count > 0),
  retry_count INTEGER NOT NULL DEFAULT 0 CHECK(retry_count >= 0),
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  next_retry_at TEXT,
  last_retry_at TEXT,
  last_parser_version TEXT
    CHECK(last_parser_version IS NULL OR length(last_parser_version) <= 256),
  resolved_at TEXT,
  PRIMARY KEY(account_key, thread_id, failure_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_gmail_mirror_queue_ready
ON gmail_mirror_triage_queue(account_key, state, available_at, mirror_sequence);

CREATE INDEX IF NOT EXISTS idx_gmail_mirror_revisions_thread
ON gmail_mirror_thread_revisions(account_key, thread_id, first_seen_sequence DESC);

CREATE INDEX IF NOT EXISTS idx_gmail_mirror_quarantine_open
ON gmail_mirror_quarantine(account_key, resolved_at, last_seen_at);
"""
