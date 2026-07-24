from __future__ import annotations

from pathlib import Path

from pkm_brain.cos_action_prechecks import apply_event_fact_action_precheck
from pkm_brain.cos_actions import get_action, propose_action
from pkm_brain.db import connection, dumps
from pkm_brain.extraction import (
    evidence_units_for_text,
    validate_extracted_facts_with_report,
)
from pkm_brain.gmail_temporal import (
    gmail_temporal_review_reason,
    stabilize_gmail_event_time,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.temporal import fact_matches_event_time


def stabilize(
    evidence: str,
    event_time: dict[str, object],
    *,
    mentions: list[dict[str, object]] | None = None,
):
    return stabilize_gmail_event_time(
        source_type="gmail_thread",
        raw_event_time=event_time,
        evidence_text=evidence,
        entity_mentions=list(mentions or []),
        cited_spans=[{"chunk_id": "chunk", "start": 0, "end": len(evidence)}],
        chunk_context_by_id={"chunk": {"text": evidence}},
    )


def test_local_times_are_downgraded_and_grounded_event_is_synthesized() -> None:
    evidence = (
        "A reservation is scheduled for the Cedar Suite from May 14, 2026 at "
        "4:00 PM through May 15, 2026 at 11:00 AM."
    )
    place_start = evidence.index("Cedar Suite")
    result = stabilize(
        evidence,
        {
            "kind": "planned",
            "start_at": "May 14, 2026 at 4:00 PM",
            "end_at": "May 15, 2026 at 11:00 AM",
            "precision": "exact",
            "expression": (
                "May 14, 2026 at 4:00 PM through May 15, 2026 at 11:00 AM"
            ),
        },
        mentions=[
            {
                "surface": "Cedar Suite",
                "entity_type": "place",
                "mention_kind": "named",
                "is_primary": True,
                "mention_span": {
                    "chunk_id": "chunk",
                    "start": place_start,
                    "end": place_start + len("Cedar Suite"),
                },
            }
        ],
    )

    assert result.errors == ()
    assert result.event_time == {
        "kind": "planned",
        "start_at": "2026-05-14",
        "end_at": "2026-05-16",
        "precision": "day",
        "expression": (
            "May 14, 2026 at 4:00 PM through May 15, 2026 at 11:00 AM"
        ),
    }
    primary = next(item for item in result.entity_mentions if item["is_primary"])
    assert primary["entity_type"] == "event"
    assert primary["mention_kind"] == "named"
    assert "reservation" in str(primary["surface"]).lower()
    assert result.audit == {
        "status": "stabilized",
        "basis": "literal_cited_expression",
        "precision": "day",
        "event_identity": "synthesized_grounded_phrase",
        "time_of_day_discarded": True,
        "inclusive_end_day_envelope": True,
    }
    assert fact_matches_event_time({"event_time": result.event_time}, "2026-05-15")
    assert not fact_matches_event_time(
        {"event_time": result.event_time}, "2026-05-16"
    )


def test_day_first_flight_date_is_repaired_without_inventing_timezone() -> None:
    evidence = (
        "Northwind flight NW 331 from Beijing to Hong Kong is scheduled to "
        "depart at 16:40 on Monday, 22 June 2026, with boarding at 16:10."
    )
    result = stabilize(
        evidence,
        {
            "kind": "planned",
            "start_at": "Monday, 22 June 2026 at 16:40",
            "precision": "exact",
            "expression": "16:40 on Monday, 22 June 2026",
        },
    )

    assert result.errors == ()
    assert result.event_time == {
        "kind": "planned",
        "start_at": "2026-06-22",
        "end_at": None,
        "precision": "day",
        "expression": "16:40 on Monday, 22 June 2026",
    }
    primary = next(item for item in result.entity_mentions if item["is_primary"])
    assert primary["entity_type"] == "event"
    assert primary["surface"] == (
        "Northwind flight NW 331 from Beijing to Hong Kong"
    )


def test_precision_mismatch_is_rederived_from_literal_dates() -> None:
    evidence = (
        "The reservation for Cedar Suite is scheduled for check-in on August "
        "12, 2026 and checkout on August 20, 2026."
    )
    result = stabilize(
        evidence,
        {
            "kind": "planned",
            "start_at": "2026-08-12",
            "end_at": "2026-08-20",
            "precision": "exact",
            "expression": "August 12, 2026 and checkout on August 20, 2026",
        },
    )

    assert result.errors == ()
    assert result.event_time and result.event_time["precision"] == "day"
    assert result.event_time["start_at"] == "2026-08-12"
    assert result.event_time["end_at"] == "2026-08-21"


def test_synthesized_event_phrase_is_tied_to_the_temporal_expression() -> None:
    evidence = (
        "Orchid Interview is scheduled on May 1, 2026. "
        "The Cedar reservation is scheduled on May 14, 2026."
    )
    result = stabilize(
        evidence,
        {
            "kind": "planned",
            "start_at": "May 14, 2026",
            "precision": "day",
            "expression": "May 14, 2026",
        },
    )

    primary = next(item for item in result.entity_mentions if item["is_primary"])
    assert "Cedar reservation" in primary["surface"]
    assert "Orchid Interview" not in primary["surface"]


def test_kind_must_match_a_literal_schedule_or_occurrence_cue() -> None:
    scheduled = "Orchid Interview is scheduled on May 14, 2026."
    wrong_actual = stabilize(
        scheduled,
        {
            "kind": "actual",
            "start_at": "2026-05-14",
            "precision": "day",
            "expression": "May 14, 2026",
        },
    )
    occurred = "Orchid Interview occurred on May 14, 2026."
    wrong_planned = stabilize(
        occurred,
        {
            "kind": "planned",
            "start_at": "2026-05-14",
            "precision": "day",
            "expression": "May 14, 2026",
        },
    )

    assert wrong_actual.event_time is None
    assert wrong_planned.event_time is None
    assert wrong_actual.errors == wrong_planned.errors == (
        "gmail event_time kind is not supported by the cited temporal predicate",
    )


def test_unrelated_dates_are_not_combined_into_an_event_interval() -> None:
    evidence = (
        "Orchid Interview is scheduled on May 1, 2026; applications close "
        "June 1, 2026."
    )
    result = stabilize(
        evidence,
        {
            "kind": "planned",
            "start_at": "2026-05-01",
            "end_at": "2026-06-01",
            "precision": "day",
            "expression": "May 1, 2026; applications close June 1, 2026",
        },
    )

    assert result.event_time is None
    assert result.errors == (
        "gmail event_time requires an explicit relation between two dates",
    )


def test_generic_or_non_temporal_meeting_phrase_is_never_promoted() -> None:
    generic = stabilize(
        "The meeting is scheduled on May 14, 2026.",
        {
            "kind": "planned",
            "start_at": "2026-05-14",
            "precision": "day",
            "expression": "May 14, 2026",
        },
        mentions=[
            {
                "surface": "meeting",
                "entity_type": "event",
                "mention_kind": "named",
                "is_primary": True,
                "mention_span": {
                    "chunk_id": "chunk",
                    "start": 4,
                    "end": 11,
                },
            }
        ],
    )
    room_change = stabilize(
        "The team meeting room changed on May 14, 2026.",
        {
            "kind": "actual",
            "start_at": "2026-05-14",
            "precision": "day",
            "expression": "May 14, 2026",
        },
    )

    assert generic.event_time is None
    assert room_change.event_time is None
    assert generic.errors == (
        "gmail event_time requires a grounded specific event phrase",
    )
    assert room_change.errors == (
        "gmail event_time requires its expression and event in one cited sentence",
    )


def test_artifact_owned_event_head_is_not_stabilized_as_a_timed_event() -> None:
    samples = (
        (
            "The recording of the seminar is scheduled for deletion on "
            "November 3, 2029.",
            "The Juniper seminar is scheduled for November 3, 2029.",
            "November 3, 2029",
            "2029-11-03",
            "seminar",
        ),
        (
            "The transcript from the briefing is scheduled for publication on "
            "November 4, 2029.",
            "The Juniper briefing is scheduled for November 4, 2029.",
            "November 4, 2029",
            "2029-11-04",
            "briefing",
        ),
    )

    for artifact_text, genuine_text, expression, expected_start, event_head in samples:
        raw_event_time = {
            "kind": "planned",
            "start_at": expected_start,
            "precision": "day",
            "expression": expression,
        }
        blocked = stabilize(artifact_text, raw_event_time)
        genuine = stabilize(genuine_text, raw_event_time)

        assert blocked.event_time is None
        assert blocked.errors == (
            "gmail event_time requires its expression and event in one cited sentence",
        )
        assert genuine.errors == ()
        assert genuine.event_time and genuine.event_time["start_at"] == expected_start
        primary = next(
            mention for mention in genuine.entity_mentions if mention["is_primary"]
        )
        assert event_head in str(primary["surface"]).casefold()


def test_nearest_event_predicate_owns_the_selected_expression() -> None:
    evidence = (
        "Orchid Interview is scheduled on May 1, 2026, and the Cedar "
        "reservation is scheduled on May 14, 2026."
    )
    result = stabilize(
        evidence,
        {
            "kind": "planned",
            "start_at": "2026-05-14",
            "precision": "day",
            "expression": "May 14, 2026",
        },
    )

    primary = next(item for item in result.entity_mentions if item["is_primary"])
    assert "Cedar" in primary["surface"]
    assert "Orchid" not in primary["surface"]


def test_unrelated_infinitive_does_not_create_a_two_date_interval() -> None:
    evidence = (
        "Orchid Interview is scheduled on May 1, 2026; remember to submit "
        "feedback by June 1, 2026."
    )
    result = stabilize(
        evidence,
        {
            "kind": "planned",
            "start_at": "2026-05-01",
            "end_at": "2026-06-01",
            "precision": "day",
            "expression": (
                "May 1, 2026; remember to submit feedback by June 1, 2026"
            ),
        },
    )

    assert result.event_time is None
    assert result.errors == (
        "gmail event_time requires an explicit relation between two dates",
    )


def test_unrelated_deadline_does_not_flip_schedule_to_end_only() -> None:
    evidence = (
        "The application deadline is June 1, 2026, and Orchid Interview is "
        "scheduled on May 14, 2026."
    )
    result = stabilize(
        evidence,
        {
            "kind": "planned",
            "end_at": "2026-05-14",
            "precision": "day",
            "expression": "May 14, 2026",
        },
    )

    assert result.event_time and result.event_time["start_at"] == "2026-05-14"
    assert result.event_time["end_at"] is None


def test_present_tense_itinerary_predicate_is_a_planned_schedule() -> None:
    result = stabilize(
        "Northwind flight NW 331 departs June 22, 2026.",
        {
            "kind": "planned",
            "start_at": "2026-06-22",
            "precision": "day",
            "expression": "June 22, 2026",
        },
    )

    assert result.event_time and result.event_time["kind"] == "planned"
    assert result.event_time["start_at"] == "2026-06-22"


def test_confirmation_action_date_is_not_treated_as_scheduled_for() -> None:
    result = stabilize(
        "The Cedar reservation was confirmed in our system on May 14, 2026.",
        {
            "kind": "planned",
            "start_at": "2026-05-14",
            "precision": "day",
            "expression": "May 14, 2026",
        },
    )

    assert result.event_time is None


def test_reschedule_uses_the_grounded_event_name_and_new_target_date() -> None:
    result = stabilize(
        (
            "Orchid Interview was scheduled on May 1, 2026 and rescheduled "
            "to May 14, 2026."
        ),
        {
            "kind": "planned",
            "start_at": "2026-05-14",
            "precision": "day",
            "expression": "May 14, 2026",
        },
    )

    assert result.event_time and result.event_time["start_at"] == "2026-05-14"
    primary = next(item for item in result.entity_mentions if item["is_primary"])
    assert primary["surface"] == "Orchid Interview"


def test_literal_iso_timestamp_with_offset_can_remain_exact() -> None:
    evidence = (
        "Orchid Interview is scheduled to start at "
        "2026-05-14T16:00:00-07:00."
    )
    event_start = evidence.index("Orchid Interview")
    result = stabilize(
        evidence,
        {
            "kind": "planned",
            "start_at": "2026-05-14T16:00:00-07:00",
            "precision": "exact",
            "expression": "2026-05-14T16:00:00-07:00",
        },
        mentions=[
            {
                "surface": "Orchid Interview",
                "entity_type": "event",
                "mention_kind": "named",
                "is_primary": True,
                "mention_span": {
                    "chunk_id": "chunk",
                    "start": event_start,
                    "end": event_start + len("Orchid Interview"),
                },
            }
        ],
    )

    assert result.errors == ()
    assert result.event_time and result.event_time["precision"] == "exact"
    assert result.event_time["start_at"] == "2026-05-14T16:00:00-07:00"
    assert result.audit and result.audit["time_of_day_discarded"] is False


def test_ambiguous_date_or_missing_event_phrase_stays_untimed() -> None:
    missing_year = stabilize(
        "The Cedar reservation starts May 14 at 4:00 PM.",
        {
            "kind": "planned",
            "start_at": "May 14 at 4:00 PM",
            "precision": "exact",
            "expression": "May 14 at 4:00 PM",
        },
    )
    no_event = stabilize(
        "Alice will be in Paris on May 14, 2026.",
        {
            "kind": "planned",
            "start_at": "2026-05-14",
            "precision": "day",
            "expression": "May 14, 2026",
        },
    )

    assert missing_year.event_time is None
    assert missing_year.errors == (
        "gmail event_time requires an explicit unambiguous date and year",
    )
    assert no_event.event_time is None
    assert no_event.errors == (
        "gmail event_time requires its expression and event in one cited sentence",
    )
    assert no_event.entity_mentions == []


def test_synthesized_event_identity_never_persists_a_sensitive_locator() -> None:
    evidence = (
        "Reservation #ABC123 for Cedar Suite is scheduled on May 14, 2026."
    )
    result = stabilize(
        evidence,
        {
            "kind": "planned",
            "start_at": "2026-05-14",
            "precision": "day",
            "expression": "May 14, 2026",
        },
    )

    assert result.event_time is None
    assert result.entity_mentions == []
    assert result.errors == (
        "gmail event_time requires a grounded specific event phrase",
    )


def test_extraction_wires_repair_and_unresolved_warning(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    text = (
        "The reservation for Cedar Suite is scheduled from May 14, 2026 at "
        "4:00 PM through May 15, 2026 at 11:00 AM.\n"
        "Alice will be in Paris on May 20, 2026."
    )
    _insert_gmail_chunk(paths, text)
    unit_by_text = {
        unit["text"]: unit["unit_id"] for unit in evidence_units_for_text(text)
    }
    common = {
        "chunk_id": "chunk_gmail_temporal",
        "claim_class": "factual_update",
        "page_hint": "concepts/travel.md",
        "section_hint": "Summary",
        "extraction_confidence": 0.9,
        "routing_confidence": 0.9,
        "truth_confidence": 0.9,
    }
    reservation = text.splitlines()[0]
    report = validate_extracted_facts_with_report(
        paths,
        [
            {
                **common,
                "statement": reservation,
                "evidence_unit_ids": [unit_by_text[reservation]],
                "entities": [
                    {
                        "surface": "Cedar Suite",
                        "type": "place",
                        "mention_kind": "named",
                        "is_primary": True,
                    }
                ],
                "event_time": {
                    "kind": "planned",
                    "start_at": "May 14, 2026 at 4:00 PM",
                    "end_at": "May 15, 2026 at 11:00 AM",
                    "precision": "exact",
                    "expression": (
                        "May 14, 2026 at 4:00 PM through May 15, 2026 at "
                        "11:00 AM"
                    ),
                },
            },
            {
                **common,
                "statement": "Alice will be in Paris on May 20, 2026.",
                "evidence_unit_ids": [
                    unit_by_text["Alice will be in Paris on May 20, 2026."]
                ],
                "entities": [
                    {
                        "surface": "Paris",
                        "type": "place",
                        "mention_kind": "named",
                        "is_primary": True,
                    }
                ],
                "event_time": {
                    "kind": "planned",
                    "start_at": "2026-05-20",
                    "precision": "day",
                    "expression": "May 20, 2026",
                },
            },
        ],
    )

    assert report["accepted_count"] == 2
    assert report["temporal_enrichment_warning_count"] == 1
    repaired, unresolved = report["candidates"]
    assert repaired["event_time_precision"] == "day"
    assert repaired["entity_type"] == "event"
    assert repaired["metadata"]["source_type"] == "gmail_thread"
    assert repaired["metadata"]["gmail_event_time_stabilization"]["status"] == (
        "stabilized"
    )
    assert unresolved["event_time"] is None
    assert gmail_temporal_review_reason(unresolved) is not None


def test_normal_policy_precheck_does_not_escalate_stripped_optional_time(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    fact = {
        "statement": "A grounded base fact remains useful without event timing.",
        "page_hint": "concepts/travel.md",
        "metadata": {
            "source_type": "gmail_thread",
            "temporal_enrichment_warnings": [
                {
                    "enrichment": "event_time",
                    "reasons": ["unsupported clock"],
                }
            ],
        },
    }
    action = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": fact},
        action_features={"clean_fact_upsert": True, "reversible": True},
        risk_tier="medium",
    )
    with connection(paths.sqlite_path) as conn:
        apply_event_fact_action_precheck(conn, action)
    checked = get_action(paths, action["id"])

    assert checked["risk_tier"] == action["risk_tier"]
    assert checked["action_features"]["clean_fact_upsert"] is True
    assert checked["status"] == "proposed"


def _insert_gmail_chunk(paths: BrainPaths, text: str) -> None:
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO documents(
              id, source_type, title, source_path, raw_path, content_hash,
              created_at, ingested_at, tags, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "doc_gmail_temporal",
                "gmail_thread",
                "Synthetic temporal test",
                "raw/gmail/temporal.md",
                "raw/gmail/temporal.md",
                "doc-hash",
                "2026-07-18T00:00:00+00:00",
                "2026-07-18T00:00:00+00:00",
                dumps(
                    [
                        "gmail:delivery:transactional",
                        "gmail:importance:important-temporal",
                        "gmail:fact-eligible",
                    ]
                ),
                "active",
            ),
        )
        conn.execute(
            """
            INSERT INTO chunks(
              id, document_id, chunk_index, corpus_type, text, heading_path,
              start_offset, end_offset, token_count, content_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "chunk_gmail_temporal",
                "doc_gmail_temporal",
                0,
                "raw",
                text,
                "",
                0,
                len(text),
                50,
                "chunk-hash",
                "2026-07-18T00:00:00+00:00",
            ),
        )
