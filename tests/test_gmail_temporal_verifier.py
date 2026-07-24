from __future__ import annotations

from pkm_brain.gmail_temporal_verifier import (
    GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT,
    GMAIL_TEMPORAL_VERIFIER_MODEL,
    GMAIL_TEMPORAL_VERIFIER_POLICY_VERSION,
    GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
    gmail_temporal_verifier_policy_fingerprint,
)


def test_temporal_verifier_policy_is_pinned_and_content_free() -> None:
    assert GMAIL_TEMPORAL_VERIFIER_POLICY_VERSION.endswith("_v6")
    assert GMAIL_TEMPORAL_VERIFIER_MODEL == "gpt-5.6-luna"
    assert GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT == "medium"
    assert "requires_defer" in GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT
    assert "do not by themselves make" in GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT
    assert "Routine authentication-code expirations" in (
        GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT
    )
    assert "represents unresolved clusters without exact citations" in (
        GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT
    )
    assert "field_near_review_only and long_association_gap" in (
        GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT
    )
    assert "mutually exclusive possibilities" in (
        GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT
    )
    assert "effective date for a consequential policy" in (
        GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT
    )
    assert "Repeated direct schedules are also separate assertions" in (
        GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT
    )
    assert "Deadline words are relation cues" in (
        GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT
    )
    assert "cancelled_scheduled_slot_derived_as_planned_occurrence" in (
        GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT
    )
    assert "null normalized value" in GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT
    assert "never one candidate per page" in (
        GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT
    )
    assert "Do not erase an explicit lifecycle" in (
        GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT
    )
    assert "lifecycle-free base may be uncertain but must not be supported" in (
        GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT
    )
    assert "independently asserted actual-occurrence semantics" in (
        GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT
    )
    assert "occurred, happened, or took place" in (
        GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT
    )
    assert (
        "explicit lifecycle must not suppress the directly asserted actual occurrence"
        in (GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT)
    )
    assert "must not be independently emitted" in (
        GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT
    )
    assert "aggregation may group the unresolved aliases" in (
        GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT
    )


def test_temporal_verifier_policy_fingerprint_is_stable() -> None:
    fingerprint = gmail_temporal_verifier_policy_fingerprint()

    assert fingerprint.startswith("gtvp_")
    assert len(fingerprint) == len("gtvp_") + 64
    assert fingerprint == gmail_temporal_verifier_policy_fingerprint()
