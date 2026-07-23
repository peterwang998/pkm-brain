#!/usr/bin/env python3
"""Evaluate the disabled public Gmail temporal-context sidecar.

The fixture is wholly synthetic and constructed to exercise deterministic
invariants. It makes no model, network, Gmail, index, persistence, or
production-retrieval call. Its metrics are regression evidence, not estimates
of population precision or recall on a real mailbox.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pkm_brain.gmail_temporal_thread_retrieval_experiment import (
    EXPERIMENT_MAX_TOTAL_CONTEXT_RESULTS,
    GmailTemporalThreadRetrievalPlan,
    plan_gmail_temporal_thread_retrieval_experiment,
    temporal_thread_query_intent,
)
from pkm_brain.gmail_temporal_thread_retrieval_public import (
    PUBLIC_TEMPORAL_THREAD_RETRIEVAL_CASES,
    PUBLIC_TEMPORAL_THREAD_RETRIEVAL_FIXTURE_VERSION,
    PublicTemporalThreadQuery,
    PublicTemporalThreadRetrievalCase,
)


REPORT_VERSION = "gmail_temporal_thread_retrieval_public_report_v2"


@dataclass(frozen=True)
class _Outcome:
    case: PublicTemporalThreadRetrievalCase
    query: PublicTemporalThreadQuery
    plan: GmailTemporalThreadRetrievalPlan
    baseline_required_count: int
    answer_required_count: int
    missing_required_ids: tuple[str, ...]
    forbidden_context_ids: tuple[str, ...]
    unsafe_context_ids: tuple[str, ...]
    protected_missing_ids: tuple[str, ...]
    future_or_excluded_context_ids: tuple[str, ...]
    required_before_review: bool
    current_head_correct: bool | None
    classifier_agrees: bool


def _timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _evaluate(
    case: PublicTemporalThreadRetrievalCase,
    query: PublicTemporalThreadQuery,
) -> _Outcome:
    plan = plan_gmail_temporal_thread_retrieval_experiment(
        query=query.query_text,
        source_available_as_of=case.source_available_as_of,
        baseline_ranked_evidence_ids=case.baseline_ranked_evidence_ids,
        evidence_sources=case.sources,
        excluded_evidence_ids=case.excluded_evidence_ids,
        temporal_intent=query.authored_intent,
    )
    required = set(query.required_evidence_ids)
    review = set(query.review_only_evidence_ids)
    forbidden = set(query.forbidden_context_evidence_ids)
    baseline = set(plan.direct_ranked_evidence_ids)
    answer = set(plan.answer_evidence_ids)
    source_by_id = {source.evidence_id: source for source in case.sources}
    cutoff = _timestamp(case.source_available_as_of)
    missing_required = tuple(
        evidence_id
        for evidence_id in query.required_evidence_ids
        if evidence_id not in answer
    )
    forbidden_context = tuple(
        evidence_id
        for evidence_id in plan.context_evidence_ids
        if evidence_id in forbidden
    )
    unsafe_context = tuple(
        evidence_id
        for evidence_id in plan.context_evidence_ids
        if evidence_id not in required | review
    )
    protected_missing = tuple(
        evidence_id
        for evidence_id in query.protected_direct_evidence_ids
        if evidence_id not in plan.direct_ranked_evidence_ids
    )
    future_or_excluded = tuple(
        evidence_id
        for evidence_id in plan.context_evidence_ids
        if evidence_id in case.excluded_evidence_ids
        or _timestamp(source_by_id[evidence_id].available_at) > cutoff
    )
    required_context_positions = tuple(
        index
        for index, evidence_id in enumerate(plan.context_evidence_ids)
        if evidence_id in required - baseline
    )
    review_positions = tuple(
        index
        for index, evidence_id in enumerate(plan.context_evidence_ids)
        if evidence_id in review
    )
    required_before_review = (
        not review_positions
        or not required_context_positions
        or (max(required_context_positions) < min(review_positions))
    )
    current_head_correct: bool | None = None
    if query.current_head_evidence_id is not None:
        current_head_correct = bool(
            plan.context_evidence_ids
            and plan.context_evidence_ids[0] == query.current_head_evidence_id
        )
    authored = None if query.authored_intent == "none" else query.authored_intent
    return _Outcome(
        case=case,
        query=query,
        plan=plan,
        baseline_required_count=len(required.intersection(baseline)),
        answer_required_count=len(required.intersection(answer)),
        missing_required_ids=missing_required,
        forbidden_context_ids=forbidden_context,
        unsafe_context_ids=unsafe_context,
        protected_missing_ids=protected_missing,
        future_or_excluded_context_ids=future_or_excluded,
        required_before_review=required_before_review,
        current_head_correct=current_head_correct,
        classifier_agrees=temporal_thread_query_intent(query.query_text) == authored,
    )


def _ratio(numerator: int, denominator: int) -> dict[str, int | float]:
    if denominator < 1:
        raise ValueError("public retrieval metric has no denominator")
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": numerator / denominator,
    }


def _mean(values: tuple[float, ...]) -> dict[str, int | float]:
    if not values:
        raise ValueError("public retrieval metric has no denominator")
    total = sum(values)
    return {
        "sum": total,
        "denominator": len(values),
        "rate": total / len(values),
    }


def evaluate_public_temporal_thread_retrieval() -> dict[str, Any]:
    outcomes = tuple(
        _evaluate(case, query)
        for case in PUBLIC_TEMPORAL_THREAD_RETRIEVAL_CASES
        for query in case.queries
    )
    controls = tuple(
        item
        for item in outcomes
        if item.case.stratum in {"control", "identity_binding_adversary"}
        or item.query.query_id == "apollo-filing-with-demo-anchor"
    )
    lifecycle = tuple(
        item for item in outcomes if item.query.authored_intent == "lifecycle"
    )
    total_required = sum(len(item.query.required_evidence_ids) for item in outcomes)
    baseline_required = sum(item.baseline_required_count for item in outcomes)
    answer_required = sum(item.answer_required_count for item in outcomes)
    baseline_query_recalls = tuple(
        item.baseline_required_count / len(item.query.required_evidence_ids)
        for item in outcomes
    )
    answer_query_recalls = tuple(
        item.answer_required_count / len(item.query.required_evidence_ids)
        for item in outcomes
    )
    baseline_complete = sum(
        item.baseline_required_count == len(item.query.required_evidence_ids)
        for item in outcomes
    )
    answer_complete = sum(not item.missing_required_ids for item in outcomes)
    direct_exact = sum(
        item.plan.direct_ranked_evidence_ids == item.case.baseline_ranked_evidence_ids
        for item in outcomes
    )
    protected_total = sum(
        len(item.query.protected_direct_evidence_ids) for item in outcomes
    )
    protected_retained = protected_total - sum(
        len(item.protected_missing_ids) for item in outcomes
    )
    context_total = sum(len(item.plan.context_evidence_ids) for item in outcomes)
    safe_context = context_total - sum(
        len(item.unsafe_context_ids) for item in outcomes
    )
    verified_context_total = sum(
        len(item.plan.verified_context_evidence_ids) for item in outcomes
    )
    verified_required_context = sum(
        evidence_id in set(item.query.required_evidence_ids)
        for item in outcomes
        for evidence_id in item.plan.verified_context_evidence_ids
    )
    forbidden_count = sum(len(item.forbidden_context_ids) for item in outcomes)
    cutoff_violations = sum(
        len(item.future_or_excluded_context_ids) for item in outcomes
    )
    controls_exact = sum(
        item.plan.direct_ranked_evidence_ids == item.case.baseline_ranked_evidence_ids
        and not item.plan.context_evidence_ids
        for item in controls
    )
    mixed_context = tuple(
        item
        for item in outcomes
        if item.plan.verified_context_evidence_ids
        and item.plan.review_context_evidence_ids
    )
    required_before_review = sum(item.required_before_review for item in mixed_context)
    current_head_cases = tuple(
        item for item in lifecycle if item.current_head_correct is not None
    )
    current_head_correct = sum(
        item.current_head_correct is True for item in current_head_cases
    )
    classifier_agreement = sum(item.classifier_agrees for item in outcomes)
    unique_and_bounded = sum(
        len(item.plan.context_evidence_ids) == len(set(item.plan.context_evidence_ids))
        and len(item.plan.context_evidence_ids) <= EXPERIMENT_MAX_TOTAL_CONTEXT_RESULTS
        and not set(item.plan.context_evidence_ids).intersection(
            item.plan.direct_ranked_evidence_ids
        )
        for item in outcomes
    )
    authorization_errors = sum(
        item.plan.production_integration_enabled is not False
        or item.plan.persisted is not False
        for item in outcomes
    )
    metrics = {
        "baseline_macro_required_source_recall": _mean(
            baseline_query_recalls,
        ),
        "answer_union_macro_required_source_recall": _mean(
            answer_query_recalls,
        ),
        "baseline_micro_required_source_recall": _ratio(
            baseline_required,
            total_required,
        ),
        "answer_union_micro_required_source_recall": _ratio(
            answer_required,
            total_required,
        ),
        "baseline_complete_required_set_recall": _ratio(
            baseline_complete,
            len(outcomes),
        ),
        "answer_union_complete_required_set_recall": _ratio(
            answer_complete,
            len(outcomes),
        ),
        "canonical_direct_ranking_preservation": _ratio(direct_exact, len(outcomes)),
        "safe_context_precision": _ratio(safe_context, context_total),
        "verified_context_required_precision": _ratio(
            verified_required_context,
            verified_context_total,
        ),
        "protected_direct_retention": _ratio(
            protected_retained,
            protected_total,
        ),
        "control_exact_preservation": _ratio(controls_exact, len(controls)),
        "required_before_review_ordering": _ratio(
            required_before_review,
            len(mixed_context),
        ),
        "lifecycle_current_head_recall": _ratio(
            current_head_correct,
            len(current_head_cases),
        ),
        "authored_intent_classifier_agreement": _ratio(
            classifier_agreement,
            len(outcomes),
        ),
        "context_unique_bounded_disjoint": _ratio(
            unique_and_bounded,
            len(outcomes),
        ),
        "forbidden_context_count": forbidden_count,
        "future_or_excluded_context_count": cutoff_violations,
        "authorization_or_persistence_errors": authorization_errors,
    }
    gates = {
        "all_required_sources_recalled_in_answer_union": answer_required
        == total_required,
        "all_required_sets_complete_in_answer_union": answer_complete == len(outcomes),
        "zero_forbidden_context": forbidden_count == 0,
        "zero_future_or_excluded_context": cutoff_violations == 0,
        "all_context_is_required_or_review_only": safe_context == context_total,
        "all_verified_context_is_required": verified_required_context
        == verified_context_total,
        "canonical_direct_ranking_always_preserved": direct_exact == len(outcomes),
        "all_protected_direct_evidence_retained": protected_retained == protected_total,
        "all_controls_exactly_preserved": controls_exact == len(controls),
        "required_context_precedes_review_context": required_before_review
        == len(mixed_context),
        "current_status_queries_surface_required_head": current_head_correct
        == len(current_head_cases),
        "context_is_unique_bounded_and_disjoint": unique_and_bounded == len(outcomes),
        "review_only_non_persisting": authorization_errors == 0,
        "all_authored_intents_match_query_classifier": classifier_agreement
        == len(outcomes),
    }
    return {
        "version": REPORT_VERSION,
        "fixture_version": PUBLIC_TEMPORAL_THREAD_RETRIEVAL_FIXTURE_VERSION,
        "evidence_class": "public_synthetic_constructed_regression_only",
        "population_claim_authorized": False,
        "production_integration_enabled": False,
        "cases": len(PUBLIC_TEMPORAL_THREAD_RETRIEVAL_CASES),
        "queries": len(outcomes),
        "control_queries": len(controls),
        "metrics": metrics,
        "gates": gates,
        "all_gates_passed": all(gates.values()),
        "query_results": [
            {
                "case_id": item.case.case_id,
                "query_id": item.query.query_id,
                "stratum": item.case.stratum,
                "authored_intent": item.query.authored_intent,
                "derived_intent": temporal_thread_query_intent(item.query.query_text),
                "target_event_identity_resolved": (
                    item.plan.target_event_identity_key is not None
                ),
                "direct_count": len(item.plan.direct_ranked_evidence_ids),
                "context_count": len(item.plan.context_evidence_ids),
                "review_context_count": len(item.plan.review_context_evidence_ids),
                "baseline_required_count": item.baseline_required_count,
                "required_count": len(item.query.required_evidence_ids),
                "answer_required_count": item.answer_required_count,
                "complete_required_set": not item.missing_required_ids,
                "forbidden_context_count": len(item.forbidden_context_ids),
                "unsafe_context_count": len(item.unsafe_context_ids),
                "protected_missing_count": len(item.protected_missing_ids),
                "future_or_excluded_context_count": len(
                    item.future_or_excluded_context_ids
                ),
                "required_before_review": item.required_before_review,
                "current_head_correct": item.current_head_correct,
                "classifier_agrees": item.classifier_agrees,
            }
            for item in outcomes
        ],
    }


def main() -> int:
    report = evaluate_public_temporal_thread_retrieval()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["all_gates_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
