from __future__ import annotations

import base64
import json
from typing import Any

from pkm_brain.google_normalization import (
    normalize_gmail_thread,
    sanitize_gmail_thread_payload,
)


def _encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _message(message_id: str, internal_date: str, body: str) -> dict[str, Any]:
    return {
        "id": message_id,
        "threadId": "thread-cap",
        "internalDate": internal_date,
        "payload": {
            "mimeType": "text/plain",
            "headers": [{"name": "Subject", "value": message_id}],
            "body": {"data": _encoded(body)},
        },
    }


def test_attachment_ancestry_blocks_nested_rfc822_and_multipart_bodies() -> None:
    value = {
        "id": "thread-attachments",
        "historyId": "42",
        "messages": [
            {
                "id": "message-1",
                "threadId": "thread-attachments",
                "internalDate": "1783969200000",
                "payload": {
                    "mimeType": "multipart/mixed",
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": _encoded("Visible current reply")},
                        },
                        {
                            "mimeType": "multipart/mixed",
                            "headers": [
                                {
                                    "name": "Content-Disposition",
                                    "value": "attachment",
                                }
                            ],
                            "parts": [
                                {
                                    "mimeType": "multipart/alternative",
                                    "parts": [
                                        {
                                            "mimeType": "text/plain",
                                            "body": {
                                                "data": _encoded(
                                                    "SECRET_FROM_MULTIPART_ATTACHMENT"
                                                )
                                            },
                                        },
                                        {
                                            "mimeType": "text/html",
                                            "body": {
                                                "data": _encoded(
                                                    "<p>SECRET_HTML_ATTACHMENT</p>"
                                                )
                                            },
                                        },
                                    ],
                                }
                            ],
                        },
                        {
                            "mimeType": "message/rfc822; name=forwarded.eml",
                            "parts": [
                                {
                                    "mimeType": "text/plain",
                                    "body": {
                                        "data": _encoded("SECRET_FROM_ATTACHED_EMAIL")
                                    },
                                }
                            ],
                        },
                    ],
                },
            }
        ],
    }

    sanitized = sanitize_gmail_thread_payload(value)
    multipart_text = sanitized["messages"][0]["payload"]["parts"][1]["parts"][0][
        "parts"
    ][0]
    attached_email_text = sanitized["messages"][0]["payload"]["parts"][2][
        "parts"
    ][0]
    assert "data" not in multipart_text["body"]
    assert "data" not in attached_email_text["body"]

    for source in (value, sanitized):
        normalized = normalize_gmail_thread(source)
        serialized = json.dumps(normalized.as_dict())
        assert normalized.messages[0].body == "Visible current reply"
        assert normalized.attachment_count == 2
        assert "SECRET_" not in serialized


def test_thread_body_cap_keeps_newest_replies_but_returns_chronologically() -> None:
    value = {
        "id": "thread-cap",
        "historyId": "43",
        "messages": [
            _message("newest", "3000", "NEW333"),
            _message("oldest", "1000", "OLD111"),
            _message("middle", "2000", "MID222"),
        ],
    }

    normalized = normalize_gmail_thread(
        value,
        message_body_cap=100,
        thread_body_cap=8,
    )

    assert [message.message_id for message in normalized.messages] == [
        "oldest",
        "middle",
        "newest",
    ]
    assert [message.body for message in normalized.messages] == ["", "MI", "NEW333"]
    assert [message.truncated for message in normalized.messages] == [True, True, False]
    assert normalized.body_chars == 8
    assert normalized.truncated is True
