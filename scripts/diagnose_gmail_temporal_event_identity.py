from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

from pkm_brain.gmail_temporal_batching import (
    GmailTemporalBatchPlan,
    plan_gmail_temporal_selector_batches,
)
from pkm_brain.gmail_temporal_event_identity import (
    GmailTemporalEventIdentityError,
    GmailTemporalEventIdentityPlan,
    GmailTemporalEventIdentityResolution,
    bind_gmail_temporal_event_identity_resolution,
    make_gmail_temporal_event_identity_verdict_set,
    plan_gmail_temporal_event_identity,
    resolve_gmail_temporal_event_identity,
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
from pkm_brain.gmail_temporal_persistence import (
    GmailTemporalSourceLocator,
    gmail_temporal_message_scope_key,
)
from pkm_brain.gmail_temporal_review import (
    GmailTemporalReviewBatchResult,
    GmailTemporalReviewProjection,
    canonical_gmail_temporal_review_projection_bytes,
    project_gmail_temporal_review,
)
from pkm_brain.gmail_temporal_thread_lifecycle import (
    GmailTemporalThreadMessageAuthority,
    GmailTemporalThreadMessageReview,
    GmailTemporalThreadSnapshotAuthority,
    gmail_temporal_source_bound_event_identity_key,
    gmail_temporal_source_bound_self_provenance,
    project_gmail_temporal_thread_lifecycle,
)


DIAGNOSTIC_VERSION = "gmail_temporal_event_identity_structural_diagnostic_v1"
SCOPE = "structural_event_identity_addressability_not_semantic_recall"
_FIXTURE_MODULE_NAME = "_pkm_brain_public_gmail_temporal_synthetic_fixtures"
_FIXTURE_PATH = Path(__file__).with_name("build_gmail_temporal_synthetic_benchmark.py")
_ACCOUNT = "public-synthetic@example.test"
_PIPELINE_SCOPE = "gmail_temporal_review_v1"

IdentityRunKind = Literal["none", "same_event", "different_event", "uncertain"]
CandidateSelector = Callable[
    [tuple[GmailTemporalVerificationCandidate, ...]],
    Mapping[str, str],
]


@dataclass(frozen=True)
class _FixtureMessage:
    sample_id: str
    selector: CandidateSelector


@dataclass(frozen=True)
class _Scenario:
    name: str
    messages: tuple[_FixtureMessage, ...]
    identity_run_kind: IdentityRunKind


@dataclass(frozen=True)
class _ScenarioExecution:
    result: dict[str, Any]
    plan: GmailTemporalEventIdentityPlan
    resolution: GmailTemporalEventIdentityResolution
    projection_artifact_count: int
    excluded_artifact_count: int
    lifecycle_event_keys: tuple[str, ...]


def _fixture_cases() -> dict[str, Any]:
    """Load the checked-in public synthetic corpus without copying its text."""

    module = sys.modules.get(_FIXTURE_MODULE_NAME)
    if module is None:
        spec = importlib.util.spec_from_file_location(
            _FIXTURE_MODULE_NAME,
            _FIXTURE_PATH,
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("public synthetic fixture module cannot be loaded")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_FIXTURE_MODULE_NAME] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(_FIXTURE_MODULE_NAME, None)
            raise
    if not isinstance(module, ModuleType):
        raise RuntimeError("public synthetic fixture module is invalid")
    cases = getattr(module, "CASES", ())
    by_id = {item.sample_id: item for item in cases}
    if len(by_id) != len(cases):
        raise RuntimeError("public synthetic fixture ids are duplicated")
    return by_id


def _selector(
    *,
    lifecycles: frozenset[str] = frozenset(),
    relations: frozenset[str] = frozenset(),
) -> CandidateSelector:
    """Choose one deterministic fixture-oracle candidate per expression."""

    if not lifecycles and not relations:
        raise ValueError("at least one candidate selector dimension is required")

    def select(
        candidates: tuple[GmailTemporalVerificationCandidate, ...],
    ) -> Mapping[str, str]:
        selected_by_expression: dict[str, GmailTemporalVerificationCandidate] = {}
        for candidate in candidates:
            if (
                candidate.lifecycle not in lifecycles
                and candidate.relation not in relations
            ):
                continue
            current = selected_by_expression.get(candidate.expression_id)
            if current is None or _candidate_rank(candidate) < _candidate_rank(current):
                selected_by_expression[candidate.expression_id] = candidate
        return {
            candidate.candidate_id: "supported"
            for candidate in selected_by_expression.values()
        }

    return select


def _candidate_rank(
    candidate: GmailTemporalVerificationCandidate,
) -> tuple[bool, int, int, str]:
    return (
        candidate.requires_defer,
        len(candidate.blockers),
        len(candidate.risk_features),
        candidate.candidate_id,
    )


def _page_verdicts(
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


def _projection(
    *,
    case: Any,
    selector: CandidateSelector,
    chunk_id: str,
) -> tuple[GmailTemporalReviewProjection, TemporalLeadAnalysis]:
    analysis: TemporalLeadAnalysis = analyze_gmail_temporal_leads(
        text=case.text,
        message_internal_at=datetime.fromisoformat(case.message_internal_at).astimezone(
            timezone.utc
        ),
        fact_admitted=bool(case.expected_material),
        temporal_review_rescue=not bool(case.expected_material),
        chunk_id=chunk_id,
    )
    batch_plan: GmailTemporalBatchPlan = plan_gmail_temporal_selector_batches(
        text=case.text,
        analysis=analysis,
    )
    results: list[GmailTemporalReviewBatchResult] = []
    selected_candidate_count = 0
    for batch in batch_plan.batches:
        frontier = build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        )
        page_plan = plan_gmail_temporal_candidate_pages(
            analysis=analysis,
            batch=batch,
        )
        selected = selector(frontier.candidates)
        selected_candidate_count += len(selected)
        verdict_rows = _page_verdicts(page_plan, selected)
        results.append(
            GmailTemporalReviewBatchResult(
                batch=batch,
                page_plan=page_plan,
                ensemble=validate_gmail_temporal_candidate_ensemble_verdict_set(
                    analysis=analysis,
                    batch=batch,
                    plan=page_plan,
                    runs=(verdict_rows, verdict_rows, verdict_rows),
                ),
            )
        )
    if selected_candidate_count == 0:
        raise RuntimeError(f"fixture selector did not match {case.sample_id}")
    return (
        project_gmail_temporal_review(
            text=case.text,
            analysis=analysis,
            batch_plan=batch_plan,
            batch_results=tuple(results),
        ),
        analysis,
    )


def _thread_inputs(
    scenario: _Scenario,
    fixture_cases: Mapping[str, Any],
) -> tuple[
    GmailTemporalThreadSnapshotAuthority,
    tuple[GmailTemporalThreadMessageReview, ...],
    tuple[TemporalLeadAnalysis, ...],
]:
    document_hash = hashlib.sha256(
        f"public-synthetic-document:{scenario.name}".encode()
    ).hexdigest()
    source_revision = hashlib.sha256(
        f"public-synthetic-revision:{scenario.name}".encode()
    ).hexdigest()
    authorities: list[GmailTemporalThreadMessageAuthority] = []
    reviews: list[GmailTemporalThreadMessageReview] = []
    analyses: list[TemporalLeadAnalysis] = []
    for order, specification in enumerate(scenario.messages, start=1):
        case = fixture_cases[specification.sample_id]
        thread_id = f"public-synthetic-{scenario.name}"
        gmail_message_id = (
            f"public-synthetic-{scenario.name}-{specification.sample_id}-{order}"
        )
        projection, analysis = _projection(
            case=case,
            selector=specification.selector,
            chunk_id=gmail_temporal_message_scope_key(
                gmail_account_key=_ACCOUNT,
                gmail_thread_id=thread_id,
                gmail_message_id=gmail_message_id,
            ),
        )
        start_offset = (order - 1) * 10_000
        source = GmailTemporalSourceLocator(
            document_id=f"public-synthetic-{scenario.name}",
            document_content_hash=document_hash,
            gmail_account_key=_ACCOUNT,
            gmail_thread_id=thread_id,
            gmail_source_revision=source_revision,
            gmail_message_id=gmail_message_id,
            message_internal_at=case.message_internal_at,
            message_start_offset=start_offset,
            message_end_offset=start_offset + len(case.text),
            source_sha256=projection.source_sha256,
        )
        review_run_id = f"gtrr_public_{scenario.name}_{order}"
        authorities.append(
            GmailTemporalThreadMessageAuthority(
                version="gmail_temporal_thread_message_authority_v2",
                source=source,
                pipeline_scope=_PIPELINE_SCOPE,
                current_review_run_id=review_run_id,
                current_head_generation=order,
                current_analysis_fingerprint=projection.analysis_fingerprint,
                current_projection_fingerprint=projection.projection_fingerprint,
                current_projection_sha256=hashlib.sha256(
                    canonical_gmail_temporal_review_projection_bytes(projection)
                ).hexdigest(),
            )
        )
        reviews.append(
            GmailTemporalThreadMessageReview(
                version="gmail_temporal_thread_message_review_v1",
                source=source,
                review_run_id=review_run_id,
                projection=projection,
            )
        )
        analyses.append(analysis)
    return (
        GmailTemporalThreadSnapshotAuthority(
            version="gmail_temporal_thread_snapshot_authority_v2",
            messages=tuple(authorities),
        ),
        tuple(reviews),
        tuple(analyses),
    )


def _identity_verdict_runs(
    plan: GmailTemporalEventIdentityPlan,
    run_kind: IdentityRunKind,
) -> tuple[dict[str, str], ...]:
    if not plan.pairs:
        if run_kind != "none":
            raise RuntimeError("a zero-pair scenario must use the zero-call path")
        return ()
    if run_kind == "same_event":
        values = ("same_event", "same_event", "same_event")
    elif run_kind == "different_event":
        values = ("different_event", "different_event", "different_event")
    elif run_kind == "uncertain":
        values = ("same_event", "same_event", "different_event")
    else:
        raise RuntimeError("a pair-bearing scenario requires fixture verdicts")
    return tuple({pair.pair_id: value for pair in plan.pairs} for value in values)


def _execute_scenario(
    scenario: _Scenario,
    fixture_cases: Mapping[str, Any],
) -> _ScenarioExecution:
    snapshot_authority, messages, analysis_authorities = _thread_inputs(
        scenario,
        fixture_cases,
    )
    plan = plan_gmail_temporal_event_identity(
        snapshot_authority=snapshot_authority,
        messages=messages,
        analysis_authorities=analysis_authorities,
    )
    run_maps = _identity_verdict_runs(plan, scenario.identity_run_kind)
    verdict_sets = tuple(
        make_gmail_temporal_event_identity_verdict_set(
            plan=plan,
            run_ordinal=ordinal,
            invocation_id=f"public-synthetic-fixture-oracle-{scenario.name}-{ordinal}",
            response_sha256=hashlib.sha256(
                f"public-synthetic-response:{scenario.name}:{ordinal}".encode()
            ).hexdigest(),
            verdicts=verdicts,  # type: ignore[arg-type]
        )
        for ordinal, verdicts in enumerate(run_maps, start=1)
    )
    resolution = resolve_gmail_temporal_event_identity(
        plan=plan,
        verdict_sets=verdict_sets,
    )
    bound_messages = bind_gmail_temporal_event_identity_resolution(
        snapshot_authority=snapshot_authority,
        messages=messages,
        analysis_authorities=analysis_authorities,
        plan=plan,
        resolution=resolution,
    )
    lifecycle = project_gmail_temporal_thread_lifecycle(
        snapshot_authority=snapshot_authority,
        messages=bound_messages,
        event_identity_analysis_authorities=analysis_authorities,
        event_identity_plan=plan,
        event_identity_resolution=resolution,
    )

    eligible_artifact_ids = {item.artifact_id for item in plan.units}
    artifacts = tuple(
        artifact for message in messages for artifact in message.projection.artifacts
    )
    excluded_artifacts = tuple(
        artifact
        for artifact in artifacts
        if artifact.artifact_id not in eligible_artifact_ids
    )
    unit_message_orders = {item.message_order for item in plan.units}
    assertion_verification_by_key: dict[str, set[str]] = {}
    for assertion in resolution.assertions:
        assertion_verification_by_key.setdefault(
            assertion.event_identity_key,
            set(),
        ).add(assertion.verification)
    external_clusters = tuple(
        cluster
        for cluster in resolution.clusters
        if assertion_verification_by_key.get(cluster.event_identity_key)
        == {"external_verified"}
    )
    source_self_clusters = tuple(
        cluster
        for cluster in resolution.clusters
        if assertion_verification_by_key.get(cluster.event_identity_key)
        == {"source_bound_self_identity"}
    )
    result = {
        "scenario": scenario.name,
        "public_fixture_ids": [item.sample_id for item in scenario.messages],
        "messages": len(messages),
        "eligible_event_bearing_messages": len(unit_message_orders),
        "review_artifacts": len(artifacts),
        "eligible_event_identity_units": len(plan.units),
        "one_unit_plan": len(plan.units) == 1,
        "identity_pairs": len(plan.pairs),
        "zero_call_plan": not plan.pairs,
        "fixture_oracle_verdict_sets": len(verdict_sets),
        "resolution_clusters": len(resolution.clusters),
        "canonical_cross_unit_clusters": len(external_clusters),
        "source_self_clusters": len(source_self_clusters),
        "provisional_source_self_views": len(source_self_clusters),
        "source_self_assertions": sum(
            item.verification == "source_bound_self_identity"
            for item in resolution.assertions
        ),
        "external_consensus_clusters": len(external_clusters),
        "external_consensus_assertions": sum(
            item.verification == "external_verified" for item in resolution.assertions
        ),
        "identity_reviews": len(resolution.reviews),
        "excluded_non_event_artifacts": len(excluded_artifacts),
        "lifecycle_event_views": len(lifecycle.events),
        "unresolved_lifecycle_alternatives": len(lifecycle.unresolved_alternatives),
    }
    return _ScenarioExecution(
        result=result,
        plan=plan,
        resolution=resolution,
        projection_artifact_count=len(artifacts),
        excluded_artifact_count=len(excluded_artifacts),
        lifecycle_event_keys=tuple(
            item.event_identity_key for item in lifecycle.events
        ),
    )


def _named_values(value: Any, name: str) -> tuple[Any, ...]:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Mapping):
        values = [value[name]] if name in value else []
        return tuple(values) + tuple(
            nested for item in value.values() for nested in _named_values(item, name)
        )
    if isinstance(value, (tuple, list)):
        return tuple(nested for item in value for nested in _named_values(item, name))
    return ()


def _external_three_set_gate(execution: _ScenarioExecution) -> bool:
    plan = execution.plan
    if not plan.pairs:
        return False
    verdicts = {item.pair_id: "same_event" for item in plan.pairs}
    two_sets = tuple(
        make_gmail_temporal_event_identity_verdict_set(
            plan=plan,
            run_ordinal=ordinal,
            invocation_id=f"public-synthetic-gate-probe-{ordinal}",
            response_sha256=str(ordinal) * 64,
            verdicts=verdicts,
        )
        for ordinal in (1, 2)
    )
    try:
        resolve_gmail_temporal_event_identity(
            plan=plan,
            verdict_sets=two_sets,
        )
    except GmailTemporalEventIdentityError as exc:
        return "exactly three verdict sets" in str(exc)
    return False


def diagnose() -> dict[str, Any]:
    """Run a public, deterministic structural diagnostic with no model calls."""

    fixture_cases = _fixture_cases()
    scheduled = _selector(lifecycles=frozenset({"scheduled"}))
    # The public reschedule fixture intentionally leaves endpoint lifecycle roles
    # unresolved in the deterministic frontier. Its two occurrence candidates
    # still form the complete structural reschedule group exercised here.
    rescheduled = _selector(relations=frozenset({"occurrence"}))
    deadline = _selector(relations=frozenset({"deadline"}))
    scenarios = (
        _Scenario(
            name="zero_call_singleton_schedule",
            messages=(_FixtureMessage("syn_clear_02", scheduled),),
            identity_run_kind="none",
        ),
        _Scenario(
            name="external_consensus_reschedule",
            messages=(_FixtureMessage("syn_lifecycle_03", rescheduled),),
            identity_run_kind="same_event",
        ),
        _Scenario(
            name="distinct_event_self_views",
            messages=(_FixtureMessage("syn_clear_07", scheduled),),
            identity_run_kind="different_event",
        ),
        _Scenario(
            name="uncertain_pair_self_views",
            messages=(_FixtureMessage("syn_clear_07", scheduled),),
            identity_run_kind="uncertain",
        ),
        _Scenario(
            name="non_event_deadline_exclusion",
            messages=(
                _FixtureMessage("syn_clear_03", deadline),
                _FixtureMessage("syn_lifecycle_04", deadline),
            ),
            identity_run_kind="none",
        ),
    )
    executions = tuple(
        _execute_scenario(scenario, fixture_cases) for scenario in scenarios
    )
    by_name = {item.result["scenario"]: item for item in executions}
    singleton = by_name["zero_call_singleton_schedule"]
    external = by_name["external_consensus_reschedule"]
    distinct = by_name["distinct_event_self_views"]
    uncertain = by_name["uncertain_pair_self_views"]
    excluded = by_name["non_event_deadline_exclusion"]

    source_self_assertions = tuple(
        assertion
        for execution in executions
        for assertion in execution.resolution.assertions
        if assertion.verification == "source_bound_self_identity"
    )
    safety_invariants = {
        "all_identity_outputs_non_routable": all(
            value is False
            for execution in executions
            for value in _named_values(
                (execution.plan, execution.resolution),
                "routable",
            )
        ),
        "all_identity_outputs_non_authorizing": all(
            value is False
            for execution in executions
            for value in _named_values(
                (execution.plan, execution.resolution),
                "candidate_authorization",
            )
        ),
        "all_identity_outputs_deferred": all(
            value is True
            for execution in executions
            for value in _named_values(
                (execution.plan, execution.resolution),
                "requires_defer",
            )
        ),
        "source_self_assertions_are_unit_bound": all(
            assertion.event_identity_key
            == gmail_temporal_source_bound_event_identity_key(assertion.unit_id)
            and assertion.provenance_ref
            == gmail_temporal_source_bound_self_provenance(assertion.unit_id)
            for assertion in source_self_assertions
        ),
        "zero_call_singleton_is_addressable": (
            singleton.result["eligible_event_identity_units"] == 1
            and singleton.result["fixture_oracle_verdict_sets"] == 0
            and singleton.result["source_self_assertions"] == 1
            and singleton.result["lifecycle_event_views"] == 1
        ),
        "external_cluster_requires_three_verdict_sets": _external_three_set_gate(
            external
        ),
        "external_consensus_forms_one_cross_unit_cluster": (
            external.result["eligible_event_identity_units"] == 2
            and external.result["canonical_cross_unit_clusters"] == 1
            and external.result["external_consensus_clusters"] == 1
            and len(external.resolution.clusters[0].unit_ids) == 2
        ),
        "different_events_do_not_merge": (
            distinct.result["eligible_event_identity_units"] == 2
            and distinct.result["source_self_clusters"] == 2
            and len({item.event_identity_key for item in distinct.resolution.clusters})
            == 2
        ),
        "uncertain_pair_has_no_cross_unit_authority": (
            {item.consensus for item in uncertain.resolution.pair_consensus}
            == {"uncertain"}
            and uncertain.result["canonical_cross_unit_clusters"] == 0
            and uncertain.result["external_consensus_clusters"] == 0
            and all(len(item.unit_ids) == 1 for item in uncertain.resolution.clusters)
        ),
        "uncertain_pair_has_no_cross_unit_lifecycle_mutation": (
            len(uncertain.lifecycle_event_keys) == 2
            and len(set(uncertain.lifecycle_event_keys)) == 2
        ),
        "non_event_artifacts_are_excluded": (
            excluded.projection_artifact_count == 2
            and excluded.excluded_artifact_count == 2
            and excluded.result["eligible_event_identity_units"] == 0
            and excluded.result["lifecycle_event_views"] == 0
        ),
    }
    if not all(safety_invariants.values()):
        failed = sorted(key for key, passed in safety_invariants.items() if not passed)
        raise RuntimeError(f"structural safety invariant failed: {failed}")

    aggregate_fields = (
        "messages",
        "eligible_event_bearing_messages",
        "review_artifacts",
        "eligible_event_identity_units",
        "source_self_assertions",
        "resolution_clusters",
        "canonical_cross_unit_clusters",
        "provisional_source_self_views",
        "external_consensus_clusters",
        "excluded_non_event_artifacts",
        "lifecycle_event_views",
    )
    aggregate = {
        field: sum(int(item.result[field]) for item in executions)
        for field in aggregate_fields
    }
    aggregate.update(
        {
            "plans": len(executions),
            "one_unit_plans": sum(
                bool(item.result["one_unit_plan"]) for item in executions
            ),
            "zero_call_plans": sum(
                bool(item.result["zero_call_plan"]) for item in executions
            ),
            "fixture_oracle_verdict_sets": sum(
                int(item.result["fixture_oracle_verdict_sets"]) for item in executions
            ),
            "external_model_calls": 0,
        }
    )
    return {
        "version": DIAGNOSTIC_VERSION,
        "scope": SCOPE,
        "semantic_recall_measured": False,
        "semantic_precision_measured": False,
        "private_gmail_used": False,
        "external_model_calls": 0,
        "verdict_source": "deterministic_public_fixture_oracle",
        "interpretation": {
            "resolution_clusters": (
                "validator-accepted deterministic structural groupings; source-self "
                "clusters remain provisional observations"
            ),
            "canonical_cross_unit_clusters": (
                "cross-unit identity clusters accepted through the external-consensus "
                "code path; provisional source-self views are never counted here"
            ),
            "external_consensus_clusters": (
                "the external-verification code path exercised with three "
                "deterministic public-fixture verdict sets, not model calls"
            ),
            "source_self_assertions": (
                "one source event unit bound only to its own deterministic key"
            ),
        },
        "aggregate": aggregate,
        "scenarios": [item.result for item in executions],
        "safety_invariants": safety_invariants,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Measure structural Gmail event-identity addressability on public "
            "synthetic fixtures; this does not measure semantic recall."
        )
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="indent the JSON report",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            diagnose(),
            indent=2 if args.pretty else None,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
