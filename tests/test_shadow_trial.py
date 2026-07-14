from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

import pkm_brain.shadow_trial as shadow_trial_module
from pkm_brain.gmail_operations import (
    DETECTOR_INPUT_TOKEN_OVERHEAD_RESERVE,
    DETECTOR_OUTPUT_TOKEN_RESERVE,
)
from pkm_brain.google_cache import GoogleEvidenceCache
from pkm_brain.google_normalization import (
    NormalizedCalendarEvent,
    NormalizedGmailMessage,
    NormalizedGmailThread,
    normalize_calendar_event,
)
from pkm_brain.google_sources import CalendarFetchResult, GmailFetchResult
from pkm_brain.llm_usage import record_provider_usage
from pkm_brain.operational_budget import DailyBudgetExceeded, daily_budget_usage
from pkm_brain.operational_db import operational_connection
from pkm_brain.operational_service import OperationalService
from pkm_brain.operational_shadow import (
    latest_handled_assessments,
    list_shadow_decisions,
    list_shadow_runs,
)
from pkm_brain.operational_state import get_source_cursor
from pkm_brain.operations_http import operations_evidence_payload
from pkm_brain.operations_policy import (
    CALENDAR_OWNED_READ_SCOPE,
    GMAIL_READ_SCOPE,
    OperationsPolicy,
    operations_policy_path,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.shadow_controller import ShadowTrialController, _completion_message
from pkm_brain.shadow_setup import default_operations_policy_payload
from pkm_brain.shadow_trial import ShadowTrialRunner


NOW = datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc)


def test_manual_gmail_run_caps_responsive_thread_work() -> None:
    assert shadow_trial_module._gmail_manual_run_thread_cap(1_200) == 200
    assert shadow_trial_module._gmail_manual_run_thread_cap(100) == 75
    assert shadow_trial_module._gmail_manual_run_thread_cap(1) == 1


class FakeCalendarReader:
    def __init__(self, result: CalendarFetchResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    def fetch(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class FakeGmailReader:
    def __init__(self, result: GmailFetchResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    def fetch(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class FakeProvider:
    name = "fake"
    model = "fake-ops"

    def __init__(
        self,
        response: dict,
        *,
        usage: dict[str, int] | None = None,
    ) -> None:
        self.response = response
        self.calls = 0
        self.usage = usage or {
            "input_tokens": 100,
            "cached_input_tokens": 0,
            "output_tokens": 20,
            "reasoning_output_tokens": 0,
            "total_tokens": 120,
        }

    def complete(self, prompt: str) -> str:
        self.calls += 1
        record_provider_usage(
            self,
            model=self.model,
            usage=self.usage,
            status="success",
            started_at=NOW.isoformat(),
            duration_ms=1,
        )
        return json.dumps(self.response)


def policy(
    *,
    detector_input_tokens: int = 150_000,
    policy_version: int = 1,
) -> OperationsPolicy:
    return OperationsPolicy.from_dict(
        {
            "schema_version": 1,
            "policy_id": "shadow-test",
            "policy_version": policy_version,
            "mode": "shadow_read_only",
            "operator": {
                "timezone": "America/Los_Angeles",
                "calendar": {
                    "account_key": "calendar.personal",
                    "email": "owner@example.com",
                },
                "gmail": {
                    "account_key": "gmail.personal",
                    "email": "owner@example.com",
                },
            },
            "sources": {
                "calendar": {
                    "enabled": True,
                    "account_key": "calendar.personal",
                    "calendar_id": "primary",
                    "ownership": "owned",
                    "scope": CALENDAR_OWNED_READ_SCOPE,
                },
                "gmail": {
                    "enabled": True,
                    "account_key": "gmail.personal",
                    "scope": GMAIL_READ_SCOPE,
                    "content_access_approved": True,
                },
            },
            "privacy": {
                "raw_cache_days": 7,
                "normalized_evidence_days": 30,
                "fetch_attachments": False,
                "strip_quoted_history": True,
                "external_writes": False,
            },
            "budgets": {
                "calendar": {"requests_per_day": 500},
                "gmail": {
                    "api_requests_per_day": 1200,
                    "detector_calls_per_day": 100,
                    "detector_input_tokens_per_day": detector_input_tokens,
                    "detector_total_tokens_per_day": max(
                        detector_input_tokens,
                        180_000,
                    ),
                },
            },
            "responsibility": {
                "owned": ["personal administration"],
                "shared": [],
                "adjacent": [],
                "out_of_area_action": "demote",
                "unknown_action": "surface_unknown",
                "direct_obligations_remain_eligible": True,
                "high_consequence": {
                    "categories": [
                        "legal",
                        "financial",
                        "security",
                        "safety",
                        "travel",
                        "direct_commitment",
                    ],
                    "remain_eligible": True,
                    "never_auto_suppress": True,
                },
            },
        }
    )


def calendar_result() -> CalendarFetchResult:
    raw = {
        "id": "event-1",
        "etag": '"calendar-etag-1"',
        "status": "confirmed",
        "summary": "Planning review",
        "updated": "2026-07-13T14:00:00Z",
        "iCalUID": "event-1@example.com",
        "start": {"dateTime": "2026-07-13T09:00:00-07:00"},
        "end": {"dateTime": "2026-07-13T10:00:00-07:00"},
    }
    event = NormalizedCalendarEvent(
        event_id="event-1",
        etag='"calendar-etag-1"',
        source_revision='"calendar-etag-1"',
        status="confirmed",
        title="Planning review",
        details=None,
        location=None,
        created_at="2026-07-01T10:00:00Z",
        updated_at="2026-07-13T14:00:00Z",
        starts_at="2026-07-13T09:00:00-07:00",
        start_date=None,
        ends_at="2026-07-13T10:00:00-07:00",
        end_date=None,
        source_timezone="America/Los_Angeles",
        recurrence=(),
        recurring_event_id=None,
        original_start_time=None,
        original_start_date=None,
        sequence=0,
        visibility="default",
        transparency="opaque",
        event_type="default",
        organizer_email="owner@example.com",
        organizer_self=True,
        attendee_count=1,
        attendee_response="accepted",
        cancelled=False,
        ical_uid="event-1@example.com",
    )
    return CalendarFetchResult(
        mode="full",
        raw_events=(raw,),
        events=(event,),
        next_sync_token="calendar-sync-1",
        reset_required=False,
        coverage_complete=True,
        pages_fetched=1,
    )


def gmail_result(*, body: str = "Can you please send the board deck?") -> GmailFetchResult:
    normalized_message = NormalizedGmailMessage(
        message_id="message-1",
        thread_id="thread-1",
        internal_date="1783960000000",
        timestamp="2026-07-13T16:26:40+00:00",
        from_addresses=("person@example.com",),
        to_addresses=("owner@example.com",),
        cc_addresses=(),
        subject="Board deck",
        date_header=None,
        internet_message_id="<message-1@example.com>",
        in_reply_to=None,
        references=(),
        label_ids=("INBOX", "UNREAD"),
        outgoing=False,
        operator_authored=False,
        body=body,
        body_kind="text/plain",
        attachment_count=0,
        quoted_chars_removed=0,
        truncated=False,
    )
    normalized = NormalizedGmailThread(
        thread_id="thread-1",
        history_id="gmail-history-thread-1",
        source_revision="gmail-history-thread-1",
        subject="Board deck",
        created_at="2026-07-13T16:26:40+00:00",
        updated_at="2026-07-13T16:26:40+00:00",
        message_class="human",
        messages=(normalized_message,),
        body_chars=len(body),
        attachment_count=0,
        quoted_chars_removed=0,
        truncated=False,
    )
    raw = {
        "id": "thread-1",
        "historyId": "gmail-history-thread-1",
        "messages": [],
    }
    return GmailFetchResult(
        mode="full",
        raw_threads=(raw,),
        threads=(normalized,),
        changed_thread_ids=("thread-1",),
        missing_thread_ids=(),
        next_history_id="mailbox-history-1",
        reset_required=False,
        coverage_complete=True,
        pages_fetched=1,
    )


def detector_response() -> dict:
    return {
        "threads": [
            {
                "thread_id": "thread-1",
                "decision": "candidates",
                "confidence": 0.95,
                "candidates": [
                    {
                        "detector_key": "send-board-deck",
                        "operation": "create",
                        "kind": "commitment",
                        "title": "Send the board deck",
                        "owner": "operator",
                        "priority": "high",
                        "confidence": 0.95,
                        "due_at": None,
                        "starts_at": None,
                        "ends_at": None,
                        "expires_at": None,
                        "counterparty": "person@example.com",
                        "evidence_message_ids": ["message-1"],
                        "evidence": [
                            {
                                "message_id": "message-1",
                                "quote": "Can you please send the board deck?",
                            }
                        ],
                        "handled_verdict": "needs_action",
                        "handled_confidence": 0.96,
                        "reason": "Direct unanswered request.",
                        "reconciliation_status": "confirmed",
                    }
                ],
            }
        ]
    }


def test_live_shadow_run_writes_only_ops_and_private_evidence_and_builds_today(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = OperationalService(paths, writer_guard=lambda: None)
    calendar = FakeCalendarReader(calendar_result())
    gmail = FakeGmailReader(gmail_result())
    provider = FakeProvider(detector_response())
    runner = ShadowTrialRunner(
        paths,
        service,
        calendar_reader=calendar,
        gmail_reader=gmail,
        llm_provider=provider,
        now=lambda: NOW,
    )

    result = runner.run_live(policy=policy())

    assert result.run["status"] == "complete"
    assert result.briefing["status"] == "complete"
    assert len(result.briefing["sections"]["focus"]) == 1
    assert result.briefing["sections"]["focus"][0]["title"] == "Send the board deck"
    assert result.briefing["sections"]["focus"][0]["provider_route"].startswith(
        "https://mail.google.com/"
    )
    assert len(result.briefing["sections"]["now_and_next"]) == 1
    assert provider.calls == 1
    assert gmail.calls[0]["query"] == (
        "newer_than:7d {newer_than:2d is:unread} -in:spam -in:trash"
    )
    assert not paths.sqlite_path.exists()
    with operational_connection(paths.ops_sqlite_path, write=False) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ops_items").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM ops_shadow_decisions").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM ops_briefing_snapshots").fetchone()[0] == 1
        detector_reservations = {
            str(row["metric"]): int(row["amount"])
            for row in conn.execute(
                """
                SELECT metric, SUM(amount) AS amount FROM ops_budget_reservations
                WHERE source_type = 'gmail'
                  AND metric IN ('detector_input_tokens', 'detector_total_tokens')
                GROUP BY metric
                """
            ).fetchall()
        }
        detector_reserved = detector_reservations["detector_input_tokens"]
    assert detector_reserved == result.usage["gmail"]["estimated_input_tokens"]
    assert detector_reserved > DETECTOR_INPUT_TOKEN_OVERHEAD_RESERVE
    assert detector_reservations["detector_total_tokens"] == (
        detector_reserved + DETECTOR_OUTPUT_TOKEN_RESERVE
    )
    assert result.usage["gmail"]["pre_reserved_calls"] == 1
    assert result.usage["gmail"]["pre_reserved_input_tokens"] == detector_reserved
    assert result.usage["gmail"]["pre_reserved_total_tokens"] == (
        detector_reserved + DETECTOR_OUTPUT_TOKEN_RESERVE
    )
    assert result.usage["gmail"]["actual_input_tokens"] == 100
    assert result.usage["gmail"]["actual_total_tokens"] == 120
    assert result.usage["gmail"]["actual_usage_complete"] is True
    assert result.usage["gmail"]["reported_provider_calls"] == 1
    assert result.usage["gmail"]["unreported_provider_calls"] == 0
    calendar_cursor = get_source_cursor(
        paths.ops_sqlite_path,
        "calendar",
        "calendar.personal",
        "primary",
    )
    gmail_cursor = get_source_cursor(
        paths.ops_sqlite_path,
        "gmail",
        "gmail.personal",
        "mailbox",
    )
    assert calendar_cursor["cursor"] == "calendar-sync-1"
    assert gmail_cursor["cursor"] == "mailbox-history-1"
    cache = GoogleEvidenceCache.for_paths(paths)
    assert cache.read_normalized(
        "calendar", "calendar.personal:primary:event-1"
    )["event_id"] == "event-1"
    assert cache.read_normalized(
        "gmail", "gmail.personal:thread-1"
    )["thread_id"] == "thread-1"


def test_detector_budget_deferral_keeps_provider_cursor_and_all_clear_blocked(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = OperationalService(paths, writer_guard=lambda: None)
    provider = FakeProvider(detector_response())
    runner = ShadowTrialRunner(
        paths,
        service,
        calendar_reader=FakeCalendarReader(calendar_result()),
        gmail_reader=FakeGmailReader(
            replace(
                gmail_result(body="Can you review? " + "x" * 5000),
                next_history_id=None,
                coverage_complete=False,
                continuation_page_token="provider-page-2",
                baseline_history_id="provider-baseline",
            )
        ),
        llm_provider=provider,
        now=lambda: NOW,
    )

    result = runner.run_live(
        sources=("gmail",),
        policy=policy(detector_input_tokens=1),
    )

    assert result.run["status"] == "partial"
    assert result.briefing["status"] == "partial"
    assert result.briefing["all_clear"] is False
    assert result.coverage["gmail"]["deferred_count"] == 1
    assert result.coverage["gmail"]["cursor_advanced"] is False
    assert provider.calls == 0
    cursor = get_source_cursor(
        paths.ops_sqlite_path,
        "gmail",
        "gmail.personal",
        "mailbox",
    )
    assert cursor["cursor"] is None
    assert cursor["generation"] == 1
    assert cursor["metadata"]["continuation_page_token"] is None
    assert cursor["metadata"]["baseline_history_id"] is None


def test_observed_provider_usage_above_reserve_is_appended_without_refunds(
    tmp_path: Path,
) -> None:
    class MultiAttemptProvider:
        name = "multi-attempt"
        model = "multi-attempt"

        def __init__(self) -> None:
            self.calls = 0

        def complete(self, _prompt: str) -> str:
            self.calls += 1
            for input_tokens, output_tokens in ((10_000, 1_000), (12_000, 1_000)):
                record_provider_usage(
                    self,
                    model=self.model,
                    usage={
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "total_tokens": input_tokens + output_tokens,
                    },
                    status="success",
                    started_at=NOW.isoformat(),
                    duration_ms=1,
                )
            return json.dumps(detector_response())

    paths = BrainPaths.from_value(tmp_path / "brain")
    service = OperationalService(paths, writer_guard=lambda: None)
    provider = MultiAttemptProvider()
    result = ShadowTrialRunner(
        paths,
        service,
        gmail_reader=FakeGmailReader(gmail_result()),
        llm_provider=provider,
        now=lambda: NOW,
    ).run_live(sources=("gmail",), policy=policy())

    gmail_usage = result.usage["gmail"]
    assert result.run["status"] == "complete"
    assert gmail_usage["pre_reserved_calls"] == 1
    assert gmail_usage["actual_input_tokens"] == 22_000
    assert gmail_usage["actual_total_tokens"] == 24_000
    assert gmail_usage["reported_provider_calls"] == 2
    assert gmail_usage["unreported_provider_calls"] == 0
    assert gmail_usage["actual_usage_complete"] is True
    durable = daily_budget_usage(
        paths.ops_sqlite_path,
        local_day="2026-07-13",
    )["gmail"]
    assert durable["detector_calls"] == 2
    assert durable["detector_input_tokens"] == 22_000
    assert durable["detector_total_tokens"] == 24_000


def test_observed_provider_overage_is_persisted_latched_and_blocks_later_calls(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = OperationalService(paths, writer_guard=lambda: None)
    service.initialize()
    run = service.start_shadow_run(
        mode="live",
        requested_sources=("gmail",),
        policy_version=policy().version_ref,
        started_at=NOW.isoformat(),
    )
    provider = FakeProvider(
        {},
        usage={
            "input_tokens": 200_000,
            "output_tokens": 100,
            "total_tokens": 200_100,
        },
    )
    wrapped = shadow_trial_module._DurablyBudgetedDetectorProvider(
        provider,
        operational_service=service,
        policy=policy(),
        run_id=str(run["id"]),
        started=NOW,
    )

    with pytest.raises(DailyBudgetExceeded, match="observed detector usage"):
        wrapped.complete("small request")
    with pytest.raises(DailyBudgetExceeded, match="observed detector usage"):
        wrapped.complete("must not reach the provider")

    assert provider.calls == 1
    assert wrapped.usage_stats()["actual_input_tokens"] == 200_000
    durable = daily_budget_usage(
        paths.ops_sqlite_path,
        local_day="2026-07-13",
    )["gmail"]
    assert durable["detector_calls"] == 1
    assert durable["detector_input_tokens"] == 200_000
    assert durable["detector_total_tokens"] == 200_100


def test_missing_provider_usage_fails_closed_and_latches_wrapper(
    tmp_path: Path,
) -> None:
    class UnreportedProvider:
        name = "unreported"
        model = "unreported"

        def __init__(self) -> None:
            self.calls = 0

        def complete(self, _prompt: str) -> str:
            self.calls += 1
            return "{}"

    paths = BrainPaths.from_value(tmp_path / "brain")
    service = OperationalService(paths, writer_guard=lambda: None)
    service.initialize()
    run = service.start_shadow_run(
        mode="live",
        requested_sources=("gmail",),
        policy_version=policy().version_ref,
        started_at=NOW.isoformat(),
    )
    provider = UnreportedProvider()
    wrapped = shadow_trial_module._DurablyBudgetedDetectorProvider(
        provider,
        operational_service=service,
        policy=policy(),
        run_id=str(run["id"]),
        started=NOW,
    )

    with pytest.raises(DailyBudgetExceeded, match="did not report complete"):
        wrapped.complete("small request")
    with pytest.raises(DailyBudgetExceeded, match="did not report complete"):
        wrapped.complete("must not reach the provider")

    assert provider.calls == 1
    stats = wrapped.usage_stats()
    assert stats["pre_reserved_calls"] == 1
    assert stats["pre_reserved_input_tokens"] > 0
    assert stats["pre_reserved_total_tokens"] > stats["pre_reserved_input_tokens"]
    assert stats["actual_input_tokens"] == 0
    assert stats["actual_total_tokens"] == 0
    assert stats["actual_usage_complete"] is False
    assert stats["reported_provider_calls"] == 0
    assert stats["unreported_provider_calls"] == 1


def test_missing_gmail_thread_advances_cursor_but_keeps_item_visible_uncertain(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = OperationalService(paths, writer_guard=lambda: None)
    first = ShadowTrialRunner(
        paths,
        service,
        gmail_reader=FakeGmailReader(gmail_result()),
        llm_provider=FakeProvider(detector_response()),
        now=lambda: NOW,
    )
    first.run_live(sources=("gmail",), policy=policy())

    missing = GmailFetchResult(
        mode="incremental",
        raw_threads=(),
        threads=(),
        changed_thread_ids=("thread-1",),
        missing_thread_ids=("thread-1",),
        next_history_id="mailbox-history-2",
        reset_required=False,
        coverage_complete=True,
        pages_fetched=1,
    )
    second = ShadowTrialRunner(
        paths,
        service,
        gmail_reader=FakeGmailReader(missing),
        llm_provider=FakeProvider([]),
        now=lambda: NOW + timedelta(hours=1),
    ).run_live(sources=("gmail",), policy=policy())

    assert second.coverage["gmail"]["status"] == "partial"
    assert second.coverage["gmail"]["cursor_advanced"] is True
    cursor = get_source_cursor(
        paths.ops_sqlite_path,
        "gmail",
        "gmail.personal",
        "mailbox",
    )
    assert cursor["cursor"] == "mailbox-history-2"
    assert cursor["last_success_at"] == NOW.isoformat()
    with operational_connection(paths.ops_sqlite_path, write=False) as conn:
        item = conn.execute(
            "SELECT id, state, confidence FROM ops_items WHERE source_type = 'gmail'"
        ).fetchone()
    assert item["state"] == "active"
    assert item["confidence"] == 0.1
    assert latest_handled_assessments(paths.ops_sqlite_path)[item["id"]][
        "verdict"
    ] == "unknown"
    decisions = list_shadow_decisions(
        paths.ops_sqlite_path,
        run_id=str(second.run["id"]),
    )
    assert [entry["reason_code"] for entry in decisions] == [
        "gmail_thread_missing"
    ]

    third = ShadowTrialRunner(
        paths,
        service,
        gmail_reader=FakeGmailReader(
            replace(
                missing,
                changed_thread_ids=(),
                missing_thread_ids=(),
                next_history_id="mailbox-history-3",
            )
        ),
        llm_provider=FakeProvider([]),
        now=lambda: NOW + timedelta(hours=2),
    ).run_live(sources=("gmail",), policy=policy())
    cursor = get_source_cursor(
        paths.ops_sqlite_path,
        "gmail",
        "gmail.personal",
        "mailbox",
    )

    assert third.coverage["gmail"]["status"] == "partial"
    assert third.coverage["gmail"]["deferred_count"] == 1
    assert cursor["cursor"] == "mailbox-history-3"
    assert cursor["last_success_at"] == NOW.isoformat()


def test_gmail_same_provider_revision_restores_without_another_model_call(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = OperationalService(paths, writer_guard=lambda: None)
    ShadowTrialRunner(
        paths,
        service,
        gmail_reader=FakeGmailReader(gmail_result()),
        llm_provider=FakeProvider(detector_response()),
        now=lambda: NOW,
    ).run_live(sources=("gmail",), policy=policy())
    ShadowTrialRunner(
        paths,
        service,
        gmail_reader=FakeGmailReader(
            GmailFetchResult(
                mode="incremental",
                raw_threads=(),
                threads=(),
                changed_thread_ids=("thread-1",),
                missing_thread_ids=("thread-1",),
                next_history_id="mailbox-history-2",
                reset_required=False,
                coverage_complete=True,
                pages_fetched=1,
            )
        ),
        llm_provider=FakeProvider([]),
        now=lambda: NOW + timedelta(hours=1),
    ).run_live(sources=("gmail",), policy=policy())
    provider = FakeProvider(detector_response())

    result = ShadowTrialRunner(
        paths,
        service,
        gmail_reader=FakeGmailReader(
            replace(
                gmail_result(),
                mode="incremental",
                next_history_id="mailbox-history-3",
            )
        ),
        llm_provider=provider,
        now=lambda: NOW + timedelta(hours=2),
    ).run_live(sources=("gmail",), policy=policy())

    assert provider.calls == 0
    assert result.coverage["gmail"]["status"] == "complete"
    with operational_connection(paths.ops_sqlite_path, write=False) as conn:
        item = conn.execute(
            """
            SELECT i.id, i.current_observation_id, i.confidence, i.metadata,
                   o.source_revision
            FROM ops_items i
            JOIN ops_observations o ON o.id = i.current_observation_id
            WHERE i.source_type = 'gmail'
            """
        ).fetchone()
        observations = conn.execute(
            """
            SELECT id, source_revision, source_order
            FROM ops_observations
            WHERE source_type = 'gmail'
            ORDER BY source_order, source_revision
            """
        ).fetchall()
        authority_event = conn.execute(
            """
            SELECT metadata FROM ops_item_events
            WHERE item_id = ? ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (item["id"],),
        ).fetchone()
    assert item["source_revision"] == "gmail-history-thread-1"
    assert item["confidence"] == 0.95
    assert json.loads(item["metadata"])["reconciliation_status"] == "confirmed"
    assert len(observations) == 2
    provider_observation, revalidation_observation = observations
    assert provider_observation["source_revision"] == "gmail-history-thread-1"
    assert provider_observation["source_order"] == 1
    assert str(revalidation_observation["source_revision"]).startswith(
        "gmail-revalidation-"
    )
    assert revalidation_observation["source_order"] == 2
    assert item["current_observation_id"] == provider_observation["id"]
    assert json.loads(authority_event["metadata"])["authority_reapplied"] is True


def test_calendar_reset_advances_sync_token_and_marks_unseen_event_ambiguous(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = OperationalService(paths, writer_guard=lambda: None)
    ShadowTrialRunner(
        paths,
        service,
        calendar_reader=FakeCalendarReader(calendar_result()),
        now=lambda: NOW,
    ).run_live(sources=("calendar",), policy=policy())

    reset = CalendarFetchResult(
        mode="full",
        raw_events=(),
        events=(),
        next_sync_token="calendar-sync-2",
        reset_required=True,
        coverage_complete=True,
        pages_fetched=1,
    )
    result = ShadowTrialRunner(
        paths,
        service,
        calendar_reader=FakeCalendarReader(reset),
        now=lambda: NOW + timedelta(hours=1),
    ).run_live(sources=("calendar",), policy=policy())

    assert result.coverage["calendar"]["status"] == "partial"
    assert result.coverage["calendar"]["cursor_advanced"] is True
    cursor = get_source_cursor(
        paths.ops_sqlite_path,
        "calendar",
        "calendar.personal",
        "primary",
    )
    assert cursor["cursor"] == "calendar-sync-2"
    assert cursor["last_success_at"] == NOW.isoformat()
    with operational_connection(paths.ops_sqlite_path, write=False) as conn:
        item = conn.execute(
            "SELECT state, confidence, metadata FROM ops_items "
            "WHERE source_type = 'calendar'"
        ).fetchone()
    assert item["state"] == "active"
    assert item["confidence"] == 0.1
    assert json.loads(item["metadata"])["reconciliation_status"] == "ambiguous"

    third = ShadowTrialRunner(
        paths,
        service,
        calendar_reader=FakeCalendarReader(
            replace(
                reset,
                mode="incremental",
                next_sync_token="calendar-sync-3",
                reset_required=False,
            )
        ),
        now=lambda: NOW + timedelta(hours=2),
    ).run_live(sources=("calendar",), policy=policy())
    cursor = get_source_cursor(
        paths.ops_sqlite_path,
        "calendar",
        "calendar.personal",
        "primary",
    )

    assert third.coverage["calendar"]["status"] == "partial"
    assert third.coverage["calendar"]["deferred_count"] == 1
    assert cursor["cursor"] == "calendar-sync-3"
    assert cursor["last_success_at"] == NOW.isoformat()


def test_calendar_same_provider_revision_restores_authority_after_revalidation(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = OperationalService(paths, writer_guard=lambda: None)
    ShadowTrialRunner(
        paths,
        service,
        calendar_reader=FakeCalendarReader(calendar_result()),
        now=lambda: NOW,
    ).run_live(sources=("calendar",), policy=policy())
    ShadowTrialRunner(
        paths,
        service,
        calendar_reader=FakeCalendarReader(
            CalendarFetchResult(
                mode="full",
                raw_events=(),
                events=(),
                next_sync_token="calendar-sync-2",
                reset_required=True,
                coverage_complete=True,
                pages_fetched=1,
            )
        ),
        now=lambda: NOW,
    ).run_live(sources=("calendar",), policy=policy())

    restored = replace(
        calendar_result(),
        mode="incremental",
        next_sync_token="calendar-sync-3",
    )
    result = ShadowTrialRunner(
        paths,
        service,
        calendar_reader=FakeCalendarReader(restored),
        now=lambda: NOW,
    ).run_live(sources=("calendar",), policy=policy())

    assert result.coverage["calendar"]["status"] == "complete"
    with operational_connection(paths.ops_sqlite_path, write=False) as conn:
        item = conn.execute(
            """
            SELECT i.confidence, i.metadata, o.source_revision
            FROM ops_items i
            JOIN ops_observations o ON o.id = i.current_observation_id
            WHERE i.source_type = 'calendar'
            """
        ).fetchone()
    assert item["source_revision"] == '"calendar-etag-1"'
    assert item["confidence"] == 1.0
    assert json.loads(item["metadata"])["reconciliation_status"] == "confirmed"


def test_confirming_calendar_revalidation_clears_partial_coverage(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = OperationalService(paths, writer_guard=lambda: None)
    ShadowTrialRunner(
        paths,
        service,
        calendar_reader=FakeCalendarReader(calendar_result()),
        now=lambda: NOW,
    ).run_live(sources=("calendar",), policy=policy())
    ShadowTrialRunner(
        paths,
        service,
        calendar_reader=FakeCalendarReader(
            CalendarFetchResult(
                mode="full",
                raw_events=(),
                events=(),
                next_sync_token="calendar-sync-2",
                reset_required=True,
                coverage_complete=True,
                pages_fetched=1,
            )
        ),
        now=lambda: NOW,
    ).run_live(sources=("calendar",), policy=policy())
    with operational_connection(paths.ops_sqlite_path, write=False) as conn:
        item_id = str(
            conn.execute(
                "SELECT id FROM ops_items WHERE source_type = 'calendar'"
            ).fetchone()["id"]
        )
    service.record_item_feedback(
        item_id,
        "confirm",
        idempotency_key="confirm-calendar-revalidation",
        created_at="2026-07-13T15:01:00+00:00",
    )

    result = ShadowTrialRunner(
        paths,
        service,
        calendar_reader=FakeCalendarReader(
            CalendarFetchResult(
                mode="incremental",
                raw_events=(),
                events=(),
                next_sync_token="calendar-sync-3",
                reset_required=False,
                coverage_complete=True,
                pages_fetched=1,
            )
        ),
        now=lambda: NOW,
    ).run_live(sources=("calendar",), policy=policy())

    assert result.coverage["calendar"]["status"] == "complete"
    assert result.coverage["calendar"]["deferred_count"] == 0


def test_calendar_revalidation_card_opens_retained_revision_evidence(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    policy_payload = default_operations_policy_payload(
        timezone_name="America/Los_Angeles",
        calendar_email="owner@example.com",
        gmail_email="owner@example.com",
    )
    active_policy = OperationsPolicy.from_dict(policy_payload)
    policy_path = operations_policy_path(paths)
    policy_path.parent.mkdir(parents=True, mode=0o700)
    policy_path.write_text(
        yaml.safe_dump(policy_payload, sort_keys=True),
        encoding="utf-8",
    )
    policy_path.chmod(0o600)
    service = OperationalService(paths, writer_guard=lambda: None)
    ShadowTrialRunner(
        paths,
        service,
        calendar_reader=FakeCalendarReader(calendar_result()),
        now=lambda: NOW,
    ).run_live(sources=("calendar",), policy=active_policy)

    reset = CalendarFetchResult(
        mode="full",
        raw_events=(),
        events=(),
        next_sync_token="calendar-sync-2",
        reset_required=True,
        coverage_complete=True,
        pages_fetched=1,
    )
    result = ShadowTrialRunner(
        paths,
        service,
        calendar_reader=FakeCalendarReader(reset),
        now=lambda: NOW,
    ).run_live(sources=("calendar",), policy=active_policy)

    card = result.briefing["sections"]["now_and_next"][0]
    route = urlsplit(card["local_evidence_route"])
    evidence = operations_evidence_payload(
        paths,
        parse_qs(route.query, keep_blank_values=True),
    )

    assert evidence["source_revision"] == '"calendar-etag-1"'
    assert evidence["evidence"]["event_id"] == "event-1"


def test_resumed_gmail_page_is_retried_when_detector_cannot_finish(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = OperationalService(paths, writer_guard=lambda: None)
    service.initialize()
    service.save_source_cursor(
        "gmail",
        "gmail.personal",
        "mailbox",
        source_type="gmail",
        cursor="history-9",
        metadata={
            "coverage_status": "partial",
            "continuation_page_token": "provider-page-2",
            "continuation_mode": "incremental",
            "continuation_history_id": "history-11",
            "pending_thread_ids": json.dumps(["pending-old"]),
        },
        updated_at=NOW.isoformat(),
    )
    gmail = FakeGmailReader(
        replace(
            gmail_result(body="Can you review? " + "x" * 5000),
            next_history_id=None,
            coverage_complete=False,
            continuation_page_token="provider-page-3",
            baseline_history_id=None,
            pending_thread_ids=("pending-next",),
            continuation_history_id="history-12",
        )
    )
    runner = ShadowTrialRunner(
        paths,
        service,
        gmail_reader=gmail,
        llm_provider=FakeProvider(detector_response()),
        now=lambda: NOW,
    )

    result = runner.run_live(
        sources=("gmail",),
        policy=policy(detector_input_tokens=1),
    )

    assert result.coverage["gmail"]["cursor_advanced"] is False
    assert gmail.calls[0]["continuation_page_token"] == "provider-page-2"
    assert gmail.calls[0]["pending_thread_ids"] == ("pending-old",)
    assert gmail.calls[0]["continuation_history_id"] == "history-11"
    cursor = get_source_cursor(
        paths.ops_sqlite_path,
        "gmail",
        "gmail.personal",
        "mailbox",
    )
    assert cursor["metadata"]["continuation_page_token"] == "provider-page-2"
    assert cursor["metadata"]["baseline_history_id"] is None
    assert json.loads(cursor["metadata"]["pending_thread_ids"]) == ["pending-old"]
    assert cursor["metadata"]["continuation_history_id"] == "history-11"


def test_calendar_410_full_rebuild_resumes_without_the_expired_sync_token(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = OperationalService(paths, writer_guard=lambda: None)
    ShadowTrialRunner(
        paths,
        service,
        calendar_reader=FakeCalendarReader(calendar_result()),
        now=lambda: NOW,
    ).run_live(sources=("calendar",), policy=policy())
    reset_page = replace(
        calendar_result(),
        next_sync_token=None,
        reset_required=True,
        coverage_complete=False,
        continuation_page_token="calendar-reset-page-2",
    )
    reset_reader = FakeCalendarReader(reset_page)
    ShadowTrialRunner(
        paths,
        service,
        calendar_reader=reset_reader,
        now=lambda: NOW,
    ).run_live(sources=("calendar",), policy=policy())
    partial_cursor = get_source_cursor(
        paths.ops_sqlite_path,
        "calendar",
        "calendar.personal",
        "primary",
    )
    assert reset_reader.calls[0]["sync_token"] == "calendar-sync-1"
    assert partial_cursor["cursor"] is None
    assert partial_cursor["metadata"]["continuation_mode"] == "full"
    assert partial_cursor["metadata"]["reset_rebuild"] is True
    resume_reader = FakeCalendarReader(
        replace(
            calendar_result(),
            raw_events=(),
            events=(),
            next_sync_token="calendar-sync-2",
        )
    )

    result = ShadowTrialRunner(
        paths,
        service,
        calendar_reader=resume_reader,
        now=lambda: NOW + timedelta(hours=1),
    ).run_live(sources=("calendar",), policy=policy())

    assert resume_reader.calls[0]["sync_token"] is None
    assert (
        resume_reader.calls[0]["continuation_page_token"]
        == "calendar-reset-page-2"
    )
    assert resume_reader.calls[0]["time_min"] == reset_reader.calls[0]["time_min"]
    assert resume_reader.calls[0]["time_max"] == reset_reader.calls[0]["time_max"]
    assert result.coverage["calendar"]["status"] == "complete"
    completed_cursor = get_source_cursor(
        paths.ops_sqlite_path,
        "calendar",
        "calendar.personal",
        "primary",
    )
    assert completed_cursor["cursor"] == "calendar-sync-2"
    assert completed_cursor["last_success_at"] == (
        NOW + timedelta(hours=1)
    ).isoformat()
    assert completed_cursor["metadata"]["reset_rebuild"] is False


def test_gmail_404_full_rebuild_resumes_without_expired_history_or_false_absence(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = OperationalService(paths, writer_guard=lambda: None)
    first = ShadowTrialRunner(
        paths,
        service,
        gmail_reader=FakeGmailReader(gmail_result()),
        llm_provider=FakeProvider(detector_response()),
        now=lambda: NOW,
    ).run_live(sources=("gmail",), policy=policy())
    item_id = first.briefing["sections"]["focus"][0]["item_id"]
    reset_page = replace(
        gmail_result(),
        next_history_id=None,
        reset_required=True,
        coverage_complete=False,
        continuation_page_token="gmail-reset-page-2",
        baseline_history_id="gmail-reset-baseline",
    )
    reset_reader = FakeGmailReader(reset_page)
    ShadowTrialRunner(
        paths,
        service,
        gmail_reader=reset_reader,
        llm_provider=FakeProvider(detector_response()),
        now=lambda: NOW + timedelta(hours=1),
    ).run_live(sources=("gmail",), policy=policy())
    partial_cursor = get_source_cursor(
        paths.ops_sqlite_path,
        "gmail",
        "gmail.personal",
        "mailbox",
    )
    assert reset_reader.calls[0]["history_id"] == "mailbox-history-1"
    assert partial_cursor["cursor"] is None
    assert partial_cursor["metadata"]["continuation_mode"] == "full"
    assert partial_cursor["metadata"]["reset_rebuild"] is True
    assert partial_cursor["last_success_at"] == NOW.isoformat()
    final_page = replace(
        gmail_result(),
        raw_threads=(),
        threads=(),
        changed_thread_ids=(),
        next_history_id="gmail-reset-baseline",
        baseline_history_id="gmail-reset-baseline",
    )
    resume_reader = FakeGmailReader(final_page)

    result = ShadowTrialRunner(
        paths,
        service,
        gmail_reader=resume_reader,
        llm_provider=FakeProvider(detector_response()),
        now=lambda: NOW + timedelta(hours=2),
    ).run_live(sources=("gmail",), policy=policy())

    assert resume_reader.calls[0]["history_id"] is None
    assert resume_reader.calls[0]["continuation_page_token"] == "gmail-reset-page-2"
    assert resume_reader.calls[0]["baseline_history_id"] == "gmail-reset-baseline"
    assert result.coverage["gmail"]["status"] == "complete"
    completed_cursor = get_source_cursor(
        paths.ops_sqlite_path,
        "gmail",
        "gmail.personal",
        "mailbox",
    )
    assert completed_cursor["cursor"] == "gmail-reset-baseline"
    assert completed_cursor["last_success_at"] == (
        NOW + timedelta(hours=2)
    ).isoformat()
    assert completed_cursor["metadata"]["reset_rebuild"] is False
    assert completed_cursor["metadata"]["reset_seen_item_ids"] is None
    assert latest_handled_assessments(paths.ops_sqlite_path)[item_id]["verdict"] == "needs_action"
    assert not any(
        decision["reason_code"] == "gmail_history_reset_revalidation"
        for decision in list_shadow_decisions(paths.ops_sqlite_path, limit=20)
    )


def test_declined_and_cancelled_calendar_occurrences_never_enter_today(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    raw_events = (
        {
            "id": "weekly_20260713T160000Z",
            "etag": '"accepted"',
            "status": "confirmed",
            "summary": "Accepted weekly review",
            "recurringEventId": "weekly",
            "originalStartTime": {"dateTime": "2026-07-13T09:00:00-07:00"},
            "start": {"dateTime": "2026-07-13T09:00:00-07:00"},
            "end": {"dateTime": "2026-07-13T09:30:00-07:00"},
            "attendees": [{"self": True, "responseStatus": "accepted"}],
        },
        {
            "id": "weekly_20260713T170000Z",
            "etag": '"declined"',
            "status": "confirmed",
            "summary": "Declined weekly review",
            "recurringEventId": "weekly-declined",
            "originalStartTime": {"dateTime": "2026-07-13T10:00:00-07:00"},
            "start": {"dateTime": "2026-07-13T10:00:00-07:00"},
            "end": {"dateTime": "2026-07-13T10:30:00-07:00"},
            "attendees": [{"self": True, "responseStatus": "declined"}],
        },
        {
            "id": "weekly_20260713T180000Z",
            "etag": '"cancelled"',
            "status": "cancelled",
            "summary": "Cancelled weekly review",
            "recurringEventId": "weekly-cancelled",
            "originalStartTime": {"dateTime": "2026-07-13T11:00:00-07:00"},
        },
    )
    result = CalendarFetchResult(
        mode="full",
        raw_events=raw_events,
        events=tuple(normalize_calendar_event(value) for value in raw_events),
        next_sync_token="calendar-sync-statuses",
        reset_required=False,
        coverage_complete=True,
        pages_fetched=1,
    )
    runner = ShadowTrialRunner(
        paths,
        OperationalService(paths, writer_guard=lambda: None),
        calendar_reader=FakeCalendarReader(result),
        now=lambda: NOW,
    )

    trial = runner.run_live(sources=("calendar",), policy=policy())

    today_titles = {
        card["title"] for card in trial.briefing["sections"]["now_and_next"]
    }
    assert today_titles == {"Accepted weekly review"}
    suppressed_titles = {
        card["title"] for card in trial.briefing["sections"]["suppressed"]
    }
    assert suppressed_titles == {
        "Declined weekly review",
        "Cancelled weekly review",
    }
    with operational_connection(paths.ops_sqlite_path, write=False) as conn:
        states = conn.execute(
            "SELECT title, state FROM ops_items ORDER BY title"
        ).fetchall()
    assert {str(row["title"]): str(row["state"]) for row in states} == {
        "Accepted weekly review": "active",
        "Cancelled weekly review": "cancelled",
        "Declined weekly review": "cancelled",
    }


def test_missing_gmail_thread_keeps_existing_item_visible_and_advances_checkpoint(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = OperationalService(paths, writer_guard=lambda: None)
    first = ShadowTrialRunner(
        paths,
        service,
        gmail_reader=FakeGmailReader(gmail_result()),
        llm_provider=FakeProvider(detector_response()),
        now=lambda: NOW,
    ).run_live(sources=("gmail",), policy=policy())
    item_id = first.briefing["sections"]["focus"][0]["item_id"]
    missing = replace(
        gmail_result(),
        mode="incremental",
        raw_threads=(),
        threads=(),
        changed_thread_ids=("thread-1",),
        missing_thread_ids=("thread-1",),
        next_history_id="mailbox-history-2",
    )

    second = ShadowTrialRunner(
        paths,
        service,
        gmail_reader=FakeGmailReader(missing),
        llm_provider=FakeProvider(detector_response()),
        now=lambda: NOW,
    ).run_live(sources=("gmail",), policy=policy())

    assert second.run["status"] == "partial"
    assert second.coverage["gmail"]["missing_thread_count"] == 1
    assert second.coverage["gmail"]["cursor_advanced"] is True
    assert item_id not in {
        card["item_id"] for card in second.briefing["sections"]["focus"]
    }
    assert item_id in {
        card["item_id"]
        for card in second.briefing["sections"]["low_confidence"]
    }
    assert latest_handled_assessments(paths.ops_sqlite_path)[item_id]["verdict"] == "unknown"
    assert any(
        decision["reason_code"] == "gmail_thread_missing"
        for decision in list_shadow_decisions(paths.ops_sqlite_path, limit=10)
    )
    cursor = get_source_cursor(
        paths.ops_sqlite_path,
        "gmail",
        "gmail.personal",
        "mailbox",
    )
    assert cursor["cursor"] == "mailbox-history-2"


def test_gmail_decision_cache_is_policy_and_detector_context_aware(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = OperationalService(paths, writer_guard=lambda: None)
    provider = FakeProvider(detector_response())
    for active_policy in (policy(), policy(), policy(policy_version=2)):
        ShadowTrialRunner(
            paths,
            service,
            gmail_reader=FakeGmailReader(gmail_result()),
            llm_provider=provider,
            now=lambda: NOW,
        ).run_live(sources=("gmail",), policy=active_policy)

    assert provider.calls == 2


def test_provider_error_fallback_is_cached_and_next_run_advances_cursor(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = OperationalService(paths, writer_guard=lambda: None)

    class FailingProvider:
        name = "failing"
        model = "failing"

        def __init__(self) -> None:
            self.calls = 0

        def complete(self, _prompt: str) -> str:
            self.calls += 1
            record_provider_usage(
                self,
                model=self.model,
                usage={
                    "input_tokens": 80,
                    "output_tokens": 5,
                    "total_tokens": 85,
                },
                status="error",
                started_at=NOW.isoformat(),
                duration_ms=1,
                error_type="RuntimeError",
            )
            raise RuntimeError("provider unavailable")

    provider = FailingProvider()
    first = ShadowTrialRunner(
        paths,
        service,
        gmail_reader=FakeGmailReader(gmail_result(body="FYI on the project update.")),
        llm_provider=provider,
        now=lambda: NOW,
    ).run_live(sources=("gmail",), policy=policy())

    first_decisions = list_shadow_decisions(
        paths.ops_sqlite_path,
        run_id=str(first.run["id"]),
    )
    assert first.coverage["gmail"]["status"] == "partial"
    assert first.coverage["gmail"]["cursor_advanced"] is False
    assert provider.calls == 1
    assert first.usage["gmail"]["actual_input_tokens"] == 80
    assert first.usage["gmail"]["actual_total_tokens"] == 85
    assert first.usage["gmail"]["actual_usage_complete"] is True
    assert first.usage["gmail"]["reported_provider_calls"] == 1
    assert first.usage["gmail"]["unreported_provider_calls"] == 0
    assert first_decisions[0]["disposition"] == "surfaced"
    assert first_decisions[0]["reason_code"] == "detector_error_uncertain"

    second = ShadowTrialRunner(
        paths,
        service,
        gmail_reader=FakeGmailReader(gmail_result(body="FYI on the project update.")),
        llm_provider=provider,
        now=lambda: NOW,
    ).run_live(sources=("gmail",), policy=policy())

    assert provider.calls == 1
    assert second.coverage["gmail"]["cursor_advanced"] is True
    cursor = get_source_cursor(
        paths.ops_sqlite_path,
        "gmail",
        "gmail.personal",
        "mailbox",
    )
    assert cursor["cursor"] == "mailbox-history-1"


def test_post_start_failure_closes_the_owned_shadow_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = OperationalService(paths, writer_guard=lambda: None)

    def fail_briefing(*_args, **_kwargs):
        raise RuntimeError("briefing synthesis failed")

    monkeypatch.setattr(
        shadow_trial_module,
        "build_operational_briefing",
        fail_briefing,
    )
    runner = ShadowTrialRunner(
        paths,
        service,
        calendar_reader=FakeCalendarReader(calendar_result()),
        now=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="briefing synthesis failed"):
        runner.run_live(sources=("calendar",), policy=policy())

    run = list_shadow_runs(paths.ops_sqlite_path, limit=1)[0]
    assert run["status"] == "failed"
    assert run["hard_stop_reason"] == "shadow_run_interrupted"
    assert "briefing synthesis failed" in run["error"]
    assert run["coverage"]["calendar"]["status"] == "complete"
    assert run["coverage"]["system"]["reason"] == "shadow_run_interrupted"
    assert run["counts"]["calendar_items"] == 1


def test_snapshot_failure_preserves_completed_source_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = OperationalService(paths, writer_guard=lambda: None)

    def fail_snapshot(*_args, **_kwargs):
        raise RuntimeError("briefing snapshot storage failed")

    monkeypatch.setattr(service, "save_briefing_snapshot", fail_snapshot)
    runner = ShadowTrialRunner(
        paths,
        service,
        calendar_reader=FakeCalendarReader(calendar_result()),
        now=lambda: NOW,
    )

    with pytest.raises(RuntimeError, match="briefing snapshot storage failed"):
        runner.run_live(sources=("calendar",), policy=policy())

    run = list_shadow_runs(paths.ops_sqlite_path, limit=1)[0]
    assert run["status"] == "failed"
    assert run["coverage"]["calendar"]["status"] == "complete"
    assert run["coverage"]["calendar"]["fresh_at"] == NOW.isoformat()
    assert run["coverage"]["system"]["status"] == "unavailable"
    assert run["counts"]["calendar_items"] == 1
    assert "briefing snapshot storage failed" in run["error"]


def test_restarted_controller_presents_and_recovers_orphaned_run(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = OperationalService(paths, writer_guard=lambda: None)
    service.initialize()
    orphan = service.start_shadow_run(
        mode="live",
        requested_sources=("calendar", "gmail"),
        policy_version="operations-v1@1",
        started_at=NOW.isoformat(),
    )
    controller = ShadowTrialController(paths, service)

    status = controller.status()

    assert status["status"] == "failed"
    assert status["run_id"] == orphan["id"]
    assert "interrupted" in status["message"]
    recovered = service.interrupt_running_shadow_runs(
        interrupted_at=NOW.isoformat()
    )
    assert recovered[0]["status"] == "stopped"
    assert recovered[0]["hard_stop_reason"] == "daemon_restart_interrupted_run"


def test_shadow_completion_message_uses_natural_singular_and_plural() -> None:
    singular = _completion_message(
        {
            "status": "partial",
            "counts": {"calendar_items": 1, "gmail_items": 0},
        }
    )
    plural = _completion_message(
        {
            "status": "complete",
            "counts": {"calendar_items": 1, "gmail_items": 2},
        }
    )

    assert "with 1 item;" in singular
    assert "with 3 operational items." in plural
    assert "item(s)" not in singular + plural
