from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .operational_db import operational_connection
from .util import new_id, now_iso


ITEM_STATES = {"active", "resolved", "dismissed", "cancelled", "expired"}
ITEM_KINDS = {
    "event",
    "commitment",
    "waiting",
    "follow_up",
    "deadline",
    "attention",
}
ITEM_OWNERS = {"operator", "other", "shared", "unknown"}
FEEDBACK_DECISIONS = {
    "confirm",
    "dismiss",
    "incorrect",
    "resolve",
    "done",
    "restore",
    "snooze",
    "unsnooze",
}
TIME_FIELDS = ("starts_at", "due_at", "ends_at", "expires_at")
RESCHEDULE_FIELDS = ("starts_at", "due_at", "ends_at")
PROJECTED_FIELDS = (
    "item_kind",
    "title",
    "details",
    "owner",
    "counterparty_entity_id",
    "project_ref",
    *TIME_FIELDS,
    "source_timezone",
    "confidence",
    "priority",
    "metadata",
)
RECONCILIATION_VERSION = "source-key-v1"
FEEDBACK_VERSION = "human-feedback-v1"
MAX_EVIDENCE_REFS = 64
MAX_OBSERVATION_PAYLOAD_BYTES = 32768
MAX_EVIDENCE_REFS_BYTES = 16384
MAX_METADATA_BYTES = 16384
MAX_CURSOR_METADATA_BYTES = 512 * 1024
MAX_PENDING_THREAD_IDS_BYTES = 500 * 1024
FORBIDDEN_REFERENCE_KEYS = {"body", "content", "html", "payload", "raw", "text"}
OBSERVATION_METADATA_SCHEMA_VERSION = 1
OBSERVATION_METADATA_KEYS_V1 = {
    "all_day",
    "attendee_count",
    "attendee_response",
    "calendar_id",
    "detector_version",
    "event_type",
    "ical_uid",
    "location",
    "message_class",
    "organizer_self",
    "original_start_time",
    "provider_sequence",
    "reconciliation_status",
    "revalidation_reason",
    "recurring_event_id",
    "source_status",
    "transparency",
    "visibility",
}
EVIDENCE_REF_KEYS = {
    "account_key",
    "calendar_id",
    "chunk_id",
    "content_hash",
    "document_id",
    "end_offset",
    "event_id",
    "message_id",
    "observation_id",
    "quote_hash",
    "source_ref",
    "source_revision",
    "start_offset",
    "stream_key",
    "thread_id",
}
EVIDENCE_IDENTITY_KEYS = {
    "chunk_id",
    "document_id",
    "event_id",
    "message_id",
    "observation_id",
    "source_ref",
    "thread_id",
}
EVIDENCE_INTEGER_KEYS = {"end_offset", "start_offset"}
CURSOR_METADATA_KEYS = {
    "baseline_history_id",
    "continuation_mode",
    "continuation_history_id",
    "coverage_status",
    "continuation_page_token",
    "deferred_count",
    "full_sync",
    "item_count",
    "page_count",
    "pages",
    "pending_thread_ids",
    "retry_count",
    "reset_rebuild",
    "reset_seen_item_ids",
    "reset_seen_overflow",
    "reset_started_generation",
    "window_end",
    "window_start",
}


class ObservationConflictError(ValueError):
    """The same immutable source revision was presented with different content."""


class CursorConflictError(RuntimeError):
    """A connector tried to advance a cursor from an unexpected prior value."""


class FeedbackConflictError(ValueError):
    """A feedback idempotency key was reused for a different decision."""


@dataclass(frozen=True)
class OperationalObservation:
    source_type: str
    account_key: str
    stream_key: str
    source_key: str
    source_revision: str
    source_order: int
    source_updated_at: str | None
    item_kind: str
    title: str
    observed_at: str = field(default_factory=now_iso)
    details: str | None = None
    owner: str = "unknown"
    counterparty_entity_id: str | None = None
    project_ref: str | None = None
    starts_at: str | None = None
    due_at: str | None = None
    ends_at: str | None = None
    source_timezone: str | None = None
    expires_at: str | None = None
    confidence: float = 1.0
    priority: int = 0
    cancelled: bool = False
    evidence_refs: Sequence[Any] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def effective_source_updated_at(self) -> str | None:
        return _optional_canonical_timestamp(
            self.source_updated_at,
            "source_updated_at",
        )

    def validated(self) -> OperationalObservation:
        _validate_identifier(self.source_type, "source_type", 128)
        _validate_identifier(self.account_key, "account_key", 512)
        _validate_identifier(self.stream_key, "stream_key", 512)
        _validate_identifier(self.source_key, "source_key", 1024)
        _validate_identifier(self.source_revision, "source_revision", 1024)
        _validate_text(self.title, "title", 500, required=True)
        _validate_text(self.details, "details", 4000)
        _validate_text(
            self.counterparty_entity_id,
            "counterparty_entity_id",
            512,
        )
        _validate_text(self.project_ref, "project_ref", 512)
        _validate_text(self.source_timezone, "source_timezone", 128)
        if self.source_timezone is not None:
            try:
                ZoneInfo(self.source_timezone)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(
                    "source_timezone must be a valid IANA timezone"
                ) from exc
        for name in TIME_FIELDS:
            value = getattr(self, name)
            _validate_text(value, name, 128)
            if value is not None:
                _parse_source_timestamp(value, name)
        if self.item_kind not in ITEM_KINDS:
            raise ValueError(
                f"observation item_kind must be one of: {', '.join(sorted(ITEM_KINDS))}"
            )
        if self.owner not in ITEM_OWNERS:
            raise ValueError(
                f"observation owner must be one of: {', '.join(sorted(ITEM_OWNERS))}"
            )
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not math.isfinite(float(self.confidence))
            or not 0.0 <= float(self.confidence) <= 1.0
        ):
            raise ValueError("observation confidence must be between 0 and 1")
        if (
            isinstance(self.priority, bool)
            or not isinstance(self.priority, int)
            or not -100 <= self.priority <= 100
        ):
            raise ValueError("observation priority must be between -100 and 100")
        if (
            isinstance(self.source_order, bool)
            or not isinstance(self.source_order, int)
            or self.source_order < 0
        ):
            raise ValueError("source_order must be a non-negative integer")
        if not isinstance(self.cancelled, bool):
            raise ValueError("cancelled must be a boolean")
        _parse_source_timestamp(self.observed_at, "observed_at")
        if self.effective_source_updated_at is not None:
            _parse_source_timestamp(
                self.effective_source_updated_at,
                "source_updated_at",
            )
        if self.starts_at is not None and self.ends_at is not None:
            if _parse_source_timestamp(
                self.ends_at, "ends_at"
            ) <= _parse_source_timestamp(self.starts_at, "starts_at"):
                raise ValueError("ends_at must be later than starts_at")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("metadata must be a mapping")
        if isinstance(self.evidence_refs, (str, bytes)):
            raise ValueError("evidence_refs must be a sequence of bounded references")
        if len(self.evidence_refs) > MAX_EVIDENCE_REFS:
            raise ValueError(f"evidence_refs cannot exceed {MAX_EVIDENCE_REFS} entries")
        evidence = list(self.evidence_refs)
        _validate_evidence_refs(evidence)
        _validate_observation_metadata(dict(self.metadata))
        _bounded_json(
            self.payload(), "observation payload", MAX_OBSERVATION_PAYLOAD_BYTES
        )
        _bounded_json(evidence, "evidence_refs", MAX_EVIDENCE_REFS_BYTES)
        _bounded_json(self.normalized_metadata(), "metadata", MAX_METADATA_BYTES)
        return self

    def normalized_metadata(self) -> dict[str, Any]:
        return {
            "schema_version": OBSERVATION_METADATA_SCHEMA_VERSION,
            **dict(self.metadata),
        }

    def payload(self) -> dict[str, Any]:
        return {
            "item_kind": self.item_kind,
            "title": self.title,
            "details": self.details,
            "owner": self.owner,
            "counterparty_entity_id": self.counterparty_entity_id,
            "project_ref": self.project_ref,
            "starts_at": _optional_canonical_timestamp(self.starts_at, "starts_at"),
            "due_at": _optional_canonical_timestamp(self.due_at, "due_at"),
            "ends_at": _optional_canonical_timestamp(self.ends_at, "ends_at"),
            "source_timezone": self.source_timezone,
            "expires_at": _optional_canonical_timestamp(self.expires_at, "expires_at"),
            "confidence": float(self.confidence),
            "priority": int(self.priority),
            "cancelled": bool(self.cancelled),
            "metadata": self.normalized_metadata(),
        }


@dataclass(frozen=True)
class ReconciliationResult:
    observation_id: str
    item_id: str
    event_id: str | None
    event_type: str
    state: str
    observation_created: bool
    item_created: bool
    item_changed: bool

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class SourceCursorUpdate:
    connector_id: str
    source_type: str
    account_key: str
    stream_key: str
    cursor: str | None
    watermark: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    last_success_at: str | None = None
    expected_cursor: str | None = None
    expected_generation: int | None = None
    enforce_expected_cursor: bool = True
    updated_at: str | None = None

    def validated(self) -> SourceCursorUpdate:
        _validate_identifier(self.connector_id, "connector_id", 128)
        _validate_identifier(self.source_type, "source_type", 128)
        _validate_identifier(self.account_key, "account_key", 512)
        _validate_identifier(self.stream_key, "stream_key", 512)
        _validate_text(self.cursor, "cursor", 8192)
        _validate_text(self.watermark, "watermark", 1024)
        _validate_text(self.expected_cursor, "expected_cursor", 8192)
        if self.expected_generation is not None and (
            isinstance(self.expected_generation, bool)
            or not isinstance(self.expected_generation, int)
            or self.expected_generation < 0
        ):
            raise ValueError("expected_generation must be a non-negative integer")
        cursor_metadata = dict(self.metadata)
        _validate_cursor_metadata(cursor_metadata)
        _bounded_json(cursor_metadata, "cursor metadata", MAX_CURSOR_METADATA_BYTES)
        if self.last_success_at is not None:
            _parse_source_timestamp(self.last_success_at, "last_success_at")
        if self.updated_at is not None:
            _parse_source_timestamp(self.updated_at, "updated_at")
        return self


@dataclass(frozen=True)
class SourceUnitResult:
    run_id: str
    reconciliations: tuple[ReconciliationResult, ...]
    cursor: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "reconciliations": [result.as_dict() for result in self.reconciliations],
            "cursor": self.cursor,
        }


def reconcile_observation(
    db_path: Path,
    observation: OperationalObservation,
    *,
    processed_at: str | None = None,
    run_id: str | None = None,
) -> ReconciliationResult:
    result = reconcile_source_unit(
        db_path,
        [observation],
        processed_at=processed_at,
        run_id=run_id,
    )
    return result.reconciliations[0]


def reconcile_source_unit(
    db_path: Path,
    observations: Sequence[OperationalObservation],
    *,
    cursor_update: SourceCursorUpdate | None = None,
    processed_at: str | None = None,
    run_id: str | None = None,
) -> SourceUnitResult:
    timestamp, effective_run_id = _prepare_source_unit(
        observations,
        cursor_update=cursor_update,
        processed_at=processed_at,
        run_id=run_id,
    )
    with operational_connection(db_path, write=True) as conn:
        return _reconcile_source_unit_in_connection(
            conn,
            observations,
            cursor_update=cursor_update,
            processed_at=timestamp,
            run_id=effective_run_id,
        )


def _prepare_source_unit(
    observations: Sequence[OperationalObservation],
    *,
    cursor_update: SourceCursorUpdate | None,
    processed_at: str | None,
    run_id: str | None,
) -> tuple[str, str]:
    if not observations and cursor_update is None:
        raise ValueError("a source unit requires observations or a cursor update")
    for observation in observations:
        observation.validated()
    observation_units = {
        (observation.source_type, observation.account_key, observation.stream_key)
        for observation in observations
    }
    if len(observation_units) > 1:
        raise ValueError(
            "source-unit observations must share source_type, account_key, and "
            "stream_key"
        )
    if cursor_update is not None:
        cursor_update.validated()
        cursor_unit = (
            cursor_update.source_type,
            cursor_update.account_key,
            cursor_update.stream_key,
        )
        if observation_units and observation_units != {cursor_unit}:
            raise ValueError(
                "source-unit cursor must match observation source_type, "
                "account_key, and stream_key"
            )
    timestamp = _canonical_timestamp(processed_at or now_iso(), "processed_at")
    effective_run_id = run_id or new_id("opsrun")
    _validate_identifier(effective_run_id, "run_id", 256)
    return timestamp, effective_run_id


def _reconcile_source_unit_in_connection(
    conn: sqlite3.Connection,
    observations: Sequence[OperationalObservation],
    *,
    cursor_update: SourceCursorUpdate | None,
    processed_at: str,
    run_id: str,
    before_cursor: Callable[[tuple[ReconciliationResult, ...]], None] | None = None,
) -> SourceUnitResult:
    """Persist one validated unit; optional audit work runs before cursor mutation.

    The caller must own the surrounding SQLite write transaction.  Keeping the
    cursor write last lets higher-level source-unit projections participate in
    the same all-or-nothing boundary without teaching reconciliation about them.
    """

    reconciliations: list[ReconciliationResult] = []
    cursor: dict[str, Any] | None = None
    for observation in observations:
        reconciliations.append(
            _reconcile_in_connection(
                conn,
                observation,
                processed_at=processed_at,
                run_id=run_id,
            )
        )
    frozen_reconciliations = tuple(reconciliations)
    if before_cursor is not None:
        before_cursor(frozen_reconciliations)
    if cursor_update is not None:
        cursor = _save_source_cursor_in_connection(
            conn,
            cursor_update,
            updated_at=cursor_update.updated_at or processed_at,
        )
    return SourceUnitResult(
        run_id=run_id,
        reconciliations=frozen_reconciliations,
        cursor=cursor,
    )


def record_item_feedback(
    db_path: Path,
    item_id: str,
    decision: str,
    *,
    note: str | None = None,
    snoozed_until: str | None = None,
    idempotency_key: str | None = None,
    created_at: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    normalized = decision.strip().casefold()
    if normalized == "done":
        normalized = "resolve"
    if normalized not in FEEDBACK_DECISIONS:
        raise ValueError(
            f"feedback decision must be one of: {', '.join(sorted(FEEDBACK_DECISIONS))}"
        )
    _validate_identifier(item_id, "item_id", 256)
    _validate_text(note, "feedback note", 2000)
    if snoozed_until is not None:
        _parse_source_timestamp(snoozed_until, "snoozed_until")
    timestamp = _canonical_timestamp(created_at or now_iso(), "created_at")
    created_timestamp = _parse_source_timestamp(timestamp, "created_at")
    normalized_snoozed_until = _optional_canonical_timestamp(
        snoozed_until,
        "snoozed_until",
    )
    if normalized == "snooze":
        if normalized_snoozed_until is None:
            raise ValueError("snooze feedback requires snoozed_until")
        if (
            _parse_source_timestamp(normalized_snoozed_until, "snoozed_until")
            <= created_timestamp
        ):
            raise ValueError("snoozed_until must be later than created_at")
    elif normalized_snoozed_until is not None:
        raise ValueError("snoozed_until is valid only for snooze feedback")
    effective_run_id = run_id or new_id("opsrun")
    effective_idempotency_key = idempotency_key or new_id("feedback")
    _validate_identifier(effective_run_id, "run_id", 256)
    _validate_identifier(
        effective_idempotency_key,
        "idempotency_key",
        256,
    )
    feedback_metadata = {
        "decision": normalized,
        "note": note,
        "snoozed_until": normalized_snoozed_until,
    }
    with operational_connection(db_path, write=True) as conn:
        existing_event = conn.execute(
            """
            SELECT * FROM ops_item_events
            WHERE item_id = ? AND idempotency_key = ?
            """,
            (item_id, effective_idempotency_key),
        ).fetchone()
        if existing_event is not None:
            existing = row_to_event(existing_event)
            if existing["metadata"] != feedback_metadata:
                raise FeedbackConflictError(
                    "feedback idempotency key was reused with different content"
                )
            current = conn.execute(
                "SELECT * FROM ops_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            if current is None:
                raise RuntimeError(
                    f"feedback event is missing its operational item: {item_id}"
                )
            return {
                "event_id": existing["id"],
                "decision": normalized,
                "item": row_to_item(current),
                "idempotent": True,
            }
        row = conn.execute(
            "SELECT * FROM ops_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"operational item not found: {item_id}")
        before = row_to_item(row)
        superseded_observation = _load_observation(
            conn,
            row["current_observation_id"],
        )
        state = str(row["state"])
        override_state = row["human_override_state"]
        override_at = row["human_override_at"]
        override_reason = row["human_override_reason"]
        item_snoozed_until = row["snoozed_until"]
        human_confirmed_at = row["human_confirmed_at"]
        metadata = dict(before["metadata"])
        if normalized == "confirm":
            _require_state(state, {"active"}, normalized)
            human_confirmed_at = timestamp
            metadata["reconciliation_status"] = "confirmed"
        elif normalized in {"dismiss", "incorrect"}:
            _require_state(state, {"active"}, normalized)
            state = "dismissed"
            override_state = "dismissed"
            override_at = timestamp
            override_reason = note or normalized
            human_confirmed_at = None
            metadata["human_feedback_status"] = normalized
        elif normalized == "resolve":
            _require_state(state, {"active"}, normalized)
            state = "resolved"
            override_state = "resolved"
            override_at = timestamp
            override_reason = note or "resolved"
            human_confirmed_at = timestamp
            metadata["human_feedback_status"] = "resolved"
        elif normalized == "restore":
            if override_state is None and state not in {
                "resolved",
                "dismissed",
                "expired",
            }:
                raise ValueError(f"feedback restore is not valid from state {state}")
            current_observation = _load_observation(
                conn,
                row["current_observation_id"],
            )
            override_state = None
            override_at = None
            override_reason = None
            state = (
                "cancelled" if current_observation["payload"]["cancelled"] else "active"
            )
            human_confirmed_at = timestamp
            metadata.pop("human_feedback_status", None)
        elif normalized == "snooze":
            _require_state(state, {"active"}, normalized)
            item_snoozed_until = normalized_snoozed_until
        elif normalized == "unsnooze":
            _require_state(state, {"active"}, normalized)
            if item_snoozed_until is None:
                raise ValueError("feedback unsnooze requires an existing snooze")
            item_snoozed_until = None
        state_changed_at = (
            timestamp if state != row["state"] else row["state_changed_at"]
        )
        conn.execute(
            """
            UPDATE ops_items
            SET state = ?, snoozed_until = ?, human_override_state = ?,
                human_override_at = ?, human_override_reason = ?,
                human_confirmed_at = ?, last_human_action_at = ?, metadata = ?,
                updated_at = ?, state_changed_at = ?
            WHERE id = ?
            """,
            (
                state,
                item_snoozed_until,
                override_state,
                override_at,
                override_reason,
                human_confirmed_at,
                timestamp,
                _bounded_json(metadata, "item metadata", MAX_METADATA_BYTES),
                timestamp,
                state_changed_at,
                item_id,
            ),
        )
        updated = conn.execute(
            "SELECT * FROM ops_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        assert updated is not None
        after = row_to_item(updated)
        event_id = _append_event(
            conn,
            item_id=item_id,
            observation=superseded_observation,
            event_type="resolved" if normalized == "resolve" else "feedback",
            actor="human",
            before=before,
            after=after,
            metadata=feedback_metadata,
            run_id=effective_run_id,
            reconciliation_version=FEEDBACK_VERSION,
            idempotency_key=effective_idempotency_key,
            created_at=timestamp,
        )
        return {
            "event_id": event_id,
            "decision": normalized,
            "item": after,
            "idempotent": False,
        }


def get_item(db_path: Path, item_id: str) -> dict[str, Any] | None:
    with operational_connection(db_path, write=False) as conn:
        row = conn.execute(
            "SELECT * FROM ops_items WHERE id = ?",
            (item_id,),
        ).fetchone()
        return row_to_item(row) if row is not None else None


def list_observations(
    db_path: Path,
    *,
    item_id: str | None = None,
) -> list[dict[str, Any]]:
    with operational_connection(db_path, write=False) as conn:
        if item_id is None:
            rows = conn.execute(
                "SELECT * FROM ops_observations ORDER BY created_at, id"
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT DISTINCT o.*
                FROM ops_observations o
                JOIN ops_item_events e ON e.observation_id = o.id
                WHERE e.item_id = ?
                ORDER BY o.created_at, o.id
                """,
                (item_id,),
            ).fetchall()
        return [row_to_observation(row) for row in rows]


def list_item_events(db_path: Path, item_id: str) -> list[dict[str, Any]]:
    with operational_connection(db_path, write=False) as conn:
        rows = conn.execute(
            """
            SELECT * FROM ops_item_events
            WHERE item_id = ?
            ORDER BY sequence
            """,
            (item_id,),
        ).fetchall()
        return [row_to_event(row) for row in rows]


def save_source_cursor(
    db_path: Path,
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
    update = SourceCursorUpdate(
        connector_id=connector_id,
        source_type=source_type,
        account_key=account_key,
        stream_key=stream_key,
        cursor=cursor,
        watermark=watermark,
        metadata=dict(metadata or {}),
        last_success_at=last_success_at,
        expected_cursor=expected_cursor,
        expected_generation=expected_generation,
        enforce_expected_cursor=enforce_expected_cursor,
        updated_at=updated_at,
    ).validated()
    timestamp = _canonical_timestamp(updated_at or now_iso(), "updated_at")
    with operational_connection(db_path, write=True) as conn:
        return _save_source_cursor_in_connection(
            conn,
            update,
            updated_at=timestamp,
        )


def get_source_cursor(
    db_path: Path,
    connector_id: str,
    account_key: str,
    stream_key: str,
) -> dict[str, Any] | None:
    with operational_connection(db_path, write=False) as conn:
        row = conn.execute(
            """
            SELECT * FROM ops_source_cursors
            WHERE connector_id = ? AND account_key = ? AND stream_key = ?
            """,
            (connector_id, account_key, stream_key),
        ).fetchone()
        return row_to_source_cursor(row) if row is not None else None


def canonical_source_key(observation: OperationalObservation) -> str:
    identity = _json(
        [
            observation.source_type,
            observation.account_key,
            observation.stream_key,
            observation.source_key,
        ]
    )
    return f"source_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]}"


def operational_item_id(observation: OperationalObservation) -> str:
    """Return the stable item id reconciliation assigns to an observation."""

    return f"item_{canonical_source_key(observation).removeprefix('source_')}"


def row_to_observation(row: sqlite3.Row) -> dict[str, Any]:
    output = dict(row)
    output["payload"] = json.loads(str(output["payload"]))
    output["evidence_refs"] = json.loads(str(output["evidence_refs"]))
    return output


def row_to_item(row: sqlite3.Row) -> dict[str, Any]:
    output = dict(row)
    output["metadata"] = json.loads(str(output["metadata"]))
    return output


def row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    output = dict(row)
    for key in ("before_state", "after_state", "metadata"):
        output[key] = json.loads(str(output[key]))
    return output


def row_to_source_cursor(row: sqlite3.Row) -> dict[str, Any]:
    output = dict(row)
    output["metadata"] = json.loads(str(output["metadata"]))
    return output


def _reconcile_in_connection(
    conn: sqlite3.Connection,
    observation: OperationalObservation,
    *,
    processed_at: str,
    run_id: str,
) -> ReconciliationResult:
    observation_id, observation_created = _insert_observation(
        conn,
        observation,
        created_at=processed_at,
    )
    canonical_key = canonical_source_key(observation)
    item = conn.execute(
        "SELECT * FROM ops_items WHERE canonical_key = ?",
        (canonical_key,),
    ).fetchone()
    if not observation_created and item is None:
        raise RuntimeError(
            "an existing observation is missing its reconciled operational item"
        )
    if item is None:
        return _create_item(
            conn,
            observation,
            observation_id=observation_id,
            canonical_key=canonical_key,
            timestamp=processed_at,
            run_id=run_id,
        )
    incoming = _load_observation(conn, observation_id)
    current = _load_observation(conn, item["current_observation_id"])
    if not observation_created:
        if observation_id == str(current["id"]):
            return _noop_reconciliation(item, observation_id)
        if _restores_provider_authority(incoming, current):
            return _update_item(
                conn,
                item,
                observation,
                observation_id=observation_id,
                timestamp=processed_at,
                run_id=run_id,
                observation_created=False,
                authority_reapplied=True,
            )
        return _noop_reconciliation(item, observation_id)
    incoming_order = _observation_order_key(incoming)
    current_order = _observation_order_key(current)
    if incoming_order <= current_order:
        before = row_to_item(item)
        ordering_status = (
            "older" if incoming_order < current_order else "ambiguous_equal"
        )
        event_id = _append_event(
            conn,
            item_id=str(item["id"]),
            observation=incoming,
            event_type="stale_ignored",
            actor="deterministic",
            before=before,
            after=before,
            metadata={
                "reason": "source authority does not order revision after current",
                "ordering_status": ordering_status,
                "current_observation_id": current["id"],
                "current_source_order": current["source_order"],
                "current_source_updated_at": current["source_updated_at"],
            },
            run_id=run_id,
            reconciliation_version=RECONCILIATION_VERSION,
            idempotency_key=f"observation:{observation_id}",
            created_at=processed_at,
        )
        return ReconciliationResult(
            observation_id=observation_id,
            item_id=str(item["id"]),
            event_id=event_id,
            event_type="stale_ignored",
            state=str(item["state"]),
            observation_created=True,
            item_created=False,
            item_changed=False,
        )
    return _update_item(
        conn,
        item,
        observation,
        observation_id=observation_id,
        timestamp=processed_at,
        run_id=run_id,
    )


def _insert_observation(
    conn: sqlite3.Connection,
    observation: OperationalObservation,
    *,
    created_at: str,
) -> tuple[str, bool]:
    payload_json = _bounded_json(
        observation.payload(),
        "observation payload",
        MAX_OBSERVATION_PAYLOAD_BYTES,
    )
    evidence_json = _bounded_json(
        list(observation.evidence_refs),
        "evidence_refs",
        MAX_EVIDENCE_REFS_BYTES,
    )
    content_hash = hashlib.sha256(
        _json(
            {
                "event_kind": "cancelled" if observation.cancelled else "upsert",
                "source_updated_at": observation.effective_source_updated_at,
                "payload": json.loads(payload_json),
                "evidence_refs": json.loads(evidence_json),
            }
        ).encode("utf-8")
    ).hexdigest()
    identity = _json(
        [
            observation.source_type,
            observation.account_key,
            observation.stream_key,
            observation.source_key,
            observation.source_revision,
        ]
    )
    observation_id = (
        f"observation_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]}"
    )
    existing = conn.execute(
        """
        SELECT * FROM ops_observations
        WHERE source_type = ? AND account_key = ?
          AND stream_key = ? AND source_key = ? AND source_revision = ?
        """,
        (
            observation.source_type,
            observation.account_key,
            observation.stream_key,
            observation.source_key,
            observation.source_revision,
        ),
    ).fetchone()
    if existing is not None:
        if str(existing["content_hash"]) != content_hash:
            raise ObservationConflictError(
                "immutable observation revision has conflicting content: "
                f"{observation.source_type}/{observation.source_key}/"
                f"{observation.source_revision}"
            )
        return str(existing["id"]), False
    conn.execute(
        """
        INSERT INTO ops_observations(
          id, canonical_key, source_type, account_key, stream_key, source_key,
          source_revision, source_order, source_updated_at, observed_at, item_kind,
          event_kind, payload, content_hash, evidence_refs, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation_id,
            canonical_source_key(observation),
            observation.source_type,
            observation.account_key,
            observation.stream_key,
            observation.source_key,
            observation.source_revision,
            observation.source_order,
            observation.effective_source_updated_at,
            _canonical_timestamp(observation.observed_at, "observed_at"),
            observation.item_kind,
            "cancelled" if observation.cancelled else "upsert",
            payload_json,
            content_hash,
            evidence_json,
            created_at,
        ),
    )
    return observation_id, True


def _create_item(
    conn: sqlite3.Connection,
    observation: OperationalObservation,
    *,
    observation_id: str,
    canonical_key: str,
    timestamp: str,
    run_id: str,
) -> ReconciliationResult:
    item_id = operational_item_id(observation)
    state = "cancelled" if observation.cancelled else "active"
    payload = observation.payload()
    conn.execute(
        """
        INSERT INTO ops_items(
          id, canonical_key, source_type, account_key, stream_key, source_key,
          item_kind, state, title, details, owner, counterparty_entity_id,
          project_ref, starts_at, due_at, ends_at, source_timezone, expires_at,
          snoozed_until, human_confirmed_at, last_human_action_at, confidence,
          priority, reconciliation_method,
          current_observation_id, metadata, created_at, updated_at,
          state_changed_at
        ) VALUES (
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
          ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            item_id,
            canonical_key,
            observation.source_type,
            observation.account_key,
            observation.stream_key,
            observation.source_key,
            payload["item_kind"],
            state,
            payload["title"],
            payload["details"],
            payload["owner"],
            payload["counterparty_entity_id"],
            payload["project_ref"],
            payload["starts_at"],
            payload["due_at"],
            payload["ends_at"],
            payload["source_timezone"],
            payload["expires_at"],
            None,
            None,
            None,
            payload["confidence"],
            payload["priority"],
            RECONCILIATION_VERSION,
            observation_id,
            _bounded_json(payload["metadata"], "item metadata", MAX_METADATA_BYTES),
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    row = conn.execute(
        "SELECT * FROM ops_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    assert row is not None
    after = row_to_item(row)
    source = _load_observation(conn, observation_id)
    event_id = _append_event(
        conn,
        item_id=item_id,
        observation=source,
        event_type="created",
        actor="connector",
        before={},
        after=after,
        metadata={"source_event": "cancelled" if observation.cancelled else "upsert"},
        run_id=run_id,
        reconciliation_version=RECONCILIATION_VERSION,
        idempotency_key=f"observation:{observation_id}",
        created_at=timestamp,
    )
    return ReconciliationResult(
        observation_id=observation_id,
        item_id=item_id,
        event_id=event_id,
        event_type="created",
        state=state,
        observation_created=True,
        item_created=True,
        item_changed=True,
    )


def _update_item(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    observation: OperationalObservation,
    *,
    observation_id: str,
    timestamp: str,
    run_id: str,
    observation_created: bool = True,
    authority_reapplied: bool = False,
) -> ReconciliationResult:
    before = row_to_item(row)
    previous_observation = _load_observation(conn, row["current_observation_id"])
    source = _load_observation(conn, observation_id)
    payload = observation.payload()
    source_cancelled_before = bool(previous_observation["payload"]["cancelled"])
    source_cancelled_now = bool(payload["cancelled"])
    if row["human_override_state"]:
        state = str(row["human_override_state"])
    elif source_cancelled_now:
        state = "cancelled"
    else:
        state = "active"
    time_changed = any(before[field] != payload[field] for field in RESCHEDULE_FIELDS)
    if source_cancelled_now and not source_cancelled_before:
        event_type = "cancelled"
    elif time_changed:
        event_type = "rescheduled"
    else:
        event_type = "updated"
    state_changed_at = timestamp if state != row["state"] else row["state_changed_at"]
    conn.execute(
        """
        UPDATE ops_items
        SET item_kind = ?, state = ?, title = ?, details = ?, owner = ?,
            counterparty_entity_id = ?, project_ref = ?, starts_at = ?,
            due_at = ?, ends_at = ?, source_timezone = ?, expires_at = ?,
            confidence = ?, priority = ?, reconciliation_method = ?,
            current_observation_id = ?, metadata = ?, updated_at = ?,
            state_changed_at = ?
        WHERE id = ?
        """,
        (
            payload["item_kind"],
            state,
            payload["title"],
            payload["details"],
            payload["owner"],
            payload["counterparty_entity_id"],
            payload["project_ref"],
            payload["starts_at"],
            payload["due_at"],
            payload["ends_at"],
            payload["source_timezone"],
            payload["expires_at"],
            payload["confidence"],
            payload["priority"],
            RECONCILIATION_VERSION,
            observation_id,
            _bounded_json(payload["metadata"], "item metadata", MAX_METADATA_BYTES),
            timestamp,
            state_changed_at,
            row["id"],
        ),
    )
    updated = conn.execute(
        "SELECT * FROM ops_items WHERE id = ?",
        (row["id"],),
    ).fetchone()
    assert updated is not None
    after = row_to_item(updated)
    event_id = _append_event(
        conn,
        item_id=str(row["id"]),
        observation=source,
        event_type=event_type,
        actor="connector",
        before=before,
        after=after,
        metadata={
            "changed_fields": [
                field for field in PROJECTED_FIELDS if before[field] != payload[field]
            ],
            "source_cancelled_before": source_cancelled_before,
            "source_cancelled_now": source_cancelled_now,
            "authority_reapplied": authority_reapplied,
        },
        run_id=run_id,
        reconciliation_version=RECONCILIATION_VERSION,
        idempotency_key=(
            "authority:"
            + hashlib.sha256(
                _json([observation_id, previous_observation["id"], run_id]).encode(
                    "utf-8"
                )
            ).hexdigest()
            if authority_reapplied
            else f"observation:{observation_id}"
        ),
        created_at=timestamp,
    )
    return ReconciliationResult(
        observation_id=observation_id,
        item_id=str(row["id"]),
        event_id=event_id,
        event_type=event_type,
        state=state,
        observation_created=observation_created,
        item_created=False,
        item_changed=True,
    )


def _save_source_cursor_in_connection(
    conn: sqlite3.Connection,
    update: SourceCursorUpdate,
    *,
    updated_at: str,
) -> dict[str, Any]:
    updated_at = _canonical_timestamp(updated_at, "updated_at")
    last_success_at = _optional_canonical_timestamp(
        update.last_success_at,
        "last_success_at",
    )
    current = conn.execute(
        """
        SELECT * FROM ops_source_cursors
        WHERE connector_id = ? AND account_key = ? AND stream_key = ?
        """,
        (update.connector_id, update.account_key, update.stream_key),
    ).fetchone()
    current_cursor = current["cursor"] if current is not None else None
    current_generation = int(current["generation"]) if current is not None else None
    if current is not None and current["source_type"] != update.source_type:
        raise CursorConflictError(
            "operational source cursor source_type changed: "
            f"expected {update.source_type!r}, found {current['source_type']!r}"
        )
    if update.enforce_expected_cursor and (
        current_cursor != update.expected_cursor
        or current_generation != update.expected_generation
    ):
        raise CursorConflictError(
            "operational source cursor changed concurrently: "
            f"expected cursor/generation {update.expected_cursor!r}/"
            f"{update.expected_generation!r}, found {current_cursor!r}/"
            f"{current_generation!r}"
        )
    metadata_json = _bounded_json(
        dict(update.metadata),
        "cursor metadata",
        MAX_CURSOR_METADATA_BYTES,
    )
    conn.execute(
        """
        INSERT INTO ops_source_cursors(
          connector_id, source_type, account_key, stream_key, cursor, watermark,
          metadata, last_success_at, generation, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(connector_id, account_key, stream_key) DO UPDATE SET
          cursor = excluded.cursor,
          watermark = excluded.watermark,
          metadata = excluded.metadata,
          last_success_at = COALESCE(
            excluded.last_success_at,
            ops_source_cursors.last_success_at
          ),
          generation = ops_source_cursors.generation + 1,
          updated_at = excluded.updated_at
        """,
        (
            update.connector_id,
            update.source_type,
            update.account_key,
            update.stream_key,
            update.cursor,
            update.watermark,
            metadata_json,
            last_success_at,
            updated_at,
        ),
    )
    row = conn.execute(
        """
        SELECT * FROM ops_source_cursors
        WHERE connector_id = ? AND account_key = ? AND stream_key = ?
        """,
        (update.connector_id, update.account_key, update.stream_key),
    ).fetchone()
    assert row is not None
    return row_to_source_cursor(row)


def _append_event(
    conn: sqlite3.Connection,
    *,
    item_id: str,
    observation: Mapping[str, Any] | None,
    event_type: str,
    actor: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    metadata: Mapping[str, Any],
    run_id: str,
    reconciliation_version: str,
    idempotency_key: str | None,
    created_at: str,
) -> str:
    sequence = int(
        conn.execute(
            """
            SELECT COALESCE(MAX(sequence), 0) + 1
            FROM ops_item_events
            WHERE item_id = ?
            """,
            (item_id,),
        ).fetchone()[0]
    )
    before_json = _json(dict(before))
    after_json = _json(dict(after))
    metadata_json = _json(dict(metadata))
    observation_id = str(observation["id"]) if observation is not None else None
    source_type = str(observation["source_type"]) if observation is not None else None
    account_key = str(observation["account_key"]) if observation is not None else None
    stream_key = str(observation["stream_key"]) if observation is not None else None
    source_key = str(observation["source_key"]) if observation is not None else None
    source_revision = (
        str(observation["source_revision"]) if observation is not None else None
    )
    source_order = int(observation["source_order"]) if observation is not None else None
    transition = {
        "item_id": item_id,
        "observation_id": observation_id,
        "event_type": event_type,
        "actor": actor,
        "sequence": sequence,
        "from_state": before.get("state"),
        "to_state": after["state"],
        "source_type": source_type,
        "account_key": account_key,
        "stream_key": stream_key,
        "source_key": source_key,
        "source_revision": source_revision,
        "source_order": source_order,
        "run_id": run_id,
        "reconciliation_version": reconciliation_version,
        "before_state": json.loads(before_json),
        "after_state": json.loads(after_json),
        "metadata": json.loads(metadata_json),
        "idempotency_key": idempotency_key,
        "created_at": created_at,
    }
    transition_hash = hashlib.sha256(_json(transition).encode("utf-8")).hexdigest()
    event_id = new_id("itemevent")
    conn.execute(
        """
        INSERT INTO ops_item_events(
          id, item_id, observation_id, event_type, actor, sequence, from_state,
          to_state, source_type, account_key, stream_key, source_key,
          source_revision, source_order, run_id, reconciliation_version,
          before_state, after_state, metadata, transition_hash, idempotency_key,
          created_at
        ) VALUES (
          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        (
            event_id,
            item_id,
            observation_id,
            event_type,
            actor,
            sequence,
            before.get("state"),
            str(after["state"]),
            source_type,
            account_key,
            stream_key,
            source_key,
            source_revision,
            source_order,
            run_id,
            reconciliation_version,
            before_json,
            after_json,
            metadata_json,
            transition_hash,
            idempotency_key,
            created_at,
        ),
    )
    return event_id


def _load_observation(conn: sqlite3.Connection, observation_id: Any) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM ops_observations WHERE id = ?",
        (str(observation_id),),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"operational observation not found: {observation_id}")
    return row_to_observation(row)


def _observation_order_key(observation: Mapping[str, Any]) -> tuple[int, float]:
    raw_updated = observation["source_updated_at"]
    updated_timestamp = float("-inf")
    if raw_updated is not None:
        updated_timestamp = _parse_source_timestamp(
            str(raw_updated),
            "source_updated_at",
        ).timestamp()
    return (
        int(observation["source_order"]),
        updated_timestamp,
    )


def _noop_reconciliation(
    item: Mapping[str, Any],
    observation_id: str,
) -> ReconciliationResult:
    return ReconciliationResult(
        observation_id=observation_id,
        item_id=str(item["id"]),
        event_id=None,
        event_type="noop",
        state=str(item["state"]),
        observation_created=False,
        item_created=False,
        item_changed=False,
    )


def _restores_provider_authority(
    incoming: Mapping[str, Any],
    current: Mapping[str, Any],
) -> bool:
    """Let a retained provider revision replace a synthetic uncertainty marker."""

    if not _is_synthetic_revalidation(current):
        return False
    if _is_synthetic_revalidation(incoming):
        return False
    predecessor_revisions = {
        str(reference.get("source_revision") or "")
        for reference in current.get("evidence_refs") or ()
        if isinstance(reference, Mapping)
    }
    return str(incoming["source_revision"]) in predecessor_revisions


def _is_synthetic_revalidation(observation: Mapping[str, Any]) -> bool:
    metadata = dict(observation.get("payload", {}).get("metadata") or {})
    if metadata.get("revalidation_reason"):
        return True
    revision = str(observation.get("source_revision") or "")
    return revision.startswith(("calendar-revalidation-", "gmail-revalidation-"))


def _require_state(state: str, allowed: set[str], decision: str) -> None:
    if state not in allowed:
        raise ValueError(f"feedback {decision} is not valid from state {state}")


def _validate_identifier(value: Any, label: str, maximum: int) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty canonical string")
    if len(value) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters")


def _validate_text(
    value: Any,
    label: str,
    maximum: int,
    *,
    required: bool = False,
) -> None:
    if value is None:
        if required:
            raise ValueError(f"{label} is required")
        return
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text")
    if required and not value.strip():
        raise ValueError(f"{label} is required")
    if len(value) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} characters")


def _parse_source_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    if parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be normalized to UTC")
    return parsed.astimezone(timezone.utc)


def _canonical_timestamp(value: str, label: str) -> str:
    return _parse_source_timestamp(value, label).isoformat()


def _optional_canonical_timestamp(value: str | None, label: str) -> str | None:
    return _canonical_timestamp(value, label) if value is not None else None


def _reject_embedded_source_bodies(value: Any, label: str) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).casefold() in FORBIDDEN_REFERENCE_KEYS:
                raise ValueError(
                    f"{label} must contain references/metadata, not source bodies ({key})"
                )
            _reject_embedded_source_bodies(nested, label)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_embedded_source_bodies(nested, label)


def _validate_evidence_refs(evidence_refs: Sequence[Any]) -> None:
    for index, reference in enumerate(evidence_refs):
        if not isinstance(reference, Mapping):
            raise ValueError(f"evidence_refs[{index}] must be a reference object")
        keys = {str(key) for key in reference}
        unknown = sorted(keys - EVIDENCE_REF_KEYS)
        if unknown:
            raise ValueError(
                f"evidence_refs[{index}] has unsupported keys: {', '.join(unknown)}"
            )
        if not keys.intersection(EVIDENCE_IDENTITY_KEYS):
            raise ValueError(
                f"evidence_refs[{index}] requires a stable source identity"
            )
        for key, value in reference.items():
            if key in EVIDENCE_INTEGER_KEYS:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError(
                        f"evidence_refs[{index}].{key} must be a non-negative integer"
                    )
                continue
            if not isinstance(value, str) or not value.strip() or len(value) > 1024:
                raise ValueError(
                    f"evidence_refs[{index}].{key} must be bounded non-empty text"
                )


def _validate_observation_metadata(metadata: Mapping[str, Any]) -> None:
    _reject_embedded_source_bodies(metadata, "metadata")
    unknown = sorted({str(key) for key in metadata} - OBSERVATION_METADATA_KEYS_V1)
    if unknown:
        raise ValueError(
            "observation metadata v1 has unsupported keys: " + ", ".join(unknown)
        )
    for key, value in metadata.items():
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, int) and not isinstance(value, bool):
            continue
        if not isinstance(value, str) or len(value) > 1024:
            raise ValueError(f"observation metadata v1 {key} must be a bounded scalar")


def _validate_cursor_metadata(metadata: Mapping[str, Any]) -> None:
    unknown = sorted({str(key) for key in metadata} - CURSOR_METADATA_KEYS)
    if unknown:
        raise ValueError(f"cursor metadata has unsupported keys: {', '.join(unknown)}")
    for key, value in metadata.items():
        if value is None or isinstance(value, (bool, int)):
            continue
        maximum = (
            MAX_PENDING_THREAD_IDS_BYTES
            if key == "pending_thread_ids"
            else 8_192
            if key in {"continuation_page_token", "reset_seen_item_ids"}
            else 256
        )
        if not isinstance(value, str) or len(value) > maximum:
            raise ValueError(f"cursor metadata {key} must be a bounded scalar")


def _bounded_json(value: Any, label: str, maximum_bytes: int) -> str:
    try:
        encoded = _json(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be canonical JSON") from exc
    if len(encoded.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{label} exceeds {maximum_bytes} bytes")
    return encoded


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
