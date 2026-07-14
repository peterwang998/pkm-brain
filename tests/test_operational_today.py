from __future__ import annotations

from datetime import datetime, timezone

from pkm_brain.operational_today import today_briefing_from_operational


NOW = datetime(2026, 7, 13, 16, 0, tzinfo=timezone.utc)


def card(**overrides):
    value = {
        "id": "item-1",
        "item_id": "item-1",
        "kind": "commitment",
        "state": "active",
        "title": "Send the board deck",
        "details": "Requested by Pat",
        "owner": "operator",
        "counterparty": "person@example.com",
        "priority": 70,
        "confidence": 0.92,
        "starts_at": None,
        "ends_at": None,
        "due_at": "2026-07-13T18:00:00+00:00",
        "source_type": "gmail",
        "source_key": "thread-1",
        "reconciliation_status": "confirmed",
        "latest_event_type": "created",
        "handled_verdict": "needs_action",
        "why_now": "Due within the next 24 hours.",
        "next_move": "Send the board deck",
        "evidence_refs": [
            {
                "source_ref": "gmail.primary:thread-1",
                "thread_id": "thread-1",
            }
        ],
        "local_evidence_route": "/api/ops/evidence?source_type=gmail",
        "provider_route": "https://mail.google.com/mail/u/0/#all/thread-1",
        "feedback_actions": ["confirm", "done", "incorrect", "dismiss"],
    }
    value.update(overrides)
    return value


def projection(*, status: str = "complete"):
    event = card(
        id="event-1",
        item_id="event-1",
        kind="event",
        title="Planning review",
        starts_at="2026-07-13T15:30:00+00:00",
        ends_at="2026-07-13T16:30:00+00:00",
        due_at=None,
        source_type="calendar",
        source_key="event-1",
        handled_verdict="unknown",
        feedback_actions=["confirm", "dismiss"],
    )
    return {
        "status": status,
        "generated_at": "2026-07-13T16:00:00+00:00",
        "timezone": "America/Los_Angeles",
        "policy_version": "shadow@1",
        "run_id": "run-1",
        "headline": "One item needs attention",
        "coverage": {
            "calendar": {
                "status": "complete",
                "fresh_at": "2026-07-13T15:59:00+00:00",
                "mode": "full",
                "deferred_count": 0,
            },
            "gmail": {
                "status": status,
                "fresh_at": "2026-07-13T15:58:00+00:00",
                "mode": "full",
                "deferred_count": 0,
            },
        },
        "sections": {
            "focus": [card()],
            "urgent_overflow": [],
            "now_and_next": [event],
            "upcoming": [],
            "overdue_and_due": [card()],
            "waiting": [],
            "attention": [card()],
            "awareness": [],
            "low_confidence": [],
            "suppressed": [
                {
                    "id": "decision-1",
                    "title": "Store receipt",
                    "source_type": "gmail",
                    "source_key": "thread-2",
                    "disposition": "suppressed",
                    "reason_code": "transactional_no_current_action",
                    "confidence": 0.98,
                    "evidence_refs": [
                        {"source_ref": "gmail.primary:thread-2"}
                    ],
                    "local_evidence_route": "/api/ops/evidence?source_type=gmail",
                }
            ],
        },
    }


def test_operational_projection_maps_to_today_with_coverage_evidence_and_audit() -> None:
    briefing = today_briefing_from_operational(projection(), now=NOW)

    assert briefing.status == "available"
    assert briefing.freshness.state == "fresh"
    assert briefing.as_of == "2026-07-13T15:58:00+00:00"
    assert [item.source_id for item in briefing.coverage] == ["calendar", "gmail"]
    assert len(briefing.focus) == 1
    assert briefing.focus[0].priority == "P1"
    assert briefing.focus[0].summary == "Send the board deck"
    assert briefing.focus[0].evidence[0].brain_route.startswith("/api/ops/evidence")
    assert len(briefing.calendar_now) == 1
    assert briefing.calendar_now[0].title == "Planning review"
    assert len(briefing.ignored_suppressed) == 1
    assert briefing.ignored_suppressed_count == 1
    assert briefing.ignored_suppressed[0].reason_codes == (
        "transactional_no_current_action",
    )
    assert briefing.feedback.enabled is True


def test_partial_projection_never_becomes_an_all_clear() -> None:
    value = projection(status="partial")
    value["sections"]["focus"] = []

    briefing = today_briefing_from_operational(value, now=NOW)

    assert briefing.status == "partial"
    assert briefing.availability_reason
    assert briefing.coverage[1].state == "partial"


def test_uncertain_items_do_not_display_verified_priority_badges() -> None:
    value = projection(status="partial")
    value["sections"]["focus"] = []
    value["sections"]["low_confidence"] = [
        card(
            id="uncertain-1",
            item_id="uncertain-1",
            priority=95,
            confidence=0.25,
            reconciliation_status="ambiguous",
            handled_verdict="unknown",
        )
    ]

    briefing = today_briefing_from_operational(value, now=NOW)

    assert len(briefing.uncertain) == 1
    assert briefing.uncertain[0].priority is None
    assert briefing.uncertain[0].confidence == 0.25


def test_coverage_reason_codes_are_presented_as_plain_language() -> None:
    value = projection(status="partial")
    value["coverage"]["calendar"] = {
        "status": "unavailable",
        "reason": "missing_coverage",
    }
    value["coverage"]["gmail"] = {
        "status": "partial",
        "reason": "detector_version_mismatch",
    }

    briefing = today_briefing_from_operational(value, now=NOW)

    assert briefing.coverage[0].detail == "This run is still gathering source coverage."
    assert briefing.coverage[1].detail == "Gmail needs a fresh detector review."
    assert "_" not in briefing.coverage[0].detail
    assert "_" not in briefing.coverage[1].detail


def test_today_preserves_total_urgent_count_when_snapshot_is_a_preview() -> None:
    value = projection()
    value["sections"]["urgent_overflow"] = [
        card(id="urgent-1", item_id="urgent-1")
    ]
    value["counts"] = {
        "urgent_overflow": 17,
        "section_projection": {
            "urgent_overflow": {"total": 17, "included": 1, "omitted": 16}
        },
    }

    briefing = today_briefing_from_operational(value, now=NOW)

    assert briefing.urgent_overflow_count == 17
    assert len(briefing.urgent_overflow) == 1
