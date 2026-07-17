from __future__ import annotations

import pytest

from pkm_brain.temporal import (
    TemporalRetrievalRequest,
    event_time_grounding_errors,
    fact_matches_knowledge_time,
    fact_matches_current_time,
    fact_matches_event_time,
    fact_matches_temporal_request,
    fact_matches_valid_time,
    is_safe_temporal_successor,
    normalize_event_time_candidate,
    normalize_temporal_candidate,
    temporal_merge_compatible,
    timeline_currentness,
    timeline_sort_key,
)


def test_normalize_event_time_mirrors_valid_actual_time_to_flat_fields() -> None:
    candidate, errors = normalize_event_time_candidate(
        {
            "statement": "The CloudZero interview happened.",
            "event_time": {
                "kind": "ACTUAL",
                "start_at": "2026-05-19T17:00:00+00:00",
                "end_at": "2026-05-19T18:00:00+00:00",
                "precision": "exact",
                "expression": "2026-05-19T17:00:00+00:00",
            },
        },
        primary_entity_is_event=True,
    )

    assert errors == []
    assert candidate["event_time"] == {
        "kind": "actual",
        "start_at": "2026-05-19T17:00:00+00:00",
        "end_at": "2026-05-19T18:00:00+00:00",
        "precision": "exact",
        "expression": "2026-05-19T17:00:00+00:00",
    }
    assert candidate["event_time_kind"] == "actual"
    assert candidate["event_start_at"] == "2026-05-19T17:00:00+00:00"
    assert candidate["event_end_at"] == "2026-05-19T18:00:00+00:00"
    assert candidate["event_time_precision"] == "exact"


def test_normalize_event_time_accepts_planned_deadline_with_only_end() -> None:
    candidate, errors = normalize_event_time_candidate(
        {
            "statement": "The proposal is due in May 2026.",
            "event_time": {
                "kind": "planned",
                "end_at": "2026-06",
                "precision": "month",
                "expression": "in May 2026",
            },
        },
        primary_entity_is_event=True,
    )

    assert errors == []
    assert candidate["event_start_at"] is None
    assert candidate["event_end_at"] == "2026-06"
    assert candidate["event_time_precision"] == "month"


@pytest.mark.parametrize(
    ("event_time", "is_event", "expected_error"),
    [
        (
            {"kind": "planned", "start_at": "2026-05-01", "precision": "day"},
            False,
            "event_time requires the primary entity to be an event",
        ),
        (
            {"kind": "planned", "precision": "day"},
            True,
            "event_time requires start_at or end_at",
        ),
        (
            {"kind": "actual", "end_at": "2026-05-01", "precision": "day"},
            True,
            "actual event_time requires start_at",
        ),
        (
            {
                "kind": "actual",
                "start_at": "2026-05-02",
                "end_at": "2026-05-01",
                "precision": "day",
            },
            True,
            "event_time.end_at must be later than start_at",
        ),
        (
            {"kind": "actual", "start_at": "2026-05-01", "precision": "rough"},
            True,
            "event_time.precision must be one of day|exact|month|year",
        ),
        (
            {"kind": "planned", "start_at": "2026-05", "precision": "day"},
            True,
            "event_time.precision does not match the supplied start/end bounds",
        ),
    ],
)
def test_invalid_event_time_is_reported_and_stripped_without_losing_base_fact(
    event_time: dict[str, str], is_event: bool, expected_error: str
) -> None:
    candidate, errors = normalize_event_time_candidate(
        {"statement": "The base fact survives.", "event_time": event_time},
        primary_entity_is_event=is_event,
    )

    assert expected_error in errors
    assert candidate["statement"] == "The base fact survives."
    assert candidate["event_time"] is None
    assert candidate["event_time_kind"] is None
    assert candidate["event_start_at"] is None
    assert candidate["event_end_at"] is None


def test_event_time_expression_must_be_literal_cited_evidence() -> None:
    candidate, errors = normalize_event_time_candidate(
        {
            "event_time": {
                "kind": "planned",
                "start_at": "2026-05",
                "precision": "month",
                "expression": "sometime in May",
            }
        },
        primary_entity_is_event=True,
    )

    assert errors == []
    assert (
        event_time_grounding_errors(candidate, "It should launch sometime in May.")
        == []
    )
    assert event_time_grounding_errors(candidate, "It should launch next month.") == [
        "event_time.expression is not present in cited evidence"
    ]


def test_normalize_temporal_candidate_preserves_explicit_interval() -> None:
    candidate, errors = normalize_temporal_candidate(
        {
            "statement": "Atlas was in beta.",
            "temporal_kind": "time_bound",
            "valid_from": "2026-03-01",
            "valid_to": "2026-04-01",
            "valid_time_precision": "exact",
            "temporal_confidence": 0.9,
            "temporal_expression": "during March 2026",
            "metadata": {"source": "test"},
        }
    )

    assert errors == []
    assert candidate["valid_from"] == "2026-03-01"
    assert candidate["effective_at"] == "2026-03-01"
    assert candidate["valid_to"] == "2026-04-01"
    assert candidate["temporal_kind"] == "time_bound"
    assert candidate["valid_time_precision"] == "day"
    assert candidate["temporal_confidence"] == 0.9
    assert candidate["temporal_expression"] == "during March 2026"


def test_normalize_temporal_candidate_rejects_inverted_interval() -> None:
    _candidate, errors = normalize_temporal_candidate(
        {
            "temporal_kind": "time_bound",
            "valid_from": "2026-04-01",
            "valid_to": "2026-03-01",
        }
    )

    assert "valid_to must be later than valid_from" in errors


def test_instantaneous_fact_rejects_end_boundary() -> None:
    _candidate, errors = normalize_temporal_candidate(
        {
            "temporal_kind": "instantaneous",
            "valid_from": "2026-04-01T12:00:00+00:00",
            "valid_to": "2026-04-01T12:01:00+00:00",
        }
    )

    assert "instantaneous facts cannot have valid_to" in errors


def test_fact_matches_valid_time_for_interval_instant_and_atemporal() -> None:
    interval = {
        "status": "superseded",
        "temporal_kind": "time_bound",
        "valid_from": "2026-03-01",
        "valid_to": "2026-04-01",
    }
    instant = {
        "status": "active",
        "temporal_kind": "instantaneous",
        "valid_from": "2026-03-15",
        "valid_time_precision": "day",
    }
    atemporal = {"status": "active", "temporal_kind": "atemporal"}

    assert fact_matches_valid_time(interval, "2026-03-15") is True
    assert fact_matches_valid_time(interval, "2026-04-01") is False
    assert fact_matches_valid_time(instant, "2026-03-15") is True
    assert fact_matches_valid_time(instant, "2026-03-16") is False
    assert fact_matches_valid_time(atemporal, "2026-03-15") is True


def test_superseded_fact_without_valid_end_is_not_historically_inferred() -> None:
    fact = {
        "status": "superseded",
        "temporal_kind": "ongoing",
        "valid_from": "2026-03-01",
        "valid_to": None,
    }

    assert fact_matches_valid_time(fact, "2026-03-15") is False


def test_unreviewed_instantaneous_fact_is_not_historically_visible() -> None:
    fact = {
        "status": "needs_confirmation",
        "temporal_kind": "instantaneous",
        "valid_from": "2026-03-15",
        "valid_time_precision": "day",
    }

    assert fact_matches_valid_time(fact, "2026-03-15") is False


def test_knowledge_interval_and_temporal_merge_identity_are_explicit() -> None:
    fact = {
        "created_at": "2026-03-01T00:00:00+00:00",
        "knowledge_to": "2026-04-01T00:00:00+00:00",
    }
    march = {
        "temporal_kind": "time_bound",
        "valid_from": "2026-03-01",
        "valid_to": "2026-04-01",
        "valid_time_precision": "day",
    }
    april = {
        **march,
        "valid_from": "2026-04-01",
        "valid_to": "2026-05-01",
    }

    assert fact_matches_knowledge_time(fact, "2026-03-15") is True
    assert fact_matches_knowledge_time(fact, "2026-04-01") is False
    assert temporal_merge_compatible(march, dict(march)) is True
    assert temporal_merge_compatible(march, april) is False


def test_temporal_merge_identity_includes_event_time() -> None:
    planned = {
        "temporal_kind": "unknown",
        "event_time": {
            "kind": "planned",
            "start_at": "2026-05-01",
            "end_at": None,
            "precision": "day",
            "expression": "May 1",
        },
    }
    rescheduled = {
        **planned,
        "event_time": {
            **planned["event_time"],
            "start_at": "2026-05-08",
            "expression": "May 8",
        },
    }
    actual = {
        **planned,
        "event_time": {**planned["event_time"], "kind": "actual"},
    }

    assert temporal_merge_compatible(planned, dict(planned)) is True
    assert temporal_merge_compatible(planned, rescheduled) is False
    assert temporal_merge_compatible(planned, actual) is False


def test_malformed_temporal_bounds_fail_closed() -> None:
    malformed_valid = {
        "status": "active",
        "temporal_kind": "time_bound",
        "valid_from": "2026-03-01",
        "valid_to": "not-a-date",
    }
    malformed_knowledge = {
        "status": "active",
        "created_at": "2026-03-01T00:00:00+00:00",
        "knowledge_to": "not-a-date",
    }

    assert fact_matches_valid_time(malformed_valid, "2026-03-15") is False
    assert fact_matches_knowledge_time(malformed_knowledge, "2026-03-15") is False


def test_temporal_request_resolves_modes_and_clocks_explicitly() -> None:
    current = TemporalRetrievalRequest.resolve("What is true now?")
    valid = TemporalRetrievalRequest.resolve(
        "ignored task date", valid_as_of="2026-03-15"
    )
    known = TemporalRetrievalRequest.resolve("ignored task date", known_as_of="2026-03")
    bitemporal = TemporalRetrievalRequest.resolve(
        "ignored task date",
        valid_as_of="2026-03-15",
        known_as_of="2026-04-01",
    )
    timeline = TemporalRetrievalRequest.resolve(
        "Show the timeline", temporal_mode="timeline"
    )

    assert current.mode == "current"
    assert valid.mode == "valid"
    assert valid.valid_as_of == "2026-03-15T23:59:59.999999+00:00"
    assert known.mode == "known"
    assert known.known_as_of == "2026-03-31T23:59:59.999999+00:00"
    assert bitemporal.mode == "bitemporal"
    assert timeline.mode == "timeline"


def test_explicit_temporal_mode_rejects_missing_or_conflicting_clocks() -> None:
    with pytest.raises(ValueError, match="requires known_as_of"):
        TemporalRetrievalRequest.resolve("No date here", temporal_mode="known")
    with pytest.raises(ValueError, match="does not accept known_as_of"):
        TemporalRetrievalRequest.resolve(
            "No date here",
            known_as_of="2026-03-15",
            temporal_mode="valid",
        )
    with pytest.raises(ValueError, match="requires valid_as_of and known_as_of"):
        TemporalRetrievalRequest.resolve("As of 2026-03-15", temporal_mode="bitemporal")
    with pytest.raises(ValueError, match="event_kind requires event_as_of"):
        TemporalRetrievalRequest.resolve("Show actual events", event_kind="actual")
    with pytest.raises(ValueError, match=r"event_kind must be one of actual\|planned"):
        TemporalRetrievalRequest.resolve(
            "Show tentative events",
            event_as_of="2026-03-15",
            event_kind="tentative",
        )


def test_natural_language_infers_knowledge_time_without_conflating_event_date() -> None:
    known = TemporalRetrievalRequest.resolve(
        "What did we know about Atlas as of 2026-03-15?"
    )
    valid = TemporalRetrievalRequest.resolve(
        "What do we know about the Atlas meeting on 2026-03-15?"
    )
    assert known.mode == "known"
    assert known.known_inferred is True
    assert known.valid_as_of is None
    assert valid.mode == "valid"
    assert valid.valid_inferred is True
    assert valid.known_as_of is None


def test_explicit_clock_does_not_infer_a_second_clock_from_prose() -> None:
    request = TemporalRetrievalRequest.resolve(
        "What did we know as of 2026-03-15?",
        valid_as_of="2026-02-01",
    )

    assert request.mode == "valid"
    assert request.valid_inferred is False
    assert request.known_as_of is None


def test_combined_clock_predicate_prevents_valid_or_knowledge_time_leakage() -> None:
    fact = {
        "status": "superseded",
        "temporal_kind": "ongoing",
        "valid_from": "2026-03-01",
        "valid_to": None,
        "created_at": "2026-03-10T00:00:00+00:00",
        "knowledge_to": "2026-04-01T00:00:00+00:00",
    }
    matching = TemporalRetrievalRequest.resolve(
        "ignored",
        valid_as_of="2026-03-15",
        known_as_of="2026-03-15",
    )
    before_knowledge = TemporalRetrievalRequest.resolve(
        "ignored",
        valid_as_of="2026-03-15",
        known_as_of="2026-03-01",
    )
    after_validity = TemporalRetrievalRequest.resolve(
        "ignored",
        valid_as_of="2026-02-15",
        known_as_of="2026-03-15",
    )

    assert fact_matches_temporal_request(fact, matching) is True
    assert fact_matches_temporal_request(fact, before_knowledge) is False
    assert fact_matches_temporal_request(fact, after_validity) is False


def test_current_time_matching_keeps_all_active_unknown_time_facts_visible() -> None:
    current_at = "2026-03-15T12:00:00+00:00"
    legacy = {"status": "active", "temporal_kind": "unknown"}
    explicit_unknown = {
        "status": "active",
        "temporal_kind": "unknown",
        "temporal_confidence": 0.0,
    }
    ongoing = {"status": "active", "temporal_kind": "ongoing"}
    future = {
        "status": "active",
        "temporal_kind": "time_bound",
        "valid_from": "2026-04-01",
        "valid_to": "2026-05-01",
    }
    past_instant = {
        "status": "active",
        "temporal_kind": "instantaneous",
        "valid_from": "2026-03-14",
        "valid_time_precision": "day",
    }

    assert timeline_currentness(legacy, current_at=current_at) == "legacy_unknown"
    assert fact_matches_current_time(legacy, current_at=current_at) is True
    assert (
        timeline_currentness(explicit_unknown, current_at=current_at)
        == "unknown_valid_time"
    )
    assert fact_matches_current_time(explicit_unknown, current_at=current_at) is True
    assert timeline_currentness(ongoing, current_at=current_at) == (
        "ongoing_unknown_start"
    )
    assert fact_matches_current_time(ongoing, current_at=current_at) is True
    assert timeline_currentness(future, current_at=current_at) == "future"
    assert fact_matches_current_time(future, current_at=current_at) is False
    assert timeline_currentness(past_instant, current_at=current_at) == "historical"


def test_event_time_is_opt_in_and_does_not_hide_past_actual_or_future_plan() -> None:
    current_at = "2026-03-15T12:00:00+00:00"
    past_actual = {
        "status": "active",
        "temporal_kind": "unknown",
        "event_time": {
            "kind": "actual",
            "start_at": "2026-03-01T17:30:00+00:00",
            "end_at": None,
            "precision": "exact",
            "expression": "March 1",
        },
    }
    future_plan = {
        "status": "conflicted",
        "temporal_kind": "unknown",
        "event_time_kind": "planned",
        "event_start_at": "2026-05-01",
        "event_end_at": "2026-06-01",
        "event_time_precision": "month",
    }

    assert fact_matches_current_time(past_actual, current_at=current_at) is True
    assert fact_matches_current_time(future_plan, current_at=current_at) is True
    assert fact_matches_event_time(past_actual, "2026-03-01") is True
    assert fact_matches_event_time(past_actual, "2026-03-02") is False
    assert fact_matches_event_time(past_actual, "2026-03-01", kind="planned") is False
    assert fact_matches_event_time(future_plan, "2026-05-15") is True
    assert fact_matches_event_time(future_plan, "2026-05-15", kind="planned") is True
    assert fact_matches_event_time(future_plan, "2026-05-15", kind="actual") is False
    assert fact_matches_event_time(future_plan, "2026-06-01") is False
    event_request = TemporalRetrievalRequest.resolve(
        "What happened on May 15?", valid_as_of="2026-05-15"
    )
    assert fact_matches_temporal_request(future_plan, event_request) is False


def test_event_and_proposition_clocks_are_orthogonal_and_combined_with_and() -> None:
    fact = {
        "status": "active",
        "temporal_kind": "time_bound",
        "valid_from": "2026-03-01",
        "valid_to": "2026-04-01",
        "created_at": "2026-02-01T00:00:00+00:00",
        "event_time": {
            "kind": "actual",
            "start_at": "2026-04-15T17:30:00+00:00",
            "end_at": None,
            "precision": "exact",
            "expression": "April 15",
        },
    }
    valid_march_event_april = TemporalRetrievalRequest.resolve(
        "Find the March-valid fact for the April event",
        valid_as_of="2026-03-15",
        event_as_of="2026-04-15",
        event_kind="actual",
    )
    wrong_valid_clock = TemporalRetrievalRequest.resolve(
        "Find the April-valid fact for the April event",
        valid_as_of="2026-04-15",
        event_as_of="2026-04-15",
        event_kind="actual",
    )
    wrong_event_clock = TemporalRetrievalRequest.resolve(
        "Find the March-valid fact for a March event",
        valid_as_of="2026-03-15",
        event_as_of="2026-03-15",
        event_kind="actual",
    )

    assert fact_matches_temporal_request(fact, valid_march_event_april) is True
    assert fact_matches_temporal_request(fact, wrong_valid_clock) is False
    assert fact_matches_temporal_request(fact, wrong_event_clock) is False
    assert valid_march_event_april.debug()["event_as_of"] == "2026-04-15"
    assert valid_march_event_april.envelope()["fact_coverage"] == (
        "explicit_valid_time_only_with_event_time"
    )


def test_event_time_cannot_substitute_for_valid_clock_in_bitemporal_request() -> None:
    event_only_fact = {
        "status": "active",
        "temporal_kind": "unknown",
        "created_at": "2026-03-01T00:00:00+00:00",
        "event_time": {
            "kind": "actual",
            "start_at": "2026-04-15",
            "end_at": None,
            "precision": "day",
            "expression": "April 15",
        },
    }
    request = TemporalRetrievalRequest.resolve(
        "What did we know about the April 15 event?",
        valid_as_of="2026-04-15",
        known_as_of="2026-04-15",
        event_as_of="2026-04-15",
        event_kind="actual",
    )

    assert fact_matches_knowledge_time(event_only_fact, request.known_as_of) is True
    assert fact_matches_event_time(event_only_fact, request.event_as_of) is True
    assert fact_matches_temporal_request(event_only_fact, request) is False


def test_timeline_sort_key_orders_valid_time_and_puts_undated_rows_last() -> None:
    rows = [
        {"id": "unknown", "temporal_kind": "unknown"},
        {"id": "later", "valid_from": "2026-04-01"},
        {
            "id": "event",
            "event_time_kind": "actual",
            "event_start_at": "2026-03-15",
            "event_time_precision": "day",
        },
        {"id": "earlier", "valid_from": "2026-03-01"},
    ]

    assert [row["id"] for row in sorted(rows, key=timeline_sort_key)] == [
        "earlier",
        "event",
        "later",
        "unknown",
    ]


def test_safe_temporal_successor_requires_strong_predecessor_time_and_truth() -> None:
    predecessor = {
        "id": "old",
        "source_ids": ["document:old"],
        "truth_confidence": 0.9,
        "temporal_confidence": 0.9,
        "temporal_kind": "time_bound",
        "valid_from": "2026-01-01",
        "valid_to": "2026-02-01",
        "valid_time_precision": "day",
    }
    successor = {
        "id": "new",
        "source_ids": ["document:new"],
        "truth_confidence": 0.9,
        "temporal_confidence": 0.9,
        "temporal_kind": "ongoing",
        "valid_from": "2026-02-01",
        "valid_time_precision": "day",
    }

    assert is_safe_temporal_successor(successor, [predecessor, successor]) is True
    assert (
        is_safe_temporal_successor(
            successor,
            [{**predecessor, "truth_confidence": 0.7}, successor],
        )
        is False
    )
    assert (
        is_safe_temporal_successor(
            successor,
            [{**predecessor, "valid_time_precision": "month"}, successor],
        )
        is False
    )
