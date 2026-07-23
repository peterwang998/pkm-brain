#!/usr/bin/env python3
"""Evaluate the disabled public date-free thread-lifecycle experiment.

The fixture is wholly synthetic.  This script makes no model, network,
persistence, retrieval, or production-pipeline call and emits no source text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pkm_brain.gmail_temporal_contextual_lifecycle import (
    GmailTemporalContextualLifecycleObservation,
    GmailTemporalPriorEventAnchor,
    plan_gmail_temporal_contextual_lifecycle_experiment,
)
from pkm_brain.gmail_temporal_contextual_lifecycle_public import (
    PUBLIC_CONTEXTUAL_LIFECYCLE_CASES,
    PUBLIC_CONTEXTUAL_LIFECYCLE_FIXTURE_VERSION,
    PublicContextualLifecycleCase,
)
from pkm_brain.gmail_temporal_batching import plan_gmail_temporal_selector_batches
from pkm_brain.gmail_temporal_frontier import (
    build_gmail_temporal_candidate_frontier,
)
from pkm_brain.gmail_temporal_leads import analyze_gmail_temporal_leads


REPORT_VERSION = "gmail_temporal_contextual_lifecycle_public_report_v2"
CURRENT_AT = "2027-08-10T10:00:00-07:00"


@dataclass(frozen=True)
class _Outcome:
    case: PublicContextualLifecycleCase
    observation: GmailTemporalContextualLifecycleObservation | None
    omission_reason: str | None
    expected_key: str | None
    baseline_lead_count: int
    baseline_cue_linked: bool
    correct: bool
    critical_error: bool


def _anchors(
    case: PublicContextualLifecycleCase,
    *,
    thread_id: str,
) -> tuple[GmailTemporalPriorEventAnchor, ...]:
    return tuple(
        GmailTemporalPriorEventAnchor(
            version="gmail_temporal_prior_event_anchor_v1",
            event_identity_key=f"event:{case.case_id}:{item.key_suffix}",
            gmail_thread_id=(
                thread_id if item.thread == "current" else f"other-{thread_id}"
            ),
            source_message_id=f"prior-{case.case_id}-{index}",
            source_internal_at=item.source_internal_at,
            subject_aliases=(item.alias,),
            current_status=item.status,
            identity_verification=item.verification,
        )
        for index, item in enumerate(case.prior_events, start=1)
    )


def _evaluate_case(case: PublicContextualLifecycleCase) -> _Outcome:
    thread_id = f"public-thread-{case.case_id}"
    expected_key = (
        f"event:{case.case_id}:{case.expected_key_suffix}"
        if case.expected_key_suffix is not None
        else None
    )
    baseline = analyze_gmail_temporal_leads(
        text=case.text,
        message_internal_at=CURRENT_AT,
        fact_admitted=case.fact_admitted,
        temporal_review_rescue=case.temporal_review_rescue,
        chunk_id=f"public-baseline:{case.case_id}",
    )
    lifecycle_mentions = tuple(
        item
        for item in baseline.mentions
        if item.mention_type == "lifecycle"
        and item.lifecycle_role in {"cancelled", "completed", "rescheduled"}
    )
    baseline_plan = plan_gmail_temporal_selector_batches(
        text=case.text,
        analysis=baseline,
    )
    baseline_expressions = {item.expression_id: item for item in baseline.expressions}
    baseline_cue_linked = bool(
        len(lifecycle_mentions) == 1
        and any(
            candidate.lifecycle_mention_id == lifecycle_mentions[0].mention_id
            and baseline_expressions[candidate.expression_id].segment_id
            == lifecycle_mentions[0].segment_id
            for batch in baseline_plan.batches
            for candidate in build_gmail_temporal_candidate_frontier(
                analysis=baseline,
                batch=batch,
            ).candidates
        )
    )
    plan = plan_gmail_temporal_contextual_lifecycle_experiment(
        text=case.text,
        message_internal_at=CURRENT_AT,
        fact_admitted=case.fact_admitted,
        temporal_review_rescue=case.temporal_review_rescue,
        gmail_thread_id=thread_id,
        gmail_message_id=f"current-{case.case_id}",
        prior_events=_anchors(case, thread_id=thread_id),
    )
    observation = plan.observations[0] if len(plan.observations) == 1 else None
    omission_reason = plan.omissions[0].reason if len(plan.omissions) == 1 else None

    if case.expected_outcome == "none":
        correct = (
            not plan.observations
            and len(plan.omissions) == 1
            and omission_reason == case.expected_omission
        )
        critical = bool(plan.observations)
    elif case.expected_outcome == "supported":
        correct = bool(
            observation is not None
            and not plan.omissions
            and observation.resolution == "supported"
            and observation.lifecycle == case.expected_lifecycle
            and observation.selected_event_identity_key == expected_key
            and observation.possible_event_identity_keys == (expected_key,)
        )
        critical = bool(
            observation is not None
            and (
                observation.lifecycle != case.expected_lifecycle
                or observation.selected_event_identity_key != expected_key
            )
        )
    else:
        correct = bool(
            observation is not None
            and not plan.omissions
            and observation.resolution == "uncertain"
            and observation.lifecycle == case.expected_lifecycle
            and observation.selected_event_identity_key is None
            and observation.possible_event_identity_keys
        )
        critical = bool(
            observation is not None
            and (
                observation.lifecycle != case.expected_lifecycle
                or observation.selected_event_identity_key is not None
            )
        )
    return _Outcome(
        case=case,
        observation=observation,
        omission_reason=omission_reason,
        expected_key=expected_key,
        baseline_lead_count=len(baseline.leads),
        baseline_cue_linked=baseline_cue_linked,
        correct=correct,
        critical_error=critical,
    )


def _ratio(numerator: int, denominator: int) -> dict[str, int | float]:
    if denominator < 1:
        raise ValueError("public contextual-lifecycle metric has no denominator")
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": numerator / denominator,
    }


def evaluate_public_contextual_lifecycle() -> dict[str, Any]:
    outcomes = tuple(_evaluate_case(case) for case in PUBLIC_CONTEXTUAL_LIFECYCLE_CASES)
    positives = tuple(item for item in outcomes if item.case.expected_outcome != "none")
    confirmed = tuple(
        item for item in outcomes if item.case.expected_outcome == "supported"
    )
    negatives = tuple(item for item in outcomes if item.case.expected_outcome == "none")
    emitted = tuple(item for item in outcomes if item.observation is not None)
    supported_emitted = tuple(
        item
        for item in emitted
        if item.observation is not None and item.observation.resolution == "supported"
    )
    ambiguous_emitted = tuple(
        item
        for item in emitted
        if item.observation is not None and item.observation.resolution == "uncertain"
    )
    effective_correct = sum(item.correct for item in positives)
    confirmed_correct = sum(item.correct for item in confirmed)
    negative_correct = sum(item.correct for item in negatives)
    supported_correct = sum(item.correct for item in supported_emitted)
    review_correct = sum(item.correct for item in emitted)
    critical = sum(item.critical_error for item in outcomes)
    ambiguous_selected = sum(
        item.observation is not None
        and item.observation.resolution == "uncertain"
        and item.observation.selected_event_identity_key is not None
        for item in outcomes
    )
    authorization_errors = sum(
        item.observation is not None
        and (
            item.observation.candidate_authorization is not False
            or item.observation.requires_defer is not True
            or item.observation.routable is not False
        )
        for item in outcomes
    )
    metrics = {
        "baseline_any_temporal_lead_presence": _ratio(
            sum(bool(item.baseline_lead_count) for item in positives),
            len(positives),
        ),
        "baseline_cue_linked_lifecycle_recall": _ratio(
            sum(item.baseline_cue_linked for item in positives),
            len(positives),
        ),
        "effective_lifecycle_recall": _ratio(effective_correct, len(positives)),
        "confirmed_lifecycle_recall": _ratio(confirmed_correct, len(confirmed)),
        "supported_observation_precision": _ratio(
            supported_correct,
            len(supported_emitted),
        ),
        "review_observation_precision": _ratio(review_correct, len(emitted)),
        "matched_negative_suppression": _ratio(negative_correct, len(negatives)),
        "ambiguous_observations_without_selected_identity": {
            "count": len(ambiguous_emitted) - ambiguous_selected,
            "denominator": len(ambiguous_emitted),
            "rate": (
                (len(ambiguous_emitted) - ambiguous_selected) / len(ambiguous_emitted)
            ),
        },
        "critical_errors": critical,
        "authorization_errors": authorization_errors,
    }
    gates = {
        "effective_recall_at_least_95_percent": (
            metrics["effective_lifecycle_recall"]["rate"] >= 0.95
        ),
        "supported_precision_at_least_95_percent": (
            metrics["supported_observation_precision"]["rate"] >= 0.95
        ),
        "review_precision_at_least_90_percent": (
            metrics["review_observation_precision"]["rate"] >= 0.90
        ),
        "all_matched_negatives_suppressed": negative_correct == len(negatives),
        "no_critical_errors": critical == 0,
        "no_ambiguous_identity_selection": ambiguous_selected == 0,
        "all_outputs_deferred_non_authorizing_non_routable": authorization_errors == 0,
    }
    return {
        "version": REPORT_VERSION,
        "fixture_version": PUBLIC_CONTEXTUAL_LIFECYCLE_FIXTURE_VERSION,
        "scope": "disabled_public_deterministic_experiment_not_production_evidence",
        "case_count": len(outcomes),
        "positive_count": len(positives),
        "confirmed_positive_count": len(confirmed),
        "ambiguous_positive_count": len(positives) - len(confirmed),
        "matched_negative_count": len(negatives),
        "emitted_observation_count": len(emitted),
        "metrics": metrics,
        "metric_comparison": {
            "previous_reported_fixture_v1": {
                "supported_observation_precision": _ratio(18, 18),
                "effective_lifecycle_recall": _ratio(32, 32),
                "matched_negative_suppression": _ratio(32, 32),
                "caveat": (
                    "included_one_subset_alias_as_supported_and_only_32_negatives"
                ),
            },
            "hardened_fixture_v2": {
                "supported_observation_precision": metrics[
                    "supported_observation_precision"
                ],
                "effective_lifecycle_recall": metrics["effective_lifecycle_recall"],
                "matched_negative_suppression": metrics["matched_negative_suppression"],
                "caveat": "expanded_negative_denominator_and_exact_alias_support_only",
            },
        },
        "gates": gates,
        "gate_passed": all(gates.values()),
        "claims": {
            "production_integration_enabled": False,
            "private_gmail_used": False,
            "external_model_calls": 0,
            "network_calls": 0,
            "persistence_writes": 0,
            "source_text_emitted": False,
            "semantic_model_precision_measured": False,
            "deterministic_fixture_semantics_measured": True,
        },
    }


def main() -> None:
    print(json.dumps(evaluate_public_contextual_lifecycle(), sort_keys=True))


if __name__ == "__main__":
    main()
