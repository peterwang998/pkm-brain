from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from pkm_brain.gmail_temporal_batching import (
    GMAIL_TEMPORAL_SINGLETON_EVENT_FALLBACK_DIAGNOSTIC,
    GmailTemporalBatchAuthorityError,
    GmailTemporalBatchCaps,
    GmailTemporalBatchingError,
    gmail_temporal_selector_batch_payload,
    plan_gmail_temporal_selector_batches,
    validate_gmail_temporal_batch_citation,
)
from pkm_brain.gmail_temporal_leads import analyze_gmail_temporal_leads


ANCHOR = "2027-05-01T10:00:00-07:00"


def analyze(
    text: str,
    *,
    admitted: bool = True,
    rescue: bool = False,
    chunk_id: str | None = "synthetic-batching-message",
):
    return analyze_gmail_temporal_leads(
        text=text,
        message_internal_at=ANCHOR,
        fact_admitted=admitted,
        temporal_review_rescue=rescue,
        chunk_id=chunk_id,
    )


def test_plan_is_deterministic_immutable_and_surfaces_are_exact() -> None:
    text = (
        "Subject: Café Planning Meeting\n\n"
        "The interview is scheduled for May 14, 2027 at 4:30 PM."
    )
    value = analyze(text)

    first = plan_gmail_temporal_selector_batches(text=text, analysis=value)
    repeated = plan_gmail_temporal_selector_batches(text=text, analysis=value)

    assert first == repeated
    assert first.plan_fingerprint.startswith("gtp_")
    assert first.routable is False
    assert first.batches
    for batch in first.batches:
        payload = gmail_temporal_selector_batch_payload(batch)
        parsed = json.loads(payload)
        assert len(payload.encode("utf-8")) == batch.payload_bytes
        assert parsed["batch_fingerprint"] == batch.manifest.batch_fingerprint
        assert parsed["authority"]["expression_ids"] == list(
            batch.manifest.expression_ids
        )
        assert parsed["routable"] is False
        assert all(
            item.surface == text[item.start : item.end]
            for item in (*batch.contexts, *batch.expressions, *batch.mentions)
        )
        assert batch.routable is False

    with pytest.raises(FrozenInstanceError):
        first.batches[0].field = "message"  # type: ignore[misc]


def test_plan_rejects_analysis_rebound_to_changed_source_text() -> None:
    original = "The meeting is May 14, 2027."
    changed = "The workshop is May 14, 2027."
    value = analyze(original)

    with pytest.raises(GmailTemporalBatchingError, match="source fingerprint"):
        plan_gmail_temporal_selector_batches(text=changed, analysis=value)


def test_segment_batches_repeat_subject_but_exclude_other_segment_endpoints() -> None:
    text = (
        "Subject: Orchid Interview\n\n"
        "Alpha meeting is May 14, 2027. "
        "Beta workshop is May 15, 2027."
    )
    value = analyze(text)
    caps = GmailTemporalBatchCaps(overlap_chars=24)

    plan = plan_gmail_temporal_selector_batches(
        text=text,
        analysis=value,
        caps=caps,
    )

    assert len(plan.batches) == 2
    first, second = plan.batches
    assert [item.surface for item in first.expressions] == ["May 14, 2027"]
    assert [item.surface for item in second.expressions] == ["May 15, 2027"]
    assert "meeting" in {
        item.surface.casefold()
        for item in first.mentions
        if item.candidate_role == "local"
    }
    assert "workshop" not in {item.surface.casefold() for item in first.mentions}
    assert "workshop" in {
        item.surface.casefold()
        for item in second.mentions
        if item.candidate_role == "local"
    }
    assert "meeting" not in {item.surface.casefold() for item in second.mentions}
    for batch in plan.batches:
        assert any(
            item.role == "subject_bridge" and item.surface == "Orchid Interview"
            for item in batch.contexts
        )
        assert any(
            item.candidate_role == "subject_bridge"
            and item.surface.casefold() == "interview"
            for item in batch.mentions
        )
        local = next(item for item in batch.contexts if item.role == "local")
        assert batch.segment_start - local.start <= caps.overlap_chars
        assert local.end - batch.segment_end <= caps.overlap_chars


def test_subject_title_context_is_repeated_without_inventing_an_endpoint() -> None:
    text = "Subject: Project Apollo\n\nThe meeting is May 14, 2027."
    value = analyze(text)

    plan = plan_gmail_temporal_selector_batches(text=text, analysis=value)

    batch = plan.batches[0]
    assert any(
        item.role == "subject_bridge" and item.surface == "Project Apollo"
        for item in batch.contexts
    )
    assert not any(item.candidate_role == "subject_bridge" for item in batch.mentions)


def test_structured_event_title_is_repeated_as_a_citable_subject_bridge() -> None:
    text = "Subject: Q3 Leadership Forum\n\nWhen: May 14, 2027"
    value = analyze(text)

    plan = plan_gmail_temporal_selector_batches(text=text, analysis=value)

    assert len(plan.batches) == 1
    batch = plan.batches[0]
    title = next(
        item for item in batch.mentions if item.mention_type == "event_title_candidate"
    )
    assert title.candidate_role == "subject_bridge"
    assert title.surface == "Q3 Leadership Forum"
    assert title.mention_id in batch.manifest.mention_ids


@pytest.mark.parametrize(
    ("admitted", "rescue", "expected_basis"),
    (
        (True, False, "fact"),
        (False, True, "temporal_rescue"),
    ),
)
def test_singleton_cross_segment_event_fallback_packetizes_only_linked_event(
    admitted: bool,
    rescue: bool,
    expected_basis: str,
) -> None:
    text = "The workshop update is ready. May 14, 2027."
    value = analyze(text, admitted=admitted, rescue=rescue)
    assert value.association_admission_basis == expected_basis
    assert len(value.expressions) == 1
    assert len(value.leads) == 1
    lead = value.leads[0]
    event = next(item for item in value.mentions if item.mention_type == "event")
    expression = value.expressions[0]
    assert lead.association_mode == "field_near"
    assert lead.mention_id == event.mention_id
    assert expression.field == event.field
    assert expression.segment_id != event.segment_id

    plan = plan_gmail_temporal_selector_batches(text=text, analysis=value)

    assert plan.omissions == ()
    assert len(plan.batches) == 1
    batch = plan.batches[0]
    assert (
        batch.diagnostics.count(GMAIL_TEMPORAL_SINGLETON_EVENT_FALLBACK_DIAGNOSTIC) == 1
    )
    assert tuple(item.mention_id for item in batch.mentions) == (event.mention_id,)
    assert tuple(item.lead_id for item in batch.lead_hints) == (lead.lead_id,)
    assert batch.segment_start <= expression.start < batch.segment_end
    assert not (batch.segment_start <= event.start < batch.segment_end)
    local = next(item for item in batch.contexts if item.role == "local")
    assert local.start <= min(expression.start, event.start)
    assert local.end >= max(expression.end, event.end)


@pytest.mark.parametrize(
    "text",
    (
        "Please submit the form. May 14, 2027.",
        "Workshop and meeting updates are ready. May 14, 2027.",
        "The workshop update is ready. May 14, 2027. May 15, 2027.",
    ),
)
def test_singleton_cross_segment_fallback_does_not_broaden_non_singletons(
    text: str,
) -> None:
    value = analyze(text)

    plan = plan_gmail_temporal_selector_batches(text=text, analysis=value)

    assert all(
        GMAIL_TEMPORAL_SINGLETON_EVENT_FALLBACK_DIAGNOSTIC not in batch.diagnostics
        for batch in plan.batches
    )


def test_singleton_cross_segment_fallback_requires_admission() -> None:
    text = "The workshop update is ready. May 14, 2027."
    value = analyze(text, admitted=False, rescue=False)

    plan = plan_gmail_temporal_selector_batches(text=text, analysis=value)

    assert plan.batches == ()
    assert {item.reason for item in plan.omissions} == {"fact_not_admitted"}


def test_singleton_cross_segment_fallback_fails_closed_at_payload_cap() -> None:
    text = "The workshop update is ready. May 14, 2027."
    value = analyze(text)

    plan = plan_gmail_temporal_selector_batches(
        text=text,
        analysis=value,
        caps=GmailTemporalBatchCaps(max_payload_bytes=128),
    )

    assert plan.batches == ()
    assert plan.covered_expression_ids == ()
    assert len(plan.omissions) == 1
    assert plan.omissions[0].reason == "payload_byte_cap"


def test_citable_subject_bridges_precede_local_artifacts_at_mention_cap() -> None:
    text = (
        "Subject: Cancelled Planning Meeting\n\n"
        "Email calendar invitation attachment document link agenda "
        "Meeting May 14, 2027."
    )
    value = analyze(text)
    citable_types = {
        "event",
        "event_title_candidate",
        "event_predicate",
        "deadline",
        "action",
        "boundary",
        "lifecycle",
    }
    expected = {
        item.mention_id for item in value.mentions if item.mention_type in citable_types
    }
    assert len(expected) == 4

    plan = plan_gmail_temporal_selector_batches(
        text=text,
        analysis=value,
        caps=GmailTemporalBatchCaps(max_mentions_per_batch=4),
    )

    assert len(plan.batches) == 1
    assert expected == set(plan.batches[0].manifest.mention_ids)


def test_default_batch_cap_covers_dense_but_bounded_message() -> None:
    text = " ".join(f"Meeting July {(index % 28) + 1}, 2027." for index in range(66))
    value = analyze(text)
    assert len(value.expressions) == 66

    plan = plan_gmail_temporal_selector_batches(text=text, analysis=value)

    assert len(plan.batches) == 66
    assert plan.omissions == ()


def test_default_mention_cap_covers_dense_citable_segment() -> None:
    text = (
        "Subject: Cancelled Planning Meeting\n\n"
        "Meeting interview workshop conference appointment session event call "
        "visit tour presentation demo review training screening discussion "
        "webinar lecture summit briefing May 14, 2027."
    )
    value = analyze(text)
    citable_types = {
        "event",
        "event_title_candidate",
        "event_predicate",
        "deadline",
        "action",
        "boundary",
        "lifecycle",
    }
    expected = {
        item.mention_id for item in value.mentions if item.mention_type in citable_types
    }
    assert len(expected) == 21

    plan = plan_gmail_temporal_selector_batches(text=text, analysis=value)

    assert len(plan.batches) == 1
    assert expected <= set(plan.batches[0].manifest.mention_ids)


def test_hard_caps_and_expression_omission_accounting() -> None:
    text = "Subject: Planning Meeting\n\n" + " ".join(
        f"Meeting July {day}, 2027" for day in range(1, 10)
    )
    value = analyze(text)
    caps = GmailTemporalBatchCaps(
        max_payload_bytes=2_200,
        max_expressions_per_batch=3,
        max_mentions_per_batch=3,
        max_batches=2,
        max_lead_hints_per_batch=1,
        overlap_chars=16,
        max_local_context_chars=600,
    )

    plan = plan_gmail_temporal_selector_batches(
        text=text,
        analysis=value,
        caps=caps,
    )

    assert len(plan.batches) == caps.max_batches
    assert all(batch.payload_bytes <= caps.max_payload_bytes for batch in plan.batches)
    assert all(
        len(batch.expressions) <= caps.max_expressions_per_batch
        and len(batch.mentions) <= caps.max_mentions_per_batch
        and len(batch.lead_hints) <= caps.max_lead_hints_per_batch
        for batch in plan.batches
    )
    covered = set(plan.covered_expression_ids)
    omitted = {item.expression_id for item in plan.omissions}
    inventoried = {item.expression_id for item in value.expressions}
    assert covered.isdisjoint(omitted)
    assert covered | omitted == inventoried
    assert all(item.reason == "batch_cap_reached" for item in plan.omissions)


def test_payload_too_small_produces_content_free_expression_omission() -> None:
    text = "The meeting is May 14, 2027."
    value = analyze(text)

    plan = plan_gmail_temporal_selector_batches(
        text=text,
        analysis=value,
        caps=GmailTemporalBatchCaps(max_payload_bytes=700),
    )

    assert plan.batches == ()
    assert len(plan.omissions) == 1
    assert plan.omissions[0].expression_id == value.expressions[0].expression_id
    assert plan.omissions[0].reason == "payload_byte_cap"
    assert not hasattr(plan.omissions[0], "surface")


def test_fact_and_scope_gates_fail_closed_with_explicit_omissions() -> None:
    text = "The meeting is May 14, 2027."
    held = analyze(text, admitted=False)
    anonymous = analyze(text, chunk_id=None)

    held_plan = plan_gmail_temporal_selector_batches(text=text, analysis=held)
    anonymous_plan = plan_gmail_temporal_selector_batches(
        text=text,
        analysis=anonymous,
    )

    assert held_plan.batches == ()
    assert {item.reason for item in held_plan.omissions} == {"fact_not_admitted"}
    assert anonymous_plan.batches == ()
    assert {item.reason for item in anonymous_plan.omissions} == {"scope_not_bound"}


def test_temporal_review_rescue_is_batchable_but_remains_nonroutable() -> None:
    text = "The meeting is May 14, 2027."
    rescued = analyze(text, admitted=False, rescue=True)

    plan = plan_gmail_temporal_selector_batches(text=text, analysis=rescued)

    assert plan.batches
    assert plan.omissions == ()
    assert plan.routable is False
    assert all(batch.routable is False for batch in plan.batches)


def test_batch_manifest_rejects_out_of_subset_and_mismatched_lead_citations() -> None:
    text = (
        "Subject: Orchid Interview\n\n"
        "Alpha meeting is May 14, 2027. "
        "Beta workshop is May 15, 2027."
    )
    value = analyze(text)
    plan = plan_gmail_temporal_selector_batches(text=text, analysis=value)
    first, second = plan.batches
    lead = first.lead_hints[0]

    verified = validate_gmail_temporal_batch_citation(
        first,
        batch_fingerprint=first.manifest.batch_fingerprint,
        expression_id=lead.expression_id,
        subject_mention_id=lead.mention_id,
        selected_lead_id=lead.lead_id,
    )

    assert verified.routable is False
    with pytest.raises(GmailTemporalBatchAuthorityError, match="fingerprint"):
        validate_gmail_temporal_batch_citation(
            first,
            batch_fingerprint=second.manifest.batch_fingerprint,
            expression_id=lead.expression_id,
            subject_mention_id=lead.mention_id,
        )
    with pytest.raises(GmailTemporalBatchAuthorityError, match="outside"):
        validate_gmail_temporal_batch_citation(
            first,
            batch_fingerprint=first.manifest.batch_fingerprint,
            expression_id=second.expressions[0].expression_id,
            subject_mention_id=lead.mention_id,
        )
    other_mention = next(
        item for item in first.mentions if item.mention_id != lead.mention_id
    )
    with pytest.raises(GmailTemporalBatchAuthorityError, match="does not match"):
        validate_gmail_temporal_batch_citation(
            first,
            batch_fingerprint=first.manifest.batch_fingerprint,
            expression_id=lead.expression_id,
            subject_mention_id=other_mention.mention_id,
            selected_lead_id=lead.lead_id,
        )
    forged = replace(first, expressions=())
    with pytest.raises(GmailTemporalBatchAuthorityError, match="manifest"):
        validate_gmail_temporal_batch_citation(
            forged,
            batch_fingerprint=first.manifest.batch_fingerprint,
            expression_id=lead.expression_id,
            subject_mention_id=lead.mention_id,
        )

    for mutated in (
        replace(first, payload_bytes=1),
        replace(first, routable=True),  # type: ignore[arg-type]
        replace(first, version="gmail_temporal_selector_batch_v0"),  # type: ignore[arg-type]
    ):
        with pytest.raises(GmailTemporalBatchAuthorityError, match="batch|payload"):
            validate_gmail_temporal_batch_citation(
                mutated,
                batch_fingerprint=first.manifest.batch_fingerprint,
                expression_id=lead.expression_id,
                subject_mention_id=lead.mention_id,
            )


def test_invalid_endpoint_span_is_rejected_before_surface_exposure() -> None:
    text = "The meeting is May 14, 2027."
    value = analyze(text)
    invalid_expression = replace(value.expressions[0], end=len(text) + 1)
    invalid = replace(value, expressions=(invalid_expression,))

    with pytest.raises(GmailTemporalBatchingError, match="outside"):
        plan_gmail_temporal_selector_batches(text=text, analysis=invalid)


@pytest.mark.parametrize(
    "field,value",
    (
        ("max_payload_bytes", 0),
        ("max_expressions_per_batch", 0),
        ("max_mentions_per_batch", -1),
        ("max_batches", 0),
        ("max_lead_hints_per_batch", 0),
        ("overlap_chars", -1),
    ),
)
def test_caps_reject_non_positive_or_negative_values(field: str, value: int) -> None:
    with pytest.raises(ValueError):
        GmailTemporalBatchCaps(**{field: value})
