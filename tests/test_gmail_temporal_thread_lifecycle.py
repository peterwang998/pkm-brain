from __future__ import annotations

from dataclasses import replace
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
from pkm_brain.gmail_temporal_persistence import GmailTemporalSourceLocator
from pkm_brain.gmail_temporal_review import (
    GmailTemporalReviewBatchResult,
    GmailTemporalReviewProjection,
    project_gmail_temporal_review,
)
from pkm_brain.gmail_temporal_thread_lifecycle import (
    GmailTemporalEventIdentityAssertion,
    GmailTemporalThreadLifecycleError,
    GmailTemporalThreadMessageAuthority,
    GmailTemporalThreadMessageReview,
    GmailTemporalThreadSnapshotAuthority,
    canonical_gmail_temporal_thread_lifecycle_projection_bytes,
    gmail_temporal_thread_lifecycle_projection_payload,
    project_gmail_temporal_thread_lifecycle,
)


ACCOUNT = "personal@example.test"
THREAD = "thread-1"
DOCUMENT = "doc-gmail-current"
DOCUMENT_HASH = "d" * 64
SOURCE_REVISION = "e" * 64
PIPELINE = "gmail_temporal_review_v1"
EVENT_KEY = "event:apollo-interview"

CandidateSelector = Callable[
    [tuple[GmailTemporalVerificationCandidate, ...]],
    dict[str, str],
]


def _verdict_rows(
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


def _ensemble(
    *,
    analysis: TemporalLeadAnalysis,
    batch: GmailTemporalSelectorBatch,
    page_plan: GmailTemporalCandidatePagePlan,
    overrides: dict[str, str],
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


def _message(
    *,
    index: int,
    projection: GmailTemporalReviewProjection,
    internal_at: str,
    event_keys: tuple[str, ...] = (EVENT_KEY,),
    verification: str = "external_verified",
) -> tuple[GmailTemporalThreadMessageAuthority, GmailTemporalThreadMessageReview]:
    source = GmailTemporalSourceLocator(
        document_id=DOCUMENT,
        document_content_hash=DOCUMENT_HASH,
        gmail_account_key=ACCOUNT,
        gmail_thread_id=THREAD,
        gmail_source_revision=SOURCE_REVISION,
        gmail_message_id=f"message-{index}",
        message_internal_at=internal_at,
        message_start_offset=(index - 1) * 1_000,
        message_end_offset=(index - 1) * 1_000 + 900,
        source_sha256=projection.source_sha256,
    )
    run_id = f"gtrr_message_{index}"
    assertions = tuple(
        GmailTemporalEventIdentityAssertion(
            version="gmail_temporal_event_identity_assertion_v1",
            projection_fingerprint=projection.projection_fingerprint,
            artifact_id=artifact.artifact_id,
            hypothesis_id=hypothesis.hypothesis_id,
            event_identity_key=event_key,
            verification=verification,  # type: ignore[arg-type]
            provenance_ref=f"receipt:{index}:{key_index}",
        )
        for artifact in projection.artifacts
        for hypothesis in artifact.hypotheses
        for key_index, event_key in enumerate(event_keys, start=1)
    )
    return (
        GmailTemporalThreadMessageAuthority(
            version="gmail_temporal_thread_message_authority_v1",
            source=source,
            pipeline_scope=PIPELINE,
            current_review_run_id=run_id,
            current_head_generation=index,
        ),
        GmailTemporalThreadMessageReview(
            version="gmail_temporal_thread_message_review_v1",
            source=source,
            review_run_id=run_id,
            projection=projection,
            identity_assertions=assertions,
        ),
    )


def _project(
    pairs: tuple[
        tuple[GmailTemporalThreadMessageAuthority, GmailTemporalThreadMessageReview],
        ...,
    ],
):
    return project_gmail_temporal_thread_lifecycle(
        snapshot_authority=GmailTemporalThreadSnapshotAuthority(
            version="gmail_temporal_thread_snapshot_authority_v1",
            messages=tuple(item[0] for item in pairs),
        ),
        messages=tuple(item[1] for item in pairs),
    )


@pytest.mark.parametrize("terminal", ("cancelled", "completed"))
def test_schedule_reschedule_terminal_retains_every_occurrence_and_artifact(
    terminal: str,
) -> None:
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
    terminal_projection = _projection(
        f"The Apollo interview was {terminal} on August 16, 2027.",
        _select_lifecycle(terminal),
        internal_at="2027-08-16T18:00:00-07:00",
        chunk_id="message-3",
    )
    pairs = (
        _message(
            index=1,
            projection=schedule,
            internal_at="2027-08-01T09:00:00-07:00",
        ),
        _message(
            index=2,
            projection=reschedule,
            internal_at="2027-08-02T09:00:00-07:00",
        ),
        _message(
            index=3,
            projection=terminal_projection,
            internal_at="2027-08-16T18:00:00-07:00",
        ),
    )

    result = _project(pairs)

    assert len(result.events) == 1
    event = result.events[0]
    assert (event.entity_type, event.current_status) == ("event", terminal)
    assert event.last_unambiguous_status == terminal
    assert [item.normalized_value for item in event.occurrences] == [
        "2027-08-14",
        "2027-08-16",
    ]
    assert [item.state for item in event.occurrences] == ["superseded", terminal]
    assert (
        event.occurrences[0].superseded_by_occurrence_id
        == event.occurrences[1].occurrence_id
    )
    assert event.current_occurrence_id == event.occurrences[1].occurrence_id
    assert result.unresolved_alternatives == ()
    assert tuple(item.projection for item in result.source_messages) == (
        schedule,
        reschedule,
        terminal_projection,
    )
    assert sum(
        len(item.projection.artifacts) for item in result.source_messages
    ) == sum(
        len(item.artifacts) for item in (schedule, reschedule, terminal_projection)
    )
    assert result.routable is False
    assert event.routable is False
    assert all(item.routable is False for item in event.occurrences)
    assert canonical_gmail_temporal_thread_lifecycle_projection_bytes(result) == (
        canonical_gmail_temporal_thread_lifecycle_projection_bytes(result)
    )
    assert (
        gmail_temporal_thread_lifecycle_projection_payload(result)[
            "projection_fingerprint"
        ]
        == result.projection_fingerprint
    )


def test_same_thread_and_date_without_verified_identity_never_merge() -> None:
    first = _projection(
        "The Apollo interview is scheduled for August 14, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-1",
    )
    second = _projection(
        "The Apollo interview is scheduled for August 14, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-02T09:00:00-07:00",
        chunk_id="message-2",
    )
    pair_one = _message(
        index=1,
        projection=first,
        internal_at="2027-08-01T09:00:00-07:00",
        event_keys=(),
    )
    pair_two = _message(
        index=2,
        projection=second,
        internal_at="2027-08-02T09:00:00-07:00",
        event_keys=(),
    )

    result = _project((pair_one, pair_two))

    assert result.events == ()
    assert [item.reason for item in result.unresolved_alternatives] == [
        "stable_event_identity_missing",
        "stable_event_identity_missing",
    ]
    assert all(
        item.possible_event_identity_keys == ()
        for item in result.unresolved_alternatives
    )


def test_unverified_identity_is_preserved_but_never_authorizes_reconciliation() -> None:
    projection = _projection(
        "The Apollo interview is scheduled for August 14, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-1",
    )
    pair = _message(
        index=1,
        projection=projection,
        internal_at="2027-08-01T09:00:00-07:00",
        verification="unverified",
    )

    result = _project((pair,))

    assert result.events == ()
    assert len(result.source_messages[0].identity_assertions) == 1
    assert (
        result.unresolved_alternatives[0].reason == "stable_event_identity_unverified"
    )


def test_unverified_later_message_cannot_change_a_verified_event_state() -> None:
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

    result = _project(
        (
            _message(
                index=1,
                projection=schedule,
                internal_at="2027-08-01T09:00:00-07:00",
            ),
            _message(
                index=2,
                projection=cancellation,
                internal_at="2027-08-02T09:00:00-07:00",
                verification="unverified",
            ),
        )
    )

    assert result.events[0].current_status == "scheduled"
    assert result.events[0].occurrences[0].state == "scheduled"
    assert (
        result.unresolved_alternatives[0].reason == "stable_event_identity_unverified"
    )


def test_conflicting_verified_identity_assertions_stay_unresolved() -> None:
    projection = _projection(
        "The Apollo interview is scheduled for August 14, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-1",
    )
    pair = _message(
        index=1,
        projection=projection,
        internal_at="2027-08-01T09:00:00-07:00",
        event_keys=("event:apollo-a", "event:apollo-b"),
    )

    result = _project((pair,))

    assert result.events == ()
    assert len(result.unresolved_alternatives) == 1
    alternative = result.unresolved_alternatives[0]
    assert alternative.reason == "conflicting_verified_event_identity"
    assert alternative.possible_event_identity_keys == (
        "event:apollo-a",
        "event:apollo-b",
    )


def test_different_schedule_without_explicit_reschedule_does_not_overwrite() -> None:
    first = _projection(
        "The Apollo interview is scheduled for August 14, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-1",
    )
    second = _projection(
        "The Apollo interview is scheduled for August 16, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-02T09:00:00-07:00",
        chunk_id="message-2",
    )

    result = _project(
        (
            _message(
                index=1,
                projection=first,
                internal_at="2027-08-01T09:00:00-07:00",
            ),
            _message(
                index=2,
                projection=second,
                internal_at="2027-08-02T09:00:00-07:00",
            ),
        )
    )

    event = result.events[0]
    assert event.current_status == "unresolved"
    assert event.last_unambiguous_status == "scheduled"
    assert len(event.occurrences) == 1
    assert event.occurrences[0].normalized_value == "2027-08-14"
    assert result.unresolved_alternatives[0].reason == (
        "schedule_change_requires_explicit_reschedule"
    )


def test_stale_head_cross_thread_and_missing_chronology_fail_closed() -> None:
    projection = _projection(
        "The Apollo interview is scheduled for August 14, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-1",
    )
    authority, message = _message(
        index=1,
        projection=projection,
        internal_at="2027-08-01T09:00:00-07:00",
    )
    snapshot = GmailTemporalThreadSnapshotAuthority(
        version="gmail_temporal_thread_snapshot_authority_v1",
        messages=(authority,),
    )

    with pytest.raises(GmailTemporalThreadLifecycleError, match="ledger head"):
        project_gmail_temporal_thread_lifecycle(
            snapshot_authority=snapshot,
            messages=(replace(message, review_run_id="gtrr_stale"),),
        )

    other_thread_source = replace(message.source, gmail_thread_id="thread-2")
    with pytest.raises(GmailTemporalThreadLifecycleError, match="cross-thread"):
        project_gmail_temporal_thread_lifecycle(
            snapshot_authority=snapshot,
            messages=(replace(message, source=other_thread_source),),
        )

    second_projection = _projection(
        "The Apollo interview was cancelled on August 14, 2027.",
        _select_lifecycle("cancelled"),
        internal_at="2027-08-02T09:00:00-07:00",
        chunk_id="message-2",
    )
    second_pair = _message(
        index=2,
        projection=second_projection,
        internal_at="2027-08-02T09:00:00-07:00",
    )
    with pytest.raises(GmailTemporalThreadLifecycleError, match="chronology"):
        _project((second_pair, (authority, message)))
