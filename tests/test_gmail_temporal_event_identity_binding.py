from __future__ import annotations

from dataclasses import replace

import pytest

from pkm_brain.gmail_temporal_event_identity import (
    GmailTemporalEventIdentityError,
    GmailTemporalEventIdentityPlan,
    GmailTemporalEventIdentityResolution,
    bind_gmail_temporal_event_identity_resolution,
    plan_gmail_temporal_event_identity,
    validate_gmail_temporal_event_identity_resolution,
)
from pkm_brain.gmail_temporal_thread_lifecycle import (
    GmailTemporalThreadLifecycleError,
    GmailTemporalThreadMessageReview,
    GmailTemporalThreadSnapshotAuthority,
    project_gmail_temporal_thread_lifecycle,
)
from test_gmail_temporal_event_identity import (
    _analysis_authorities,
    _message,
    _plan,
    _projection,
    _refingerprint_projection,
    _refingerprint_resolution,
    _resolve,
    _select_lifecycle,
    _snapshot,
    _uniform_verdicts,
    _with_assertions,
    _with_subject_type_references,
)


def _same_event_thread() -> tuple[
    GmailTemporalThreadSnapshotAuthority,
    tuple[GmailTemporalThreadMessageReview, ...],
    GmailTemporalEventIdentityPlan,
    GmailTemporalEventIdentityResolution,
]:
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
    authority, messages = _snapshot(pairs)
    plan = _plan(pairs)
    resolution = _resolve(plan, _uniform_verdicts(plan, "same_event"))
    return authority, messages, plan, resolution


def _two_external_cluster_thread() -> tuple[
    GmailTemporalThreadSnapshotAuthority,
    tuple[GmailTemporalThreadMessageReview, ...],
    GmailTemporalEventIdentityPlan,
    GmailTemporalEventIdentityResolution,
]:
    specifications = (
        (
            "The Apollo interview is scheduled for August 14, 2027.",
            "scheduled",
            "2027-08-01T09:00:00-07:00",
        ),
        (
            "The Apollo interview was cancelled on August 14, 2027.",
            "cancelled",
            "2027-08-02T09:00:00-07:00",
        ),
        (
            "The budget review is scheduled for September 14, 2027.",
            "scheduled",
            "2027-08-03T09:00:00-07:00",
        ),
        (
            "The budget review was cancelled on September 14, 2027.",
            "cancelled",
            "2027-08-04T09:00:00-07:00",
        ),
    )
    projections = tuple(
        _projection(
            text,
            _select_lifecycle(lifecycle),
            internal_at=internal_at,
            chunk_id=f"message-{index}",
        )
        for index, (text, lifecycle, internal_at) in enumerate(
            specifications,
            start=1,
        )
    )
    pairs = tuple(
        _message(
            order=index,
            provider_message_id=f"message-{index}",
            projection=projection,
            internal_at=specifications[index - 1][2],
            start_offset=(index - 1) * 1_000,
        )
        for index, projection in enumerate(projections, start=1)
    )
    authority, messages = _snapshot(pairs)
    plan = _plan(pairs)
    order_by_unit = {item.unit_id: item.message_order for item in plan.units}
    same_event_orders = {frozenset((1, 2)), frozenset((3, 4))}
    verdicts = {
        pair.pair_id: (
            "same_event"
            if frozenset(
                (
                    order_by_unit[pair.left_unit_id],
                    order_by_unit[pair.right_unit_id],
                )
            )
            in same_event_orders
            else "different_event"
        )
        for pair in plan.pairs
    }
    resolution = _resolve(plan, verdicts)
    assert len(resolution.clusters) == 2
    assert all(len(item.unit_ids) == 2 for item in resolution.clusters)
    return authority, messages, plan, resolution


def test_public_validation_and_binding_reject_cross_cluster_target_swap() -> None:
    authority, messages, plan, resolution = _two_external_cluster_thread()
    clusters = tuple(
        sorted(resolution.clusters, key=lambda item: item.event_identity_key)
    )
    assertions_by_key = {
        cluster.event_identity_key: tuple(
            sorted(
                (
                    assertion
                    for assertion in resolution.assertions
                    if assertion.event_identity_key == cluster.event_identity_key
                ),
                key=lambda item: item.unit_id,
            )
        )
        for cluster in clusters
    }
    swapped = []
    for source, target in ((clusters[0], clusters[1]), (clusters[1], clusters[0])):
        for assertion, target_assertion in zip(
            assertions_by_key[source.event_identity_key],
            assertions_by_key[target.event_identity_key],
            strict=True,
        ):
            swapped.append(
                replace(
                    assertion,
                    unit_id=target_assertion.unit_id,
                    projection_fingerprint=target_assertion.projection_fingerprint,
                    artifact_id=target_assertion.artifact_id,
                    hypothesis_id=target_assertion.hypothesis_id,
                )
            )
    forged = _refingerprint_resolution(
        replace(
            resolution,
            assertions=tuple(
                sorted(
                    swapped,
                    key=lambda item: (
                        item.unit_id,
                        item.projection_fingerprint,
                        item.artifact_id,
                        item.hypothesis_id,
                    ),
                )
            ),
        )
    )

    with pytest.raises(GmailTemporalEventIdentityError, match="exact plan units"):
        validate_gmail_temporal_event_identity_resolution(
            plan=plan,
            resolution=forged,
        )
    with pytest.raises(GmailTemporalEventIdentityError, match="exact plan units"):
        bind_gmail_temporal_event_identity_resolution(
            snapshot_authority=authority,
            messages=messages,
            analysis_authorities=_analysis_authorities(messages),
            plan=plan,
            resolution=forged,
        )


def test_binding_rejects_stale_current_messages_and_plan_mismatch() -> None:
    schedule = _projection(
        "The dental appointment is scheduled for September 2, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-1",
    )
    original_pair = _message(
        order=1,
        provider_message_id="message-1",
        projection=schedule,
        internal_at="2027-08-01T09:00:00-07:00",
        start_offset=0,
    )
    original_authority, original_messages = _snapshot((original_pair,))
    original_plan = _plan((original_pair,))
    original_resolution = _resolve(original_plan, {})
    revised_pair = _message(
        order=1,
        provider_message_id="message-1",
        projection=schedule,
        internal_at="2027-08-01T09:00:00-07:00",
        start_offset=0,
        document_hash="f" * 64,
        source_revision="a" * 64,
        head_generation=2,
    )
    revised_authority, revised_messages = _snapshot((revised_pair,))

    with pytest.raises(
        GmailTemporalEventIdentityError, match="thread review authority"
    ):
        bind_gmail_temporal_event_identity_resolution(
            snapshot_authority=revised_authority,
            messages=original_messages,
            analysis_authorities=_analysis_authorities(original_messages),
            plan=original_plan,
            resolution=original_resolution,
        )
    with pytest.raises(GmailTemporalEventIdentityError, match="not bound"):
        bind_gmail_temporal_event_identity_resolution(
            snapshot_authority=revised_authority,
            messages=revised_messages,
            analysis_authorities=_analysis_authorities(revised_messages),
            plan=original_plan,
            resolution=original_resolution,
        )

    assert original_authority != revised_authority


def test_binding_rejects_refingerprinted_action_to_event_type_forgery() -> None:
    projection = _projection(
        "The upload is scheduled for August 18, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-action-forgery",
    )
    artifact = projection.artifacts[0]
    hypothesis = artifact.hypotheses[0]
    assert hypothesis.subject_type_references == (
        (hypothesis.subject_mention_ids[0], "action"),
    )
    original_pair = _message(
        order=1,
        provider_message_id="message-action-forgery",
        projection=projection,
        internal_at="2027-08-01T09:00:00-07:00",
        start_offset=0,
    )
    authority, original_messages = _snapshot((original_pair,))
    plan = _plan((original_pair,))
    resolution = _resolve(plan, {})
    assert plan.units == ()

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
    forged_pair = _message(
        order=1,
        provider_message_id="message-action-forgery",
        projection=forged_projection,
        internal_at="2027-08-01T09:00:00-07:00",
        start_offset=0,
    )
    forged_authority, forged_messages = _snapshot((forged_pair,))
    assert forged_authority != authority

    with pytest.raises(
        GmailTemporalEventIdentityError,
        match="thread review authority is stale or invalid",
    ):
        bind_gmail_temporal_event_identity_resolution(
            snapshot_authority=authority,
            messages=forged_messages,
            analysis_authorities=_analysis_authorities(original_messages),
            plan=plan,
            resolution=resolution,
        )


def test_planning_recomputes_analysis_snapshot_before_trusting_subject_type() -> None:
    projection = _projection(
        "The upload is scheduled for August 18, 2027.",
        _select_lifecycle("scheduled"),
        internal_at="2027-08-01T09:00:00-07:00",
        chunk_id="message-analysis-forgery",
    )
    original_pair = _message(
        order=1,
        provider_message_id="message-analysis-forgery",
        projection=projection,
        internal_at="2027-08-01T09:00:00-07:00",
        start_offset=0,
    )
    _, original_messages = _snapshot((original_pair,))
    original_analysis = _analysis_authorities(original_messages)[0]
    artifact = projection.artifacts[0]
    hypothesis = artifact.hypotheses[0]
    subject_id = hypothesis.subject_mention_ids[0]
    forged_hypothesis = _with_subject_type_references(
        hypothesis,
        ((subject_id, "event"),),
    )
    forged_projection = _refingerprint_projection(
        replace(
            projection,
            projection_fingerprint="",
            artifacts=(replace(artifact, hypotheses=(forged_hypothesis,)),),
        )
    )
    forged_pair = _message(
        order=1,
        provider_message_id="message-analysis-forgery",
        projection=forged_projection,
        internal_at="2027-08-01T09:00:00-07:00",
        start_offset=0,
    )
    forged_authority, forged_messages = _snapshot((forged_pair,))
    forged_analysis = replace(
        original_analysis,
        mentions=tuple(
            replace(mention, mention_type="event")
            if mention.mention_id == subject_id
            else mention
            for mention in original_analysis.mentions
        ),
    )
    assert forged_analysis.snapshot_fingerprint == (
        original_analysis.snapshot_fingerprint
    )

    with pytest.raises(
        GmailTemporalEventIdentityError,
        match="analysis authority is stale or mismatched",
    ):
        plan_gmail_temporal_event_identity(
            snapshot_authority=forged_authority,
            messages=forged_messages,
            analysis_authorities=(forged_analysis,),
        )


def test_valid_binding_feeds_lifecycle_with_the_resolved_event() -> None:
    authority, messages, plan, resolution = _same_event_thread()

    validate_gmail_temporal_event_identity_resolution(
        plan=plan,
        resolution=resolution,
    )
    bound_messages = bind_gmail_temporal_event_identity_resolution(
        snapshot_authority=authority,
        messages=messages,
        analysis_authorities=_analysis_authorities(messages),
        plan=plan,
        resolution=resolution,
    )
    with pytest.raises(
        GmailTemporalThreadLifecycleError,
        match="complete plan and resolution authority",
    ):
        project_gmail_temporal_thread_lifecycle(
            snapshot_authority=authority,
            messages=bound_messages,
        )
    lifecycle = project_gmail_temporal_thread_lifecycle(
        snapshot_authority=authority,
        messages=bound_messages,
        event_identity_analysis_authorities=_analysis_authorities(messages),
        event_identity_plan=plan,
        event_identity_resolution=resolution,
    )

    assert sum(len(item.identity_assertions) for item in bound_messages) == 2
    assert len(lifecycle.events) == 1
    assert lifecycle.events[0].event_identity_key == (
        resolution.clusters[0].event_identity_key
    )
    assert lifecycle.events[0].current_status == "cancelled"
    assert [item.normalized_value for item in lifecycle.events[0].occurrences] == [
        "2027-08-14"
    ]
    assert lifecycle.unresolved_alternatives == ()


def test_binding_requires_the_exact_prior_resolution_authority() -> None:
    root_authority, messages, root_plan, prior = _same_event_thread()
    authority = replace(
        root_authority,
        prior_event_identity_resolution_fingerprint=prior.resolution_fingerprint,
    )
    plan = plan_gmail_temporal_event_identity(
        snapshot_authority=authority,
        messages=messages,
        analysis_authorities=_analysis_authorities(messages),
    )
    updated = _resolve(
        plan,
        _uniform_verdicts(plan, "same_event"),
        prior_resolution=prior,
    )

    with pytest.raises(
        GmailTemporalEventIdentityError,
        match="requires its exact prior authority",
    ):
        bind_gmail_temporal_event_identity_resolution(
            snapshot_authority=authority,
            messages=messages,
            analysis_authorities=_analysis_authorities(messages),
            plan=plan,
            resolution=updated,
        )

    wrong_prior = _resolve(
        root_plan,
        _uniform_verdicts(root_plan, "different_event"),
    )
    with pytest.raises(
        GmailTemporalEventIdentityError,
        match="prior authority is missing or stale",
    ):
        bind_gmail_temporal_event_identity_resolution(
            snapshot_authority=authority,
            messages=messages,
            analysis_authorities=_analysis_authorities(messages),
            plan=plan,
            resolution=updated,
            prior_resolution=wrong_prior,
        )

    bound = bind_gmail_temporal_event_identity_resolution(
        snapshot_authority=authority,
        messages=messages,
        analysis_authorities=_analysis_authorities(messages),
        plan=plan,
        resolution=updated,
        prior_resolution=prior,
    )
    assert sum(len(message.identity_assertions) for message in bound) == 2


@pytest.mark.parametrize("owner_key_matches_resolution", (True, False))
def test_binding_rejects_owner_verified_assertion_without_trusted_lineage(
    owner_key_matches_resolution: bool,
) -> None:
    authority, messages, plan, resolution = _same_event_thread()
    resolved_assertion = resolution.assertions[0]
    owner_assertion = replace(
        resolved_assertion,
        event_identity_key=(
            resolved_assertion.event_identity_key
            if owner_key_matches_resolution
            else "owner-event:apollo-interview"
        ),
        verification="owner_verified",
        provenance_ref="owner-receipt:apollo-interview",
    )
    messages_with_owner_lineage = _with_assertions(messages, (owner_assertion,))

    with pytest.raises(
        GmailTemporalEventIdentityError,
        match="owner-verified identity assertions require trusted owner-lineage",
    ):
        bind_gmail_temporal_event_identity_resolution(
            snapshot_authority=authority,
            messages=messages_with_owner_lineage,
            analysis_authorities=_analysis_authorities(messages),
            plan=plan,
            resolution=resolution,
        )

    assert tuple(
        assertion
        for message in messages_with_owner_lineage
        for assertion in message.identity_assertions
    ) == (owner_assertion,)


def test_lifecycle_rejects_assertion_unit_id_from_a_different_source_unit() -> None:
    authority, messages, _, resolution = _same_event_thread()
    first = resolution.assertions[0]
    foreign_unit_id = next(
        item.unit_id for item in resolution.assertions if item.unit_id != first.unit_id
    )
    forged_assertions = tuple(
        replace(item, unit_id=foreign_unit_id) if item == first else item
        for item in resolution.assertions
    )

    with pytest.raises(GmailTemporalThreadLifecycleError, match="wrong source unit"):
        project_gmail_temporal_thread_lifecycle(
            snapshot_authority=authority,
            messages=_with_assertions(messages, forged_assertions),
        )
