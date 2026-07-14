from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any

import pytest

from pkm_brain.google_api import GoogleAPIError
from pkm_brain.google_normalization import (
    normalize_calendar_event,
    normalize_gmail_message,
)
from pkm_brain.google_routes import (
    calendar_event_route,
    gmail_thread_route,
    local_google_evidence_route,
)
from pkm_brain.google_sources import (
    MAX_PENDING_THREAD_IDS,
    GoogleCalendarReader,
    GmailThreadReader,
    _validated_pending_thread_ids,
    calendar_event_is_inactive,
    calendar_occurrence_key,
)


class FakeClient:
    def __init__(self, replies: list[dict[str, Any] | Exception]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, dict[str, Any], int]] = []

    def get_json(
        self,
        relative_path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        quota_units: int = 1,
    ) -> dict[str, Any]:
        self.calls.append((relative_path, dict(params or {}), quota_units))
        if not self.replies:
            raise AssertionError(f"unexpected request: {relative_path}")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def gmail_thread(thread_id: str = "thread-1") -> dict[str, Any]:
    return {
        "id": thread_id,
        "historyId": "321",
        "messages": [
            {
                "id": "message-1",
                "threadId": thread_id,
                "internalDate": "1783969200000",
                "labelIds": ["SENT"],
                "payload": {
                    "mimeType": "multipart/mixed",
                    "headers": [
                        {"name": "From", "value": "Peter <peter@example.com>"},
                        {"name": "To", "value": "Teammate <team@example.com>"},
                        {"name": "Subject", "value": "Deck due Friday"},
                        {"name": "Message-ID", "value": "<message-1@example.com>"},
                    ],
                    "parts": [
                        {
                            "mimeType": "text/html",
                            "body": {"data": encoded("<p>HTML fallback</p>")},
                        },
                        {
                            "mimeType": "text/plain",
                            "body": {
                                "data": encoded(
                                    "Please send the deck.\n\nOn Sun, Jul 12, 2026 wrote:\nOld text"
                                )
                            },
                        },
                        {
                            "mimeType": "application/pdf",
                            "filename": "deck.pdf",
                            "body": {
                                "attachmentId": "attachment-1",
                                "data": encoded("private attachment bytes"),
                            },
                        },
                    ],
                },
            }
        ],
    }


def test_calendar_full_fetch_expands_weekly_occurrences_and_cancelled_exceptions() -> (
    None
):
    occurrence = {
        "id": "series-1_20260714T160000Z",
        "etag": '"etag-1"',
        "status": "confirmed",
        "summary": "Weekly review",
        "start": {
            "dateTime": "2026-07-14T09:00:00-07:00",
            "timeZone": "America/Los_Angeles",
        },
        "end": {"dateTime": "2026-07-14T09:30:00-07:00"},
        "recurringEventId": "series-1",
        "originalStartTime": {"dateTime": "2026-07-14T09:00:00-07:00"},
        "iCalUID": "weekly-review@example.com",
        "extendedProperties": {"private": {"secret": "not cached"}},
    }
    cancelled = {
        "id": "exception-1",
        "status": "cancelled",
        "recurringEventId": "series-1",
        "originalStartTime": {"dateTime": "2026-07-21T09:00:00-07:00"},
    }
    client = FakeClient(
        [
            {"items": [occurrence], "nextPageToken": "page-2"},
            {"items": [cancelled], "nextSyncToken": "sync-2"},
        ]
    )

    result = GoogleCalendarReader(client, page_size=10).fetch(
        time_min="2026-06-29T00:00:00Z",
        time_max="2026-10-12T00:00:00Z",
        timezone_name="America/Los_Angeles",
    )

    assert result.mode == "full"
    assert result.coverage_complete is True
    assert result.next_sync_token == "sync-2"
    assert result.events[0].recurrence == ()
    assert result.events[0].recurring_event_id == "series-1"
    assert result.events[0].original_start_time == "2026-07-14T09:00:00-07:00"
    assert result.events[0].ical_uid == "weekly-review@example.com"
    assert result.events[0].source_revision == '"etag-1"'
    assert result.events[1].cancelled is True
    assert result.events[1].recurring_event_id == "series-1"
    assert result.events[1].original_start_time == "2026-07-21T09:00:00-07:00"
    assert "extendedProperties" not in result.raw_events[0]
    assert result.api_requests == 2
    assert result.quota_units == 2
    assert client.calls[0] == (
        "calendars/primary/events",
        {
            "maxResults": 10,
            "showDeleted": "true",
            "singleEvents": "true",
            "timeMin": "2026-06-29T00:00:00Z",
            "timeMax": "2026-10-12T00:00:00Z",
            "timeZone": "America/Los_Angeles",
        },
        1,
    )
    assert client.calls[1][1]["pageToken"] == "page-2"


def test_calendar_expanded_exception_keeps_original_identity_and_decline() -> None:
    moved_declined = normalize_calendar_event(
        {
            "id": "series-1_20260721T160000Z",
            "etag": '"exception-etag"',
            "status": "confirmed",
            "summary": "Weekly review — moved",
            "start": {"dateTime": "2026-07-22T11:00:00-07:00"},
            "end": {"dateTime": "2026-07-22T11:30:00-07:00"},
            "recurringEventId": "series-1",
            "originalStartTime": {"dateTime": "2026-07-21T09:00:00-07:00"},
            "attendees": [
                {
                    "email": "operator@example.com",
                    "self": True,
                    "responseStatus": "declined",
                }
            ],
        }
    )

    assert moved_declined.recurring_event_id == "series-1"
    assert moved_declined.original_start_time == "2026-07-21T09:00:00-07:00"
    assert moved_declined.starts_at == "2026-07-22T11:00:00-07:00"
    assert moved_declined.attendee_response == "declined"
    assert moved_declined.cancelled is False
    assert calendar_occurrence_key(moved_declined) == (
        "series-1:2026-07-21T09:00:00-07:00"
    )
    assert calendar_event_is_inactive(moved_declined) is True


def test_calendar_410_returns_explicit_replacement_full_snapshot() -> None:
    client = FakeClient(
        [
            GoogleAPIError(410, "full sync required"),
            {"items": [{"id": "fresh", "status": "confirmed"}], "nextSyncToken": "new"},
        ]
    )

    result = GoogleCalendarReader(client).fetch(
        time_min="2026-06-29T00:00:00Z",
        time_max="2026-10-12T00:00:00Z",
        sync_token="expired",
        continuation_page_token="expired-page",
    )

    assert result.mode == "full"
    assert result.reset_required is True
    assert result.next_sync_token == "new"
    assert result.api_requests == 2
    assert result.quota_units == 2
    assert client.calls[0][1]["syncToken"] == "expired"
    assert client.calls[0][1]["pageToken"] == "expired-page"
    assert "timeMin" not in client.calls[0][1]
    assert client.calls[1][1]["timeMin"] == "2026-06-29T00:00:00Z"
    assert "pageToken" not in client.calls[1][1]


def test_calendar_reader_refuses_non_primary_calendar_and_partial_cursor() -> None:
    with pytest.raises(ValueError, match="calendarId=primary"):
        GoogleCalendarReader(FakeClient([]), calendar_id="shared@example.com")

    client = FakeClient([{"items": [{"id": "one"}], "nextPageToken": "more"}])
    result = GoogleCalendarReader(client, max_pages=1).fetch(
        time_min="2026-07-01T00:00:00Z",
        time_max="2026-08-01T00:00:00Z",
    )
    assert result.coverage_complete is False
    assert result.next_sync_token is None
    assert result.continuation_page_token == "more"
    assert result.api_requests == 1
    assert result.quota_units == 1

    resume_client = FakeClient(
        [{"items": [{"id": "two"}], "nextSyncToken": "sync-after-resume"}]
    )
    resumed = GoogleCalendarReader(resume_client, max_pages=1).fetch(
        time_min="2026-07-01T00:00:00Z",
        time_max="2026-08-01T00:00:00Z",
        continuation_page_token=result.continuation_page_token,
    )

    assert resumed.coverage_complete is True
    assert resumed.next_sync_token == "sync-after-resume"
    assert resume_client.calls[0][1]["pageToken"] == "more"
    assert resume_client.calls[0][1]["singleEvents"] == "true"
    assert resumed.api_requests == 1
    assert resumed.quota_units == 1


def test_calendar_fails_closed_when_provider_exceeds_requested_page_bound() -> None:
    client = FakeClient(
        [
            {
                "items": [{"id": "one"}, {"id": "two"}, {"id": "three"}],
                "nextPageToken": "unsafe-resume",
            }
        ]
    )

    with pytest.raises(RuntimeError, match="exceeded its requested page bound"):
        GoogleCalendarReader(client, max_events=2).fetch(
            time_min="2026-07-01T00:00:00Z",
            time_max="2026-08-01T00:00:00Z",
        )


def test_calendar_incremental_fetch_resumes_page_sequence_with_expansion() -> None:
    client = FakeClient([{"items": [{"id": "changed"}], "nextSyncToken": "sync-3"}])

    result = GoogleCalendarReader(client).fetch(
        time_min="2026-07-01T00:00:00Z",
        time_max="2026-08-01T00:00:00Z",
        sync_token="sync-2",
        continuation_page_token="incremental-page-2",
    )

    assert result.mode == "incremental"
    assert result.next_sync_token == "sync-3"
    assert client.calls[0][1]["syncToken"] == "sync-2"
    assert client.calls[0][1]["pageToken"] == "incremental-page-2"
    assert client.calls[0][1]["singleEvents"] == "true"
    assert "timeMin" not in client.calls[0][1]


def test_gmail_full_fetch_normalizes_plain_text_and_removes_attachment_bytes() -> None:
    client = FakeClient(
        [
            {"historyId": "300"},
            {"threads": [{"id": "thread-1"}]},
            gmail_thread(),
        ]
    )

    result = GmailThreadReader(
        client,
        operator_emails=("peter@example.com",),
    ).fetch(query="newer_than:30d -in:spam -in:trash")

    assert result.mode == "full"
    assert result.next_history_id == "300"
    assert result.coverage_complete is True
    thread = result.threads[0]
    assert thread.source_revision == "321"
    assert thread.message_class == "human"
    message = thread.messages[0]
    assert message.body == "Please send the deck."
    assert message.body_kind == "text/plain"
    assert message.timestamp == "2026-07-13T19:00:00+00:00"
    assert message.outgoing is True
    assert message.operator_authored is True
    assert message.attachment_count == 1
    attachment = result.raw_threads[0]["messages"][0]["payload"]["parts"][2]
    assert "data" not in attachment["body"]
    assert [call[2] for call in client.calls] == [1, 10, 40]
    assert result.baseline_history_id == "300"
    assert result.api_requests == 3
    assert result.quota_units == 51


def test_gmail_incremental_history_collects_changes_and_missing_tombstone() -> None:
    client = FakeClient(
        [
            {
                "history": [
                    {
                        "id": "10",
                        "messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}],
                        "messagesDeleted": [
                            {"message": {"id": "m2", "threadId": "t2"}}
                        ],
                    }
                ],
                "historyId": "11",
            },
            gmail_thread("t1"),
            GoogleAPIError(404, "thread disappeared"),
        ]
    )

    result = GmailThreadReader(client).fetch(
        query="newer_than:30d -in:spam -in:trash",
        history_id="9",
    )

    assert result.mode == "incremental"
    assert result.changed_thread_ids == ("t1", "t2")
    assert result.missing_thread_ids == ("t2",)
    assert result.next_history_id == "11"
    assert result.coverage_complete is True
    assert client.calls[0][0] == "history"
    assert client.calls[0][2] == 2
    assert result.api_requests == 3
    assert result.quota_units == 82


def test_gmail_history_404_returns_explicit_bounded_full_reset() -> None:
    client = FakeClient(
        [
            GoogleAPIError(404, "history expired"),
            {"historyId": "500"},
            {"threads": [{"id": "thread-1"}]},
            gmail_thread(),
        ]
    )

    result = GmailThreadReader(client).fetch(
        query="newer_than:7d -in:spam -in:trash",
        history_id="old",
        continuation_page_token="expired-history-page",
    )

    assert result.mode == "full"
    assert result.reset_required is True
    assert result.next_history_id == "500"
    assert client.calls[0][0] == "history"
    assert client.calls[1][0] == "profile"
    assert result.api_requests == 4
    assert result.quota_units == 53
    assert client.calls[0][1]["pageToken"] == "expired-history-page"
    assert "pageToken" not in client.calls[2][1]


def test_gmail_invalid_incremental_page_token_replays_retained_history() -> None:
    client = FakeClient(
        [
            GoogleAPIError(400, "Invalid pageToken", reason="invalidArgument"),
            {
                "history": [
                    {
                        "id": "10",
                        "messagesAdded": [
                            {"message": {"id": "m1", "threadId": "t1"}}
                        ],
                    }
                ],
                "historyId": "11",
            },
            gmail_thread("t1"),
        ]
    )

    result = GmailThreadReader(client).fetch(
        query="newer_than:7d -in:spam -in:trash",
        history_id="9",
        continuation_page_token="expired-page",
        continuation_history_id="10",
    )

    assert result.mode == "incremental"
    assert result.coverage_complete is True
    assert result.next_history_id == "11"
    assert client.calls[0][1]["startHistoryId"] == "9"
    assert client.calls[0][1]["pageToken"] == "expired-page"
    assert client.calls[1][1]["startHistoryId"] == "9"
    assert "pageToken" not in client.calls[1][1]
    assert result.api_requests == 3


def test_gmail_invalid_full_page_token_takes_new_baseline() -> None:
    client = FakeClient(
        [
            GoogleAPIError(400, "Invalid pageToken", reason="invalidArgument"),
            {"historyId": "new-baseline"},
            {"threads": [{"id": "thread-1"}]},
            gmail_thread(),
        ]
    )

    result = GmailThreadReader(client).fetch(
        query="newer_than:7d -in:spam -in:trash",
        continuation_page_token="expired-page",
        baseline_history_id="old-baseline",
    )

    assert result.mode == "full"
    assert result.reset_required is True
    assert result.coverage_complete is True
    assert result.next_history_id == "new-baseline"
    assert client.calls[0][0] == "threads"
    assert client.calls[0][1]["pageToken"] == "expired-page"
    assert client.calls[1][0] == "profile"
    assert client.calls[2][0] == "threads"
    assert "pageToken" not in client.calls[2][1]
    assert result.api_requests == 4


def test_gmail_malformed_thread_is_quarantined_without_losing_valid_peer() -> None:
    malformed = {
        "id": "thread-bad",
        "historyId": "322",
        "messages": [{"id": "message-bad", "payload": {}}],
    }
    client = FakeClient(
        [
            {"historyId": "300"},
            {"threads": [{"id": "thread-1"}, {"id": "thread-bad"}]},
            gmail_thread(),
            malformed,
        ]
    )

    result = GmailThreadReader(client).fetch(query="newer_than:7d")

    assert [thread.thread_id for thread in result.threads] == ["thread-1"]
    assert [raw["id"] for raw in result.raw_threads] == ["thread-1"]
    assert result.changed_thread_ids == ("thread-1", "thread-bad")
    assert len(result.quarantined_threads) == 1
    failure = result.quarantined_threads[0]
    assert failure.thread_id == "thread-bad"
    assert failure.source_revision == "322"
    assert failure.stage == "normalize"
    assert len(failure.payload_sha256) == 64
    assert result.coverage_complete is True
    assert result.next_history_id == "300"


def test_gmail_partial_full_fetch_never_advances_history_cursor() -> None:
    client = FakeClient(
        [
            {"historyId": "500"},
            {"threads": [{"id": "thread-1"}], "nextPageToken": "more"},
            gmail_thread(),
        ]
    )

    result = GmailThreadReader(client, max_pages=1).fetch(query="newer_than:30d")

    assert result.coverage_complete is False
    assert result.next_history_id is None
    assert result.continuation_page_token == "more"
    assert result.baseline_history_id == "500"
    assert result.api_requests == 3
    assert result.quota_units == 51

    resume_client = FakeClient(
        [
            {"threads": [{"id": "thread-2"}]},
            gmail_thread("thread-2"),
        ]
    )
    resumed = GmailThreadReader(resume_client, max_pages=1).fetch(
        query="newer_than:30d",
        continuation_page_token=result.continuation_page_token,
        baseline_history_id=result.baseline_history_id,
    )

    assert resumed.coverage_complete is True
    assert resumed.next_history_id == "500"
    assert resumed.baseline_history_id == "500"
    assert resume_client.calls[0][0] == "threads"
    assert resume_client.calls[0][1]["pageToken"] == "more"
    assert all(call[0] != "profile" for call in resume_client.calls)
    assert resumed.api_requests == 2
    assert resumed.quota_units == 50


def test_gmail_full_fetch_fails_closed_on_overfull_provider_page() -> None:
    client = FakeClient(
        [
            {"historyId": "500"},
            {
                "threads": [{"id": "t1"}, {"id": "t2"}, {"id": "t3"}],
                "nextPageToken": "unsafe-resume",
            },
        ]
    )

    with pytest.raises(RuntimeError, match="exceeded its requested page bound"):
        GmailThreadReader(client, max_threads=2).fetch(query="newer_than:30d")


def test_gmail_incremental_history_resumes_from_page_token() -> None:
    first_client = FakeClient(
        [
            {
                "history": [
                    {
                        "id": "10",
                        "messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}],
                    }
                ],
                "historyId": "11",
                "nextPageToken": "history-page-2",
            },
            gmail_thread("t1"),
        ]
    )
    partial = GmailThreadReader(first_client, max_pages=1).fetch(
        query="newer_than:30d",
        history_id="9",
    )

    assert partial.coverage_complete is False
    assert partial.next_history_id is None
    assert partial.continuation_page_token == "history-page-2"
    assert partial.api_requests == 2
    assert partial.quota_units == 42

    resume_client = FakeClient(
        [
            {
                "history": [
                    {
                        "id": "11",
                        "messagesAdded": [{"message": {"id": "m2", "threadId": "t2"}}],
                    }
                ],
                "historyId": "12",
            },
            gmail_thread("t2"),
        ]
    )
    resumed = GmailThreadReader(resume_client, max_pages=1).fetch(
        query="newer_than:30d",
        history_id="9",
        continuation_page_token=partial.continuation_page_token,
    )

    assert resumed.coverage_complete is True
    assert resumed.next_history_id == "12"
    assert resume_client.calls[0][1]["startHistoryId"] == "9"
    assert resume_client.calls[0][1]["pageToken"] == "history-page-2"
    assert resumed.api_requests == 2
    assert resumed.quota_units == 42


def test_gmail_incremental_thread_cap_stops_at_page_boundary_and_resumes() -> None:
    first_client = FakeClient(
        [
            {
                "history": [
                    {"id": "10", "messagesAdded": [{"message": {"threadId": "t1"}}]},
                    {"id": "11", "messagesAdded": [{"message": {"threadId": "t2"}}]},
                ],
                "historyId": "11",
                "nextPageToken": "cap-page-2",
            },
            gmail_thread("t1"),
            gmail_thread("t2"),
        ]
    )
    partial = GmailThreadReader(
        first_client,
        max_threads=2,
        max_pages=10,
    ).fetch(query="newer_than:30d", history_id="9")

    assert partial.coverage_complete is False
    assert partial.changed_thread_ids == ("t1", "t2")
    assert partial.continuation_page_token == "cap-page-2"
    assert first_client.calls[0][1]["maxResults"] == 2

    resume_client = FakeClient(
        [
            {
                "history": [
                    {"id": "12", "messagesAdded": [{"message": {"threadId": "t3"}}]}
                ],
                "historyId": "12",
            },
            gmail_thread("t3"),
        ]
    )
    resumed = GmailThreadReader(resume_client, max_threads=2).fetch(
        query="newer_than:30d",
        history_id="9",
        continuation_page_token=partial.continuation_page_token,
    )

    assert resumed.coverage_complete is True
    assert resumed.changed_thread_ids == ("t3",)
    assert resumed.next_history_id == "12"
    assert resume_client.calls[0][1]["pageToken"] == "cap-page-2"


def test_gmail_history_overflow_is_drained_before_advancing_page_token() -> None:
    first_client = FakeClient(
        [
            {
                "history": [
                    {
                        "id": "10",
                        "messagesAdded": [
                            {"message": {"id": "m1", "threadId": "t1"}},
                            {"message": {"id": "m2", "threadId": "t2"}},
                            {"message": {"id": "m3", "threadId": "t3"}},
                        ],
                    }
                ],
                "historyId": "11",
                "nextPageToken": "history-page-2",
            },
            gmail_thread("t1"),
            gmail_thread("t2"),
        ]
    )
    first = GmailThreadReader(first_client, max_threads=2).fetch(
        query="newer_than:30d",
        history_id="9",
    )

    assert first.coverage_complete is False
    assert first.changed_thread_ids == ("t1", "t2")
    assert first.pending_thread_ids == ("t3",)
    assert first.continuation_page_token == "history-page-2"
    assert first.continuation_history_id == "11"

    drain_client = FakeClient([gmail_thread("t3")])
    drained = GmailThreadReader(drain_client, max_threads=2).fetch(
        query="newer_than:30d",
        history_id="9",
        continuation_page_token=first.continuation_page_token,
        pending_thread_ids=first.pending_thread_ids,
        continuation_history_id=first.continuation_history_id,
    )

    assert drained.coverage_complete is False
    assert drained.changed_thread_ids == ("t3",)
    assert drained.pending_thread_ids == ()
    assert drained.continuation_page_token == "history-page-2"
    assert drained.continuation_history_id == "11"
    assert all(call[0] != "history" for call in drain_client.calls)

    final_client = FakeClient(
        [
            {
                "history": [
                    {
                        "id": "11",
                        "messagesAdded": [
                            {"message": {"id": "m4", "threadId": "t4"}}
                        ],
                    }
                ],
                "historyId": "12",
            },
            gmail_thread("t4"),
        ]
    )
    final = GmailThreadReader(final_client, max_threads=2).fetch(
        query="newer_than:30d",
        history_id="9",
        continuation_page_token=drained.continuation_page_token,
        continuation_history_id=drained.continuation_history_id,
    )

    assert final.coverage_complete is True
    assert final.changed_thread_ids == ("t4",)
    assert final.next_history_id == "12"
    assert final_client.calls[0][1]["pageToken"] == "history-page-2"


def test_google_readers_fail_closed_when_provider_exceeds_requested_page_bound() -> None:
    calendar = GoogleCalendarReader(
        FakeClient(
            [
                {
                    "items": [
                        {"id": "event-1", "status": "cancelled"},
                        {"id": "event-2", "status": "cancelled"},
                    ],
                    "nextSyncToken": "sync-2",
                }
            ]
        ),
        page_size=1,
        max_events=1,
    )
    with pytest.raises(RuntimeError, match="events.list exceeded"):
        calendar.fetch(
            time_min="2026-07-01T00:00:00Z",
            time_max="2026-08-01T00:00:00Z",
        )

    full_gmail = GmailThreadReader(
        FakeClient(
            [
                {"historyId": "20"},
                {"threads": [{"id": "t1"}, {"id": "t2"}]},
            ]
        ),
        page_size=1,
        max_threads=1,
    )
    with pytest.raises(RuntimeError, match="threads.list exceeded"):
        full_gmail.fetch(query="newer_than:30d")

    incremental_gmail = GmailThreadReader(
        FakeClient(
            [
                {
                    "history": [
                        {"id": "10", "messages": []},
                        {"id": "11", "messages": []},
                    ],
                    "historyId": "12",
                }
            ]
        ),
        history_page_size=1,
        max_history_records=1,
    )
    with pytest.raises(RuntimeError, match="history.list exceeded"):
        incremental_gmail.fetch(query="newer_than:30d", history_id="9")


def test_gmail_pending_backlog_accepts_its_durable_count_and_byte_envelope() -> None:
    pending = tuple(
        f"thread-{index:038d}" for index in range(MAX_PENDING_THREAD_IDS)
    )

    assert _validated_pending_thread_ids(pending) == pending


def test_gmail_pending_backlog_rejects_count_or_encoded_payload_overflow() -> None:
    with pytest.raises(ValueError, match="durable continuation bound"):
        _validated_pending_thread_ids(
            tuple(f"thread-{index}" for index in range(MAX_PENDING_THREAD_IDS + 1))
        )
    with pytest.raises(ValueError, match="durable continuation bound"):
        _validated_pending_thread_ids(
            tuple(
                f"{index:05d}-{'x' * 50}"
                for index in range(MAX_PENDING_THREAD_IDS)
            )
        )


def test_gmail_full_resume_requires_its_original_baseline() -> None:
    with pytest.raises(ValueError, match="baseline_history_id is required"):
        GmailThreadReader(FakeClient([])).fetch(
            query="newer_than:30d",
            continuation_page_token="page-2",
        )


def test_html_fallback_private_calendar_and_route_builders() -> None:
    html_message = {
        "id": "message-html",
        "threadId": "thread-html",
        "internalDate": "1783969200000",
        "payload": {
            "mimeType": "text/html",
            "headers": [{"name": "From", "value": "sender@example.com"}],
            "body": {
                "data": encoded(
                    "<p>Current answer</p><blockquote>Old quoted answer</blockquote>"
                )
            },
        },
    }
    message = normalize_gmail_message(html_message)
    event = normalize_calendar_event(
        {
            "id": "private-1",
            "visibility": "private",
            "summary": "Secret title",
            "description": "Secret details",
            "location": "Secret room",
        }
    )

    assert message.body == "Current answer"
    assert message.body_kind == "text/html"
    assert event.title == "Private event"
    assert event.details is None
    assert event.location is None
    assert gmail_thread_route("peter@example.com", "abc/123") == (
        "https://mail.google.com/mail/u/peter@example.com/#all/abc%2F123"
    )
    assert calendar_event_route("peter@example.com", "event id") == (
        "https://calendar.google.com/calendar/u/peter@example.com/r/eventedit/event%20id"
    )
    assert local_google_evidence_route("gmail", "account/1", "thread#1") == (
        "/evidence/google/gmail/account%2F1/thread%231"
    )
    with pytest.raises(ValueError):
        gmail_thread_route("https://attacker.example", "thread")
