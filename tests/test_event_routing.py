from __future__ import annotations

from pathlib import Path

import pytest

from pkm_brain.cos_actions import apply_action, decide_action, get_action, propose_action
from pkm_brain.db import connection
from pkm_brain.entities import resolve_entity
from pkm_brain.event_routing import (
    candidate_event_intervals,
    explicit_date_intervals,
    guard_event_candidate_route,
    guard_event_candidate_routes,
    intervals_compatible,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


def service_for(tmp_path: Path) -> BrainService:
    return BrainService(BrainPaths.from_value(tmp_path / "brain"))


def event_candidate(
    *,
    statement: str,
    evidence_quote: str,
    start_at: str | None,
    end_at: str | None,
    source_id: str = "gmail:thread-one",
) -> dict[str, object]:
    event_time = (
        {
            "kind": "planned",
            "start_at": start_at,
            "end_at": end_at,
            "precision": "day",
            "expression": "July 28-30, 2026",
        }
        if start_at
        else None
    )
    return {
        "statement": statement,
        "entity_key": "events:grand-hall:summary",
        "entity_mention": "Summit at Grand Hall",
        "entity_type": "event",
        "entity_mentions": [
            {
                "surface": "Summit at Grand Hall",
                "entity_type": "event",
                "mention_kind": "named",
                "is_primary": True,
            }
        ],
        "page_hint": "events/grand-hall.md",
        "section_hint": "Summary",
        "source_ids": [source_id],
        "source_spans": [{"document_id": "doc_mail", "start": 0, "end": 50}],
        "evidence_quote": evidence_quote,
        "event_time": event_time,
        "event_start_at": start_at,
        "event_end_at": end_at,
        "confidence": 0.95,
        "extraction_confidence": 0.95,
        "routing_confidence": 0.95,
        "truth_confidence": 0.95,
        "extraction_method": "llm",
        "metadata": {
            "source": "source_to_facts_extraction",
            "model_entity_key": "Summit at Grand Hall",
            "routing": {
                "original_page_hint": "events/grand-hall.md",
                "normalized_page_hint": "events/grand-hall.md",
                "route_destination_valid": True,
                "route_target_exists": True,
                "route_resolution": "existing_canonical_page",
            },
        },
    }


def grand_hall_target() -> dict[str, dict[str, object]]:
    return {
        "events/grand-hall.md": {
            "page_hint": "events/grand-hall.md",
            "canonical_entity": "Summit at Grand Hall",
            "_event_occurrence_intervals": [["2026-07-25", "2026-07-27"]],
        }
    }


def attached_entity_candidate(
    *,
    entity_type: str,
    entity_surface: str,
    statement: str,
    evidence_quote: str | None = None,
    page_hint: str = "events/grand-hall.md",
    source_id: str = "gmail:thread-one",
) -> dict[str, object]:
    return {
        "statement": statement,
        "entity_key": "events:grand-hall:summary",
        "entity_mention": entity_surface,
        "entity_type": entity_type,
        "entity_mentions": [
            {
                "surface": entity_surface,
                "entity_type": entity_type,
                "mention_kind": "named",
                "is_primary": True,
            }
        ],
        "page_hint": page_hint,
        "section_hint": "Summary",
        "source_ids": [source_id],
        "source_spans": [{"document_id": "doc_mail", "start": 0, "end": 50}],
        "evidence_quote": evidence_quote or statement,
        "confidence": 0.95,
        "extraction_confidence": 0.95,
        "routing_confidence": 0.95,
        "truth_confidence": 0.95,
        "extraction_method": "llm",
        "metadata": {
            "source": "source_to_facts_extraction",
            "model_entity_key": entity_surface,
            "routing": {
                "original_page_hint": page_hint,
                "normalized_page_hint": page_hint,
                "route_destination_valid": True,
                "route_target_exists": True,
                "route_resolution": "existing_canonical_page",
            },
        },
    }


def test_explicit_date_parser_preserves_same_and_cross_month_ranges() -> None:
    assert explicit_date_intervals("July 28-30, 2026") == [
        ("2026-07-28", "2026-07-30")
    ]
    assert explicit_date_intervals("July 31-August 2, 2026") == [
        ("2026-07-31", "2026-08-02")
    ]


def test_unaccepted_batch_candidate_cannot_anchor_an_undated_sibling() -> None:
    occurrence = event_candidate(
        statement="Summit at Grand Hall runs July 28-30, 2026.",
        evidence_quote="Summit at Grand Hall runs July 28-30, 2026.",
        start_at="2026-07-28",
        end_at="2026-07-31",
    )
    attribute = event_candidate(
        statement="Summit at Grand Hall includes an executive workshop.",
        evidence_quote="Summit at Grand Hall includes an executive workshop.",
        start_at=None,
        end_at=None,
    )

    guarded = guard_event_candidate_routes([occurrence, attribute], {})

    assert guarded[0]["page_hint"] == (
        "events/summit-at-grand-hall-2026-07-28.md"
    )
    assert guarded[1]["page_hint"] == "events/grand-hall.md"
    assert guarded[1]["metadata"]["routing"]["route_destination_valid"] is False
    assert guarded[1]["metadata"]["routing"]["route_review_reason"] == (
        "event_temporal_identity_unresolved"
    )


def test_nonexistent_dated_event_path_is_not_occurrence_evidence() -> None:
    candidate = attached_entity_candidate(
        entity_type="person",
        entity_surface="Jordan Vale",
        statement="Jordan Vale welcomed attendees.",
        page_hint="events/invented-summit-2026-07-28.md",
    )

    guarded = guard_event_candidate_route(candidate, {})

    assert guarded["metadata"]["routing"]["route_destination_valid"] is False
    assert guarded["metadata"]["routing"]["route_review_reason"] == (
        "event_temporal_identity_unresolved"
    )


def test_direct_evidence_date_can_ground_a_new_event_page_attachment() -> None:
    candidate = attached_entity_candidate(
        entity_type="person",
        entity_surface="Jordan Vale",
        statement="Jordan Vale attended on July 28, 2026.",
        page_hint="events/new-summit-2026-07-28.md",
    )

    guarded = guard_event_candidate_route(candidate, {})

    routing = guarded["metadata"]["routing"]
    assert routing["route_destination_valid"] is True
    assert routing["route_target_exists"] is False
    assert routing["route_resolution"] == (
        "event_candidate_occurrence_compatible"
    )


def test_gmail_route_cannot_recreate_time_rejected_by_temporal_repair() -> None:
    candidate = attached_entity_candidate(
        entity_type="person",
        entity_surface="Jordan Vale",
        statement="Jordan Vale attended on July 28, 2026.",
        page_hint="events/new-summit-2026-07-28.md",
    )
    candidate["metadata"]["source_type"] = "gmail_thread"
    candidate["event_start_at"] = "2026-07-28"
    candidate["event_end_at"] = "2026-07-29"

    guarded = guard_event_candidate_route(candidate, {})

    assert candidate_event_intervals(candidate) == []
    assert guarded["metadata"]["routing"]["route_destination_valid"] is False
    assert guarded["metadata"]["routing"]["route_review_reason"] == (
        "event_temporal_identity_unresolved"
    )


def test_half_open_end_does_not_overlap_an_adjacent_day_occurrence() -> None:
    first = event_candidate(
        statement="Summit at Grand Hall runs May 14-15, 2026.",
        evidence_quote="Summit at Grand Hall runs May 14-15, 2026.",
        start_at="2026-05-14",
        end_at="2026-05-16",
    )
    first["event_time"]["expression"] = "May 14-15, 2026"
    adjacent = event_candidate(
        statement="Summit at Grand Hall returns May 16, 2026.",
        evidence_quote="Summit at Grand Hall returns May 16, 2026.",
        start_at="2026-05-16",
        end_at="2026-05-17",
    )
    adjacent["event_time"]["expression"] = "May 16, 2026"

    first_intervals = candidate_event_intervals(first)
    adjacent_intervals = candidate_event_intervals(adjacent)

    assert first_intervals == [("2026-05-14", "2026-05-15")]
    assert adjacent_intervals == [("2026-05-16", "2026-05-16")]
    assert not intervals_compatible(first_intervals, adjacent_intervals)


def test_adjacent_legacy_intervals_do_not_ground_one_occurrence() -> None:
    candidate = attached_entity_candidate(
        entity_type="person",
        entity_surface="Jordan Vale",
        statement="Jordan Vale welcomed attendees.",
    )
    targets = {
        "events/grand-hall.md": {
            "page_hint": "events/grand-hall.md",
            "canonical_entity": "Summit at Grand Hall",
            "_event_occurrence_intervals": [
                ["2026-05-15", "2026-05-15"],
                ["2026-05-16", "2026-05-16"],
            ],
        }
    }

    guarded = guard_event_candidate_route(candidate, targets)

    assert guarded["metadata"]["routing"]["route_destination_valid"] is False
    assert guarded["metadata"]["routing"]["route_review_reason"] == (
        "event_temporal_identity_multiple_occurrences"
    )


def test_same_venue_different_date_range_gets_distinct_event_identity() -> None:
    candidate = event_candidate(
        statement="Summit at Grand Hall runs July 28-30, 2026.",
        evidence_quote="Summit at Grand Hall runs July 28-30, 2026.",
        start_at="2026-07-28",
        end_at="2026-07-30",
    )

    guarded = guard_event_candidate_route(candidate, grand_hall_target())

    assert guarded["page_hint"] == (
        "events/summit-at-grand-hall-2026-07-28.md"
    )
    assert guarded["metadata"]["routing"]["route_resolution"] == (
        "event_temporal_identity_reroute"
    )
    assert guarded["entity_mentions"][0]["entity_identity"] == (
        "Summit at Grand Hall (2026-07-28)"
    )


def test_same_day_exact_sessions_keep_distinct_occurrence_pages() -> None:
    candidate = event_candidate(
        statement="Summit at Grand Hall starts at 2:00 PM on July 28, 2026.",
        evidence_quote=(
            "Summit at Grand Hall starts at 2:00 PM on July 28, 2026. "
            "Start: 2026-07-28T14:00:00+00:00"
        ),
        start_at="2026-07-28T14:00:00+00:00",
        end_at="2026-07-28T15:00:00+00:00",
    )
    candidate["event_time"]["precision"] = "exact"
    candidate["event_time"]["expression"] = "July 28, 2026"
    targets = grand_hall_target()
    targets["events/grand-hall.md"]["_event_occurrence_bounds"] = [
        ["2026-07-28T10:00:00+00:00", "2026-07-28T11:00:00+00:00"]
    ]
    targets["events/grand-hall.md"]["_event_occurrence_intervals"] = [
        ["2026-07-28", "2026-07-28"]
    ]

    guarded = guard_event_candidate_route(candidate, targets)

    assert guarded["page_hint"] == (
        "events/summit-at-grand-hall-2026-07-28t14-00-00-00-00.md"
    )


def test_deterministic_route_collision_is_held_instead_of_merged() -> None:
    candidate = event_candidate(
        statement="Summit at Grand Hall runs July 28-30, 2026.",
        evidence_quote="Summit at Grand Hall runs July 28-30, 2026.",
        start_at="2026-07-28",
        end_at="2026-07-30",
    )
    deterministic_page = "events/summit-at-grand-hall-2026-07-28.md"
    candidate["page_hint"] = deterministic_page
    targets = {
        deterministic_page: {
            "page_hint": deterministic_page,
            "_event_occurrence_bounds": [["2026-07-29", "2026-07-30"]],
            "_event_occurrence_intervals": [["2026-07-29", "2026-07-30"]],
        }
    }

    guarded = guard_event_candidate_route(candidate, targets)

    assert guarded["metadata"]["routing"]["route_review_reason"] == (
        "event_temporal_identity_collision"
    )


def test_literal_grounded_range_routes_even_when_model_omits_event_time() -> None:
    candidate = event_candidate(
        statement="Summit at Grand Hall runs July 28-30, 2026.",
        evidence_quote="Summit at Grand Hall runs July 28-30, 2026.",
        start_at=None,
        end_at=None,
    )

    guarded = guard_event_candidate_route(candidate, grand_hall_target())

    assert guarded["page_hint"] == (
        "events/summit-at-grand-hall-2026-07-28.md"
    )
    assert guarded["metadata"]["event_route_temporal_identity"]["intervals"] == [
        ["2026-07-28", "2026-07-30"]
    ]


def test_undergrounded_occurrence_is_locked_for_routing_review() -> None:
    candidate = event_candidate(
        statement="Summit at Grand Hall is scheduled next month.",
        evidence_quote="Summit at Grand Hall is scheduled next month.",
        start_at=None,
        end_at=None,
    )

    guarded = guard_event_candidate_route(candidate, grand_hall_target())

    assert guarded["page_hint"] == "events/grand-hall.md"
    assert guarded["metadata"]["routing"] == {
        **candidate["metadata"]["routing"],
        "route_destination_valid": False,
        "route_resolution": "held_for_routing_review",
        "route_review_reason": "event_temporal_identity_unresolved",
        "event_temporal_identity_guard": "event_temporal_identity_v1",
        "event_temporal_identity_guard_locked": True,
    }


def test_one_accepted_source_event_keeps_undated_attributes_on_its_route() -> None:
    occurrence = event_candidate(
        statement="Summit at Grand Hall runs July 28-30, 2026.",
        evidence_quote="Summit at Grand Hall runs July 28-30, 2026.",
        start_at="2026-07-28",
        end_at="2026-07-30",
    )
    attribute = event_candidate(
        statement="Summit at Grand Hall includes an executive workshop.",
        evidence_quote="Summit at Grand Hall includes an executive workshop.",
        start_at=None,
        end_at=None,
    )

    guarded = guard_event_candidate_routes(
        [occurrence, attribute],
        grand_hall_target(),
        accepted_anchor_candidates=[occurrence],
    )

    assert guarded[0]["page_hint"] == guarded[1]["page_hint"]
    assert guarded[1]["metadata"]["routing"]["route_resolution"] == (
        "event_source_occurrence_coherence"
    )
    assert guarded[1]["entity_mentions"][0]["entity_identity"] == (
        "Summit at Grand Hall (2026-07-28)"
    )


def test_primary_event_checked_in_fact_uses_accepted_occurrence_anchor() -> None:
    occurrence = event_candidate(
        statement="Summit at Grand Hall runs July 28-30, 2026.",
        evidence_quote="Summit at Grand Hall runs July 28-30, 2026.",
        start_at="2026-07-28",
        end_at="2026-07-30",
    )
    checked_in = event_candidate(
        statement="Summit at Grand Hall checked in its first attendees.",
        evidence_quote="Summit at Grand Hall checked in its first attendees.",
        start_at=None,
        end_at=None,
    )

    guarded = guard_event_candidate_routes(
        [occurrence, checked_in],
        grand_hall_target(),
        accepted_anchor_candidates=[occurrence],
    )

    assert guarded[1]["page_hint"] == guarded[0]["page_hint"]
    assert guarded[1]["metadata"]["routing"]["route_resolution"] == (
        "event_source_occurrence_coherence"
    )


def test_two_source_occurrences_do_not_claim_one_undated_attribute() -> None:
    first = event_candidate(
        statement="Summit at Grand Hall runs July 28-30, 2026.",
        evidence_quote="Summit at Grand Hall runs July 28-30, 2026.",
        start_at="2026-07-28",
        end_at="2026-07-30",
    )
    second = event_candidate(
        statement="Summit at Grand Hall returns August 8-9, 2026.",
        evidence_quote="Summit at Grand Hall returns August 8-9, 2026.",
        start_at=None,
        end_at=None,
    )
    attribute = event_candidate(
        statement="Summit at Grand Hall includes an executive workshop.",
        evidence_quote="Summit at Grand Hall includes an executive workshop.",
        start_at=None,
        end_at=None,
    )

    guarded = guard_event_candidate_routes(
        [first, second, attribute],
        grand_hall_target(),
        accepted_anchor_candidates=[first, second],
    )

    assert guarded[0]["page_hint"] != guarded[1]["page_hint"]
    assert guarded[2]["page_hint"] == "events/grand-hall.md"


@pytest.mark.parametrize(
    ("entity_type", "entity_surface", "statement"),
    [
        ("person", "Jordan Vale", "Jordan Vale checked in at Grand Hall."),
        ("place", "Grand Hall", "Grand Hall used its north entrance."),
        ("organization", "Cedar Labs", "Cedar Labs staffed the welcome desk."),
        ("product", "Badge Reader", "Badge Reader handled attendee entry."),
    ],
)
def test_non_event_primary_fact_inherits_unique_source_occurrence(
    entity_type: str, entity_surface: str, statement: str
) -> None:
    occurrence = event_candidate(
        statement="Summit at Grand Hall runs July 28-30, 2026.",
        evidence_quote="Summit at Grand Hall runs July 28-30, 2026.",
        start_at="2026-07-28",
        end_at="2026-07-30",
    )
    attached = attached_entity_candidate(
        entity_type=entity_type,
        entity_surface=entity_surface,
        statement=statement,
    )

    guarded = guard_event_candidate_routes(
        [occurrence, attached],
        grand_hall_target(),
        accepted_anchor_candidates=[occurrence],
    )

    assert guarded[1]["page_hint"] == guarded[0]["page_hint"]
    assert guarded[1]["metadata"]["routing"]["route_resolution"] == (
        "event_source_occurrence_coherence"
    )
    assert "entity_identity" not in guarded[1]["entity_mentions"][0]


def test_existing_event_page_dates_do_not_anchor_an_undated_fact() -> None:
    candidate = attached_entity_candidate(
        entity_type="person",
        entity_surface="Jordan Vale",
        statement="Jordan Vale welcomed attendees.",
    )

    guarded = guard_event_candidate_route(candidate, grand_hall_target())

    assert guarded["page_hint"] == "events/grand-hall.md"
    assert guarded["metadata"]["routing"]["route_destination_valid"] is False
    assert guarded["metadata"]["routing"]["route_review_reason"] == (
        "event_temporal_identity_unresolved"
    )


def test_non_event_fact_conflicting_with_event_page_dates_is_held() -> None:
    candidate = attached_entity_candidate(
        entity_type="person",
        entity_surface="Jordan Vale",
        statement="Jordan Vale attended on July 28, 2026.",
    )

    guarded = guard_event_candidate_route(candidate, grand_hall_target())

    assert guarded["metadata"]["routing"]["route_destination_valid"] is False
    assert guarded["metadata"]["routing"]["route_review_reason"] == (
        "event_temporal_identity_collision"
    )


def test_people_page_named_person_mismatch_is_held() -> None:
    candidate = attached_entity_candidate(
        entity_type="person",
        entity_surface="Jordan Vale",
        statement="Jordan Vale leads the launch review.",
        page_hint="people/morgan-lane.md",
    )
    target = {
        "people/morgan-lane.md": {
            "page_hint": "people/morgan-lane.md",
            "canonical_entity": "Morgan Lane",
        }
    }

    guarded = guard_event_candidate_route(candidate, target)

    assert guarded["metadata"]["routing"]["route_destination_valid"] is False
    assert guarded["metadata"]["routing"]["route_review_reason"] == (
        "person_page_identity_mismatch"
    )


def test_people_page_matching_named_person_remains_routable() -> None:
    candidate = attached_entity_candidate(
        entity_type="person",
        entity_surface="Dr. Morgan Lane",
        statement="Dr. Morgan Lane leads the launch review.",
        page_hint="people/morgan-lane.md",
    )
    target = {
        "people/morgan-lane.md": {
            "page_hint": "people/morgan-lane.md",
            "canonical_entity": "Morgan Lane",
        }
    }

    guarded = guard_event_candidate_route(candidate, target)

    assert guarded["metadata"]["routing"]["route_destination_valid"] is True
    assert guarded["metadata"]["routing"]["route_resolution"] == (
        "person_page_identity_compatible"
    )


def test_people_page_medical_mismatch_uses_sensitive_fail_closed_reason() -> None:
    candidate = attached_entity_candidate(
        entity_type="person",
        entity_surface="Jordan Vale",
        statement="Jordan Vale was prescribed a new medication.",
        page_hint="people/morgan-lane.md",
    )
    target = {
        "people/morgan-lane.md": {
            "page_hint": "people/morgan-lane.md",
            "canonical_entity": "Morgan Lane",
        }
    }

    guarded = guard_event_candidate_route(candidate, target)

    routing = guarded["metadata"]["routing"]
    assert routing["route_review_reason"] == (
        "person_page_identity_sensitive_mismatch"
    )
    assert routing["person_page_identity_sensitive"] is True


def test_people_page_matching_entity_id_does_not_override_surface_conflict() -> None:
    candidate = attached_entity_candidate(
        entity_type="person",
        entity_surface="Jordan Vale",
        statement="Jordan Vale owns the rollout plan.",
        page_hint="people/morgan-lane.md",
    )
    candidate["entity_id"] = "entity_morgan"
    target = {
        "people/morgan-lane.md": {
            "page_hint": "people/morgan-lane.md",
            "canonical_entity": "Morgan Lane",
            "_primary_person_entity_ids": ["entity_morgan"],
            "_primary_person_names": ["Morgan Lane"],
        }
    }

    guarded = guard_event_candidate_route(candidate, target)

    assert guarded["metadata"]["routing"]["route_destination_valid"] is False
    assert guarded["metadata"]["routing"]["route_review_reason"] == (
        "person_page_identity_mismatch"
    )


def test_people_identity_guard_does_not_unlock_prior_invalid_route() -> None:
    candidate = attached_entity_candidate(
        entity_type="person",
        entity_surface="Morgan Lane",
        statement="Morgan Lane owns the rollout plan.",
        page_hint="people/morgan-lane.md",
    )
    candidate["metadata"]["routing"].update(
        {
            "route_destination_valid": False,
            "route_resolution": "held_for_routing_review",
            "route_review_reason": "existing_review_reason",
        }
    )
    target = {
        "people/morgan-lane.md": {
            "page_hint": "people/morgan-lane.md",
            "canonical_entity": "Morgan Lane",
        }
    }

    guarded = guard_event_candidate_route(candidate, target)

    assert guarded["metadata"]["routing"]["route_destination_valid"] is False
    assert guarded["metadata"]["routing"]["route_review_reason"] == (
        "existing_review_reason"
    )


def test_action_precheck_reroutes_exact_existing_page_and_splits_entity(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        existing_entity = resolve_entity(
            conn,
            "Summit at Grand Hall",
            type_hint="event",
            mention_kind="named",
        )
        assert existing_entity is not None
    existing = {
        "id": "fact_grand_hall_first",
        "statement": "Summit at Grand Hall ran July 25-27, 2026.",
        "entity_key": "events:grand-hall:summary",
        "entity_id": existing_entity.entity_id,
        "entity_mention": "Summit at Grand Hall",
        "entity_type": "event",
        "page_hint": "events/grand-hall.md",
        "section_hint": "Summary",
        "source_ids": ["manual:first"],
        "confidence": 0.95,
        "event_time": {
            "kind": "actual",
            "start_at": "2026-07-25",
            "end_at": "2026-07-27",
            "precision": "day",
            "expression": "July 25-27, 2026",
        },
    }
    first_action = propose_action(
        svc.paths,
        "fact_upsert",
        action_payload={"fact": existing},
        action_features={"reversible": True},
    )
    apply_action(svc.paths, first_action["id"])

    second = event_candidate(
        statement="Summit at Grand Hall runs July 28-30, 2026.",
        evidence_quote="Summit at Grand Hall runs July 28-30, 2026.",
        start_at="2026-07-28",
        end_at="2026-07-30",
    )
    second["id"] = "fact_grand_hall_second"
    action = propose_action(
        svc.paths,
        "fact_upsert",
        action_payload={"fact": second},
        action_features={
            "candidate_signal": "source_extraction",
            "clean_fact_upsert": True,
            "fact_upsert_resolution": "new_clean_fact",
            "quote_backed": True,
            "fallback_route": False,
            "resolver_precheck": "passed",
            "reversible": True,
            "truth_mutation": False,
            "confidence": 0.95,
        },
        target_page_paths=["events/grand-hall.md"],
    )

    decided = decide_action(svc.paths, action["id"])
    guarded_fact = decided["evidence_json"]["payload"]["fact"]

    assert guarded_fact["page_hint"] == (
        "events/summit-at-grand-hall-2026-07-28.md"
    )
    assert decided["target_page_paths"] == [guarded_fact["page_hint"]]
    apply_action(svc.paths, action["id"])
    with connection(svc.paths.sqlite_path) as conn:
        facts = list(
            conn.execute(
                """
                SELECT id, page_hint, entity_id FROM facts
                WHERE id IN ('fact_grand_hall_first', 'fact_grand_hall_second')
                ORDER BY id
                """
            )
        )
    assert len(facts) == 2
    assert facts[0]["page_hint"] != facts[1]["page_hint"]
    assert facts[0]["entity_id"] != facts[1]["entity_id"]


def test_action_precheck_escalates_unresolved_occurrence_to_l3(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    unresolved = event_candidate(
        statement="Summit at Grand Hall is scheduled next month.",
        evidence_quote="Summit at Grand Hall is scheduled next month.",
        start_at=None,
        end_at=None,
    )
    action = propose_action(
        svc.paths,
        "fact_upsert",
        action_payload={"fact": unresolved},
        action_features={
            "candidate_signal": "source_extraction",
            "clean_fact_upsert": True,
            "fact_upsert_resolution": "new_clean_fact",
            "quote_backed": True,
            "fallback_route": False,
            "resolver_precheck": "passed",
            "reversible": True,
            "truth_mutation": False,
            "confidence": 0.95,
        },
        target_page_paths=["events/grand-hall.md"],
    )

    decided = decide_action(svc.paths, action["id"])

    assert decided["status"] == "needs_human"
    assert decided["autonomy_level"] == "L3"
    assert decided["action_features"]["clean_fact_upsert"] is False
    guarded_fact = get_action(svc.paths, action["id"])["evidence_json"]["payload"][
        "fact"
    ]
    assert guarded_fact["metadata"]["routing"]["route_review_reason"] == (
        "event_temporal_identity_unresolved"
    )
    with pytest.raises(ValueError, match="temporally compatible"):
        apply_action(svc.paths, action["id"])


def test_action_precheck_does_not_use_proposed_same_run_event_sibling(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    occurrence = event_candidate(
        statement="Summit at Grand Hall runs July 28-30, 2026.",
        evidence_quote="Summit at Grand Hall runs July 28-30, 2026.",
        start_at="2026-07-28",
        end_at="2026-07-30",
    )
    attached = attached_entity_candidate(
        entity_type="person",
        entity_surface="Jordan Vale",
        statement="Jordan Vale checked in at Grand Hall.",
    )
    run_id = "regen_route_guard"
    propose_action(
        svc.paths,
        "fact_upsert",
        run_id=run_id,
        action_payload={"fact": occurrence},
        action_features={"reversible": True},
        decide=False,
    )
    action = propose_action(
        svc.paths,
        "fact_upsert",
        run_id=run_id,
        action_payload={"fact": attached},
        action_features={
            "candidate_signal": "source_extraction",
            "clean_fact_upsert": True,
            "fact_upsert_resolution": "new_clean_fact",
            "quote_backed": True,
            "fallback_route": False,
            "resolver_precheck": "passed",
            "reversible": True,
            "truth_mutation": False,
            "confidence": 0.95,
        },
        target_page_paths=["events/grand-hall.md"],
        decide=False,
    )

    decided = decide_action(svc.paths, action["id"])
    guarded = decided["evidence_json"]["payload"]["fact"]

    assert decided["status"] == "needs_human"
    assert guarded["page_hint"] == "events/grand-hall.md"
    assert guarded["metadata"]["routing"]["route_destination_valid"] is False
    assert guarded["metadata"]["routing"]["route_review_reason"] == (
        "event_temporal_identity_unresolved"
    )


@pytest.mark.parametrize(
    ("anchor_status", "critic_decision"),
    [
        ("needs_human", "agree"),
        ("rejected", "disagree"),
        ("applied", "agree"),
    ],
)
def test_action_precheck_excludes_unfinalized_or_rejected_occurrence_sibling(
    tmp_path: Path, anchor_status: str, critic_decision: str
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    occurrence = event_candidate(
        statement="Summit at Grand Hall runs July 28-30, 2026.",
        evidence_quote="Summit at Grand Hall runs July 28-30, 2026.",
        start_at="2026-07-28",
        end_at="2026-07-31",
    )
    attached = attached_entity_candidate(
        entity_type="person",
        entity_surface="Jordan Vale",
        statement="Jordan Vale checked in at Grand Hall.",
    )
    run_id = f"regen_route_guard_{anchor_status}"
    anchor = propose_action(
        svc.paths,
        "fact_upsert",
        run_id=run_id,
        action_payload={"fact": occurrence},
        action_features={"reversible": True},
        decide=False,
    )
    with connection(svc.paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE cos_actions SET status = ?, critic_decision = ? WHERE id = ?",
            (anchor_status, critic_decision, anchor["id"]),
        )
    action = propose_action(
        svc.paths,
        "fact_upsert",
        run_id=run_id,
        action_payload={"fact": attached},
        action_features={"reversible": True},
        target_page_paths=["events/grand-hall.md"],
        decide=False,
    )

    decided = decide_action(svc.paths, action["id"])
    guarded = decided["evidence_json"]["payload"]["fact"]

    assert guarded["metadata"]["routing"]["route_destination_valid"] is False
    assert guarded["metadata"]["routing"]["route_review_reason"] == (
        "event_temporal_identity_unresolved"
    )


def test_action_precheck_uses_only_applied_critic_agreed_occurrence_sibling(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    occurrence = event_candidate(
        statement="Summit at Grand Hall runs July 28-30, 2026.",
        evidence_quote="Summit at Grand Hall runs July 28-30, 2026.",
        start_at="2026-07-28",
        end_at="2026-07-31",
    )
    attached = attached_entity_candidate(
        entity_type="person",
        entity_surface="Jordan Vale",
        statement="Jordan Vale checked in at Grand Hall.",
    )
    run_id = "regen_route_guard_accepted"
    anchor = propose_action(
        svc.paths,
        "fact_upsert",
        run_id=run_id,
        action_payload={"fact": occurrence},
        action_features={"reversible": True},
        decide=False,
    )
    with connection(svc.paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE cos_actions
            SET status = 'applied', critic_decision = 'agree',
                applied_at = '2026-07-18T00:00:00+00:00'
            WHERE id = ?
            """,
            (anchor["id"],),
        )
    action = propose_action(
        svc.paths,
        "fact_upsert",
        run_id=run_id,
        action_payload={"fact": attached},
        action_features={
            "candidate_signal": "source_extraction",
            "clean_fact_upsert": True,
            "fact_upsert_resolution": "new_clean_fact",
            "quote_backed": True,
            "fallback_route": False,
            "resolver_precheck": "passed",
            "reversible": True,
            "truth_mutation": False,
            "confidence": 0.95,
        },
        target_page_paths=["events/grand-hall.md"],
        decide=False,
    )

    decided = decide_action(svc.paths, action["id"])
    guarded = decided["evidence_json"]["payload"]["fact"]

    assert guarded["page_hint"] == (
        "events/summit-at-grand-hall-2026-07-28.md"
    )
    assert guarded["metadata"]["routing"]["route_destination_valid"] is True
    assert guarded["metadata"]["routing"]["route_resolution"] == (
        "event_source_occurrence_coherence"
    )


def test_action_precheck_escalates_medical_person_page_mismatch_to_l3(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    mismatched = attached_entity_candidate(
        entity_type="person",
        entity_surface="Jordan Vale",
        statement="Jordan Vale was prescribed a new medication.",
        page_hint="people/morgan-lane.md",
    )
    action = propose_action(
        svc.paths,
        "fact_upsert",
        action_payload={"fact": mismatched},
        action_features={
            "candidate_signal": "source_extraction",
            "clean_fact_upsert": True,
            "fact_upsert_resolution": "new_clean_fact",
            "quote_backed": True,
            "fallback_route": False,
            "resolver_precheck": "passed",
            "reversible": True,
            "truth_mutation": False,
            "confidence": 0.95,
        },
        target_page_paths=["people/morgan-lane.md"],
    )

    decided = decide_action(svc.paths, action["id"])

    assert decided["status"] == "needs_human"
    assert decided["autonomy_level"] == "L3"
    routing = decided["evidence_json"]["payload"]["fact"]["metadata"]["routing"]
    assert routing["route_review_reason"] == (
        "person_page_identity_sensitive_mismatch"
    )
    with pytest.raises(ValueError, match="target page identity"):
        apply_action(svc.paths, action["id"])
