from __future__ import annotations

import unicodedata

import pytest

from pkm_brain.gmail_temporal_thread_retrieval_experiment import (
    GmailTemporalThreadEvidence,
    GmailTemporalThreadRetrievalExperimentError,
    GmailTemporalVerifiedEventBinding,
    plan_gmail_temporal_thread_retrieval_experiment,
    temporal_thread_query_intent,
)


CUTOFF = "2027-10-31T00:00:00Z"


def _source(
    evidence_id: str,
    *,
    account: str = "account-a",
    thread: str = "atlas-thread",
    ordinal: int,
    text: str,
    available_at: str | None = None,
    verified_key: str | None = "event:atlas",
    verified_aliases: tuple[str, ...] = ("Atlas launch",),
    contextual_key: str | None = None,
) -> GmailTemporalThreadEvidence:
    return GmailTemporalThreadEvidence(
        evidence_id=evidence_id,
        gmail_account_scope_id=account,
        gmail_provider_thread_id=thread,
        available_at=available_at or f"2027-10-{ordinal + 1:02d}T12:00:00Z",
        message_ordinal=ordinal,
        text=text,
        verified_event_bindings=(
            (
                GmailTemporalVerifiedEventBinding(
                    event_identity_key=verified_key,
                    aliases=verified_aliases,
                ),
            )
            if verified_key
            else ()
        ),
        contextual_event_identity_keys=((contextual_key,) if contextual_key else ()),
    )


def _distractors(count: int = 9) -> tuple[GmailTemporalThreadEvidence, ...]:
    return tuple(
        _source(
            f"noise-{index}",
            thread=f"account-a:noise-{index}",
            ordinal=1,
            text=f"Subject: Routine note {index}\n\nGeneral information.",
            verified_key=None,
        )
        for index in range(1, count + 1)
    )


def _baseline(anchor: str = "atlas-m1") -> tuple[str, ...]:
    return (anchor,) + tuple(f"noise-{index}" for index in range(1, 10))


def test_lifecycle_sidecar_keeps_full_direct_ranking_and_selects_final_update() -> None:
    sources = (
        _source(
            "atlas-m1",
            ordinal=1,
            text="Subject: Atlas launch\n\nAtlas launch was booked for October 2, 2027.",
        ),
        _source(
            "atlas-m2",
            ordinal=2,
            text="Subject: Atlas launch\n\nAtlas launch was rescheduled to October 4, 2027.",
        ),
        _source(
            "atlas-m3",
            ordinal=3,
            text="Subject: Atlas launch\n\nAtlas launch was rescheduled to October 6, 2027.",
        ),
        _source(
            "atlas-m4",
            ordinal=4,
            text="Subject: Atlas launch\n\nAtlas launch was confirmed for October 8, 2027.",
        ),
        _source(
            "atlas-m5",
            ordinal=5,
            text="Subject: Atlas launch\n\nAtlas launch was cancelled.",
        ),
    ) + _distractors()
    baseline = _baseline()

    plan = plan_gmail_temporal_thread_retrieval_experiment(
        query="What is the current status of the Atlas launch?",
        temporal_intent="lifecycle",
        source_available_as_of=CUTOFF,
        baseline_ranked_evidence_ids=baseline,
        evidence_sources=sources,
    )

    assert plan.direct_ranked_evidence_ids == baseline
    assert plan.context_evidence_ids == ("atlas-m5",)
    assert plan.answer_evidence_ids == baseline + ("atlas-m5",)
    assert plan.target_event_identity_key == "event:atlas"
    assert plan.production_integration_enabled is False
    assert plan.persisted is False


def test_same_thread_temporal_noise_with_another_event_key_is_forbidden() -> None:
    sources = (
        _source(
            "atlas-m1",
            ordinal=1,
            text="Subject: Atlas\n\nAtlas launch was booked for October 2, 2027.",
        ),
        _source(
            "atlas-m2",
            ordinal=2,
            text="Subject: Atlas\n\nAtlas launch was cancelled.",
        ),
        _source(
            "team-lunch",
            ordinal=3,
            text="Subject: Atlas\n\nTeam lunch was scheduled for October 9, 2027.",
            verified_key="event:team-lunch",
            verified_aliases=("Team lunch",),
        ),
    ) + _distractors()

    plan = plan_gmail_temporal_thread_retrieval_experiment(
        query="What is the current status of the Atlas launch?",
        temporal_intent="lifecycle",
        source_available_as_of=CUTOFF,
        baseline_ranked_evidence_ids=_baseline(),
        evidence_sources=sources,
    )

    assert plan.context_evidence_ids == ("atlas-m2",)
    assert "team-lunch" not in plan.answer_evidence_ids


def test_same_provider_thread_id_in_another_account_never_joins() -> None:
    sources = (
        _source(
            "atlas-account-a",
            account="account-a",
            thread="shared-provider-thread",
            ordinal=1,
            text="Subject: Atlas\n\nAtlas launch was booked for October 2, 2027.",
        ),
        _source(
            "atlas-account-b",
            account="account-b",
            thread="shared-provider-thread",
            ordinal=2,
            text="Subject: Atlas\n\nAtlas launch was cancelled.",
        ),
    ) + _distractors()

    plan = plan_gmail_temporal_thread_retrieval_experiment(
        query="What is the current status of the Atlas launch?",
        temporal_intent="lifecycle",
        source_available_as_of=CUTOFF,
        baseline_ranked_evidence_ids=_baseline("atlas-account-a"),
        evidence_sources=sources,
    )

    assert plan.target_event_identity_key == "event:atlas"
    assert plan.context_evidence_ids == ()
    assert "atlas-account-b" not in plan.answer_evidence_ids


def test_inverse_sibling_alias_does_not_authorize_same_project_event() -> None:
    sources = (
        _source(
            "apollo-demo-anchor",
            ordinal=1,
            text="Subject: Apollo project\n\nApollo demo was booked for October 2, 2027.",
            verified_key="event:apollo-demo",
            verified_aliases=("Apollo demo",),
        ),
        _source(
            "apollo-filing-update",
            ordinal=2,
            text="Subject: Apollo project\n\nApollo filing was cancelled.",
            verified_key="event:apollo-filing",
            verified_aliases=("Apollo filing",),
        ),
    ) + _distractors()

    plan = plan_gmail_temporal_thread_retrieval_experiment(
        query="What is the latest status of the Apollo filing?",
        temporal_intent="lifecycle",
        source_available_as_of=CUTOFF,
        baseline_ranked_evidence_ids=_baseline("apollo-demo-anchor"),
        evidence_sources=sources,
    )

    assert plan.target_event_identity_key is None
    assert plan.context_evidence_ids == ()
    assert "apollo-filing-update" not in plan.answer_evidence_ids


def test_mixed_source_prose_cannot_override_its_authorized_event_binding() -> None:
    sources = (
        _source(
            "orion-anchor",
            ordinal=1,
            text=(
                "Subject: Orion review and Atlas launch\n\n"
                "Atlas launch dependencies are covered. "
                "Orion review was booked for October 2, 2027."
            ),
            verified_key="event:orion-review",
            verified_aliases=("Orion review",),
        ),
        _source(
            "atlas-hidden",
            ordinal=2,
            text="Subject: Atlas launch\n\nAtlas launch was cancelled.",
        ),
    ) + _distractors()

    plan = plan_gmail_temporal_thread_retrieval_experiment(
        query="What is the current status of the Atlas launch?",
        temporal_intent="lifecycle",
        source_available_as_of=CUTOFF,
        baseline_ranked_evidence_ids=_baseline("orion-anchor"),
        evidence_sources=sources,
    )

    assert plan.target_event_identity_key is None
    assert plan.context_evidence_ids == ()
    assert "atlas-hidden" not in plan.answer_evidence_ids


def test_pronoun_requires_upstream_contextual_identity_and_stays_review_only() -> None:
    anchor = _source(
        "atlas-m1",
        ordinal=1,
        text="Subject: Atlas\n\nAtlas launch was booked for October 2, 2027.",
    )
    unbound = _source(
        "atlas-pronoun-unbound",
        ordinal=2,
        text="Subject: Update\n\nIt was cancelled.",
        verified_key=None,
    )
    bound = _source(
        "atlas-pronoun-bound",
        ordinal=3,
        text="Subject: Update\n\nIt was cancelled.",
        verified_key=None,
        contextual_key="event:atlas",
    )
    sources = (anchor, unbound, bound) + _distractors()

    plan = plan_gmail_temporal_thread_retrieval_experiment(
        query="What is the current status of the Atlas launch?",
        temporal_intent="lifecycle",
        source_available_as_of=CUTOFF,
        baseline_ranked_evidence_ids=_baseline(),
        evidence_sources=sources,
    )

    assert plan.verified_context_evidence_ids == ()
    assert plan.review_context_evidence_ids == ("atlas-pronoun-bound",)
    assert "atlas-pronoun-unbound" not in plan.answer_evidence_ids


def test_newer_review_only_lifecycle_update_follows_verified_update() -> None:
    sources = (
        _source(
            "atlas-m1",
            ordinal=1,
            text="Subject: Atlas\n\nAtlas launch was booked for October 2, 2027.",
        ),
        _source(
            "atlas-m2",
            ordinal=2,
            text="Subject: Atlas\n\nAtlas launch was rescheduled to October 4, 2027.",
        ),
        _source(
            "atlas-review",
            ordinal=3,
            text="Subject: Atlas\n\nIt was cancelled.",
            verified_key=None,
            contextual_key="event:atlas",
        ),
    ) + _distractors()

    plan = plan_gmail_temporal_thread_retrieval_experiment(
        query="What is the current status of the Atlas launch?",
        temporal_intent="lifecycle",
        source_available_as_of=CUTOFF,
        baseline_ranked_evidence_ids=_baseline(),
        evidence_sources=sources,
    )

    assert plan.verified_context_evidence_ids == ("atlas-m2",)
    assert plan.review_context_evidence_ids == ("atlas-review",)
    assert plan.context_evidence_ids == ("atlas-m2", "atlas-review")


def test_zero_verified_budget_preserves_separate_review_only_channel() -> None:
    sources = (
        _source(
            "atlas-m1",
            ordinal=1,
            text="Subject: Atlas\n\nAtlas launch was booked for October 2, 2027.",
        ),
        _source(
            "atlas-m2",
            ordinal=2,
            text="Subject: Atlas\n\nAtlas launch was rescheduled to October 4, 2027.",
        ),
        _source(
            "atlas-review",
            ordinal=3,
            text="Subject: Atlas\n\nIt was cancelled.",
            verified_key=None,
            contextual_key="event:atlas",
        ),
    ) + _distractors()
    baseline = _baseline()

    plan = plan_gmail_temporal_thread_retrieval_experiment(
        query="What is the current status of the Atlas launch?",
        temporal_intent="lifecycle",
        source_available_as_of=CUTOFF,
        baseline_ranked_evidence_ids=baseline,
        evidence_sources=sources,
        verified_context_limit=0,
    )

    assert plan.target_event_identity_key == "event:atlas"
    assert plan.direct_ranked_evidence_ids == baseline
    assert plan.verified_context_evidence_ids == ()
    assert plan.review_context_evidence_ids == ("atlas-review",)
    assert plan.context_evidence_ids == ("atlas-review",)
    assert plan.answer_evidence_ids == baseline + ("atlas-review",)


def test_zero_verified_budget_does_not_weaken_review_context_safeguards() -> None:
    sources = (
        _source(
            "atlas-m1",
            ordinal=2,
            text="Subject: Atlas\n\nAtlas launch was booked for October 2, 2027.",
        ),
        _source(
            "stale-review",
            ordinal=1,
            text="Subject: Atlas\n\nIt was cancelled.",
            verified_key=None,
            contextual_key="event:atlas",
        ),
        _source(
            "wrong-event-review",
            ordinal=3,
            text="Subject: Atlas\n\nIt was cancelled.",
            verified_key=None,
            contextual_key="event:other",
        ),
        _source(
            "question-review",
            ordinal=4,
            text="Subject: Atlas\n\nWas it cancelled?",
            verified_key=None,
            contextual_key="event:atlas",
        ),
        _source(
            "verified-update",
            ordinal=5,
            text="Subject: Atlas\n\nAtlas launch was cancelled.",
        ),
    ) + _distractors()

    plan = plan_gmail_temporal_thread_retrieval_experiment(
        query="What is the current status of the Atlas launch?",
        temporal_intent="lifecycle",
        source_available_as_of=CUTOFF,
        baseline_ranked_evidence_ids=_baseline(),
        evidence_sources=sources,
        verified_context_limit=0,
    )

    assert plan.target_event_identity_key == "event:atlas"
    assert plan.verified_context_evidence_ids == ()
    assert plan.review_context_evidence_ids == ()
    assert plan.context_evidence_ids == ()


@pytest.mark.parametrize(
    "text",
    [
        "Subject: Atlas\n\nWas Atlas launch cancelled?",
        "Subject: Atlas\n\nAtlas launch wasn't cancelled.",
        "Subject: Atlas\n\nIt is false that Atlas launch was cancelled.",
        "Subject: Atlas\n\nAtlas launch was cancelled, but that is wrong.",
        "Subject: Atlas\n\nPat said Atlas launch was cancelled.",
        "Subject: Atlas\n\nAtlas launch was probably cancelled.",
        "Subject: Atlas\n\nThe note said “Atlas launch was cancelled.”",
        "Subject: Atlas\n\n-----Original Message-----\nAtlas launch was cancelled.",
        "Subject: Atlas\n\nBegin forwarded message:\nAtlas launch was cancelled.",
        "Subject: Atlas\n\nIf Atlas launch is cancelled, call me.",
    ],
)
def test_non_asserted_lifecycle_sources_never_enter_context(text: str) -> None:
    sources = (
        _source(
            "atlas-m1",
            ordinal=1,
            text="Subject: Atlas\n\nAtlas launch was booked for October 2, 2027.",
        ),
        _source("blocked", ordinal=2, text=text),
    ) + _distractors()

    plan = plan_gmail_temporal_thread_retrieval_experiment(
        query="What is the current status of the Atlas launch?",
        temporal_intent="lifecycle",
        source_available_as_of=CUTOFF,
        baseline_ranked_evidence_ids=_baseline(),
        evidence_sources=sources,
    )

    assert plan.context_evidence_ids == ()


def test_unrelated_prior_negation_does_not_hide_later_assertion() -> None:
    sources = (
        _source(
            "atlas-m1",
            ordinal=1,
            text="Subject: Atlas\n\nAtlas launch was booked for October 2, 2027.",
        ),
        _source(
            "atlas-m2",
            ordinal=2,
            text=(
                "Subject: Atlas\n\nParking was not included. "
                "Atlas launch was rescheduled to October 8, 2027."
            ),
        ),
    ) + _distractors()

    plan = plan_gmail_temporal_thread_retrieval_experiment(
        query="What happened with the Atlas launch?",
        temporal_intent="timeline",
        source_available_as_of=CUTOFF,
        baseline_ranked_evidence_ids=_baseline(),
        evidence_sources=sources,
    )

    assert plan.context_evidence_ids == ("atlas-m2",)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("What’s the latest on Q3 BR?", "lifecycle"),
        ("What happened with Q3 BR?", "timeline"),
        ("Where did Q3 BR land?", "timeline"),
        ("What’s the latest on 東京会議?", "lifecycle"),
        ("What date did we settle on for Lumen launch?", "timeline"),
        ("By when do I need to answer Juniper?", "timeline"),
        ("When does Maple access run out?", "timeline"),
        ("How do I change my password?", None),
        ("What is the history of Rome?", None),
    ],
)
def test_intent_paraphrase_acronym_and_unicode_matrix(
    query: str,
    expected: str | None,
) -> None:
    assert temporal_thread_query_intent(query) == expected


def test_unicode_topic_matching_is_normalization_stable() -> None:
    composed = "Café review"
    decomposed = unicodedata.normalize("NFD", composed)
    key = "event:cafe-review"
    sources = (
        _source(
            "cafe-m1",
            ordinal=1,
            text=f"Subject: {composed}\n\n{composed} was booked for October 2, 2027.",
            verified_key=key,
            verified_aliases=(composed,),
        ),
        _source(
            "cafe-m2",
            ordinal=2,
            text=f"Subject: {composed}\n\n{composed} was cancelled.",
            verified_key=key,
            verified_aliases=(composed,),
        ),
    ) + _distractors()
    baseline = _baseline("cafe-m1")

    plan = plan_gmail_temporal_thread_retrieval_experiment(
        query=f"What is the current status of the {decomposed}?",
        temporal_intent="lifecycle",
        source_available_as_of=CUTOFF,
        baseline_ranked_evidence_ids=baseline,
        evidence_sources=sources,
    )

    assert plan.context_evidence_ids == ("cafe-m2",)


def test_timeline_uses_chronological_change_point_coverage_within_cap() -> None:
    sources = (
        _source(
            "atlas-m1",
            ordinal=1,
            text="Subject: Atlas\n\nAtlas launch was booked for October 2, 2027.",
        ),
        *(
            _source(
                f"atlas-m{index}",
                ordinal=index,
                text=(
                    f"Subject: Atlas\n\nAtlas launch was rescheduled to "
                    f"October {index + 2}, 2027."
                ),
            )
            for index in range(2, 6)
        ),
    ) + _distractors()

    plan = plan_gmail_temporal_thread_retrieval_experiment(
        query="Walk me through the Atlas launch schedule.",
        temporal_intent="timeline",
        source_available_as_of=CUTOFF,
        baseline_ranked_evidence_ids=_baseline(),
        evidence_sources=sources,
    )

    assert plan.context_evidence_ids == ("atlas-m2", "atlas-m4", "atlas-m5")
    assert (
        "timeline_context_is_capped_change_point_coverage_not_exhaustive_history"
        in (plan.limitations)
    )


def test_timeline_cap_preserves_an_interior_terminal_change() -> None:
    sources = (
        _source(
            "atlas-m1",
            ordinal=1,
            text="Subject: Atlas\n\nAtlas launch was booked for October 2, 2027.",
        ),
        _source(
            "atlas-m2",
            ordinal=2,
            text="Subject: Atlas\n\nAtlas launch was rescheduled to October 4, 2027.",
        ),
        _source(
            "atlas-m3",
            ordinal=3,
            text="Subject: Atlas\n\nAtlas launch was confirmed for October 4, 2027.",
        ),
        _source(
            "atlas-m4",
            ordinal=4,
            text="Subject: Atlas\n\nAtlas launch was rescheduled to October 6, 2027.",
        ),
        _source(
            "atlas-m5",
            ordinal=5,
            text="Subject: Atlas\n\nAtlas launch was cancelled.",
        ),
        _source(
            "atlas-m6",
            ordinal=6,
            text="Subject: Atlas\n\nAtlas launch was rescheduled to October 8, 2027.",
        ),
        _source(
            "atlas-m7",
            ordinal=7,
            text="Subject: Atlas\n\nAtlas launch was confirmed for October 8, 2027.",
        ),
    ) + _distractors()

    plan = plan_gmail_temporal_thread_retrieval_experiment(
        query="Walk me through the Atlas launch schedule.",
        temporal_intent="timeline",
        source_available_as_of=CUTOFF,
        baseline_ranked_evidence_ids=_baseline(),
        evidence_sources=sources,
    )

    assert plan.context_evidence_ids == ("atlas-m2", "atlas-m5", "atlas-m7")


def test_timeline_keeps_three_verified_changes_plus_one_review_terminal() -> None:
    sources = (
        _source(
            "atlas-m1",
            ordinal=1,
            text="Subject: Atlas\n\nAtlas launch was booked for October 2, 2027.",
        ),
        _source(
            "atlas-m2",
            ordinal=2,
            text="Subject: Atlas\n\nAtlas launch was rescheduled to October 4, 2027.",
        ),
        _source(
            "atlas-m3",
            ordinal=3,
            text="Subject: Atlas\n\nAtlas launch was confirmed for October 4, 2027.",
        ),
        _source(
            "atlas-m4",
            ordinal=4,
            text="Subject: Atlas\n\nAtlas launch was rescheduled to October 6, 2027.",
        ),
        _source(
            "atlas-review",
            ordinal=5,
            text="Subject: Atlas\n\nIt was cancelled.",
            verified_key=None,
            contextual_key="event:atlas",
        ),
    ) + _distractors()

    plan = plan_gmail_temporal_thread_retrieval_experiment(
        query="Walk me through the Atlas launch schedule.",
        temporal_intent="timeline",
        source_available_as_of=CUTOFF,
        baseline_ranked_evidence_ids=_baseline(),
        evidence_sources=sources,
    )

    assert plan.verified_context_evidence_ids == (
        "atlas-m2",
        "atlas-m3",
        "atlas-m4",
    )
    assert plan.review_context_evidence_ids == ("atlas-review",)
    assert plan.context_evidence_ids == (
        "atlas-m2",
        "atlas-m3",
        "atlas-m4",
        "atlas-review",
    )


def test_lifecycle_does_not_append_stale_context_when_final_state_is_direct() -> None:
    sources = (
        _source(
            "atlas-m1",
            ordinal=1,
            text="Subject: Atlas\n\nAtlas launch was booked for October 2, 2027.",
        ),
        _source(
            "atlas-m2",
            ordinal=2,
            text="Subject: Atlas\n\nAtlas launch was rescheduled to October 4, 2027.",
        ),
        _source(
            "atlas-m3",
            ordinal=3,
            text="Subject: Atlas\n\nAtlas launch was cancelled.",
        ),
    ) + _distractors(count=8)
    baseline = ("atlas-m3", "atlas-m1") + tuple(
        f"noise-{index}" for index in range(1, 9)
    )

    plan = plan_gmail_temporal_thread_retrieval_experiment(
        query="What is the current status of the Atlas launch?",
        temporal_intent="lifecycle",
        source_available_as_of=CUTOFF,
        baseline_ranked_evidence_ids=baseline,
        evidence_sources=sources,
    )

    assert plan.direct_ranked_evidence_ids == baseline
    assert plan.context_evidence_ids == ()


def test_future_excluded_and_ambiguous_anchor_authority_fail_closed() -> None:
    anchor = _source(
        "atlas-m1",
        ordinal=1,
        text="Subject: Atlas\n\nAtlas launch was booked for October 2, 2027.",
    )
    excluded = _source(
        "atlas-excluded",
        ordinal=2,
        text="Subject: Atlas\n\nAtlas launch was rescheduled to October 4, 2027.",
    )
    future = _source(
        "atlas-future",
        ordinal=3,
        text="Subject: Atlas\n\nAtlas launch was cancelled.",
        available_at="2027-11-01T00:00:00Z",
    )
    sources = (anchor, excluded, future) + _distractors()
    plan = plan_gmail_temporal_thread_retrieval_experiment(
        query="What is the current status of the Atlas launch?",
        temporal_intent="lifecycle",
        source_available_as_of=CUTOFF,
        baseline_ranked_evidence_ids=_baseline(),
        evidence_sources=sources,
        excluded_evidence_ids=("atlas-excluded",),
    )
    assert plan.context_evidence_ids == ()

    ambiguous_anchor = GmailTemporalThreadEvidence(
        evidence_id="ambiguous",
        gmail_account_scope_id="account-a",
        gmail_provider_thread_id="atlas-thread",
        available_at="2027-10-02T12:00:00Z",
        message_ordinal=1,
        text="Subject: Atlas launch\n\nAtlas planning.",
        verified_event_bindings=(
            GmailTemporalVerifiedEventBinding(
                event_identity_key="event:atlas",
                aliases=("Atlas launch",),
            ),
            GmailTemporalVerifiedEventBinding(
                event_identity_key="event:atlas-filing",
                aliases=("Atlas launch",),
            ),
        ),
    )
    ambiguous_sources = (ambiguous_anchor,) + _distractors()
    ambiguous_plan = plan_gmail_temporal_thread_retrieval_experiment(
        query="What is the current status of the Atlas launch?",
        temporal_intent="lifecycle",
        source_available_as_of=CUTOFF,
        baseline_ranked_evidence_ids=_baseline("ambiguous"),
        evidence_sources=ambiguous_sources,
    )
    assert ambiguous_plan.target_event_identity_key is None
    assert ambiguous_plan.context_evidence_ids == ()


def test_invalid_account_scoped_thread_or_identity_authority_is_rejected() -> None:
    invalid = GmailTemporalThreadEvidence(
        evidence_id="bad",
        gmail_account_scope_id="bad\naccount",
        gmail_provider_thread_id="thread",
        available_at="2027-10-02T00:00:00Z",
        message_ordinal=1,
        text="Atlas was cancelled.",
        verified_event_bindings=(
            GmailTemporalVerifiedEventBinding(
                event_identity_key="event:atlas",
                aliases=("Atlas launch",),
            ),
        ),
    )
    with pytest.raises(
        GmailTemporalThreadRetrievalExperimentError,
        match="gmail_account_scope_id",
    ):
        plan_gmail_temporal_thread_retrieval_experiment(
            query="What is the Atlas status?",
            temporal_intent="lifecycle",
            source_available_as_of=CUTOFF,
            baseline_ranked_evidence_ids=("bad",),
            evidence_sources=(invalid,),
        )

    overlap = _source(
        "overlap",
        ordinal=1,
        text="Atlas was cancelled.",
        contextual_key="event:atlas",
    )
    with pytest.raises(
        GmailTemporalThreadRetrievalExperimentError,
        match="authority is ambiguous",
    ):
        plan_gmail_temporal_thread_retrieval_experiment(
            query="What is the Atlas status?",
            temporal_intent="lifecycle",
            source_available_as_of=CUTOFF,
            baseline_ranked_evidence_ids=("overlap",),
            evidence_sources=(overlap,),
        )
