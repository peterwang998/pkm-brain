from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from typing import Callable

import pytest

import pkm_brain.gmail_temporal_review as review_module
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
    gmail_temporal_review_grouping_policy_fingerprint,
    project_gmail_temporal_review,
    _structural_frames,
)


VerdictSelector = Callable[
    [str, TemporalLeadAnalysis, tuple[GmailTemporalVerificationCandidate, ...]],
    dict[str, str],
]


def test_grouping_policy_binds_deterministic_candidate_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = gmail_temporal_review_grouping_policy_fingerprint()
    monkeypatch.setattr(
        review_module,
        "gmail_temporal_candidate_policy_fingerprint",
        lambda: "gtcp_" + "0" * 64,
    )

    assert gmail_temporal_review_grouping_policy_fingerprint() != before


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


def _select_exact_reschedule(verdict: str) -> VerdictSelector:
    def select(
        _expression_id: str,
        _analysis: TemporalLeadAnalysis,
        candidates: tuple[GmailTemporalVerificationCandidate, ...],
    ) -> dict[str, str]:
        candidate = next(
            item
            for item in candidates
            if item.lifecycle in {"rescheduled_old", "rescheduled_replacement"}
        )
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


def _select_generic_event_supported(
    _expression_id: str,
    analysis: TemporalLeadAnalysis,
    candidates: tuple[GmailTemporalVerificationCandidate, ...],
) -> dict[str, str]:
    mention_types = {
        mention.mention_id: mention.mention_type for mention in analysis.mentions
    }
    candidate = next(
        item for item in candidates if mention_types[item.subject_mention_id] == "event"
    )
    return {candidate.candidate_id: "supported"}


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
        "version": "gmail_temporal_review_hypothesis_v3",
        "signature": signature,
        "subject_type_references": references,
        "subject_alias_type_references": (hypothesis.subject_alias_type_references),
        "canonical_subject_mention_id": hypothesis.canonical_subject_mention_id,
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

    def select_non_deferred_scheduled(
        _expression_id: str,
        _analysis: TemporalLeadAnalysis,
        candidates: tuple[GmailTemporalVerificationCandidate, ...],
    ) -> dict[str, str]:
        candidate = next(
            item
            for item in candidates
            if item.lifecycle == "scheduled" and not item.requires_defer
        )
        return {candidate.candidate_id: "supported"}

    analysis, _, _, projection = _fixture(
        text,
        select_non_deferred_scheduled,
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
    ("text", "expected_bare", "expected_title", "expected_artifacts"),
    (
        (
            (
                "The Lumen Quay planning session has been rescheduled to "
                "September 22, 2027 from September 19, 2027. "
                "The room is unchanged."
            ),
            "session",
            "Lumen Quay planning session",
            2,
        ),
        (
            "The Northstar design review is scheduled for October 2, 2027.",
            "review",
            "Northstar design review",
            1,
        ),
    ),
)
def test_supported_generic_event_preserves_evidence_and_promotes_unique_title_identity(
    text: str,
    expected_bare: str,
    expected_title: str,
    expected_artifacts: int,
) -> None:
    analysis, _, _, projection = _fixture(
        text,
        _select_generic_event_supported,
        chunk_id=f"review-canonical-title-{expected_bare}",
    )
    surfaces = {
        mention.mention_id: text[mention.start : mention.end]
        for mention in analysis.mentions
    }

    assert projection.version == "gmail_temporal_review_projection_v3"
    assert len(projection.artifacts) == expected_artifacts
    for artifact in projection.artifacts:
        hypothesis = artifact.hypotheses[0]
        assert hypothesis.version == "gmail_temporal_review_hypothesis_v3"
        assert {surfaces[value] for value in hypothesis.subject_mention_ids} == {
            expected_bare
        }
        assert {surfaces[value] for value in hypothesis.subject_alias_mention_ids} == {
            expected_bare,
            expected_title,
        }
        assert hypothesis.canonical_subject_mention_id not in (
            *hypothesis.subject_mention_ids,
            None,
        )
        assert surfaces[hypothesis.canonical_subject_mention_id] == expected_title
        assert (
            tuple(mention_id for mention_id, _ in hypothesis.subject_type_references)
            == hypothesis.subject_mention_ids
        )
        assert (
            tuple(
                mention_id for mention_id, _ in hypothesis.subject_alias_type_references
            )
            == hypothesis.subject_alias_mention_ids
        )

        signature = (
            hypothesis.expression_id,
            hypothesis.relation,
            hypothesis.kind,
            hypothesis.lifecycle,
            hypothesis.normalized_value,
        )
        legacy_id = (
            "gtrh_"
            + hashlib.sha256(
                json.dumps(
                    {
                        "version": "gmail_temporal_review_hypothesis_v2",
                        "signature": signature,
                        "subject_type_references": hypothesis.subject_type_references,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
        )
        assert hypothesis.hypothesis_id != legacy_id

    payload = gmail_temporal_review_projection_payload(projection)
    serialized = payload["artifacts"][0]["hypotheses"][0]
    assert serialized["subject_alias_mention_ids"]
    assert serialized["subject_alias_type_references"]
    assert serialized["canonical_subject_mention_id"]
    assert b"subject_alias_mention_ids" in (
        canonical_gmail_temporal_review_projection_bytes(projection)
    )


def test_subject_identity_canonicalization_fails_closed_for_two_named_titles() -> None:
    aliases, references, canonical = review_module._subject_identity_metadata(  # noqa: SLF001
        subject_mention_ids=("bare",),
        subject_types_by_id={
            "bare": "event",
            "title-a": "event_title_candidate",
            "title-b": "event_title_candidate",
        },
        subject_families={
            "bare": "family-a",
            "title-a": "family-a",
            "title-b": "family-a",
        },
        subject_family_members={
            "family-a": ("bare", "title-a", "title-b"),
        },
    )

    assert aliases == ("bare", "title-a", "title-b")
    assert references == (
        ("bare", "event"),
        ("title-a", "event_title_candidate"),
        ("title-b", "event_title_candidate"),
    )
    assert canonical is None
    assert review_module._subject_identity_references_are_valid(  # noqa: SLF001
        subject_mention_ids=("bare",),
        subject_type_references=(("bare", "event"),),
        subject_alias_mention_ids=aliases,
        subject_alias_type_references=references,
        canonical_subject_mention_id=None,
    )
    assert not review_module._subject_identity_references_are_valid(  # noqa: SLF001
        subject_mention_ids=("bare",),
        subject_type_references=(("bare", "event"),),
        subject_alias_mention_ids=aliases,
        subject_alias_type_references=references,
        canonical_subject_mention_id="title-a",
    )


def test_subject_identity_never_collapses_a_distinct_named_event() -> None:
    aliases, _, canonical = review_module._subject_identity_metadata(  # noqa: SLF001
        subject_mention_ids=("bare",),
        subject_types_by_id={
            "bare": "event",
            "same-title": "event_title_candidate",
            "different-title": "event_title_candidate",
        },
        subject_families={
            "bare": "family-a",
            "same-title": "family-a",
            "different-title": "family-b",
        },
        subject_family_members={
            "family-a": ("bare", "same-title"),
            "family-b": ("different-title",),
        },
    )

    assert aliases == ("bare", "same-title")
    assert canonical == "same-title"


def test_projection_keeps_distinct_named_event_families_separate() -> None:
    text = (
        "The Northstar design review is scheduled for October 2, 2027. "
        "The Lumen Quay planning session is scheduled for October 3, 2027."
    )
    analysis, _, _, projection = _fixture(
        text,
        _select_generic_event_supported,
        chunk_id="review-distinct-named-events",
    )
    surfaces = {
        mention.mention_id: text[mention.start : mention.end]
        for mention in analysis.mentions
    }
    aliases_by_value = {
        hypothesis.normalized_value: {
            surfaces[value] for value in hypothesis.subject_alias_mention_ids
        }
        for artifact in projection.artifacts
        for hypothesis in artifact.hypotheses
    }

    assert aliases_by_value == {
        "2027-10-02": {"review", "Northstar design review"},
        "2027-10-03": {"session", "Lumen Quay planning session"},
    }
    assert len({group.subject_family_id for group in projection.groups}) == 2


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


def test_exact_reschedule_candidates_preserve_structural_endpoint_roles() -> None:
    text = (
        "The hiring interview was rescheduled from August 20, 2027 to August 16, 2027."
    )
    _, _, _, projection = _fixture(text, _select_exact_reschedule("uncertain"))

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
    assert [item.lifecycle for item in hypotheses] == [
        "rescheduled_old",
        "rescheduled_replacement",
    ]
    assert all(item.candidate_requires_defer is False for item in hypotheses)
    assert all(
        (item.relation, item.kind) == ("occurrence", "planned") for item in hypotheses
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
        candidate = next(
            item for item in candidates if item.lifecycle == "rescheduled_old"
        )
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


@pytest.mark.parametrize(
    ("text", "expected_roles", "expected_values"),
    (
        (
            "The meeting was pushed back from Aug 12 until Aug 15.",
            ("rescheduled_old", "rescheduled_replacement"),
            ("2027-08-12", "2027-08-15"),
        ),
        (
            "The meeting changed from Aug 12 until Aug 15.",
            ("rescheduled_old", "rescheduled_replacement"),
            ("2027-08-12", "2027-08-15"),
        ),
        (
            "Meeting update — New date: Aug 15 (was Aug 12).",
            ("rescheduled_replacement", "rescheduled_old"),
            ("2027-08-15", "2027-08-12"),
        ),
        (
            "The meeting is now Aug 15 instead of Aug 12.",
            ("rescheduled_replacement", "rescheduled_old"),
            ("2027-08-15", "2027-08-12"),
        ),
        (
            "The meeting moved Aug 12 -> Aug 15.",
            ("rescheduled_old", "rescheduled_replacement"),
            ("2027-08-12", "2027-08-15"),
        ),
        (
            "The meeting moved Aug 15 <- Aug 12.",
            ("rescheduled_replacement", "rescheduled_old"),
            ("2027-08-15", "2027-08-12"),
        ),
        (
            "The meeting moved to Aug 15 from Aug 12.",
            ("rescheduled_replacement", "rescheduled_old"),
            ("2027-08-15", "2027-08-12"),
        ),
        (
            "The meeting was rescheduled to Aug 15 from Aug 12.",
            ("rescheduled_replacement", "rescheduled_old"),
            ("2027-08-15", "2027-08-12"),
        ),
    ),
)
def test_directional_reschedule_grammar_preserves_exact_endpoint_roles(
    text: str,
    expected_roles: tuple[str, str],
    expected_values: tuple[str, str],
) -> None:
    analysis, _, _, projection = _fixture(text, _select_first_uncertain)

    assert [
        expression.normalized_options[0] for expression in analysis.expressions
    ] == list(expected_values)
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("reschedule", "complete")
    assert tuple(item.role for item in group.members) == expected_roles
    assert tuple(item.source_order for item in group.members) == (1, 2)
    assert group.reasons == ()
    assert group.candidate_authorization is False
    assert group.requires_defer is True
    assert group.routable is False


@pytest.mark.parametrize(
    ("text", "expected_roles"),
    (
        (
            "Meeting update — New date: Aug 15 (was Aug 12 or Aug 13).",
            ("rescheduled_replacement", "unresolved", "unresolved"),
        ),
        (
            "The meeting is now Aug 15 instead of Aug 12 or Aug 13.",
            ("rescheduled_replacement", "unresolved", "unresolved"),
        ),
        (
            "The meeting moved Aug 12 -> Aug 15 or Aug 16.",
            ("rescheduled_old", "unresolved", "unresolved"),
        ),
        (
            "The meeting was rescheduled from Aug 12 to Aug 15 or Aug 16.",
            ("rescheduled_old", "unresolved", "unresolved"),
        ),
        (
            "The meeting was rescheduled from Aug 12 to Aug 15 or on Aug 16.",
            ("rescheduled_old", "unresolved", "unresolved"),
        ),
        (
            "The meeting was rescheduled from Aug 12 to Aug 15, or perhaps Aug 16.",
            ("rescheduled_old", "unresolved", "unresolved"),
        ),
        (
            "The meeting was rescheduled from Aug 12 to Aug 15 or maybe at Aug 16.",
            ("rescheduled_old", "unresolved", "unresolved"),
        ),
        (
            "The meeting moved to Aug 15 from Aug 12 or Aug 13.",
            ("rescheduled_replacement", "unresolved", "unresolved"),
        ),
        (
            "The meeting was rescheduled from Aug 12 to Aug 15, Aug 16, or Aug 17.",
            (
                "rescheduled_old",
                "unresolved",
                "unresolved",
                "unresolved",
            ),
        ),
    ),
)
def test_endpoint_alternatives_form_one_conflicted_reschedule_group(
    text: str,
    expected_roles: tuple[str, ...],
) -> None:
    analysis, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(analysis.expressions) == len(expected_roles)
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("reschedule", "conflicted")
    assert tuple(member.role for member in group.members) == expected_roles
    assert tuple(member.source_order for member in group.members) == tuple(
        range(1, len(expected_roles) + 1)
    )
    assert group.reasons == ("reschedule_endpoint_alternatives_unresolved",)
    assert {member.expression_id for member in group.members} == {
        expression.expression_id for expression in analysis.expressions
    }
    assert sum(len(member.artifact_ids) for member in group.members) == len(
        projection.artifacts
    )


@pytest.mark.parametrize("hedge", ("possibly", "potentially", "conceivably"))
def test_unrecognized_bounded_or_connector_quarantines_the_reschedule(
    hedge: str,
) -> None:
    text = f"The meeting was rescheduled from Aug 12 to Aug 15 or {hedge} Aug 16."
    analysis, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(analysis.expressions) == 3
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("reschedule", "conflicted")
    assert tuple(member.role for member in group.members) == (
        "unresolved",
        "unresolved",
        "unresolved",
    )
    assert tuple(member.source_order for member in group.members) == (1, 2, 3)
    assert group.reasons == ("reschedule_endpoint_connector_unresolved",)
    assert {member.expression_id for member in group.members} == {
        expression.expression_id for expression in analysis.expressions
    }


@pytest.mark.parametrize(
    "text",
    (
        (
            "The meeting was rescheduled from Aug 12 to Aug 15 or the hotel "
            "was booked for Aug 16."
        ),
        (
            "The meeting was rescheduled from Aug 12 to Aug 15 or we could "
            "meet on Aug 16."
        ),
        ("The meeting was rescheduled from Aug 12 to Aug 15 or dinner is on Aug 16."),
        (
            "The meeting was rescheduled from Aug 12 to Aug 15 or Project "
            "Phoenix is on Aug 16."
        ),
    ),
)
def test_ordinary_or_clause_does_not_expand_the_reschedule_span(text: str) -> None:
    analysis, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(analysis.expressions) == 3
    assert len(projection.groups) == 2
    reschedule = next(
        group for group in projection.groups if group.kind == "reschedule"
    )
    assert reschedule.coverage == "complete"
    assert tuple(member.role for member in reschedule.members) == (
        "rescheduled_old",
        "rescheduled_replacement",
    )
    independent = next(group for group in projection.groups if group.kind == "single")
    assert tuple(member.role for member in independent.members) == ("independent",)
    assert {
        member.expression_id for group in projection.groups for member in group.members
    } == {expression.expression_id for expression in analysis.expressions}


@pytest.mark.parametrize("tail", (" or 16", "/16"))
def test_abbreviated_shared_month_day_is_consumed_by_the_reschedule(tail: str) -> None:
    text = f"The meeting was rescheduled from August 12, 2027 to August 15, 2027{tail}."
    analysis, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(analysis.expressions) == 3
    assert analysis.expressions[-1].form == "abbreviated_shared_month_day"
    assert analysis.expressions[-1].blockers == (
        "reschedule_endpoint_alternatives_unresolved",
    )
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("reschedule", "conflicted")
    assert tuple(member.role for member in group.members) == (
        "rescheduled_old",
        "unresolved",
        "unresolved",
    )
    assert tuple(member.source_order for member in group.members) == (1, 2, 3)
    assert group.reasons == ("reschedule_endpoint_alternatives_unresolved",)
    assert group.source_end == analysis.expressions[-1].end
    assert not any("missing_from_source" in reason for reason in group.reasons)


@pytest.mark.parametrize(
    ("text", "retained_count", "expected_missing_roles"),
    (
        (
            (
                "The meeting was rescheduled from August 12, 2027 to "
                "August 15, 2027 or 16."
            ),
            2,
            (),
        ),
        (
            "The meeting was postponed until August 15, 2027/16.",
            1,
            ("rescheduled_old",),
        ),
    ),
)
def test_raw_tail_guard_survives_an_absent_abbreviated_day_lead(
    text: str,
    retained_count: int,
    expected_missing_roles: tuple[str, ...],
) -> None:
    analysis = analyze_gmail_temporal_leads(
        text=text,
        message_internal_at="2027-08-10T09:00:00-07:00",
        fact_admitted=True,
        chunk_id="review-raw-tail-fallback",
    )
    assert analysis.expressions[-1].form == "abbreviated_shared_month_day"
    retained = analysis.expressions[:retained_count]
    lagging_analysis = replace(analysis, expressions=retained)

    frames = _structural_frames(text=text, analysis=lagging_analysis)

    assert len(frames) == 1
    frame = frames[0]
    assert frame.kind == "reschedule"
    assert (
        tuple(member.role for member in frame.members)
        == ("unresolved",) * retained_count
    )
    assert frame.missing_roles == expected_missing_roles
    assert frame.conflict_reasons == (
        "reschedule_endpoint_abbreviated_alternative_unresolved",
    )
    assert frame.source_end > retained[-1].end


@pytest.mark.parametrize(
    "text",
    (
        (
            "The meeting was rescheduled from August 12, 2027 to "
            "August 15, 2027 or the 16th."
        ),
        ("The meeting was rescheduled from August 12, 2027 to August 15 or 16, 2027."),
    ),
)
def test_unparsed_ordinal_or_shared_year_tail_quarantines_the_pair(text: str) -> None:
    analysis, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(analysis.expressions) == 2
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("reschedule", "conflicted")
    assert tuple(member.role for member in group.members) == (
        "unresolved",
        "unresolved",
    )
    assert tuple(member.source_order for member in group.members) == (1, 2)
    assert group.reasons == ("reschedule_endpoint_abbreviated_alternative_unresolved",)
    assert group.source_end > analysis.expressions[-1].end
    assert not any("missing_from_source" in reason for reason in group.reasons)


@pytest.mark.parametrize(
    "text",
    (
        "The meeting was postponed until August 15, 2027 or the 16th.",
        "The meeting was postponed until August 15 or 16, 2027.",
    ),
)
def test_unparsed_ordinal_or_shared_year_replacement_only_is_conflicted(
    text: str,
) -> None:
    analysis, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(analysis.expressions) == 1
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("reschedule", "conflicted")
    assert tuple(member.role for member in group.members) == ("unresolved",)
    assert tuple(member.source_order for member in group.members) == (1,)
    assert group.reasons == (
        "reschedule_endpoint_abbreviated_alternative_unresolved",
        "rescheduled_old_missing_from_source",
    )
    assert group.source_end > analysis.expressions[-1].end


@pytest.mark.parametrize(
    "text",
    (
        ("The meeting was moved to August 15, 2027 or the 16th from August 12, 2027."),
        (
            "The meeting was rescheduled from August 12, 2027 or the 13th to "
            "August 15, 2027."
        ),
        ("The meeting was moved to August 15 or 16, 2027 from August 12, 2027."),
        ("The meeting was rescheduled from August 12 or 13, 2027 to August 15, 2027."),
    ),
)
def test_unparsed_interior_alternative_never_assigns_endpoint_roles(text: str) -> None:
    analysis, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(analysis.expressions) == 2
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("reschedule", "conflicted")
    assert tuple(member.role for member in group.members) == (
        "unresolved",
        "unresolved",
    )
    assert tuple(member.source_order for member in group.members) == (1, 2)
    assert group.reasons == ("reschedule_endpoint_abbreviated_alternative_unresolved",)
    assert not any("missing_from_source" in reason for reason in group.reasons)


@pytest.mark.parametrize(
    ("text", "missing_reason"),
    (
        (
            "The meeting was postponed until August 15, 2027 or 16.",
            "rescheduled_old_missing_from_source",
        ),
        (
            "The meeting was rescheduled from August 12, 2027 or 13.",
            "rescheduled_replacement_missing_from_source",
        ),
        (
            "Meeting update — New date: August 15, 2027 / 16.",
            "rescheduled_old_missing_from_source",
        ),
    ),
)
def test_single_endpoint_abbreviated_day_is_never_incomplete_or_known_role(
    text: str,
    missing_reason: str,
) -> None:
    analysis, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(analysis.expressions) == 2
    assert analysis.expressions[-1].form == "abbreviated_shared_month_day"
    assert analysis.expressions[-1].blockers == (
        "reschedule_endpoint_alternatives_unresolved",
    )
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("reschedule", "conflicted")
    assert tuple(member.role for member in group.members) == (
        "unresolved",
        "unresolved",
    )
    assert tuple(member.source_order for member in group.members) == (1, 2)
    assert group.reasons == (
        "reschedule_endpoint_alternatives_unresolved",
        missing_reason,
    )
    assert group.source_end == analysis.expressions[-1].end


def test_full_date_slash_quarantines_the_complete_reschedule_span() -> None:
    text = (
        "The meeting was rescheduled from August 12, 2027 to August 15, 2027 "
        "/ August 16, 2027."
    )
    analysis, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(analysis.expressions) == 3
    assert analysis.expressions[-1].form != "abbreviated_shared_month_day"
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("reschedule", "conflicted")
    assert tuple(member.role for member in group.members) == (
        "unresolved",
        "unresolved",
        "unresolved",
    )
    assert tuple(member.source_order for member in group.members) == (1, 2, 3)
    assert group.reasons == ("reschedule_endpoint_connector_unresolved",)
    assert {member.expression_id for member in group.members} == {
        expression.expression_id for expression in analysis.expressions
    }


def test_non_reschedule_full_date_slash_remains_independent() -> None:
    text = "Hotel check-in is August 15, 2027 / Dinner is August 16, 2027."
    analysis, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(analysis.expressions) == 2
    assert len(projection.groups) == 2
    assert all(
        (group.kind, group.coverage) == ("single", "complete")
        for group in projection.groups
    )


@pytest.mark.parametrize(
    "tail",
    (
        " / 2.0 release.",
        " / 16 attendees joined.",
        " or the 16th attendees joined.",
    ),
)
def test_numeric_version_or_count_tail_does_not_quarantine_reschedule(
    tail: str,
) -> None:
    text = f"The meeting was rescheduled from August 12, 2027 to August 15, 2027{tail}"
    _, _, _, projection = _fixture(text, _select_first_uncertain)

    reschedule = next(
        group for group in projection.groups if group.kind == "reschedule"
    )
    assert (reschedule.kind, reschedule.coverage) == ("reschedule", "complete")
    assert tuple(member.role for member in reschedule.members) == (
        "rescheduled_old",
        "rescheduled_replacement",
    )
    assert "reschedule_endpoint_connector_unresolved" not in reschedule.reasons
    assert (
        "reschedule_endpoint_abbreviated_alternative_unresolved"
        not in reschedule.reasons
    )


def test_day_like_count_tail_is_not_inventoried_as_a_date_alternative() -> None:
    text = (
        "The meeting was rescheduled from August 12, 2027 to August 15, 2027 "
        "or 16 people joined the waitlist."
    )
    analysis, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(analysis.expressions) == 2
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("reschedule", "complete")
    assert tuple(member.role for member in group.members) == (
        "rescheduled_old",
        "rescheduled_replacement",
    )


def test_invalid_abbreviated_day_never_gains_slash_connector_authority() -> None:
    text = "The meeting was rescheduled from August 12, 2027 to August 15, 2027/32."
    analysis, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(analysis.expressions) == 3
    assert analysis.expressions[-1].form == "abbreviated_shared_month_day"
    assert "invalid_calendar_date" in analysis.expressions[-1].blockers
    reschedule = next(
        group for group in projection.groups if group.kind == "reschedule"
    )
    assert reschedule.coverage == "conflicted"
    assert tuple(member.role for member in reschedule.members) == (
        "unresolved",
        "unresolved",
        "unresolved",
    )
    assert reschedule.reasons == ("reschedule_endpoint_connector_unresolved",)


@pytest.mark.parametrize(
    ("text", "expected_roles"),
    (
        (
            "The meeting was moved to August 15, 2027/16 from August 12, 2027.",
            ("unresolved", "unresolved", "rescheduled_old"),
        ),
        (
            "The meeting was rescheduled from August 12, 2027/13 to August 15, 2027.",
            ("unresolved", "unresolved", "rescheduled_replacement"),
        ),
    ),
)
def test_slash_shorthand_preserves_only_the_unambiguous_opposite_role(
    text: str,
    expected_roles: tuple[str, ...],
) -> None:
    analysis, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(analysis.expressions) == 3
    assert analysis.expressions[1].form == "abbreviated_shared_month_day"
    assert analysis.expressions[1].blockers == (
        "reschedule_endpoint_alternatives_unresolved",
    )
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("reschedule", "conflicted")
    assert tuple(member.role for member in group.members) == expected_roles
    assert tuple(member.source_order for member in group.members) == (1, 2, 3)
    assert group.reasons == ("reschedule_endpoint_alternatives_unresolved",)


@pytest.mark.parametrize(
    ("text", "expected_count", "missing_reason"),
    (
        (
            "The meeting was postponed until Aug 15 or Aug 16.",
            2,
            "rescheduled_old_missing_from_source",
        ),
        (
            "Meeting update — New date: Aug 15 or Aug 16.",
            2,
            "rescheduled_old_missing_from_source",
        ),
        (
            "The meeting was rescheduled for Aug 15, Aug 16, or Aug 17.",
            3,
            "rescheduled_old_missing_from_source",
        ),
        (
            "The meeting was rescheduled from Aug 12 or Aug 13.",
            2,
            "rescheduled_replacement_missing_from_source",
        ),
    ),
)
def test_single_endpoint_alternatives_form_one_all_unresolved_group(
    text: str,
    expected_count: int,
    missing_reason: str,
) -> None:
    analysis, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(analysis.expressions) == expected_count
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("reschedule", "conflicted")
    assert (
        tuple(member.role for member in group.members)
        == ("unresolved",) * expected_count
    )
    assert tuple(member.source_order for member in group.members) == tuple(
        range(1, expected_count + 1)
    )
    assert group.reasons == (
        "reschedule_endpoint_alternatives_unresolved",
        missing_reason,
    )
    assert {member.expression_id for member in group.members} == {
        expression.expression_id for expression in analysis.expressions
    }
    assert sum(len(member.artifact_ids) for member in group.members) == len(
        projection.artifacts
    )


@pytest.mark.parametrize(
    ("text", "expected_singleton_role"),
    (
        (
            "The meeting was rescheduled to Aug 15 or Aug 16 from Aug 12.",
            "rescheduled_old",
        ),
        (
            "The meeting is now Aug 15 or Aug 16 instead of Aug 12.",
            "rescheduled_old",
        ),
        (
            "The meeting moved Aug 12 or Aug 13 -> Aug 15.",
            "rescheduled_replacement",
        ),
        (
            "Meeting update — New date: Aug 15 or Aug 16 (was Aug 12).",
            "rescheduled_old",
        ),
    ),
)
def test_leading_endpoint_alternatives_preserve_only_opposite_role(
    text: str,
    expected_singleton_role: str,
) -> None:
    analysis, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(analysis.expressions) == 3
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("reschedule", "conflicted")
    assert tuple(member.role for member in group.members) == (
        "unresolved",
        "unresolved",
        expected_singleton_role,
    )
    assert tuple(member.source_order for member in group.members) == (1, 2, 3)
    assert group.reasons == ("reschedule_endpoint_alternatives_unresolved",)
    assert {member.expression_id for member in group.members} == {
        expression.expression_id for expression in analysis.expressions
    }
    assert sum(len(member.artifact_ids) for member in group.members) == len(
        projection.artifacts
    )


def test_collapsed_old_slot_alternatives_form_one_all_unresolved_group() -> None:
    text = "The meeting was rescheduled from Aug 12 or Aug 13 to Aug 15."
    analysis, _, _, projection = _fixture(text, _select_first_uncertain)

    assert [expression.form for expression in analysis.expressions] == [
        "inferred_date",
        "date_range",
    ]
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("reschedule", "conflicted")
    assert tuple(member.role for member in group.members) == (
        "unresolved",
        "unresolved",
    )
    assert tuple(member.source_order for member in group.members) == (1, 2)
    assert group.reasons == (
        "reschedule_endpoint_alternatives_unresolved",
        "reschedule_endpoint_representation_unresolved",
    )
    assert {member.expression_id for member in group.members} == {
        expression.expression_id for expression in analysis.expressions
    }
    assert sum(len(member.artifact_ids) for member in group.members) == len(
        projection.artifacts
    )


def test_both_endpoint_alternative_chains_form_one_all_unresolved_group() -> None:
    text = "The meeting was moved to Aug 15 or Aug 16 from Aug 12 or Aug 13."
    analysis, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(analysis.expressions) == 4
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("reschedule", "conflicted")
    assert tuple(member.role for member in group.members) == (
        "unresolved",
        "unresolved",
        "unresolved",
        "unresolved",
    )
    assert tuple(member.source_order for member in group.members) == (1, 2, 3, 4)
    assert group.reasons == ("reschedule_endpoint_alternatives_unresolved",)
    assert {member.expression_id for member in group.members} == {
        expression.expression_id for expression in analysis.expressions
    }
    assert sum(len(member.artifact_ids) for member in group.members) == len(
        projection.artifacts
    )


def test_collapsed_both_slot_alternatives_never_invent_a_missing_role() -> None:
    text = "The meeting was rescheduled from Aug 12 or Aug 13 to Aug 15 or Aug 16."
    analysis, _, _, projection = _fixture(text, _select_first_uncertain)

    assert [expression.form for expression in analysis.expressions] == [
        "inferred_date",
        "date_range",
        "inferred_date",
    ]
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("reschedule", "conflicted")
    assert tuple(member.role for member in group.members) == (
        "unresolved",
        "unresolved",
        "unresolved",
    )
    assert tuple(member.source_order for member in group.members) == (1, 2, 3)
    assert group.reasons == (
        "reschedule_endpoint_alternatives_unresolved",
        "reschedule_endpoint_representation_unresolved",
    )
    assert not any("missing_from_source" in reason for reason in group.reasons)
    assert {member.expression_id for member in group.members} == {
        expression.expression_id for expression in analysis.expressions
    }
    assert sum(len(member.artifact_ids) for member in group.members) == len(
        projection.artifacts
    )


@pytest.mark.parametrize(
    "text",
    (
        "The meeting was postponed until Aug 15.",
        "The meeting moved to Aug 15.",
    ),
)
def test_replacement_only_reschedule_is_explicitly_incomplete(text: str) -> None:
    _, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("reschedule", "incomplete")
    assert tuple(item.role for item in group.members) == ("rescheduled_replacement",)
    assert tuple(item.source_order for item in group.members) == (1,)
    assert group.members[0].state == "present"
    assert group.reasons == ("rescheduled_old_missing_from_source",)
    assert group.subject_family_id is None
    assert group.candidate_authorization is False


@pytest.mark.parametrize(
    "text",
    (
        "The meeting was postponed and the hotel was booked for Aug 15.",
        "The meeting was rescheduled because we booked the room for Aug 15.",
        "The meeting moved to Zoom and the hotel was booked for Aug 15.",
    ),
)
def test_replacement_only_cue_does_not_capture_an_unrelated_later_date(
    text: str,
) -> None:
    _, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("single", "complete")
    assert tuple(member.role for member in group.members) == ("independent",)


def test_bare_changed_to_date_does_not_invent_reschedule_roles() -> None:
    text = "The meeting changed to Aug 15."
    _, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("single", "complete")
    assert tuple(item.role for item in group.members) == ("independent",)


def test_bidirectional_reschedule_arrow_stays_explicitly_unresolved() -> None:
    text = "The meeting moved Aug 12 <-> Aug 15."
    _, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("reschedule", "conflicted")
    assert tuple(item.role for item in group.members) == ("unresolved", "unresolved")
    assert tuple(item.source_order for item in group.members) == (1, 2)
    assert group.reasons == ("reschedule_endpoint_direction_unresolved",)
    assert group.subject_family_id is None
    assert group.candidate_authorization is False


def test_collapsed_reschedule_range_never_guesses_endpoint_roles() -> None:
    text = "The meeting was pushed back from Aug 12 until 15."
    analysis, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(analysis.expressions) == 1
    assert analysis.expressions[0].form == "date_range"
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("reschedule", "conflicted")
    assert tuple(item.role for item in group.members) == ("unresolved",)
    assert tuple(item.source_order for item in group.members) == (1,)
    assert group.reasons == ("reschedule_endpoint_representation_unresolved",)
    assert group.subject_family_id is None
    assert group.candidate_authorization is False


@pytest.mark.parametrize(
    "text",
    (
        "The meeting moved to Zoom and runs from Aug 12 until Aug 15.",
        "The meeting was postponed and vacation runs from Aug 12 until Aug 15.",
    ),
)
def test_earlier_lifecycle_cue_does_not_relabel_unrelated_interval(
    text: str,
) -> None:
    analysis, _, _, projection = _fixture(text, _select_first_uncertain)

    assert len(analysis.expressions) == 1
    assert analysis.expressions[0].form == "date_range"
    assert len(projection.groups) == 1
    group = projection.groups[0]
    assert (group.kind, group.coverage) == ("single", "complete")
    assert tuple(item.role for item in group.members) == ("independent",)


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
        "The hiring interview was rescheduled from August 14, 2027 "
        "until August 16, 2027."
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
        "The hiring interview was rescheduled from August 14, 2027 "
        "until August 16, 2027."
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
