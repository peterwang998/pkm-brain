from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import stat

import pytest
import yaml

from pkm_brain.operational_evaluation import operational_eval_fixture_from_dict
from pkm_brain.operational_replay import (
    RecordedProjectionPipeline,
    RetrospectiveReplayError,
    apply_human_reviews,
    human_reviews_from_dict,
    load_replay_timeline,
    main,
    replay_timeline_from_dict,
    run_retrospective_replay,
    write_replay_report,
)
from pkm_brain.operations_policy import OperationsPolicy

from test_operations_policy import valid_policy_dict


def truth(
    case_id: str,
    observed_at: str,
    source: str,
    *,
    expected: bool = True,
    kind: str | None = None,
    state: str | None = None,
    handled: str | None = None,
    priority: str = "normal",
    focus: str = "full_section",
    source_class: str | None = None,
    coverage: str = "complete",
) -> dict:
    return {
        "case_id": case_id,
        "observed_at": observed_at,
        "source": source,
        "source_class": source_class
        or ("calendar" if source == "calendar" else "human"),
        "day_volume": "median",
        "sampled_as_suppressed": source == "gmail",
        "item_expected": expected,
        "item_kind": kind
        or (
            "event"
            if source == "calendar" and expected
            else "commitment"
            if expected
            else "none"
        ),
        "lifecycle_state": state or ("active" if expected else "none"),
        "handled_verdict": handled
        or (
            "not_applicable" if source == "calendar" or not expected else "needs_action"
        ),
        "priority": priority if expected else "awareness",
        "high_consequence": priority == "critical",
        "human_confirmed": False,
        "owner": "shared"
        if source == "calendar" and expected
        else "operator"
        if expected
        else "unknown",
        "responsibility": "owned" if expected else "out_of_area",
        "due_at": None,
        "expected_evidence_ids": [f"evidence:{case_id}"] if expected else [],
        "sensitivity": "normal",
        "focus_expectation": focus if expected else "suppressed",
        "coverage": coverage,
        "authoritative_object_required": False,
        "authoritative_state": "not_applicable",
        "calendar_change": "ordinary" if source == "calendar" else "none",
        "local_route_required": expected,
        "provider_route_required": expected,
    }


def projection(
    label: dict,
    *,
    canonical_key: str | None = None,
    source_order: int = 1,
    applied: bool = True,
    active_instances: int = 1,
) -> dict:
    detected = bool(label["item_expected"])
    return {
        "canonical_key": canonical_key or f"{label['source']}:{label['case_id']}",
        "source_revision": f"revision-{source_order}-{label['case_id']}",
        "source_order": source_order,
        "reconciliation_applied": applied,
        "active_instances": active_instances if detected else 0,
        "item_detected": detected,
        "item_kind": label["item_kind"] if detected else "none",
        "lifecycle_state": label["lifecycle_state"] if detected else "none",
        "handled_verdict": label["handled_verdict"] if detected else "not_applicable",
        "owner": label["owner"] if detected else "unknown",
        "responsibility": label["responsibility"] if detected else "unknown",
        "due_at": label["due_at"] if detected else None,
        "evidence_ids": label["expected_evidence_ids"] if detected else [],
        "sensitivity": label["sensitivity"],
        "handled_basis": "direct_evidence" if detected else "not_applicable",
        "authoritative_state": label["authoritative_state"],
        "local_route_rendered": bool(label["local_route_required"]),
        "local_route_valid": bool(label["local_route_required"]),
        "provider_route_rendered": bool(label["provider_route_required"]),
        "provider_route_valid": bool(label["provider_route_required"]),
        "source_identity_correct": True,
        "calendar_change_applied": True,
        "priority": label["priority"],
        "confidence": 1.0,
    }


def labels_dict(cases: list[dict], *, classification: str = "synthetic") -> dict:
    return {
        "schema_version": 1,
        "fixture_id": "retrospective-fixture-v1",
        "classification": classification,
        "policy_version": 3,
        "held_out": False,
        "release_candidate": False,
        "window_start": "2026-06-01T00:00:00-07:00",
        "window_end": "2026-06-30T23:59:59-07:00",
        "cases": cases,
        "relations": [],
        "meeting_claims": [],
    }


def timeline_dict(
    cases: list[dict],
    *,
    classification: str = "synthetic",
    checkpoint_status: str = "complete",
) -> dict:
    checkpoints: list[dict] = []
    records: list[dict] = []
    sources = sorted({case["source"] for case in cases})
    for index, case in enumerate(cases, start=1):
        checkpoint_id = f"briefing-{index}"
        observed = case["observed_at"]
        checkpoints.append(
            {
                "checkpoint_id": checkpoint_id,
                "as_of": observed,
                "coverage": {source: checkpoint_status for source in sources},
            }
        )
        normalized = (
            {
                "event_id": case["case_id"],
                "title": "Synthetic planning event",
                "status": "confirmed",
            }
            if case["source"] == "calendar"
            else {
                "thread_id": case["case_id"],
                "subject": "Synthetic request",
                "messages": [{"body": "Synthetic normalized message"}],
            }
        )
        records.append(
            {
                "case_id": case["case_id"],
                "checkpoint_id": checkpoint_id,
                "observed_at": observed,
                "source": case["source"],
                "source_class": case["source_class"],
                "normalized": normalized,
                "recorded_projection": projection(case, source_order=index),
            }
        )
    return {
        "schema_version": 1,
        "fixture_id": "retrospective-fixture-v1",
        "classification": classification,
        "policy_version": 3,
        "checkpoints": checkpoints,
        "records": records,
        "usage": {
            "calendar_requests": 10,
            "gmail_api_requests": 20,
            "gmail_detector_calls": 2,
            "gmail_detector_input_tokens": 2_000,
            "gmail_detector_total_tokens": 3_000,
            "deferred_count": 0,
            "deferred_disclosed": True,
        },
        "audit": {
            "external_write_count": 0,
            "scope_violation_count": 0,
            "privacy_violation_count": 0,
        },
        "versions": {
            "calendar_adapter": "synthetic-v1",
            "gmail_detector": "synthetic-v1",
            "briefing": "deterministic-v1",
        },
    }


def policy() -> OperationsPolicy:
    return OperationsPolicy.from_dict(valid_policy_dict())


def test_replay_emits_machine_report_without_copying_normalized_content(
    tmp_path: Path,
) -> None:
    calendar = truth(
        "calendar-event",
        "2026-06-01T09:00:00-07:00",
        "calendar",
    )
    gmail = truth(
        "gmail-request",
        "2026-06-02T09:00:00-07:00",
        "gmail",
        priority="critical",
        focus="focus",
    )
    raw_labels = labels_dict([calendar, gmail])
    raw_timeline = timeline_dict([calendar, gmail])

    report = run_retrospective_replay(
        policy(),
        replay_timeline_from_dict(raw_timeline),
        operational_eval_fixture_from_dict(raw_labels),
        generated_at="2026-07-13T12:00:00+00:00",
        input_digests={"timeline": "a" * 64},
    )

    payload = report.as_dict()
    assert payload["detection"]["item_precision"] == 1.0
    assert payload["detection"]["item_recall"] == 1.0
    assert payload["briefing"]["focus_urgent_recall"] == 1.0
    assert payload["briefing"]["false_alarms_per_briefing"] == 0.0
    assert payload["reconciliation"]["duplicate_active_rate"] == 0.0
    assert payload["source_coverage"]["calendar"]["complete_rate"] == 1.0
    assert payload["source_coverage"]["gmail"]["complete_rate"] == 1.0
    assert payload["gate_status"]["hard_stop"] is False
    serialized = json.dumps(payload)
    assert "Synthetic normalized message" not in serialized
    assert "Synthetic planning event" not in serialized

    output = write_replay_report(report, tmp_path / "private" / "report.json")
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(output.read_text())["fixture_id"] == raw_labels["fixture_id"]


def test_reconciliation_replay_reports_duplicate_stale_and_resurrection() -> None:
    resolved = truth(
        "thread-resolved",
        "2026-06-01T09:00:00-07:00",
        "gmail",
        state="resolved",
        handled="fulfilled",
        focus="suppressed",
    )
    reopened = truth(
        "thread-reopened",
        "2026-06-02T09:00:00-07:00",
        "gmail",
        state="active",
        handled="needs_action",
        focus="focus",
        priority="high",
    )
    stale = truth(
        "thread-stale",
        "2026-06-03T09:00:00-07:00",
        "gmail",
        state="active",
        handled="needs_action",
        focus="focus",
        priority="high",
    )
    cases = [resolved, reopened, stale]
    raw_timeline = timeline_dict(cases)
    for record, order, active_instances in zip(
        raw_timeline["records"],
        (2, 3, 1),
        (1, 2, 1),
    ):
        record["recorded_projection"] = projection(
            cases[raw_timeline["records"].index(record)],
            canonical_key="gmail:thread-shared:commitment",
            source_order=order,
            active_instances=active_instances,
        )

    report = run_retrospective_replay(
        policy(),
        replay_timeline_from_dict(raw_timeline),
        operational_eval_fixture_from_dict(labels_dict(cases)),
        generated_at="2026-07-13T12:00:00+00:00",
    ).as_dict()

    assert report["reconciliation"]["duplicate_active_rate"] == pytest.approx(1 / 3)
    assert report["reconciliation"]["stale_active_rate"] == pytest.approx(1 / 3)
    assert report["reconciliation"]["resolved_item_resurrection_rate"] == pytest.approx(
        1 / 3
    )


def test_human_review_overlay_changes_truth_without_changing_predictions() -> None:
    case = truth(
        "gmail-not-actionable",
        "2026-06-01T09:00:00-07:00",
        "gmail",
        priority="high",
        focus="focus",
    )
    raw_timeline = timeline_dict([case])
    raw_timeline["records"][0]["recorded_projection"] = projection(
        {
            **case,
            "item_expected": False,
            "item_kind": "none",
            "lifecycle_state": "none",
            "handled_verdict": "not_applicable",
            "owner": "unknown",
            "responsibility": "unknown",
            "expected_evidence_ids": [],
        }
    )
    fixture = operational_eval_fixture_from_dict(labels_dict([case]))
    reviews = human_reviews_from_dict(
        {
            "schema_version": 1,
            "fixture_id": fixture.fixture_id,
            "reviews": [
                {
                    "case_id": case["case_id"],
                    "reviewed_at": "2026-07-01T09:00:00-07:00",
                    "decision": "dismiss",
                    "reason_code": "informational-only",
                    "overrides": {},
                }
            ],
        }
    )

    reviewed = apply_human_reviews(fixture, reviews)
    assert reviewed.cases[0].item_expected is False
    report = run_retrospective_replay(
        policy(),
        replay_timeline_from_dict(raw_timeline),
        fixture,
        reviews=reviews,
        generated_at="2026-07-13T12:00:00+00:00",
    ).as_dict()
    assert report["detection"]["item_precision"] == 1.0
    assert report["detection"]["item_recall"] == 1.0
    assert report["replay"]["human_review_decisions"] == {"dismiss": 1}


def test_private_timeline_is_owner_only_and_rejects_raw_or_credentials(
    tmp_path: Path,
) -> None:
    case = truth(
        "gmail-private",
        "2026-06-01T09:00:00-07:00",
        "gmail",
    )
    raw = timeline_dict([case], classification="private")
    path = tmp_path / "timeline.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(RetrospectiveReplayError, match="chmod 600"):
        load_replay_timeline(path)

    path.chmod(0o600)
    assert load_replay_timeline(path).classification == "private"

    unsafe = deepcopy(raw)
    unsafe["records"][0]["normalized"]["refresh_token"] = "forbidden"
    with pytest.raises(RetrospectiveReplayError, match="credential"):
        replay_timeline_from_dict(unsafe)

    unsafe = deepcopy(raw)
    unsafe["records"][0]["normalized"]["raw"] = {"provider": "payload"}
    with pytest.raises(RetrospectiveReplayError, match="raw"):
        replay_timeline_from_dict(unsafe)


def test_timeline_rejects_nonchronological_and_label_binding_drift() -> None:
    first = truth(
        "gmail-first",
        "2026-06-01T09:00:00-07:00",
        "gmail",
    )
    second = truth(
        "gmail-second",
        "2026-06-02T09:00:00-07:00",
        "gmail",
    )
    raw = timeline_dict([first, second])
    raw["records"].reverse()
    with pytest.raises(RetrospectiveReplayError, match="ordered"):
        replay_timeline_from_dict(raw)

    timeline = replay_timeline_from_dict(timeline_dict([first]))
    labels = labels_dict([first])
    labels["cases"][0]["observed_at"] = "2026-06-01T10:00:00-07:00"
    with pytest.raises(RetrospectiveReplayError, match="timestamp mismatch"):
        run_retrospective_replay(
            policy(),
            timeline,
            operational_eval_fixture_from_dict(labels),
        )


def test_hard_stop_and_coverage_are_preserved_in_report() -> None:
    case = truth(
        "gmail-critical",
        "2026-06-01T09:00:00-07:00",
        "gmail",
        priority="critical",
        focus="focus",
        coverage="partial",
    )
    raw = timeline_dict([case], checkpoint_status="partial")
    raw["audit"]["privacy_violation_count"] = 1
    bad = raw["records"][0]["recorded_projection"]
    bad["handled_verdict"] = "fulfilled"
    bad["handled_basis"] = "reply_only"

    report = run_retrospective_replay(
        policy(),
        replay_timeline_from_dict(raw),
        operational_eval_fixture_from_dict(labels_dict([case])),
        generated_at="2026-07-13T12:00:00+00:00",
    ).as_dict()

    codes = {item["code"] for item in report["gate_status"]["violations"]}
    assert report["gate_status"]["hard_stop"] is True
    assert "privacy_violation" in codes
    assert "high_consequence_false_handled" in codes
    assert "unsafe_handled_basis" in codes
    assert report["source_coverage"]["gmail"]["partial"] == 1


class CountingPipeline(RecordedProjectionPipeline):
    def __init__(self) -> None:
        super().__init__()
        self.processed: list[str] = []
        self.briefed: list[str] = []

    def process(self, record):
        self.processed.append(record.case_id)
        return super().process(record)

    def brief(self, checkpoint, case_ids):
        self.briefed.append(checkpoint.checkpoint_id)
        return super().brief(checkpoint, case_ids)


def test_injected_pipeline_is_called_chronologically() -> None:
    first = truth(
        "calendar-first",
        "2026-06-01T09:00:00-07:00",
        "calendar",
    )
    second = truth(
        "gmail-second",
        "2026-06-02T09:00:00-07:00",
        "gmail",
        priority="high",
        focus="focus",
    )
    pipeline = CountingPipeline()

    run_retrospective_replay(
        policy(),
        replay_timeline_from_dict(timeline_dict([first, second])),
        operational_eval_fixture_from_dict(labels_dict([first, second])),
        pipeline=pipeline,
        generated_at="2026-07-13T12:00:00+00:00",
    )

    assert pipeline.processed == ["calendar-first", "gmail-second"]
    assert pipeline.briefed == ["briefing-1", "briefing-2"]


def test_module_cli_writes_report_without_live_runner(tmp_path: Path) -> None:
    case = truth(
        "gmail-cli",
        "2026-06-01T09:00:00-07:00",
        "gmail",
        priority="high",
        focus="focus",
    )
    policy_path = tmp_path / "operations.yaml"
    timeline_path = tmp_path / "timeline.yaml"
    labels_path = tmp_path / "labels.yaml"
    report_path = tmp_path / "result" / "report.json"
    for path, payload in (
        (policy_path, valid_policy_dict()),
        (timeline_path, timeline_dict([case])),
        (labels_path, labels_dict([case])),
    ):
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        path.chmod(0o600)

    exit_code = main(
        [
            "--policy",
            str(policy_path),
            "--timeline",
            str(timeline_path),
            "--labels",
            str(labels_path),
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 0
    assert report_path.exists()
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    assert json.loads(report_path.read_text())["report_type"] == (
        "operational_retrospective_shadow"
    )
