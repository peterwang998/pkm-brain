from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Protocol

import yaml

from .operational_evaluation import (
    AUTHORITATIVE_STATES,
    COVERAGE_STATES,
    FOCUS_PLACEMENTS,
    HANDLED_BASES,
    HANDLED_VERDICTS,
    ITEM_KINDS,
    LIFECYCLE_STATES,
    OWNERS,
    PRIORITIES,
    RESPONSIBILITIES,
    SENSITIVITIES,
    OperationalEvalFixture,
    OperationalEvaluationError,
    ShadowEvaluationReport,
    evaluate_shadow_run,
    load_operational_eval_fixture,
    operational_eval_fixture_from_dict,
    shadow_run_from_dict,
)
from .operations_policy import OperationsPolicy, load_operations_policy


RETROSPECTIVE_REPLAY_SCHEMA_VERSION = 1
TIMELINE_CLASSIFICATIONS = frozenset({"private", "synthetic"})
REVIEW_DECISIONS = frozenset({"confirm", "correct", "missing", "dismiss"})
MAX_NORMALIZED_RECORD_BYTES = 262_144
MAX_VERSIONS = 32
_TERMINAL_STATES = {"resolved", "dismissed", "cancelled", "expired"}
_ACTION_KINDS = {"commitment", "follow_up", "deadline", "attention"}
_PRIORITY_ORDER = {"critical": 4, "high": 3, "normal": 2, "low": 1, "awareness": 0}
_COVERAGE_ORDER = {"complete": 0, "partial": 1, "unavailable": 2}
_FORBIDDEN_NORMALIZED_KEYS = {
    "access_token",
    "api_key",
    "attachment",
    "attachments",
    "attachment_bytes",
    "client_secret",
    "credential",
    "html",
    "password",
    "payload",
    "raw",
    "raw_api_payload",
    "refresh_token",
    "secret",
}
_PROJECTION_FIELDS = frozenset(
    {
        "canonical_key",
        "source_revision",
        "source_order",
        "reconciliation_applied",
        "active_instances",
        "item_detected",
        "item_kind",
        "lifecycle_state",
        "handled_verdict",
        "owner",
        "responsibility",
        "due_at",
        "evidence_ids",
        "sensitivity",
        "handled_basis",
        "authoritative_state",
        "local_route_rendered",
        "local_route_valid",
        "provider_route_rendered",
        "provider_route_valid",
        "source_identity_correct",
        "calendar_change_applied",
        "priority",
        "confidence",
    }
)
_REVIEW_OVERRIDE_FIELDS = frozenset(
    {
        "item_expected",
        "item_kind",
        "lifecycle_state",
        "handled_verdict",
        "priority",
        "high_consequence",
        "human_confirmed",
        "owner",
        "responsibility",
        "due_at",
        "expected_evidence_ids",
        "sensitivity",
        "focus_expectation",
        "coverage",
        "authoritative_object_required",
        "authoritative_state",
        "calendar_change",
        "local_route_required",
        "provider_route_required",
    }
)


class RetrospectiveReplayError(ValueError):
    """Raised when an offline replay input or interface output is invalid."""


@dataclass(frozen=True)
class ReplayCheckpoint:
    checkpoint_id: str
    as_of: datetime
    coverage: dict[str, str]


@dataclass(frozen=True)
class NormalizedReplayRecord:
    case_id: str
    checkpoint_id: str
    observed_at: datetime
    source: str
    source_class: str
    normalized: dict[str, Any]
    recorded_projection: dict[str, Any]


@dataclass(frozen=True)
class ReplayUsage:
    calendar_requests: int
    gmail_api_requests: int
    gmail_detector_calls: int
    gmail_detector_input_tokens: int
    gmail_detector_total_tokens: int
    deferred_count: int
    deferred_disclosed: bool

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class ReplayAudit:
    external_write_count: int
    scope_violation_count: int
    privacy_violation_count: int


@dataclass(frozen=True)
class ReplayTimeline:
    schema_version: int
    fixture_id: str
    classification: str
    policy_version: int
    checkpoints: tuple[ReplayCheckpoint, ...]
    records: tuple[NormalizedReplayRecord, ...]
    usage: ReplayUsage
    audit: ReplayAudit
    versions: dict[str, str]


@dataclass(frozen=True)
class HumanReview:
    case_id: str
    reviewed_at: datetime
    decision: str
    reason_code: str
    overrides: dict[str, Any]


@dataclass(frozen=True)
class HumanReviewBundle:
    schema_version: int
    fixture_id: str
    reviews: tuple[HumanReview, ...]


@dataclass(frozen=True)
class RetrospectiveReplayReport:
    report: dict[str, Any]

    @property
    def hard_stop(self) -> bool:
        return bool(self.report["gate_status"]["hard_stop"])

    @property
    def promotion_passed(self) -> bool:
        return bool(self.report["gate_status"]["promotion_passed"])

    def as_dict(self) -> dict[str, Any]:
        return dict(self.report)


class RetrospectivePipeline(Protocol):
    """Offline-only deterministic prediction/reconciliation/briefing boundary."""

    def process(self, record: NormalizedReplayRecord) -> Mapping[str, Any]: ...

    def brief(
        self,
        checkpoint: ReplayCheckpoint,
        case_ids: Sequence[str],
    ) -> Mapping[str, Any]: ...


class RecordedProjectionPipeline:
    """Replay frozen detector output through deterministic reconciliation/ranking."""

    def __init__(self) -> None:
        self._current: dict[str, dict[str, Any]] = {}
        self._outputs: dict[str, dict[str, Any]] = {}

    def process(self, record: NormalizedReplayRecord) -> Mapping[str, Any]:
        proposed = dict(record.recorded_projection)
        _validate_projection(proposed, record.case_id)
        proposed["case_id"] = record.case_id
        canonical_key = str(proposed["canonical_key"])
        current = self._current.get(canonical_key)
        current_order = int(current["source_order"]) if current else None
        proposed_order = int(proposed["source_order"])
        stale_authority = current_order is not None and proposed_order < current_order
        equal_order_conflict = bool(
            current
            and proposed_order == current_order
            and str(proposed["source_revision"]) != str(current["source_revision"])
        )
        applied = bool(proposed["reconciliation_applied"])
        stale_active = bool((stale_authority or equal_order_conflict) and applied)
        resurrected = bool(
            current
            and str(current["lifecycle_state"]) in _TERMINAL_STATES
            and str(proposed["lifecycle_state"]) == "active"
            and applied
        )
        duplicate_active = bool(
            proposed["item_detected"]
            and proposed["lifecycle_state"] == "active"
            and int(proposed["active_instances"]) > 1
        )

        if applied:
            final = proposed
            self._current[canonical_key] = proposed
        elif current is not None:
            final = {**current, "case_id": record.case_id}
        else:
            final = {
                **proposed,
                "item_detected": False,
                "item_kind": "none",
                "lifecycle_state": "none",
                "handled_verdict": "not_applicable",
                "owner": "unknown",
                "responsibility": "unknown",
                "due_at": None,
                "evidence_ids": [],
                "handled_basis": "not_applicable",
            }
        output = {
            **final,
            "case_id": record.case_id,
            "duplicate_active": duplicate_active,
            "stale_active": stale_active,
            "resurrected": resurrected,
        }
        self._outputs[record.case_id] = output
        return output

    def brief(
        self,
        checkpoint: ReplayCheckpoint,
        case_ids: Sequence[str],
    ) -> Mapping[str, Any]:
        current = list(self._current.values())
        placements: dict[str, str] = {case_id: "suppressed" for case_id in case_ids}
        current_case_ids = {str(item["case_id"]) for item in current}
        action_candidates = [
            item
            for item in current
            if item["item_detected"]
            and item["lifecycle_state"] == "active"
            and item["item_kind"] in _ACTION_KINDS
            and item["handled_verdict"] == "needs_action"
            and float(item["confidence"]) >= 0.65
        ]
        action_candidates.sort(key=_briefing_rank, reverse=True)
        focus = action_candidates[:5]
        overflow = [
            item
            for item in action_candidates[5:]
            if item["priority"] in {"critical", "high"}
        ]
        for item in focus:
            if str(item["case_id"]) in placements:
                placements[str(item["case_id"])] = "focus"
        for item in overflow:
            if str(item["case_id"]) in placements:
                placements[str(item["case_id"])] = "urgent_overflow"

        action_case_ids = {str(item["case_id"]) for item in (*focus, *overflow)}
        for case_id in case_ids:
            if case_id not in current_case_ids or case_id in action_case_ids:
                continue
            item = self._outputs[case_id]
            if not item["item_detected"] or item["lifecycle_state"] in _TERMINAL_STATES:
                placements[case_id] = "suppressed"
            elif (
                float(item["confidence"]) < 0.65 or item["handled_verdict"] == "unknown"
            ):
                placements[case_id] = "low_confidence"
            elif item["item_kind"] == "event":
                placements[case_id] = "full_section"
            elif item["handled_verdict"] in {"responded_waiting", "being_handled"}:
                placements[case_id] = "full_section"
            elif item["handled_verdict"] == "fulfilled":
                placements[case_id] = "suppressed"
            elif item["item_detected"]:
                placements[case_id] = "full_section"

        coverage_complete = all(
            status == "complete" for status in checkpoint.coverage.values()
        )
        all_clear = coverage_complete and not focus and not overflow
        return {
            "placements": placements,
            "coverage": dict(checkpoint.coverage),
            "reported_all_clear": all_clear,
        }


def load_replay_timeline(
    path: str | Path,
    *,
    enforce_private_permissions: bool = True,
) -> ReplayTimeline:
    timeline_path = Path(path).expanduser()
    raw = _load_yaml_file(timeline_path, "replay timeline")
    timeline = replay_timeline_from_dict(raw)
    if timeline.classification == "private" and enforce_private_permissions:
        _require_owner_only(timeline_path, "private replay timeline")
    return timeline


def replay_timeline_from_dict(raw: Any) -> ReplayTimeline:
    data = _mapping(raw, "replay timeline")
    _exact_keys(
        data,
        {
            "schema_version",
            "fixture_id",
            "classification",
            "policy_version",
            "checkpoints",
            "records",
            "usage",
            "audit",
            "versions",
        },
        "replay timeline",
    )
    schema_version = _integer(data, "schema_version", minimum=1)
    if schema_version != RETROSPECTIVE_REPLAY_SCHEMA_VERSION:
        raise RetrospectiveReplayError(
            f"unsupported replay timeline schema_version: {schema_version}"
        )
    checkpoints = tuple(
        _parse_checkpoint(item)
        for item in _sequence(data.get("checkpoints"), "checkpoints", nonempty=True)
    )
    _require_unique(
        [checkpoint.checkpoint_id for checkpoint in checkpoints], "checkpoint_id"
    )
    if [checkpoint.as_of for checkpoint in checkpoints] != sorted(
        checkpoint.as_of for checkpoint in checkpoints
    ):
        raise RetrospectiveReplayError("replay checkpoints must be chronological")
    checkpoint_by_id = {
        checkpoint.checkpoint_id: checkpoint for checkpoint in checkpoints
    }
    records = tuple(
        _parse_record(item)
        for item in _sequence(data.get("records"), "records", nonempty=True)
    )
    _require_unique([record.case_id for record in records], "record case_id")
    for record in records:
        checkpoint = checkpoint_by_id.get(record.checkpoint_id)
        if checkpoint is None:
            raise RetrospectiveReplayError(
                f"record {record.case_id} references an unknown checkpoint"
            )
        if record.observed_at > checkpoint.as_of:
            raise RetrospectiveReplayError(
                f"record {record.case_id} occurs after its briefing checkpoint"
            )
        if record.source not in checkpoint.coverage:
            raise RetrospectiveReplayError(
                f"checkpoint {checkpoint.checkpoint_id} omits {record.source} coverage"
            )
    order = {
        checkpoint.checkpoint_id: index for index, checkpoint in enumerate(checkpoints)
    }
    if list(records) != sorted(
        records,
        key=lambda record: (
            order[record.checkpoint_id],
            record.observed_at,
            record.case_id,
        ),
    ):
        raise RetrospectiveReplayError(
            "replay records must be ordered by checkpoint and observation time"
        )
    used_checkpoints = {record.checkpoint_id for record in records}
    missing_records = set(checkpoint_by_id).difference(used_checkpoints)
    if missing_records:
        raise RetrospectiveReplayError(
            "every checkpoint needs at least one labeled record: "
            + ", ".join(sorted(missing_records))
        )
    timeline_sources = {record.source for record in records}
    for checkpoint in checkpoints:
        if set(checkpoint.coverage) != timeline_sources:
            raise RetrospectiveReplayError(
                f"checkpoint {checkpoint.checkpoint_id} must report every timeline source"
            )
    return ReplayTimeline(
        schema_version=schema_version,
        fixture_id=_string(data, "fixture_id", maximum=120),
        classification=_choice(data, "classification", TIMELINE_CLASSIFICATIONS),
        policy_version=_integer(data, "policy_version", minimum=1),
        checkpoints=checkpoints,
        records=records,
        usage=_parse_usage(data.get("usage")),
        audit=_parse_audit(data.get("audit")),
        versions=_parse_versions(data.get("versions")),
    )


def load_human_reviews(
    path: str | Path,
    *,
    private: bool,
    enforce_private_permissions: bool = True,
) -> HumanReviewBundle:
    review_path = Path(path).expanduser()
    raw = _load_yaml_file(review_path, "human reviews")
    bundle = human_reviews_from_dict(raw)
    if private and enforce_private_permissions:
        _require_owner_only(review_path, "private human reviews")
    return bundle


def human_reviews_from_dict(raw: Any) -> HumanReviewBundle:
    data = _mapping(raw, "human reviews")
    _exact_keys(data, {"schema_version", "fixture_id", "reviews"}, "human reviews")
    schema_version = _integer(data, "schema_version", minimum=1)
    if schema_version != RETROSPECTIVE_REPLAY_SCHEMA_VERSION:
        raise RetrospectiveReplayError(
            f"unsupported human-review schema_version: {schema_version}"
        )
    reviews = tuple(
        _parse_review(item) for item in _sequence(data.get("reviews"), "reviews")
    )
    _require_unique([review.case_id for review in reviews], "review case_id")
    return HumanReviewBundle(
        schema_version=schema_version,
        fixture_id=_string(data, "fixture_id", maximum=120),
        reviews=reviews,
    )


def apply_human_reviews(
    fixture: OperationalEvalFixture,
    bundle: HumanReviewBundle | None,
) -> OperationalEvalFixture:
    if bundle is None:
        return fixture
    if bundle.fixture_id != fixture.fixture_id:
        raise RetrospectiveReplayError("human reviews target a different fixture")
    raw = _fixture_as_dict(fixture)
    cases = {str(case["case_id"]): case for case in raw["cases"]}
    for review in bundle.reviews:
        case = cases.get(review.case_id)
        if case is None:
            raise RetrospectiveReplayError(
                f"human review references unknown case: {review.case_id}"
            )
        if review.decision == "confirm":
            continue
        if review.decision in {"correct", "missing"}:
            case.update(review.overrides)
            if review.decision == "missing" and case.get("item_expected") is not True:
                raise RetrospectiveReplayError(
                    f"missing review {review.case_id} must set item_expected=true"
                )
        elif review.decision == "dismiss":
            case.update(
                {
                    "item_expected": False,
                    "item_kind": "none",
                    "lifecycle_state": "none",
                    "handled_verdict": "not_applicable",
                    "priority": "awareness",
                    "high_consequence": False,
                    "human_confirmed": False,
                    "owner": "unknown",
                    "responsibility": "out_of_area",
                    "due_at": None,
                    "sensitivity": "normal",
                    "focus_expectation": "suppressed",
                    "authoritative_object_required": False,
                    "authoritative_state": "not_applicable",
                    "calendar_change": "none",
                    "local_route_required": False,
                    "provider_route_required": False,
                }
            )
    try:
        return operational_eval_fixture_from_dict(raw)
    except OperationalEvaluationError as exc:
        raise RetrospectiveReplayError(
            f"human review produced invalid labels: {exc}"
        ) from exc


def run_retrospective_replay(
    policy: OperationsPolicy,
    timeline: ReplayTimeline,
    labels: OperationalEvalFixture,
    *,
    reviews: HumanReviewBundle | None = None,
    pipeline: RetrospectivePipeline | None = None,
    generated_at: str | None = None,
    input_digests: Mapping[str, str] | None = None,
) -> RetrospectiveReplayReport:
    reviewed_labels = apply_human_reviews(labels, reviews)
    _validate_replay_binding(policy, timeline, reviewed_labels)
    active_pipeline = pipeline or RecordedProjectionPipeline()
    records_by_checkpoint: dict[str, list[NormalizedReplayRecord]] = defaultdict(list)
    for record in timeline.records:
        records_by_checkpoint[record.checkpoint_id].append(record)

    predictions: list[dict[str, Any]] = []
    for checkpoint in timeline.checkpoints:
        records = records_by_checkpoint[checkpoint.checkpoint_id]
        processed = {
            record.case_id: dict(active_pipeline.process(record)) for record in records
        }
        briefing = _validate_briefing_output(
            active_pipeline.brief(
                checkpoint,
                [record.case_id for record in records],
            ),
            checkpoint,
            records,
        )
        for record in records:
            output = processed[record.case_id]
            predictions.append(
                _prediction_from_replay(
                    output,
                    record,
                    briefing,
                )
            )

    aggregate_coverage = _aggregate_coverage(timeline.checkpoints)
    shadow_run = shadow_run_from_dict(
        {
            "schema_version": 1,
            "fixture_id": timeline.fixture_id,
            "policy_version": timeline.policy_version,
            "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
            "briefing_count": len(timeline.checkpoints),
            "coverage": aggregate_coverage,
            "external_write_count": timeline.audit.external_write_count,
            "scope_violation_count": timeline.audit.scope_violation_count,
            "privacy_violation_count": timeline.audit.privacy_violation_count,
            "predictions": predictions,
            "relation_predictions": _default_relation_predictions(reviewed_labels),
            "meeting_claim_predictions": _default_meeting_predictions(reviewed_labels),
            "usage": timeline.usage.as_dict(),
        }
    )
    evaluation = evaluate_shadow_run(policy, reviewed_labels, shadow_run)
    report = _build_report(
        policy,
        timeline,
        reviewed_labels,
        reviews,
        evaluation,
        generated_at=str(shadow_run.generated_at.isoformat()),
        input_digests=input_digests,
    )
    return RetrospectiveReplayReport(report=report)


def write_replay_report(
    report: RetrospectiveReplayReport,
    path: str | Path,
) -> Path:
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.is_symlink() or (output.exists() and not output.is_file()):
        raise RetrospectiveReplayError("report path must be a regular file")
    payload = json.dumps(report.as_dict(), indent=2, sort_keys=True) + "\n"
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
        output.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def _build_report(
    policy: OperationsPolicy,
    timeline: ReplayTimeline,
    labels: OperationalEvalFixture,
    reviews: HumanReviewBundle | None,
    evaluation: ShadowEvaluationReport,
    *,
    generated_at: str,
    input_digests: Mapping[str, str] | None,
) -> dict[str, Any]:
    metrics = dict(evaluation.metrics)
    coverage = _coverage_summary(timeline.checkpoints)
    review_counts = Counter(
        review.decision for review in (reviews.reviews if reviews else ())
    )
    return {
        "schema_version": RETROSPECTIVE_REPLAY_SCHEMA_VERSION,
        "report_type": "operational_retrospective_shadow",
        "fixture_id": timeline.fixture_id,
        "classification": timeline.classification,
        "policy_version": policy.policy_version,
        "generated_at": generated_at,
        "input_digests": dict(sorted((input_digests or {}).items())),
        "versions": dict(sorted(timeline.versions.items())),
        "replay": {
            "chronological_days": labels.chronological_days,
            "checkpoint_count": len(timeline.checkpoints),
            "record_count": len(timeline.records),
            "human_review_count": sum(review_counts.values()),
            "human_review_decisions": dict(sorted(review_counts.items())),
        },
        "detection": {
            "item_precision": metrics["item_precision"],
            "item_recall": metrics["item_recall"],
            "gmail_item_precision": metrics["gmail_item_precision"],
            "gmail_item_recall": metrics["gmail_item_recall"],
            "critical_high_recall": metrics["critical_high_recall"],
        },
        "reconciliation": {
            "duplicate_active_rate": metrics["duplicate_active_rate"],
            "stale_active_rate": metrics["stale_active_rate"],
            "resolved_item_resurrection_rate": metrics[
                "resolved_item_resurrection_rate"
            ],
            "false_merge_rate": metrics["false_merge_rate"],
            "missed_link_rate": metrics["missed_link_rate"],
        },
        "handled_state": {
            key: value
            for key, value in metrics.items()
            if key.startswith("handled_") or key == "false_handled_rate"
        },
        "briefing": {
            "critical_high_recall": metrics["critical_high_recall"],
            "focus_urgent_recall": metrics["focus_urgent_recall"],
            "false_alarms_per_briefing": metrics["false_alarms_per_briefing"],
            "awareness_padding_count": metrics["awareness_padding_count"],
            "evidence_route_validity": metrics["evidence_route_validity"],
        },
        "source_coverage": coverage,
        "cost": {
            **timeline.usage.as_dict(),
            "budget_within_policy": metrics["budget_within_policy"],
        },
        "gate_status": {
            "hard_stop": evaluation.hard_stop,
            "promotion_passed": evaluation.promotion_passed,
            "blocked_sources": list(evaluation.blocked_sources),
            "failed_gates": list(evaluation.failed_gates),
            "violations": [
                {
                    "code": violation.code,
                    "source": violation.source,
                    "case_id": violation.case_id,
                    "message": violation.message,
                }
                for violation in evaluation.violations
            ],
        },
        "metrics": metrics,
    }


def _validate_replay_binding(
    policy: OperationsPolicy,
    timeline: ReplayTimeline,
    labels: OperationalEvalFixture,
) -> None:
    if timeline.fixture_id != labels.fixture_id:
        raise RetrospectiveReplayError("timeline and labels use different fixture IDs")
    if timeline.classification != labels.classification:
        raise RetrospectiveReplayError(
            "timeline and labels use different classifications"
        )
    if timeline.policy_version != labels.policy_version:
        raise RetrospectiveReplayError(
            "timeline and labels use different policy versions"
        )
    if timeline.policy_version != policy.policy_version:
        raise RetrospectiveReplayError(
            "timeline policy_version does not match the active policy"
        )
    labels_by_id = {case.case_id: case for case in labels.cases}
    records_by_id = {record.case_id: record for record in timeline.records}
    checkpoints_by_id = {
        checkpoint.checkpoint_id: checkpoint for checkpoint in timeline.checkpoints
    }
    if set(labels_by_id) != set(records_by_id):
        raise RetrospectiveReplayError(
            "timeline records must cover exactly the labeled cases"
        )
    for case_id, label in labels_by_id.items():
        record = records_by_id[case_id]
        if label.observed_at != record.observed_at:
            raise RetrospectiveReplayError(
                f"record/label timestamp mismatch for {case_id}"
            )
        if label.source != record.source or label.source_class != record.source_class:
            raise RetrospectiveReplayError(
                f"record/label source mismatch for {case_id}"
            )
        checkpoint = checkpoints_by_id[record.checkpoint_id]
        if label.coverage != checkpoint.coverage[record.source]:
            raise RetrospectiveReplayError(
                f"record/label coverage mismatch for {case_id}"
            )


def _prediction_from_replay(
    output: Mapping[str, Any],
    record: NormalizedReplayRecord,
    briefing: Mapping[str, Any],
) -> dict[str, Any]:
    required = {
        "case_id",
        "item_detected",
        "item_kind",
        "lifecycle_state",
        "handled_verdict",
        "owner",
        "responsibility",
        "due_at",
        "evidence_ids",
        "sensitivity",
        "handled_basis",
        "authoritative_state",
        "local_route_rendered",
        "local_route_valid",
        "provider_route_rendered",
        "provider_route_valid",
        "duplicate_active",
        "stale_active",
        "resurrected",
        "source_identity_correct",
        "calendar_change_applied",
    }
    missing = sorted(required.difference(output))
    if missing:
        raise RetrospectiveReplayError(
            f"pipeline output for {record.case_id} is missing: {', '.join(missing)}"
        )
    return {
        "case_id": record.case_id,
        "item_detected": output["item_detected"],
        "item_kind": output["item_kind"],
        "lifecycle_state": output["lifecycle_state"],
        "handled_verdict": output["handled_verdict"],
        "owner": output["owner"],
        "responsibility": output["responsibility"],
        "due_at": output["due_at"],
        "evidence_ids": output["evidence_ids"],
        "sensitivity": output["sensitivity"],
        "focus_placement": briefing["placements"][record.case_id],
        "reported_coverage": briefing["coverage"][record.source],
        "reported_all_clear": briefing["reported_all_clear"],
        "handled_basis": output["handled_basis"],
        "authoritative_state": output["authoritative_state"],
        "local_route_rendered": output["local_route_rendered"],
        "local_route_valid": output["local_route_valid"],
        "provider_route_rendered": output["provider_route_rendered"],
        "provider_route_valid": output["provider_route_valid"],
        "duplicate_active": output["duplicate_active"],
        "stale_active": output["stale_active"],
        "resurrected": output["resurrected"],
        "source_identity_correct": output["source_identity_correct"],
        "calendar_change_applied": output["calendar_change_applied"],
    }


def _validate_briefing_output(
    raw: Mapping[str, Any],
    checkpoint: ReplayCheckpoint,
    records: Sequence[NormalizedReplayRecord],
) -> dict[str, Any]:
    data = _mapping(raw, "replay briefing output")
    _exact_keys(
        data,
        {"placements", "coverage", "reported_all_clear"},
        "replay briefing output",
    )
    placements = _mapping(data.get("placements"), "briefing placements")
    expected = {record.case_id for record in records}
    if set(placements) != expected:
        raise RetrospectiveReplayError(
            f"briefing {checkpoint.checkpoint_id} must place every checkpoint case exactly once"
        )
    normalized_placements = {
        case_id: _raw_choice(value, FOCUS_PLACEMENTS, f"placement.{case_id}")
        for case_id, value in placements.items()
    }
    coverage = _mapping(data.get("coverage"), "briefing coverage")
    if set(coverage) != set(checkpoint.coverage):
        raise RetrospectiveReplayError(
            f"briefing {checkpoint.checkpoint_id} coverage keys drifted"
        )
    normalized_coverage = {
        source: _raw_choice(value, COVERAGE_STATES, f"coverage.{source}")
        for source, value in coverage.items()
    }
    return {
        "placements": normalized_placements,
        "coverage": normalized_coverage,
        "reported_all_clear": _raw_boolean(
            data.get("reported_all_clear"), "reported_all_clear"
        ),
    }


def _default_relation_predictions(
    labels: OperationalEvalFixture,
) -> list[dict[str, Any]]:
    return [
        {
            "relation_id": relation.relation_id,
            "linked": False,
            "relation_type": "none",
            "status": "none",
        }
        for relation in labels.relations
    ]


def _default_meeting_predictions(
    labels: OperationalEvalFixture,
) -> list[dict[str, Any]]:
    return [
        {
            "claim_id": claim.claim_id,
            "included": False,
            "presented_as_fact": False,
            "evidence_ids": [],
        }
        for claim in labels.meeting_claims
    ]


def _parse_checkpoint(value: Any) -> ReplayCheckpoint:
    data = _mapping(value, "replay checkpoint")
    _exact_keys(data, {"checkpoint_id", "as_of", "coverage"}, "replay checkpoint")
    coverage_data = _mapping(data.get("coverage"), "checkpoint coverage")
    if not coverage_data or not set(coverage_data).issubset({"calendar", "gmail"}):
        raise RetrospectiveReplayError(
            "checkpoint coverage must contain only calendar and/or gmail"
        )
    return ReplayCheckpoint(
        checkpoint_id=_string(data, "checkpoint_id", maximum=120),
        as_of=_timestamp(data, "as_of"),
        coverage={
            source: _raw_choice(status, COVERAGE_STATES, f"coverage.{source}")
            for source, status in coverage_data.items()
        },
    )


def _parse_record(value: Any) -> NormalizedReplayRecord:
    data = _mapping(value, "normalized replay record")
    _exact_keys(
        data,
        {
            "case_id",
            "checkpoint_id",
            "observed_at",
            "source",
            "source_class",
            "normalized",
            "recorded_projection",
        },
        "normalized replay record",
    )
    normalized = _mapping(data.get("normalized"), "normalized source record")
    if not normalized:
        raise RetrospectiveReplayError("normalized source record cannot be empty")
    _reject_forbidden_normalized_fields(normalized)
    _bounded_json(normalized, "normalized source record", MAX_NORMALIZED_RECORD_BYTES)
    projection = _mapping(data.get("recorded_projection"), "recorded projection")
    case_id = _string(data, "case_id", maximum=120)
    _validate_projection(projection, case_id)
    source = _choice(data, "source", frozenset({"calendar", "gmail"}))
    source_class = _choice(
        data,
        "source_class",
        frozenset({"calendar", "human", "bulk", "transactional", "marketing"}),
    )
    if source == "calendar" and source_class != "calendar":
        raise RetrospectiveReplayError("Calendar records require source_class=calendar")
    if source == "gmail" and source_class == "calendar":
        raise RetrospectiveReplayError("Gmail records cannot use source_class=calendar")
    return NormalizedReplayRecord(
        case_id=case_id,
        checkpoint_id=_string(data, "checkpoint_id", maximum=120),
        observed_at=_timestamp(data, "observed_at"),
        source=source,
        source_class=source_class,
        normalized=normalized,
        recorded_projection=projection,
    )


def _validate_projection(value: Mapping[str, Any], case_id: str) -> None:
    data = _mapping(value, f"recorded projection {case_id}")
    _exact_keys(data, set(_PROJECTION_FIELDS), f"recorded projection {case_id}")
    _required_raw_string(data.get("canonical_key"), "canonical_key", 512)
    _required_raw_string(data.get("source_revision"), "source_revision", 1024)
    _raw_integer(data.get("source_order"), "source_order", minimum=0)
    _raw_boolean(data.get("reconciliation_applied"), "reconciliation_applied")
    _raw_integer(data.get("active_instances"), "active_instances", minimum=0)
    detected = _raw_boolean(data.get("item_detected"), "item_detected")
    kind = _raw_choice(data.get("item_kind"), ITEM_KINDS, "item_kind")
    state = _raw_choice(
        data.get("lifecycle_state"), LIFECYCLE_STATES, "lifecycle_state"
    )
    if detected and (kind == "none" or state == "none"):
        raise RetrospectiveReplayError(
            f"recorded projection {case_id} detected item lacks kind/state"
        )
    if not detected and (kind != "none" or state != "none"):
        raise RetrospectiveReplayError(
            f"recorded projection {case_id} negative item must use none kind/state"
        )
    _raw_choice(data.get("handled_verdict"), HANDLED_VERDICTS, "handled_verdict")
    _raw_choice(data.get("owner"), OWNERS, "owner")
    _raw_choice(data.get("responsibility"), RESPONSIBILITIES, "responsibility")
    _optional_raw_timestamp(data.get("due_at"), "due_at")
    _raw_string_list(data.get("evidence_ids"), "evidence_ids")
    _raw_choice(data.get("sensitivity"), SENSITIVITIES, "sensitivity")
    _raw_choice(data.get("handled_basis"), HANDLED_BASES, "handled_basis")
    _raw_choice(
        data.get("authoritative_state"), AUTHORITATIVE_STATES, "authoritative_state"
    )
    for field in (
        "local_route_rendered",
        "local_route_valid",
        "provider_route_rendered",
        "provider_route_valid",
        "source_identity_correct",
        "calendar_change_applied",
    ):
        _raw_boolean(data.get(field), field)
    _raw_choice(data.get("priority"), PRIORITIES, "priority")
    confidence = data.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise RetrospectiveReplayError("confidence must be between 0 and 1")


def _parse_usage(value: Any) -> ReplayUsage:
    data = _mapping(value, "replay usage")
    fields = {
        "calendar_requests",
        "gmail_api_requests",
        "gmail_detector_calls",
        "gmail_detector_input_tokens",
        "gmail_detector_total_tokens",
        "deferred_count",
        "deferred_disclosed",
    }
    _exact_keys(data, fields, "replay usage")
    return ReplayUsage(
        calendar_requests=_integer(data, "calendar_requests", minimum=0),
        gmail_api_requests=_integer(data, "gmail_api_requests", minimum=0),
        gmail_detector_calls=_integer(data, "gmail_detector_calls", minimum=0),
        gmail_detector_input_tokens=_integer(
            data, "gmail_detector_input_tokens", minimum=0
        ),
        gmail_detector_total_tokens=_integer(
            data, "gmail_detector_total_tokens", minimum=0
        ),
        deferred_count=_integer(data, "deferred_count", minimum=0),
        deferred_disclosed=_boolean(data, "deferred_disclosed"),
    )


def _parse_audit(value: Any) -> ReplayAudit:
    data = _mapping(value, "replay audit")
    fields = {
        "external_write_count",
        "scope_violation_count",
        "privacy_violation_count",
    }
    _exact_keys(data, fields, "replay audit")
    return ReplayAudit(
        external_write_count=_integer(data, "external_write_count", minimum=0),
        scope_violation_count=_integer(data, "scope_violation_count", minimum=0),
        privacy_violation_count=_integer(data, "privacy_violation_count", minimum=0),
    )


def _parse_versions(value: Any) -> dict[str, str]:
    data = _mapping(value, "replay versions")
    if len(data) > MAX_VERSIONS:
        raise RetrospectiveReplayError(
            f"replay versions cannot exceed {MAX_VERSIONS} entries"
        )
    result: dict[str, str] = {}
    for key, item in data.items():
        _required_raw_string(key, "version key", 80)
        result[key] = _required_raw_string(item, f"version {key}", 200)
    return result


def _parse_review(value: Any) -> HumanReview:
    data = _mapping(value, "human review")
    _exact_keys(
        data,
        {"case_id", "reviewed_at", "decision", "reason_code", "overrides"},
        "human review",
    )
    decision = _choice(data, "decision", REVIEW_DECISIONS)
    overrides = _mapping(data.get("overrides"), "human review overrides")
    unknown = sorted(set(overrides).difference(_REVIEW_OVERRIDE_FIELDS))
    if unknown:
        raise RetrospectiveReplayError(
            "unknown human review override(s): " + ", ".join(unknown)
        )
    if decision in {"confirm", "dismiss"} and overrides:
        raise RetrospectiveReplayError(
            f"{decision} reviews cannot contain label overrides"
        )
    if decision in {"correct", "missing"} and not overrides:
        raise RetrospectiveReplayError(f"{decision} reviews require label overrides")
    return HumanReview(
        case_id=_string(data, "case_id", maximum=120),
        reviewed_at=_timestamp(data, "reviewed_at"),
        decision=decision,
        reason_code=_string(data, "reason_code", maximum=120),
        overrides=overrides,
    )


def _fixture_as_dict(fixture: OperationalEvalFixture) -> dict[str, Any]:
    return {
        "schema_version": fixture.schema_version,
        "fixture_id": fixture.fixture_id,
        "classification": fixture.classification,
        "policy_version": fixture.policy_version,
        "held_out": fixture.held_out,
        "release_candidate": fixture.release_candidate,
        "window_start": fixture.window_start.isoformat(),
        "window_end": fixture.window_end.isoformat(),
        "cases": [
            {
                **case.__dict__,
                "observed_at": case.observed_at.isoformat(),
                "due_at": case.due_at.isoformat() if case.due_at else None,
                "expected_evidence_ids": list(case.expected_evidence_ids),
            }
            for case in fixture.cases
        ],
        "relations": [dict(relation.__dict__) for relation in fixture.relations],
        "meeting_claims": [
            {
                **claim.__dict__,
                "required_evidence_ids": list(claim.required_evidence_ids),
            }
            for claim in fixture.meeting_claims
        ],
    }


def _aggregate_coverage(
    checkpoints: Sequence[ReplayCheckpoint],
) -> dict[str, str]:
    by_source: dict[str, list[str]] = defaultdict(list)
    for checkpoint in checkpoints:
        for source, status in checkpoint.coverage.items():
            by_source[source].append(status)
    return {
        source: max(statuses, key=lambda status: _COVERAGE_ORDER[status])
        for source, statuses in by_source.items()
    }


def _coverage_summary(
    checkpoints: Sequence[ReplayCheckpoint],
) -> dict[str, dict[str, float | int]]:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for checkpoint in checkpoints:
        for source, status in checkpoint.coverage.items():
            counts[source][status] += 1
    result: dict[str, dict[str, float | int]] = {}
    for source, source_counts in sorted(counts.items()):
        total = sum(source_counts.values())
        result[source] = {
            "checkpoints": total,
            "complete": source_counts["complete"],
            "partial": source_counts["partial"],
            "unavailable": source_counts["unavailable"],
            "complete_rate": source_counts["complete"] / total if total else 0.0,
        }
    return result


def _briefing_rank(item: Mapping[str, Any]) -> tuple[int, int, str]:
    due_at = item.get("due_at")
    if due_at:
        due_rank = -int(_parse_timestamp(str(due_at)).timestamp())
    else:
        due_rank = -9_999_999_999
    return (
        _PRIORITY_ORDER[str(item["priority"])],
        due_rank,
        str(item["case_id"]),
    )


def _reject_forbidden_normalized_fields(value: Any, path: str = "normalized") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _FORBIDDEN_NORMALIZED_KEYS:
                raise RetrospectiveReplayError(
                    f"{path}.{key} is raw, credential, or attachment-byte material"
                )
            _reject_forbidden_normalized_fields(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_forbidden_normalized_fields(item, f"{path}[{index}]")


def _bounded_json(value: Any, label: str, maximum: int) -> None:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise RetrospectiveReplayError(f"{label} must be JSON serializable") from exc
    if len(encoded) > maximum:
        raise RetrospectiveReplayError(f"{label} exceeds {maximum} bytes")


def _load_yaml_file(path: Path, label: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise RetrospectiveReplayError(f"{label} must be a regular, non-symlink file")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RetrospectiveReplayError(f"invalid {label} YAML: {exc}") from exc


def _require_owner_only(path: Path, label: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise RetrospectiveReplayError(f"{label} must be owner-only (chmod 600)")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RetrospectiveReplayError(f"{label} must be a mapping")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise RetrospectiveReplayError(f"{label} keys must be strings")
        result[key] = item
    return result


def _exact_keys(data: Mapping[str, Any], expected: set[str], label: str) -> None:
    unknown = sorted(set(data).difference(expected))
    missing = sorted(expected.difference(data))
    if unknown:
        raise RetrospectiveReplayError(
            f"unknown {label} field(s): {', '.join(unknown)}"
        )
    if missing:
        raise RetrospectiveReplayError(
            f"missing {label} field(s): {', '.join(missing)}"
        )


def _sequence(value: Any, label: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list):
        raise RetrospectiveReplayError(f"{label} must be a list")
    if nonempty and not value:
        raise RetrospectiveReplayError(f"{label} cannot be empty")
    return value


def _string(data: Mapping[str, Any], key: str, *, maximum: int) -> str:
    return _required_raw_string(data.get(key), key, maximum)


def _required_raw_string(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RetrospectiveReplayError(f"{label} must be a non-empty string")
    result = value.strip()
    if len(result) > maximum:
        raise RetrospectiveReplayError(f"{label} exceeds {maximum} characters")
    return result


def _integer(data: Mapping[str, Any], key: str, *, minimum: int) -> int:
    return _raw_integer(data.get(key), key, minimum=minimum)


def _raw_integer(value: Any, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RetrospectiveReplayError(f"{label} must be an integer")
    if value < minimum:
        raise RetrospectiveReplayError(f"{label} must be at least {minimum}")
    return value


def _boolean(data: Mapping[str, Any], key: str) -> bool:
    return _raw_boolean(data.get(key), key)


def _raw_boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise RetrospectiveReplayError(f"{label} must be true or false")
    return value


def _choice(data: Mapping[str, Any], key: str, choices: frozenset[str]) -> str:
    return _raw_choice(data.get(key), choices, key)


def _raw_choice(value: Any, choices: frozenset[str], label: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise RetrospectiveReplayError(
            f"{label} must be one of: {', '.join(sorted(choices))}"
        )
    return value


def _timestamp(data: Mapping[str, Any], key: str) -> datetime:
    return _parse_timestamp(_required_raw_string(data.get(key), key, 100))


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RetrospectiveReplayError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RetrospectiveReplayError("timestamp must include a UTC offset")
    return parsed


def _optional_raw_timestamp(value: Any, label: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RetrospectiveReplayError(f"{label} must be null or ISO-8601")
    return _parse_timestamp(value)


def _raw_string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise RetrospectiveReplayError(f"{label} must be a list")
    result: list[str] = []
    for item in value:
        normalized = _required_raw_string(item, f"{label} entry", 512)
        if normalized in result:
            raise RetrospectiveReplayError(f"{label} contains duplicate: {normalized}")
        result.append(normalized)
    return result


def _require_unique(values: Sequence[str], label: str) -> None:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise RetrospectiveReplayError(f"duplicate {label}: {', '.join(duplicates)}")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pkm_brain.operational_replay",
        description="Replay and score private/synthetic operational shadow fixtures.",
    )
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--timeline", required=True, type=Path)
    parser.add_argument("--labels", required=True, type=Path)
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument(
        "--require-promotion",
        action="store_true",
        help="Return non-zero when a release-candidate fixture misses a gate.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        policy = load_operations_policy(args.policy)
        timeline = load_replay_timeline(args.timeline)
        labels = load_operational_eval_fixture(args.labels)
        reviews = (
            load_human_reviews(
                args.reviews,
                private=timeline.classification == "private",
            )
            if args.reviews
            else None
        )
        digests = {
            "policy": _file_sha256(args.policy),
            "timeline": _file_sha256(args.timeline),
            "labels": _file_sha256(args.labels),
        }
        if args.reviews:
            digests["reviews"] = _file_sha256(args.reviews)
        report = run_retrospective_replay(
            policy,
            timeline,
            labels,
            reviews=reviews,
            input_digests=digests,
        )
        output = write_replay_report(report, args.report)
    except (OSError, ValueError) as exc:
        print(f"operational replay failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "report": str(output),
                "hard_stop": report.hard_stop,
                "promotion_passed": report.promotion_passed,
            },
            sort_keys=True,
        )
    )
    if report.hard_stop:
        return 2
    if args.require_promotion and not report.promotion_passed:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
