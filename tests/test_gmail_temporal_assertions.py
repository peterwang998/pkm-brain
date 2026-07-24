from __future__ import annotations

import pytest

from pkm_brain.gmail_temporal_assertions import (
    assess_gmail_temporal_source_assertions,
)


CURRENT_AT = "2027-08-10T10:00:00-07:00"


def _assertion(text: str):
    assessment = assess_gmail_temporal_source_assertions(
        text=text,
        message_internal_at=CURRENT_AT,
        fact_admitted=True,
        chunk_id="public-source-assertion",
    )
    target = tuple(
        item
        for item in assessment.lifecycle_assertions
        if item.lifecycle_role in {"cancelled", "completed", "rescheduled"}
    )
    assert len(target) == 1
    return assessment, target[0]


def test_direct_lifecycle_assertion_is_retained_without_granting_authority() -> None:
    text = "Subject: Update\n\nThe Apollo interview was cancelled."

    assessment, assertion = _assertion(text)

    assert assertion.disposition == "asserted"
    assert assertion.blockers == ()
    assert assertion.primary_blocker is None
    assert assessment.source_sha256 == assessment.analysis.source_sha256
    assert assessment.analysis_fingerprint == assessment.analysis.snapshot_fingerprint
    assert assessment.production_integration_enabled is False
    assert assessment.creates_facts is False
    assert assessment.creates_identities is False
    assert assessment.creates_times is False
    assert text not in repr(assessment)


@pytest.mark.parametrize(
    ("text", "primary"),
    [
        (
            "Subject: Update\n\nThe Apollo interview was probably cancelled.",
            "epistemic_assertion",
        ),
        (
            "Subject: Update\n\nThe Apollo interview was allegedly cancelled.",
            "epistemic_assertion",
        ),
        (
            "Subject: Update\n\nIt appears that the Apollo interview was cancelled.",
            "epistemic_assertion",
        ),
        (
            "Subject: Update\n\nIt seems the Apollo interview was cancelled.",
            "epistemic_assertion",
        ),
        (
            "Subject: Update\n\nThe Apollo interview was likely cancelled.",
            "epistemic_assertion",
        ),
        (
            "Subject: Update\n\nIt is probable that the interview was cancelled.",
            "epistemic_assertion",
        ),
        (
            "Subject: Update\n\nAlex said the Apollo interview was cancelled.",
            "reported_assertion",
        ),
        (
            "Subject: Update\n\nMaya Chen reported the interview was cancelled.",
            "reported_assertion",
        ),
        (
            "Subject: Update\n\nPat tells me the interview was cancelled.",
            "reported_assertion",
        ),
        (
            "Subject: Update\n\nThe Apollo interview was cancelled, Pat confirmed.",
            "reported_assertion",
        ),
        (
            "Subject: Update\n\nThe Apollo interview was cancelled, or so I was told.",
            "reported_assertion",
        ),
        (
            "Subject: Update\n\nAccording to Alex, the interview was cancelled.",
            "reported_assertion",
        ),
        (
            "Subject: Update\n\nRumor has it the interview was cancelled.",
            "reported_assertion",
        ),
        (
            "Subject: Update\n\nIt is false that the interview was cancelled.",
            "refuted_assertion",
        ),
        (
            "Subject: Update\n\nThe claim that the interview was cancelled is false.",
            "refuted_assertion",
        ),
        (
            "Subject: Update\n\nIf the interview was cancelled, call me.",
            "conditional_assertion",
        ),
        (
            "Subject: Update\n\nI don't believe the interview was cancelled.",
            "denied_assertion",
        ),
        (
            "Subject: Update\n\nWas the interview cancelled?",
            "interrogative_assertion",
        ),
        (
            "Subject: Update\n\nThe interview was cancelled?",
            "interrogative_assertion",
        ),
        (
            "Subject: Update\n\nWas the Apollo interview cancelled",
            "interrogative_assertion",
        ),
        (
            "Subject: Update\n\nPlease confirm the Apollo interview was cancelled.",
            "interrogative_assertion",
        ),
        (
            "Subject: Update\n\nI wonder whether the Apollo interview was cancelled.",
            "interrogative_assertion",
        ),
        (
            "Subject: Update\n\n> The Apollo interview was cancelled.",
            "quoted_or_forwarded_context",
        ),
        (
            "Subject: Update\n\n--- Forwarded message ---\nThe interview was cancelled.",
            "quoted_or_forwarded_context",
        ),
        (
            'Subject: Update\n\n"The Apollo interview was cancelled."',
            "quoted_or_forwarded_context",
        ),
        (
            "Subject: Update\n\n“The Apollo interview was cancelled.”",
            "quoted_or_forwarded_context",
        ),
        (
            "Subject: Update\n\n'The Apollo interview was cancelled.'",
            "quoted_or_forwarded_context",
        ),
        (
            "Subject: Update\n\nThe Apollo interview might be cancelled.",
            "modal_assertion",
        ),
        (
            "Subject: Update\n\nThe Apollo interview will be cancelled.",
            "modal_assertion",
        ),
        (
            "Subject: Update\n\nThe Apollo interview is expected to be cancelled.",
            "modal_assertion",
        ),
        (
            "Subject: Update\n\nI doubt the Apollo interview was cancelled.",
            "epistemic_assertion",
        ),
        (
            "Subject: Update\n\nAs far as I know, the Apollo interview was cancelled.",
            "epistemic_assertion",
        ),
        (
            "Subject: Update\n\nThe interview was cancelled, but that is false.",
            "refuted_assertion",
        ),
        (
            "Subject: Update\n\nThe Apollo interview was cancelled, Pat said.",
            "reported_assertion",
        ),
        (
            "Subject: Update\n\nThe Apollo interview was cancelled, according to Pat.",
            "reported_assertion",
        ),
        (
            "Subject: Update\n\nThe Apollo interview was cancelled, apparently.",
            "epistemic_assertion",
        ),
    ],
)
def test_non_asserted_lifecycle_language_is_visible_but_blocked(
    text: str,
    primary: str,
) -> None:
    _, assertion = _assertion(text)

    assert assertion.disposition == "blocked"
    assert assertion.primary_blocker == primary
    assert primary in assertion.blockers


def test_assessment_is_deterministic_and_content_bound() -> None:
    first, _ = _assertion("Subject: Update\n\nThe interview was cancelled.")
    replay, _ = _assertion("Subject: Update\n\nThe interview was cancelled.")
    changed, _ = _assertion("Subject: Update\n\nThe interview was completed.")

    assert replay == first
    assert changed.assessment_fingerprint != first.assessment_fingerprint
    assert changed.source_sha256 != first.source_sha256


def test_unrelated_later_refutation_does_not_block_direct_lifecycle_assertion() -> None:
    _, assertion = _assertion(
        "Subject: Update\n\nThe Apollo interview was cancelled. "
        "The hotel report was false."
    )

    assert assertion.disposition == "asserted"
    assert assertion.blockers == ()


def test_unrelated_this_is_false_sentence_does_not_refute_prior_assertion() -> None:
    _, assertion = _assertion(
        "Subject: Update\n\nThe Apollo interview was cancelled. "
        "This is false advertising by the hotel."
    )

    assert assertion.disposition == "asserted"
    assert assertion.blockers == ()


def test_assertion_followed_by_a_separate_question_retains_recall() -> None:
    _, assertion = _assertion(
        "Subject: Update\n\nThe Apollo interview was cancelled, so what now?"
    )

    assert assertion.disposition == "asserted"
    assert assertion.blockers == ()


@pytest.mark.parametrize(
    "text",
    [
        "Subject: Update\n\nWe should note the Apollo interview was cancelled.",
        "Subject: Update\n\nIf you need context, the Apollo interview was cancelled.",
        "Subject: Update\n\nI may join, and the Apollo interview was cancelled.",
        (
            "Subject: Update\n\nAlex said the hotel closed, and the Apollo interview "
            "was cancelled."
        ),
    ],
)
def test_unrelated_modifiers_do_not_block_later_direct_assertion(text: str) -> None:
    _, assertion = _assertion(text)

    assert assertion.disposition == "asserted"
    assert assertion.blockers == ()


def test_attribution_verb_is_not_misread_as_a_second_asserted_lifecycle() -> None:
    assessment, cancellation = _assertion(
        "Subject: Update\n\nThe Apollo interview was cancelled, Pat confirmed."
    )

    assert cancellation.primary_blocker == "reported_assertion"
    scheduled = tuple(
        item
        for item in assessment.lifecycle_assertions
        if item.lifecycle_role == "scheduled"
    )
    assert scheduled
    assert all(item.disposition == "blocked" for item in scheduled)


@pytest.mark.parametrize(
    ("text", "expected_disposition", "expected_primary"),
    [
        (
            "Subject: Access\n\nRespond by October 9, 2027.",
            "asserted",
            None,
        ),
        (
            "Subject: Access\n\nShould I respond by October 9, 2027?",
            "blocked",
            "interrogative_assertion",
        ),
        (
            "Subject: Access\n\nIf access expires October 10, 2027, call me.",
            "blocked",
            "conditional_assertion",
        ),
        (
            "Subject: Access\n\nAccess expires October 10, 2027 if the plan lapses.",
            "blocked",
            "conditional_assertion",
        ),
        (
            "Subject: Update\n\nThe Apollo interview is on October 11, 2027, "
            "according to Pat.",
            "blocked",
            "reported_assertion",
        ),
        (
            'Subject: Access\n\n"Respond by October 9, 2027."',
            "blocked",
            "quoted_or_forwarded_context",
        ),
        (
            "Subject: Update\n\nThe Apollo interview may be on October 11, 2027.",
            "blocked",
            "modal_assertion",
        ),
        (
            "Subject: Update\n\nThe Apollo interview will be on October 11, 2027.",
            "asserted",
            None,
        ),
        (
            "Subject: Update\n\nThe Apollo interview, according to Pat, is on "
            "October 11, 2027.",
            "blocked",
            "reported_assertion",
        ),
        (
            "Subject: Update\n\nThe Apollo interview, if approved, is on "
            "October 11, 2027.",
            "blocked",
            "conditional_assertion",
        ),
        (
            "Subject: Update\n\nOn October 11, 2027, the Apollo interview might occur.",
            "blocked",
            "modal_assertion",
        ),
    ],
)
def test_canonical_temporal_leads_share_the_source_assertion_profile(
    text: str,
    expected_disposition: str,
    expected_primary: str | None,
) -> None:
    assessment = assess_gmail_temporal_source_assertions(
        text=text,
        message_internal_at=CURRENT_AT,
        fact_admitted=True,
        chunk_id="public-lead-source-assertion",
    )

    assert assessment.lead_assertions
    assert {item.disposition for item in assessment.lead_assertions} == {
        expected_disposition
    }
    assert {item.primary_blocker for item in assessment.lead_assertions} == {
        expected_primary
    }
    assert all(
        item.evidence_start < item.evidence_end for item in assessment.lead_assertions
    )
