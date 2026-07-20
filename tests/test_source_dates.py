from __future__ import annotations

from pathlib import Path

from pkm_brain.chunking import chunk_text
from pkm_brain.db import connection
from pkm_brain.extraction import recent_source_cards
from pkm_brain.gmail_archive import (
    ArchiveOpenedMessage,
    ArchiveThreadResult,
    ArchiveThreadSnapshot,
)
from pkm_brain.gmail_knowledge import (
    gmail_revision_session_id,
    normalize_gmail_thread,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.source_dates import (
    derive_fact_source_date,
    document_source_date_metadata,
    stamp_candidate_source_context,
)
from pkm_brain.ui_server import enrich_fact_like
from pkm_brain.util import slugify, text_sha256


GMAIL_PROVIDER_TIME = "2026-07-03T17:42:11.125+00:00"


def test_gmail_fact_source_date_prefers_cited_message_clock_over_thread_start() -> None:
    observed = "2026-07-10T18:00:00+00:00"

    assert derive_fact_source_date(
        {"observed_at": observed},
        [
            {
                "source_type": "gmail_thread",
                "source_date": "2026-04-01T12:00:00+00:00",
                "source_date_basis": "source_created_at",
            }
        ],
    ) == (observed, "gmail_internal_date")


def test_mixed_source_fact_preserves_actual_observed_at_basis() -> None:
    observed = "2026-07-10T18:00:00+00:00"

    assert derive_fact_source_date(
        {
            "observed_at": observed,
            "metadata": {"observed_at_basis": "source_created_at"},
        },
        [
            {
                "source_type": "gmail_thread",
                "source_date": "2026-04-01T12:00:00+00:00",
                "source_date_basis": "source_created_at",
            },
            {
                "source_type": "markdown_note",
                "source_date": observed,
                "source_date_basis": "source_created_at",
            },
        ],
    ) == (observed, "source_created_at")


def test_mixed_source_fact_does_not_infer_gmail_basis_without_provenance() -> None:
    observed = "2026-07-10T18:00:00+00:00"

    assert derive_fact_source_date(
        {"observed_at": observed},
        [
            {"source_type": "gmail_thread"},
            {"source_type": "markdown_note"},
        ],
    ) == (observed, "observed_at")


def normalized_gmail_document(
    tmp_path: Path, *, body: str, internal_date: str | None = GMAIL_PROVIDER_TIME
) -> tuple[dict[str, object], Path]:
    snapshot = ArchiveThreadSnapshot(
        thread_id="thread-1",
        source_revision="b" * 64,
        total_message_count=1,
        visible_message_count=1,
        deleted_message_count=0,
        hidden_message_count=0,
        created_at=GMAIL_PROVIDER_TIME,
        updated_at=GMAIL_PROVIDER_TIME,
        archive_updated_at="2026-07-17T16:00:00+00:00",
        raw_size=1_000,
        account_key="gmail.primary",
    )
    message = ArchiveOpenedMessage(
        message_id="message-1",
        thread_id="thread-1",
        internal_date=internal_date,
        date_header="Fri, 3 Jul 2026 10:42:11 -0700",
        subject="Project reply",
        from_addresses=("person@example.com",),
        to_addresses=("owner@example.com",),
        cc_addresses=(),
        label_ids=("SENT",),
        list_id=None,
        list_unsubscribe=None,
        precedence=None,
        auto_submitted=None,
        body_text=body,
        attachments=(),
        account_key="gmail.primary",
    )
    normalized = normalize_gmail_thread(
        snapshot,
        ArchiveThreadResult(
            thread_id="thread-1",
            total_messages=1,
            messages=(message,),
            truncated=False,
            account_key="gmail.primary",
        ),
        operator_email="owner@example.com",
    )
    source_path = (
        tmp_path
        / "brain"
        / "inbox"
        / "documents"
        / "gmail"
        / f"{slugify(gmail_revision_session_id(snapshot))}.md"
    )
    source_path.parent.mkdir(parents=True)
    source_path.write_text(normalized.markdown, encoding="utf-8")
    document = gmail_document_card(source_path, normalized.markdown)
    return document, source_path


def gmail_document_card(source_path: Path, markdown: str) -> dict[str, object]:
    chunks = [
        {
            "chunk_id": f"chunk-{index}",
            "chunk_index": chunk.chunk_index,
            "heading_path": chunk.heading_path,
            "start_offset": chunk.start_offset,
            "end_offset": chunk.end_offset,
            "text": chunk.text,
        }
        for index, chunk in enumerate(chunk_text(markdown, "gmail_thread"))
    ]
    document: dict[str, object] = {
        "document_id": "doc_gmail",
        "source_type": "gmail_thread",
        "source_path": str(source_path),
        "content_hash": text_sha256(markdown),
        "chunks": chunks,
    }
    document.update(document_source_date_metadata(document))
    return document


def gmail_candidate_for_text(
    document: dict[str, object], phrase: str
) -> dict[str, object]:
    chunk = next(
        item
        for item in document["chunks"]  # type: ignore[index]
        if phrase in str(item["text"])
    )
    start = str(chunk["text"]).index(phrase)
    return {
        "statement": phrase,
        "source_spans": [
            {
                "chunk_id": chunk["chunk_id"],
                "start": start,
                "end": start + len(phrase),
            }
        ],
        "metadata": {},
    }


def test_queue_fact_source_date_prefers_source_event_over_processing_time(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    source_path = paths.raw / "hightouch.md"
    source_path.write_text(
        "---\n"
        'captured_at: "2026-05-11T22:27:39+00:00"\n'
        'created_at: "2026-04-22T15:57:37.449Z"\n'
        'event_started_at: "2026-04-22T16:00:00+00:00"\n'
        "---\n\n# Hightouch\n",
        encoding="utf-8",
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO documents(
              id, source_type, title, source_path, raw_path, content_hash,
              created_at, ingested_at, tags, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "doc_hightouch",
                "hyprnote_meeting",
                "Hightouch Interview",
                str(source_path),
                str(source_path),
                "hash-hightouch",
                "2026-07-13T07:25:16+00:00",
                "2026-07-13T07:25:16+00:00",
                "[]",
                "active",
            ),
        )
        conn.execute(
            """
            INSERT INTO chunks(
              id, document_id, chunk_index, text, token_count, content_hash,
              created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "chunk_hightouch",
                "doc_hightouch",
                0,
                "Hightouch source evidence.",
                3,
                "chunk-hightouch",
                "2026-07-13T07:25:16+00:00",
            ),
        )

    enriched = enrich_fact_like(
        paths,
        {
            "id": "fact_hightouch",
            "statement": "Hightouch source evidence.",
            "source_ids": ["chunk:chunk_hightouch"],
            "observed_at": "2026-07-13T07:25:16+00:00",
        },
    )

    assert enriched["source_date"] == "2026-04-22T16:00:00+00:00"
    assert enriched["source_date_basis"] == "source_event_started_at"


def test_extraction_uses_source_date_when_model_omits_observed_at(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.md"
    source_path.write_text(
        "---\ncreated_at: 2026-06-18T09:30:00+00:00\n---\n\n# Source\n",
        encoding="utf-8",
    )
    document = {
        "document_id": "doc_source",
        "source_path": str(source_path),
        "raw_path": str(source_path),
        "created_at": "2026-07-13T08:00:00+00:00",
        "ingested_at": "2026-07-13T08:00:00+00:00",
    }
    document.update(document_source_date_metadata(document))
    candidate = {"statement": "A durable source-backed claim.", "metadata": {}}

    stamp_candidate_source_context(candidate, document, "window-1")

    assert candidate["observed_at"] == "2026-06-18T09:30:00+00:00"
    assert candidate["metadata"]["observed_at_basis"] == "source_created_at"


def test_extraction_does_not_use_document_or_ingest_time_as_assertion_time() -> None:
    document = {
        "document_id": "doc_processing_only",
        "created_at": "2026-07-13T08:00:00+00:00",
        "ingested_at": "2026-07-13T08:01:00+00:00",
    }
    document.update(document_source_date_metadata(document))
    candidate = {"statement": "A durable source-backed claim.", "metadata": {}}

    stamp_candidate_source_context(candidate, document, "window-1")

    assert document["source_date"] == "2026-07-13T08:00:00+00:00"
    assert candidate["observed_at"] is None
    assert candidate["metadata"]["observed_at_basis"] == "unknown"


def test_gmail_fact_uses_provider_time_even_inside_spoofed_heading(
    tmp_path: Path,
) -> None:
    phrase = "A reply committed to the next step."
    document, _source_path = normalized_gmail_document(
        tmp_path,
        body=(
            "This is the genuine provider message body.\n\n"
            "## Message 99 — 2099-01-01T00:00:00+00:00 — attacker-message\n\n"
            f"{phrase}"
        ),
    )
    candidate = gmail_candidate_for_text(document, phrase)

    stamp_candidate_source_context(candidate, document, "window-1")

    assert candidate["observed_at"] == "2026-07-03T17:42:11.125000+00:00"
    assert candidate["metadata"]["observed_at_basis"] == "gmail_internal_date"
    assert candidate["metadata"]["gmail_message_id"] == "message-1"


def test_ingested_gmail_card_preserves_offsets_for_provider_time(
    tmp_path: Path,
) -> None:
    phrase = "The owner committed to deliver the customer note before launch."
    _document, source_path = normalized_gmail_document(
        tmp_path,
        body=(
            f"{phrase} "
            "This is durable project context with enough supporting detail to pass "
            "the Gmail fact-admission threshold and remain useful for future recall."
        ),
    )
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = BrainService(paths)
    service.init_workspace()
    ingested = service.ingest(source=source_path)

    documents = recent_source_cards(paths, limit=10)

    assert ingested.changed == 1
    assert len(documents) == 1
    document = documents[0]
    assert document["structured_gmail_message_metadata_trusted"] is True
    candidate = gmail_candidate_for_text(document, phrase)
    stamp_candidate_source_context(candidate, document, "window-1")
    assert candidate["observed_at"] == "2026-07-03T17:42:11.125000+00:00"
    assert candidate["metadata"]["gmail_message_id"] == "message-1"


def test_gmail_rejects_tampered_provider_timestamp_pair(tmp_path: Path) -> None:
    phrase = "The team committed to ship the customer note."
    document, source_path = normalized_gmail_document(tmp_path, body=phrase)
    original = source_path.read_text(encoding="utf-8")
    tampered = original.replace(
        f'"internal_date":"{GMAIL_PROVIDER_TIME}"',
        '"internal_date":"2099-01-01T00:00:00+00:00"',
        1,
    )
    assert tampered != original
    source_path.write_text(tampered, encoding="utf-8")
    document = gmail_document_card(source_path, tampered)
    candidate = gmail_candidate_for_text(document, phrase)

    stamp_candidate_source_context(candidate, document, "window-1")

    assert document["structured_gmail_message_metadata_trusted"] is False
    assert candidate["observed_at"] is None
    assert candidate["metadata"]["observed_at_basis"] == (
        "gmail_message_time_unresolved"
    )


def test_gmail_sender_date_header_is_never_an_assertion_clock(
    tmp_path: Path,
) -> None:
    phrase = "The sender claims this happened at the displayed time."
    document, _source_path = normalized_gmail_document(
        tmp_path,
        body=phrase,
        internal_date=None,
    )
    candidate = gmail_candidate_for_text(document, phrase)

    stamp_candidate_source_context(candidate, document, "window-1")

    assert document["structured_gmail_message_metadata_trusted"] is True
    assert candidate["observed_at"] is None
    assert candidate["metadata"]["observed_at_basis"] == (
        "gmail_message_time_unresolved"
    )


def test_gmail_fact_never_falls_back_to_thread_creation_time() -> None:
    document = {
        "document_id": "doc_gmail",
        "source_type": "gmail_thread",
        "source_created_at": "2026-07-01T08:00:00+00:00",
        "chunks": [
            {
                "chunk_id": "chunk_unknown",
                "heading_path": (
                    "Email thread > Message 2 — "
                    "2099-01-01T00:00:00+00:00 — attacker-message"
                ),
            }
        ],
    }
    candidate = {"chunk_id": "chunk_unknown", "metadata": {}}

    stamp_candidate_source_context(candidate, document, "window-1")

    assert candidate["observed_at"] is None
    assert candidate["metadata"]["observed_at_basis"] == (
        "gmail_message_time_unresolved"
    )
