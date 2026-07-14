from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from pkm_brain.paths import BrainPaths
from pkm_brain.today_presentation import (
    TodayBriefing,
    TodayCoverage,
    TodayEvidenceLink,
    TodayFeedbackCapabilities,
    TodayFeedbackRequest,
    TodayFeedbackResult,
    TodayFreshness,
    TodayItem,
    TodayMissingReport,
    UnavailableTodayPresentationService,
)
from pkm_brain.ui_server import BrainUIServer, create_ui_server


@contextmanager
def running_today_server(paths: BrainPaths) -> Iterator[tuple[BrainUIServer, str, int]]:
    server = create_ui_server(paths, "127.0.0.1", 0, token="today-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield server, str(host), int(port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request_json(
    host: str,
    port: int,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    encoded = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Authorization": "Bearer today-token"}
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection(host, port, timeout=5)
    connection.request(method, path, body=encoded, headers=headers)
    response = connection.getresponse()
    body = json.loads(response.read().decode("utf-8"))
    connection.close()
    return response.status, body


class RecordingTodayService:
    def __init__(self) -> None:
        self.feedback: list[tuple[str, TodayFeedbackRequest]] = []
        self.missing: list[TodayMissingReport] = []

    def briefing(self) -> TodayBriefing:
        evidence = TodayEvidenceLink(
            source_type="google_calendar",
            label="Calendar event",
            reference="primary:event-42",
            brain_route="/today/evidence/primary:event-42",
            provider_url="https://calendar.google.com/calendar/event?eid=safe",
        )
        focus = TodayItem(
            id="item-1",
            section="focus",
            title="Prepare for the project review",
            summary="Review the open decision before the meeting.",
            kind="event",
            priority="P1",
            starts_at="2026-07-14T17:00:00+00:00",
            confidence=0.94,
            handled_verdict="needs_action",
            reason_codes=("direct_question",),
            evidence=(evidence,),
            feedback_actions=("confirm", "correct", "done", "snooze", "dismiss"),
        )
        return TodayBriefing(
            status="partial",
            briefing_id="briefing-1",
            generated_at="2026-07-14T14:00:00+00:00",
            as_of="2026-07-14T13:59:00+00:00",
            timezone="America/Los_Angeles",
            freshness=TodayFreshness(
                state="fresh",
                as_of="2026-07-14T13:59:00+00:00",
                age_seconds=60,
                stale_after_seconds=900,
            ),
            coverage=(
                TodayCoverage(
                    source_id="calendar",
                    label="Calendar",
                    state="complete",
                    last_success_at="2026-07-14T13:59:00+00:00",
                ),
                TodayCoverage(
                    source_id="gmail",
                    label="Email",
                    state="partial",
                    detail="Two threads deferred by the source budget.",
                    deferred_count=2,
                ),
            ),
            focus=(focus,),
            urgent_overflow_count=1,
            urgent_overflow=(
                TodayItem(
                    id="item-overflow",
                    section="urgent_overflow",
                    title="One more urgent item",
                    handled_verdict="unknown",
                ),
            ),
            calendar_next=(
                TodayItem(
                    id="event-42",
                    section="calendar_next",
                    title="Project review",
                    kind="event",
                    starts_at="2026-07-14T17:00:00+00:00",
                    handled_verdict="unknown",
                    evidence=(evidence,),
                ),
            ),
            ignored_suppressed=(
                TodayItem(
                    id="ignored-1",
                    section="ignored_suppressed",
                    title="Already handled follow-up",
                    handled_verdict="fulfilled",
                    reason_codes=("authoritative_reply_found",),
                    feedback_actions=("restore", "correct"),
                ),
            ),
            feedback=TodayFeedbackCapabilities(
                enabled=True,
                actions=(
                    "confirm",
                    "correct",
                    "done",
                    "snooze",
                    "dismiss",
                    "restore",
                    "report_missing",
                ),
            ),
        )

    def submit_feedback(
        self,
        item_id: str,
        request: TodayFeedbackRequest,
    ) -> TodayFeedbackResult:
        self.feedback.append((item_id, request))
        return TodayFeedbackResult(
            status="accepted",
            item_id=item_id,
            action=request.action,
            recorded_at="2026-07-14T14:01:00+00:00",
            message="Feedback recorded.",
        )

    def report_missing(self, report: TodayMissingReport) -> TodayFeedbackResult:
        self.missing.append(report)
        return TodayFeedbackResult(
            status="accepted",
            action="report_missing",
            recorded_at="2026-07-14T14:02:00+00:00",
            message="Missing item reported.",
        )


def test_unavailable_briefing_is_complete_and_safe() -> None:
    payload = UnavailableTodayPresentationService().briefing().as_dict()

    assert payload["schema_version"] == 1
    assert payload["status"] == "unavailable"
    assert payload["focus"] == []
    assert payload["urgent_overflow"] == {"count": 0, "items": []}
    assert payload["calendar"] == {"now": [], "next": []}
    assert payload["ignored_suppressed"] == []
    assert payload["feedback"]["enabled"] is False


def test_today_contract_enforces_focus_and_item_confidence() -> None:
    item = TodayItem(
        id="item",
        section="focus",
        title="Focus",
        handled_verdict="unknown",
    )
    with pytest.raises(ValueError, match="more than five"):
        TodayBriefing(
            status="available",
            generated_at="2026-07-14T14:00:00+00:00",
            focus=(item, item, item, item, item, item),
        )
    with pytest.raises(ValueError, match="confidence"):
        TodayItem(
            id="bad-confidence",
            section="attention",
            title="Bad confidence",
            confidence=1.1,
        )


def test_today_feedback_supports_confirmation_and_requires_correction_note() -> None:
    confirmation = TodayFeedbackRequest.from_payload({"action": "confirm"})

    assert confirmation.action == "confirm"
    assert confirmation.note is None
    with pytest.raises(ValueError, match="note is required"):
        TodayFeedbackRequest.from_payload({"action": "correct"})


def test_v1_today_routes_expose_briefing_and_feedback(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = RecordingTodayService()
    with running_today_server(paths) as (server, host, port):
        server.daemon_today_service = service
        status, briefing = request_json(host, port, "GET", "/api/v1/today")
        feedback_status, feedback = request_json(
            host,
            port,
            "POST",
            "/api/v1/today/items/item-1/feedback",
            {"action": "confirm"},
        )
        missing_status, missing = request_json(
            host,
            port,
            "POST",
            "/api/v1/today/feedback/missing",
            {"title": "Renew the domain", "source_hint": "email"},
        )

    assert status == 200
    assert briefing["schema_version"] == 1
    assert briefing["status"] == "partial"
    assert len(briefing["focus"]) == 1
    assert briefing["urgent_overflow"]["count"] == 1
    assert briefing["coverage"][1]["deferred_count"] == 2
    assert feedback_status == 200
    assert feedback["status"] == "accepted"
    assert service.feedback[0][0] == "item-1"
    assert service.feedback[0][1].action == "confirm"
    assert missing_status == 200
    assert missing["action"] == "report_missing"
    assert service.missing[0].title == "Renew the domain"


def test_v1_today_default_stub_and_request_validation(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    with running_today_server(paths) as (_server, host, port):
        status, briefing = request_json(host, port, "GET", "/api/v1/today")
        invalid_status, invalid = request_json(
            host,
            port,
            "POST",
            "/api/v1/today/items/item-1/feedback",
            {"action": "snooze"},
        )
        missing_status, missing = request_json(
            host,
            port,
            "POST",
            "/api/v1/today/feedback/missing",
            {"detail": "No title"},
        )

    assert status == 200
    assert briefing["status"] == "unavailable"
    assert invalid_status == 400
    assert "snoozed_until" in str(invalid["error"])
    assert missing_status == 400
    assert "title is required" in str(missing["error"])
