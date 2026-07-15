from __future__ import annotations

import base64
from typing import Any

import pytest

from pkm_brain.gmail_archive_source import (
    GmailArchiveReader,
    GmailHistoryExpired,
    GmailPageTokenExpired,
)
from pkm_brain.google_api import GoogleAPIError


class FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str | int], int]] = []

    def get_json(
        self,
        relative_path: str,
        *,
        params: dict[str, str | int] | None = None,
        quota_units: int = 1,
    ) -> dict[str, Any]:
        self.calls.append((relative_path, dict(params or {}), quota_units))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _raw_payload(
    message_id: str,
    raw: bytes,
    *,
    thread_id: str = "thread-1",
) -> dict[str, Any]:
    return {
        "id": message_id,
        "threadId": thread_id,
        "historyId": "42",
        "internalDate": "1710000000000",
        "labelIds": ["INBOX"],
        "raw": base64.urlsafe_b64encode(raw).decode().rstrip("="),
    }


def test_profile_and_backfill_preserve_exact_raw_message() -> None:
    raw = (
        b"Subject: archive test\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: application/octet-stream\r\n\r\n\x00attachment\xff"
    )
    client = FakeClient(
        [
            {"historyId": "40"},
            {
                "messages": [{"id": "m1"}],
                "nextPageToken": "next",
                "resultSizeEstimate": 12,
            },
            _raw_payload("m1", raw),
        ]
    )
    reader = GmailArchiveReader(client, page_size=10)

    assert reader.capture_history_id() == ("40", 1, 1)
    batch = reader.backfill_page("after:123", page_token=None)

    assert batch.messages[0].raw == raw
    assert batch.next_page_token == "next"
    assert batch.result_size_estimate == 12
    assert batch.api_requests == 2
    assert batch.quota_units == 25
    assert client.calls[1] == (
        "messages",
        {"q": "after:123", "maxResults": 10, "includeSpamTrash": "true"},
        5,
    )
    assert client.calls[2][1] == {"format": "raw"}


def test_backfill_isolates_missing_and_malformed_messages() -> None:
    client = FakeClient(
        [
            {"messages": [{"id": "ok"}, {"id": "gone"}, {"id": "bad"}]},
            _raw_payload("ok", b"Subject: ok\r\n\r\nbody"),
            GoogleAPIError(404, "gone"),
            {"id": "bad", "threadId": "t", "raw": "%%%"},
        ]
    )

    batch = GmailArchiveReader(client).backfill_page("after:123")

    assert [item.message_id for item in batch.messages] == ["ok"]
    assert batch.missing_ids == ("gone",)
    assert [(item.message_id, item.code) for item in batch.failures] == [
        ("bad", "malformed_raw_message")
    ]


def test_history_page_deduplicates_and_resumes_pending_before_next_page() -> None:
    history = {
        "history": [
            {
                "messagesAdded": [{"message": {"id": "m1"}}],
                "labelsAdded": [{"message": {"id": "m1"}}],
                "messages": [{"id": "m2"}],
            }
        ],
        "nextPageToken": "page-2",
        "historyId": "55",
    }
    client = FakeClient(
        [
            history,
            _raw_payload("m1", b"Subject: one\r\n\r\n1"),
            _raw_payload("m2", b"Subject: two\r\n\r\n2"),
            _raw_payload("m3", b"Subject: three\r\n\r\n3"),
        ]
    )
    reader = GmailArchiveReader(client, page_size=10)

    first = reader.history_page("40")
    assert [item.message_id for item in first.messages] == ["m1", "m2"]
    assert first.next_page_token == "page-2"
    assert first.next_history_id is None
    assert first.continuation_history_id == "55"

    resumed = reader.history_page(
        "40",
        page_token="page-2",
        pending_ids=("m3",),
        continuation_history_id="55",
    )
    assert [item.message_id for item in resumed.messages] == ["m3"]
    assert resumed.next_page_token == "page-2"
    assert resumed.next_history_id is None
    assert client.calls[-1][0] == "messages/m3"


def test_history_completion_advances_cursor_and_expiry_is_typed() -> None:
    client = FakeClient(
        [
            {"history": [], "historyId": "72"},
            GoogleAPIError(404, "history expired"),
        ]
    )
    reader = GmailArchiveReader(client)

    complete = reader.history_page("70")
    assert complete.next_history_id == "72"
    assert complete.continuation_history_id == "72"
    assert complete.api_requests == 1

    with pytest.raises(GmailHistoryExpired):
        reader.history_page("72")


@pytest.mark.parametrize(
    "record",
    [
        {"messagesAdded": {"message": {"id": "m1"}}},
        {"messagesAdded": [None]},
        {"messagesAdded": [{}]},
        {"messagesAdded": [{"message": "m1"}]},
        {"messagesAdded": [{"message": {}}]},
        {"messages": {"id": "m1"}},
        {"messages": [None]},
        {"messages": [{}]},
    ],
)
def test_malformed_history_changes_cannot_advance_cursor(
    record: dict[str, Any],
) -> None:
    client = FakeClient([{"history": [record], "historyId": "99"}])

    with pytest.raises(ValueError, match="malformed|invalid"):
        GmailArchiveReader(client).history_page("40")

    assert [call[0] for call in client.calls] == ["history"]


def test_invalid_saved_page_tokens_are_restartable() -> None:
    backfill_client = FakeClient([GoogleAPIError(400, "invalid token")])
    with pytest.raises(GmailPageTokenExpired):
        GmailArchiveReader(backfill_client).backfill_page(
            "after:123", page_token="stale"
        )

    history_client = FakeClient([GoogleAPIError(400, "invalid token")])
    with pytest.raises(GmailPageTokenExpired):
        GmailArchiveReader(history_client).history_page(
            "40", page_token="stale"
        )


def test_reader_rejects_unsafe_bounds_and_oversized_raw() -> None:
    with pytest.raises(ValueError):
        GmailArchiveReader(FakeClient([]), page_size=0)
    client = FakeClient(
        [
            {"messages": [{"id": "m1"}]},
            _raw_payload("m1", b"too large"),
        ]
    )
    batch = GmailArchiveReader(client, max_raw_bytes=2).backfill_page("after:123")
    assert batch.messages == ()
    assert batch.failures[0].code == "malformed_raw_message"
