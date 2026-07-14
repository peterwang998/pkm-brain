from __future__ import annotations

import base64
import importlib.util
from datetime import date, datetime
from pathlib import Path
import sys
from typing import Any


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "gmail_brain_benchmark.py"
SPEC = importlib.util.spec_from_file_location("gmail_brain_benchmark", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
gmail_benchmark = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gmail_benchmark
SPEC.loader.exec_module(gmail_benchmark)


def encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def message(
    value: str,
    *,
    when: datetime,
    sender: str = "Person <person@example.com>",
    subject: str = "Project update",
    labels: list[str] | None = None,
    extra_headers: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "id": f"message-{when.timestamp()}",
        "internalDate": str(int(when.timestamp() * 1000)),
        "labelIds": labels or ["INBOX"],
        "sizeEstimate": 1000,
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": "Me <me@example.com>"},
                {"name": "Subject", "value": subject},
                *(extra_headers or []),
            ],
            "body": {"data": encoded(value)},
        },
    }


def record(updated_date: str, *, eligible: bool = True, suffix: str = "x") -> Any:
    return gmail_benchmark.NormalizedThread(
        thread_id=f"thread-{updated_date}-{suffix}",
        path=Path(f"/{updated_date}-{suffix}.md"),
        classification="human" if eligible else "bulk",
        fact_eligible=eligible,
        created_at=f"{updated_date}T08:00:00-07:00",
        updated_at=f"{updated_date}T09:00:00-07:00",
        updated_date=updated_date,
        message_count=1,
        source_size_estimate=1000,
        normalized_bytes=100,
        body_chars=50,
        quoted_chars_removed=0,
        attachment_count=0,
        truncated_message_count=0,
    )


def test_message_body_prefers_plain_text_and_removes_quotes_and_attachments() -> None:
    value = message(
        "Current decision.\n\nOn Fri, someone wrote:\n> prior reply",
        when=datetime.fromisoformat("2026-07-01T09:00:00-07:00"),
    )
    value["payload"] = {
        "mimeType": "multipart/mixed",
        "headers": value["payload"]["headers"],
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": value["payload"]["body"]},
                    {
                        "mimeType": "text/html",
                        "body": {"data": encoded("<p>Duplicate HTML</p>")},
                    },
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "attachment.pdf",
                "body": {"attachmentId": "attachment-id", "size": 5000},
            },
        ],
    }

    body, removed, attachment_count, truncated = gmail_benchmark.message_body(value)

    assert body == "Current decision."
    assert removed > 0
    assert attachment_count == 1
    assert truncated is False


def test_thread_classification_prioritizes_participation_then_bulk_and_automation() -> None:
    when = datetime.fromisoformat("2026-07-01T09:00:00-07:00")
    sent = message("A reply", when=when, labels=["SENT", "CATEGORY_PROMOTIONS"])
    bulk = message(
        "Newsletter",
        when=when,
        extra_headers=[{"name": "List-Unsubscribe", "value": "<mailto:leave@example.com>"}],
    )
    automated = message(
        "Receipt",
        when=when,
        sender="No Reply <no-reply@example.com>",
    )

    assert gmail_benchmark.thread_classification([sent]) == "human"
    assert gmail_benchmark.thread_classification([bulk]) == "bulk"
    assert gmail_benchmark.thread_classification([automated]) == "transactional"


def test_normalized_thread_document_enforces_strict_local_date_window(tmp_path: Path) -> None:
    inside = message(
        "A durable project decision with enough retained text for extraction eligibility.",
        when=datetime.fromisoformat("2026-07-01T09:00:00-07:00"),
        labels=["SENT"],
    )
    outside = message(
        "Old content should not be retained.",
        when=datetime.fromisoformat("2026-06-01T09:00:00-07:00"),
    )

    result = gmail_benchmark.normalized_thread_document(
        {"id": "thread-1", "messages": [outside, inside]},
        start_date=date(2026, 7, 1),
        end_date_exclusive=date(2026, 7, 2),
        corpus_root=tmp_path,
    )

    assert result is not None
    normalized, payload = result
    assert normalized.message_count == 1
    assert normalized.updated_date == "2026-07-01"
    assert 'created_at: "2026-07-01T09:00:00-07:00"' in payload
    assert "source_created_at:" not in payload
    assert "Old content" not in payload
    assert "durable project decision" in payload


def test_representative_days_span_eligible_weekday_volume() -> None:
    records = [
        record("2026-06-01", suffix="1"),
        *[record("2026-06-02", suffix=str(index)) for index in range(3)],
        *[record("2026-06-03", suffix=str(index)) for index in range(5)],
        *[record("2026-06-04", suffix=str(index)) for index in range(7)],
        record("2026-06-06", suffix="weekend"),
        record("2026-06-05", eligible=False, suffix="bulk"),
    ]

    selected = gmail_benchmark.select_representative_days(
        records,
        requested=3,
        today=date(2026, 6, 10),
    )

    assert selected == ["2026-06-02", "2026-06-03", "2026-06-04"]
