from __future__ import annotations

from pathlib import Path

from pkm_brain.db import connection, dumps
from pkm_brain.extraction import (
    evidence_units_for_text,
    extraction_prompt,
    validate_extracted_facts_with_report,
)
from pkm_brain.gmail_fact_quality import evaluate_gmail_fact_quality
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


TRANSACTIONAL_TAGS = [
    "gmail:delivery:transactional",
    "gmail:importance:important-temporal",
    "gmail:fact-eligible",
]
EVENT_ENTITY = [
    {
        "surface": "Orchid Stay",
        "type": "event",
        "mention_kind": "named",
        "is_primary": True,
    }
]


def quality(
    statement: str,
    claim_class: str,
    evidence: str,
    *,
    tags: list[str] | None = None,
    entities: list[dict[str, object]] | None = None,
    source_type: str = "gmail_thread",
):
    return evaluate_gmail_fact_quality(
        source_type=source_type,
        source_tags=tags if tags is not None else TRANSACTIONAL_TAGS,
        statement=statement,
        claim_class=claim_class,
        evidence_text=evidence,
        entities=entities,
    )


def test_request_is_not_a_commitment_without_explicit_acceptance() -> None:
    rejected = quality(
        "Michelle requested sofa-bed preparation for Orchid Stay.",
        "commitment",
        "Could you please prepare the sofa bed for Orchid Stay?",
        entities=EVENT_ENTITY,
    )
    accepted = quality(
        "The host committed to prepare the sofa bed for Orchid Stay.",
        "commitment",
        "Could you prepare the sofa bed? We will prepare it for Orchid Stay.",
        entities=EVENT_ENTITY,
    )
    reported_request = quality(
        "Michelle requested sofa-bed preparation for Orchid Stay.",
        "commitment",
        "The sofa bed would be prepared for you.",
        entities=EVENT_ENTITY,
    )

    assert rejected.disposition == "reject"
    assert rejected.reason and "request evidence" in rejected.reason
    assert reported_request.disposition == "reject"
    assert accepted.disposition == "accept"


def test_conditional_scheduling_instruction_is_not_an_open_question() -> None:
    decision = quality(
        "If you would like to schedule an appointment, contact the office.",
        "open_question",
        "If you would like to schedule an appointment, contact the office.",
    )
    actual_question = quality(
        "The meeting time remains unresolved.",
        "open_question",
        "What time should we schedule the meeting?",
    )
    declarative_condition = quality(
        "If the intake is approved, the recipient can contact the office.",
        "open_question",
        "After approval, the recipient can contact the office.",
    )

    assert decision.disposition == "drop"
    assert decision.reason == "gmail_non_durable_conditional_scheduling_instruction"
    assert declarative_condition.disposition == "drop"
    assert actual_question.disposition == "accept"


def test_gmail_prompt_states_semantic_and_durability_boundary() -> None:
    prompt = extraction_prompt(
        {
            "document": {"id": "doc-gmail", "source_type": "gmail_thread"},
            "window": {"chunks": []},
            "routing_hints": [],
        }
    )

    assert "A request is not a commitment" in prompt
    assert "conditional instruction about how to schedule" in prompt
    assert "context-specific rule as universal" in prompt
    assert "core event identity, schedule" in prompt


def test_context_specific_legal_rule_must_keep_its_jurisdiction() -> None:
    generalized = quality(
        "Hotels must retain guest registration records.",
        "factual_update",
        "Under Hong Kong law, hotels must retain guest registration records.",
    )
    qualified = quality(
        "Hong Kong hotels must retain guest registration records under Hong Kong law.",
        "factual_update",
        "Under Hong Kong law, hotels must retain guest registration records.",
    )

    assert generalized.disposition == "reject"
    assert generalized.reason == "gmail_context_qualifier_omitted_from_general_rule"
    assert qualified.disposition == "accept"


def test_one_off_rule_cannot_be_promoted_to_a_generic_policy() -> None:
    generalized = quality(
        "Guests may arrive after 3 PM.",
        "factual_update",
        "For this reservation, you may arrive after 3 PM.",
        entities=EVENT_ENTITY,
    )
    qualified = quality(
        "Guests on this reservation may arrive after 3 PM.",
        "factual_update",
        "For this reservation, guests may arrive after 3 PM.",
        entities=EVENT_ENTITY,
    )

    assert generalized.disposition == "reject"
    assert qualified.disposition == "accept"


def test_source_specific_operational_rule_must_keep_its_authority_scope() -> None:
    generalized = quality(
        "Passengers must store battery packs in carry-on baggage.",
        "factual_update",
        "You must store battery packs in your carry-on baggage.",
    )
    qualified = quality(
        "Passengers on Northwind flights must store battery packs in carry-on baggage.",
        "factual_update",
        "You must store battery packs in your carry-on baggage.",
        entities=[
            {
                "surface": "Northwind",
                "type": "organization",
                "mention_kind": "named",
                "is_primary": False,
            }
        ],
    )

    assert generalized.disposition == "reject"
    assert generalized.reason == "gmail_context_qualifier_omitted_from_general_rule"
    assert qualified.disposition == "accept"


def test_access_details_and_routine_transactional_attributes_are_not_facts() -> None:
    booking_reference = quality(
        "Orchid Stay has booking reference ABC123.",
        "factual_update",
        "The booking reference for Orchid Stay is ABC123.",
        entities=EVENT_ENTITY,
    )
    guest_count = quality(
        "Orchid Stay has two adult guests.",
        "factual_update",
        "Orchid Stay is reserved for two adults.",
        entities=EVENT_ENTITY,
    )
    payment_amount = quality(
        "The payment amount for Orchid Stay is $420.",
        "factual_update",
        "The payment amount for Orchid Stay is $420.",
        entities=EVENT_ENTITY,
    )

    assert booking_reference.reason == "gmail_non_durable_access_detail"
    assert guest_count.reason == "gmail_non_durable_one_off_event_attribute"
    assert payment_amount.reason == "gmail_non_durable_one_off_event_attribute"


def test_core_event_schedule_identity_and_obligation_survive() -> None:
    schedule = quality(
        "Check-in for Orchid Stay starts at 3:00 PM.",
        "factual_update",
        "Check-in for Orchid Stay starts at 3:00 PM.",
        entities=EVENT_ENTITY,
    )
    identity = quality(
        "The Orchid Stay reservation is confirmed.",
        "project_state",
        "The Orchid Stay reservation is confirmed.",
        entities=EVENT_ENTITY,
    )
    obligation = quality(
        "The remaining payment for Orchid Stay is due by July 20.",
        "factual_update",
        "The remaining payment for Orchid Stay is due by July 20.",
        entities=EVENT_ENTITY,
    )

    assert schedule.disposition == "accept"
    assert identity.disposition == "accept"
    assert obligation.disposition == "accept"


def test_routine_attribute_guard_is_bounded_to_transactional_event_mail() -> None:
    human_correspondence = quality(
        "Project Atlas has two adult research participants.",
        "factual_update",
        "Project Atlas has two adult research participants.",
        tags=["gmail:delivery:human", "gmail:fact-eligible"],
        entities=[
            {
                "surface": "Project Atlas",
                "type": "project",
                "mention_kind": "named",
                "is_primary": True,
            }
        ],
    )
    non_gmail = quality(
        "Orchid Stay has two adult guests.",
        "factual_update",
        "Orchid Stay has two adult guests.",
        entities=EVENT_ENTITY,
        source_type="note",
    )
    product_capability = quality(
        "TravelApp supports booking-reference and access-code management.",
        "factual_update",
        "TravelApp supports booking-reference and access-code management for a booking.",
        entities=[
            {
                "surface": "TravelApp",
                "type": "product",
                "mention_kind": "named",
                "is_primary": True,
            }
        ],
    )

    assert human_correspondence.disposition == "accept"
    assert non_gmail.disposition == "accept"
    assert product_capability.disposition == "accept"


def test_extraction_validation_applies_gmail_quality_gate_before_actions(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    chunk_text = (
        "Michelle requested sofa-bed preparation for Orchid Stay.\n"
        "If you would like to schedule an appointment, contact the office.\n"
        "Under Hong Kong law, hotels must retain guest registration records.\n"
        "Orchid Stay has two adult guests."
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
                "doc_gmail_quality",
                "gmail_thread",
                "Private Gmail thread",
                "raw/gmail/thread.md",
                "raw/gmail/thread.md",
                "doc-hash",
                "2026-07-18T00:00:00+00:00",
                "2026-07-18T00:00:00+00:00",
                dumps(TRANSACTIONAL_TAGS),
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
                "chunk_gmail_quality",
                "doc_gmail_quality",
                0,
                "raw",
                chunk_text,
                "",
                0,
                len(chunk_text),
                40,
                "chunk-hash",
                "2026-07-18T00:00:00+00:00",
            ),
        )
    unit_by_text = {
        unit["text"]: unit["unit_id"] for unit in evidence_units_for_text(chunk_text)
    }

    report = validate_extracted_facts_with_report(
        paths,
        [
            _fact(
                "Michelle requested sofa-bed preparation for Orchid Stay.",
                "commitment",
                unit_by_text,
                entities=EVENT_ENTITY,
            ),
            _fact(
                "If you would like to schedule an appointment, contact the office.",
                "open_question",
                unit_by_text,
            ),
            _fact(
                "Hotels must retain guest registration records.",
                "factual_update",
                unit_by_text,
                evidence="Under Hong Kong law, hotels must retain guest registration records.",
            ),
            _fact(
                "Orchid Stay has two adult guests.",
                "factual_update",
                unit_by_text,
                entities=EVENT_ENTITY,
            ),
        ],
    )

    assert report["accepted_count"] == 0
    assert report["rejected_count"] == 2
    assert report["dropped_count"] == 2
    assert {
        reason
        for rejection in report["rejections"]
        for reason in rejection["reasons"]
    } == {
        "gmail_claim_class_mismatch: request evidence does not establish a commitment",
        "gmail_context_qualifier_omitted_from_general_rule",
    }
    assert {item["reason"] for item in report["dropped"]} == {
        "gmail_non_durable_conditional_scheduling_instruction",
        "gmail_non_durable_one_off_event_attribute",
    }


def _fact(
    statement: str,
    claim_class: str,
    unit_by_text: dict[str, str],
    *,
    evidence: str | None = None,
    entities: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "statement": statement,
        "chunk_id": "chunk_gmail_quality",
        "evidence_unit_ids": [unit_by_text[evidence or statement]],
        "claim_class": claim_class,
        "entities": entities or [],
    }
