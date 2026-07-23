#!/usr/bin/env python3
"""Run the public-only Gmail temporal v20 structural contrast benchmark.

The benchmark uses checked-in synthetic text, deterministic candidate frontiers,
and fixed fixture verdicts. It makes no model, network, persistence, private-mail,
semantic-precision, or retrieval claim. Message text is never emitted.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, replace
from typing import Any, Mapping

from pkm_brain import gmail_temporal_review as temporal_review
from pkm_brain.gmail_temporal_batching import (
    GmailTemporalBatchPlan,
    plan_gmail_temporal_selector_batches,
)
from pkm_brain.gmail_temporal_frontier import (
    GmailTemporalCandidatePagePlan,
    GmailTemporalCandidatePageVerdicts,
    GmailTemporalCandidateVerdict,
    GmailTemporalVerificationCandidate,
    build_gmail_temporal_candidate_frontier,
    plan_gmail_temporal_candidate_pages,
    validate_gmail_temporal_candidate_ensemble_verdict_set,
)
from pkm_brain.gmail_temporal_leads import (
    TemporalLeadAnalysis,
    analyze_gmail_temporal_leads,
)
from pkm_brain.gmail_temporal_public_v20 import (
    PUBLIC_GMAIL_TEMPORAL_V20_CASES,
    PUBLIC_GMAIL_TEMPORAL_V20_FIXTURE_VERSION,
    PublicGmailTemporalV20Case,
)
from pkm_brain.gmail_temporal_review import (
    GmailTemporalReviewBatchResult,
    GmailTemporalReviewProjection,
    project_gmail_temporal_review,
)


REPORT_VERSION = "gmail_temporal_public_v20_contrast_report_v2"
REPORT_SCOPE = "public_only_deterministic_structure_not_semantic_precision"
ANCHOR = "2027-08-10T09:00:00-07:00"


@dataclass(frozen=True)
class _CaseOutcome:
    case: PublicGmailTemporalV20Case
    analysis: TemporalLeadAnalysis
    plan: GmailTemporalBatchPlan
    candidates: tuple[GmailTemporalVerificationCandidate, ...]
    projection: GmailTemporalReviewProjection
    selected_artifact_count: int
    group_matches: bool
    fail_closed: bool
    lifecycle_matches: bool
    abbreviated_day_matches: bool
    raw_fallback_matches: bool
    structural_authority_safe: bool
    errors: tuple[str, ...]


def _page_rows(
    page_plan: GmailTemporalCandidatePagePlan,
    selected: Mapping[str, str],
) -> tuple[GmailTemporalCandidatePageVerdicts, ...]:
    return tuple(
        GmailTemporalCandidatePageVerdicts(
            frontier_fingerprint=page_plan.frontier_fingerprint,
            page_fingerprint=page.page_fingerprint,
            verdicts=tuple(
                GmailTemporalCandidateVerdict(
                    candidate_id=candidate_id,
                    verdict=selected.get(candidate_id, "unsupported"),  # type: ignore[arg-type]
                )
                for cluster in page.clusters
                for candidate_id in cluster.candidate_ids
            ),
        )
        for page in page_plan.pages
    )


def _fixture_selection(
    case: PublicGmailTemporalV20Case,
    candidates: tuple[GmailTemporalVerificationCandidate, ...],
) -> dict[str, str]:
    if case.selection == "none" or not candidates:
        return {}
    if case.selection == "first_uncertain":
        return {candidates[0].candidate_id: "uncertain"}
    candidate = next(
        (item for item in candidates if item.lifecycle == "cancelled"),
        None,
    )
    return {candidate.candidate_id: "uncertain"} if candidate is not None else {}


def _projection_for(
    case: PublicGmailTemporalV20Case,
) -> tuple[
    TemporalLeadAnalysis,
    GmailTemporalBatchPlan,
    tuple[GmailTemporalVerificationCandidate, ...],
    GmailTemporalReviewProjection,
]:
    analysis = analyze_gmail_temporal_leads(
        text=case.text,
        message_internal_at=ANCHOR,
        fact_admitted=True,
        chunk_id=f"public-v20:{case.case_id}",
    )
    plan = plan_gmail_temporal_selector_batches(
        text=case.text,
        analysis=analysis,
    )
    results: list[GmailTemporalReviewBatchResult] = []
    all_candidates: list[GmailTemporalVerificationCandidate] = []
    for batch in plan.batches:
        frontier = build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        )
        page_plan = plan_gmail_temporal_candidate_pages(
            analysis=analysis,
            batch=batch,
        )
        all_candidates.extend(frontier.candidates)
        selected = _fixture_selection(case, frontier.candidates)
        rows = _page_rows(page_plan, selected)
        results.append(
            GmailTemporalReviewBatchResult(
                batch=batch,
                page_plan=page_plan,
                ensemble=validate_gmail_temporal_candidate_ensemble_verdict_set(
                    analysis=analysis,
                    batch=batch,
                    plan=page_plan,
                    runs=(rows, rows, rows),
                ),
            )
        )
    projection = project_gmail_temporal_review(
        text=case.text,
        analysis=analysis,
        batch_plan=plan,
        batch_results=tuple(results),
    )
    return analysis, plan, tuple(all_candidates), projection


def _expected_group_matches(
    case: PublicGmailTemporalV20Case,
    projection: GmailTemporalReviewProjection,
) -> bool:
    if case.expected_group_kind is None:
        return True
    groups = tuple(
        item for item in projection.groups if item.kind == case.expected_group_kind
    )
    if len(groups) != 1:
        return False
    group = groups[0]
    return (
        group.coverage == case.expected_group_coverage
        and tuple(item.role for item in group.members) == case.expected_group_roles
        and group.reasons == case.expected_group_reasons
        and group.candidate_authorization is False
        and group.requires_defer is True
        and group.routable is False
        and all(
            member.candidate_authorization is False
            and member.requires_defer is True
            and member.routable is False
            for member in group.members
        )
    )


def _case_fails_closed(
    case: PublicGmailTemporalV20Case,
    candidates: tuple[GmailTemporalVerificationCandidate, ...],
    projection: GmailTemporalReviewProjection,
) -> bool:
    expectation = case.fail_closed_expectation
    if expectation is None:
        return True
    if expectation == "candidate_free":
        return not candidates and not projection.artifacts and not projection.groups
    if expectation == "no_reschedule":
        return not any(item.kind == "reschedule" for item in projection.groups)
    groups = tuple(item for item in projection.groups if item.kind == "reschedule")
    return (
        len(groups) == 1
        and groups[0].coverage == "conflicted"
        and _expected_group_matches(case, projection)
        and all(item.kind == "uncertainty_sidecar" for item in projection.artifacts)
    )


def _artifact_lifecycles(
    analysis: TemporalLeadAnalysis,
    projection: GmailTemporalReviewProjection,
) -> tuple[str, ...]:
    output: list[str] = []
    for expression in analysis.expressions:
        values = {
            hypothesis.lifecycle
            for artifact in projection.artifacts
            for hypothesis in artifact.hypotheses
            if hypothesis.expression_id == expression.expression_id
        }
        if len(values) == 1:
            output.append(next(iter(values)))
        elif values:
            output.append("conflicted")
    return tuple(output)


def _abbreviated_day_matches(
    case: PublicGmailTemporalV20Case,
    analysis: TemporalLeadAnalysis,
) -> bool:
    expectation = case.abbreviated_day_expectation
    if expectation is None:
        return True
    expressions = tuple(
        item
        for item in analysis.expressions
        if item.form == "abbreviated_shared_month_day"
    )
    if expectation == "none":
        return not expressions
    if len(expressions) != 1:
        return False
    expression = expressions[0]
    return (
        expression.resolution_status == expectation
        and (
            case.expected_abbreviated_day_surface is None
            or case.text[expression.start : expression.end]
            == case.expected_abbreviated_day_surface
        )
        and expression.normalized_options == case.expected_abbreviated_day_options
        and expression.calendar_date_options == case.expected_abbreviated_day_options
        and expression.blockers == case.expected_abbreviated_day_blockers
        and expression.resolution_basis
        == ("month_and_year_inherited_from_preceding_explicit_reschedule_endpoint",)
        and expression.local_time is None
    )


def _raw_fallback_matches(
    case: PublicGmailTemporalV20Case,
    analysis: TemporalLeadAnalysis,
) -> bool:
    expected_count = case.raw_fallback_retained_expression_count
    if expected_count is None:
        return True
    abbreviated = tuple(
        item
        for item in analysis.expressions
        if item.form == "abbreviated_shared_month_day"
    )
    retained = tuple(
        item
        for item in analysis.expressions
        if item.form != "abbreviated_shared_month_day"
    )
    if len(abbreviated) != 1 or len(retained) != expected_count or not retained:
        return False
    lagging_analysis = replace(analysis, expressions=retained)
    frames = temporal_review._structural_frames(  # noqa: SLF001
        text=case.text,
        analysis=lagging_analysis,
    )
    if len(frames) != 1 or frames[0].kind != "reschedule":
        return False
    frame = frames[0]
    return (
        tuple(member.role for member in frame.members)
        == case.expected_raw_fallback_roles
        and frame.missing_roles == case.expected_raw_fallback_missing_roles
        and frame.conflict_reasons == case.expected_raw_fallback_reasons
        and frame.source_end > retained[-1].end
    )


def _structural_authority_is_safe(
    projection: GmailTemporalReviewProjection,
) -> bool:
    return (
        projection.complete is True
        and projection.requires_defer is True
        and projection.routable is False
        and all(
            artifact.requires_defer is True and artifact.routable is False
            for artifact in projection.artifacts
        )
        and all(
            review.candidate_authorization is False
            and review.requires_defer is True
            and review.routable is False
            for review in projection.cluster_reviews
        )
        and all(
            group.candidate_authorization is False
            and group.requires_defer is True
            and group.routable is False
            for group in projection.groups
        )
    )


def _evaluate_case(case: PublicGmailTemporalV20Case) -> _CaseOutcome:
    analysis, plan, candidates, projection = _projection_for(case)
    group_matches = _expected_group_matches(case, projection)
    fail_closed = _case_fails_closed(case, candidates, projection)
    lifecycles = _artifact_lifecycles(analysis, projection)
    lifecycle_matches = (
        not case.expected_artifact_lifecycles
        or lifecycles == case.expected_artifact_lifecycles
    )
    abbreviated_day_matches = _abbreviated_day_matches(case, analysis)
    raw_fallback_matches = _raw_fallback_matches(case, analysis)
    structural_authority_safe = _structural_authority_is_safe(projection)
    errors: list[str] = []
    if case.positive_candidate and not candidates:
        errors.append("positive_candidate_missing")
    if (
        case.positive_candidate
        and case.selection != "none"
        and not projection.artifacts
    ):
        errors.append("positive_fixture_artifact_missing")
    if case.selected_negative and projection.artifacts:
        errors.append("fixture_selected_negative_artifact")
    if case.selected_negative and not case.advertising_negative and candidates:
        errors.append("matched_non_advertising_negative_candidate_exposure")
    if case.exact_group_case and not group_matches:
        errors.append("exact_lifecycle_group_mismatch")
    if case.fail_closed_expectation is not None and not fail_closed:
        errors.append("ambiguous_case_not_fail_closed")
    if not lifecycle_matches:
        errors.append("lifecycle_limit_mismatch")
    if not abbreviated_day_matches:
        errors.append("abbreviated_shared_month_day_expectation_mismatch")
    if not raw_fallback_matches:
        errors.append("raw_abbreviated_tail_fallback_mismatch")
    if not structural_authority_safe:
        errors.append("unsafe_structural_authority")
    return _CaseOutcome(
        case=case,
        analysis=analysis,
        plan=plan,
        candidates=candidates,
        projection=projection,
        selected_artifact_count=len(projection.artifacts),
        group_matches=group_matches,
        fail_closed=fail_closed,
        lifecycle_matches=lifecycle_matches,
        abbreviated_day_matches=abbreviated_day_matches,
        raw_fallback_matches=raw_fallback_matches,
        structural_authority_safe=structural_authority_safe,
        errors=tuple(errors),
    )


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": numerator / denominator if denominator else None,
    }


def evaluate_public_v20() -> dict[str, Any]:
    """Return a content-free aggregate report over checked-in public text."""

    outcomes = tuple(_evaluate_case(case) for case in PUBLIC_GMAIL_TEMPORAL_V20_CASES)
    positives = tuple(item for item in outcomes if item.case.positive_candidate)
    negatives = tuple(item for item in outcomes if item.case.selected_negative)
    matched_non_advertising_negatives = tuple(
        item for item in negatives if not item.case.advertising_negative
    )
    exact_groups = tuple(item for item in outcomes if item.case.exact_group_case)
    ambiguous = tuple(
        item for item in outcomes if item.case.fail_closed_expectation is not None
    )
    lifecycle_limits = tuple(
        item for item in outcomes if item.case.expected_artifact_lifecycles
    )
    abbreviated_day_cases = tuple(
        item for item in outcomes if item.case.abbreviated_day_expectation is not None
    )
    abbreviated_day_positive_cases = tuple(
        item
        for item in abbreviated_day_cases
        if item.case.abbreviated_day_expectation in {"resolved", "unresolved"}
    )
    abbreviated_day_negative_cases = tuple(
        item
        for item in abbreviated_day_cases
        if item.case.abbreviated_day_expectation == "none"
    )
    abbreviated_day_invalid_cases = tuple(
        item
        for item in abbreviated_day_cases
        if item.case.abbreviated_day_expectation == "unresolved"
    )
    raw_fallback_cases = tuple(
        item
        for item in outcomes
        if item.case.raw_fallback_retained_expression_count is not None
    )
    raw_source_guard_cases = tuple(
        item for item in outcomes if item.case.raw_source_guard_case
    )
    error_rows = tuple(item for item in outcomes if item.errors)
    negative_candidate_bearing = sum(bool(item.candidates) for item in negatives)
    fixture_selected_negative_artifacts = sum(
        bool(item.projection.artifacts) for item in negatives
    )
    report = {
        "version": REPORT_VERSION,
        "scope": REPORT_SCOPE,
        "fixture_version": PUBLIC_GMAIL_TEMPORAL_V20_FIXTURE_VERSION,
        "case_count": len(outcomes),
        "metrics": {
            "positive_candidate_bearing_recall": _rate(
                sum(bool(item.candidates) for item in positives),
                len(positives),
            ),
            "matched_non_advertising_negative_candidate_free": {
                "candidate_free_count": sum(
                    not item.candidates for item in matched_non_advertising_negatives
                ),
                "candidate_bearing_count": sum(
                    bool(item.candidates) for item in matched_non_advertising_negatives
                ),
                "denominator": len(matched_non_advertising_negatives),
                "rate": (
                    sum(
                        not item.candidates
                        for item in matched_non_advertising_negatives
                    )
                    / len(matched_non_advertising_negatives)
                    if matched_non_advertising_negatives
                    else None
                ),
            },
            "all_selected_negative_candidate_bearing": {
                "count": negative_candidate_bearing,
                "denominator": len(negatives),
                "rate": (
                    negative_candidate_bearing / len(negatives) if negatives else None
                ),
            },
            "fixture_selected_negative_artifacts_not_semantic_precision": {
                "count": fixture_selected_negative_artifacts,
                "denominator": len(negatives),
                "rate": (
                    fixture_selected_negative_artifacts / len(negatives)
                    if negatives
                    else None
                ),
                "not_semantic_precision": True,
            },
            "exact_lifecycle_grouping_role_coverage": _rate(
                sum(item.group_matches for item in exact_groups),
                len(exact_groups),
            ),
            "ambiguous_case_fail_closed_coverage": _rate(
                sum(item.fail_closed for item in ambiguous),
                len(ambiguous),
            ),
            "cancellation_lifecycle_limit_coverage": _rate(
                sum(item.lifecycle_matches for item in lifecycle_limits),
                len(lifecycle_limits),
            ),
            "abbreviated_shared_month_day_discovery_coverage": _rate(
                sum(item.abbreviated_day_matches for item in abbreviated_day_cases),
                len(abbreviated_day_cases),
            ),
            "abbreviated_shared_month_day_positive_inventory_coverage": _rate(
                sum(
                    item.abbreviated_day_matches
                    for item in abbreviated_day_positive_cases
                ),
                len(abbreviated_day_positive_cases),
            ),
            "abbreviated_shared_month_day_negative_exclusion_coverage": _rate(
                sum(
                    item.abbreviated_day_matches
                    for item in abbreviated_day_negative_cases
                ),
                len(abbreviated_day_negative_cases),
            ),
            "abbreviated_shared_month_day_invalid_date_inventory_coverage": _rate(
                sum(
                    item.abbreviated_day_matches
                    for item in abbreviated_day_invalid_cases
                ),
                len(abbreviated_day_invalid_cases),
            ),
            "raw_abbreviated_tail_fallback_coverage": _rate(
                sum(item.raw_fallback_matches for item in raw_fallback_cases),
                len(raw_fallback_cases),
            ),
            "raw_abbreviated_source_guard_coverage": _rate(
                sum(
                    item.group_matches and item.fail_closed
                    for item in raw_source_guard_cases
                ),
                len(raw_source_guard_cases),
            ),
            "critical_structural_errors": {
                "count": sum(len(item.errors) for item in error_rows),
                "case_count": len(error_rows),
                "case_ids": [item.case.case_id for item in error_rows],
                "error_codes": sorted(
                    {error for item in error_rows for error in item.errors}
                ),
            },
        },
        "coverage": {
            "expressions": sum(len(item.analysis.expressions) for item in outcomes),
            "selector_batches": sum(len(item.plan.batches) for item in outcomes),
            "frontier_candidates": sum(len(item.candidates) for item in outcomes),
            "fixture_selected_artifacts": sum(
                item.selected_artifact_count for item in outcomes
            ),
            "review_groups": sum(len(item.projection.groups) for item in outcomes),
        },
        "claims": {
            "public_synthetic_text_only": True,
            "private_email_records": 0,
            "external_model_calls": 0,
            "network_calls": 0,
            "persistence_writes": 0,
            "retrieval_measured": False,
            "promoted_semantic_precision_measured": False,
            "fixture_verdicts_are_model_judgments": False,
            "deterministic_negative_candidate_metrics_only": True,
            "abbreviated_parser_guards_measured_separately_from_semantic_negatives": True,
            "fixture_selected_negative_artifact_metric_is_semantic_precision": False,
            "grouped_metadata_counted_as_promoted_semantic_precision": False,
            "source_text_emitted": False,
        },
        "gate_passed": not error_rows,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()
    report = evaluate_public_v20()
    print(
        json.dumps(
            report,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
            sort_keys=True,
        )
    )
    return 0 if report["gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
