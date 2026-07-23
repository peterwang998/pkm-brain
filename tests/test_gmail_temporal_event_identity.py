from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Callable, Mapping

import pytest

from pkm_brain.gmail_temporal_batching import (
    GmailTemporalBatchPlan,
    GmailTemporalSelectorBatch,
    plan_gmail_temporal_selector_batches,
)
from pkm_brain.gmail_temporal_event_identity import (
    MAX_EVENT_IDENTITY_PAIRS,
    MAX_EVENT_IDENTITY_UNITS,
    GmailTemporalEventIdentityError,
    GmailTemporalEventIdentityPair,
    GmailTemporalEventIdentityPlan,
    GmailTemporalEventIdentityResolution,
    make_gmail_temporal_event_identity_verdict_set,
    plan_gmail_temporal_event_identity,
    resolve_gmail_temporal_event_identity,
    validate_gmail_temporal_event_identity_resolution,
)
from pkm_brain.gmail_temporal_event_identity_consumers import (
    GmailTemporalEventIdentityConsumerError,
    GmailTemporalEventIdentitySourceText,
    bind_gmail_temporal_event_identity_unit_surfaces,
    build_gmail_temporal_event_identity_pair_requests,
    build_gmail_temporal_verified_event_bindings,
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
from pkm_brain.gmail_temporal_persistence import (
    GmailTemporalSourceLocator,
    gmail_temporal_message_scope_key,
)
from pkm_brain.gmail_temporal_review import (
    GmailTemporalReviewBatchResult,
    GmailTemporalReviewHypothesis,
    GmailTemporalReviewProjection,
    canonical_gmail_temporal_review_projection_bytes,
    project_gmail_temporal_review,
)
from pkm_brain.gmail_temporal_thread_lifecycle import (
    GmailTemporalThreadLifecycleError,
    GmailTemporalThreadMessageAuthority,
    GmailTemporalThreadMessageReview,
    GmailTemporalThreadSnapshotAuthority,
    project_gmail_temporal_thread_lifecycle,
)
from pkm_brain.gmail_temporal_thread_retrieval_experiment import (
    GmailTemporalThreadEvidence,
    plan_gmail_temporal_thread_retrieval_experiment,
)


ACCOUNT = "personal@example.test"
THREAD = "thread-event-identity"
DOCUMENT = "doc-gmail-current"
DOCUMENT_HASH = "d" * 64
SOURCE_REVISION = "e" * 64
PIPELINE = "gmail_temporal_review_v1"
_ANALYSIS_AUTHORITIES: dict[str, TemporalLeadAnalysis] = {}

CandidateSelector = Callable[
    [tuple[GmailTemporalVerificationCandidate, ...]],
    dict[str, str],
]


def _verdict_rows(
    page_plan: GmailTemporalCandidatePagePlan,
    overrides: Mapping[str, str],
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


def _ensemble(
    *,
    analysis: TemporalLeadAnalysis,
    batch: GmailTemporalSelectorBatch,
    page_plan: GmailTemporalCandidatePagePlan,
    overrides: Mapping[str, str],
) -> GmailTemporalCandidateEnsembleVerdictSet:
    rows = _verdict_rows(page_plan, overrides)
    return validate_gmail_temporal_candidate_ensemble_verdict_set(
        analysis=analysis,
        batch=batch,
        plan=page_plan,
        runs=(rows, rows, rows),
    )


def _projection(
    text: str,
    selector: CandidateSelector,
    *,
    internal_at: str,
    chunk_id: str,
) -> GmailTemporalReviewProjection:
    analysis = analyze_gmail_temporal_leads(
        text=text,
        message_internal_at=datetime.fromisoformat(internal_at).astimezone(
            timezone.utc
        ),
        fact_admitted=True,
        chunk_id=gmail_temporal_message_scope_key(
            gmail_account_key=ACCOUNT,
            gmail_thread_id=THREAD,
            gmail_message_id=chunk_id,
        ),
    )
    batch_plan: GmailTemporalBatchPlan = plan_gmail_temporal_selector_batches(
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
        results.append(
            GmailTemporalReviewBatchResult(
                batch=batch,
                page_plan=page_plan,
                ensemble=_ensemble(
                    analysis=analysis,
                    batch=batch,
                    page_plan=page_plan,
                    overrides=selector(frontier.candidates),
                ),
            )
        )
    projection = project_gmail_temporal_review(
        text=text,
        analysis=analysis,
        batch_plan=batch_plan,
        batch_results=tuple(results),
    )
    _ANALYSIS_AUTHORITIES[projection.analysis_fingerprint] = analysis
    return projection


def _select_lifecycle(lifecycle: str) -> CandidateSelector:
    def select(
        candidates: tuple[GmailTemporalVerificationCandidate, ...],
    ) -> dict[str, str]:
        candidate = next(item for item in candidates if item.lifecycle == lifecycle)
        return {candidate.candidate_id: "supported"}

    return select


def _select_exact_reschedule_endpoint(
    candidates: tuple[GmailTemporalVerificationCandidate, ...],
) -> dict[str, str]:
    candidate = next(
        item
        for item in candidates
        if item.lifecycle in {"rescheduled_old", "rescheduled_replacement"}
    )
    return {candidate.candidate_id: "supported"}


def _select_relation(relation: str) -> CandidateSelector:
    def select(
        candidates: tuple[GmailTemporalVerificationCandidate, ...],
    ) -> dict[str, str]:
        candidate = next(item for item in candidates if item.relation == relation)
        return {candidate.candidate_id: "supported"}

    return select


def _message(
    *,
    order: int,
    provider_message_id: str,
    projection: GmailTemporalReviewProjection,
    internal_at: str,
    start_offset: int,
    document_hash: str = DOCUMENT_HASH,
    source_revision: str = SOURCE_REVISION,
    head_generation: int | None = None,
) -> tuple[GmailTemporalThreadMessageAuthority, GmailTemporalThreadMessageReview]:
    source = GmailTemporalSourceLocator(
        document_id=DOCUMENT,
        document_content_hash=document_hash,
        gmail_account_key=ACCOUNT,
        gmail_thread_id=THREAD,
        gmail_source_revision=source_revision,
        gmail_message_id=provider_message_id,
        message_internal_at=internal_at,
        message_start_offset=start_offset,
        message_end_offset=start_offset + 900,
        source_sha256=projection.source_sha256,
    )
    run_id = f"gtrr_{provider_message_id}"
    return (
        GmailTemporalThreadMessageAuthority(
            version="gmail_temporal_thread_message_authority_v2",
            source=source,
            pipeline_scope=PIPELINE,
            current_review_run_id=run_id,
            current_head_generation=(
                order if head_generation is None else head_generation
            ),
            current_analysis_fingerprint=projection.analysis_fingerprint,
            current_projection_fingerprint=projection.projection_fingerprint,
            current_projection_sha256=hashlib.sha256(
                canonical_gmail_temporal_review_projection_bytes(projection)
            ).hexdigest(),
        ),
        GmailTemporalThreadMessageReview(
            version="gmail_temporal_thread_message_review_v1",
            source=source,
            review_run_id=run_id,
            projection=projection,
        ),
    )


def _snapshot(
    pairs: tuple[
        tuple[GmailTemporalThreadMessageAuthority, GmailTemporalThreadMessageReview],
        ...,
    ],
    *,
    prior_resolution: GmailTemporalEventIdentityResolution | None = None,
) -> tuple[
    GmailTemporalThreadSnapshotAuthority,
    tuple[GmailTemporalThreadMessageReview, ...],
]:
    return (
        GmailTemporalThreadSnapshotAuthority(
            version="gmail_temporal_thread_snapshot_authority_v2",
            messages=tuple(item[0] for item in pairs),
            prior_event_identity_resolution_fingerprint=(
                prior_resolution.resolution_fingerprint
                if prior_resolution is not None
                else None
            ),
        ),
        tuple(item[1] for item in pairs),
    )


def _plan(
    pairs: tuple[
        tuple[GmailTemporalThreadMessageAuthority, GmailTemporalThreadMessageReview],
        ...,
    ],
    *,
    prior_resolution: GmailTemporalEventIdentityResolution | None = None,
) -> GmailTemporalEventIdentityPlan:
    authority, messages = _snapshot(
        pairs,
        prior_resolution=prior_resolution,
    )
    return plan_gmail_temporal_event_identity(
        snapshot_authority=authority,
        messages=messages,
        analysis_authorities=_analysis_authorities(messages),
    )


def _analysis_authorities(
    messages: tuple[GmailTemporalThreadMessageReview, ...],
) -> tuple[TemporalLeadAnalysis, ...]:
    return tuple(
        _ANALYSIS_AUTHORITIES[message.projection.analysis_fingerprint]
        for message in messages
    )


def _source_text_authorities(
    messages: tuple[GmailTemporalThreadMessageReview, ...],
    texts: tuple[str, ...],
) -> tuple[GmailTemporalEventIdentitySourceText, ...]:
    assert len(messages) == len(texts)
    return tuple(
        GmailTemporalEventIdentitySourceText(
            version="gmail_temporal_event_identity_source_text_v1",
            gmail_account_key=message.source.gmail_account_key,
            gmail_thread_id=message.source.gmail_thread_id,
            gmail_message_id=message.source.gmail_message_id,
            source_sha256=message.source.source_sha256,
            text=text,
        )
        for message, text in zip(messages, texts, strict=True)
    )


def _resolve(
    plan: GmailTemporalEventIdentityPlan,
    verdicts: Mapping[str, str],
    *,
    prior_resolution=None,
):
    sets = (
        tuple(
            make_gmail_temporal_event_identity_verdict_set(
                plan=plan,
                run_ordinal=ordinal,
                invocation_id=f"external-event-identity-{ordinal}",
                response_sha256=str(ordinal) * 64,
                verdicts=verdicts,  # type: ignore[arg-type]
            )
            for ordinal in (1, 2, 3)
        )
        if plan.pairs
        else ()
    )
    return resolve_gmail_temporal_event_identity(
        plan=plan,
        verdict_sets=sets,
        prior_resolution=prior_resolution,
    )


def _uniform_verdicts(
    plan: GmailTemporalEventIdentityPlan,
    verdict: str,
) -> dict[str, str]:
    return {item.pair_id: verdict for item in plan.pairs}


def _event_key_for_unit(unit_id: str) -> str:
    material = {
        "version": "gmail_temporal_stable_event_key_v1",
        "anchor_unit_id": unit_id,
    }
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "gmail-event:" + hashlib.sha256(encoded).hexdigest()


def _refingerprint_resolution(
    resolution: GmailTemporalEventIdentityResolution,
) -> GmailTemporalEventIdentityResolution:
    material = asdict(resolution)
    material.pop("resolution_fingerprint")
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return replace(
        resolution,
        resolution_fingerprint="gteirx_" + hashlib.sha256(encoded).hexdigest(),
    )


def _refingerprint_projection(
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
    replacement_types = dict(references)
    alias_references = tuple(
        (mention_id, replacement_types.get(mention_id, mention_type))
        for mention_id, mention_type in hypothesis.subject_alias_type_references
    )
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
        "subject_alias_type_references": alias_references,
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
        subject_alias_type_references=alias_references,
    )


def _forge_resolution_event_key(
    resolution: GmailTemporalEventIdentityResolution,
    event_identity_key: str,
) -> GmailTemporalEventIdentityResolution:
    cluster = next(item for item in resolution.clusters if len(item.unit_ids) > 1)
    forged = replace(
        resolution,
        resolution_fingerprint="",
        clusters=tuple(
            replace(item, event_identity_key=event_identity_key)
            if item == cluster
            else item
            for item in resolution.clusters
        ),
        assertions=tuple(
            replace(assertion, event_identity_key=event_identity_key)
            if assertion.provenance_ref == cluster.provenance_ref
            else assertion
            for assertion in resolution.assertions
        ),
    )
    return _refingerprint_resolution(forged)


def _cluster_receipt(
    resolution: GmailTemporalEventIdentityResolution,
    unit_ids: tuple[str, ...],
    pair_ids: tuple[str, ...],
) -> tuple[str, str]:
    material = {
        "version": "gmail_temporal_event_identity_cluster_v2",
        "plan_fingerprint": resolution.plan_fingerprint,
        "unit_ids": unit_ids,
        "supporting_pair_ids": pair_ids,
        "verdict_set_fingerprints": resolution.verdict_set_fingerprints,
    }
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"gteic_{digest}", f"gteiprov:{digest}"


def _pair_for_orders(
    plan: GmailTemporalEventIdentityPlan,
    left_order: int,
    right_order: int,
) -> GmailTemporalEventIdentityPair:
    units_by_order: dict[int, list[str]] = {}
    for unit in plan.units:
        units_by_order.setdefault(unit.message_order, []).append(unit.unit_id)
    left_ids = units_by_order[left_order]
    right_ids = units_by_order[right_order]
    assert len(left_ids) == len(right_ids) == 1
    endpoints = frozenset((left_ids[0], right_ids[0]))
    return next(
        item
        for item in plan.pairs
        if frozenset((item.left_unit_id, item.right_unit_id)) == endpoints
    )


def _with_assertions(
    messages: tuple[GmailTemporalThreadMessageReview, ...],
    assertions,
) -> tuple[GmailTemporalThreadMessageReview, ...]:
    by_projection: dict[str, list] = {}
    for assertion in assertions:
        by_projection.setdefault(assertion.projection_fingerprint, []).append(assertion)
    return tuple(
        replace(
            message,
            identity_assertions=tuple(
                by_projection.get(message.projection.projection_fingerprint, ())
            ),
        )
        for message in messages
    )


def test_schedule_reschedule_cancel_projects_one_verified_lifecycle() -> None:
    schedule = _projection(
        "The Apollo interview is scheduled for August 14, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-1",
    )
    reschedule = _projection(
        "The Apollo interview was rescheduled from August 14, 2027 to August 16, 2027.",
        _select_exact_reschedule_endpoint,
        internal_at="2027-08-02T09:00:00-07:00",
        chunk_id="message-2",
    )
    cancellation = _projection(
        "The Apollo interview was cancelled on August 16, 2027.",
        _select_lifecycle("cancelled"),
        internal_at="2027-08-16T18:00:00-07:00",
        chunk_id="message-3",
    )
    pairs = (
        _message(
            order=1,
            provider_message_id="message-1",
            projection=schedule,
            internal_at="2027-08-01T09:00:00-07:00",
            start_offset=0,
        ),
        _message(
            order=2,
            provider_message_id="message-2",
            projection=reschedule,
            internal_at="2027-08-02T09:00:00-07:00",
            start_offset=1_000,
        ),
        _message(
            order=3,
            provider_message_id="message-3",
            projection=cancellation,
            internal_at="2027-08-16T18:00:00-07:00",
            start_offset=2_000,
        ),
    )
    plan = _plan(pairs)

    assert len(plan.units) == 4
    assert len(plan.pairs) == 6
    resolution = _resolve(plan, _uniform_verdicts(plan, "same_event"))

    assert len(resolution.clusters) == 1
    assert len(resolution.assertions) == 4
    assert {item.event_identity_key for item in resolution.assertions} == {
        resolution.clusters[0].event_identity_key
    }
    assert {item.provenance_ref for item in resolution.assertions} == {
        resolution.clusters[0].provenance_ref
    }
    assert resolution.independent_invocations_verified is False
    assert resolution.requires_defer is True
    assert resolution.routable is False

    authority, messages = _snapshot(pairs)
    lifecycle = project_gmail_temporal_thread_lifecycle(
        snapshot_authority=authority,
        messages=_with_assertions(messages, resolution.assertions),
        event_identity_analysis_authorities=_analysis_authorities(messages),
        event_identity_plan=plan,
        event_identity_resolution=resolution,
    )
    assert len(lifecycle.events) == 1
    event = lifecycle.events[0]
    assert event.current_status == "cancelled"
    assert [item.normalized_value for item in event.occurrences] == [
        "2027-08-14",
        "2027-08-16",
    ]
    assert [item.state for item in event.occurrences] == [
        "superseded",
        "cancelled",
    ]


@pytest.mark.parametrize(
    ("text", "lifecycle", "expected_subject_type"),
    (
        ("The upload is scheduled for August 18, 2027.", "scheduled", "action"),
        (
            "The arrival was cancelled on August 18, 2027.",
            "cancelled",
            "boundary",
        ),
    ),
)
def test_non_event_temporal_artifacts_are_retained_but_not_identity_units(
    text: str,
    lifecycle: str,
    expected_subject_type: str,
) -> None:
    projection = _projection(
        text,
        _select_lifecycle(lifecycle),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id=f"non-event-{expected_subject_type}",
    )

    assert len(projection.artifacts) == 1
    hypothesis = projection.artifacts[0].hypotheses[0]
    assert hypothesis.subject_type_references == (
        (hypothesis.subject_mention_ids[0], expected_subject_type),
    )
    pairs = (
        _message(
            order=1,
            provider_message_id=f"non-event-{expected_subject_type}",
            projection=projection,
            internal_at="2027-08-01T09:00:00-07:00",
            start_offset=0,
        ),
    )
    plan = _plan(pairs)

    assert plan.units == ()
    assert plan.pairs == ()
    assert plan.pages == ()


def test_normal_interview_event_remains_an_identity_unit() -> None:
    def select_non_deferred_scheduled_event(
        candidates: tuple[GmailTemporalVerificationCandidate, ...],
    ) -> dict[str, str]:
        candidate = next(
            item
            for item in candidates
            if item.lifecycle == "scheduled" and not item.requires_defer
        )
        return {candidate.candidate_id: "supported"}

    projection = _projection(
        "The Apollo interview is scheduled for August 14, 2027.",
        select_non_deferred_scheduled_event,
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="eventhood-interview",
    )
    plan = _plan(
        (
            _message(
                order=1,
                provider_message_id="eventhood-interview",
                projection=projection,
                internal_at="2027-08-01T09:00:00-07:00",
                start_offset=0,
            ),
        ),
    )

    assert projection.artifacts[0].hypotheses[0].subject_type_references[0][1] == (
        "event"
    )
    assert len(plan.units) == 1
    assert (
        plan.units[0].hypothesis_id
        == projection.artifacts[0].hypotheses[0].hypothesis_id
    )


def test_event_identity_unit_consumes_canonical_aliases_without_losing_evidence() -> (
    None
):
    text = "The Northstar design review is scheduled for October 2, 2027."

    def select_generic_event(
        candidates: tuple[GmailTemporalVerificationCandidate, ...],
    ) -> dict[str, str]:
        candidate = next(item for item in candidates if not item.requires_defer)
        return {candidate.candidate_id: "supported"}

    projection = _projection(
        text,
        select_generic_event,
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="eventhood-canonical-title",
    )
    plan = _plan(
        (
            _message(
                order=1,
                provider_message_id="eventhood-canonical-title",
                projection=projection,
                internal_at="2027-08-01T09:00:00-07:00",
                start_offset=0,
            ),
        )
    )
    analysis = _ANALYSIS_AUTHORITIES[projection.analysis_fingerprint]
    surfaces = {
        mention.mention_id: text[mention.start : mention.end]
        for mention in analysis.mentions
    }
    hypothesis = projection.artifacts[0].hypotheses[0]

    assert len(plan.units) == 1
    unit = plan.units[0]
    assert unit.version == "gmail_temporal_event_identity_unit_v3"
    assert plan.version == "gmail_temporal_event_identity_plan_v3"
    assert unit.subject_mention_ids == hypothesis.subject_mention_ids
    assert unit.subject_type_references == hypothesis.subject_type_references
    assert {surfaces[value] for value in unit.subject_mention_ids} == {"review"}
    assert unit.subject_alias_mention_ids == hypothesis.subject_alias_mention_ids
    assert unit.subject_alias_type_references == (
        hypothesis.subject_alias_type_references
    )
    assert {surfaces[value] for value in unit.subject_alias_mention_ids} == {
        "review",
        "Northstar design review",
    }
    assert unit.canonical_subject_mention_id == (
        hypothesis.canonical_subject_mention_id
    )
    assert surfaces[unit.canonical_subject_mention_id] == "Northstar design review"


def test_event_predicate_transition_remains_an_identity_unit() -> None:
    projection = _projection(
        "The benefits policy takes effect on August 18, 2027.",
        _select_relation("occurrence"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="eventhood-transition",
    )
    plan = _plan(
        (
            _message(
                order=1,
                provider_message_id="eventhood-transition",
                projection=projection,
                internal_at="2027-08-01T09:00:00-07:00",
                start_offset=0,
            ),
        )
    )

    assert projection.artifacts[0].hypotheses[0].subject_type_references[0][1] == (
        "event_predicate"
    )
    assert len(plan.units) == 1
    assert (
        plan.units[0].hypothesis_id
        == projection.artifacts[0].hypotheses[0].hypothesis_id
    )


def test_refingerprinted_action_to_event_subject_type_forgery_is_rejected() -> None:
    projection = _projection(
        "The upload is scheduled for August 18, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="eventhood-forged-action",
    )
    assert len(projection.artifacts) == 1
    artifact = projection.artifacts[0]
    hypothesis = artifact.hypotheses[0]
    assert hypothesis.subject_type_references == (
        (hypothesis.subject_mention_ids[0], "action"),
    )
    forged_hypothesis = _with_subject_type_references(
        hypothesis,
        ((hypothesis.subject_mention_ids[0], "event"),),
    )
    forged_projection = _refingerprint_projection(
        replace(
            projection,
            projection_fingerprint="",
            artifacts=(replace(artifact, hypotheses=(forged_hypothesis,)),),
        )
    )
    pairs = (
        _message(
            order=1,
            provider_message_id="eventhood-forged-action",
            projection=forged_projection,
            internal_at="2027-08-01T09:00:00-07:00",
            start_offset=0,
        ),
    )

    with pytest.raises(
        GmailTemporalEventIdentityError,
        match="subject types do not match analysis authority",
    ):
        _plan(pairs)

    assert forged_hypothesis.subject_type_references == (
        (hypothesis.subject_mention_ids[0], "event"),
    )


def test_one_event_unit_resolves_locally_and_projects_a_scheduled_event() -> None:
    schedule = _projection(
        "The dental appointment is scheduled for September 2, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-1",
    )
    pairs = (
        _message(
            order=1,
            provider_message_id="message-1",
            projection=schedule,
            internal_at="2027-08-01T09:00:00-07:00",
            start_offset=0,
        ),
    )
    plan = _plan(pairs)

    assert len(plan.units) == 1
    assert plan.pairs == ()
    assert plan.pages == ()
    resolution = resolve_gmail_temporal_event_identity(
        plan=plan,
        verdict_sets=(),
    )

    assert resolution.verdict_set_fingerprints == ()
    assert resolution.pair_consensus == ()
    assert len(resolution.clusters) == 1
    assert resolution.clusters[0].unit_ids == (plan.units[0].unit_id,)
    assert resolution.clusters[0].supporting_pair_ids == ()
    assert len(resolution.assertions) == 1
    assert resolution.assertions[0].verification == "source_bound_self_identity"

    authority, messages = _snapshot(pairs)
    lifecycle = project_gmail_temporal_thread_lifecycle(
        snapshot_authority=authority,
        messages=_with_assertions(messages, resolution.assertions),
        event_identity_analysis_authorities=_analysis_authorities(messages),
        event_identity_plan=plan,
        event_identity_resolution=resolution,
    )
    assert len(lifecycle.events) == 1
    assert lifecycle.events[0].current_status == "scheduled"
    assert lifecycle.events[0].occurrences[0].normalized_value == "2027-09-02"
    assert lifecycle.unresolved_alternatives == ()

    external_empty_set = make_gmail_temporal_event_identity_verdict_set(
        plan=plan,
        run_ordinal=1,
        invocation_id="unnecessary-external-call",
        response_sha256="1" * 64,
        verdicts={},
    )
    with pytest.raises(GmailTemporalEventIdentityError, match="zero verdict sets"):
        resolve_gmail_temporal_event_identity(
            plan=plan,
            verdict_sets=(external_empty_set,),
        )


def test_source_bound_self_identity_cannot_be_rebound_to_a_foreign_key() -> None:
    schedule = _projection(
        "The dental appointment is scheduled for September 2, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-1",
    )
    pairs = (
        _message(
            order=1,
            provider_message_id="message-1",
            projection=schedule,
            internal_at="2027-08-01T09:00:00-07:00",
            start_offset=0,
        ),
    )
    resolution = resolve_gmail_temporal_event_identity(
        plan=_plan(pairs),
        verdict_sets=(),
    )
    forged = replace(
        resolution.assertions[0],
        event_identity_key="gmail-event:" + "f" * 64,
    )
    authority, messages = _snapshot(pairs)

    with pytest.raises(GmailTemporalThreadLifecycleError, match="source-bound"):
        project_gmail_temporal_thread_lifecycle(
            snapshot_authority=authority,
            messages=_with_assertions(messages, (forged,)),
        )


def test_one_terminal_unit_gets_self_identity_but_not_invented_history() -> None:
    cancellation = _projection(
        "The dentist appointment scheduled for August 14, 2027 was cancelled.",
        _select_lifecycle("cancelled"),
        internal_at="2027-08-15T09:00:00-07:00",
        chunk_id="message-1",
    )
    pairs = (
        _message(
            order=1,
            provider_message_id="message-1",
            projection=cancellation,
            internal_at="2027-08-15T09:00:00-07:00",
            start_offset=0,
        ),
    )
    plan = _plan(pairs)
    resolution = resolve_gmail_temporal_event_identity(
        plan=plan,
        verdict_sets=(),
    )
    authority, messages = _snapshot(pairs)

    lifecycle = project_gmail_temporal_thread_lifecycle(
        snapshot_authority=authority,
        messages=_with_assertions(messages, resolution.assertions),
        event_identity_analysis_authorities=_analysis_authorities(messages),
        event_identity_plan=plan,
        event_identity_resolution=resolution,
    )

    assert len(lifecycle.events) == 1
    assert lifecycle.events[0].current_status == "unresolved"
    assert lifecycle.events[0].occurrences == ()
    assert [item.reason for item in lifecycle.unresolved_alternatives] == [
        "terminal_transition_lacks_current_scheduled_occurrence"
    ]


def test_same_title_distinct_events_do_not_merge() -> None:
    first = _projection(
        "The Apollo interview is scheduled for August 14, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-1",
    )
    second = _projection(
        "The Apollo interview is scheduled for September 14, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-02T09:00:00-07:00",
        chunk_id="message-2",
    )
    pairs = (
        _message(
            order=1,
            provider_message_id="message-1",
            projection=first,
            internal_at="2027-08-01T09:00:00-07:00",
            start_offset=0,
        ),
        _message(
            order=2,
            provider_message_id="message-2",
            projection=second,
            internal_at="2027-08-02T09:00:00-07:00",
            start_offset=1_000,
        ),
    )
    plan = _plan(pairs)

    resolution = _resolve(plan, _uniform_verdicts(plan, "different_event"))

    assert [item.consensus for item in resolution.pair_consensus] == ["different_event"]
    assert len(resolution.clusters) == 2
    assert all(len(item.unit_ids) == 1 for item in resolution.clusters)
    assert len({item.event_identity_key for item in resolution.clusters}) == 2
    assert len(resolution.assertions) == 2
    assert {item.verification for item in resolution.assertions} == {
        "source_bound_self_identity"
    }

    authority, messages = _snapshot(pairs)
    lifecycle = project_gmail_temporal_thread_lifecycle(
        snapshot_authority=authority,
        messages=_with_assertions(messages, resolution.assertions),
        event_identity_analysis_authorities=_analysis_authorities(messages),
        event_identity_plan=plan,
        event_identity_resolution=resolution,
    )
    assert len(lifecycle.events) == 2
    assert {item.current_status for item in lifecycle.events} == {"scheduled"}
    assert {item.occurrences[0].normalized_value for item in lifecycle.events} == {
        "2027-08-14",
        "2027-09-14",
    }


def test_non_clique_transitive_links_fail_closed_including_same_message_pair() -> None:
    two_events = _projection(
        (
            "The Apollo interview is scheduled for August 14, 2027. "
            "The Apollo interview is scheduled for August 16, 2027."
        ),
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-1",
    )
    cancellation = _projection(
        "The Apollo interview was cancelled on August 16, 2027.",
        _select_lifecycle("cancelled"),
        internal_at="2027-08-02T09:00:00-07:00",
        chunk_id="message-2",
    )
    plan = _plan(
        (
            _message(
                order=1,
                provider_message_id="message-1",
                projection=two_events,
                internal_at="2027-08-01T09:00:00-07:00",
                start_offset=0,
            ),
            _message(
                order=2,
                provider_message_id="message-2",
                projection=cancellation,
                internal_at="2027-08-02T09:00:00-07:00",
                start_offset=1_000,
            ),
        )
    )
    assert [item.message_order for item in plan.units].count(1) == 2
    assert len(plan.pairs) == 3
    same_message_ids = {item.unit_id for item in plan.units if item.message_order == 1}
    verdicts = _uniform_verdicts(plan, "same_event")
    same_message_pair = next(
        item
        for item in plan.pairs
        if {item.left_unit_id, item.right_unit_id} == same_message_ids
    )
    verdicts[same_message_pair.pair_id] = "different_event"

    resolution = _resolve(plan, verdicts)

    assert resolution.clusters == ()
    assert resolution.assertions == ()
    assert len(resolution.reviews) == 1
    assert set(resolution.reviews[0].unit_ids) == {item.unit_id for item in plan.units}


def test_event_key_and_unit_ids_survive_append_and_thread_revision() -> None:
    unrelated = _projection(
        "The budget review is scheduled for July 30, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-07-30T09:00:00-07:00",
        chunk_id="message-1",
    )
    schedule = _projection(
        "The Apollo interview is scheduled for August 14, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-2",
    )
    cancellation = _projection(
        "The Apollo interview was cancelled on August 14, 2027.",
        _select_lifecycle("cancelled"),
        internal_at="2027-08-02T09:00:00-07:00",
        chunk_id="message-3",
    )
    initial_pairs = (
        _message(
            order=1,
            provider_message_id="message-2",
            projection=schedule,
            internal_at="2027-08-01T09:00:00-07:00",
            start_offset=1_000,
        ),
        _message(
            order=2,
            provider_message_id="message-3",
            projection=cancellation,
            internal_at="2027-08-02T09:00:00-07:00",
            start_offset=2_000,
        ),
    )
    initial_plan = _plan(initial_pairs)
    initial_resolution = _resolve(
        initial_plan,
        _uniform_verdicts(initial_plan, "same_event"),
    )

    revised_hash = "a" * 64
    revised_revision = "b" * 64
    expanded_pairs = (
        _message(
            order=1,
            provider_message_id="message-1",
            projection=unrelated,
            internal_at="2027-07-30T09:00:00-07:00",
            start_offset=0,
            document_hash=revised_hash,
            source_revision=revised_revision,
            head_generation=10,
        ),
        _message(
            order=2,
            provider_message_id="message-2",
            projection=schedule,
            internal_at="2027-08-01T09:00:00-07:00",
            start_offset=1_000,
            document_hash=revised_hash,
            source_revision=revised_revision,
            head_generation=11,
        ),
        _message(
            order=3,
            provider_message_id="message-3",
            projection=cancellation,
            internal_at="2027-08-02T09:00:00-07:00",
            start_offset=2_000,
            document_hash=revised_hash,
            source_revision=revised_revision,
            head_generation=12,
        ),
    )
    expanded_plan = _plan(expanded_pairs)
    expanded_verdicts = _uniform_verdicts(expanded_plan, "different_event")
    expanded_verdicts[_pair_for_orders(expanded_plan, 2, 3).pair_id] = "same_event"
    expanded_resolution = _resolve(expanded_plan, expanded_verdicts)

    initial_unit_ids = {item.unit_id for item in initial_plan.units}
    expanded_event_unit_ids = {
        item.unit_id for item in expanded_plan.units if item.message_order in {2, 3}
    }
    assert expanded_event_unit_ids == initial_unit_ids
    assert expanded_plan.thread_authority_fingerprint != (
        initial_plan.thread_authority_fingerprint
    )
    assert {item.unit_authority_fingerprint for item in expanded_plan.units} != {
        item.unit_authority_fingerprint for item in initial_plan.units
    }
    expanded_event_cluster = next(
        item for item in expanded_resolution.clusters if len(item.unit_ids) == 2
    )
    assert expanded_event_cluster.event_identity_key == (
        initial_resolution.clusters[0].event_identity_key
    )


def test_prior_singleton_key_survives_later_same_event_join() -> None:
    schedule = _projection(
        "The Apollo interview is scheduled for August 14, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-1",
    )
    initial_pairs = (
        _message(
            order=1,
            provider_message_id="message-1",
            projection=schedule,
            internal_at="2027-08-01T09:00:00-07:00",
            start_offset=0,
        ),
    )
    initial_plan = _plan(initial_pairs)
    prior = resolve_gmail_temporal_event_identity(
        plan=initial_plan,
        verdict_sets=(),
    )
    prior_key = prior.clusters[0].event_identity_key

    cancellation = _projection(
        "The Apollo interview was cancelled on August 14, 2027.",
        _select_lifecycle("cancelled"),
        internal_at="2027-08-02T09:00:00-07:00",
        chunk_id="message-2",
    )
    expanded_pairs = (
        *initial_pairs,
        _message(
            order=2,
            provider_message_id="message-2",
            projection=cancellation,
            internal_at="2027-08-02T09:00:00-07:00",
            start_offset=1_000,
        ),
    )
    expanded_plan = _plan(expanded_pairs, prior_resolution=prior)
    updated = _resolve(
        expanded_plan,
        _uniform_verdicts(expanded_plan, "same_event"),
        prior_resolution=prior,
    )

    assert len(updated.clusters) == 1
    assert len(updated.clusters[0].unit_ids) == 2
    assert updated.clusters[0].event_identity_key == prior_key
    assert {item.verification for item in updated.assertions} == {"external_verified"}
    authority, messages = _snapshot(expanded_pairs, prior_resolution=prior)
    lifecycle = project_gmail_temporal_thread_lifecycle(
        snapshot_authority=authority,
        messages=_with_assertions(messages, updated.assertions),
        event_identity_analysis_authorities=_analysis_authorities(messages),
        event_identity_plan=expanded_plan,
        event_identity_resolution=updated,
        event_identity_prior_resolution=prior,
    )
    assert lifecycle.events[0].current_status == "cancelled"


def test_uncertain_append_preserves_prior_singleton_and_new_unit_stays_unresolved() -> (
    None
):
    schedule = _projection(
        "The Apollo interview is scheduled for August 14, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-1",
    )
    initial_pairs = (
        _message(
            order=1,
            provider_message_id="message-1",
            projection=schedule,
            internal_at="2027-08-01T09:00:00-07:00",
            start_offset=0,
        ),
    )
    prior = resolve_gmail_temporal_event_identity(
        plan=_plan(initial_pairs),
        verdict_sets=(),
    )
    cancellation = _projection(
        "The Apollo interview was cancelled on August 14, 2027.",
        _select_lifecycle("cancelled"),
        internal_at="2027-08-02T09:00:00-07:00",
        chunk_id="message-2",
    )
    expanded_pairs = (
        *initial_pairs,
        _message(
            order=2,
            provider_message_id="message-2",
            projection=cancellation,
            internal_at="2027-08-02T09:00:00-07:00",
            start_offset=1_000,
        ),
    )
    expanded_plan = _plan(expanded_pairs, prior_resolution=prior)
    pair_id = expanded_plan.pairs[0].pair_id
    verdict_sets = tuple(
        make_gmail_temporal_event_identity_verdict_set(
            plan=expanded_plan,
            run_ordinal=ordinal,
            invocation_id=f"external-uncertain-{ordinal}",
            response_sha256=str(ordinal) * 64,
            verdicts={pair_id: "same_event" if ordinal < 3 else "different_event"},
        )
        for ordinal in (1, 2, 3)
    )

    updated = resolve_gmail_temporal_event_identity(
        plan=expanded_plan,
        verdict_sets=verdict_sets,
        prior_resolution=prior,
    )

    assert [item.consensus for item in updated.pair_consensus] == ["uncertain"]
    assert len(updated.clusters) == 2
    assert all(len(item.unit_ids) == 1 for item in updated.clusters)
    prior_unit_id = prior.clusters[0].unit_ids[0]
    retained = next(item for item in updated.clusters if prior_unit_id in item.unit_ids)
    assert retained.event_identity_key == prior.clusters[0].event_identity_key
    assert len(updated.assertions) == 2
    assert {item.verification for item in updated.assertions} == {
        "source_bound_self_identity"
    }

    authority, messages = _snapshot(expanded_pairs, prior_resolution=prior)
    lifecycle = project_gmail_temporal_thread_lifecycle(
        snapshot_authority=authority,
        messages=_with_assertions(messages, updated.assertions),
        event_identity_analysis_authorities=_analysis_authorities(messages),
        event_identity_plan=expanded_plan,
        event_identity_resolution=updated,
        event_identity_prior_resolution=prior,
    )
    assert len(lifecycle.events) == 2
    assert sorted(item.current_status for item in lifecycle.events) == [
        "scheduled",
        "unresolved",
    ]
    assert [item.reason for item in lifecycle.unresolved_alternatives] == [
        "terminal_transition_lacks_current_scheduled_occurrence"
    ]

    replay = resolve_gmail_temporal_event_identity(
        plan=expanded_plan,
        verdict_sets=verdict_sets,
        prior_resolution=prior,
    )
    assert replay == updated

    joined_plan = _plan(expanded_pairs, prior_resolution=updated)
    joined = _resolve(
        joined_plan,
        _uniform_verdicts(joined_plan, "same_event"),
        prior_resolution=updated,
    )
    assert len(joined.clusters) == 1
    assert len(joined.clusters[0].unit_ids) == 2
    assert joined.clusters[0].event_identity_key == (
        prior.clusters[0].event_identity_key
    )


def test_later_clique_cannot_merge_two_prior_multi_unit_event_keys() -> None:
    projections = (
        _projection(
            "The Apollo interview is scheduled for August 14, 2027.",
            _select_lifecycle("scheduled"),
            internal_at="2027-08-01T09:00:00-07:00",
            chunk_id="message-1",
        ),
        _projection(
            "The Apollo interview was cancelled on August 14, 2027.",
            _select_lifecycle("cancelled"),
            internal_at="2027-08-02T09:00:00-07:00",
            chunk_id="message-2",
        ),
        _projection(
            "The budget review is scheduled for September 14, 2027.",
            _select_lifecycle("scheduled"),
            internal_at="2027-08-03T09:00:00-07:00",
            chunk_id="message-3",
        ),
        _projection(
            "The budget review was cancelled on September 14, 2027.",
            _select_lifecycle("cancelled"),
            internal_at="2027-08-04T09:00:00-07:00",
            chunk_id="message-4",
        ),
    )
    pairs = tuple(
        _message(
            order=index,
            provider_message_id=f"message-{index}",
            projection=projection,
            internal_at=f"2027-08-0{index}T09:00:00-07:00",
            start_offset=(index - 1) * 1_000,
        )
        for index, projection in enumerate(projections, start=1)
    )
    plan = _plan(pairs)
    prior_verdicts = _uniform_verdicts(plan, "different_event")
    prior_verdicts[_pair_for_orders(plan, 1, 2).pair_id] = "same_event"
    prior_verdicts[_pair_for_orders(plan, 3, 4).pair_id] = "same_event"
    prior = _resolve(plan, prior_verdicts)
    assert len(prior.clusters) == 2
    assert all(len(item.unit_ids) == 2 for item in prior.clusters)

    updated_plan = _plan(pairs, prior_resolution=prior)
    with pytest.raises(GmailTemporalEventIdentityError, match="multiple prior"):
        _resolve(
            updated_plan,
            _uniform_verdicts(updated_plan, "same_event"),
            prior_resolution=prior,
        )


def test_prior_resolution_preserves_key_when_older_same_event_message_arrives() -> None:
    later_schedule = _projection(
        "The Apollo interview is scheduled for August 14, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-2",
    )
    cancellation = _projection(
        "The Apollo interview was cancelled on August 14, 2027.",
        _select_lifecycle("cancelled"),
        internal_at="2027-08-02T09:00:00-07:00",
        chunk_id="message-3",
    )
    initial_plan = _plan(
        (
            _message(
                order=1,
                provider_message_id="message-2",
                projection=later_schedule,
                internal_at="2027-08-01T09:00:00-07:00",
                start_offset=1_000,
            ),
            _message(
                order=2,
                provider_message_id="message-3",
                projection=cancellation,
                internal_at="2027-08-02T09:00:00-07:00",
                start_offset=2_000,
            ),
        )
    )
    prior = _resolve(initial_plan, _uniform_verdicts(initial_plan, "same_event"))

    delayed_schedule = _projection(
        "The Apollo interview is scheduled for August 14, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-07-30T09:00:00-07:00",
        chunk_id="message-1",
    )
    revised_hash = "a" * 64
    revised_revision = "b" * 64
    expanded_pairs = (
        _message(
            order=1,
            provider_message_id="message-1",
            projection=delayed_schedule,
            internal_at="2027-07-30T09:00:00-07:00",
            start_offset=0,
            document_hash=revised_hash,
            source_revision=revised_revision,
        ),
        _message(
            order=2,
            provider_message_id="message-2",
            projection=later_schedule,
            internal_at="2027-08-01T09:00:00-07:00",
            start_offset=1_000,
            document_hash=revised_hash,
            source_revision=revised_revision,
        ),
        _message(
            order=3,
            provider_message_id="message-3",
            projection=cancellation,
            internal_at="2027-08-02T09:00:00-07:00",
            start_offset=2_000,
            document_hash=revised_hash,
            source_revision=revised_revision,
        ),
    )
    expanded_plan = _plan(expanded_pairs, prior_resolution=prior)
    updated = _resolve(
        expanded_plan,
        _uniform_verdicts(expanded_plan, "same_event"),
        prior_resolution=prior,
    )

    assert (
        updated.clusters[0].event_identity_key == prior.clusters[0].event_identity_key
    )
    assert updated.clusters[0].event_identity_anchor_unit_id == (
        prior.clusters[0].event_identity_anchor_unit_id
    )
    assert updated.prior_resolution_fingerprint == prior.resolution_fingerprint
    with pytest.raises(
        GmailTemporalEventIdentityError,
        match="requires its exact prior authority",
    ):
        validate_gmail_temporal_event_identity_resolution(
            plan=expanded_plan,
            resolution=updated,
        )
    validate_gmail_temporal_event_identity_resolution(
        plan=expanded_plan,
        resolution=updated,
        prior_resolution=prior,
    )

    third_plan = _plan(expanded_pairs, prior_resolution=updated)
    third = _resolve(
        third_plan,
        _uniform_verdicts(third_plan, "same_event"),
        prior_resolution=updated,
    )
    assert third.clusters[0].event_identity_key == (
        prior.clusters[0].event_identity_key
    )
    assert third.clusters[0].event_identity_anchor_unit_id == (
        prior.clusters[0].event_identity_anchor_unit_id
    )


def test_refingerprinted_same_cluster_member_key_substitution_is_rejected() -> None:
    schedule = _projection(
        "The Apollo interview is scheduled for August 14, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-1",
    )
    cancellation = _projection(
        "The Apollo interview was cancelled on August 14, 2027.",
        _select_lifecycle("cancelled"),
        internal_at="2027-08-02T09:00:00-07:00",
        chunk_id="message-2",
    )
    pairs = (
        _message(
            order=1,
            provider_message_id="message-1",
            projection=schedule,
            internal_at="2027-08-01T09:00:00-07:00",
            start_offset=0,
        ),
        _message(
            order=2,
            provider_message_id="message-2",
            projection=cancellation,
            internal_at="2027-08-02T09:00:00-07:00",
            start_offset=1_000,
        ),
    )
    plan = _plan(pairs)
    resolution = _resolve(plan, _uniform_verdicts(plan, "same_event"))
    cluster = resolution.clusters[0]
    substitute_anchor = next(
        unit_id
        for unit_id in cluster.unit_ids
        if unit_id != cluster.event_identity_anchor_unit_id
    )
    substitute_key = _event_key_for_unit(substitute_anchor)
    forged = _refingerprint_resolution(
        replace(
            resolution,
            clusters=(
                replace(
                    cluster,
                    event_identity_anchor_unit_id=substitute_anchor,
                    event_identity_key=substitute_key,
                ),
            ),
            assertions=tuple(
                replace(assertion, event_identity_key=substitute_key)
                for assertion in resolution.assertions
            ),
        )
    )

    with pytest.raises(
        GmailTemporalEventIdentityError,
        match="event anchor is not canonical or prior-derived",
    ):
        validate_gmail_temporal_event_identity_resolution(
            plan=plan,
            resolution=forged,
        )

    with pytest.raises(
        GmailTemporalEventIdentityError,
        match="does not match the trusted thread authority",
    ):
        authorized_plan = _plan(pairs, prior_resolution=resolution)
        _resolve(
            authorized_plan,
            _uniform_verdicts(authorized_plan, "same_event"),
            prior_resolution=forged,
        )


@pytest.mark.parametrize("foreign_key_source", ["arbitrary", "unclustered_unit"])
def test_fabricated_prior_resolution_cannot_inject_a_foreign_event_key(
    foreign_key_source: str,
) -> None:
    schedule = _projection(
        "The Apollo interview is scheduled for August 14, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-1",
    )
    cancellation = _projection(
        "The Apollo interview was cancelled on August 14, 2027.",
        _select_lifecycle("cancelled"),
        internal_at="2027-08-02T09:00:00-07:00",
        chunk_id="message-2",
    )
    unrelated = _projection(
        "The budget review is scheduled for September 2, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-03T09:00:00-07:00",
        chunk_id="message-3",
    )
    pairs = (
        _message(
            order=1,
            provider_message_id="message-1",
            projection=schedule,
            internal_at="2027-08-01T09:00:00-07:00",
            start_offset=0,
        ),
        _message(
            order=2,
            provider_message_id="message-2",
            projection=cancellation,
            internal_at="2027-08-02T09:00:00-07:00",
            start_offset=1_000,
        ),
        _message(
            order=3,
            provider_message_id="message-3",
            projection=unrelated,
            internal_at="2027-08-03T09:00:00-07:00",
            start_offset=2_000,
        ),
    )
    plan = _plan(pairs)
    verdicts = _uniform_verdicts(plan, "different_event")
    verdicts[_pair_for_orders(plan, 1, 2).pair_id] = "same_event"
    prior = _resolve(plan, verdicts)
    unrelated_unit_id = next(
        unit.unit_id for unit in plan.units if unit.message_order == 3
    )
    foreign_key = (
        "gmail-event:" + "f" * 64
        if foreign_key_source == "arbitrary"
        else _event_key_for_unit(unrelated_unit_id)
    )
    forged_prior = _forge_resolution_event_key(prior, foreign_key)

    with pytest.raises(
        GmailTemporalEventIdentityError,
        match="does not match the trusted thread authority",
    ):
        authorized_plan = _plan(pairs, prior_resolution=prior)
        _resolve(
            authorized_plan,
            verdicts,
            prior_resolution=forged_prior,
        )


@pytest.mark.parametrize("forgery", ["omit_clique_member", "replace_pair_id"])
def test_fabricated_prior_resolution_cannot_rewrite_cluster_topology(
    forgery: str,
) -> None:
    projections = (
        _projection(
            "The Apollo interview is scheduled for August 14, 2027.",
            _select_lifecycle("scheduled"),
            internal_at="2027-08-01T09:00:00-07:00",
            chunk_id="message-1",
        ),
        _projection(
            "The Apollo interview was cancelled on August 14, 2027.",
            _select_lifecycle("cancelled"),
            internal_at="2027-08-02T09:00:00-07:00",
            chunk_id="message-2",
        ),
        _projection(
            "The Apollo interview is scheduled for August 16, 2027.",
            _select_lifecycle("scheduled"),
            internal_at="2027-08-03T09:00:00-07:00",
            chunk_id="message-3",
        ),
    )
    pairs = tuple(
        _message(
            order=index,
            provider_message_id=f"message-{index}",
            projection=projection,
            internal_at=f"2027-08-0{index}T09:00:00-07:00",
            start_offset=(index - 1) * 1_000,
        )
        for index, projection in enumerate(projections, start=1)
    )
    plan = _plan(pairs)
    verdicts = _uniform_verdicts(plan, "same_event")
    prior = _resolve(plan, verdicts)
    cluster = prior.clusters[0]

    if forgery == "omit_clique_member":
        forged_unit_ids = cluster.unit_ids[:2]
        forged_pair_ids = tuple(
            pair.pair_id
            for pair in plan.pairs
            if {pair.left_unit_id, pair.right_unit_id} == set(forged_unit_ids)
        )
        forged_consensus = prior.pair_consensus
    else:
        replaced_pair_id = "gteipair_" + "f" * 64
        original_pair_id = cluster.supporting_pair_ids[0]
        forged_unit_ids = cluster.unit_ids
        forged_pair_ids = tuple(
            replaced_pair_id if pair_id == original_pair_id else pair_id
            for pair_id in cluster.supporting_pair_ids
        )
        forged_consensus = tuple(
            replace(item, pair_id=replaced_pair_id)
            if item.pair_id == original_pair_id
            else item
            for item in prior.pair_consensus
        )
    cluster_id, provenance_ref = _cluster_receipt(
        prior,
        forged_unit_ids,
        forged_pair_ids,
    )
    forged_cluster = replace(
        cluster,
        cluster_id=cluster_id,
        unit_ids=forged_unit_ids,
        supporting_pair_ids=forged_pair_ids,
        provenance_ref=provenance_ref,
    )
    forged = _refingerprint_resolution(
        replace(
            prior,
            resolution_fingerprint="",
            pair_consensus=forged_consensus,
            clusters=(forged_cluster,),
            assertions=tuple(
                replace(
                    assertion,
                    event_identity_key=forged_cluster.event_identity_key,
                    provenance_ref=provenance_ref,
                )
                for assertion in prior.assertions[: len(forged_unit_ids)]
            ),
        )
    )

    authorized_plan = _plan(pairs, prior_resolution=prior)
    with pytest.raises(
        GmailTemporalEventIdentityError,
        match="does not match the trusted thread authority",
    ):
        _resolve(authorized_plan, verdicts, prior_resolution=forged)


def test_deadline_hypotheses_are_excluded_and_bounds_are_consistent() -> None:
    deadline = _projection(
        "Please submit the application by August 14, 2027.",
        _select_relation("deadline"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="deadline-1",
    )
    plan = _plan(
        (
            _message(
                order=1,
                provider_message_id="deadline-1",
                projection=deadline,
                internal_at="2027-08-01T09:00:00-07:00",
                start_offset=0,
            ),
        )
    )

    assert plan.units == ()
    assert plan.pairs == ()
    resolution = resolve_gmail_temporal_event_identity(
        plan=plan,
        verdict_sets=(),
    )
    assert resolution.verdict_set_fingerprints == ()
    assert resolution.clusters == ()
    assert resolution.assertions == ()
    assert MAX_EVENT_IDENTITY_PAIRS == (
        MAX_EVENT_IDENTITY_UNITS * (MAX_EVENT_IDENTITY_UNITS - 1) // 2
    )


def test_incomplete_stale_and_conflicting_external_sets_fail_closed() -> None:
    first = _projection(
        "The Apollo interview is scheduled for August 14, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-1",
    )
    second = _projection(
        "The Apollo interview was cancelled on August 14, 2027.",
        _select_lifecycle("cancelled"),
        internal_at="2027-08-02T09:00:00-07:00",
        chunk_id="message-2",
    )
    plan = _plan(
        (
            _message(
                order=1,
                provider_message_id="message-1",
                projection=first,
                internal_at="2027-08-01T09:00:00-07:00",
                start_offset=0,
            ),
            _message(
                order=2,
                provider_message_id="message-2",
                projection=second,
                internal_at="2027-08-02T09:00:00-07:00",
                start_offset=1_000,
            ),
        )
    )
    pair_id = plan.pairs[0].pair_id

    with pytest.raises(GmailTemporalEventIdentityError, match="coverage"):
        make_gmail_temporal_event_identity_verdict_set(
            plan=plan,
            run_ordinal=1,
            invocation_id="external-1",
            response_sha256="1" * 64,
            verdicts={},
        )

    sets = tuple(
        make_gmail_temporal_event_identity_verdict_set(
            plan=plan,
            run_ordinal=ordinal,
            invocation_id=f"external-{ordinal}",
            response_sha256=str(ordinal) * 64,
            verdicts={pair_id: "same_event" if ordinal < 3 else "different_event"},
        )
        for ordinal in (1, 2, 3)
    )
    conflicting = resolve_gmail_temporal_event_identity(
        plan=plan,
        verdict_sets=sets,
    )
    assert conflicting.pair_consensus[0].consensus == "uncertain"
    assert len(conflicting.clusters) == 2
    assert all(len(item.unit_ids) == 1 for item in conflicting.clusters)
    assert len(conflicting.assertions) == 2
    assert {item.verification for item in conflicting.assertions} == {
        "source_bound_self_identity"
    }

    with pytest.raises(GmailTemporalEventIdentityError, match="stale"):
        resolve_gmail_temporal_event_identity(
            plan=plan,
            verdict_sets=(replace(sets[0], plan_fingerprint="gteip_stale"), *sets[1:]),
        )
    with pytest.raises(GmailTemporalEventIdentityError, match="exactly three"):
        resolve_gmail_temporal_event_identity(
            plan=plan,
            verdict_sets=sets[:2],
        )


def test_c2_pair_request_uses_full_title_without_rewriting_selected_evidence() -> None:
    text = (
        "The Lumen Quay planning session has been rescheduled to "
        "September 22, 2027 from September 19, 2027. "
        "The room is unchanged."
    )

    def select_generic_reschedule_endpoint(
        candidates: tuple[GmailTemporalVerificationCandidate, ...],
    ) -> dict[str, str]:
        candidate = next(
            item
            for item in candidates
            if item.lifecycle in {"rescheduled_old", "rescheduled_replacement"}
            and not item.requires_defer
        )
        return {candidate.candidate_id: "supported"}

    projection = _projection(
        text,
        select_generic_reschedule_endpoint,
        internal_at="2027-09-01T09:00:00-07:00",
        chunk_id="consumer-c2",
    )
    pairs = (
        _message(
            order=1,
            provider_message_id="consumer-c2",
            projection=projection,
            internal_at="2027-09-01T09:00:00-07:00",
            start_offset=0,
        ),
    )
    snapshot, messages = _snapshot(pairs)
    analyses = _analysis_authorities(messages)
    plan = plan_gmail_temporal_event_identity(
        snapshot_authority=snapshot,
        messages=messages,
        analysis_authorities=analyses,
    )
    source_texts = _source_text_authorities(messages, (text,))

    surfaces = bind_gmail_temporal_event_identity_unit_surfaces(
        snapshot_authority=snapshot,
        messages=messages,
        analysis_authorities=analyses,
        plan=plan,
        source_texts=source_texts,
    )

    assert len(surfaces.units) == 2
    for unit in surfaces.units:
        assert {item.surface for item in unit.verifier_selected_evidence} == {"session"}
        assert {item.surface for item in unit.deterministic_identity_aliases} == {
            "session",
            "Lumen Quay planning session",
        }
        assert unit.canonical_identity is not None
        assert unit.canonical_identity.surface == "Lumen Quay planning session"

    requests = build_gmail_temporal_event_identity_pair_requests(
        snapshot_authority=snapshot,
        messages=messages,
        analysis_authorities=analyses,
        plan=plan,
        source_texts=source_texts,
    )

    assert len(requests) == 1
    payload = json.loads(requests[0].payload)
    assert payload["version"] == "gmail_temporal_event_identity_pair_request_v1"
    assert payload["request_fingerprint"] == requests[0].request_fingerprint
    assert len(payload["pairs"]) == 1
    assert len(payload["units"]) == 2
    for unit in payload["units"]:
        assert {item["surface"] for item in unit["verifier_selected_evidence"]} == {
            "session"
        }
        metadata = unit["deterministic_identity_metadata"]
        assert metadata["authority"] == ("source_verified_non_authorizing_alias_family")
        assert metadata["canonical_full_title"]["surface"] == (
            "Lumen Quay planning session"
        )


def test_surface_adapter_rejects_source_hash_span_and_plan_tampering() -> None:
    text = "The Northstar design review is scheduled for October 2, 2027."

    def select_generic_event(
        candidates: tuple[GmailTemporalVerificationCandidate, ...],
    ) -> dict[str, str]:
        candidate = next(item for item in candidates if not item.requires_defer)
        return {candidate.candidate_id: "supported"}

    projection = _projection(
        text,
        select_generic_event,
        internal_at="2027-09-01T09:00:00-07:00",
        chunk_id="consumer-tamper",
    )
    pairs = (
        _message(
            order=1,
            provider_message_id="consumer-tamper",
            projection=projection,
            internal_at="2027-09-01T09:00:00-07:00",
            start_offset=0,
        ),
    )
    snapshot, messages = _snapshot(pairs)
    analyses = _analysis_authorities(messages)
    plan = plan_gmail_temporal_event_identity(
        snapshot_authority=snapshot,
        messages=messages,
        analysis_authorities=analyses,
    )
    source_texts = _source_text_authorities(messages, (text,))

    with pytest.raises(
        GmailTemporalEventIdentityConsumerError,
        match="source text",
    ):
        bind_gmail_temporal_event_identity_unit_surfaces(
            snapshot_authority=snapshot,
            messages=messages,
            analysis_authorities=analyses,
            plan=plan,
            source_texts=(replace(source_texts[0], text=text + " altered"),),
        )

    with pytest.raises(
        GmailTemporalEventIdentityConsumerError,
        match="source text",
    ):
        bind_gmail_temporal_event_identity_unit_surfaces(
            snapshot_authority=snapshot,
            messages=messages,
            analysis_authorities=analyses,
            plan=plan,
            source_texts=(replace(source_texts[0], source_sha256="0" * 64),),
        )

    canonical_id = plan.units[0].canonical_subject_mention_id
    assert canonical_id is not None
    mention_index = next(
        index
        for index, mention in enumerate(analyses[0].mentions)
        if mention.mention_id == canonical_id
    )
    changed_mentions = list(analyses[0].mentions)
    changed_mentions[mention_index] = replace(
        changed_mentions[mention_index],
        start=changed_mentions[mention_index].start + 1,
    )
    with pytest.raises(
        GmailTemporalEventIdentityConsumerError,
        match="source authority",
    ):
        bind_gmail_temporal_event_identity_unit_surfaces(
            snapshot_authority=snapshot,
            messages=messages,
            analysis_authorities=(
                replace(analyses[0], mentions=tuple(changed_mentions)),
            ),
            plan=plan,
            source_texts=source_texts,
        )

    with pytest.raises(
        GmailTemporalEventIdentityConsumerError,
        match="current thread inputs",
    ):
        bind_gmail_temporal_event_identity_unit_surfaces(
            snapshot_authority=snapshot,
            messages=messages,
            analysis_authorities=analyses,
            plan=replace(plan, plan_fingerprint="gteip_stale"),
            source_texts=source_texts,
        )


def test_missing_canonical_exports_no_generic_retrieval_alias() -> None:
    text = "The meeting is scheduled for October 2, 2027."

    def select_generic_event(
        candidates: tuple[GmailTemporalVerificationCandidate, ...],
    ) -> dict[str, str]:
        candidate = next(item for item in candidates if not item.requires_defer)
        return {candidate.candidate_id: "supported"}

    projection = _projection(
        text,
        select_generic_event,
        internal_at="2027-09-01T09:00:00-07:00",
        chunk_id="consumer-no-canonical",
    )
    pairs = (
        _message(
            order=1,
            provider_message_id="consumer-no-canonical",
            projection=projection,
            internal_at="2027-09-01T09:00:00-07:00",
            start_offset=0,
        ),
    )
    snapshot, messages = _snapshot(pairs)
    analyses = _analysis_authorities(messages)
    plan = plan_gmail_temporal_event_identity(
        snapshot_authority=snapshot,
        messages=messages,
        analysis_authorities=analyses,
    )
    source_texts = _source_text_authorities(messages, (text,))
    surfaces = bind_gmail_temporal_event_identity_unit_surfaces(
        snapshot_authority=snapshot,
        messages=messages,
        analysis_authorities=analyses,
        plan=plan,
        source_texts=source_texts,
    )
    resolution = _resolve(plan, {})

    assert len(surfaces.units) == 1
    assert surfaces.units[0].canonical_identity is None
    assert (
        build_gmail_temporal_verified_event_bindings(
            snapshot_authority=snapshot,
            messages=messages,
            analysis_authorities=analyses,
            plan=plan,
            resolution=resolution,
            source_texts=source_texts,
        )
        == ()
    )


def test_distinct_named_events_remain_isolated_in_retrieval_bindings() -> None:
    texts = (
        "The Northstar design review is scheduled for October 2, 2027.",
        "The Lumen Quay planning session is scheduled for October 3, 2027.",
    )

    def select_generic_event(
        candidates: tuple[GmailTemporalVerificationCandidate, ...],
    ) -> dict[str, str]:
        candidate = next(item for item in candidates if not item.requires_defer)
        return {candidate.candidate_id: "supported"}

    projections = tuple(
        _projection(
            text,
            select_generic_event,
            internal_at=f"2027-09-0{index}T09:00:00-07:00",
            chunk_id=f"consumer-distinct-{index}",
        )
        for index, text in enumerate(texts, start=1)
    )
    pairs = tuple(
        _message(
            order=index,
            provider_message_id=f"consumer-distinct-{index}",
            projection=projection,
            internal_at=f"2027-09-0{index}T09:00:00-07:00",
            start_offset=(index - 1) * 1_000,
        )
        for index, projection in enumerate(projections, start=1)
    )
    snapshot, messages = _snapshot(pairs)
    analyses = _analysis_authorities(messages)
    plan = plan_gmail_temporal_event_identity(
        snapshot_authority=snapshot,
        messages=messages,
        analysis_authorities=analyses,
    )
    resolution = _resolve(plan, _uniform_verdicts(plan, "different_event"))
    bindings = build_gmail_temporal_verified_event_bindings(
        snapshot_authority=snapshot,
        messages=messages,
        analysis_authorities=analyses,
        plan=plan,
        resolution=resolution,
        source_texts=_source_text_authorities(messages, texts),
    )

    assert len(bindings) == 2
    assert {item.aliases for item in bindings} == {
        ("Northstar design review",),
        ("Lumen Quay planning session",),
    }
    assert all("review" not in item.aliases for item in bindings)
    assert all("session" not in item.aliases for item in bindings)
    assert len({item.event_identity_key for item in bindings}) == 2


def test_c4_canonical_binding_matches_retrieval_query_and_attaches_context() -> None:
    text = "The Northstar design review is scheduled for October 2, 2027."

    def select_generic_event(
        candidates: tuple[GmailTemporalVerificationCandidate, ...],
    ) -> dict[str, str]:
        candidate = next(item for item in candidates if not item.requires_defer)
        return {candidate.candidate_id: "supported"}

    projection = _projection(
        text,
        select_generic_event,
        internal_at="2027-09-01T09:00:00-07:00",
        chunk_id="consumer-c4",
    )
    pairs = (
        _message(
            order=1,
            provider_message_id="consumer-c4",
            projection=projection,
            internal_at="2027-09-01T09:00:00-07:00",
            start_offset=0,
        ),
    )
    snapshot, messages = _snapshot(pairs)
    analyses = _analysis_authorities(messages)
    plan = plan_gmail_temporal_event_identity(
        snapshot_authority=snapshot,
        messages=messages,
        analysis_authorities=analyses,
    )
    resolution = _resolve(plan, {})
    bindings = build_gmail_temporal_verified_event_bindings(
        snapshot_authority=snapshot,
        messages=messages,
        analysis_authorities=analyses,
        plan=plan,
        resolution=resolution,
        source_texts=_source_text_authorities(messages, (text,)),
    )

    assert len(bindings) == 1
    assert bindings[0].aliases == ("Northstar design review",)
    evidence = (
        GmailTemporalThreadEvidence(
            evidence_id="northstar-anchor",
            gmail_account_scope_id="account-a",
            gmail_provider_thread_id="northstar-thread",
            available_at="2027-09-01T12:00:00Z",
            message_ordinal=1,
            text=(
                "Subject: Northstar design review\n\n"
                "Northstar design review was booked for October 2, 2027."
            ),
            verified_event_bindings=bindings,
        ),
        GmailTemporalThreadEvidence(
            evidence_id="northstar-update",
            gmail_account_scope_id="account-a",
            gmail_provider_thread_id="northstar-thread",
            available_at="2027-09-02T12:00:00Z",
            message_ordinal=2,
            text=(
                "Subject: Northstar design review\n\n"
                "Northstar design review was cancelled."
            ),
            verified_event_bindings=bindings,
        ),
        GmailTemporalThreadEvidence(
            evidence_id="noise",
            gmail_account_scope_id="account-a",
            gmail_provider_thread_id="noise-thread",
            available_at="2027-09-02T12:00:00Z",
            message_ordinal=1,
            text="Subject: Routine note\n\nGeneral information.",
        ),
    )
    retrieval = plan_gmail_temporal_thread_retrieval_experiment(
        query="What is the latest status of the Northstar design review?",
        temporal_intent="lifecycle",
        source_available_as_of="2027-10-31T00:00:00Z",
        baseline_ranked_evidence_ids=("northstar-anchor", "noise"),
        evidence_sources=evidence,
    )

    assert retrieval.target_event_identity_key == bindings[0].event_identity_key
    assert retrieval.verified_context_evidence_ids == ("northstar-update",)
