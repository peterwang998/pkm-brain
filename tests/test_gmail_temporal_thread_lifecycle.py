from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from datetime import datetime, timezone
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
from pkm_brain.gmail_temporal_persistence import (
    GmailTemporalSourceLocator,
    gmail_temporal_message_scope_key,
)
from pkm_brain.gmail_temporal_event_identity import (
    bind_gmail_temporal_event_identity_resolution,
    make_gmail_temporal_event_identity_verdict_set,
    plan_gmail_temporal_event_identity,
    resolve_gmail_temporal_event_identity,
)
from pkm_brain.gmail_temporal_review import (
    GmailTemporalReviewBatchResult,
    GmailTemporalReviewProjection,
    canonical_gmail_temporal_review_projection_bytes,
    project_gmail_temporal_review,
)
from pkm_brain.gmail_temporal_thread_lifecycle import (
    GmailTemporalEventIdentityAssertion,
    GmailTemporalThreadLifecycleError,
    GmailTemporalThreadMessageAuthority,
    GmailTemporalThreadMessageReview,
    GmailTemporalThreadSnapshotAuthority,
    canonical_gmail_temporal_thread_lifecycle_projection_bytes,
    gmail_temporal_event_identity_unit_id,
    gmail_temporal_source_bound_event_identity_key,
    gmail_temporal_source_bound_self_provenance,
    gmail_temporal_thread_lifecycle_projection_payload,
    project_gmail_temporal_thread_lifecycle,
    validate_gmail_temporal_thread_review_inputs,
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
_ANALYSIS_AUTHORITIES: dict[str, TemporalLeadAnalysis] = {}


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


def _select_first_supported(
    candidates: tuple[GmailTemporalVerificationCandidate, ...],
) -> dict[str, str]:
    return {candidates[0].candidate_id: "supported"}


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

    def unit_id(artifact, hypothesis) -> str:
        return gmail_temporal_event_identity_unit_id(
            source_anchor={
                "gmail_account_key": source.gmail_account_key,
                "gmail_thread_id": source.gmail_thread_id,
                "gmail_message_id": source.gmail_message_id,
                "source_sha256": source.source_sha256,
            },
            artifact=artifact,
            hypothesis=hypothesis,
        )

    assertions = []
    for artifact in projection.artifacts:
        for hypothesis in artifact.hypotheses:
            source_unit_id = unit_id(artifact, hypothesis)
            for key_index, requested_event_key in enumerate(event_keys, start=1):
                event_key = requested_event_key
                provenance_ref = f"receipt:{index}:{key_index}"
                if verification == "source_bound_self_identity":
                    event_key = gmail_temporal_source_bound_event_identity_key(
                        source_unit_id
                    )
                    provenance_ref = gmail_temporal_source_bound_self_provenance(
                        source_unit_id
                    )
                assertions.append(
                    GmailTemporalEventIdentityAssertion(
                        version="gmail_temporal_event_identity_assertion_v2",
                        unit_id=source_unit_id,
                        projection_fingerprint=projection.projection_fingerprint,
                        artifact_id=artifact.artifact_id,
                        hypothesis_id=hypothesis.hypothesis_id,
                        event_identity_key=event_key,
                        verification=verification,  # type: ignore[arg-type]
                        provenance_ref=provenance_ref,
                    )
                )
    return (
        GmailTemporalThreadMessageAuthority(
            version="gmail_temporal_thread_message_authority_v2",
            source=source,
            pipeline_scope=PIPELINE,
            current_review_run_id=run_id,
            current_head_generation=index,
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
            identity_assertions=tuple(assertions),
        ),
    )


def _refingerprinted_projection(
    projection: GmailTemporalReviewProjection,
) -> GmailTemporalReviewProjection:
    material = asdict(projection)
    material.pop("projection_fingerprint")
    return replace(
        projection,
        projection_fingerprint="gtrp_"
        + hashlib.sha256(
            json.dumps(
                material,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
    )


def _forge_reschedule_roles(
    projection: GmailTemporalReviewProjection,
    *,
    roles: tuple[str, ...],
    missing_roles: tuple[str, ...] = (),
    reasons: tuple[str, ...] = (),
) -> GmailTemporalReviewProjection:
    group = next(item for item in projection.groups if item.kind == "reschedule")
    group_material = {
        "version": "gmail_temporal_review_group_v1",
        "analysis_fingerprint": projection.analysis_fingerprint,
        "kind": group.kind,
        "source_start": group.source_start,
        "source_end": group.source_end,
        "members": [
            {
                "expression_id": member.expression_id,
                "role": role,
                "source_order": member.source_order,
            }
            for member, role in zip(group.members, roles, strict=True)
        ],
        "missing_roles": missing_roles,
        "conflict_reasons": (),
    }
    group_id = (
        "gtrg_"
        + hashlib.sha256(
            json.dumps(
                group_material,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    )
    members = tuple(
        replace(
            member,
            member_id="gtrgm_"
            + hashlib.sha256(
                json.dumps(
                    {
                        "version": "gmail_temporal_review_group_member_v1",
                        "group_id": group_id,
                        "expression_id": member.expression_id,
                        "role": role,
                        "source_order": member.source_order,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
            role=role,  # type: ignore[arg-type]
        )
        for member, role in zip(group.members, roles, strict=True)
    )
    forged_group = replace(
        group,
        group_id=group_id,
        members=members,
        reasons=reasons,
    )
    return _refingerprinted_projection(
        replace(
            projection,
            groups=tuple(
                forged_group if item.group_id == group.group_id else item
                for item in projection.groups
            ),
        )
    )


def _project(
    pairs: tuple[
        tuple[GmailTemporalThreadMessageAuthority, GmailTemporalThreadMessageReview],
        ...,
    ],
):
    authority = GmailTemporalThreadSnapshotAuthority(
        version="gmail_temporal_thread_snapshot_authority_v2",
        messages=tuple(item[0] for item in pairs),
    )
    messages = tuple(item[1] for item in pairs)
    validate_gmail_temporal_thread_review_inputs(
        snapshot_authority=authority,
        messages=messages,
    )
    resolver_assertions = tuple(
        assertion
        for message in messages
        for assertion in message.identity_assertions
        if assertion.verification in {"source_bound_self_identity", "external_verified"}
    )
    if not resolver_assertions:
        return project_gmail_temporal_thread_lifecycle(
            snapshot_authority=authority,
            messages=messages,
        )
    keys_by_unit: dict[str, set[str]] = {}
    for assertion in resolver_assertions:
        keys_by_unit.setdefault(assertion.unit_id, set()).add(
            assertion.event_identity_key
        )
    if any(len(keys) != 1 for keys in keys_by_unit.values()) or any(
        assertion.verification
        not in {"source_bound_self_identity", "external_verified"}
        for message in messages
        for assertion in message.identity_assertions
    ):
        return project_gmail_temporal_thread_lifecycle(
            snapshot_authority=authority,
            messages=messages,
        )

    bare_messages = tuple(
        replace(message, identity_assertions=()) for message in messages
    )
    analysis_authorities = tuple(
        _ANALYSIS_AUTHORITIES[message.projection.analysis_fingerprint]
        for message in bare_messages
    )
    plan = plan_gmail_temporal_event_identity(
        snapshot_authority=authority,
        messages=bare_messages,
        analysis_authorities=analysis_authorities,
    )
    verdicts = {
        pair.pair_id: (
            "same_event"
            if keys_by_unit.get(pair.left_unit_id)
            == keys_by_unit.get(pair.right_unit_id)
            and keys_by_unit.get(pair.left_unit_id) is not None
            else "different_event"
        )
        for pair in plan.pairs
    }
    verdict_sets = (
        tuple(
            make_gmail_temporal_event_identity_verdict_set(
                plan=plan,
                run_ordinal=ordinal,
                invocation_id=f"lifecycle-test-{ordinal}",
                response_sha256=str(ordinal) * 64,
                verdicts=verdicts,
            )
            for ordinal in (1, 2, 3)
        )
        if plan.pairs
        else ()
    )
    resolution = resolve_gmail_temporal_event_identity(
        plan=plan,
        verdict_sets=verdict_sets,
    )
    bound_messages = bind_gmail_temporal_event_identity_resolution(
        snapshot_authority=authority,
        messages=bare_messages,
        analysis_authorities=analysis_authorities,
        plan=plan,
        resolution=resolution,
    )
    return project_gmail_temporal_thread_lifecycle(
        snapshot_authority=authority,
        messages=bound_messages,
        event_identity_analysis_authorities=analysis_authorities,
        event_identity_plan=plan,
        event_identity_resolution=resolution,
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
        _select_exact_reschedule_endpoint,
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


@pytest.mark.parametrize(
    "guarded_text",
    (
        (
            "The Apollo interview was rescheduled from August 14, 2027 to "
            "August 16, 2027 or conceivably August 18, 2027."
        ),
        (
            "The Apollo interview was rescheduled from August 14, 2027 to "
            "August 16, 2027 or 18."
        ),
        (
            "The Apollo interview was rescheduled from August 14, 2027 to "
            "August 16, 2027 or the 18th."
        ),
        (
            "The Apollo interview was rescheduled from August 14, 2027 to "
            "August 16 or 18, 2027."
        ),
        (
            "The Apollo interview was rescheduled from August 14, 2027 to "
            "August 16, 2027 / August 18, 2027."
        ),
        (
            "The Apollo interview was moved to August 16, 2027 or the 18th "
            "from August 14, 2027."
        ),
    ),
)
def test_conflicted_reschedule_guard_never_changes_the_current_occurrence(
    guarded_text: str,
) -> None:
    schedule = _projection(
        "The Apollo interview is scheduled for August 14, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-1",
    )
    guarded = _projection(
        guarded_text,
        _select_lifecycle("unknown"),
        internal_at="2027-08-02T09:00:00-07:00",
        chunk_id="message-2",
    )
    guarded_group = next(
        group for group in guarded.groups if group.kind == "reschedule"
    )
    assert guarded_group.coverage == "conflicted"
    guarded_roles = {member.role for member in guarded_group.members}
    assert "unresolved" in guarded_roles
    assert "rescheduled_replacement" not in guarded_roles

    result = _project(
        (
            _message(
                index=1,
                projection=schedule,
                internal_at="2027-08-01T09:00:00-07:00",
            ),
            _message(
                index=2,
                projection=guarded,
                internal_at="2027-08-02T09:00:00-07:00",
            ),
        )
    )

    assert len(result.events) == 1
    event = result.events[0]
    assert event.current_status == "scheduled"
    assert event.last_unambiguous_status == "scheduled"
    assert len(event.occurrences) == 1
    assert event.occurrences[0].normalized_value == "2027-08-14"
    assert event.occurrences[0].state == "scheduled"
    assert event.occurrences[0].superseded_by_occurrence_id is None
    assert result.unresolved_alternatives[-1].reason == (
        "incomplete_or_conflicted_message_group"
    )


def test_trusted_receipt_rejects_fully_refingerprinted_reschedule_role_flip() -> None:
    projection = _projection(
        "The Apollo interview was rescheduled from August 14, 2027 to August 16, 2027.",
        _select_exact_reschedule_endpoint,
        internal_at="2027-08-02T09:00:00-07:00",
        chunk_id="message-1",
    )
    authority, message = _message(
        index=1,
        projection=projection,
        internal_at="2027-08-02T09:00:00-07:00",
    )
    forged = _forge_reschedule_roles(
        projection,
        roles=("rescheduled_replacement", "rescheduled_old"),
    )
    forged_bytes = canonical_gmail_temporal_review_projection_bytes(forged)
    assert forged_bytes != canonical_gmail_temporal_review_projection_bytes(projection)

    receipt = replace(
        authority,
        current_projection_fingerprint=forged.projection_fingerprint,
    )
    with pytest.raises(
        GmailTemporalThreadLifecycleError,
        match="does not match the current ledger receipt",
    ):
        validate_gmail_temporal_thread_review_inputs(
            snapshot_authority=GmailTemporalThreadSnapshotAuthority(
                version="gmail_temporal_thread_snapshot_authority_v2",
                messages=(receipt,),
            ),
            messages=(replace(message, projection=forged),),
        )


def test_trusted_receipt_rejects_refingerprinted_missing_reschedule_role_flip() -> None:
    projection = _projection(
        "The Apollo interview was postponed until August 16, 2027.",
        _select_first_supported,
        internal_at="2027-08-02T09:00:00-07:00",
        chunk_id="message-1",
    )
    authority, message = _message(
        index=1,
        projection=projection,
        internal_at="2027-08-02T09:00:00-07:00",
    )
    forged = _forge_reschedule_roles(
        projection,
        roles=("rescheduled_old",),
        missing_roles=("rescheduled_replacement",),
        reasons=("rescheduled_replacement_missing_from_source",),
    )
    forged_bytes = canonical_gmail_temporal_review_projection_bytes(forged)
    assert forged_bytes != canonical_gmail_temporal_review_projection_bytes(projection)

    receipt = replace(
        authority,
        current_projection_fingerprint=forged.projection_fingerprint,
    )
    with pytest.raises(
        GmailTemporalThreadLifecycleError,
        match="does not match the current ledger receipt",
    ):
        validate_gmail_temporal_thread_review_inputs(
            snapshot_authority=GmailTemporalThreadSnapshotAuthority(
                version="gmail_temporal_thread_snapshot_authority_v2",
                messages=(receipt,),
            ),
            messages=(replace(message, projection=forged),),
        )


def test_refingerprinted_caller_mutation_cannot_replace_derived_lifecycle() -> None:
    projection = _projection(
        "The Apollo interview is scheduled for August 14, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-1",
    )
    result = _project(
        (
            _message(
                index=1,
                projection=projection,
                internal_at="2027-08-01T09:00:00-07:00",
            ),
        )
    )
    forged = replace(
        result,
        events=(replace(result.events[0], current_status="cancelled"),),
    )
    material = asdict(forged)
    material.pop("projection_fingerprint")
    forged = replace(
        forged,
        projection_fingerprint="gtlp_"
        + hashlib.sha256(
            json.dumps(
                material,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest(),
    )

    with pytest.raises(
        GmailTemporalThreadLifecycleError,
        match="does not match its deterministic derivation",
    ):
        gmail_temporal_thread_lifecycle_projection_payload(forged)


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


def test_mixed_manual_verified_and_unverified_assertions_require_exact_binding() -> (
    None
):
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
            index=1,
            projection=schedule,
            internal_at="2027-08-01T09:00:00-07:00",
            verification="source_bound_self_identity",
        ),
        _message(
            index=2,
            projection=cancellation,
            internal_at="2027-08-02T09:00:00-07:00",
            verification="unverified",
        ),
    )

    with pytest.raises(
        GmailTemporalThreadLifecycleError,
        match="complete plan and resolution authority",
    ):
        _project(pairs)


def test_caller_authored_external_identity_assertions_fail_without_resolver_authority() -> (
    None
):
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

    with pytest.raises(
        GmailTemporalThreadLifecycleError,
        match="complete plan and resolution authority",
    ):
        _project((pair,))


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
        version="gmail_temporal_thread_snapshot_authority_v2",
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
