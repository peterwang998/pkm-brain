from __future__ import annotations

from pathlib import Path

from pkm_brain.gmail_operations import GMAIL_DETECTOR_VERSION
from pkm_brain.operational_briefing import (
    build_operational_briefing,
    operational_briefing_or_unavailable,
)
from pkm_brain.operational_db import init_operational_db
from pkm_brain.operational_shadow import (
    HandledAssessment,
    finish_shadow_run,
    record_handled_assessment,
    start_shadow_run,
)
from pkm_brain.operational_state import OperationalObservation, reconcile_observation
from pkm_brain.paths import BrainPaths


NOW = "2026-07-13T15:00:00+00:00"


def add_item(
    db_path: Path,
    *,
    source_key: str,
    kind: str,
    title: str,
    priority: int,
    owner: str = "operator",
    due_at: str | None = None,
    starts_at: str | None = None,
    ends_at: str | None = None,
    source_type: str = "gmail",
    confidence: float = 0.9,
) -> str:
    account_key = (
        "calendar:test@example.com" if source_type == "calendar" else "gmail:test@example.com"
    )
    evidence = (
        {
            "event_id": source_key,
            "source_ref": f"calendar:test@example.com:primary:{source_key}",
        }
        if source_type == "calendar"
        else {
            "thread_id": f"thread-{source_key}",
            "message_id": f"message-{source_key}",
            "source_ref": f"gmail:test@example.com:thread-{source_key}:message-{source_key}",
        }
    )
    result = reconcile_observation(
        db_path,
        OperationalObservation(
            source_type=source_type,
            account_key=account_key,
            stream_key="primary" if source_type == "calendar" else f"thread-{source_key}",
            source_key=source_key,
            source_revision=f"revision-{source_key}",
            source_order=1,
            source_updated_at=NOW,
            observed_at=NOW,
            item_kind=kind,
            title=title,
            owner=owner,
            due_at=due_at,
            starts_at=starts_at,
            ends_at=ends_at,
            source_timezone="America/Los_Angeles",
            priority=priority,
            confidence=confidence,
            evidence_refs=(evidence,),
            metadata={
                "calendar_id": "primary" if source_type == "calendar" else None,
                "detector_version": "gmail-ops-v1" if source_type == "gmail" else None,
                "reconciliation_status": "confirmed",
            },
        ),
        processed_at=NOW,
    )
    return result.item_id


def test_briefing_focus_is_adaptive_and_urgent_overflow_is_never_hidden(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ops.sqlite"
    init_operational_db(db_path)
    run = start_shadow_run(
        db_path,
        mode="fixture",
        requested_sources=["calendar", "gmail"],
        policy_version="operations-v1",
        detector_version=GMAIL_DETECTOR_VERSION,
        started_at=NOW,
    )
    item_ids = []
    for index in range(7):
        item_id = add_item(
            db_path,
            source_key=f"work-{index}",
            kind="commitment",
            title=f"Complete work item {index}",
            priority=90 - index,
            due_at="2026-07-13T17:00:00+00:00",
        )
        item_ids.append(item_id)
        record_handled_assessment(
            db_path,
            HandledAssessment(
                item_id=item_id,
                verdict="needs_action",
                sources_checked=("gmail",),
                coverage={"gmail": {"status": "complete"}},
                policy_version="operations-v1",
                confidence=0.95,
                as_of=NOW,
            ),
            run_id=run["id"],
        )
    add_item(
        db_path,
        source_key="today-meeting",
        kind="event",
        title="Planning review",
        priority=10,
        starts_at="2026-07-13T16:00:00+00:00",
        ends_at="2026-07-13T17:00:00+00:00",
        source_type="calendar",
    )
    finish_shadow_run(
        db_path,
        run["id"],
        status="complete",
        coverage={
            "calendar": {"status": "complete", "fresh_at": NOW},
            "gmail": {"status": "complete", "fresh_at": NOW},
        },
        counts={"surfaced": 8},
        finished_at=NOW,
    )

    briefing = build_operational_briefing(
        db_path,
        timezone_name="America/Los_Angeles",
        policy_version="operations-v1",
        as_of=NOW,
    )

    assert briefing["status"] == "complete"
    assert briefing["all_clear"] is False
    assert len(briefing["sections"]["focus"]) == 5
    assert len(briefing["sections"]["urgent_overflow"]) == 2
    assert len(briefing["sections"]["now_and_next"]) == 1
    assert briefing["sections"]["now_and_next"][0]["provider_route"] is None
    assert {
        card["item_id"] for card in briefing["sections"]["focus"]
    }.isdisjoint(
        {card["item_id"] for card in briefing["sections"]["urgent_overflow"]}
    )
    assert all(
        card["handled_verdict"] == "needs_action"
        for card in briefing["sections"]["focus"]
    )
    assert briefing["sections"]["focus"][0]["provider_route"].startswith(
        "https://mail.google.com/"
    )
    assert briefing["sections"]["focus"][0]["local_evidence_route"].startswith(
        "/api/ops/evidence?"
    )


def test_briefing_does_not_claim_all_clear_when_coverage_is_partial(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ops.sqlite"
    init_operational_db(db_path)
    run = start_shadow_run(
        db_path,
        mode="fixture",
        requested_sources=["calendar", "gmail"],
        policy_version="operations-v1",
        detector_version=GMAIL_DETECTOR_VERSION,
        started_at=NOW,
    )
    finish_shadow_run(
        db_path,
        run["id"],
        status="partial",
        coverage={
            "calendar": {"status": "complete", "fresh_at": NOW},
            "gmail": {"status": "partial", "deferred_count": 4},
        },
        finished_at=NOW,
    )
    briefing = build_operational_briefing(
        db_path,
        timezone_name="America/Los_Angeles",
        policy_version="operations-v1",
        as_of=NOW,
    )
    assert briefing["status"] == "partial"
    assert briefing["all_clear"] is False
    assert briefing["sections"]["focus"] == []
    assert briefing["headline"] == "Shadow coverage is incomplete"


def test_missing_operational_store_is_an_explicit_unavailable_briefing(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    briefing = operational_briefing_or_unavailable(
        paths,
        timezone_name="America/Los_Angeles",
        policy_version="operations-v1",
        as_of=NOW,
    )
    assert briefing["status"] == "unavailable"
    assert briefing["all_clear"] is False
    assert briefing["coverage"]["calendar"]["status"] == "unavailable"
