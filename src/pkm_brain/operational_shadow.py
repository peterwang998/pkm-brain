from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .operational_db import operational_connection
from .operational_state import (
    OperationalObservation,
    ReconciliationResult,
    SourceCursorUpdate,
    SourceUnitResult,
    _prepare_source_unit,
    _reconcile_source_unit_in_connection,
)
from .util import new_id, now_iso


SHADOW_RUN_MODES = {"live", "replay", "fixture"}
SHADOW_RUN_STATUSES = {"running", "complete", "partial", "failed", "stopped"}
SHADOW_DISPOSITIONS = {"surfaced", "suppressed", "deferred", "error"}
HANDLED_VERDICTS = {
    "needs_action",
    "responded_waiting",
    "being_handled",
    "fulfilled",
    "unknown",
}
BRIEFING_STATUSES = {"complete", "partial", "unavailable"}
MISSING_REPORT_STATUSES = {"open", "resolved", "dismissed"}
ITEM_KINDS = {"event", "commitment", "waiting", "follow_up", "deadline", "attention"}
FORBIDDEN_EMBEDDED_KEYS = {
    "attachment",
    "attachments",
    "body",
    "content",
    "html",
    "payload",
    "raw",
    "text",
}
MAX_JSON_BYTES = 262_144
MAX_EVIDENCE_REFS = 64


@dataclass(frozen=True)
class ShadowDecision:
    source_type: str
    account_key: str
    stream_key: str
    source_key: str
    disposition: str
    reason_code: str
    source_revision: str | None = None
    item_ids: Sequence[str] = field(default_factory=tuple)
    evidence_refs: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    confidence: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validated(self) -> ShadowDecision:
        _bounded_text(self.source_type, "source_type", 128)
        _bounded_text(self.account_key, "account_key", 512)
        _bounded_text(self.stream_key, "stream_key", 512)
        _bounded_text(self.source_key, "source_key", 1024)
        _bounded_optional_text(self.source_revision, "source_revision", 1024)
        _bounded_text(self.reason_code, "reason_code", 128)
        if self.disposition not in SHADOW_DISPOSITIONS:
            raise ValueError("unsupported shadow decision disposition")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("shadow decision confidence must be between 0 and 1")
        _bounded_string_list(self.item_ids, "item_ids", maximum=256)
        _validate_evidence_refs(self.evidence_refs)
        _validate_metadata(self.metadata, "shadow decision metadata")
        return self


@dataclass(frozen=True)
class HandledAssessment:
    item_id: str
    verdict: str
    observation_id: str | None = None
    source_revision: str | None = None
    supporting_evidence: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    contradicting_evidence: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    sources_checked: Sequence[str] = field(default_factory=tuple)
    coverage: Mapping[str, Any] = field(default_factory=dict)
    method_version: str = "source-local-v1"
    policy_version: str = "operations-v1"
    confidence: float = 0.0
    as_of: str = field(default_factory=now_iso)

    def validated(self) -> HandledAssessment:
        _bounded_text(self.item_id, "item_id", 256)
        _bounded_optional_text(self.observation_id, "observation_id", 256)
        _bounded_optional_text(self.source_revision, "source_revision", 1024)
        if self.verdict not in HANDLED_VERDICTS:
            raise ValueError("unsupported handled verdict")
        _validate_evidence_refs(self.supporting_evidence)
        _validate_evidence_refs(self.contradicting_evidence)
        _bounded_string_list(self.sources_checked, "sources_checked", maximum=128)
        _validate_metadata(self.coverage, "handled coverage")
        _bounded_text(self.method_version, "method_version", 128)
        _bounded_text(self.policy_version, "policy_version", 128)
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("handled confidence must be between 0 and 1")
        _canonical_timestamp(self.as_of, "as_of")
        return self


@dataclass(frozen=True)
class ShadowSourceUnitResult:
    """All projections committed for one provider source unit."""

    source_unit: SourceUnitResult
    decisions: tuple[dict[str, Any], ...]
    handled_assessments: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_unit": self.source_unit.as_dict(),
            "decisions": list(self.decisions),
            "handled_assessments": list(self.handled_assessments),
        }


def start_shadow_run(
    db_path: Path,
    *,
    mode: str,
    requested_sources: Sequence[str],
    policy_version: str,
    detector_version: str | None = None,
    started_at: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    if mode not in SHADOW_RUN_MODES:
        raise ValueError("shadow run mode must be live, replay, or fixture")
    sources = _bounded_string_list(
        requested_sources,
        "requested_sources",
        maximum=128,
    )
    _bounded_text(policy_version, "policy_version", 128)
    _bounded_optional_text(detector_version, "detector_version", 128)
    effective_id = run_id or new_id("opsshadow")
    _bounded_text(effective_id, "run_id", 256)
    timestamp = _canonical_timestamp(started_at or now_iso(), "started_at")
    with operational_connection(db_path, write=True) as conn:
        conn.execute(
            """
            INSERT INTO ops_shadow_runs(
              id, mode, status, requested_sources, policy_version,
              detector_version, started_at, created_at, updated_at
            ) VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?)
            """,
            (
                effective_id,
                mode,
                _json(sources),
                policy_version,
                detector_version,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        row = conn.execute(
            "SELECT * FROM ops_shadow_runs WHERE id = ?",
            (effective_id,),
        ).fetchone()
    assert row is not None
    return _run_row(row)


def finish_shadow_run(
    db_path: Path,
    run_id: str,
    *,
    status: str,
    coverage: Mapping[str, Any],
    usage: Mapping[str, Any] | None = None,
    counts: Mapping[str, Any] | None = None,
    error: str | None = None,
    hard_stop_reason: str | None = None,
    finished_at: str | None = None,
) -> dict[str, Any]:
    _bounded_text(run_id, "run_id", 256)
    if status not in SHADOW_RUN_STATUSES - {"running"}:
        raise ValueError("finished shadow run has an invalid status")
    _validate_metadata(coverage, "shadow coverage")
    _validate_metadata(usage or {}, "shadow usage")
    _validate_metadata(counts or {}, "shadow counts")
    _bounded_optional_text(error, "error", 4000)
    _bounded_optional_text(hard_stop_reason, "hard_stop_reason", 1000)
    timestamp = _canonical_timestamp(finished_at or now_iso(), "finished_at")
    with operational_connection(db_path, write=True) as conn:
        current = conn.execute(
            "SELECT * FROM ops_shadow_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if current is None:
            raise ValueError(f"unknown shadow run: {run_id}")
        if str(current["status"]) != "running":
            existing = _run_row(current)
            if existing["status"] == status:
                return existing
            raise ValueError("a finished shadow run cannot be rewritten")
        conn.execute(
            """
            UPDATE ops_shadow_runs
            SET status = ?, finished_at = ?, coverage = ?, usage = ?, counts = ?,
                error = ?, hard_stop_reason = ?, updated_at = ?
            WHERE id = ? AND status = 'running'
            """,
            (
                status,
                timestamp,
                _bounded_json(coverage, "shadow coverage", 32_768),
                _bounded_json(usage or {}, "shadow usage", 16_384),
                _bounded_json(counts or {}, "shadow counts", 16_384),
                error,
                hard_stop_reason,
                timestamp,
                run_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM ops_shadow_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    assert row is not None
    return _run_row(row)


def interrupt_running_shadow_runs(
    db_path: Path,
    *,
    interrupted_at: str | None = None,
) -> list[dict[str, Any]]:
    """Close runs that could only have survived a terminated daemon process."""

    timestamp = _canonical_timestamp(interrupted_at or now_iso(), "interrupted_at")
    with operational_connection(db_path, write=True) as conn:
        rows = conn.execute(
            """
            SELECT id FROM ops_shadow_runs
            WHERE status = 'running'
            ORDER BY started_at, id
            """
        ).fetchall()
        run_ids = [str(row["id"]) for row in rows]
        if not run_ids:
            return []
        placeholders = ",".join("?" for _ in run_ids)
        conn.execute(
            f"""
            UPDATE ops_shadow_runs
            SET status = 'stopped', finished_at = ?,
                error = 'Shadow run was interrupted by daemon termination.',
                hard_stop_reason = 'daemon_restart_interrupted_run',
                updated_at = ?
            WHERE status = 'running' AND id IN ({placeholders})
            """,
            (timestamp, timestamp, *run_ids),
        )
        closed = conn.execute(
            f"""
            SELECT * FROM ops_shadow_runs
            WHERE id IN ({placeholders})
            ORDER BY started_at, id
            """,
            run_ids,
        ).fetchall()
    return [_run_row(row) for row in closed]


def record_shadow_decision(
    db_path: Path,
    run_id: str,
    decision: ShadowDecision,
    *,
    created_at: str | None = None,
) -> dict[str, Any]:
    decision.validated()
    _bounded_text(run_id, "run_id", 256)
    timestamp = _canonical_timestamp(created_at or now_iso(), "created_at")
    with operational_connection(db_path, write=True) as conn:
        return _record_shadow_decision_in_connection(
            conn,
            run_id,
            decision,
            created_at=timestamp,
        )


def _record_shadow_decision_in_connection(
    conn: sqlite3.Connection,
    run_id: str,
    decision: ShadowDecision,
    *,
    created_at: str,
) -> dict[str, Any]:
    decision_id = new_id("opsdecision")
    conn.execute(
        """
        INSERT INTO ops_shadow_decisions(
          id, run_id, source_type, account_key, stream_key, source_key,
          source_revision, disposition, reason_code, item_ids,
          evidence_refs, confidence, metadata, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision_id,
            run_id,
            decision.source_type,
            decision.account_key,
            decision.stream_key,
            decision.source_key,
            decision.source_revision,
            decision.disposition,
            decision.reason_code,
            _bounded_json(list(decision.item_ids), "item_ids", 8192),
            _bounded_json(
                list(decision.evidence_refs),
                "evidence_refs",
                16_384,
            ),
            float(decision.confidence),
            _bounded_json(decision.metadata, "decision metadata", 8192),
            created_at,
        ),
    )
    row = conn.execute(
        "SELECT * FROM ops_shadow_decisions WHERE id = ?",
        (decision_id,),
    ).fetchone()
    assert row is not None
    return _decision_row(row)


def record_handled_assessment(
    db_path: Path,
    assessment: HandledAssessment,
    *,
    run_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    assessment.validated()
    _bounded_optional_text(run_id, "run_id", 256)
    timestamp = _canonical_timestamp(created_at or now_iso(), "created_at")
    with operational_connection(db_path, write=True) as conn:
        return _record_handled_assessment_in_connection(
            conn,
            assessment,
            run_id=run_id,
            created_at=timestamp,
        )


def _record_handled_assessment_in_connection(
    conn: sqlite3.Connection,
    assessment: HandledAssessment,
    *,
    run_id: str | None,
    created_at: str,
) -> dict[str, Any]:
    binding = conn.execute(
        """
        SELECT i.current_observation_id, o.source_revision
        FROM ops_items i
        JOIN ops_observations o ON o.id = i.current_observation_id
        WHERE i.id = ?
        """,
        (assessment.item_id,),
    ).fetchone()
    if binding is None:
        raise ValueError(
            "handled assessment references an unknown operational item: "
            f"{assessment.item_id}"
        )
    observation_id = str(binding["current_observation_id"])
    source_revision = str(binding["source_revision"])
    if (
        assessment.observation_id is not None
        and assessment.observation_id != observation_id
    ):
        raise ValueError("handled assessment observation is not the item's current revision")
    if (
        assessment.source_revision is not None
        and assessment.source_revision != source_revision
    ):
        raise ValueError("handled assessment source revision is not current")
    canonical = {
        "item_id": assessment.item_id,
        "observation_id": observation_id,
        "source_revision": source_revision,
        "run_id": run_id,
        "verdict": assessment.verdict,
        "supporting_evidence": list(assessment.supporting_evidence),
        "contradicting_evidence": list(assessment.contradicting_evidence),
        "sources_checked": list(assessment.sources_checked),
        "coverage": dict(assessment.coverage),
        "method_version": assessment.method_version,
        "policy_version": assessment.policy_version,
        "confidence": float(assessment.confidence),
        "as_of": _canonical_timestamp(assessment.as_of, "as_of"),
    }
    assessment_hash = hashlib.sha256(_json(canonical).encode("utf-8")).hexdigest()
    existing = conn.execute(
        "SELECT * FROM ops_handled_assessments WHERE assessment_hash = ?",
        (assessment_hash,),
    ).fetchone()
    if existing is not None:
        return {**_assessment_row(existing), "idempotent": True}
    assessment_id = new_id("opshandled")
    conn.execute(
        """
        INSERT INTO ops_handled_assessments(
          id, item_id, observation_id, source_revision, run_id, verdict,
          supporting_evidence,
          contradicting_evidence, sources_checked, coverage,
          method_version, policy_version, confidence, as_of,
          assessment_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            assessment_id,
            assessment.item_id,
            observation_id,
            source_revision,
            run_id,
            assessment.verdict,
            _bounded_json(
                list(assessment.supporting_evidence),
                "supporting evidence",
                16_384,
            ),
            _bounded_json(
                list(assessment.contradicting_evidence),
                "contradicting evidence",
                16_384,
            ),
            _bounded_json(
                list(assessment.sources_checked),
                "sources checked",
                4096,
            ),
            _bounded_json(assessment.coverage, "handled coverage", 8192),
            assessment.method_version,
            assessment.policy_version,
            float(assessment.confidence),
            canonical["as_of"],
            assessment_hash,
            created_at,
        ),
    )
    row = conn.execute(
        "SELECT * FROM ops_handled_assessments WHERE id = ?",
        (assessment_id,),
    ).fetchone()
    assert row is not None
    return {**_assessment_row(row), "idempotent": False}


def persist_shadow_source_unit(
    db_path: Path,
    observations: Sequence[OperationalObservation],
    *,
    cursor_update: SourceCursorUpdate | None,
    decisions: Sequence[ShadowDecision] = (),
    handled_assessments: Sequence[HandledAssessment] = (),
    processed_at: str | None = None,
    run_id: str,
) -> ShadowSourceUnitResult:
    """Atomically persist a provider unit and advance its cursor last.

    Validation happens before opening the transaction. Reconciliation, audit
    decisions, handled-state assessments, and the provider cursor then share one
    SQLite transaction. Any projection failure rolls every mutation back.
    """

    timestamp, effective_run_id = _prepare_source_unit(
        observations,
        cursor_update=cursor_update,
        processed_at=processed_at,
        run_id=run_id,
    )
    for decision in decisions:
        decision.validated()
    for assessment in handled_assessments:
        assessment.validated()
    unit = _source_unit_identity(observations, cursor_update)
    for decision in decisions:
        decision_unit = (
            decision.source_type,
            decision.account_key,
            decision.stream_key,
        )
        if decision_unit != unit:
            raise ValueError("shadow decisions must match their source unit")

    decision_rows: list[dict[str, Any]] = []
    assessment_rows: list[dict[str, Any]] = []
    with operational_connection(db_path, write=True) as conn:

        def persist_audit_before_cursor(
            _reconciliations: tuple[ReconciliationResult, ...],
        ) -> None:
            for decision in decisions:
                _require_existing_decision_items(conn, decision)
                decision_rows.append(
                    _record_shadow_decision_in_connection(
                        conn,
                        effective_run_id,
                        decision,
                        created_at=timestamp,
                    )
                )
            for assessment in handled_assessments:
                assessment_rows.append(
                    _record_handled_assessment_in_connection(
                        conn,
                        assessment,
                        run_id=effective_run_id,
                        created_at=timestamp,
                    )
                )

        source_unit = _reconcile_source_unit_in_connection(
            conn,
            observations,
            cursor_update=cursor_update,
            processed_at=timestamp,
            run_id=effective_run_id,
            before_cursor=persist_audit_before_cursor,
        )
    return ShadowSourceUnitResult(
        source_unit=source_unit,
        decisions=tuple(decision_rows),
        handled_assessments=tuple(assessment_rows),
    )


def _source_unit_identity(
    observations: Sequence[OperationalObservation],
    cursor_update: SourceCursorUpdate | None,
) -> tuple[str, str, str]:
    if cursor_update is not None:
        return (
            cursor_update.source_type,
            cursor_update.account_key,
            cursor_update.stream_key,
        )
    first = observations[0]
    return (first.source_type, first.account_key, first.stream_key)


def _require_existing_decision_items(
    conn: sqlite3.Connection,
    decision: ShadowDecision,
) -> None:
    for item_id in decision.item_ids:
        row = conn.execute("SELECT 1 FROM ops_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise ValueError(
                f"shadow decision references an unknown operational item: {item_id}"
            )


def save_briefing_snapshot(
    db_path: Path,
    *,
    as_of: str,
    timezone_name: str,
    policy_version: str,
    status: str,
    sections: Mapping[str, Any],
    coverage: Mapping[str, Any],
    counts: Mapping[str, Any] | None = None,
    run_id: str | None = None,
    generated_at: str | None = None,
    retention_days: int = 30,
) -> dict[str, Any]:
    if status not in BRIEFING_STATUSES:
        raise ValueError("unsupported briefing status")
    if not 1 <= retention_days <= 365:
        raise ValueError("briefing retention_days must be between 1 and 365")
    _bounded_text(timezone_name, "timezone", 128)
    _bounded_text(policy_version, "policy_version", 128)
    _bounded_optional_text(run_id, "run_id", 256)
    _validate_metadata(sections, "briefing sections")
    _validate_metadata(coverage, "briefing coverage")
    _validate_metadata(counts or {}, "briefing counts")
    generated = _canonical_timestamp(generated_at or now_iso(), "generated_at")
    effective_as_of = _canonical_timestamp(as_of, "as_of")
    expires = (
        _parse_timestamp(generated, "generated_at") + timedelta(days=retention_days)
    ).isoformat()
    canonical = {
        "run_id": run_id,
        "generated_at": generated,
        "as_of": effective_as_of,
        "timezone": timezone_name,
        "policy_version": policy_version,
        "status": status,
        "sections": dict(sections),
        "coverage": dict(coverage),
        "counts": dict(counts or {}),
    }
    snapshot_hash = hashlib.sha256(_json(canonical).encode("utf-8")).hexdigest()
    with operational_connection(db_path, write=True) as conn:
        conn.execute(
            "DELETE FROM ops_briefing_snapshots WHERE expires_at <= ?",
            (generated,),
        )
        existing = conn.execute(
            "SELECT * FROM ops_briefing_snapshots WHERE snapshot_hash = ?",
            (snapshot_hash,),
        ).fetchone()
        if existing is not None:
            return {**_briefing_row(existing), "idempotent": True}
        snapshot_id = new_id("opsbriefing")
        conn.execute(
            """
            INSERT INTO ops_briefing_snapshots(
              id, run_id, generated_at, as_of, timezone, policy_version,
              status, sections, coverage, counts, snapshot_hash,
              expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                run_id,
                generated,
                effective_as_of,
                timezone_name,
                policy_version,
                status,
                _bounded_json(sections, "briefing sections", 262_144),
                _bounded_json(coverage, "briefing coverage", 32_768),
                _bounded_json(counts or {}, "briefing counts", 16_384),
                snapshot_hash,
                expires,
                generated,
            ),
        )
        row = conn.execute(
            "SELECT * FROM ops_briefing_snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
    assert row is not None
    return {**_briefing_row(row), "idempotent": False}


def record_missing_report(
    db_path: Path,
    *,
    summary: str,
    run_id: str | None = None,
    source_type: str | None = None,
    source_ref: str | None = None,
    expected_kind: str | None = None,
    idempotency_key: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    _bounded_text(summary, "summary", 2000)
    _bounded_optional_text(run_id, "run_id", 256)
    _bounded_optional_text(source_type, "source_type", 128)
    _bounded_optional_text(source_ref, "source_ref", 1024)
    _bounded_optional_text(idempotency_key, "idempotency_key", 256)
    if expected_kind is not None and expected_kind not in ITEM_KINDS:
        raise ValueError("unsupported expected item kind")
    timestamp = _canonical_timestamp(created_at or now_iso(), "created_at")
    report_id = new_id("opsmissing")
    with operational_connection(db_path, write=True) as conn:
        if idempotency_key:
            existing = conn.execute(
                "SELECT * FROM ops_missing_reports WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                existing_row = dict(existing)
                if (
                    existing_row["summary"] != summary
                    or existing_row["source_type"] != source_type
                    or existing_row["source_ref"] != source_ref
                    or existing_row["expected_kind"] != expected_kind
                ):
                    raise ValueError(
                        "missing-report idempotency key was reused with different content"
                    )
                return {**existing_row, "idempotent": True}
        conn.execute(
            """
            INSERT INTO ops_missing_reports(
              id, run_id, source_type, source_ref, expected_kind, summary,
              status, idempotency_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
            """,
            (
                report_id,
                run_id,
                source_type,
                source_ref,
                expected_kind,
                summary,
                idempotency_key,
                timestamp,
                timestamp,
            ),
        )
        row = conn.execute(
            "SELECT * FROM ops_missing_reports WHERE id = ?",
            (report_id,),
        ).fetchone()
    assert row is not None
    return {**dict(row), "idempotent": False}


def list_shadow_runs(db_path: Path, *, limit: int = 20) -> list[dict[str, Any]]:
    safe_limit = min(max(int(limit), 1), 200)
    with operational_connection(db_path, write=False) as conn:
        rows = conn.execute(
            "SELECT * FROM ops_shadow_runs ORDER BY started_at DESC, id DESC LIMIT ?",
            (safe_limit,),
        ).fetchall()
    return [_run_row(row) for row in rows]


def get_shadow_run(db_path: Path, run_id: str) -> dict[str, Any] | None:
    with operational_connection(db_path, write=False) as conn:
        row = conn.execute(
            "SELECT * FROM ops_shadow_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    return _run_row(row) if row is not None else None


def list_shadow_decisions(
    db_path: Path,
    *,
    run_id: str | None = None,
    disposition: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    if disposition is not None and disposition not in SHADOW_DISPOSITIONS:
        raise ValueError("unsupported shadow disposition filter")
    clauses: list[str] = []
    params: list[Any] = []
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    if disposition:
        clauses.append("disposition = ?")
        params.append(disposition)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    params.append(min(max(int(limit), 1), 5000))
    with operational_connection(db_path, write=False) as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM ops_shadow_decisions{where}
            ORDER BY created_at DESC, id DESC LIMIT ?
            """,
            params,
        ).fetchall()
    return [_decision_row(row) for row in rows]


def latest_handled_assessments(db_path: Path) -> dict[str, dict[str, Any]]:
    with operational_connection(db_path, write=False) as conn:
        rows = conn.execute(
            """
            SELECT a.*
            FROM ops_handled_assessments a
            JOIN ops_items i ON i.id = a.item_id
            JOIN ops_observations o ON o.id = i.current_observation_id
            WHERE a.observation_id = i.current_observation_id
              AND a.source_revision = o.source_revision
              AND a.id = (
              SELECT candidate.id
              FROM ops_handled_assessments candidate
              WHERE candidate.item_id = a.item_id
                AND candidate.observation_id = i.current_observation_id
                AND candidate.source_revision = o.source_revision
              ORDER BY candidate.as_of DESC, candidate.created_at DESC,
                       candidate.id DESC
              LIMIT 1
            )
            """
        ).fetchall()
    return {str(row["item_id"]): _assessment_row(row) for row in rows}


def prune_expired_briefing_snapshots(
    db_path: Path,
    *,
    as_of: str | None = None,
) -> dict[str, int]:
    timestamp = _canonical_timestamp(as_of or now_iso(), "as_of")
    with operational_connection(db_path, write=True) as conn:
        rows = conn.execute(
            "SELECT id, length(sections) + length(coverage) AS bytes FROM ops_briefing_snapshots WHERE expires_at <= ?",
            (timestamp,),
        ).fetchall()
        conn.execute(
            "DELETE FROM ops_briefing_snapshots WHERE expires_at <= ?",
            (timestamp,),
        )
    return {
        "removed_snapshots": len(rows),
        "removed_bytes": sum(int(row["bytes"] or 0) for row in rows),
    }


def latest_briefing_snapshot(
    db_path: Path,
    *,
    as_of: str | None = None,
) -> dict[str, Any] | None:
    timestamp = _canonical_timestamp(as_of or now_iso(), "as_of")
    with operational_connection(db_path, write=False) as conn:
        row = conn.execute(
            """
            SELECT * FROM ops_briefing_snapshots
            WHERE expires_at > ?
            ORDER BY generated_at DESC, id DESC LIMIT 1
            """,
            (timestamp,),
        ).fetchone()
    return _briefing_row(row) if row is not None else None


def list_missing_reports(
    db_path: Path,
    *,
    status: str | None = "open",
    limit: int = 200,
) -> list[dict[str, Any]]:
    if status is not None and status not in MISSING_REPORT_STATUSES:
        raise ValueError("unsupported missing-report status")
    with operational_connection(db_path, write=False) as conn:
        if status is None:
            rows = conn.execute(
                """
                SELECT * FROM ops_missing_reports
                ORDER BY created_at DESC LIMIT ?
                """,
                (min(max(int(limit), 1), 1000),),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM ops_missing_reports
                WHERE status = ? ORDER BY created_at DESC LIMIT ?
                """,
                (status, min(max(int(limit), 1), 1000)),
            ).fetchall()
    return [dict(row) for row in rows]


def _run_row(row: Any) -> dict[str, Any]:
    output = dict(row)
    for key in ("requested_sources", "coverage", "usage", "counts"):
        output[key] = json.loads(str(output[key]))
    return output


def _decision_row(row: Any) -> dict[str, Any]:
    output = dict(row)
    for key in ("item_ids", "evidence_refs", "metadata"):
        output[key] = json.loads(str(output[key]))
    return output


def _assessment_row(row: Any) -> dict[str, Any]:
    output = dict(row)
    for key in (
        "supporting_evidence",
        "contradicting_evidence",
        "sources_checked",
        "coverage",
    ):
        output[key] = json.loads(str(output[key]))
    return output


def _briefing_row(row: Any) -> dict[str, Any]:
    output = dict(row)
    for key in ("sections", "coverage", "counts"):
        output[key] = json.loads(str(output[key]))
    return output


def _validate_evidence_refs(refs: Sequence[Mapping[str, Any]]) -> None:
    if len(refs) > MAX_EVIDENCE_REFS:
        raise ValueError(f"evidence references cannot exceed {MAX_EVIDENCE_REFS}")
    for index, ref in enumerate(refs):
        if not isinstance(ref, Mapping) or not ref:
            raise ValueError(f"evidence reference {index} must be a non-empty object")
        _validate_metadata(ref, f"evidence reference {index}")
        if not any(
            str(ref.get(key) or "").strip()
            for key in (
                "event_id",
                "message_id",
                "observation_id",
                "source_ref",
                "thread_id",
            )
        ):
            raise ValueError(f"evidence reference {index} needs a stable source identity")


def _validate_metadata(value: Any, label: str) -> None:
    _reject_source_bodies(value, label)
    _bounded_json(value, label, MAX_JSON_BYTES)


def _reject_source_bodies(value: Any, label: str) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).casefold() in FORBIDDEN_EMBEDDED_KEYS:
                raise ValueError(f"{label} cannot contain source bodies ({key})")
            _reject_source_bodies(nested, label)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_source_bodies(nested, label)


def _bounded_string_list(
    values: Sequence[str],
    label: str,
    *,
    maximum: int,
) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{label} must be a sequence")
    if len(values) > maximum:
        raise ValueError(f"{label} cannot exceed {maximum} values")
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        _bounded_text(value, label, 1024)
        if value not in seen:
            output.append(value)
            seen.add(value)
    return output


def _bounded_text(value: Any, label: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be non-empty text up to {maximum} characters")


def _bounded_optional_text(value: Any, label: str, maximum: int) -> None:
    if value is not None:
        _bounded_text(value, label, maximum)


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _canonical_timestamp(value: str, label: str) -> str:
    return _parse_timestamp(value, label).isoformat()


def _bounded_json(value: Any, label: str, maximum: int) -> str:
    try:
        encoded = _json(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be canonical JSON") from exc
    if len(encoded.encode("utf-8")) > maximum:
        raise ValueError(f"{label} exceeds {maximum} bytes")
    return encoded


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
