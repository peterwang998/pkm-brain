from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
import stat
from typing import Any, Mapping, Sequence

import yaml

from .operations_policy import OperationsPolicy


OPERATIONAL_EVAL_SCHEMA_VERSION = 1
SOURCES = frozenset({"calendar", "gmail"})
SOURCE_CLASSES = frozenset({"calendar", "human", "bulk", "transactional", "marketing"})
ITEM_KINDS = frozenset(
    {"none", "event", "commitment", "waiting", "follow_up", "deadline", "attention"}
)
LIFECYCLE_STATES = frozenset(
    {"none", "active", "resolved", "dismissed", "cancelled", "expired"}
)
HANDLED_VERDICTS = frozenset(
    {
        "not_applicable",
        "needs_action",
        "responded_waiting",
        "being_handled",
        "fulfilled",
        "unknown",
    }
)
PRIORITIES = frozenset({"critical", "high", "normal", "low", "awareness"})
OWNERS = frozenset({"operator", "other", "shared", "unknown"})
RESPONSIBILITIES = frozenset({"owned", "shared", "adjacent", "out_of_area", "unknown"})
SENSITIVITIES = frozenset({"normal", "sensitive", "restricted"})
DAY_VOLUMES = frozenset({"low", "median", "high"})
FOCUS_PLACEMENTS = frozenset(
    {
        "focus",
        "urgent_overflow",
        "full_section",
        "awareness",
        "low_confidence",
        "suppressed",
    }
)
COVERAGE_STATES = frozenset({"complete", "partial", "unavailable"})
AUTHORITATIVE_STATES = frozenset({"current", "stale", "unavailable", "not_applicable"})
CALENDAR_CHANGES = frozenset(
    {"none", "ordinary", "recurrence", "reschedule", "cancellation"}
)
HANDLED_BASES = frozenset(
    {
        "not_applicable",
        "direct_evidence",
        "provider_state",
        "reply_only",
        "view_only",
        "notification_only",
        "insufficient",
    }
)
RELATION_EXPECTATIONS = frozenset({"linked", "separate", "ambiguous"})
RELATION_TYPES = frozenset(
    {
        "none",
        "same_episode",
        "duplicate_of",
        "responds_to",
        "fulfills",
        "delegates",
        "supersedes",
    }
)
RELATION_STATUSES = frozenset(
    {"none", "proposed", "confirmed", "rejected", "retracted"}
)
ITEM_TRUTH_FIELDS = frozenset(
    {
        "case_id",
        "observed_at",
        "source",
        "source_class",
        "day_volume",
        "sampled_as_suppressed",
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
RELATION_TRUTH_FIELDS = frozenset(
    {"relation_id", "left_case_id", "right_case_id", "expectation", "relation_type"}
)
MEETING_TRUTH_FIELDS = frozenset(
    {"claim_id", "supported", "required_evidence_ids", "stale", "wrong_person"}
)


OPERATIONAL_EVAL_FIXTURE_SCHEMA_V1: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "pkm-brain://schemas/operational-eval-fixture-v1",
    "title": "PKM Brain operational shadow evaluation fixture",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "fixture_id",
        "classification",
        "policy_version",
        "held_out",
        "release_candidate",
        "window_start",
        "window_end",
        "cases",
        "relations",
        "meeting_claims",
    ],
    "properties": {
        "schema_version": {"const": OPERATIONAL_EVAL_SCHEMA_VERSION},
        "fixture_id": {"type": "string", "minLength": 1, "maxLength": 120},
        "classification": {"enum": ["private", "synthetic"]},
        "policy_version": {"type": "integer", "minimum": 1},
        "held_out": {"type": "boolean"},
        "release_candidate": {"type": "boolean"},
        "window_start": {"type": "string", "format": "date-time"},
        "window_end": {"type": "string", "format": "date-time"},
        "cases": {
            "type": "array",
            "minItems": 1,
            "items": {"$ref": "#/$defs/item_truth"},
        },
        "relations": {
            "type": "array",
            "items": {"$ref": "#/$defs/relation_truth"},
        },
        "meeting_claims": {
            "type": "array",
            "items": {"$ref": "#/$defs/meeting_truth"},
        },
    },
    "$defs": {
        "item_truth": {
            "type": "object",
            "additionalProperties": False,
            "required": sorted(ITEM_TRUTH_FIELDS),
            "properties": {
                "case_id": {"type": "string", "minLength": 1, "maxLength": 120},
                "observed_at": {"type": "string", "format": "date-time"},
                "source": {"enum": sorted(SOURCES)},
                "source_class": {"enum": sorted(SOURCE_CLASSES)},
                "day_volume": {"enum": sorted(DAY_VOLUMES)},
                "sampled_as_suppressed": {"type": "boolean"},
                "item_expected": {"type": "boolean"},
                "item_kind": {"enum": sorted(ITEM_KINDS)},
                "lifecycle_state": {"enum": sorted(LIFECYCLE_STATES)},
                "handled_verdict": {"enum": sorted(HANDLED_VERDICTS)},
                "priority": {"enum": sorted(PRIORITIES)},
                "high_consequence": {"type": "boolean"},
                "human_confirmed": {"type": "boolean"},
                "owner": {"enum": sorted(OWNERS)},
                "responsibility": {"enum": sorted(RESPONSIBILITIES)},
                "due_at": {
                    "anyOf": [
                        {"type": "string", "format": "date-time"},
                        {"type": "null"},
                    ]
                },
                "expected_evidence_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "uniqueItems": True,
                },
                "sensitivity": {"enum": sorted(SENSITIVITIES)},
                "focus_expectation": {"enum": sorted(FOCUS_PLACEMENTS)},
                "coverage": {"enum": sorted(COVERAGE_STATES)},
                "authoritative_object_required": {"type": "boolean"},
                "authoritative_state": {"enum": sorted(AUTHORITATIVE_STATES)},
                "calendar_change": {"enum": sorted(CALENDAR_CHANGES)},
                "local_route_required": {"type": "boolean"},
                "provider_route_required": {"type": "boolean"},
            },
        },
        "relation_truth": {
            "type": "object",
            "additionalProperties": False,
            "required": sorted(RELATION_TRUTH_FIELDS),
            "properties": {
                "relation_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 120,
                },
                "left_case_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 120,
                },
                "right_case_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 120,
                },
                "expectation": {"enum": sorted(RELATION_EXPECTATIONS)},
                "relation_type": {"enum": sorted(RELATION_TYPES)},
            },
        },
        "meeting_truth": {
            "type": "object",
            "additionalProperties": False,
            "required": sorted(MEETING_TRUTH_FIELDS),
            "properties": {
                "claim_id": {"type": "string", "minLength": 1, "maxLength": 120},
                "supported": {"type": "boolean"},
                "required_evidence_ids": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "uniqueItems": True,
                },
                "stale": {"type": "boolean"},
                "wrong_person": {"type": "boolean"},
            },
        },
    },
}


class OperationalEvaluationError(ValueError):
    """Raised when a fixture or shadow result violates the eval schema."""


@dataclass(frozen=True)
class ItemTruth:
    case_id: str
    observed_at: datetime
    source: str
    source_class: str
    day_volume: str
    sampled_as_suppressed: bool
    item_expected: bool
    item_kind: str
    lifecycle_state: str
    handled_verdict: str
    priority: str
    high_consequence: bool
    human_confirmed: bool
    owner: str
    responsibility: str
    due_at: datetime | None
    expected_evidence_ids: tuple[str, ...]
    sensitivity: str
    focus_expectation: str
    coverage: str
    authoritative_object_required: bool
    authoritative_state: str
    calendar_change: str
    local_route_required: bool
    provider_route_required: bool


@dataclass(frozen=True)
class RelationTruth:
    relation_id: str
    left_case_id: str
    right_case_id: str
    expectation: str
    relation_type: str


@dataclass(frozen=True)
class MeetingClaimTruth:
    claim_id: str
    supported: bool
    required_evidence_ids: tuple[str, ...]
    stale: bool
    wrong_person: bool


@dataclass(frozen=True)
class OperationalEvalFixture:
    schema_version: int
    fixture_id: str
    classification: str
    policy_version: int
    held_out: bool
    release_candidate: bool
    window_start: datetime
    window_end: datetime
    cases: tuple[ItemTruth, ...]
    relations: tuple[RelationTruth, ...]
    meeting_claims: tuple[MeetingClaimTruth, ...]

    @property
    def chronological_days(self) -> int:
        return (self.window_end.date() - self.window_start.date()).days + 1


@dataclass(frozen=True)
class ItemPrediction:
    case_id: str
    item_detected: bool
    item_kind: str
    lifecycle_state: str
    handled_verdict: str
    owner: str
    responsibility: str
    due_at: datetime | None
    evidence_ids: tuple[str, ...]
    sensitivity: str
    focus_placement: str
    reported_coverage: str
    reported_all_clear: bool
    handled_basis: str
    authoritative_state: str
    local_route_rendered: bool
    local_route_valid: bool
    provider_route_rendered: bool
    provider_route_valid: bool
    duplicate_active: bool
    stale_active: bool
    resurrected: bool
    source_identity_correct: bool
    calendar_change_applied: bool


@dataclass(frozen=True)
class RelationPrediction:
    relation_id: str
    linked: bool
    relation_type: str
    status: str


@dataclass(frozen=True)
class MeetingClaimPrediction:
    claim_id: str
    included: bool
    presented_as_fact: bool
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class ShadowUsage:
    calendar_requests: int
    gmail_api_requests: int
    gmail_detector_calls: int
    gmail_detector_input_tokens: int
    gmail_detector_total_tokens: int
    deferred_count: int
    deferred_disclosed: bool


@dataclass(frozen=True)
class ShadowRun:
    schema_version: int
    fixture_id: str
    policy_version: int
    generated_at: datetime
    briefing_count: int
    coverage: dict[str, str]
    external_write_count: int
    scope_violation_count: int
    privacy_violation_count: int
    predictions: tuple[ItemPrediction, ...]
    relation_predictions: tuple[RelationPrediction, ...]
    meeting_claim_predictions: tuple[MeetingClaimPrediction, ...]
    usage: ShadowUsage


@dataclass(frozen=True)
class HardStopViolation:
    code: str
    message: str
    source: str | None = None
    case_id: str | None = None


@dataclass(frozen=True)
class ShadowEvaluationReport:
    fixture_id: str
    hard_stop: bool
    promotion_passed: bool
    blocked_sources: tuple[str, ...]
    violations: tuple[HardStopViolation, ...]
    failed_gates: tuple[str, ...]
    metrics: dict[str, float | int | bool]

    def as_dict(self) -> dict[str, Any]:
        return {
            "fixture_id": self.fixture_id,
            "hard_stop": self.hard_stop,
            "promotion_passed": self.promotion_passed,
            "blocked_sources": list(self.blocked_sources),
            "violations": [asdict(violation) for violation in self.violations],
            "failed_gates": list(self.failed_gates),
            "metrics": dict(self.metrics),
        }


def load_operational_eval_fixture(
    path: str | Path,
    *,
    enforce_private_permissions: bool = True,
) -> OperationalEvalFixture:
    fixture_path = Path(path).expanduser()
    if fixture_path.is_symlink() or not fixture_path.is_file():
        raise OperationalEvaluationError(
            "operational eval fixture must be a regular, non-symlink file"
        )
    try:
        raw = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise OperationalEvaluationError(f"invalid eval fixture YAML: {exc}") from exc
    fixture = operational_eval_fixture_from_dict(raw)
    if fixture.classification == "private" and enforce_private_permissions:
        mode = stat.S_IMODE(fixture_path.stat().st_mode)
        if mode & 0o077:
            raise OperationalEvaluationError(
                "private operational eval fixtures must be owner-only (chmod 600)"
            )
    return fixture


def operational_eval_fixture_from_dict(raw: Any) -> OperationalEvalFixture:
    data = _mapping(raw, "fixture")
    _exact_keys(data, set(OPERATIONAL_EVAL_FIXTURE_SCHEMA_V1["required"]), "fixture")
    schema_version = _integer(data, "schema_version", minimum=1)
    if schema_version != OPERATIONAL_EVAL_SCHEMA_VERSION:
        raise OperationalEvaluationError(
            f"unsupported operational eval schema_version: {schema_version}"
        )
    fixture_id = _string(data, "fixture_id", maximum=120)
    classification = _choice(
        data, "classification", frozenset({"private", "synthetic"})
    )
    policy_version = _integer(data, "policy_version", minimum=1)
    held_out = _boolean(data, "held_out")
    release_candidate = _boolean(data, "release_candidate")
    window_start = _timestamp(data, "window_start")
    window_end = _timestamp(data, "window_end")
    if window_end < window_start:
        raise OperationalEvaluationError("fixture window_end precedes window_start")
    if release_candidate and not held_out:
        raise OperationalEvaluationError("release_candidate fixtures must be held_out")

    raw_cases = _sequence(data.get("cases"), "fixture.cases", nonempty=True)
    cases = tuple(_parse_item_truth(item) for item in raw_cases)
    case_ids = [case.case_id for case in cases]
    _require_unique(case_ids, "fixture case_id")
    if [case.observed_at for case in cases] != sorted(
        case.observed_at for case in cases
    ):
        raise OperationalEvaluationError("fixture cases must be chronological")
    for case in cases:
        if not window_start <= case.observed_at <= window_end:
            raise OperationalEvaluationError(
                f"case {case.case_id} falls outside the fixture window"
            )

    relations = tuple(
        _parse_relation_truth(item)
        for item in _sequence(data.get("relations"), "fixture.relations")
    )
    _require_unique([relation.relation_id for relation in relations], "relation_id")
    known_case_ids = set(case_ids)
    for relation in relations:
        if (
            relation.left_case_id not in known_case_ids
            or relation.right_case_id not in known_case_ids
        ):
            raise OperationalEvaluationError(
                f"relation {relation.relation_id} references an unknown case"
            )
        if relation.left_case_id == relation.right_case_id:
            raise OperationalEvaluationError(
                f"relation {relation.relation_id} must have distinct endpoints"
            )

    meeting_claims = tuple(
        _parse_meeting_truth(item)
        for item in _sequence(data.get("meeting_claims"), "fixture.meeting_claims")
    )
    _require_unique([claim.claim_id for claim in meeting_claims], "meeting claim_id")
    return OperationalEvalFixture(
        schema_version=schema_version,
        fixture_id=fixture_id,
        classification=classification,
        policy_version=policy_version,
        held_out=held_out,
        release_candidate=release_candidate,
        window_start=window_start,
        window_end=window_end,
        cases=cases,
        relations=relations,
        meeting_claims=meeting_claims,
    )


def shadow_run_from_dict(raw: Any) -> ShadowRun:
    data = _mapping(raw, "shadow run")
    _exact_keys(
        data,
        {
            "schema_version",
            "fixture_id",
            "policy_version",
            "generated_at",
            "briefing_count",
            "coverage",
            "external_write_count",
            "scope_violation_count",
            "privacy_violation_count",
            "predictions",
            "relation_predictions",
            "meeting_claim_predictions",
            "usage",
        },
        "shadow run",
    )
    schema_version = _integer(data, "schema_version", minimum=1)
    if schema_version != OPERATIONAL_EVAL_SCHEMA_VERSION:
        raise OperationalEvaluationError(
            f"unsupported shadow run schema_version: {schema_version}"
        )
    coverage_data = _mapping(data.get("coverage"), "shadow run.coverage")
    if not coverage_data or not set(coverage_data).issubset(SOURCES):
        raise OperationalEvaluationError(
            "shadow run.coverage must contain only calendar and/or gmail"
        )
    coverage = {
        source: _raw_choice(value, COVERAGE_STATES, f"coverage.{source}")
        for source, value in coverage_data.items()
    }
    predictions = tuple(
        _parse_item_prediction(item)
        for item in _sequence(
            data.get("predictions"), "shadow run.predictions", nonempty=True
        )
    )
    _require_unique([item.case_id for item in predictions], "prediction case_id")
    relation_predictions = tuple(
        _parse_relation_prediction(item)
        for item in _sequence(
            data.get("relation_predictions"), "shadow run.relation_predictions"
        )
    )
    _require_unique(
        [item.relation_id for item in relation_predictions],
        "relation prediction relation_id",
    )
    meeting_predictions = tuple(
        _parse_meeting_prediction(item)
        for item in _sequence(
            data.get("meeting_claim_predictions"),
            "shadow run.meeting_claim_predictions",
        )
    )
    _require_unique(
        [item.claim_id for item in meeting_predictions],
        "meeting prediction claim_id",
    )
    usage = _parse_usage(data.get("usage"))
    return ShadowRun(
        schema_version=schema_version,
        fixture_id=_string(data, "fixture_id", maximum=120),
        policy_version=_integer(data, "policy_version", minimum=1),
        generated_at=_timestamp(data, "generated_at"),
        briefing_count=_integer(data, "briefing_count", minimum=1),
        coverage=coverage,
        external_write_count=_integer(data, "external_write_count", minimum=0),
        scope_violation_count=_integer(data, "scope_violation_count", minimum=0),
        privacy_violation_count=_integer(data, "privacy_violation_count", minimum=0),
        predictions=predictions,
        relation_predictions=relation_predictions,
        meeting_claim_predictions=meeting_predictions,
        usage=usage,
    )


def evaluate_shadow_run(
    policy: OperationsPolicy,
    fixture: OperationalEvalFixture,
    run: ShadowRun,
) -> ShadowEvaluationReport:
    _validate_run_binding(policy, fixture, run)
    predictions = {prediction.case_id: prediction for prediction in run.predictions}
    relations = {
        prediction.relation_id: prediction for prediction in run.relation_predictions
    }
    meeting_predictions = {
        prediction.claim_id: prediction for prediction in run.meeting_claim_predictions
    }

    violations: list[HardStopViolation] = []
    blocked_sources: set[str] = set()
    if run.external_write_count:
        violations.append(
            HardStopViolation(
                code="external_mutation",
                message="shadow mode observed an external provider write",
            )
        )
        blocked_sources.update(run.coverage)
    if run.scope_violation_count:
        violations.append(
            HardStopViolation(
                code="scope_violation",
                message="connector access exceeded the approved read-only policy",
            )
        )
        blocked_sources.update(run.coverage)
    if run.privacy_violation_count:
        violations.append(
            HardStopViolation(
                code="privacy_violation",
                message="runtime retention/redaction/attachment behavior violated policy",
            )
        )
        blocked_sources.update(run.coverage)

    counts: dict[str, int] = {
        "expected": 0,
        "detected": 0,
        "true_positive": 0,
        "false_positive": 0,
        "critical_high_expected": 0,
        "critical_high_found": 0,
        "false_handled": 0,
        "high_consequence_false_handled": 0,
        "focus_urgent_expected": 0,
        "focus_urgent_found": 0,
        "awareness_padding": 0,
        "invalid_required_routes": 0,
        "required_routes": 0,
        "duplicate_active": 0,
        "stale_active": 0,
        "resurrected": 0,
        "calendar_recurring": 0,
        "calendar_recurring_correct": 0,
        "calendar_changes": 0,
        "calendar_changes_correct": 0,
        "silent_incomplete_coverage": 0,
        "dated_items": 0,
        "dated_items_correct": 0,
        "evidenced_items": 0,
        "evidenced_items_complete": 0,
        "owner_labels_correct": 0,
        "responsibility_labels_correct": 0,
        "sensitivity_labels_correct": 0,
        "authoritative_labels": 0,
        "authoritative_labels_correct": 0,
    }
    handled_truth_counts = {verdict: 0 for verdict in HANDLED_VERDICTS}
    handled_prediction_counts = {verdict: 0 for verdict in HANDLED_VERDICTS}
    handled_true_positive = {verdict: 0 for verdict in HANDLED_VERDICTS}
    source_class_counts = {
        source_class: {"expected": 0, "detected": 0, "true_positive": 0}
        for source_class in SOURCE_CLASSES
    }
    suppressive_verdicts = {"fulfilled", "being_handled"}
    active_action_verdicts = {"needs_action", "responded_waiting", "unknown"}

    for truth in fixture.cases:
        prediction = predictions[truth.case_id]
        if truth.item_expected:
            counts["expected"] += 1
        if prediction.item_detected:
            counts["detected"] += 1
        if truth.item_expected and prediction.item_detected:
            counts["true_positive"] += 1
        if not truth.item_expected and prediction.item_detected:
            counts["false_positive"] += 1
        class_counts = source_class_counts[truth.source_class]
        class_counts["expected"] += int(truth.item_expected)
        class_counts["detected"] += int(prediction.item_detected)
        class_counts["true_positive"] += int(
            truth.item_expected and prediction.item_detected
        )
        if truth.item_expected and truth.handled_verdict != "not_applicable":
            handled_truth_counts[truth.handled_verdict] += 1
            handled_prediction_counts[prediction.handled_verdict] += 1
            handled_true_positive[truth.handled_verdict] += int(
                prediction.handled_verdict == truth.handled_verdict
            )

        urgent_truth = (
            truth.item_expected
            and truth.lifecycle_state == "active"
            and truth.handled_verdict == "needs_action"
            and truth.priority in {"critical", "high"}
        )
        if urgent_truth:
            counts["critical_high_expected"] += 1
            counts["focus_urgent_expected"] += 1
            if prediction.item_detected:
                counts["critical_high_found"] += 1
            if prediction.focus_placement in {"focus", "urgent_overflow"}:
                counts["focus_urgent_found"] += 1
            else:
                _violate(
                    violations,
                    blocked_sources,
                    "hidden_urgent",
                    "unresolved critical/high-priority work is absent from focus and overflow",
                    truth,
                )

        false_handled = (
            truth.item_expected
            and truth.handled_verdict in active_action_verdicts
            and prediction.handled_verdict in suppressive_verdicts
        )
        if false_handled:
            counts["false_handled"] += 1
            if truth.high_consequence:
                counts["high_consequence_false_handled"] += 1
                _violate(
                    violations,
                    blocked_sources,
                    "high_consequence_false_handled",
                    "high-consequence work was incorrectly classified as handled",
                    truth,
                )

        if (
            prediction.handled_verdict in suppressive_verdicts
            and prediction.handled_basis
            in {"reply_only", "view_only", "notification_only", "insufficient"}
        ):
            _violate(
                violations,
                blocked_sources,
                "unsafe_handled_basis",
                "reply/view/notification/incomplete evidence suppressed required work",
                truth,
            )
        if (
            truth.authoritative_object_required
            and truth.authoritative_state in {"stale", "unavailable"}
            and prediction.handled_verdict in suppressive_verdicts
        ):
            _violate(
                violations,
                blocked_sources,
                "authoritative_state_unavailable",
                "handled state was asserted without fresh authoritative coverage",
                truth,
            )

        if truth.item_expected:
            counts["owner_labels_correct"] += int(prediction.owner == truth.owner)
            counts["responsibility_labels_correct"] += int(
                prediction.responsibility == truth.responsibility
            )
            counts["sensitivity_labels_correct"] += int(
                prediction.sensitivity == truth.sensitivity
            )
        if truth.authoritative_object_required:
            counts["authoritative_labels"] += 1
            counts["authoritative_labels_correct"] += int(
                prediction.authoritative_state == truth.authoritative_state
            )
        if truth.due_at is not None:
            counts["dated_items"] += 1
            counts["dated_items_correct"] += int(prediction.due_at == truth.due_at)
        if truth.expected_evidence_ids:
            counts["evidenced_items"] += 1
            evidence_complete = set(truth.expected_evidence_ids).issubset(
                prediction.evidence_ids
            )
            counts["evidenced_items_complete"] += int(evidence_complete)
            if truth.high_consequence and not evidence_complete:
                _violate(
                    violations,
                    blocked_sources,
                    "missing_high_consequence_evidence",
                    "a high-consequence item omitted required direct evidence",
                    truth,
                )

        if (
            truth.human_confirmed
            and truth.lifecycle_state == "active"
            and prediction.lifecycle_state
            in {"resolved", "dismissed", "cancelled", "expired"}
        ):
            _violate(
                violations,
                blocked_sources,
                "false_human_confirmed_closure",
                "a human-confirmed active item was closed without valid new evidence",
                truth,
            )
        if (
            truth.item_expected
            and truth.owner != "unknown"
            and prediction.owner != truth.owner
        ):
            _violate(
                violations,
                blocked_sources,
                "wrong_person",
                "predicted owner does not match the labeled owner",
                truth,
            )

        if (
            truth.focus_expectation in {"awareness", "suppressed"}
            and prediction.focus_placement == "focus"
        ):
            counts["awareness_padding"] += 1
            _violate(
                violations,
                blocked_sources,
                "awareness_padded_focus",
                "focus was padded with non-actionable awareness",
                truth,
            )

        if truth.coverage != "complete" and (
            prediction.reported_coverage == "complete" or prediction.reported_all_clear
        ):
            counts["silent_incomplete_coverage"] += 1
            _violate(
                violations,
                blocked_sources,
                "silent_incomplete_coverage",
                "partial/unavailable source coverage was presented as complete or all-clear",
                truth,
            )

        for required, rendered, valid in (
            (
                truth.local_route_required,
                prediction.local_route_rendered,
                prediction.local_route_valid,
            ),
            (
                truth.provider_route_required,
                prediction.provider_route_rendered,
                prediction.provider_route_valid,
            ),
        ):
            if required:
                counts["required_routes"] += 1
                if not rendered or not valid:
                    counts["invalid_required_routes"] += 1
                    _violate(
                        violations,
                        blocked_sources,
                        "invalid_evidence_route",
                        "a required evidence route is missing or invalid",
                        truth,
                    )

        counts["duplicate_active"] += int(prediction.duplicate_active)
        counts["stale_active"] += int(prediction.stale_active)
        counts["resurrected"] += int(prediction.resurrected)
        if truth.source == "calendar" and truth.calendar_change == "recurrence":
            counts["calendar_recurring"] += 1
            counts["calendar_recurring_correct"] += int(
                prediction.source_identity_correct
            )
            if not prediction.source_identity_correct:
                _violate(
                    violations,
                    blocked_sources,
                    "calendar_identity_mismatch",
                    "a recurring Calendar instance did not preserve deterministic identity",
                    truth,
                )
        if truth.source == "calendar" and truth.calendar_change in {
            "reschedule",
            "cancellation",
        }:
            counts["calendar_changes"] += 1
            counts["calendar_changes_correct"] += int(
                prediction.calendar_change_applied
            )
            if not prediction.calendar_change_applied:
                _violate(
                    violations,
                    blocked_sources,
                    "calendar_change_missed",
                    "a Calendar cancellation/reschedule was not applied deterministically",
                    truth,
                )
            if truth.high_consequence and not prediction.calendar_change_applied:
                _violate(
                    violations,
                    blocked_sources,
                    "missed_high_consequence_calendar_change",
                    "a high-consequence cancellation/reschedule was not applied",
                    truth,
                )

    false_merges = 0
    missed_links = 0
    labeled_relations = 0
    for truth in fixture.relations:
        prediction = relations[truth.relation_id]
        active_link = prediction.linked and prediction.status == "confirmed"
        if truth.expectation != "ambiguous":
            labeled_relations += 1
        if truth.expectation in {"separate", "ambiguous"} and active_link:
            false_merges += 1
            violations.append(
                HardStopViolation(
                    code="false_episode_merge",
                    message="an unconfirmed/negative episode relation was activated",
                    case_id=truth.relation_id,
                )
            )
            blocked_sources.update({"calendar", "gmail"})
        if truth.expectation == "linked" and not active_link:
            missed_links += 1

    unsupported_meeting_claims = 0
    meeting_facts = 0
    meeting_facts_with_evidence = 0
    for truth in fixture.meeting_claims:
        prediction = meeting_predictions[truth.claim_id]
        if not prediction.included or not prediction.presented_as_fact:
            continue
        meeting_facts += 1
        evidence_complete = set(truth.required_evidence_ids).issubset(
            prediction.evidence_ids
        )
        if evidence_complete:
            meeting_facts_with_evidence += 1
        if (
            not truth.supported
            or truth.stale
            or truth.wrong_person
            or not evidence_complete
        ):
            unsupported_meeting_claims += 1
            violations.append(
                HardStopViolation(
                    code="unsupported_meeting_claim",
                    message="meeting preparation emitted an unsupported, stale, wrong-person, or unevidenced fact",
                    case_id=truth.claim_id,
                )
            )

    budget_overages = _budget_overages(policy, run.usage)
    if budget_overages and not run.usage.deferred_disclosed:
        violations.append(
            HardStopViolation(
                code="silent_budget_overflow",
                message="daily source/detector budget overflow was not disclosed: "
                + ", ".join(budget_overages),
            )
        )
        for name in budget_overages:
            blocked_sources.add("calendar" if name.startswith("calendar") else "gmail")

    gmail_cases = [truth for truth in fixture.cases if truth.source == "gmail"]
    gmail_expected = sum(truth.item_expected for truth in gmail_cases)
    gmail_detected = sum(
        predictions[truth.case_id].item_detected for truth in gmail_cases
    )
    gmail_true_positive = sum(
        truth.item_expected and predictions[truth.case_id].item_detected
        for truth in gmail_cases
    )
    gmail_urgent = [
        truth
        for truth in gmail_cases
        if truth.item_expected
        and truth.lifecycle_state == "active"
        and truth.handled_verdict == "needs_action"
        and truth.priority in {"critical", "high"}
    ]
    gmail_urgent_detected = sum(
        predictions[truth.case_id].item_detected for truth in gmail_urgent
    )
    gmail_urgent_focused = sum(
        predictions[truth.case_id].focus_placement in {"focus", "urgent_overflow"}
        for truth in gmail_urgent
    )
    gmail_false_handled = sum(
        truth.item_expected
        and truth.handled_verdict in active_action_verdicts
        and predictions[truth.case_id].handled_verdict in suppressive_verdicts
        for truth in gmail_cases
    )

    metrics: dict[str, float | int | bool] = {
        "item_precision": _ratio(counts["true_positive"], counts["detected"]),
        "item_recall": _ratio(counts["true_positive"], counts["expected"]),
        "critical_high_recall": _ratio(
            counts["critical_high_found"], counts["critical_high_expected"]
        ),
        "gmail_item_precision": _ratio(gmail_true_positive, gmail_detected),
        "gmail_item_recall": _ratio(gmail_true_positive, gmail_expected),
        "gmail_critical_high_recall": _ratio(gmail_urgent_detected, len(gmail_urgent)),
        "gmail_focus_urgent_recall": _ratio(gmail_urgent_focused, len(gmail_urgent)),
        "gmail_false_handled_rate": _ratio(
            gmail_false_handled, gmail_expected, empty=0.0
        ),
        "false_alarms_per_briefing": counts["false_positive"] / run.briefing_count,
        "false_handled_rate": _ratio(
            counts["false_handled"], counts["expected"], empty=0.0
        ),
        "high_consequence_false_handled": counts["high_consequence_false_handled"],
        "focus_urgent_recall": _ratio(
            counts["focus_urgent_found"], counts["focus_urgent_expected"]
        ),
        "awareness_padding_count": counts["awareness_padding"],
        "evidence_route_validity": 1.0
        - _ratio(
            counts["invalid_required_routes"], counts["required_routes"], empty=0.0
        ),
        "duplicate_active_rate": _ratio(
            counts["duplicate_active"], counts["expected"], empty=0.0
        ),
        "stale_active_rate": _ratio(
            counts["stale_active"], counts["expected"], empty=0.0
        ),
        "resolved_item_resurrection_rate": _ratio(
            counts["resurrected"], counts["expected"], empty=0.0
        ),
        "calendar_recurring_identity_accuracy": _ratio(
            counts["calendar_recurring_correct"], counts["calendar_recurring"]
        ),
        "calendar_change_accuracy": _ratio(
            counts["calendar_changes_correct"], counts["calendar_changes"]
        ),
        "silent_incomplete_coverage": counts["silent_incomplete_coverage"],
        "source_date_accuracy": _ratio(
            counts["dated_items_correct"], counts["dated_items"]
        ),
        "item_evidence_coverage": _ratio(
            counts["evidenced_items_complete"], counts["evidenced_items"]
        ),
        "owner_accuracy": _ratio(counts["owner_labels_correct"], counts["expected"]),
        "responsibility_accuracy": _ratio(
            counts["responsibility_labels_correct"], counts["expected"]
        ),
        "sensitivity_accuracy": _ratio(
            counts["sensitivity_labels_correct"], counts["expected"]
        ),
        "authoritative_state_accuracy": _ratio(
            counts["authoritative_labels_correct"], counts["authoritative_labels"]
        ),
        "false_merge_rate": _ratio(false_merges, labeled_relations, empty=0.0),
        "missed_link_rate": _ratio(missed_links, labeled_relations, empty=0.0),
        "meeting_factual_evidence_coverage": _ratio(
            meeting_facts_with_evidence, meeting_facts
        ),
        "unsupported_meeting_claims": unsupported_meeting_claims,
        "budget_within_policy": not budget_overages,
        "chronological_days": fixture.chronological_days,
    }
    for verdict in sorted(HANDLED_VERDICTS.difference({"not_applicable"})):
        metrics[f"handled_{verdict}_precision"] = _ratio(
            handled_true_positive[verdict],
            handled_prediction_counts[verdict],
        )
        metrics[f"handled_{verdict}_recall"] = _ratio(
            handled_true_positive[verdict],
            handled_truth_counts[verdict],
        )
    for source_class in sorted(SOURCE_CLASSES):
        class_counts = source_class_counts[source_class]
        metrics[f"{source_class}_item_precision"] = _ratio(
            class_counts["true_positive"], class_counts["detected"]
        )
        metrics[f"{source_class}_item_recall"] = _ratio(
            class_counts["true_positive"], class_counts["expected"]
        )

    failed_gates = _promotion_failures(fixture, run, metrics, counts, budget_overages)
    promotion_passed = fixture.release_candidate and not violations and not failed_gates
    return ShadowEvaluationReport(
        fixture_id=fixture.fixture_id,
        hard_stop=bool(violations),
        promotion_passed=promotion_passed,
        blocked_sources=tuple(sorted(blocked_sources)),
        violations=tuple(violations),
        failed_gates=tuple(failed_gates),
        metrics=metrics,
    )


def _promotion_failures(
    fixture: OperationalEvalFixture,
    run: ShadowRun,
    metrics: Mapping[str, float | int | bool],
    counts: Mapping[str, int],
    budget_overages: Sequence[str],
) -> list[str]:
    if not fixture.release_candidate:
        return ["fixture is shadow-only, not a release candidate"]
    failures: list[str] = []
    sources = {case.source for case in fixture.cases}
    if "calendar" in sources:
        if counts["calendar_recurring"] == 0:
            failures.append("Calendar release set has no recurring-instance labels")
        if counts["calendar_changes"] == 0:
            failures.append(
                "Calendar release set has no cancellation/reschedule labels"
            )
        if metrics["calendar_recurring_identity_accuracy"] != 1.0:
            failures.append(
                "Calendar recurring-instance identity accuracy is below 1.0"
            )
        if metrics["calendar_change_accuracy"] != 1.0:
            failures.append("Calendar cancellation/reschedule accuracy is below 1.0")
        if counts["duplicate_active"]:
            failures.append("Calendar replay created duplicate active items")
        if counts["silent_incomplete_coverage"]:
            failures.append("Calendar has silent freshness/coverage failure")
    if "gmail" in sources:
        if fixture.chronological_days < 30:
            failures.append("Gmail release set covers fewer than 30 chronological days")
        if not any(
            case.source == "gmail"
            and case.item_expected
            and case.priority in {"critical", "high"}
            for case in fixture.cases
        ):
            failures.append("Gmail release set has no critical/high-priority label")
        if float(metrics["gmail_critical_high_recall"]) < 0.95:
            failures.append("Gmail critical/high-priority recall is below 0.95")
        if float(metrics["gmail_item_precision"]) < 0.80:
            failures.append("Gmail item precision is below 0.80")
        if float(metrics["false_merge_rate"]) > 0.01:
            failures.append("Gmail false-merge rate exceeds 0.01")
        if int(metrics["high_consequence_false_handled"]) != 0:
            failures.append("Gmail has a high-consequence false-handled result")
        if float(metrics["gmail_false_handled_rate"]) > 0.01:
            failures.append("Gmail false-handled rate exceeds 0.01")
        if float(metrics["gmail_focus_urgent_recall"]) != 1.0:
            failures.append(
                "Not all unresolved critical/high items are in focus/overflow"
            )
        if int(metrics["awareness_padding_count"]) != 0:
            failures.append("Focus contains awareness padding")
        if int(metrics["silent_incomplete_coverage"]) != 0:
            failures.append("Incomplete coverage was presented as handled/all-clear")
        if float(metrics["evidence_route_validity"]) != 1.0:
            failures.append("Evidence route validity is below 1.0")
        if float(metrics["duplicate_active_rate"]) > 0.05:
            failures.append("Duplicate-active-item rate exceeds 0.05")
        if float(metrics["stale_active_rate"]) > 0.05:
            failures.append("Stale-active-item rate exceeds 0.05")
        if float(metrics["resolved_item_resurrection_rate"]) > 0.01:
            failures.append("Resolved-item resurrection rate exceeds 0.01")
        for metric, label in (
            ("source_date_accuracy", "source-date accuracy"),
            ("item_evidence_coverage", "item evidence coverage"),
            ("owner_accuracy", "owner accuracy"),
            ("responsibility_accuracy", "responsibility accuracy"),
            ("sensitivity_accuracy", "sensitivity accuracy"),
            ("authoritative_state_accuracy", "authoritative-state accuracy"),
        ):
            if float(metrics[metric]) != 1.0:
                failures.append(f"Gmail {label} is below 1.0")
        suppressed_classes = {
            case.source_class
            for case in fixture.cases
            if case.source == "gmail" and case.sampled_as_suppressed
        }
        missing_classes = {"human", "bulk", "transactional", "marketing"}.difference(
            suppressed_classes
        )
        if missing_classes:
            failures.append(
                "Suppressed Gmail labels are missing classes: "
                + ", ".join(sorted(missing_classes))
            )
        volume_classes = {
            case.day_volume for case in fixture.cases if case.source == "gmail"
        }
        missing_volumes = DAY_VOLUMES.difference(volume_classes)
        if missing_volumes:
            failures.append(
                "Gmail labels are missing day-volume classes: "
                + ", ".join(sorted(missing_volumes))
            )
        handled_labels = {
            case.handled_verdict
            for case in fixture.cases
            if case.source == "gmail" and case.item_expected
        }
        required_handled = {
            "needs_action",
            "responded_waiting",
            "being_handled",
            "fulfilled",
            "unknown",
        }
        missing_handled = required_handled.difference(handled_labels)
        if missing_handled:
            failures.append(
                "Gmail labels are missing handled-state classes: "
                + ", ".join(sorted(missing_handled))
            )
        if budget_overages:
            failures.append(
                "Daily source/model usage exceeds the approved policy budget"
            )
    if fixture.meeting_claims:
        if int(metrics["unsupported_meeting_claims"]):
            failures.append("Meeting preparation contains unsupported factual claims")
        if float(metrics["meeting_factual_evidence_coverage"]) != 1.0:
            failures.append("Meeting factual evidence coverage is below 1.0")
    if run.external_write_count:
        failures.append("Shadow run performed an external write")
    if run.scope_violation_count:
        failures.append("Shadow run exceeded an approved connector scope")
    if run.privacy_violation_count:
        failures.append("Shadow run violated retention/redaction/attachment policy")
    return failures


def _validate_run_binding(
    policy: OperationsPolicy,
    fixture: OperationalEvalFixture,
    run: ShadowRun,
) -> None:
    if fixture.policy_version != policy.policy_version:
        raise OperationalEvaluationError(
            "fixture policy_version does not match the active operations policy"
        )
    if run.policy_version != fixture.policy_version:
        raise OperationalEvaluationError(
            "shadow run policy_version does not match the fixture"
        )
    if run.fixture_id != fixture.fixture_id:
        raise OperationalEvaluationError("shadow run fixture_id does not match")
    expected_cases = {case.case_id for case in fixture.cases}
    actual_cases = {prediction.case_id for prediction in run.predictions}
    if expected_cases != actual_cases:
        raise OperationalEvaluationError(
            "shadow predictions must cover exactly the labeled fixture cases"
        )
    expected_relations = {relation.relation_id for relation in fixture.relations}
    actual_relations = {
        prediction.relation_id for prediction in run.relation_predictions
    }
    if expected_relations != actual_relations:
        raise OperationalEvaluationError(
            "relation predictions must cover exactly the labeled relations"
        )
    expected_claims = {claim.claim_id for claim in fixture.meeting_claims}
    actual_claims = {
        prediction.claim_id for prediction in run.meeting_claim_predictions
    }
    if expected_claims != actual_claims:
        raise OperationalEvaluationError(
            "meeting predictions must cover exactly the labeled claims"
        )
    fixture_sources = {case.source for case in fixture.cases}
    if not fixture_sources.issubset(run.coverage):
        raise OperationalEvaluationError(
            "shadow run coverage is missing a fixture source"
        )


def _parse_item_truth(value: Any) -> ItemTruth:
    data = _mapping(value, "item truth")
    _exact_keys(data, set(ITEM_TRUTH_FIELDS), "item truth")
    source = _choice(data, "source", SOURCES)
    source_class = _choice(data, "source_class", SOURCE_CLASSES)
    if source == "calendar" and source_class != "calendar":
        raise OperationalEvaluationError("Calendar cases require source_class=calendar")
    if source == "gmail" and source_class == "calendar":
        raise OperationalEvaluationError("Gmail cases cannot use source_class=calendar")
    item_expected = _boolean(data, "item_expected")
    item_kind = _choice(data, "item_kind", ITEM_KINDS)
    lifecycle_state = _choice(data, "lifecycle_state", LIFECYCLE_STATES)
    handled_verdict = _choice(data, "handled_verdict", HANDLED_VERDICTS)
    if item_expected and (item_kind == "none" or lifecycle_state == "none"):
        raise OperationalEvaluationError(
            "expected items require item_kind and lifecycle_state labels"
        )
    if not item_expected and (item_kind != "none" or lifecycle_state != "none"):
        raise OperationalEvaluationError(
            "negative cases must label item_kind/lifecycle_state as none"
        )
    evidence_ids = _string_tuple(
        data.get("expected_evidence_ids"), "expected_evidence_ids"
    )
    if item_expected and not evidence_ids:
        raise OperationalEvaluationError(
            "expected items require at least one direct evidence ID"
        )
    authoritative_required = _boolean(data, "authoritative_object_required")
    authoritative_state = _choice(data, "authoritative_state", AUTHORITATIVE_STATES)
    if authoritative_required and authoritative_state == "not_applicable":
        raise OperationalEvaluationError(
            "authoritative-object-required labels need an authoritative state"
        )
    return ItemTruth(
        case_id=_string(data, "case_id", maximum=120),
        observed_at=_timestamp(data, "observed_at"),
        source=source,
        source_class=source_class,
        day_volume=_choice(data, "day_volume", DAY_VOLUMES),
        sampled_as_suppressed=_boolean(data, "sampled_as_suppressed"),
        item_expected=item_expected,
        item_kind=item_kind,
        lifecycle_state=lifecycle_state,
        handled_verdict=handled_verdict,
        priority=_choice(data, "priority", PRIORITIES),
        high_consequence=_boolean(data, "high_consequence"),
        human_confirmed=_boolean(data, "human_confirmed"),
        owner=_choice(data, "owner", OWNERS),
        responsibility=_choice(data, "responsibility", RESPONSIBILITIES),
        due_at=_optional_timestamp(data, "due_at"),
        expected_evidence_ids=evidence_ids,
        sensitivity=_choice(data, "sensitivity", SENSITIVITIES),
        focus_expectation=_choice(data, "focus_expectation", FOCUS_PLACEMENTS),
        coverage=_choice(data, "coverage", COVERAGE_STATES),
        authoritative_object_required=authoritative_required,
        authoritative_state=authoritative_state,
        calendar_change=_choice(data, "calendar_change", CALENDAR_CHANGES),
        local_route_required=_boolean(data, "local_route_required"),
        provider_route_required=_boolean(data, "provider_route_required"),
    )


def _parse_relation_truth(value: Any) -> RelationTruth:
    data = _mapping(value, "relation truth")
    _exact_keys(data, set(RELATION_TRUTH_FIELDS), "relation truth")
    expectation = _choice(data, "expectation", RELATION_EXPECTATIONS)
    relation_type = _choice(data, "relation_type", RELATION_TYPES)
    if expectation == "linked" and relation_type == "none":
        raise OperationalEvaluationError(
            "linked relation truth requires a relation_type"
        )
    if expectation == "separate" and relation_type != "none":
        raise OperationalEvaluationError(
            "separate relation truth requires relation_type=none"
        )
    return RelationTruth(
        relation_id=_string(data, "relation_id", maximum=120),
        left_case_id=_string(data, "left_case_id", maximum=120),
        right_case_id=_string(data, "right_case_id", maximum=120),
        expectation=expectation,
        relation_type=relation_type,
    )


def _parse_meeting_truth(value: Any) -> MeetingClaimTruth:
    data = _mapping(value, "meeting claim truth")
    _exact_keys(data, set(MEETING_TRUTH_FIELDS), "meeting claim truth")
    supported = _boolean(data, "supported")
    evidence = _string_tuple(data.get("required_evidence_ids"), "required_evidence_ids")
    if supported and not evidence:
        raise OperationalEvaluationError(
            "supported meeting claims require at least one evidence ID"
        )
    return MeetingClaimTruth(
        claim_id=_string(data, "claim_id", maximum=120),
        supported=supported,
        required_evidence_ids=evidence,
        stale=_boolean(data, "stale"),
        wrong_person=_boolean(data, "wrong_person"),
    )


def _parse_item_prediction(value: Any) -> ItemPrediction:
    data = _mapping(value, "item prediction")
    fields = {
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
        "focus_placement",
        "reported_coverage",
        "reported_all_clear",
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
    _exact_keys(data, fields, "item prediction")
    item_detected = _boolean(data, "item_detected")
    item_kind = _choice(data, "item_kind", ITEM_KINDS)
    lifecycle_state = _choice(data, "lifecycle_state", LIFECYCLE_STATES)
    if item_detected and (item_kind == "none" or lifecycle_state == "none"):
        raise OperationalEvaluationError(
            "detected predictions require item_kind and lifecycle_state"
        )
    if not item_detected and (item_kind != "none" or lifecycle_state != "none"):
        raise OperationalEvaluationError(
            "non-detected predictions require item_kind/lifecycle_state=none"
        )
    return ItemPrediction(
        case_id=_string(data, "case_id", maximum=120),
        item_detected=item_detected,
        item_kind=item_kind,
        lifecycle_state=lifecycle_state,
        handled_verdict=_choice(data, "handled_verdict", HANDLED_VERDICTS),
        owner=_choice(data, "owner", OWNERS),
        responsibility=_choice(data, "responsibility", RESPONSIBILITIES),
        due_at=_optional_timestamp(data, "due_at"),
        evidence_ids=_string_tuple(data.get("evidence_ids"), "evidence_ids"),
        sensitivity=_choice(data, "sensitivity", SENSITIVITIES),
        focus_placement=_choice(data, "focus_placement", FOCUS_PLACEMENTS),
        reported_coverage=_choice(data, "reported_coverage", COVERAGE_STATES),
        reported_all_clear=_boolean(data, "reported_all_clear"),
        handled_basis=_choice(data, "handled_basis", HANDLED_BASES),
        authoritative_state=_choice(data, "authoritative_state", AUTHORITATIVE_STATES),
        local_route_rendered=_boolean(data, "local_route_rendered"),
        local_route_valid=_boolean(data, "local_route_valid"),
        provider_route_rendered=_boolean(data, "provider_route_rendered"),
        provider_route_valid=_boolean(data, "provider_route_valid"),
        duplicate_active=_boolean(data, "duplicate_active"),
        stale_active=_boolean(data, "stale_active"),
        resurrected=_boolean(data, "resurrected"),
        source_identity_correct=_boolean(data, "source_identity_correct"),
        calendar_change_applied=_boolean(data, "calendar_change_applied"),
    )


def _parse_relation_prediction(value: Any) -> RelationPrediction:
    data = _mapping(value, "relation prediction")
    _exact_keys(
        data,
        {"relation_id", "linked", "relation_type", "status"},
        "relation prediction",
    )
    linked = _boolean(data, "linked")
    relation_type = _choice(data, "relation_type", RELATION_TYPES)
    status = _choice(data, "status", RELATION_STATUSES)
    if linked and relation_type == "none":
        raise OperationalEvaluationError("linked relation prediction requires a type")
    if not linked and status == "confirmed":
        raise OperationalEvaluationError("unlinked relation cannot be confirmed")
    return RelationPrediction(
        relation_id=_string(data, "relation_id", maximum=120),
        linked=linked,
        relation_type=relation_type,
        status=status,
    )


def _parse_meeting_prediction(value: Any) -> MeetingClaimPrediction:
    data = _mapping(value, "meeting claim prediction")
    _exact_keys(
        data,
        {"claim_id", "included", "presented_as_fact", "evidence_ids"},
        "meeting claim prediction",
    )
    return MeetingClaimPrediction(
        claim_id=_string(data, "claim_id", maximum=120),
        included=_boolean(data, "included"),
        presented_as_fact=_boolean(data, "presented_as_fact"),
        evidence_ids=_string_tuple(data.get("evidence_ids"), "evidence_ids"),
    )


def _parse_usage(value: Any) -> ShadowUsage:
    data = _mapping(value, "shadow usage")
    fields = {
        "calendar_requests",
        "gmail_api_requests",
        "gmail_detector_calls",
        "gmail_detector_input_tokens",
        "gmail_detector_total_tokens",
        "deferred_count",
        "deferred_disclosed",
    }
    _exact_keys(data, fields, "shadow usage")
    return ShadowUsage(
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


def _budget_overages(policy: OperationsPolicy, usage: ShadowUsage) -> list[str]:
    comparisons = (
        (
            "calendar.requests_per_day",
            usage.calendar_requests,
            policy.budgets.calendar.requests_per_day,
        ),
        (
            "gmail.api_requests_per_day",
            usage.gmail_api_requests,
            policy.budgets.gmail.api_requests_per_day,
        ),
        (
            "gmail.detector_calls_per_day",
            usage.gmail_detector_calls,
            policy.budgets.gmail.detector_calls_per_day,
        ),
        (
            "gmail.detector_input_tokens_per_day",
            usage.gmail_detector_input_tokens,
            policy.budgets.gmail.detector_input_tokens_per_day,
        ),
        (
            "gmail.detector_total_tokens_per_day",
            usage.gmail_detector_total_tokens,
            policy.budgets.gmail.detector_total_tokens_per_day,
        ),
    )
    return [name for name, actual, limit in comparisons if actual > limit]


def _violate(
    violations: list[HardStopViolation],
    blocked_sources: set[str],
    code: str,
    message: str,
    truth: ItemTruth,
) -> None:
    violations.append(
        HardStopViolation(
            code=code,
            message=message,
            source=truth.source,
            case_id=truth.case_id,
        )
    )
    blocked_sources.add(truth.source)


def _ratio(numerator: int, denominator: int, *, empty: float = 1.0) -> float:
    return numerator / denominator if denominator else empty


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OperationalEvaluationError(f"{label} must be a mapping")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise OperationalEvaluationError(f"{label} keys must be strings")
        result[key] = item
    return result


def _exact_keys(data: Mapping[str, Any], expected: set[str], label: str) -> None:
    unknown = sorted(set(data).difference(expected))
    missing = sorted(expected.difference(data))
    if unknown:
        raise OperationalEvaluationError(
            f"unknown {label} field(s): {', '.join(unknown)}"
        )
    if missing:
        raise OperationalEvaluationError(
            f"missing {label} field(s): {', '.join(missing)}"
        )


def _sequence(value: Any, label: str, *, nonempty: bool = False) -> list[Any]:
    if not isinstance(value, list):
        raise OperationalEvaluationError(f"{label} must be a list")
    if nonempty and not value:
        raise OperationalEvaluationError(f"{label} cannot be empty")
    return value


def _string(data: Mapping[str, Any], key: str, *, maximum: int) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OperationalEvaluationError(f"{key} must be a non-empty string")
    result = value.strip()
    if len(result) > maximum:
        raise OperationalEvaluationError(f"{key} exceeds {maximum} characters")
    return result


def _integer(data: Mapping[str, Any], key: str, *, minimum: int) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise OperationalEvaluationError(f"{key} must be an integer")
    if value < minimum:
        raise OperationalEvaluationError(f"{key} must be at least {minimum}")
    return value


def _boolean(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise OperationalEvaluationError(f"{key} must be true or false")
    return value


def _choice(data: Mapping[str, Any], key: str, choices: frozenset[str]) -> str:
    return _raw_choice(data.get(key), choices, key)


def _raw_choice(value: Any, choices: frozenset[str], label: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise OperationalEvaluationError(
            f"{label} must be one of: {', '.join(sorted(choices))}"
        )
    return value


def _timestamp(data: Mapping[str, Any], key: str) -> datetime:
    value = data.get(key)
    if not isinstance(value, str):
        raise OperationalEvaluationError(f"{key} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OperationalEvaluationError(
            f"{key} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OperationalEvaluationError(f"{key} must include a UTC offset")
    return parsed


def _optional_timestamp(data: Mapping[str, Any], key: str) -> datetime | None:
    if data.get(key) is None:
        return None
    return _timestamp(data, key)


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    items = _sequence(value, label)
    result: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise OperationalEvaluationError(
                f"{label} entries must be non-empty strings"
            )
        normalized = item.strip()
        if normalized in result:
            raise OperationalEvaluationError(
                f"{label} contains duplicate: {normalized}"
            )
        result.append(normalized)
    return tuple(result)


def _require_unique(values: Sequence[str], label: str) -> None:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise OperationalEvaluationError(f"duplicate {label}: {', '.join(duplicates)}")
