from __future__ import annotations

from pkm_brain.gmail_temporal_verifier import (
    GMAIL_TEMPORAL_CANDIDATE_VERIFIER_CONTRACT,
    GMAIL_TEMPORAL_VERIFIER_MODEL,
    GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
    gmail_temporal_verifier_policy_fingerprint,
)


def test_temporal_verifier_policy_is_pinned_and_content_free() -> None:
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


def test_temporal_verifier_policy_fingerprint_is_stable() -> None:
    fingerprint = gmail_temporal_verifier_policy_fingerprint()

    assert fingerprint.startswith("gtvp_")
    assert len(fingerprint) == len("gtvp_") + 64
    assert fingerprint == gmail_temporal_verifier_policy_fingerprint()
