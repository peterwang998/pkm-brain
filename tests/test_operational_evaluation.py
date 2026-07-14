from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pkm_brain.operational_evaluation import (
    OPERATIONAL_EVAL_FIXTURE_SCHEMA_V1,
    OperationalEvaluationError,
    evaluate_shadow_run,
    load_operational_eval_fixture,
    operational_eval_fixture_from_dict,
    shadow_run_from_dict,
)
from pkm_brain.operations_policy import OperationsPolicy

from test_operations_policy import valid_policy_dict


def item_truth(
    case_id: str,
    observed_at: str,
    source: str,
    *,
    source_class: str | None = None,
    day_volume: str = "median",
    expected: bool = True,
    item_kind: str | None = None,
    lifecycle_state: str | None = None,
    handled_verdict: str | None = None,
    priority: str = "normal",
    high_consequence: bool = False,
    human_confirmed: bool = False,
    owner: str | None = None,
    due_at: str | None = None,
    expected_evidence_ids: list[str] | None = None,
    sensitivity: str = "normal",
    focus_expectation: str = "full_section",
    coverage: str = "complete",
    authoritative_object_required: bool = False,
    authoritative_state: str = "not_applicable",
    calendar_change: str = "none",
    sampled_as_suppressed: bool = False,
    routes_required: bool = True,
) -> dict:
    return {
        "case_id": case_id,
        "observed_at": observed_at,
        "source": source,
        "source_class": source_class
        or ("calendar" if source == "calendar" else "human"),
        "day_volume": day_volume,
        "sampled_as_suppressed": sampled_as_suppressed,
        "item_expected": expected,
        "item_kind": item_kind
        or (
            "event"
            if source == "calendar" and expected
            else "commitment"
            if expected
            else "none"
        ),
        "lifecycle_state": lifecycle_state or ("active" if expected else "none"),
        "handled_verdict": handled_verdict
        or (
            "not_applicable" if source == "calendar" or not expected else "needs_action"
        ),
        "priority": priority if expected else "awareness",
        "high_consequence": high_consequence,
        "human_confirmed": human_confirmed,
        "owner": owner
        or (
            "shared"
            if source == "calendar" and expected
            else "operator"
            if expected
            else "unknown"
        ),
        "responsibility": "owned" if expected else "out_of_area",
        "due_at": due_at,
        "expected_evidence_ids": expected_evidence_ids
        if expected_evidence_ids is not None
        else ([f"evidence:{source}:{case_id}"] if expected else []),
        "sensitivity": sensitivity,
        "focus_expectation": focus_expectation if expected else "suppressed",
        "coverage": coverage,
        "authoritative_object_required": authoritative_object_required,
        "authoritative_state": authoritative_state,
        "calendar_change": calendar_change,
        "local_route_required": routes_required and expected,
        "provider_route_required": routes_required and expected,
    }


def item_prediction(
    truth: dict,
    *,
    detected: bool | None = None,
    focus_placement: str | None = None,
) -> dict:
    detected = truth["item_expected"] if detected is None else detected
    return {
        "case_id": truth["case_id"],
        "item_detected": detected,
        "item_kind": truth["item_kind"] if detected else "none",
        "lifecycle_state": truth["lifecycle_state"] if detected else "none",
        "handled_verdict": truth["handled_verdict"] if detected else "not_applicable",
        "owner": truth["owner"] if detected else "unknown",
        "responsibility": truth["responsibility"] if detected else "unknown",
        "due_at": truth["due_at"] if detected else None,
        "evidence_ids": truth["expected_evidence_ids"] if detected else [],
        "sensitivity": truth["sensitivity"],
        "focus_placement": focus_placement or truth["focus_expectation"],
        "reported_coverage": truth["coverage"],
        "reported_all_clear": False,
        "handled_basis": "direct_evidence" if detected else "not_applicable",
        "authoritative_state": truth["authoritative_state"],
        "local_route_rendered": truth["local_route_required"],
        "local_route_valid": truth["local_route_required"],
        "provider_route_rendered": truth["provider_route_required"],
        "provider_route_valid": truth["provider_route_required"],
        "duplicate_active": False,
        "stale_active": False,
        "resurrected": False,
        "source_identity_correct": True,
        "calendar_change_applied": True,
    }


def fixture_dict(*, release_candidate: bool = False) -> dict:
    calendar = item_truth(
        "cal-recurring",
        "2026-06-01T09:00:00-07:00",
        "calendar",
        calendar_change="recurrence",
    )
    gmail = item_truth(
        "gmail-commitment",
        "2026-06-02T10:00:00-07:00",
        "gmail",
        priority="high",
        high_consequence=True,
        human_confirmed=True,
        due_at="2026-06-05T17:00:00-07:00",
        focus_expectation="focus",
        authoritative_object_required=True,
        authoritative_state="current",
        sampled_as_suppressed=True,
    )
    return {
        "schema_version": 1,
        "fixture_id": "synthetic-cos-shadow-v1",
        "classification": "synthetic",
        "policy_version": 3,
        "held_out": release_candidate,
        "release_candidate": release_candidate,
        "window_start": "2026-06-01T00:00:00-07:00",
        "window_end": "2026-06-30T23:59:59-07:00",
        "cases": [calendar, gmail],
        "relations": [
            {
                "relation_id": "not-same-episode",
                "left_case_id": "cal-recurring",
                "right_case_id": "gmail-commitment",
                "expectation": "separate",
                "relation_type": "none",
            }
        ],
        "meeting_claims": [
            {
                "claim_id": "meeting-time",
                "supported": True,
                "required_evidence_ids": ["evidence:calendar:1"],
                "stale": False,
                "wrong_person": False,
            }
        ],
    }


def run_dict(fixture: dict) -> dict:
    return {
        "schema_version": 1,
        "fixture_id": fixture["fixture_id"],
        "policy_version": fixture["policy_version"],
        "generated_at": "2026-07-01T08:00:00-07:00",
        "briefing_count": 2,
        "coverage": {"calendar": "complete", "gmail": "complete"},
        "external_write_count": 0,
        "scope_violation_count": 0,
        "privacy_violation_count": 0,
        "predictions": [item_prediction(item) for item in fixture["cases"]],
        "relation_predictions": [
            {
                "relation_id": relation["relation_id"],
                "linked": relation["expectation"] == "linked",
                "relation_type": relation["relation_type"],
                "status": "confirmed"
                if relation["expectation"] == "linked"
                else "none",
            }
            for relation in fixture["relations"]
        ],
        "meeting_claim_predictions": [
            {
                "claim_id": claim["claim_id"],
                "included": True,
                "presented_as_fact": True,
                "evidence_ids": claim["required_evidence_ids"],
            }
            for claim in fixture["meeting_claims"]
        ],
        "usage": {
            "calendar_requests": 20,
            "gmail_api_requests": 100,
            "gmail_detector_calls": 10,
            "gmail_detector_input_tokens": 25_000,
            "gmail_detector_total_tokens": 40_000,
            "deferred_count": 0,
            "deferred_disclosed": True,
        },
    }


def policy() -> OperationsPolicy:
    return OperationsPolicy.from_dict(valid_policy_dict())


def violation_codes(report) -> set[str]:
    return {violation.code for violation in report.violations}


def test_schema_and_loader_keep_lifecycle_handled_and_focus_truth_separate() -> None:
    raw = fixture_dict()

    fixture = operational_eval_fixture_from_dict(raw)

    gmail = fixture.cases[1]
    assert OPERATIONAL_EVAL_FIXTURE_SCHEMA_V1["properties"]["schema_version"] == {
        "const": 1
    }
    assert gmail.lifecycle_state == "active"
    assert gmail.handled_verdict == "needs_action"
    assert gmail.focus_expectation == "focus"
    assert gmail.sampled_as_suppressed is True
    assert fixture.chronological_days == 30


def test_safe_shadow_run_scores_without_claiming_release() -> None:
    raw_fixture = fixture_dict()
    fixture = operational_eval_fixture_from_dict(raw_fixture)
    run = shadow_run_from_dict(run_dict(raw_fixture))

    report = evaluate_shadow_run(policy(), fixture, run)

    assert report.hard_stop is False
    assert report.promotion_passed is False
    assert report.failed_gates == ("fixture is shadow-only, not a release candidate",)
    assert report.metrics["item_precision"] == 1.0
    assert report.metrics["critical_high_recall"] == 1.0
    assert report.metrics["evidence_route_validity"] == 1.0
    assert report.metrics["source_date_accuracy"] == 1.0
    assert report.metrics["item_evidence_coverage"] == 1.0
    assert report.metrics["responsibility_accuracy"] == 1.0


def test_fixture_is_strict_chronological_and_run_is_exactly_bound() -> None:
    raw = fixture_dict()
    raw["debug"] = True
    with pytest.raises(OperationalEvaluationError, match="unknown fixture field"):
        operational_eval_fixture_from_dict(raw)

    raw = fixture_dict()
    raw["cases"].reverse()
    with pytest.raises(OperationalEvaluationError, match="chronological"):
        operational_eval_fixture_from_dict(raw)

    raw = fixture_dict()
    fixture = operational_eval_fixture_from_dict(raw)
    run_raw = run_dict(raw)
    run_raw["predictions"].pop()
    run = shadow_run_from_dict(run_raw)
    with pytest.raises(OperationalEvaluationError, match="exactly"):
        evaluate_shadow_run(policy(), fixture, run)


def test_private_fixture_requires_owner_only_permissions(tmp_path: Path) -> None:
    raw = fixture_dict()
    raw["classification"] = "private"
    path = tmp_path / "private-eval.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(OperationalEvaluationError, match="chmod 600"):
        load_operational_eval_fixture(path)

    path.chmod(0o600)
    assert load_operational_eval_fixture(path).classification == "private"


def test_false_handled_hidden_urgent_and_unsafe_reply_basis_are_hard_stops() -> None:
    raw_fixture = fixture_dict()
    raw_run = run_dict(raw_fixture)
    prediction = raw_run["predictions"][1]
    prediction["handled_verdict"] = "fulfilled"
    prediction["handled_basis"] = "reply_only"
    prediction["focus_placement"] = "suppressed"

    report = evaluate_shadow_run(
        policy(),
        operational_eval_fixture_from_dict(raw_fixture),
        shadow_run_from_dict(raw_run),
    )

    assert {
        "high_consequence_false_handled",
        "unsafe_handled_basis",
        "hidden_urgent",
    }.issubset(violation_codes(report))
    assert report.blocked_sources == ("gmail",)


def test_coverage_route_owner_and_human_closure_fail_closed() -> None:
    raw_fixture = fixture_dict()
    gmail_truth = raw_fixture["cases"][1]
    gmail_truth["coverage"] = "partial"
    raw_run = run_dict(raw_fixture)
    prediction = raw_run["predictions"][1]
    prediction["reported_coverage"] = "complete"
    prediction["reported_all_clear"] = True
    prediction["provider_route_valid"] = False
    prediction["owner"] = "other"
    prediction["lifecycle_state"] = "resolved"
    prediction["evidence_ids"] = []

    report = evaluate_shadow_run(
        policy(),
        operational_eval_fixture_from_dict(raw_fixture),
        shadow_run_from_dict(raw_run),
    )

    assert {
        "silent_incomplete_coverage",
        "invalid_evidence_route",
        "wrong_person",
        "false_human_confirmed_closure",
        "missing_high_consequence_evidence",
    }.issubset(violation_codes(report))


def test_external_scope_and_silent_budget_overflow_are_hard_stops() -> None:
    raw_fixture = fixture_dict()
    raw_run = run_dict(raw_fixture)
    raw_run["external_write_count"] = 1
    raw_run["scope_violation_count"] = 1
    raw_run["privacy_violation_count"] = 1
    raw_run["usage"]["gmail_detector_total_tokens"] = 500_001
    raw_run["usage"]["deferred_disclosed"] = False

    report = evaluate_shadow_run(
        policy(),
        operational_eval_fixture_from_dict(raw_fixture),
        shadow_run_from_dict(raw_run),
    )

    assert {
        "external_mutation",
        "scope_violation",
        "privacy_violation",
        "silent_budget_overflow",
    }.issubset(violation_codes(report))
    assert report.blocked_sources == ("calendar", "gmail")


def test_false_episode_merge_and_unsupported_meeting_fact_are_hard_stops() -> None:
    raw_fixture = fixture_dict()
    raw_fixture["meeting_claims"][0]["supported"] = False
    raw_fixture["meeting_claims"][0]["required_evidence_ids"] = []
    raw_run = run_dict(raw_fixture)
    relation = raw_run["relation_predictions"][0]
    relation.update(
        {"linked": True, "relation_type": "same_episode", "status": "confirmed"}
    )
    raw_run["meeting_claim_predictions"][0]["evidence_ids"] = []

    report = evaluate_shadow_run(
        policy(),
        operational_eval_fixture_from_dict(raw_fixture),
        shadow_run_from_dict(raw_run),
    )

    assert "false_episode_merge" in violation_codes(report)
    assert "unsupported_meeting_claim" in violation_codes(report)


def test_calendar_identity_and_change_replay_failures_are_hard_stops() -> None:
    raw_fixture = fixture_dict()
    cancellation = item_truth(
        "cal-cancelled",
        "2026-06-02T09:30:00-07:00",
        "calendar",
        lifecycle_state="cancelled",
        calendar_change="cancellation",
        focus_expectation="suppressed",
    )
    raw_fixture["cases"].insert(1, cancellation)
    raw_fixture["relations"][0]["right_case_id"] = "cal-cancelled"
    raw_run = run_dict(raw_fixture)
    raw_run["predictions"][0]["source_identity_correct"] = False
    raw_run["predictions"][1]["calendar_change_applied"] = False

    report = evaluate_shadow_run(
        policy(),
        operational_eval_fixture_from_dict(raw_fixture),
        shadow_run_from_dict(raw_run),
    )

    assert {
        "calendar_identity_mismatch",
        "calendar_change_missed",
    }.issubset(violation_codes(report))


def release_fixture_dict() -> dict:
    recurring = item_truth(
        "cal-recurring",
        "2026-06-01T09:00:00-07:00",
        "calendar",
        calendar_change="recurrence",
    )
    cancellation = item_truth(
        "cal-cancelled",
        "2026-06-02T09:00:00-07:00",
        "calendar",
        lifecycle_state="cancelled",
        calendar_change="cancellation",
        focus_expectation="suppressed",
    )
    human = item_truth(
        "gmail-human",
        "2026-06-03T09:00:00-07:00",
        "gmail",
        source_class="human",
        priority="critical",
        high_consequence=True,
        focus_expectation="focus",
        authoritative_state="current",
        sampled_as_suppressed=True,
    )
    bulk = item_truth(
        "gmail-bulk",
        "2026-06-04T09:00:00-07:00",
        "gmail",
        source_class="bulk",
        day_volume="low",
        expected=False,
        sampled_as_suppressed=True,
        routes_required=False,
    )
    transactional = item_truth(
        "gmail-transactional",
        "2026-06-05T09:00:00-07:00",
        "gmail",
        source_class="transactional",
        item_kind="deadline",
        due_at="2026-06-15T17:00:00-07:00",
        focus_expectation="full_section",
        authoritative_state="current",
        sampled_as_suppressed=True,
    )
    marketing = item_truth(
        "gmail-marketing",
        "2026-06-06T09:00:00-07:00",
        "gmail",
        source_class="marketing",
        day_volume="high",
        expected=False,
        sampled_as_suppressed=True,
        routes_required=False,
    )
    responded_waiting = item_truth(
        "gmail-responded-waiting",
        "2026-06-07T09:00:00-07:00",
        "gmail",
        handled_verdict="responded_waiting",
        authoritative_state="current",
    )
    being_handled = item_truth(
        "gmail-being-handled",
        "2026-06-08T09:00:00-07:00",
        "gmail",
        handled_verdict="being_handled",
        authoritative_state="current",
    )
    fulfilled = item_truth(
        "gmail-fulfilled",
        "2026-06-09T09:00:00-07:00",
        "gmail",
        handled_verdict="fulfilled",
        focus_expectation="suppressed",
        authoritative_state="current",
    )
    unknown = item_truth(
        "gmail-unknown",
        "2026-06-10T09:00:00-07:00",
        "gmail",
        handled_verdict="unknown",
        focus_expectation="low_confidence",
        authoritative_object_required=True,
        authoritative_state="unavailable",
    )
    return {
        "schema_version": 1,
        "fixture_id": "private-release-v1",
        "classification": "private",
        "policy_version": 3,
        "held_out": True,
        "release_candidate": True,
        "window_start": "2026-06-01T00:00:00-07:00",
        "window_end": "2026-06-30T23:59:59-07:00",
        "cases": [
            recurring,
            cancellation,
            human,
            bulk,
            transactional,
            marketing,
            responded_waiting,
            being_handled,
            fulfilled,
            unknown,
        ],
        "relations": [],
        "meeting_claims": [
            {
                "claim_id": "supported-meeting-fact",
                "supported": True,
                "required_evidence_ids": ["evidence:calendar:release"],
                "stale": False,
                "wrong_person": False,
            }
        ],
    }


def test_complete_held_out_30_day_release_fixture_can_pass() -> None:
    raw_fixture = release_fixture_dict()
    raw_run = run_dict(raw_fixture)

    report = evaluate_shadow_run(
        policy(),
        operational_eval_fixture_from_dict(raw_fixture),
        shadow_run_from_dict(raw_run),
    )

    assert report.hard_stop is False
    assert report.failed_gates == ()
    assert report.promotion_passed is True
    assert report.metrics["chronological_days"] == 30


def test_release_gate_rejects_short_window_missing_suppressed_class_and_budget() -> (
    None
):
    raw_fixture = release_fixture_dict()
    raw_fixture["window_end"] = "2026-06-20T23:59:59-07:00"
    raw_fixture["cases"] = [
        item for item in raw_fixture["cases"] if item["source_class"] != "marketing"
    ]
    raw_run = run_dict(raw_fixture)
    raw_run["usage"]["gmail_detector_calls"] = 1_001

    report = evaluate_shadow_run(
        policy(),
        operational_eval_fixture_from_dict(raw_fixture),
        shadow_run_from_dict(raw_run),
    )

    assert report.promotion_passed is False
    assert any("fewer than 30" in gate for gate in report.failed_gates)
    assert any("marketing" in gate for gate in report.failed_gates)
    assert any("exceeds" in gate for gate in report.failed_gates)
