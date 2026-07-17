from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pkm_brain.event_projection import (
    coalesce_structured_event_candidates,
    parse_source_updated_at,
    structured_event_kind,
    structured_source_event_candidate,
)
from pkm_brain.extraction import extract_recent_documents
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.source_dates import stamp_candidate_source_context
from pkm_brain.temporal import event_time_signature, normalize_event_time_candidate


def structured_document(
    *,
    session_id: str,
    title: str = "Hightouch Interview",
    start_at: str = "2026-05-22T16:30:00+00:00",
    end_at: str = "2026-05-22T17:15:00+00:00",
    source_updated_at: str = "2026-05-22T17:16:00+00:00",
) -> dict[str, Any]:
    text = (
        "---\n"
        f'source_updated_at: "{source_updated_at}"\n'
        f'title: "{title}"\n'
        f'event_started_at: "{start_at}"\n'
        f'event_ended_at: "{end_at}"\n'
        "---\n"
    )
    return {
        "document_id": f"doc_{session_id}",
        "source_type": "hyprnote_meeting",
        "structured_event_metadata_trusted": True,
        "event_session_id": session_id,
        "source_updated_at": source_updated_at,
        "event_started_at": start_at,
        "event_ended_at": end_at,
        "title": title,
        "chunks": [{"chunk_id": f"chunk_{session_id}", "text": text}],
    }


def stamped_candidate(document: dict[str, Any]) -> dict[str, Any]:
    candidate = structured_source_event_candidate(document)
    assert candidate is not None
    stamp_candidate_source_context(
        candidate,
        document,
        f"{document['document_id']}:structured-event",
    )
    return candidate


def test_structured_projection_uses_post_start_source_update_for_actual() -> None:
    candidate = structured_source_event_candidate(
        structured_document(session_id="actual-session")
    )

    assert candidate is not None
    assert candidate["statement"] == (
        "Hightouch Interview (2026-05-22T16:30:00+00:00) occurred."
    )
    assert candidate["event_time_kind"] == "actual"
    assert candidate["metadata"]["structured_event_kind_basis"] == (
        "source_updated_at_at_or_after_event_start"
    )
    assert "source_updated_at" in candidate["evidence_quote"]


def test_structured_projection_keeps_pre_start_metadata_as_planned() -> None:
    document = structured_document(
        session_id="planned-session",
        source_updated_at="2026-05-22T15:00:00+00:00",
    )
    # Later ingestion/capture is not occurrence evidence.
    document["captured_at"] = "2026-05-23T12:00:00+00:00"

    candidate = structured_source_event_candidate(document)

    assert candidate is not None
    assert candidate["statement"] == (
        "Hightouch Interview (2026-05-22T16:30:00+00:00) was scheduled."
    )
    assert candidate["event_time_kind"] == "planned"
    assert candidate["metadata"]["structured_event_kind_basis"] == (
        "source_updated_at_before_event_start"
    )


def test_structured_event_identity_distinguishes_same_title_same_day_sessions() -> None:
    first = stamped_candidate(structured_document(session_id="first-session"))
    second = stamped_candidate(
        structured_document(
            session_id="second-session",
            start_at="2026-05-22T17:30:00+00:00",
            end_at="2026-05-22T17:45:00+00:00",
            source_updated_at="2026-05-22T17:46:00+00:00",
        )
    )

    assert first["entity_mention"] != second["entity_mention"]
    assert first["entity_key"] != second["entity_key"]
    assert first["page_hint"] != second["page_hint"]
    assert len(coalesce_structured_event_candidates([first, second])) == 2


def test_batch_coalesces_same_occurrence_and_unions_provenance() -> None:
    first = stamped_candidate(
        structured_document(
            session_id="duplicate-one",
            start_at="2026-05-22T16:30:00Z",
            end_at="2026-05-22T17:15:00Z",
        )
    )
    second = stamped_candidate(
        structured_document(session_id="duplicate-two")
    )

    coalesced = coalesce_structured_event_candidates([first, second])

    assert len(coalesced) == 1
    candidate = coalesced[0]
    assert candidate["event_start_at"] == "2026-05-22T16:30:00+00:00"
    assert len(candidate["source_ids"]) == 2
    assert len(candidate["source_spans"]) >= 2
    assert candidate["metadata"]["source_document_ids"] == [
        "doc_duplicate-one",
        "doc_duplicate-two",
    ]
    assert candidate["metadata"]["event_session_ids"] == [
        "duplicate-one",
        "duplicate-two",
    ]
    assert candidate["metadata"]["structured_event_source_count"] == 2
    assert len(candidate["metadata"]["structured_event_sources"]) == 2


def test_batch_does_not_merge_planned_and_actual_for_same_occurrence() -> None:
    planned = stamped_candidate(
        structured_document(
            session_id="scheduled-copy",
            source_updated_at="2026-05-22T15:00:00+00:00",
        )
    )
    actual = stamped_candidate(structured_document(session_id="recorded-copy"))

    coalesced = coalesce_structured_event_candidates([planned, actual])

    assert [candidate["event_time_kind"] for candidate in coalesced] == [
        "planned",
        "actual",
    ]


def test_event_time_normalization_canonicalizes_equivalent_utc_forms() -> None:
    zulu, zulu_errors = normalize_event_time_candidate(
        {
            "event_time": {
                "kind": "actual",
                "start_at": "2026-05-22T23:30:00Z",
                "end_at": "2026-05-23T00:15:00Z",
                "precision": "exact",
            }
        },
        primary_entity_is_event=True,
    )
    offset, offset_errors = normalize_event_time_candidate(
        {
            "event_time": {
                "kind": "actual",
                "start_at": "2026-05-22T16:30:00-07:00",
                "end_at": "2026-05-22T17:15:00-07:00",
                "precision": "exact",
            }
        },
        primary_entity_is_event=True,
    )

    assert zulu_errors == offset_errors == []
    assert zulu["event_time"] == offset["event_time"]
    assert zulu["event_start_at"] == "2026-05-22T23:30:00+00:00"
    assert event_time_signature(zulu) == event_time_signature(offset)


def test_source_update_parser_accepts_capture_seconds_and_milliseconds() -> None:
    seconds = parse_source_updated_at("1779470160")
    milliseconds = parse_source_updated_at("1779470160000")

    assert seconds is not None
    assert seconds == milliseconds
    assert structured_event_kind(
        "2026-05-22T16:30:00+00:00", "1779470160"
    )[0] == "actual"


class EmptyExtractorProvider:
    name = "empty-extractor"
    model = "empty-extractor-model"

    def complete(self, _prompt: str) -> str:
        return json.dumps({"facts": []})


def write_hyprnote_source(
    root: Path,
    *,
    session_id: str,
    start_at: str = "2026-05-22T16:30:00+00:00",
    end_at: str = "2026-05-22T17:15:00+00:00",
) -> None:
    path = root / "documents" / "hyprnote" / f"{session_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        'source_type: "hyprnote_meeting"\n'
        'agent: "hyprnote"\n'
        f'session_id: "{session_id}"\n'
        f'source_path: "/Users/Peter/Library/Application Support/hyprnote/sessions/{session_id}"\n'
        'captured_at: "2026-05-23T12:00:00+00:00"\n'
        'source_updated_at: "2026-05-22T17:16:00+00:00"\n'
        'title: "Hightouch Interview"\n'
        f'event_started_at: "{start_at}"\n'
        f'event_ended_at: "{end_at}"\n'
        "---\n\n"
        "# Meeting: Hightouch Interview\n\n"
        "A substantive duplicate capture.\n",
        encoding="utf-8",
    )


def test_extraction_batch_coalesces_duplicate_structured_candidates(
    tmp_path: Path,
) -> None:
    svc = BrainService(BrainPaths.from_value(tmp_path / "brain"))
    svc.init_workspace()
    write_hyprnote_source(svc.paths.inbox, session_id="duplicate-session-one")
    write_hyprnote_source(svc.paths.inbox, session_id="duplicate-session-two")
    svc.ingest()

    result = extract_recent_documents(
        svc.paths,
        limit=10,
        shadow=True,
        changed_only=False,
        llm_provider=EmptyExtractorProvider(),
    )

    structured = [
        candidate
        for candidate in result["candidates"]
        if candidate.get("extraction_method") == "structured_metadata"
    ]
    assert len(structured) == 1
    assert structured[0]["metadata"]["structured_event_source_count"] == 2
