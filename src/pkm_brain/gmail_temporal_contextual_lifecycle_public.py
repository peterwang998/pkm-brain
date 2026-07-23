from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


PUBLIC_CONTEXTUAL_LIFECYCLE_FIXTURE_VERSION = (
    "gmail_temporal_contextual_lifecycle_public_fixture_v2"
)

ExpectedOutcome = Literal["supported", "uncertain", "none"]


@dataclass(frozen=True)
class PublicPriorEvent:
    alias: str
    key_suffix: str
    thread: Literal["current", "other"] = "current"
    source_internal_at: str = "2027-08-10T08:00:00-07:00"
    status: Literal["scheduled", "cancelled", "completed"] = "scheduled"
    verification: Literal[
        "unverified",
        "source_bound_self_identity",
        "external_verified",
        "owner_verified",
    ] = "external_verified"


@dataclass(frozen=True)
class PublicContextualLifecycleCase:
    case_id: str
    text: str
    prior_events: tuple[PublicPriorEvent, ...]
    expected_outcome: ExpectedOutcome
    expected_lifecycle: str | None = None
    expected_key_suffix: str | None = None
    expected_omission: str | None = None
    stratum: str = "positive"
    fact_admitted: bool = True
    temporal_review_rescue: bool = False


def _event(
    alias: str = "Apollo Interview",
    key_suffix: str = "apollo",
    **overrides: object,
) -> PublicPriorEvent:
    return PublicPriorEvent(alias=alias, key_suffix=key_suffix, **overrides)  # type: ignore[arg-type]


def _positive(
    case_id: str,
    text: str,
    *,
    lifecycle: str,
    key_suffix: str = "apollo",
    prior_events: tuple[PublicPriorEvent, ...] | None = None,
) -> PublicContextualLifecycleCase:
    return PublicContextualLifecycleCase(
        case_id=case_id,
        text=text,
        prior_events=(
            prior_events
            if prior_events is not None
            else (_event(key_suffix=key_suffix),)
        ),
        expected_outcome="supported",
        expected_lifecycle=lifecycle,
        expected_key_suffix=key_suffix,
    )


def _uncertain(
    case_id: str,
    text: str,
    *,
    lifecycle: str,
    prior_events: tuple[PublicPriorEvent, ...] | None = None,
    stratum: str = "ambiguous_positive",
) -> PublicContextualLifecycleCase:
    return PublicContextualLifecycleCase(
        case_id=case_id,
        text=text,
        prior_events=prior_events if prior_events is not None else (_event(),),
        expected_outcome="uncertain",
        expected_lifecycle=lifecycle,
        stratum=stratum,
    )


def _negative(
    case_id: str,
    text: str,
    *,
    omission: str,
    prior_events: tuple[PublicPriorEvent, ...] | None = None,
    fact_admitted: bool = True,
    temporal_review_rescue: bool = False,
) -> PublicContextualLifecycleCase:
    return PublicContextualLifecycleCase(
        case_id=case_id,
        text=text,
        prior_events=prior_events if prior_events is not None else (_event(),),
        expected_outcome="none",
        expected_omission=omission,
        stratum="matched_negative",
        fact_admitted=fact_admitted,
        temporal_review_rescue=temporal_review_rescue,
    )


PUBLIC_CONTEXTUAL_LIFECYCLE_CASES = (
    _positive(
        "positive_explicit_cancelled",
        "Subject: Update\n\nThe Apollo interview was cancelled.",
        lifecycle="cancelled",
    ),
    _uncertain(
        "positive_subject_anchored_pronoun_cancelled",
        "Subject: Re: Apollo Interview Update\n\nIt was cancelled.",
        lifecycle="cancelled",
    ),
    _uncertain(
        "positive_unique_event_anaphora",
        "Subject: Update\n\nIt was cancelled.",
        lifecycle="cancelled",
    ),
    _positive(
        "positive_explicit_completed",
        "Subject: Update\n\nThe Apollo interview was completed.",
        lifecycle="completed",
    ),
    _uncertain(
        "positive_subject_anchored_pronoun_completed",
        "Subject: Apollo Interview Update\n\nThis has been completed.",
        lifecycle="completed",
    ),
    _positive(
        "positive_called_off",
        "Subject: Update\n\nThe Apollo interview was called off.",
        lifecycle="cancelled",
    ),
    _positive(
        "positive_concluded",
        "Subject: Update\n\nThe Apollo interview concluded.",
        lifecycle="completed",
    ),
    _positive(
        "positive_lifecycle_in_subject",
        "Subject: Canceled: Apollo Interview",
        lifecycle="cancelled",
    ),
    _uncertain(
        "positive_subject_disambiguates_two_events",
        "Subject: Apollo Interview Update\n\nIt was cancelled.",
        lifecycle="cancelled",
        prior_events=(
            _event(),
            _event("Beta Workshop", "beta"),
        ),
    ),
    _uncertain(
        "positive_two_event_anaphora_retained",
        "Subject: Update\n\nIt was cancelled.",
        lifecycle="cancelled",
        prior_events=(
            _event(),
            _event("Beta Workshop", "beta"),
        ),
    ),
    _uncertain(
        "positive_reschedule_without_endpoint",
        "Subject: Apollo Interview Update\n\nIt was rescheduled.",
        lifecycle="rescheduled",
    ),
    _positive(
        "positive_finished",
        "Subject: Update\n\nThe Apollo interview finished.",
        lifecycle="completed",
    ),
    _positive(
        "positive_canceled_spelling",
        "Subject: Update\n\nThe Apollo interview was canceled.",
        lifecycle="cancelled",
    ),
    _positive(
        "positive_reply_subject_lifecycle_prefix",
        "Subject: Re: Canceled: Apollo Interview",
        lifecycle="cancelled",
    ),
    _positive(
        "positive_event_ended",
        "Subject: Update\n\nThe Apollo interview ended.",
        lifecycle="completed",
    ),
    _positive(
        "positive_completed_subject",
        "Subject: Completed: Apollo Interview",
        lifecycle="completed",
    ),
    _uncertain(
        "positive_explicit_rescheduled_without_endpoint",
        "Subject: Update\n\nThe Apollo interview was rescheduled.",
        lifecycle="rescheduled",
    ),
    _positive(
        "positive_two_event_explicit_beta",
        "Subject: Update\n\nThe Beta workshop was completed.",
        lifecycle="completed",
        key_suffix="beta",
        prior_events=(
            _event(),
            _event("Beta Workshop", "beta"),
        ),
    ),
    _uncertain(
        "positive_alias_subset_match",
        "Subject: Update\n\nThe Apollo interview was cancelled.",
        lifecycle="cancelled",
        prior_events=(_event("Apollo Hiring Interview", "apollo"),),
    ),
    _uncertain(
        "positive_reply_subject_completion",
        "Subject: Re: Apollo Interview\n\nIt was completed.",
        lifecycle="completed",
    ),
    _uncertain(
        "positive_unique_this_ended",
        "Subject: Update\n\nThis ended.",
        lifecycle="completed",
    ),
    _uncertain(
        "positive_two_event_reschedule_ambiguous",
        "Subject: Update\n\nIt was moved.",
        lifecycle="rescheduled",
        prior_events=(
            _event(),
            _event("Beta Workshop", "beta"),
        ),
    ),
    _positive(
        "positive_got_cancelled",
        "Subject: Update\n\nThe Apollo interview got cancelled.",
        lifecycle="cancelled",
    ),
    _positive(
        "positive_finished_subject",
        "Subject: Finished: Apollo Interview",
        lifecycle="completed",
    ),
    _positive(
        "positive_unrelated_footer_date_cancelled",
        (
            "Subject: Update\n\nThe Apollo interview was cancelled.\n\n"
            "Footer updated August 1, 2027."
        ),
        lifecycle="cancelled",
    ),
    _uncertain(
        "positive_subject_pronoun_with_unrelated_date",
        (
            "Subject: Apollo Interview Update\n\nIt was completed.\n\n"
            "Account statement dated August 1, 2027."
        ),
        lifecycle="completed",
    ),
    _uncertain(
        "positive_duplicate_alias_cancellation",
        "Subject: Apollo Interview Update\n\nIt was cancelled.",
        lifecycle="cancelled",
        prior_events=(
            _event("Apollo Interview", "apollo-first"),
            _event("Apollo Interview", "apollo-second"),
        ),
    ),
    _uncertain(
        "positive_recurrence_alias_completion",
        "Subject: Weekly Research Sync Update\n\nIt was completed.",
        lifecycle="completed",
        prior_events=(
            _event("Weekly Research Sync", "sync-first"),
            _event("Weekly Research Sync", "sync-second"),
        ),
    ),
    _positive(
        "positive_completion_with_unrelated_order_date",
        (
            "Subject: Update\n\nThe Apollo interview concluded.\n\n"
            "Order placed August 2, 2027."
        ),
        lifecycle="completed",
    ),
    _positive(
        "positive_subject_cancel_with_unrelated_body_date",
        "Subject: Cancelled: Apollo Interview\n\nNewsletter dated August 3, 2027.",
        lifecycle="cancelled",
    ),
    _uncertain(
        "positive_unique_anaphora_with_unrelated_date",
        ("Subject: Update\n\nIt was cancelled.\n\nInvoice issued August 4, 2027."),
        lifecycle="cancelled",
    ),
    _positive(
        "positive_explicit_cancel_with_quoted_date",
        (
            "Subject: Update\n\nThe Apollo interview was cancelled.\n\n"
            "> The old invite was for August 5, 2027."
        ),
        lifecycle="cancelled",
    ),
    _negative(
        "negative_not_cancelled",
        "Subject: Apollo Interview Update\n\nIt was not cancelled.",
        omission="negated_lifecycle_cue",
    ),
    _negative(
        "negative_never_completed",
        "Subject: Apollo Interview Update\n\nIt was never completed.",
        omission="negated_lifecycle_cue",
    ),
    _negative(
        "negative_quoted_cancellation",
        "Subject: Apollo Interview Update\n\n> It was cancelled.",
        omission="quoted_or_forwarded_lifecycle_cue",
    ),
    _negative(
        "negative_forwarded_completion",
        (
            "Subject: Apollo Interview Update\n\n"
            "---------- Forwarded message ----------\nIt was completed."
        ),
        omission="quoted_or_forwarded_lifecycle_cue",
    ),
    _negative(
        "negative_unrelated_hotel",
        "Subject: Apollo Interview Update\n\nThe hotel was cancelled.",
        omission="explicit_event_identity_mismatch",
    ),
    _negative(
        "negative_explicit_other_event",
        "Subject: Update\n\nThe Beta workshop was completed.",
        omission="explicit_event_identity_mismatch",
    ),
    _negative(
        "negative_no_prior_event",
        "Subject: Apollo Interview Update\n\nIt was cancelled.",
        omission="no_verified_active_prior_event",
        prior_events=(),
    ),
    _negative(
        "negative_cross_thread_anchor",
        "Subject: Apollo Interview Update\n\nIt was cancelled.",
        omission="no_verified_active_prior_event",
        prior_events=(_event(thread="other"),),
    ),
    _negative(
        "negative_future_anchor",
        "Subject: Apollo Interview Update\n\nIt was cancelled.",
        omission="no_verified_active_prior_event",
        prior_events=(_event(source_internal_at="2027-08-10T12:00:00-07:00"),),
    ),
    _negative(
        "negative_already_cancelled_anchor",
        "Subject: Apollo Interview Update\n\nIt was completed.",
        omission="no_verified_active_prior_event",
        prior_events=(_event(status="cancelled"),),
    ),
    _negative(
        "negative_standard_temporal_path",
        (
            "Subject: Apollo Interview Update\n\n"
            "The Apollo interview was cancelled on August 14, 2027."
        ),
        omission="standard_temporal_path_present",
    ),
    _negative(
        "negative_conflicting_lifecycle_cues",
        (
            "Subject: Apollo Interview Update\n\n"
            "The Apollo interview was cancelled and then completed."
        ),
        omission="multiple_contextual_lifecycle_cues",
    ),
    _negative(
        "negative_wasnt_canceled",
        "Subject: Apollo Interview Update\n\nIt wasn't canceled.",
        omission="negated_lifecycle_cue",
    ),
    _negative(
        "negative_hasnt_completed",
        "Subject: Apollo Interview Update\n\nIt hasn't been completed.",
        omission="negated_lifecycle_cue",
    ),
    _negative(
        "negative_on_wrote_quote",
        (
            "Subject: Apollo Interview Update\n\n"
            "On Alex wrote:\nThe Apollo interview was cancelled."
        ),
        omission="quoted_or_forwarded_lifecycle_cue",
    ),
    _negative(
        "negative_unrelated_order",
        "Subject: Apollo Interview Update\n\nThe order was canceled.",
        omission="explicit_event_identity_mismatch",
    ),
    _negative(
        "negative_unrelated_subscription",
        "Subject: Apollo Interview Update\n\nThe subscription was cancelled.",
        omission="explicit_event_identity_mismatch",
    ),
    _negative(
        "negative_unrelated_reservation",
        "Subject: Apollo Interview Update\n\nThe dinner reservation was completed.",
        omission="explicit_event_identity_mismatch",
    ),
    _negative(
        "negative_two_event_explicit_third",
        "Subject: Update\n\nThe Gamma review was cancelled.",
        omission="explicit_event_identity_mismatch",
        prior_events=(
            _event(),
            _event("Beta Workshop", "beta"),
        ),
    ),
    _negative(
        "negative_source_self_anchor",
        "Subject: Apollo Interview Update\n\nIt was cancelled.",
        omission="no_verified_active_prior_event",
        prior_events=(_event(verification="source_bound_self_identity"),),
    ),
    _negative(
        "negative_owner_without_receipt",
        "Subject: Apollo Interview Update\n\nIt was completed.",
        omission="no_verified_active_prior_event",
        prior_events=(_event(verification="owner_verified"),),
    ),
    _negative(
        "negative_unverified_anchor",
        "Subject: Apollo Interview Update\n\nIt was cancelled.",
        omission="no_verified_active_prior_event",
        prior_events=(_event(verification="unverified"),),
    ),
    _negative(
        "negative_not_admitted",
        "Subject: Apollo Interview Update\n\nIt was cancelled.",
        omission="message_not_admitted",
        fact_admitted=False,
    ),
    _negative(
        "negative_cross_thread_matching_other_event",
        "Subject: Update\n\nThe Beta workshop was completed.",
        omission="explicit_event_identity_mismatch",
        prior_events=(
            _event(),
            _event("Beta Workshop", "beta", thread="other"),
        ),
    ),
    _negative(
        "negative_modal_might_cancelled",
        "Subject: Apollo Interview Update\n\nIt might be cancelled.",
        omission="modal_lifecycle_cue",
    ),
    _negative(
        "negative_conditional_if_cancelled",
        "Subject: Apollo Interview Update\n\nIf it is cancelled, I will call.",
        omission="conditional_lifecycle_cue",
    ),
    _negative(
        "negative_denied_thought_cancelled",
        "Subject: Apollo Interview Update\n\nI don't think it was cancelled.",
        omission="denied_lifecycle_cue",
    ),
    _negative(
        "negative_reported_then_refuted",
        (
            "Subject: Apollo Interview Update\n\n"
            "They said it was cancelled, but that is wrong."
        ),
        omission="reported_lifecycle_cue",
    ),
    _negative(
        "negative_stale_subject_hotel_pronoun",
        (
            "Subject: Apollo Interview Update\n\n"
            "The hotel reservation changed. It was cancelled."
        ),
        omission="stale_subject_recent_object_conflict",
    ),
    _negative(
        "negative_stale_subject_subscription_pronoun",
        (
            "Subject: Apollo Interview Update\n\n"
            "The subscription renewed. It was cancelled."
        ),
        omission="stale_subject_recent_object_conflict",
    ),
    _negative(
        "negative_interrogative_cancelled",
        "Subject: Apollo Interview Update\n\nWas it cancelled?",
        omission="interrogative_lifecycle_cue",
    ),
    _negative(
        "negative_conditional_unless_completed",
        "Subject: Apollo Interview Update\n\nUnless it is completed, keep working.",
        omission="conditional_lifecycle_cue",
    ),
    _negative(
        "negative_epistemic_probably_cancelled",
        "Subject: Apollo Interview Update\n\nIt was probably cancelled.",
        omission="epistemic_lifecycle_cue",
    ),
    _negative(
        "negative_epistemic_allegedly_completed",
        "Subject: Apollo Interview Update\n\nIt was allegedly completed.",
        omission="epistemic_lifecycle_cue",
    ),
    _negative(
        "negative_epistemic_appears_cancelled",
        "Subject: Update\n\nIt appears that the Apollo interview was cancelled.",
        omission="epistemic_lifecycle_cue",
    ),
    _negative(
        "negative_epistemic_seems_completed",
        "Subject: Update\n\nIt seems the Apollo interview was completed.",
        omission="epistemic_lifecycle_cue",
    ),
    _negative(
        "negative_named_speaker_said_cancelled",
        "Subject: Update\n\nAlex said the Apollo interview was cancelled.",
        omission="reported_lifecycle_cue",
    ),
    _negative(
        "negative_named_speaker_reported_completed",
        "Subject: Update\n\nMaya Chen reported the interview was completed.",
        omission="reported_lifecycle_cue",
    ),
    _negative(
        "negative_according_to_report",
        "Subject: Update\n\nAccording to Alex, the interview was cancelled.",
        omission="reported_lifecycle_cue",
    ),
    _negative(
        "negative_false_that_cancelled",
        "Subject: Update\n\nIt is false that the interview was cancelled.",
        omission="refuted_lifecycle_cue",
    ),
    _negative(
        "negative_claim_refuted_after_cue",
        "Subject: Update\n\nThe claim that the interview was cancelled is false.",
        omission="refuted_lifecycle_cue",
    ),
    _negative(
        "negative_inline_ascii_quote",
        'Subject: Update\n\n"The Apollo interview was cancelled."',
        omission="quoted_or_forwarded_lifecycle_cue",
    ),
    _negative(
        "negative_inline_curly_quote",
        "Subject: Update\n\n“The Apollo interview was completed.”",
        omission="quoted_or_forwarded_lifecycle_cue",
    ),
    _negative(
        "negative_embedded_interrogative",
        "Subject: Apollo Interview Update\n\nDo you know whether it was cancelled?",
        omission="interrogative_lifecycle_cue",
    ),
    _negative(
        "negative_postposed_named_speaker",
        "Subject: Update\n\nThe Apollo interview was cancelled, Pat said.",
        omission="reported_lifecycle_cue",
    ),
    _negative(
        "negative_postposed_according_to",
        ("Subject: Update\n\nThe Apollo interview was cancelled, according to Pat."),
        omission="reported_lifecycle_cue",
    ),
    _negative(
        "negative_postposed_epistemic",
        "Subject: Update\n\nThe Apollo interview was cancelled, apparently.",
        omission="epistemic_lifecycle_cue",
    ),
    _negative(
        "negative_unpunctuated_interrogative",
        "Subject: Update\n\nWas the Apollo interview cancelled",
        omission="interrogative_lifecycle_cue",
    ),
    _negative(
        "negative_postposed_confirmation",
        "Subject: Update\n\nThe Apollo interview was cancelled, Pat confirmed.",
        omission="reported_lifecycle_cue",
    ),
    _negative(
        "negative_postposed_was_told",
        "Subject: Update\n\nThe Apollo interview was cancelled, or so I was told.",
        omission="reported_lifecycle_cue",
    ),
    _negative(
        "negative_expected_cancellation",
        "Subject: Update\n\nThe Apollo interview is expected to be cancelled.",
        omission="modal_lifecycle_cue",
    ),
    _negative(
        "negative_future_cancellation",
        "Subject: Update\n\nThe Apollo interview will be cancelled.",
        omission="modal_lifecycle_cue",
    ),
    _negative(
        "negative_doubted_cancellation",
        "Subject: Update\n\nI doubt the Apollo interview was cancelled.",
        omission="epistemic_lifecycle_cue",
    ),
    _negative(
        "negative_as_far_as_known",
        "Subject: Update\n\nAs far as I know, the Apollo interview was cancelled.",
        omission="epistemic_lifecycle_cue",
    ),
    _negative(
        "negative_confirmation_request",
        "Subject: Update\n\nPlease confirm the Apollo interview was cancelled.",
        omission="interrogative_lifecycle_cue",
    ),
)
