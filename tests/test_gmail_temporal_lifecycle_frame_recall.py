from __future__ import annotations

import pytest

from pkm_brain.gmail_temporal_batching import (
    plan_gmail_temporal_selector_batches,
)
from pkm_brain.gmail_temporal_frontier import (
    build_gmail_temporal_candidate_frontier,
)
from pkm_brain.gmail_temporal_leads import analyze_gmail_temporal_leads


ANCHOR = "2027-05-01T10:00:00-07:00"


def _analysis_and_candidates(text: str):
    analysis = analyze_gmail_temporal_leads(
        text=text,
        message_internal_at=ANCHOR,
        fact_admitted=True,
        chunk_id="synthetic-lifecycle-frame-recall",
    )
    batches = plan_gmail_temporal_selector_batches(
        text=text,
        analysis=analysis,
    ).batches
    candidates = tuple(
        candidate
        for batch in batches
        for candidate in build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        ).candidates
    )
    return analysis, candidates


def _event_candidates(analysis, candidates):
    mentions = {item.mention_id: item for item in analysis.mentions}
    return tuple(
        candidate
        for candidate in candidates
        if mentions[candidate.subject_mention_id].mention_type == "event"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        (
            "The Cedar design review has been rescheduled to June 16, 2027 "
            "from June 14, 2027.",
            {
                "2027-06-16": "rescheduled_replacement",
                "2027-06-14": "rescheduled_old",
            },
        ),
        (
            "The Cedar design review was rescheduled from June 14, 2027 "
            "to June 16, 2027.",
            {
                "2027-06-14": "rescheduled_old",
                "2027-06-16": "rescheduled_replacement",
            },
        ),
        (
            "The Cedar design review was rescheduled from June 14, 2027, "
            "to June 16, 2027.",
            {
                "2027-06-14": "rescheduled_old",
                "2027-06-16": "rescheduled_replacement",
            },
        ),
        (
            "The Cedar design review moved from June 14, 2027 to June 16, 2027.",
            {
                "2027-06-14": "rescheduled_old",
                "2027-06-16": "rescheduled_replacement",
            },
        ),
        (
            "The Juniper seminar is now rescheduled to June 16, 2027 "
            "from June 14, 2027.",
            {
                "2027-06-16": "rescheduled_replacement",
                "2027-06-14": "rescheduled_old",
            },
        ),
    ),
)
def test_exact_reschedule_frames_expose_both_endpoint_roles_before_verification(
    text: str,
    expected: dict[str, str],
) -> None:
    analysis, all_candidates = _analysis_and_candidates(text)
    candidates = _event_candidates(analysis, all_candidates)

    for normalized_value, lifecycle in expected.items():
        matches = tuple(
            candidate
            for candidate in candidates
            if candidate.normalized_value == normalized_value
            and candidate.lifecycle == lifecycle
        )
        assert len(matches) == 1
        candidate = matches[0]
        assert (candidate.relation, candidate.kind) == ("occurrence", "planned")
        assert "rescheduled_endpoint_role_unresolved" not in candidate.blockers
        assert candidate.repair_flags == (
            "reschedule_endpoint_role_derived_from_exact_frame",
        )
        assert candidate.requires_defer is False
        assert candidate.routable is False


@pytest.mark.parametrize(
    "text",
    (
        "The Cedar design review was rescheduled to June 16, 2027.",
        "The Cedar design review was rescheduled from June 14, 2027 "
        "to June 16, 2027 or June 17, 2027.",
    ),
)
def test_incomplete_or_ambiguous_reschedules_remain_unknown(text: str) -> None:
    analysis, all_candidates = _analysis_and_candidates(text)
    candidates = tuple(
        candidate
        for candidate in _event_candidates(analysis, all_candidates)
        if candidate.lifecycle_mention_id is not None
    )

    assert candidates
    assert {candidate.lifecycle for candidate in candidates} == {"unknown"}
    assert all(
        "rescheduled_endpoint_role_unresolved" in candidate.blockers
        or "lifecycle_subject_binding_unverified" in candidate.blockers
        for candidate in candidates
    )
    assert all(candidate.requires_defer for candidate in candidates)
    assert all(candidate.routable is False for candidate in candidates)


@pytest.mark.parametrize(
    "text",
    (
        "The Cedar design review is reportedly rescheduled to June 16, 2027 "
        "from June 14, 2027.",
        "The Cedar design review is now possibly rescheduled to June 16, 2027 "
        "from June 14, 2027.",
    ),
)
def test_inverse_reschedule_subject_link_does_not_accept_unbounded_qualifiers(
    text: str,
) -> None:
    analysis, all_candidates = _analysis_and_candidates(text)
    candidates = _event_candidates(analysis, all_candidates)

    assert candidates
    assert not any(
        candidate.lifecycle in {"rescheduled_old", "rescheduled_replacement"}
        for candidate in candidates
    )


def test_exact_reschedule_ignores_a_later_unrelated_deadline() -> None:
    text = (
        "The Cedar design review was rescheduled from June 14, 2027, "
        "to June 16, 2027, and please confirm by June 20, 2027."
    )
    analysis, all_candidates = _analysis_and_candidates(text)
    candidates = _event_candidates(analysis, all_candidates)

    exact = {
        (candidate.normalized_value, candidate.lifecycle): candidate
        for candidate in candidates
        if candidate.lifecycle in {"rescheduled_old", "rescheduled_replacement"}
    }
    assert set(exact) == {
        ("2027-06-14", "rescheduled_old"),
        ("2027-06-16", "rescheduled_replacement"),
    }
    assert all(candidate.requires_defer is False for candidate in exact.values())


@pytest.mark.parametrize(
    "text",
    (
        "The Cedar design review was rescheduled from June 14, 2027 "
        "to June 16, 2027 or June 17, 2027.",
        "The Cedar design review was rescheduled from June 14, 2027 "
        "to June 16, 2027 or the 18th.",
        "The Cedar design review was rescheduled from June 14, 2027 "
        "to June 16, 2027, maybe June 17, 2027.",
        "The Cedar design review was rescheduled from June 14, 2027 "
        "to June 16, 2027, perhaps the 18th.",
        "The Cedar design review was rescheduled from June 14, 2027 "
        "to June 16, 2027 (or June 17, 2027).",
        "The Cedar design review was rescheduled from June 14, 2027 "
        "to June 16, 2027, alternatively June 17, 2027.",
    ),
)
def test_ambiguous_replacement_does_not_receive_exact_endpoint_roles(
    text: str,
) -> None:
    analysis, all_candidates = _analysis_and_candidates(text)
    candidates = _event_candidates(analysis, all_candidates)

    assert not any(
        candidate.lifecycle in {"rescheduled_old", "rescheduled_replacement"}
        for candidate in candidates
    )
    assert any(candidate.lifecycle == "unknown" for candidate in candidates)


@pytest.mark.parametrize(
    ("subject_link", "terminal_link"),
    (
        ("", "has been"),
        ("", "was"),
        ("is ", "but has been"),
        ("", ", has been"),
        ("", ", but has been"),
    ),
)
def test_cancelled_scheduled_slot_is_representable_as_a_planned_occurrence(
    subject_link: str,
    terminal_link: str,
) -> None:
    text = (
        f"The Cedar design review {subject_link}scheduled for June 14, 2027 "
        f"{terminal_link} cancelled."
    )
    analysis, all_candidates = _analysis_and_candidates(text)
    matches = tuple(
        candidate
        for candidate in _event_candidates(analysis, all_candidates)
        if candidate.normalized_value == "2027-06-14"
        and candidate.lifecycle == "cancelled"
    )

    assert len(matches) == 1
    candidate = matches[0]
    assert (candidate.relation, candidate.kind) == ("occurrence", "planned")
    assert "competing_lifecycle_expression_cue" not in candidate.blockers
    assert candidate.repair_flags == (
        "cancelled_scheduled_slot_derived_as_planned_occurrence",
    )
    assert candidate.requires_defer is False
    assert candidate.routable is False
    assert not any(
        item.normalized_value == "2027-06-14" and item.lifecycle == "scheduled"
        for item in all_candidates
    )


@pytest.mark.parametrize(
    ("clock", "expected_value", "expected_blocker"),
    (
        ("2:00 PM", None, "missing_timezone"),
        (
            "2:00 PM PST",
            "2027-06-14T14:00:00-08:00",
            "timezone_abbreviation_requires_review",
        ),
    ),
)
def test_cancelled_slot_lifecycle_survives_deferred_time_normalization(
    clock: str,
    expected_value: str | None,
    expected_blocker: str,
) -> None:
    text = (
        f"The Cedar design review scheduled for June 14, 2027 at {clock} "
        "has been cancelled."
    )
    analysis, all_candidates = _analysis_and_candidates(text)
    matches = tuple(
        candidate
        for candidate in _event_candidates(analysis, all_candidates)
        if candidate.lifecycle == "cancelled"
    )

    assert len(matches) == 1
    candidate = matches[0]
    assert (candidate.relation, candidate.kind) == ("occurrence", "planned")
    assert candidate.normalized_value == expected_value
    assert expected_blocker in candidate.blockers
    assert candidate.repair_flags == (
        "cancelled_scheduled_slot_derived_as_planned_occurrence",
    )
    assert candidate.requires_defer is True


@pytest.mark.parametrize(
    "text",
    (
        "The Cedar design review scheduled for June 14, 2027 has been cancelled?",
        "Perhaps the Cedar design review scheduled for June 14, 2027 has been "
        "cancelled.",
        "It seems the Cedar design review scheduled for June 14, 2027 has been "
        "cancelled.",
        "I think the Cedar design review scheduled for June 14, 2027 has been "
        "cancelled.",
        "It is possible that the Cedar design review scheduled for June 14, 2027 "
        "has been cancelled.",
        "Please confirm the Cedar design review scheduled for June 14, 2027 has "
        "been cancelled.",
        "Do you know whether the Cedar design review scheduled for June 14, 2027 "
        "has been cancelled.",
    ),
)
def test_nonassertive_scheduled_slot_cancellation_remains_deferred(
    text: str,
) -> None:
    analysis, all_candidates = _analysis_and_candidates(text)
    event_candidates = _event_candidates(analysis, all_candidates)
    cancellations = tuple(
        candidate
        for candidate in event_candidates
        if candidate.lifecycle == "cancelled"
    )

    assert cancellations
    assert all(
        (candidate.relation, candidate.kind) == ("occurrence", "planned")
        for candidate in cancellations
    )
    assert all(candidate.requires_defer for candidate in cancellations)
    assert all(
        candidate.repair_flags
        == ("cancelled_scheduled_slot_assertion_strength_unverified",)
        for candidate in cancellations
    )
    assert any(candidate.lifecycle == "scheduled" for candidate in event_candidates)
    assert not any(
        candidate.lifecycle == "cancelled" and not candidate.requires_defer
        for candidate in event_candidates
    )


@pytest.mark.parametrize(
    "text",
    (
        "It is false that the Cedar design review scheduled for June 14, 2027 "
        "has been cancelled.",
        "According to Alex, the Cedar design review scheduled for June 14, 2027 "
        "has been cancelled.",
        "Alex said the Cedar design review scheduled for June 14, 2027 has been "
        "cancelled.",
        "We heard the Cedar design review scheduled for June 14, 2027 has been "
        "cancelled.",
        "Rumor has it the Cedar design review scheduled for June 14, 2027 has "
        "been cancelled.",
    ),
)
def test_denied_or_attributed_scheduled_slot_cancellation_remains_deferred(
    text: str,
) -> None:
    analysis, all_candidates = _analysis_and_candidates(text)
    event_candidates = _event_candidates(analysis, all_candidates)
    cancellations = tuple(
        candidate
        for candidate in event_candidates
        if candidate.lifecycle == "cancelled"
    )

    assert cancellations
    assert all(
        (candidate.relation, candidate.kind) == ("occurrence", "planned")
        for candidate in cancellations
    )
    assert all(candidate.requires_defer for candidate in cancellations)
    assert all(
        candidate.repair_flags
        == ("cancelled_scheduled_slot_assertion_strength_unverified",)
        for candidate in cancellations
    )
    assert any(candidate.lifecycle == "scheduled" for candidate in event_candidates)
    assert not any(
        candidate.lifecycle == "cancelled" and not candidate.requires_defer
        for candidate in event_candidates
    )


@pytest.mark.parametrize(
    "text",
    (
        "If approved, the Cedar design review scheduled for June 14, 2027 has "
        "been cancelled.",
        "Unless this is a mistake, the Cedar design review scheduled for June "
        "14, 2027 has been cancelled.",
        "Assuming the report is correct, the Cedar design review scheduled for "
        "June 14, 2027 has been cancelled.",
        "Although unconfirmed, the Cedar design review scheduled for June 14, "
        "2027 has been cancelled.",
    ),
)
def test_sentence_initial_qualified_cancellation_remains_deferred(
    text: str,
) -> None:
    analysis, all_candidates = _analysis_and_candidates(text)
    event_candidates = _event_candidates(analysis, all_candidates)
    cancellations = tuple(
        candidate
        for candidate in event_candidates
        if candidate.lifecycle == "cancelled"
    )

    assert cancellations
    assert all(candidate.requires_defer for candidate in cancellations)
    assert all(
        candidate.repair_flags
        == ("cancelled_scheduled_slot_assertion_strength_unverified",)
        for candidate in cancellations
    )
    assert any(candidate.lifecycle == "scheduled" for candidate in event_candidates)
    assert not any(
        candidate.lifecycle == "cancelled" and not candidate.requires_defer
        for candidate in event_candidates
    )


def test_affirmative_transition_before_cancelled_slot_keeps_exact_certificate() -> None:
    text = (
        "For clarity, the Cedar design review scheduled for June 14, 2027 has "
        "been cancelled."
    )
    analysis, all_candidates = _analysis_and_candidates(text)
    cancellations = tuple(
        candidate
        for candidate in _event_candidates(analysis, all_candidates)
        if candidate.lifecycle == "cancelled"
    )

    assert len(cancellations) == 1
    assert cancellations[0].requires_defer is False
    assert cancellations[0].repair_flags == (
        "cancelled_scheduled_slot_derived_as_planned_occurrence",
    )


@pytest.mark.parametrize(
    "text",
    (
        "The Cedar design review scheduled for June 14, 2027 has been cancelled!",
        "For clarity, the Cedar design review scheduled for June 14, 2027 has "
        "been cancelled.",
        "The Perhaps Foundation design review scheduled for June 14, 2027 has "
        "been cancelled.",
    ),
)
def test_affirmative_scheduled_slot_cancellation_keeps_exact_certificate(
    text: str,
) -> None:
    analysis, all_candidates = _analysis_and_candidates(text)
    cancellations = tuple(
        candidate
        for candidate in _event_candidates(analysis, all_candidates)
        if candidate.lifecycle == "cancelled"
    )

    assert len(cancellations) == 1
    candidate = cancellations[0]
    assert (candidate.relation, candidate.kind) == ("occurrence", "planned")
    assert candidate.repair_flags == (
        "cancelled_scheduled_slot_derived_as_planned_occurrence",
    )
    assert candidate.requires_defer is False


@pytest.mark.parametrize(
    "text",
    (
        "The Cedar design review scheduled for June 14, 2027 was cancelled "
        "or postponed.",
        "The Cedar design review scheduled for June 14, 2027 was cancelled, "
        "unless the client objects.",
        "The Cedar design review scheduled for June 14, 2027 was cancelled, "
        "subject to client approval.",
        "The Cedar design review scheduled for June 14, 2027 was cancelled "
        "provided the client approves.",
        "The Cedar design review scheduled for June 14, 2027 was cancelled "
        "and postponed.",
        "The Cedar design review scheduled for June 14, 2027 was cancelled"
        + " " * 97
        + "or postponed.",
    ),
)
def test_qualified_scheduled_slot_cancellation_requires_review(text: str) -> None:
    analysis, all_candidates = _analysis_and_candidates(text)
    matches = tuple(
        candidate
        for candidate in _event_candidates(analysis, all_candidates)
        if candidate.normalized_value == "2027-06-14"
        and candidate.lifecycle == "cancelled"
    )

    assert matches
    assert all(
        candidate.repair_flags
        == ("cancelled_scheduled_slot_has_ambiguous_trailing_qualification",)
        for candidate in matches
    )
    assert all(candidate.requires_defer for candidate in matches)
    assert not any(
        candidate.lifecycle == "cancelled" and not candidate.requires_defer
        for candidate in all_candidates
    )


@pytest.mark.parametrize(
    "text",
    (
        "The Cedar design review scheduled for June 14, 2027 was not cancelled.",
        "The Cedar design review scheduled for June 14, 2027 but the Birch "
        "interview was cancelled.",
    ),
)
def test_negated_or_distinct_subject_cancellation_does_not_cancel_slot(
    text: str,
) -> None:
    analysis, all_candidates = _analysis_and_candidates(text)
    subject_surfaces = {
        mention.mention_id: text[mention.start : mention.end]
        for mention in analysis.mentions
    }
    cedar = tuple(
        candidate
        for candidate in _event_candidates(analysis, all_candidates)
        if candidate.normalized_value == "2027-06-14"
        and candidate.lifecycle == "cancelled"
        and subject_surfaces[candidate.subject_mention_id]
        in {"review", "Cedar design review"}
    )

    assert cedar == ()
    assert any(
        candidate.normalized_value == "2027-06-14"
        and candidate.lifecycle == "scheduled"
        for candidate in all_candidates
    )


def test_cancelled_on_date_remains_a_terminal_unspecified_timestamp() -> None:
    text = "The Cedar design review was cancelled on June 14, 2027."
    analysis, all_candidates = _analysis_and_candidates(text)
    matches = tuple(
        candidate
        for candidate in _event_candidates(analysis, all_candidates)
        if candidate.normalized_value == "2027-06-14"
        and candidate.lifecycle == "cancelled"
    )

    assert len(matches) == 1
    candidate = matches[0]
    assert (candidate.relation, candidate.kind) == ("unspecified", "unspecified")
    assert candidate.repair_flags == ("terminal_semantics_derived_as_unspecified",)
    assert candidate.routable is False


def test_noncomplementary_lifecycle_dates_do_not_receive_the_frame_exemption() -> None:
    text = (
        "The Cedar design review was cancelled on June 14, 2027 after being "
        "scheduled for June 10, 2027."
    )
    analysis, all_candidates = _analysis_and_candidates(text)
    candidates = _event_candidates(analysis, all_candidates)

    cancellation = next(
        candidate
        for candidate in candidates
        if candidate.normalized_value == "2027-06-14"
        and candidate.lifecycle == "cancelled"
    )
    assert (cancellation.relation, cancellation.kind) == (
        "unspecified",
        "unspecified",
    )
    assert not any(
        candidate.normalized_value == "2027-06-10"
        and candidate.lifecycle == "cancelled"
        for candidate in candidates
    )
