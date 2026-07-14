from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from pkm_brain.gmail_mirror import GmailMirrorCheckpointUpdate, GmailMirrorStore
from pkm_brain.operational_today import (
    _overlay_gmail_mirror_coverage,
    today_briefing_from_operational,
)
from pkm_brain.paths import BrainPaths


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
        "hidden_calendar_series": [
            {
                "id": "rule-1",
                "label": "Family time",
                "hidden_count": 4,
                "created_at": "2026-07-13T12:00:00+00:00",
                "next_starts_at": "2026-07-14T01:00:00+00:00",
            }
        ],
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
    assert briefing.hidden_calendar_series[0].label == "Family time"
    assert briefing.hidden_calendar_series[0].hidden_count == 4
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


def test_gmail_coverage_distinguishes_synced_mailbox_from_analysis_backlog() -> None:
    value = projection(status="partial")
    value["coverage"]["gmail"].update(
        {
            "status": "partial",
            "mailbox_status": "synchronized",
            "mailbox_last_success_at": "2026-07-13T15:58:00+00:00",
            "triage_status": "backlogged",
            "triage_pending_count": 7,
            "reason": "triage_backlog",
        }
    )

    briefing = today_briefing_from_operational(value, now=NOW)

    assert briefing.coverage[1].state == "partial"
    assert briefing.coverage[1].detail == "Mailbox synchronized · 7 awaiting analysis"
    assert briefing.coverage[1].last_success_at == "2026-07-13T15:58:00+00:00"


def test_enabled_missing_gmail_mirror_blocks_stale_all_clear(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    value = projection()
    value["all_clear"] = True

    _overlay_gmail_mirror_coverage(
        value,
        paths=paths,
        account_key="gmail.primary",
        enabled=True,
    )

    assert value["all_clear"] is False
    assert value["status"] == "partial"
    assert value["coverage"]["gmail"]["status"] == "unavailable"
    assert value["coverage"]["gmail"]["mailbox_status"] == "not_initialized"
    assert "fresh_at" not in value["coverage"]["gmail"]
    briefing = today_briefing_from_operational(value, now=NOW)
    assert briefing.status == "partial"
    assert briefing.coverage[1].detail == (
        "Mailbox not initialized · awaiting first sync"
    )


def test_disabled_gmail_source_does_not_require_a_mirror(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    value = projection()
    value["all_clear"] = True

    _overlay_gmail_mirror_coverage(
        value,
        paths=paths,
        account_key="gmail.primary",
        enabled=False,
    )

    assert value["all_clear"] is True
    assert value["status"] == "complete"


def test_enabled_uninitialized_gmail_mirror_blocks_stale_all_clear(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    GmailMirrorStore(paths.gmail_mirror_sqlite_path).initialize()
    value = projection()
    value["all_clear"] = True

    _overlay_gmail_mirror_coverage(
        value,
        paths=paths,
        account_key="gmail.primary",
        enabled=True,
    )

    assert value["all_clear"] is False
    assert value["status"] == "partial"
    assert value["coverage"]["gmail"]["mailbox_status"] == "not_initialized"


def test_enabled_corrupt_gmail_mirror_blocks_stale_all_clear(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    mirror_path = paths.gmail_mirror_sqlite_path
    mirror_path.parent.mkdir(parents=True, mode=0o700)
    mirror_path.write_bytes(b"not a sqlite database")
    mirror_path.chmod(0o600)
    value = projection()
    value["all_clear"] = True

    _overlay_gmail_mirror_coverage(
        value,
        paths=paths,
        account_key="gmail.primary",
        enabled=True,
    )

    assert value["all_clear"] is False
    assert value["status"] == "partial"
    assert value["coverage"]["gmail"]["mailbox_status"] == "unavailable"
    assert value["coverage"]["gmail"]["error"].startswith(
        "Gmail mirror unavailable:"
    )


def test_enabled_insecure_gmail_mirror_blocks_stale_all_clear(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    GmailMirrorStore(paths.gmail_mirror_sqlite_path).initialize()
    paths.gmail_mirror_sqlite_path.chmod(0o644)
    value = projection()
    value["all_clear"] = True

    _overlay_gmail_mirror_coverage(
        value,
        paths=paths,
        account_key="gmail.primary",
        enabled=True,
    )

    assert value["all_clear"] is False
    assert value["status"] == "partial"
    assert value["coverage"]["gmail"]["mailbox_status"] == "unavailable"
    assert "owner-only" in value["coverage"]["gmail"]["error"]


@pytest.mark.parametrize(
    ("scheduled_sync", "expected_state", "expected_detail"),
    (
        (
            {"available": False},
            "unavailable",
            "Automatic Gmail sync status is unavailable.",
        ),
        (
            {"available": True, "enabled": False, "paused": False},
            "unavailable",
            "Automatic Gmail sync is turned off.",
        ),
        (
            {
                "available": True,
                "enabled": True,
                "paused": True,
                "paused_until": "2026-07-14T18:00:00+00:00",
            },
            "partial",
            "Automatic Gmail sync is paused until 2026-07-14T18:00:00+00:00.",
        ),
        (
            {
                "available": True,
                "enabled": True,
                "paused": False,
                "last_status": "failed",
                "last_error": "daily Gmail request budget exhausted",
            },
            "partial",
            "Automatic Gmail sync failed: daily Gmail request budget exhausted",
        ),
    ),
)
def test_current_gmail_sync_problem_blocks_today_all_clear(
    tmp_path: Path,
    scheduled_sync: dict[str, object],
    expected_state: str,
    expected_detail: str,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    _complete_gmail_mirror(paths)
    value = projection()
    value["all_clear"] = True

    _overlay_gmail_mirror_coverage(
        value,
        paths=paths,
        account_key="gmail.primary",
        enabled=True,
        scheduled_sync=scheduled_sync,
    )

    assert value["all_clear"] is False
    assert value["status"] == "partial"
    assert value["coverage"]["gmail"]["status"] == expected_state
    briefing = today_briefing_from_operational(value, now=NOW)
    assert briefing.coverage[1].detail == expected_detail


def test_later_success_supersedes_an_old_gmail_sync_error(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    _complete_gmail_mirror(paths)
    value = projection()
    value["all_clear"] = True

    _overlay_gmail_mirror_coverage(
        value,
        paths=paths,
        account_key="gmail.primary",
        enabled=True,
        scheduled_sync={
            "available": True,
            "enabled": True,
            "paused": False,
            "last_status": "success",
            "last_error": "an error retained from an older run",
        },
    )

    assert value["all_clear"] is True
    assert value["status"] == "complete"
    assert value["coverage"]["gmail"]["status"] == "complete"
    assert "sync_error" not in value["coverage"]["gmail"]


def test_newer_manual_mirror_success_supersedes_scheduler_failure(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    _complete_gmail_mirror(paths)
    value = projection()
    value["all_clear"] = True

    _overlay_gmail_mirror_coverage(
        value,
        paths=paths,
        account_key="gmail.primary",
        enabled=True,
        scheduled_sync={
            "available": True,
            "enabled": True,
            "paused": False,
            "last_run_at": "2026-07-13T15:00:00+00:00",
            "last_status": "failed",
            "last_error": "failure before the manual Shadow run",
        },
    )

    assert value["all_clear"] is True
    assert value["status"] == "complete"
    assert value["coverage"]["gmail"]["status"] == "complete"
    assert "sync_error" not in value["coverage"]["gmail"]


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


def _complete_gmail_mirror(paths: BrainPaths) -> None:
    store = GmailMirrorStore(paths.gmail_mirror_sqlite_path)
    store.initialize()
    store.apply_sync_unit(
        GmailMirrorCheckpointUpdate(
            account_key="gmail.primary",
            history_id="history-1",
            mode="full",
            coverage_complete=True,
            reset_required=False,
            continuation_page_token=None,
            baseline_history_id=None,
            pending_thread_ids=(),
            continuation_history_id=None,
            expected_generation=None,
            last_success_at="2026-07-13T15:58:00+00:00",
            updated_at="2026-07-13T15:58:00+00:00",
        ),
        (),
    )
