from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from typing import Callable

import pytest

from pkm_brain.gmail_temporal_batching import (
    GmailTemporalBatchPlan,
    GmailTemporalSelectorBatch,
    plan_gmail_temporal_selector_batches,
)
from pkm_brain.gmail_temporal_frontier import (
    GmailTemporalCandidateEnsembleVerdictSet,
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
from pkm_brain.gmail_temporal_review import (
    GmailTemporalReviewBatchResult,
    GmailTemporalReviewClusterReview,
    GmailTemporalReviewError,
    GmailTemporalReviewGroup,
    GmailTemporalReviewGroupMember,
    GmailTemporalReviewHypothesis,
    GmailTemporalReviewProjection,
    canonical_gmail_temporal_review_projection_bytes,
    gmail_temporal_review_projection_payload,
    project_gmail_temporal_review,
)


VerdictSelector = Callable[
    [str, TemporalLeadAnalysis, tuple[GmailTemporalVerificationCandidate, ...]],
    dict[str, str],
]


def _rows(
    page_plan: GmailTemporalCandidatePagePlan,
    overrides: dict[str, str],
) -> tuple[GmailTemporalCandidatePageVerdicts, ...]:
    return tuple(
        GmailTemporalCandidatePageVerdicts(
            frontier_fingerprint=page_plan.frontier_fingerprint,
            page_fingerprint=page.page_fingerprint,
            verdicts=tuple(
                GmailTemporalCandidateVerdict(
                    candidate_id=candidate_id,
                    verdict=overrides.get(candidate_id, "unsupported"),  # type: ignore[arg-type]
                )
                for cluster in page.clusters
                for candidate_id in cluster.candidate_ids
            ),
        )
        for page in page_plan.pages
    )


def _identical_ensemble(
    *,
    analysis: TemporalLeadAnalysis,
    batch: GmailTemporalSelectorBatch,
    page_plan: GmailTemporalCandidatePagePlan,
    overrides: dict[str, str],
) -> GmailTemporalCandidateEnsembleVerdictSet:
    rows = _rows(page_plan, overrides)
    return validate_gmail_temporal_candidate_ensemble_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        runs=(rows, rows, rows),
    )


def _fixture(
    text: str,
    selector: VerdictSelector,
    *,
    chunk_id: str = "review-test",
    component_evidence_fingerprints: tuple[str, ...] = (
        "a" * 64,
        "b" * 64,
        "c" * 64,
    ),
) -> tuple[
    TemporalLeadAnalysis,
    GmailTemporalBatchPlan,
    tuple[GmailTemporalReviewBatchResult, ...],
    GmailTemporalReviewProjection,
]:
    analysis = analyze_gmail_temporal_leads(
        text=text,
        message_internal_at="2027-08-10T09:00:00-07:00",
        fact_admitted=True,
        chunk_id=chunk_id,
    )
    batch_plan = plan_gmail_temporal_selector_batches(
        text=text,
        analysis=analysis,
    )
    results: list[GmailTemporalReviewBatchResult] = []
    for batch in batch_plan.batches:
        frontier = build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        )
        page_plan = plan_gmail_temporal_candidate_pages(
            analysis=analysis,
            batch=batch,
        )
        expression_id = batch.expressions[0].expression_id
        overrides = selector(expression_id, analysis, frontier.candidates)
        results.append(
            GmailTemporalReviewBatchResult(
                batch=batch,
                page_plan=page_plan,
                ensemble=_identical_ensemble(
                    analysis=analysis,
                    batch=batch,
                    page_plan=page_plan,
                    overrides=overrides,
                ),
                component_evidence_fingerprints=component_evidence_fingerprints,
            )
        )
    result_tuple = tuple(results)
    projection = project_gmail_temporal_review(
        text=text,
        analysis=analysis,
        batch_plan=batch_plan,
        batch_results=result_tuple,
    )
    return analysis, batch_plan, result_tuple, projection


def _select_lifecycle(value: str, verdict: str) -> VerdictSelector:
    def select(
        _expression_id: str,
        _analysis: TemporalLeadAnalysis,
        candidates: tuple[GmailTemporalVerificationCandidate, ...],
    ) -> dict[str, str]:
        candidate = next(item for item in candidates if item.lifecycle == value)
        return {candidate.candidate_id: verdict}

    return select


def _select_first_uncertain(
    _expression_id: str,
    _analysis: TemporalLeadAnalysis,
    candidates: tuple[GmailTemporalVerificationCandidate, ...],
) -> dict[str, str]:
    return {candidates[0].candidate_id: "uncertain"}


def _select_all_same_signature_aliases(
    _expression_id: str,
    _analysis: TemporalLeadAnalysis,
    candidates: tuple[GmailTemporalVerificationCandidate, ...],
) -> dict[str, str]:
    first = candidates[0]
    signature = (
        first.expression_id,
        first.relation,
        first.kind,
        first.lifecycle,
        first.normalized_value,
    )
    return {
        item.candidate_id: "uncertain"
        for item in candidates
        if (
            item.expression_id,
            item.relation,
            item.kind,
            item.lifecycle,
            item.normalized_value,
        )
        == signature
    }


def _refingerprinted(
    projection: GmailTemporalReviewProjection,
) -> GmailTemporalReviewProjection:
    material = asdict(projection)
    material.pop("projection_fingerprint")
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return replace(
        projection,
        projection_fingerprint="gtrp_" + hashlib.sha256(encoded).hexdigest(),
    )


def _with_subject_type_references(
    hypothesis: GmailTemporalReviewHypothesis,
    references: tuple[tuple[str, str], ...],
) -> GmailTemporalReviewHypothesis:
    signature = (
        hypothesis.expression_id,
        hypothesis.relation,
        hypothesis.kind,
        hypothesis.lifecycle,
        hypothesis.normalized_value,
    )
    material = {
        "version": "gmail_temporal_review_hypothesis_v2",
        "signature": signature,
        "subject_type_references": references,
    }
    return replace(
        hypothesis,
        hypothesis_id="gtrh_"
        + hashlib.sha256(
            json.dumps(
                material,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
        subject_type_references=references,
    )


def test_single_projection_is_canonical_content_free_and_always_deferred() -> None:
    text = "The Super Secret Meeting is scheduled for August 14, 2027."
    analysis, _, _, projection = _fixture(
        text,
        _select_lifecycle("scheduled", "supported"),
    )

    assert len(projection.artifacts) == 1
    artifact = projection.artifacts[0]
    assert (artifact.kind, artifact.evidence_status) == (
        "supported_citation",
        "supported",
    )
    assert artifact.candidate_authorization is True
    assert artifact.hypotheses[0].candidate_requires_defer is False
    mention_types = {item.mention_id: item.mention_type for item in analysis.mentions}
    assert artifact.hypotheses[0].subject_type_references == tuple(
        (mention_id, mention_types[mention_id])
        for mention_id in artifact.hypotheses[0].subject_mention_ids
    )
    assert artifact.hypotheses[0].requires_defer is True
    assert artifact.requires_defer is True
    assert artifact.routable is False
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("single", "complete")
    assert group.members[0].role == "independent"
    assert group.candidate_authorization is False
    assert projection.independent_invocations_verified is False
    assert projection.component_evidence_fingerprints == (
        "a" * 64,
        "b" * 64,
        "c" * 64,
    )

    first = canonical_gmail_temporal_review_projection_bytes(projection)
    second = canonical_gmail_temporal_review_projection_bytes(projection)
    assert first == second
    assert b"Super Secret" not in first
    assert (
        gmail_temporal_review_projection_payload(projection)["projection_fingerprint"]
        == projection.projection_fingerprint
    )


@pytest.mark.parametrize(
    ("text", "lifecycle", "expected_type"),
    (
        ("The upload is scheduled for August 18, 2027.", "scheduled", "action"),
        (
            "The arrival was cancelled on August 18, 2027.",
            "cancelled",
            "boundary",
        ),
    ),
)
def test_non_event_temporal_artifacts_preserve_exact_subject_types(
    text: str,
    lifecycle: str,
    expected_type: str,
) -> None:
    _, _, _, projection = _fixture(
        text,
        _select_lifecycle(lifecycle, "supported"),
        chunk_id=f"review-subject-type-{expected_type}",
    )

    assert len(projection.artifacts) == 1
    hypothesis = projection.artifacts[0].hypotheses[0]
    assert hypothesis.subject_type_references == (
        (hypothesis.subject_mention_ids[0], expected_type),
    )


def test_subject_type_references_fail_closed_when_incomplete_or_mixed() -> None:
    text = "The meeting is scheduled for August 14, 2027."
    _, _, _, projection = _fixture(
        text,
        _select_lifecycle("scheduled", "supported"),
        chunk_id="review-subject-type-integrity",
    )
    artifact = projection.artifacts[0]
    hypothesis = artifact.hypotheses[0]
    subject_id = hypothesis.subject_mention_ids[0]
    invalid_references = (
        (),
        ((f"{subject_id}-mismatch", "event"),),
        tuple(sorted(((subject_id, "action"), (subject_id, "event")))),
    )

    for references in invalid_references:
        changed_hypothesis = _with_subject_type_references(hypothesis, references)
        changed_projection = _refingerprinted(
            replace(
                projection,
                projection_fingerprint="",
                artifacts=(replace(artifact, hypotheses=(changed_hypothesis,)),),
            )
        )
        with pytest.raises(GmailTemporalReviewError, match="artifact structure"):
            canonical_gmail_temporal_review_projection_bytes(changed_projection)


def test_v19_syn_ambiguous_04_preserves_alternatives_and_alias_collapse() -> None:
    text = (
        "Subject: Juniper Interview\n\n"
        "Possible dates are August 18, 2027 or August 19, 2027."
    )
    _, _, _, projection = _fixture(text, _select_all_same_signature_aliases)

    assert len(projection.artifacts) == 2
    assert all(item.kind == "uncertainty_sidecar" for item in projection.artifacts)
    assert all(len(item.hypotheses) == 1 for item in projection.artifacts)
    assert any(len(item.candidate_ids) > 1 for item in projection.artifacts), (
        "at least one sidecar should retain reducer-equivalent aliases"
    )
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("alternatives", "complete")
    assert [item.role for item in group.members] == ["alternative", "alternative"]
    assert [item.source_order for item in group.members] == [1, 2]
    assert [len(item.artifact_ids) for item in group.members] == [1, 1]
    assert group.subject_family_id is not None
    assert group.candidate_authorization is False


def test_v19_syn_lifecycle_03_orders_roles_without_rewriting_candidates() -> None:
    text = (
        "The hiring interview was rescheduled from August 20, 2027 to August 16, 2027."
    )
    _, _, _, projection = _fixture(text, _select_lifecycle("unknown", "uncertain"))

    assert len(projection.artifacts) == 2
    group = next(item for item in projection.groups if item.kind == "reschedule")
    assert group.coverage == "complete"
    assert [item.role for item in group.members] == [
        "rescheduled_old",
        "rescheduled_replacement",
    ]
    assert [item.source_order for item in group.members] == [1, 2]
    hypotheses = [item.hypotheses[0] for item in projection.artifacts]
    assert [item.normalized_value for item in hypotheses] == [
        "2027-08-20",
        "2027-08-16",
    ]
    assert all(item.lifecycle == "unknown" for item in hypotheses)
    assert all(item.candidate_requires_defer is True for item in hypotheses)
    assert all(
        (item.relation, item.kind) == ("unspecified", "unspecified")
        for item in hypotheses
    )


def test_missing_reschedule_endpoint_is_explicitly_incomplete() -> None:
    text = (
        "The hiring interview was rescheduled from August 14, 2027 to August 16, 2027."
    )

    def select(
        expression_id: str,
        analysis: TemporalLeadAnalysis,
        candidates: tuple[GmailTemporalVerificationCandidate, ...],
    ) -> dict[str, str]:
        if expression_id != analysis.expressions[0].expression_id:
            return {}
        candidate = next(item for item in candidates if item.lifecycle == "unknown")
        return {candidate.candidate_id: "uncertain"}

    _, _, _, projection = _fixture(text, select)

    assert len(projection.artifacts) == 1
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("reschedule", "incomplete")
    assert [item.state for item in group.members] == ["present", "missing"]
    assert group.members[1].reasons == ("no_review_artifact",)
    assert group.subject_family_id is None
    assert sum(len(item.artifact_ids) for item in group.members) == 1


def test_independent_subject_clauses_do_not_form_an_alternative_group() -> None:
    text = "Alpha meeting is August 14, 2027 or Beta workshop is August 16, 2027."

    def select(
        expression_id: str,
        analysis: TemporalLeadAnalysis,
        candidates: tuple[GmailTemporalVerificationCandidate, ...],
    ) -> dict[str, str]:
        expression = next(
            item for item in analysis.expressions if item.expression_id == expression_id
        )
        target = "meeting" if expression.start < text.index("Beta") else "workshop"
        candidate = next(
            item
            for item in candidates
            if text[
                next(
                    mention.start
                    for mention in analysis.mentions
                    if mention.mention_id == item.subject_mention_id
                ) : next(
                    mention.end
                    for mention in analysis.mentions
                    if mention.mention_id == item.subject_mention_id
                )
            ].casefold()
            == target
        )
        return {candidate.candidate_id: "uncertain"}

    _, _, _, projection = _fixture(text, select)

    assert len(projection.artifacts) == 2
    assert [item.kind for item in projection.groups] == ["single", "single"]
    assert all(item.coverage == "complete" for item in projection.groups)


def test_exact_alternative_frame_with_incompatible_subjects_is_conflicted() -> None:
    text = (
        "Alpha meeting and Beta workshop possible dates are "
        "August 14, 2027 or August 16, 2027."
    )

    def select(
        expression_id: str,
        analysis: TemporalLeadAnalysis,
        candidates: tuple[GmailTemporalVerificationCandidate, ...],
    ) -> dict[str, str]:
        first_expression_id = analysis.expressions[0].expression_id
        target = "meeting" if expression_id == first_expression_id else "workshop"
        mentions = {item.mention_id: item for item in analysis.mentions}
        candidate = next(
            item
            for item in candidates
            if text[
                mentions[item.subject_mention_id].start : mentions[
                    item.subject_mention_id
                ].end
            ].casefold()
            == target
        )
        return {candidate.candidate_id: "uncertain"}

    _, _, _, projection = _fixture(text, select)

    assert len(projection.artifacts) == 2
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("alternatives", "conflicted")
    assert group.subject_family_id is None
    assert group.reasons == ("incompatible_subject_alias_families",)
    assert group.candidate_authorization is False


def test_split_semantics_is_triage_only_and_gets_an_unresolved_group() -> None:
    text = "The meeting is scheduled for May 14, 2027 at 2:00 PM."
    analysis = analyze_gmail_temporal_leads(
        text=text,
        message_internal_at="2027-05-10T09:00:00-07:00",
        fact_admitted=True,
        chunk_id="split-semantics",
    )
    batch_plan = plan_gmail_temporal_selector_batches(text=text, analysis=analysis)
    batch = batch_plan.batches[0]
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )
    first = next(item for item in frontier.candidates if item.lifecycle == "none")
    second = next(item for item in frontier.candidates if item.lifecycle == "scheduled")
    run_one = _rows(page_plan, {first.candidate_id: "uncertain"})
    run_two = _rows(page_plan, {second.candidate_id: "uncertain"})
    run_three = _rows(page_plan, {})
    ensemble = validate_gmail_temporal_candidate_ensemble_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        runs=(run_one, run_two, run_three),
    )
    projection = project_gmail_temporal_review(
        text=text,
        analysis=analysis,
        batch_plan=batch_plan,
        batch_results=(
            GmailTemporalReviewBatchResult(
                batch=batch,
                page_plan=page_plan,
                ensemble=ensemble,
            ),
        ),
    )

    assert projection.artifacts == ()
    assert len(projection.cluster_reviews) == 1
    review = projection.cluster_reviews[0]
    assert review.candidate_authorization is False
    assert review.reason == "split_semantics_unresolved"
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("split_semantics", "conflicted")
    assert group.members[0].role == "unresolved"
    assert group.members[0].artifact_ids == ()
    assert group.members[0].cluster_review_ids == (review.review_id,)


def test_structural_group_exposes_split_endpoint_without_duplicate_authority() -> None:
    text = (
        "The hiring interview was rescheduled from August 14, 2027 to August 16, 2027."
    )
    analysis = analyze_gmail_temporal_leads(
        text=text,
        message_internal_at="2027-08-10T09:00:00-07:00",
        fact_admitted=True,
        chunk_id="structural-split",
    )
    batch_plan = plan_gmail_temporal_selector_batches(text=text, analysis=analysis)
    results: list[GmailTemporalReviewBatchResult] = []
    for index, batch in enumerate(batch_plan.batches):
        frontier = build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        )
        page_plan = plan_gmail_temporal_candidate_pages(
            analysis=analysis,
            batch=batch,
        )
        base = next(item for item in frontier.candidates if item.lifecycle == "none")
        unknown = next(
            item for item in frontier.candidates if item.lifecycle == "unknown"
        )
        if index == 0:
            ensemble = validate_gmail_temporal_candidate_ensemble_verdict_set(
                analysis=analysis,
                batch=batch,
                plan=page_plan,
                runs=(
                    _rows(page_plan, {base.candidate_id: "uncertain"}),
                    _rows(page_plan, {unknown.candidate_id: "uncertain"}),
                    _rows(page_plan, {}),
                ),
            )
        else:
            ensemble = _identical_ensemble(
                analysis=analysis,
                batch=batch,
                page_plan=page_plan,
                overrides={unknown.candidate_id: "uncertain"},
            )
        results.append(
            GmailTemporalReviewBatchResult(
                batch=batch,
                page_plan=page_plan,
                ensemble=ensemble,
            )
        )

    projection = project_gmail_temporal_review(
        text=text,
        analysis=analysis,
        batch_plan=batch_plan,
        batch_results=tuple(results),
    )

    assert len(projection.artifacts) == 1
    assert len(projection.cluster_reviews) == 1
    structural = next(item for item in projection.groups if item.kind == "reschedule")
    split = next(item for item in projection.groups if item.kind == "split_semantics")
    assert structural.coverage == "incomplete"
    assert structural.members[0].state == "missing"
    assert structural.members[0].reasons == ("split_semantics_unresolved",)
    assert structural.members[0].cluster_review_ids == (
        projection.cluster_reviews[0].review_id,
    )
    assert structural.members[1].artifact_ids == (projection.artifacts[0].artifact_id,)
    assert split.candidate_authorization is False
    assert split.members[0].artifact_ids == ()
    assert projection.cluster_reviews[0].candidate_authorization is False
    assert projection.artifacts[0].candidate_authorization is True
    assert (
        sum(
            projection.artifacts[0].artifact_id in member.artifact_ids
            for group in projection.groups
            for member in group.members
        )
        == 1
    )


def test_split_group_cannot_duplicate_an_artifact_reference() -> None:
    text = "The meeting is scheduled for May 14, 2027 at 2:00 PM."
    analysis = analyze_gmail_temporal_leads(
        text=text,
        message_internal_at="2027-05-10T09:00:00-07:00",
        fact_admitted=True,
        chunk_id="split-duplicate-artifact",
    )
    batch_plan = plan_gmail_temporal_selector_batches(text=text, analysis=analysis)
    batch = batch_plan.batches[0]
    frontier = build_gmail_temporal_candidate_frontier(
        analysis=analysis,
        batch=batch,
    )
    page_plan = plan_gmail_temporal_candidate_pages(
        analysis=analysis,
        batch=batch,
    )
    scheduled = next(
        item for item in frontier.candidates if item.lifecycle == "scheduled"
    )
    projection = project_gmail_temporal_review(
        text=text,
        analysis=analysis,
        batch_plan=batch_plan,
        batch_results=(
            GmailTemporalReviewBatchResult(
                batch=batch,
                page_plan=page_plan,
                ensemble=_identical_ensemble(
                    analysis=analysis,
                    batch=batch,
                    page_plan=page_plan,
                    overrides={scheduled.candidate_id: "supported"},
                ),
            ),
        ),
    )
    artifact = projection.artifacts[0]
    single = projection.groups[0]
    split_review = GmailTemporalReviewClusterReview(
        version="gmail_temporal_review_cluster_review_v1",
        review_id="cluster_review:adversarial-split",
        batch_fingerprint=artifact.batch_fingerprint,
        frontier_fingerprint=artifact.frontier_fingerprint,
        cluster_id="adversarial-split",
        expression_id=artifact.hypotheses[0].expression_id,
        candidate_ids=("adversarial-candidate",),
        reason="split_semantics_unresolved",
    )
    split_group = replace(
        single,
        group_id="adversarial-split-group",
        kind="split_semantics",
        coverage="conflicted",
        subject_family_id=None,
        members=(
            replace(
                single.members[0],
                member_id="gtrgm_"
                + hashlib.sha256(
                    json.dumps(
                        {
                            "version": "gmail_temporal_review_group_member_v1",
                            "group_id": "adversarial-split-group",
                            "expression_id": artifact.hypotheses[0].expression_id,
                            "role": "unresolved",
                            "source_order": None,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest(),
                role="unresolved",
                state="conflicted",
                artifact_ids=(artifact.artifact_id,),
                cluster_review_ids=(split_review.review_id,),
                subject_family_ids=(),
                reasons=("split_semantics_unresolved",),
            ),
        ),
        reasons=("split_semantics_unresolved",),
    )
    tampered = _refingerprinted(
        replace(
            projection,
            projection_fingerprint="",
            cluster_reviews=(split_review,),
            groups=(single, split_group),
        )
    )

    with pytest.raises(GmailTemporalReviewError, match="group structure"):
        canonical_gmail_temporal_review_projection_bytes(tampered)


def test_complete_member_cannot_hide_a_split_semantic_review() -> None:
    text = (
        "The hiring interview was rescheduled from August 14, 2027 to August 16, 2027."
    )
    analysis = analyze_gmail_temporal_leads(
        text=text,
        message_internal_at="2027-08-10T09:00:00-07:00",
        fact_admitted=True,
        chunk_id="complete-with-split-review",
    )
    batch_plan = plan_gmail_temporal_selector_batches(text=text, analysis=analysis)
    results: list[GmailTemporalReviewBatchResult] = []
    for batch in batch_plan.batches:
        frontier = build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        )
        page_plan = plan_gmail_temporal_candidate_pages(
            analysis=analysis,
            batch=batch,
        )
        unknown = next(
            item for item in frontier.candidates if item.lifecycle == "unknown"
        )
        results.append(
            GmailTemporalReviewBatchResult(
                batch=batch,
                page_plan=page_plan,
                ensemble=_identical_ensemble(
                    analysis=analysis,
                    batch=batch,
                    page_plan=page_plan,
                    overrides={unknown.candidate_id: "uncertain"},
                ),
            )
        )
    projection = project_gmail_temporal_review(
        text=text,
        analysis=analysis,
        batch_plan=batch_plan,
        batch_results=tuple(results),
    )
    structural = projection.groups[0]
    artifact = projection.artifacts[0]
    review = GmailTemporalReviewClusterReview(
        version="gmail_temporal_review_cluster_review_v1",
        review_id="cluster_review:hidden-split",
        batch_fingerprint=artifact.batch_fingerprint,
        frontier_fingerprint=artifact.frontier_fingerprint,
        cluster_id="hidden-split",
        expression_id=structural.members[0].expression_id,
        candidate_ids=("hidden-candidate",),
        reason="split_semantics_unresolved",
    )
    member = replace(
        structural.members[0],
        cluster_review_ids=(review.review_id,),
    )
    split_group_id = "hidden-split-group"
    split_member_material = {
        "version": "gmail_temporal_review_group_member_v1",
        "group_id": split_group_id,
        "expression_id": review.expression_id,
        "role": "unresolved",
        "source_order": None,
    }
    split_group = GmailTemporalReviewGroup(
        version="gmail_temporal_review_group_v1",
        group_id=split_group_id,
        kind="split_semantics",
        coverage="conflicted",
        source_start=structural.source_start,
        source_end=structural.source_end,
        subject_family_id=None,
        members=(
            GmailTemporalReviewGroupMember(
                version="gmail_temporal_review_group_member_v1",
                member_id="gtrgm_"
                + hashlib.sha256(
                    json.dumps(
                        split_member_material,
                        separators=(",", ":"),
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest(),
                expression_id=review.expression_id,
                role="unresolved",
                source_order=None,
                state="conflicted",
                artifact_ids=(),
                cluster_review_ids=(review.review_id,),
                subject_family_ids=(),
                reasons=("split_semantics_unresolved",),
            ),
        ),
        reasons=("split_semantics_unresolved",),
    )
    tampered = _refingerprinted(
        replace(
            projection,
            projection_fingerprint="",
            cluster_reviews=(review,),
            groups=(
                replace(structural, members=(member, *structural.members[1:])),
                split_group,
            ),
        )
    )

    with pytest.raises(GmailTemporalReviewError, match="group structure"):
        canonical_gmail_temporal_review_projection_bytes(tampered)


def test_review_projection_rejects_missing_batch_and_component_provenance_drift() -> (
    None
):
    text = (
        "Subject: Juniper Interview\n\n"
        "Possible dates are August 18, 2027 or August 19, 2027."
    )
    analysis, batch_plan, results, _ = _fixture(text, _select_first_uncertain)

    with pytest.raises(GmailTemporalReviewError, match="cover the batch plan exactly"):
        project_gmail_temporal_review(
            text=text,
            analysis=analysis,
            batch_plan=batch_plan,
            batch_results=results[:1],
        )

    drifted = (
        results[0],
        replace(results[1], component_evidence_fingerprints=("d" * 64,)),
    )
    with pytest.raises(GmailTemporalReviewError, match="component evidence"):
        project_gmail_temporal_review(
            text=text,
            analysis=analysis,
            batch_plan=batch_plan,
            batch_results=drifted,
        )


def test_review_projection_rejects_stale_ensemble_and_tampered_payload() -> None:
    text = "The meeting is scheduled for August 14, 2027."
    analysis, batch_plan, results, projection = _fixture(
        text,
        _select_lifecycle("scheduled", "supported"),
    )
    stale_result = replace(
        results[0],
        ensemble=replace(results[0].ensemble, policy_fingerprint="gtrp_stale"),
    )
    with pytest.raises(GmailTemporalReviewError, match="ensemble authority"):
        project_gmail_temporal_review(
            text=text,
            analysis=analysis,
            batch_plan=batch_plan,
            batch_results=(stale_result,),
        )

    with pytest.raises(GmailTemporalReviewError, match="fingerprint is stale"):
        canonical_gmail_temporal_review_projection_bytes(
            replace(projection, projection_fingerprint="gtrp_stale")
        )


def test_review_projection_rejects_incomplete_batch_plan() -> None:
    text = "The meeting is August 14, 2027."
    analysis = analyze_gmail_temporal_leads(
        text=text,
        message_internal_at="2027-08-10T09:00:00-07:00",
        fact_admitted=False,
        chunk_id="not-admitted",
    )
    batch_plan = plan_gmail_temporal_selector_batches(text=text, analysis=analysis)
    assert batch_plan.omissions

    with pytest.raises(GmailTemporalReviewError, match="complete batch plan"):
        project_gmail_temporal_review(
            text=text,
            analysis=analysis,
            batch_plan=batch_plan,
            batch_results=(),
        )
