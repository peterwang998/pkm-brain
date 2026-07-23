from __future__ import annotations

from dataclasses import replace

import pytest

from pkm_brain.gmail_temporal_contextual_lifecycle import (
    GmailTemporalContextualLifecycleError,
    GmailTemporalPriorEventAnchor,
    plan_gmail_temporal_contextual_lifecycle_experiment,
)
from pkm_brain.gmail_temporal_leads import analyze_gmail_temporal_leads


THREAD = "thread-contextual-public"
CURRENT_AT = "2027-08-10T10:00:00-07:00"


def _anchor(
    *,
    alias: str = "Apollo Interview",
    key: str = "event:apollo-interview",
    thread: str = THREAD,
    status: str = "scheduled",
    verification: str = "external_verified",
    source_internal_at: str = "2027-08-10T08:00:00-07:00",
) -> GmailTemporalPriorEventAnchor:
    return GmailTemporalPriorEventAnchor(
        version="gmail_temporal_prior_event_anchor_v1",
        event_identity_key=key,
        gmail_thread_id=thread,
        source_message_id=f"source:{key}",
        source_internal_at=source_internal_at,
        subject_aliases=(alias,),
        current_status=status,  # type: ignore[arg-type]
        identity_verification=verification,  # type: ignore[arg-type]
    )


def _plan(
    text: str,
    *anchors: GmailTemporalPriorEventAnchor,
):
    return plan_gmail_temporal_contextual_lifecycle_experiment(
        text=text,
        message_internal_at=CURRENT_AT,
        fact_admitted=True,
        temporal_review_rescue=False,
        gmail_thread_id=THREAD,
        gmail_message_id="message-current",
        prior_events=anchors,
    )


def test_standalone_date_free_message_remains_without_a_temporal_lead() -> None:
    text = "Subject: Update\n\nIt was cancelled."

    analysis = analyze_gmail_temporal_leads(
        text=text,
        message_internal_at=CURRENT_AT,
        fact_admitted=True,
        chunk_id="standalone-date-free",
    )
    plan = _plan(text)

    assert analysis.expressions == ()
    assert analysis.leads == ()
    assert plan.observations == ()
    assert plan.omissions[0].reason == "no_verified_active_prior_event"
    assert plan.production_integration_enabled is False


def test_exact_alias_rescues_a_supported_review_observation() -> None:
    plan = _plan(
        "Subject: Update\n\nThe Apollo interview was cancelled.",
        _anchor(),
    )

    assert plan.omissions == ()
    assert len(plan.observations) == 1
    observation = plan.observations[0]
    assert observation.lifecycle == "cancelled"
    assert observation.resolution == "supported"
    assert observation.selected_event_identity_key == "event:apollo-interview"
    assert observation.possible_event_identity_keys == ("event:apollo-interview",)
    assert observation.reasons == ("exact_subject_alias_match",)
    assert observation.candidate_authorization is False
    assert observation.requires_defer is True
    assert observation.routable is False


def test_unique_anaphora_is_retained_but_never_selects_an_identity() -> None:
    plan = _plan(
        "Subject: Update\n\nIt was completed.",
        _anchor(),
    )

    observation = plan.observations[0]
    assert observation.lifecycle == "completed"
    assert observation.resolution == "uncertain"
    assert observation.selected_event_identity_key is None
    assert observation.possible_event_identity_keys == ("event:apollo-interview",)
    assert observation.reasons == ("unique_thread_event_anaphora",)


def test_subject_anchored_pronoun_remains_review_only() -> None:
    plan = _plan(
        "Subject: Apollo Interview Update\n\nIt was cancelled.",
        _anchor(),
    )

    observation = plan.observations[0]
    assert observation.resolution == "uncertain"
    assert observation.selected_event_identity_key is None
    assert observation.possible_event_identity_keys == ("event:apollo-interview",)
    assert observation.reasons == ("subject_only_identity_requires_review",)


def test_two_event_anaphora_preserves_all_options_without_transition() -> None:
    plan = _plan(
        "Subject: Update\n\nIt was cancelled.",
        _anchor(),
        _anchor(alias="Beta Workshop", key="event:beta-workshop"),
    )

    observation = plan.observations[0]
    assert observation.resolution == "uncertain"
    assert observation.selected_event_identity_key is None
    assert observation.possible_event_identity_keys == (
        "event:apollo-interview",
        "event:beta-workshop",
    )
    assert observation.reasons == ("multiple_possible_thread_events",)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (
            "Subject: Apollo Interview Update\n\nIt was not cancelled.",
            "negated_lifecycle_cue",
        ),
        (
            "Subject: Apollo Interview Update\n\n> It was cancelled.",
            "quoted_or_forwarded_lifecycle_cue",
        ),
        (
            "Subject: Apollo Interview Update\n\nThe hotel was cancelled.",
            "explicit_event_identity_mismatch",
        ),
        (
            "Subject: Apollo Interview Update\n\nIt was cancelled on August 14, 2027.",
            "standard_temporal_path_present",
        ),
        (
            "Subject: Apollo Interview Update\n\nIt was cancelled and then completed.",
            "multiple_contextual_lifecycle_cues",
        ),
        (
            "Subject: Apollo Interview Update\n\nIt might be cancelled.",
            "modal_lifecycle_cue",
        ),
        (
            "Subject: Apollo Interview Update\n\nIf it is cancelled, call me.",
            "conditional_lifecycle_cue",
        ),
        (
            "Subject: Apollo Interview Update\n\nI don't think it was cancelled.",
            "denied_lifecycle_cue",
        ),
        (
            "Subject: Apollo Interview Update\n\n"
            "They said it was cancelled, but that is wrong.",
            "reported_lifecycle_cue",
        ),
        (
            "Subject: Apollo Interview Update\n\nWas it cancelled?",
            "interrogative_lifecycle_cue",
        ),
        (
            "Subject: Apollo Interview Update\n\n"
            "The hotel reservation changed. It was cancelled.",
            "stale_subject_recent_object_conflict",
        ),
    ],
)
def test_precision_guards_fail_closed(text: str, reason: str) -> None:
    plan = _plan(text, _anchor())

    assert plan.observations == ()
    assert plan.omissions[0].reason == reason


def test_date_free_reschedule_is_visible_but_cannot_select_an_event() -> None:
    plan = _plan(
        "Subject: Apollo Interview Update\n\nIt was rescheduled.",
        _anchor(),
    )

    observation = plan.observations[0]
    assert observation.lifecycle == "rescheduled"
    assert observation.resolution == "uncertain"
    assert observation.selected_event_identity_key is None
    assert observation.reasons == ("replacement_endpoint_missing",)


def test_unrelated_footer_date_does_not_disable_contextual_rescue() -> None:
    plan = _plan(
        (
            "Subject: Update\n\nThe Apollo interview was cancelled.\n\n"
            "Footer updated August 1, 2027."
        ),
        _anchor(),
    )

    assert plan.omissions == ()
    assert plan.observations[0].resolution == "supported"
    assert plan.observations[0].selected_event_identity_key == (
        "event:apollo-interview"
    )


def test_duplicate_matching_aliases_never_select_one_occurrence() -> None:
    plan = _plan(
        "Subject: Apollo Interview Update\n\nIt was cancelled.",
        _anchor(key="event:apollo-first"),
        _anchor(key="event:apollo-second"),
    )

    observation = plan.observations[0]
    assert observation.resolution == "uncertain"
    assert observation.selected_event_identity_key is None
    assert observation.possible_event_identity_keys == (
        "event:apollo-first",
        "event:apollo-second",
    )


def test_subset_alias_is_retained_for_review_but_never_selects_identity() -> None:
    plan = _plan(
        "Subject: Update\n\nThe Apollo interview was cancelled.",
        _anchor(alias="Apollo Hiring Interview"),
    )

    observation = plan.observations[0]
    assert observation.resolution == "uncertain"
    assert observation.selected_event_identity_key is None
    assert observation.possible_event_identity_keys == ("event:apollo-interview",)
    assert observation.reasons == ("subset_subject_alias_match_requires_review",)


def test_reordered_alias_tokens_are_not_exact_identity_evidence() -> None:
    plan = _plan(
        "Subject: Update\n\nThe Apollo interview was cancelled.",
        _anchor(alias="Interview Apollo"),
    )

    observation = plan.observations[0]
    assert observation.resolution == "uncertain"
    assert observation.selected_event_identity_key is None
    assert observation.reasons == ("subset_subject_alias_match_requires_review",)


@pytest.mark.parametrize(
    "anchor",
    [
        _anchor(thread="other-thread"),
        _anchor(status="cancelled"),
        _anchor(verification="source_bound_self_identity"),
        _anchor(verification="owner_verified"),
        _anchor(source_internal_at="2027-08-10T12:00:00-07:00"),
    ],
)
def test_only_prior_active_cross_message_verified_same_thread_events_are_eligible(
    anchor: GmailTemporalPriorEventAnchor,
) -> None:
    plan = _plan(
        "Subject: Apollo Interview Update\n\nIt was cancelled.",
        anchor,
    )

    assert plan.observations == ()
    assert plan.omissions[0].reason == "no_verified_active_prior_event"


def test_duplicate_event_identity_authority_fails_closed() -> None:
    with pytest.raises(
        GmailTemporalContextualLifecycleError,
        match="identities must be unique",
    ):
        _plan(
            "Subject: Apollo Interview Update\n\nIt was cancelled.",
            _anchor(),
            replace(_anchor(), source_message_id="source:duplicate"),
        )


def test_plan_is_deterministic_and_content_bound() -> None:
    first = _plan(
        "Subject: Apollo Interview Update\n\nIt was cancelled.",
        _anchor(),
    )
    replay = _plan(
        "Subject: Apollo Interview Update\n\nIt was cancelled.",
        _anchor(),
    )
    changed = _plan(
        "Subject: Apollo Interview Update\n\nIt was completed.",
        _anchor(),
    )

    assert replay == first
    assert changed.plan_fingerprint != first.plan_fingerprint
    assert changed.source_sha256 != first.source_sha256


def test_plan_and_observation_bind_the_complete_eligible_anchor_snapshot() -> None:
    apollo = _anchor()
    beta = _anchor(alias="Beta Workshop", key="event:beta-workshop")
    base = _plan(
        "Subject: Update\n\nThe Apollo interview was cancelled.",
        apollo,
        beta,
    )
    changed_source = _plan(
        "Subject: Update\n\nThe Apollo interview was cancelled.",
        replace(apollo, source_message_id="source:apollo-revision"),
        beta,
    )
    changed_time = _plan(
        "Subject: Update\n\nThe Apollo interview was cancelled.",
        replace(apollo, source_internal_at="2027-08-10T08:01:00-07:00"),
        beta,
    )
    changed_aliases = _plan(
        "Subject: Update\n\nThe Apollo interview was cancelled.",
        replace(
            apollo,
            subject_aliases=("Apollo Interview", "Apollo Hiring Interview"),
        ),
        beta,
    )
    changed_key = _plan(
        "Subject: Update\n\nThe Apollo interview was cancelled.",
        replace(apollo, event_identity_key="event:apollo-interview-revised"),
        beta,
    )
    changed_status = _plan(
        "Subject: Update\n\nThe Apollo interview was cancelled.",
        replace(apollo, current_status="cancelled"),
        beta,
    )
    changed_verification = _plan(
        "Subject: Update\n\nThe Apollo interview was cancelled.",
        replace(apollo, identity_verification="owner_verified"),
        beta,
    )

    assert base.eligible_prior_anchors == (apollo, beta)
    assert base.observations[0].selected_event_identity_key == (
        "event:apollo-interview"
    )
    assert base.observations[0].prior_anchor_snapshot_fingerprint == (
        base.eligible_prior_anchor_snapshot_fingerprint
    )
    assert changed_source.observations[0].selected_event_identity_key == (
        "event:apollo-interview"
    )
    assert changed_time.observations[0].selected_event_identity_key == (
        "event:apollo-interview"
    )
    assert changed_source.eligible_prior_anchor_snapshot_fingerprint != (
        base.eligible_prior_anchor_snapshot_fingerprint
    )
    assert changed_time.eligible_prior_anchor_snapshot_fingerprint != (
        base.eligible_prior_anchor_snapshot_fingerprint
    )
    for changed in (
        changed_aliases,
        changed_key,
        changed_status,
        changed_verification,
    ):
        assert changed.eligible_prior_anchor_snapshot_fingerprint != (
            base.eligible_prior_anchor_snapshot_fingerprint
        )
        assert changed.plan_fingerprint != base.plan_fingerprint
    assert changed_source.plan_fingerprint != base.plan_fingerprint
    assert changed_time.plan_fingerprint != base.plan_fingerprint
    assert changed_source.observations[0].observation_id != (
        base.observations[0].observation_id
    )
