from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pkm_brain.operational_briefing as operational_briefing
from pkm_brain.google_cache import GoogleEvidenceCache

from pkm_brain.gmail_operations import GMAIL_DETECTOR_VERSION
from pkm_brain.operational_briefing import (
    build_meeting_packet,
    build_operational_briefing,
    operational_briefing_or_unavailable,
)
from pkm_brain.operational_db import init_operational_db
from pkm_brain.operational_meeting_packets import (
    MEETING_PACKET_CONTENT_VERSION,
    save_meeting_packet,
)
from pkm_brain.operational_shadow import (
    HandledAssessment,
    ShadowDecision,
    finish_shadow_run,
    record_handled_assessment,
    record_shadow_decision,
    start_shadow_run,
)
from pkm_brain.operational_state import OperationalObservation, reconcile_observation
from pkm_brain.operational_suppressions import suppress_calendar_series
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
    message_class: str | None = None,
    recurring_event_id: str | None = None,
    details: str | None = None,
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
            details=details,
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
                "message_class": message_class,
                "reconciliation_status": "confirmed",
                "recurring_event_id": recurring_event_id,
            },
        ),
        processed_at=NOW,
    )
    return result.item_id


def test_briefing_hides_recurring_series_and_marks_prepared_briefs(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ops.sqlite"
    init_operational_db(db_path)
    run = start_shadow_run(
        db_path,
        mode="fixture",
        requested_sources=["calendar"],
        policy_version="operations-v1",
        started_at=NOW,
    )
    hidden = add_item(
        db_path,
        source_key="family-time",
        kind="event",
        title="Family time",
        priority=0,
        starts_at="2026-07-13T16:00:00+00:00",
        ends_at="2026-07-13T17:00:00+00:00",
        source_type="calendar",
        recurring_event_id="family-weekly",
    )
    meeting = add_item(
        db_path,
        source_key="project-review-ready",
        kind="event",
        title="Project review",
        priority=0,
        starts_at="2026-07-13T18:00:00+00:00",
        ends_at="2026-07-13T19:00:00+00:00",
        source_type="calendar",
        recurring_event_id="project-weekly",
    )
    rule = suppress_calendar_series(
        db_path,
        hidden,
        updated_at=NOW,
        as_of=NOW,
    )
    save_meeting_packet(
        db_path,
        meeting,
        {
            "schema_version": 1,
            "content_version": MEETING_PACKET_CONTENT_VERSION,
            "item_id": meeting,
            "title": "Project review",
        },
        generated_at=NOW,
    )
    finish_shadow_run(
        db_path,
        run["id"],
        status="complete",
        coverage={"calendar": {"status": "complete", "fresh_at": NOW}},
        finished_at=NOW,
    )

    briefing = build_operational_briefing(
        db_path,
        timezone_name="America/Los_Angeles",
        policy_version="operations-v1",
        as_of=NOW,
        required_sources=("calendar",),
    )

    events = briefing["sections"]["now_and_next"]
    assert [event["title"] for event in events] == ["Project review"]
    assert events[0]["meeting_brief_ready"] is True
    assert events[0]["recurring_event_id"] == "project-weekly"
    assert "dismiss_series" in events[0]["feedback_actions"]
    assert briefing["counts"]["hidden_calendar_occurrences"] == 1
    assert briefing["hidden_calendar_series"] == [
        {
            **{key: rule[key] for key in rule if key != "idempotent"},
        }
    ]


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
    visible_item_ids = [
        card["item_id"]
        for section in (
            "focus",
            "urgent_overflow",
            "now_and_next",
            "upcoming",
            "overdue_and_due",
            "waiting",
            "low_confidence",
            "attention",
            "awareness",
            "changed",
        )
        for card in briefing["sections"][section]
    ]
    assert len(visible_item_ids) == len(set(visible_item_ids))


def test_meeting_packet_adds_human_brief_context_without_losing_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    init_operational_db(paths.ops_sqlite_path)
    item_id = add_item(
        paths.ops_sqlite_path,
        source_key="project-review",
        kind="event",
        title="Project review",
        priority=10,
        starts_at="2026-07-14T17:00:00+00:00",
        ends_at="2026-07-14T18:00:00+00:00",
        source_type="calendar",
    )
    source_ref = "calendar:test@example.com:primary:project-review"
    GoogleEvidenceCache.for_paths(paths).write_normalized(
        "calendar",
        source_ref,
        {
            "title": "Project review",
            "details": "Review launch readiness and decide whether to ship.",
            "location": "Video call",
            "organizer_email": "organizer@example.com",
            "attendee_count": 4,
            "attendee_response": "accepted",
        },
        cached_at=datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc),
        source_revision="revision-project-review",
    )
    add_item(
        paths.ops_sqlite_path,
        source_key="launch-readiness-email",
        kind="attention",
        title="Launch readiness question",
        details="Taylor asked whether the launch is ready to ship.",
        priority=30,
        source_type="gmail",
        message_class="human",
    )

    class FakeBrainService:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def retrieve_context(self, *_args, **_kwargs):
            return {
                "retrieval_verdict": "partial",
                "retrieval_reasons": ["One relevant source was unavailable."],
                "relevant_facts": [
                    {
                        "id": "fact-17",
                        "statement": "Launch pricing approval remains open.",
                        "source_ids": ["document-17"],
                        "truth_confidence": 0.91,
                    }
                ],
                "relevant_wiki_pages": [
                    {
                        "title": "Launch readiness",
                        "relative_path": "projects/project-review.md",
                        "summary": "Current launch decisions and open questions.",
                        "source_ids": ["document-17"],
                    }
                ],
                "supporting_chunks": [],
                "open_questions": [
                    {
                        "id": "question-1",
                        "question": "Has Legal approved the launch pricing language?",
                        "fact_ids": ["fact-17"],
                    }
                ],
            }

    monkeypatch.setattr(operational_briefing, "BrainService", FakeBrainService)

    packet = build_meeting_packet(
        paths,
        item_id,
        generated_at=NOW,
    )

    assert packet["schema_version"] == 1
    assert packet["content_version"] == MEETING_PACKET_CONTENT_VERSION
    assert packet["brief_context"] == {
        "calendar_notes": "Review launch readiness and decide whether to ship.",
        "calendar_notes_status": "available",
        "starts_at": "2026-07-14T17:00:00+00:00",
        "ends_at": "2026-07-14T18:00:00+00:00",
        "location": "Video call",
        "organizer_email": "organizer@example.com",
        "attendee_count": 4,
        "attendee_response": "accepted",
        "source_timezone": "America/Los_Angeles",
        "all_day": False,
    }
    assert packet["knowledge_claims"][0]["evidence_refs"] == [
        {"source_ref": "document-17"}
    ]
    assert packet["brief_knowledge_claims"][0]["claim"] == (
        "Launch pricing approval remains open."
    )
    assert packet["open_questions"] == [
        {
            "question": "Has Legal approved the launch pricing language?",
            "source": "brain",
            "reference": "question-1",
            "fact_ids": ["fact-17"],
        }
    ]
    assert packet["brief_open_questions"] == packet["open_questions"]
    assert [page["title"] for page in packet["brief_wiki_context"]] == [
        "Launch readiness"
    ]
    assert packet["source_links"][0]["source_type"] == "calendar"
    assert packet["source_links"][0]["brain_route"].startswith(
        "/api/ops/evidence?"
    )
    assert packet["source_links"][1]["wiki_path"] == (
        "projects/project-review.md"
    )
    gmail_claims = [
        claim
        for claim in packet["knowledge_claims"]
        if claim["claim_type"] == "gmail_operational_item"
    ]
    assert gmail_claims[0]["claim"].startswith(
        "Related email: Launch readiness question"
    )
    gmail_links = [
        link for link in packet["source_links"] if link["source_type"] == "gmail"
    ]
    assert gmail_links[0]["brain_route"].startswith("/api/ops/evidence?")
    assert gmail_links[0]["provider_url"].startswith("https://mail.google.com/")
    assert all(
        suggestion["is_factual_claim"] is False
        for suggestion in packet["suggestions"]
    )


def test_meeting_packet_keeps_generic_retrieval_only_in_diagnostics(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    init_operational_db(paths.ops_sqlite_path)
    item_id = add_item(
        paths.ops_sqlite_path,
        source_key="cruise-checkin",
        kind="event",
        title="Cruise checkin appointment at 2pm",
        priority=0,
        starts_at="2026-07-16T21:00:00+00:00",
        ends_at="2026-07-16T21:30:00+00:00",
        source_type="calendar",
    )

    class FakeBrainService:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def retrieve_context(self, *_args, **_kwargs):
            return {
                "retrieval_verdict": "found",
                "retrieval_reasons": ["Generic lexical matches were returned."],
                "relevant_facts": [
                    {
                        "id": "fact-prior-role",
                        "statement": "Peter previously held an enterprise role.",
                        "source_ids": ["document-role"],
                    }
                ],
                "relevant_wiki_pages": [
                    {
                        "title": "Virtual Appointment with Evette",
                        "relative_path": "people/evette.md",
                        "summary": "Notes from a prior virtual appointment.",
                        "source_ids": ["document-evette"],
                    },
                    {
                        "title": "Peter Prior Roles",
                        "relative_path": "people/peter-prior-roles.md",
                        "summary": "A history of Peter's work.",
                        "source_ids": ["document-role"],
                    },
                    {
                        "title": "Unity Catalog",
                        "relative_path": "concepts/unity-catalog.md",
                        "summary": "Enterprise data governance notes.",
                        "source_ids": ["document-unity"],
                    },
                    {
                        "title": "Enterprise Agent Product Work",
                        "relative_path": "projects/enterprise-agent.md",
                        "summary": "Product strategy and execution context.",
                        "source_ids": ["document-agent"],
                    },
                ],
                "supporting_chunks": [],
                "open_questions": [
                    {
                        "id": "question-prior-role",
                        "question": "Which prior role did Peter hold?",
                        "fact_ids": ["fact-prior-role"],
                    }
                ],
            }

    operator = SimpleNamespace(
        calendar=SimpleNamespace(email="peter@example.com"),
        gmail=SimpleNamespace(email="peter@example.com"),
    )
    monkeypatch.setattr(operational_briefing, "BrainService", FakeBrainService)
    monkeypatch.setattr(
        operational_briefing,
        "load_operations_policy",
        lambda _paths: SimpleNamespace(operator=operator),
    )

    packet = build_meeting_packet(paths, item_id, generated_at=NOW)

    assert len(packet["wiki_context"]) == 4
    assert len(packet["knowledge_claims"]) == 1
    assert len(packet["open_questions"]) == 1
    assert packet["brief_wiki_context"] == []
    assert packet["brief_knowledge_claims"] == []
    assert packet["brief_open_questions"] == []
    assert all(
        link["source_type"] != "wiki" for link in packet["source_links"]
    )


def test_ambiguous_low_confidence_item_stays_out_of_focus(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ops.sqlite"
    init_operational_db(db_path)
    run = start_shadow_run(
        db_path,
        mode="fixture",
        requested_sources=["gmail"],
        policy_version="operations-v1",
        detector_version=GMAIL_DETECTOR_VERSION,
        started_at=NOW,
    )
    item_id = add_item(
        db_path,
        source_key="uncertain-security",
        kind="attention",
        title="Review an uncertain security notice",
        priority=90,
        confidence=0.25,
    )
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
    finish_shadow_run(
        db_path,
        run["id"],
        status="complete",
        coverage={"gmail": {"status": "complete", "fresh_at": NOW}},
        finished_at=NOW,
    )

    briefing = build_operational_briefing(
        db_path,
        timezone_name="America/Los_Angeles",
        policy_version="operations-v1",
        as_of=NOW,
        required_sources=("gmail",),
    )

    assert briefing["sections"]["focus"] == []
    assert briefing["sections"]["urgent_overflow"] == []
    assert [
        card["item_id"] for card in briefing["sections"]["low_confidence"]
    ] == [item_id]
    assert briefing["sections"]["attention"] == []


def test_legacy_active_marketing_item_is_only_in_suppressed_audit(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ops.sqlite"
    init_operational_db(db_path)
    run = start_shadow_run(
        db_path,
        mode="fixture",
        requested_sources=["gmail"],
        policy_version="operations-v1",
        detector_version=GMAIL_DETECTOR_VERSION,
        started_at=NOW,
    )
    marketing_item_id = add_item(
        db_path,
        source_key="legacy-ad",
        kind="attention",
        title="Limited-time product promotion",
        priority=90,
        confidence=0.25,
        message_class="marketing",
    )
    record_shadow_decision(
        db_path,
        run["id"],
        ShadowDecision(
            source_type="gmail",
            account_key="gmail:test@example.com",
            stream_key="threads",
            source_key="thread-legacy-ad",
            source_revision="revision-legacy-ad",
            disposition="deferred",
            reason_code="marketing_update_pending_reconciliation",
            item_ids=(marketing_item_id,),
            evidence_refs=(
                {
                    "thread_id": "thread-legacy-ad",
                    "message_id": "message-legacy-ad",
                    "source_ref": "gmail:test@example.com:thread-legacy-ad",
                },
                ),
                confidence=0.99,
                metadata={"subject": "Limited-time product promotion"},
            ),
        created_at=NOW,
    )
    finish_shadow_run(
        db_path,
        run["id"],
        status="complete",
        coverage={"gmail": {"status": "complete", "fresh_at": NOW}},
        counts={"surfaced": 1},
        finished_at=NOW,
    )

    briefing = build_operational_briefing(
        db_path,
        timezone_name="America/Los_Angeles",
        policy_version="operations-v1",
        as_of=NOW,
        required_sources=("gmail",),
    )

    visible_ids = {
        str(card.get("item_id") or card.get("id"))
        for name, cards in briefing["sections"].items()
        if name != "suppressed"
        for card in cards
    }
    assert marketing_item_id not in visible_ids
    marketing_audit = [
        card
        for card in briefing["sections"]["suppressed"]
        if card["reason_code"].startswith("marketing_update")
    ]
    assert len(marketing_audit) == 1
    assert marketing_audit[0]["title"] == "Limited-time product promotion"
    assert marketing_audit[0]["evidence_refs"][0]["thread_id"] == (
        "thread-legacy-ad"
    )
    assert marketing_audit[0]["local_evidence_route"].startswith(
        "/api/ops/evidence?"
    )


def test_surfaced_recruiter_activity_overrides_bulk_header_and_stays_in_attention(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ops.sqlite"
    init_operational_db(db_path)
    run = start_shadow_run(
        db_path,
        mode="fixture",
        requested_sources=["gmail"],
        policy_version="operations-v1",
        detector_version=GMAIL_DETECTOR_VERSION,
        started_at=NOW,
    )
    item_id = add_item(
        db_path,
        source_key="recruiter-attention",
        kind="attention",
        title="Recruiter follow-up",
        priority=40,
        owner="unknown",
        confidence=0.8,
        message_class="marketing",
    )
    thread_id = "thread-recruiter-attention"
    record_shadow_decision(
        db_path,
        run["id"],
        ShadowDecision(
            source_type="gmail",
            account_key="gmail:test@example.com",
            stream_key="threads",
            source_key=thread_id,
            source_revision="revision-recruiter-attention",
            disposition="surfaced",
            reason_code="recruiter_activity",
            item_ids=(item_id,),
            evidence_refs=(
                {
                    "thread_id": thread_id,
                    "message_id": "message-recruiter-attention",
                    "source_ref": "gmail:test@example.com:thread-recruiter-attention",
                },
            ),
            confidence=0.8,
        ),
        created_at=NOW,
    )
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
    finish_shadow_run(
        db_path,
        run["id"],
        status="complete",
        coverage={"gmail": {"status": "complete", "fresh_at": NOW}},
        counts={"surfaced": 1},
        finished_at=NOW,
    )

    briefing = build_operational_briefing(
        db_path,
        timezone_name="America/Los_Angeles",
        policy_version="operations-v1",
        as_of=NOW,
        required_sources=("gmail",),
    )

    assert [
        card["item_id"] for card in briefing["sections"]["attention"]
    ] == [item_id]
    assert briefing["sections"]["focus"] == []
    assert briefing["sections"]["low_confidence"] == []
    assert all(
        card.get("item_id") != item_id
        for card in briefing["sections"]["suppressed"]
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


def test_briefing_requires_every_enabled_source_and_ignores_stale_handled_state(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ops.sqlite"
    init_operational_db(db_path)
    run = start_shadow_run(
        db_path,
        mode="fixture",
        requested_sources=["gmail"],
        policy_version="operations-v1",
        detector_version=GMAIL_DETECTOR_VERSION,
        started_at=NOW,
    )
    item_id = add_item(
        db_path,
        source_key="stale-work",
        kind="commitment",
        title="Complete stale work",
        priority=70,
    )
    record_handled_assessment(
        db_path,
        HandledAssessment(
            item_id=item_id,
            verdict="fulfilled",
            sources_checked=("gmail",),
            coverage={"gmail": {"status": "complete"}},
            policy_version="operations-v1",
            confidence=0.99,
            as_of=NOW,
        ),
        run_id=run["id"],
    )
    finish_shadow_run(
        db_path,
        run["id"],
        status="complete",
        coverage={"gmail": {"status": "complete", "fresh_at": NOW}},
        finished_at=NOW,
    )

    missing_source = build_operational_briefing(
        db_path,
        timezone_name="America/Los_Angeles",
        policy_version="operations-v1",
        as_of=NOW,
    )
    stale = build_operational_briefing(
        db_path,
        timezone_name="America/Los_Angeles",
        policy_version="operations-v1",
        as_of="2026-07-13T23:00:01+00:00",
        required_sources=("gmail",),
    )

    assert missing_source["status"] == "partial"
    assert missing_source["all_clear"] is False
    assert missing_source["coverage"]["calendar"]["status"] == "unavailable"
    assert stale["status"] == "partial"
    assert stale["coverage"]["gmail"]["reason"] == "stale"
    assert stale["sections"]["focus"][0]["item_id"] == item_id
    assert stale["sections"]["focus"][0]["handled_verdict"] == "unknown"
