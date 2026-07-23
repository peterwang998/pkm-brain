from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
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
from pkm_brain.gmail_temporal_persistence import GmailTemporalSourceLocator
from pkm_brain.gmail_temporal_review import (
    GmailTemporalReviewBatchResult,
    GmailTemporalReviewProjection,
    project_gmail_temporal_review,
)
from pkm_brain.gmail_temporal_thread_lifecycle import (
    GmailTemporalThreadMessageAuthority,
    GmailTemporalThreadMessageReview,
    GmailTemporalThreadSnapshotAuthority,
    project_gmail_temporal_thread_lifecycle,
)


ACCOUNT = "personal@example.test"
THREAD = "thread-event-identity"
DOCUMENT = "doc-gmail-current"
DOCUMENT_HASH = "d" * 64
SOURCE_REVISION = "e" * 64
PIPELINE = "gmail_temporal_review_v1"

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
        message_internal_at=internal_at,
        fact_admitted=True,
        chunk_id=chunk_id,
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
    return project_gmail_temporal_review(
        text=text,
        analysis=analysis,
        batch_plan=batch_plan,
        batch_results=tuple(results),
    )


def _select_lifecycle(lifecycle: str) -> CandidateSelector:
    def select(
        candidates: tuple[GmailTemporalVerificationCandidate, ...],
    ) -> dict[str, str]:
        candidate = next(item for item in candidates if item.lifecycle == lifecycle)
        return {candidate.candidate_id: "supported"}

    return select


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
            version="gmail_temporal_thread_message_authority_v1",
            source=source,
            pipeline_scope=PIPELINE,
            current_review_run_id=run_id,
            current_head_generation=(
                order if head_generation is None else head_generation
            ),
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
) -> tuple[
    GmailTemporalThreadSnapshotAuthority,
    tuple[GmailTemporalThreadMessageReview, ...],
]:
    return (
        GmailTemporalThreadSnapshotAuthority(
            version="gmail_temporal_thread_snapshot_authority_v1",
            messages=tuple(item[0] for item in pairs),
        ),
        tuple(item[1] for item in pairs),
    )


def _plan(
    pairs: tuple[
        tuple[GmailTemporalThreadMessageAuthority, GmailTemporalThreadMessageReview],
        ...,
    ],
) -> GmailTemporalEventIdentityPlan:
    authority, messages = _snapshot(pairs)
    return plan_gmail_temporal_event_identity(
        snapshot_authority=authority,
        messages=messages,
    )


def _resolve(
    plan: GmailTemporalEventIdentityPlan,
    verdicts: Mapping[str, str],
    *,
    prior_resolution=None,
):
    sets = tuple(
        make_gmail_temporal_event_identity_verdict_set(
            plan=plan,
            run_ordinal=ordinal,
            invocation_id=f"external-event-identity-{ordinal}",
            response_sha256=str(ordinal) * 64,
            verdicts=verdicts,  # type: ignore[arg-type]
        )
        for ordinal in (1, 2, 3)
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


def _forge_resolution_event_key(
    resolution: GmailTemporalEventIdentityResolution,
    event_identity_key: str,
) -> GmailTemporalEventIdentityResolution:
    assert len(resolution.clusters) == 1
    forged = replace(
        resolution,
        resolution_fingerprint="",
        clusters=(
            replace(
                resolution.clusters[0],
                event_identity_key=event_identity_key,
            ),
        ),
        assertions=tuple(
            replace(assertion, event_identity_key=event_identity_key)
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
        "version": "gmail_temporal_event_identity_cluster_v1",
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
        _select_lifecycle("unknown"),
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

    resolution = _resolve(plan, _uniform_verdicts(plan, "different_event"))

    assert [item.consensus for item in resolution.pair_consensus] == ["different_event"]
    assert resolution.clusters == ()
    assert resolution.assertions == ()


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
    assert expanded_resolution.clusters[0].event_identity_key == (
        initial_resolution.clusters[0].event_identity_key
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
    expanded_plan = _plan(
        (
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
    )
    updated = _resolve(
        expanded_plan,
        _uniform_verdicts(expanded_plan, "same_event"),
        prior_resolution=prior,
    )

    assert (
        updated.clusters[0].event_identity_key == prior.clusters[0].event_identity_key
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
    plan = _plan(
        (
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
    )
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
        match="event key is not anchored",
    ):
        _resolve(plan, verdicts, prior_resolution=forged_prior)


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
    plan = _plan(
        tuple(
            _message(
                order=index,
                provider_message_id=f"message-{index}",
                projection=projection,
                internal_at=f"2027-08-0{index}T09:00:00-07:00",
                start_offset=(index - 1) * 1_000,
            )
            for index, projection in enumerate(projections, start=1)
        )
    )
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

    with pytest.raises(GmailTemporalEventIdentityError, match="topology"):
        _resolve(plan, verdicts, prior_resolution=forged)


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
    assert conflicting.assertions == ()

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
