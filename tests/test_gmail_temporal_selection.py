from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from pkm_brain.gmail_temporal_leads import analyze_gmail_temporal_leads
from pkm_brain.gmail_temporal_selection import (
    GMAIL_TEMPORAL_SELECTION_SCHEMA,
    GmailTemporalSelectionError,
    gmail_temporal_selection_contract,
    validate_gmail_temporal_selection,
)


ANCHOR = "2027-05-01T10:00:00-07:00"


def analysis(
    text: str,
    *,
    admitted: bool = True,
    chunk_id: str = "synthetic-selection-message",
):
    return analyze_gmail_temporal_leads(
        text=text,
        message_internal_at=ANCHOR,
        fact_admitted=admitted,
        chunk_id=chunk_id,
    )


def subject_mention(value):
    return next(
        item
        for item in value.mentions
        if item.mention_type in {"event", "event_predicate", "deadline", "action"}
    )


def matching_lead(value, expression_id: str, subject_mention_id: str):
    return next(
        item
        for item in value.leads
        if item.expression_id == expression_id
        and item.mention_id == subject_mention_id
    )


def association_payload(
    value,
    *,
    expression_id: str | None = None,
    subject_mention_id: str | None = None,
    lifecycle_mention_id: str | None = None,
    selected_lead_id: str | None = None,
):
    expression = value.expressions[0]
    return {
        "expression_id": expression.expression_id
        if expression_id is None
        else expression_id,
        "subject_mention_id": (
            subject_mention(value).mention_id
            if subject_mention_id is None
            else subject_mention_id
        ),
        "lifecycle_mention_id": lifecycle_mention_id,
        "selected_lead_id": selected_lead_id,
    }


def selection_payload(value, *associations, decision: str = "select_for_review"):
    return {
        "analysis_fingerprint": value.snapshot_fingerprint,
        "decision": decision,
        "associations": list(associations),
    }


def test_valid_selection_derives_all_semantics_without_model_authorship() -> None:
    value = analysis(
        "The interview is scheduled for May 14, 2027. "
        "Additional planning details will follow shortly."
    )
    expression = value.expressions[0]
    subject = subject_mention(value)
    lifecycle = next(
        item for item in value.mentions if item.lifecycle_role == "scheduled"
    )
    lead = matching_lead(value, expression.expression_id, subject.mention_id)
    payload = association_payload(
        value,
        lifecycle_mention_id=lifecycle.mention_id,
        selected_lead_id=lead.lead_id,
    )

    result = validate_gmail_temporal_selection(value, selection_payload(value, payload))

    association = result.associations[0]
    assert result.version == "gmail_temporal_selection_v2"
    assert result.analysis_fingerprint == value.snapshot_fingerprint
    assert result.requested_decision == result.decision == "select_for_review"
    assert result.confidence == "medium"
    assert association.normalized_value == "2027-05-14"
    assert (association.relation, association.kind, association.lifecycle) == (
        "occurrence",
        "planned",
        "scheduled",
    )
    assert association.routable is result.routable is False
    with pytest.raises(FrozenInstanceError):
        association.relation = "deadline"  # type: ignore[misc]


def test_missing_kind_is_preserved_as_unspecified_without_guessing_planned() -> None:
    value = analysis("Orchid Interview\nMay 14, 2027\nPlease keep the date open.")
    expression = value.expressions[0]
    subject = subject_mention(value)
    lead = matching_lead(value, expression.expression_id, subject.mention_id)

    result = validate_gmail_temporal_selection(
        value,
        selection_payload(
            value,
            association_payload(value, selected_lead_id=lead.lead_id),
        ),
    )

    assert result.decision == "defer_ambiguous"
    assert result.confidence == "low"
    assert result.associations[0].relation == "occurrence"
    assert result.associations[0].kind == "unspecified"


def test_model_authored_semantics_confidence_or_time_are_rejected() -> None:
    value = analysis("The interview is scheduled for May 14, 2027.")
    for field, authored in (
        ("relation", "deadline"),
        ("kind", "actual"),
        ("lifecycle", "completed"),
        ("normalization_option_index", 0),
        ("timestamp", "2099-01-01"),
    ):
        payload = association_payload(value)
        payload[field] = authored
        with pytest.raises(GmailTemporalSelectionError, match="invalid fields"):
            validate_gmail_temporal_selection(
                value, selection_payload(value, payload)
            )
    envelope = selection_payload(value, association_payload(value))
    envelope["confidence"] = "high"
    with pytest.raises(GmailTemporalSelectionError, match="invalid fields"):
        validate_gmail_temporal_selection(value, envelope)


@pytest.mark.parametrize(
    "text",
    (
        "The meeting is scheduled for 7/8/2027.",
        "The meeting is scheduled for May 14, 2027 at 4:30 PM.",
    ),
)
def test_ambiguous_or_incomplete_normalization_is_forced_to_defer(text: str) -> None:
    value = analysis(text)
    expression = value.expressions[0]
    subject = subject_mention(value)
    lead = matching_lead(value, expression.expression_id, subject.mention_id)

    result = validate_gmail_temporal_selection(
        value,
        selection_payload(
            value,
            association_payload(value, selected_lead_id=lead.lead_id),
        ),
    )

    assert result.requested_decision == "select_for_review"
    assert result.decision == "defer_ambiguous"
    assert result.confidence == "low"
    assert result.associations[0].normalized_value is None
    assert "normalization_not_single_complete_value" in result.blockers
    assert result.repair_flags == ("decision_coerced_to_defer",)


def test_deadline_range_is_not_promoted_as_a_single_due_boundary() -> None:
    value = analysis("Application deadline May 14-16, 2027.")
    expression = value.expressions[0]
    subject = subject_mention(value)
    lead = matching_lead(value, expression.expression_id, subject.mention_id)

    result = validate_gmail_temporal_selection(
        value,
        selection_payload(
            value,
            association_payload(value, selected_lead_id=lead.lead_id),
        ),
    )

    association = result.associations[0]
    assert result.decision == "defer_ambiguous"
    assert association.normalized_value is None
    assert (association.relation, association.kind) == (
        "unspecified",
        "unspecified",
    )
    assert "deadline_range_not_single_boundary" in association.blockers


def test_actual_occurrence_phrase_is_not_rewritten_as_completion() -> None:
    value = analysis("The meeting was held on May 14, 2027.")
    expression = value.expressions[0]
    subject = subject_mention(value)
    lead = matching_lead(value, expression.expression_id, subject.mention_id)

    result = validate_gmail_temporal_selection(
        value,
        selection_payload(
            value,
            association_payload(value, selected_lead_id=lead.lead_id),
        ),
    )

    association = result.associations[0]
    assert result.decision == "select_for_review"
    assert (association.relation, association.kind, association.lifecycle) == (
        "occurrence",
        "actual",
        "none",
    )


def test_terminal_lifecycle_derives_unspecified_relation_and_kind() -> None:
    value = analysis("The meeting was cancelled May 14, 2027.")
    expression = value.expressions[0]
    subject = subject_mention(value)
    lifecycle = next(
        item for item in value.mentions if item.lifecycle_role == "cancelled"
    )
    lead = matching_lead(value, expression.expression_id, subject.mention_id)

    result = validate_gmail_temporal_selection(
        value,
        selection_payload(
            value,
            association_payload(
                value,
                lifecycle_mention_id=lifecycle.mention_id,
                selected_lead_id=lead.lead_id,
            ),
        ),
    )

    association = result.associations[0]
    assert result.decision == "select_for_review"
    assert (association.relation, association.kind, association.lifecycle) == (
        "unspecified",
        "unspecified",
        "cancelled",
    )
    assert association.repair_flags == (
        "terminal_semantics_derived_as_unspecified",
    )


def test_lifecycle_cue_is_not_bound_to_a_farther_competing_event() -> None:
    text = (
        "Alpha meeting was cancelled May 14, 2027. "
        "Beta meeting is May 15, 2027."
    )
    value = analysis(text)
    expression = value.expressions[0]
    subjects = [
        item
        for item in value.mentions
        if item.mention_type == "event"
        and text[item.start : item.end].casefold() == "meeting"
    ]
    subject = max(subjects, key=lambda item: item.start)
    lifecycle = next(
        item for item in value.mentions if item.lifecycle_role == "cancelled"
    )

    result = validate_gmail_temporal_selection(
        value,
        selection_payload(
            value,
            association_payload(
                value,
                expression_id=expression.expression_id,
                subject_mention_id=subject.mention_id,
                lifecycle_mention_id=lifecycle.mention_id,
            ),
        ),
    )

    association = result.associations[0]
    assert result.decision == "defer_ambiguous"
    assert (association.relation, association.kind, association.lifecycle) == (
        "unspecified",
        "unspecified",
        "unknown",
    )
    assert "competing_lifecycle_subject" in association.blockers


def test_lifecycle_cue_with_multiple_events_is_deferred_at_any_distance() -> None:
    text = (
        "The meeting "
        + ("with an extended descriptive agenda and invited attendees " * 8)
        + "was scheduled and the workshop is May 14, 2027."
    )
    value = analysis(text)
    expression = value.expressions[0]
    subject = next(
        item
        for item in value.mentions
        if item.mention_type == "event"
        and text[item.start : item.end].casefold() == "workshop"
    )
    lifecycle = next(
        item for item in value.mentions if item.lifecycle_role == "scheduled"
    )

    result = validate_gmail_temporal_selection(
        value,
        selection_payload(
            value,
            association_payload(
                value,
                expression_id=expression.expression_id,
                subject_mention_id=subject.mention_id,
                lifecycle_mention_id=lifecycle.mention_id,
            ),
        ),
    )

    association = result.associations[0]
    assert result.decision == "defer_ambiguous"
    assert association.lifecycle == "unknown"
    assert "competing_lifecycle_subject" in association.blockers


def test_terminal_boundary_subject_is_never_promoted_as_occurrence_start() -> None:
    value = analysis("Ends May 14, 2027 after the final session closes.")
    subject = next(
        item for item in value.mentions if item.boundary_role == "terminal_boundary"
    )
    with pytest.raises(GmailTemporalSelectionError, match="supported temporal subject"):
        validate_gmail_temporal_selection(
            value,
            selection_payload(
                value,
                association_payload(
                    value,
                    subject_mention_id=subject.mention_id,
                ),
            ),
        )


def test_broad_reschedule_role_is_not_guessed_as_old_or_replacement() -> None:
    value = analysis(
        "The meeting was rescheduled from July 20, 2027 to July 22, 2027."
    )
    expression = value.expressions[0]
    subject = subject_mention(value)
    lifecycle = next(
        item for item in value.mentions if item.lifecycle_role == "rescheduled"
    )
    lead = matching_lead(value, expression.expression_id, subject.mention_id)

    result = validate_gmail_temporal_selection(
        value,
        selection_payload(
            value,
            association_payload(
                value,
                lifecycle_mention_id=lifecycle.mention_id,
                selected_lead_id=lead.lead_id,
            ),
        ),
    )

    association = result.associations[0]
    assert result.decision == "defer_ambiguous"
    assert association.lifecycle == "unknown"
    assert (association.relation, association.kind) == (
        "unspecified",
        "unspecified",
    )
    assert "rescheduled_endpoint_role_unresolved" in association.blockers


def test_same_expression_subject_pair_cannot_be_shotgunned() -> None:
    value = analysis("The meeting is scheduled for May 14, 2027.")
    first = association_payload(value)
    lifecycle = next(
        item for item in value.mentions if item.lifecycle_role == "scheduled"
    )
    second = association_payload(value, lifecycle_mention_id=lifecycle.mention_id)

    with pytest.raises(GmailTemporalSelectionError, match="only once"):
        validate_gmail_temporal_selection(
            value, selection_payload(value, first, second)
        )


def test_unknown_endpoints_fail_closed_and_bad_optional_lead_fails_soft() -> None:
    value = analysis(
        "The interview is scheduled for May 14, 2027 and "
        "the workshop for May 15, 2027."
    )
    with pytest.raises(GmailTemporalSelectionError, match="unknown"):
        validate_gmail_temporal_selection(
            value,
            selection_payload(
                value,
                association_payload(
                    value, expression_id="gtl_0123456789abcdef:e99"
                ),
            ),
        )

    expression = value.expressions[0]
    subject = subject_mention(value)
    correct = matching_lead(value, expression.expression_id, subject.mention_id)
    wrong = next(item for item in value.leads if item.lead_id != correct.lead_id)
    result = validate_gmail_temporal_selection(
        value,
        selection_payload(
            value,
            association_payload(value, selected_lead_id=wrong.lead_id),
        ),
    )
    association = result.associations[0]
    assert association.selected_lead_id == correct.lead_id
    assert association.repair_flags == (
        "selected_lead_reference_discarded",
        "matching_lead_recovered_deterministically",
    )
    assert result.decision == "defer_ambiguous"


def test_non_admitted_evidence_and_negative_decisions_cannot_select() -> None:
    held = analysis("The meeting is scheduled for May 14, 2027.", admitted=False)
    with pytest.raises(GmailTemporalSelectionError, match="non-admitted"):
        validate_gmail_temporal_selection(
            held, selection_payload(held, association_payload(held))
        )
    result = validate_gmail_temporal_selection(
        held,
        selection_payload(held, decision="no_temporal_assertion"),
    )
    assert result.associations == ()
    assert result.confidence == "medium"


def test_anonymous_content_addressed_analysis_cannot_create_associations() -> None:
    value = analysis(
        "The meeting is scheduled for May 14, 2027.", chunk_id=""
    )

    assert value.scope_bound is False
    with pytest.raises(GmailTemporalSelectionError, match="opaque evidence scope"):
        validate_gmail_temporal_selection(
            value,
            selection_payload(value, association_payload(value)),
        )


def test_defer_can_report_an_inventory_miss_without_inventing_ids() -> None:
    value = analysis("The meeting is discussed without a date.")
    result = validate_gmail_temporal_selection(
        value,
        selection_payload(value, decision="defer_ambiguous"),
    )

    assert result.decision == "defer_ambiguous"
    assert result.associations == ()
    assert result.confidence == "low"


def test_artifacts_and_lifecycle_mentions_cannot_be_used_as_subjects() -> None:
    artifact_value = analysis("Attachment May 14, 2027")
    artifact = next(
        item for item in artifact_value.mentions if item.mention_type == "artifact"
    )
    with pytest.raises(GmailTemporalSelectionError, match="artifact"):
        validate_gmail_temporal_selection(
            artifact_value,
            selection_payload(
                artifact_value,
                association_payload(
                    artifact_value, subject_mention_id=artifact.mention_id
                ),
            ),
        )

    lifecycle_value = analysis("Cancelled on May 14, 2027")
    lifecycle = next(
        item
        for item in lifecycle_value.mentions
        if item.mention_type == "lifecycle"
    )
    payload = {
        "expression_id": lifecycle_value.expressions[0].expression_id,
        "subject_mention_id": lifecycle.mention_id,
        "lifecycle_mention_id": None,
        "selected_lead_id": None,
    }
    with pytest.raises(GmailTemporalSelectionError, match="lifecycle mentions"):
        validate_gmail_temporal_selection(
            lifecycle_value, selection_payload(lifecycle_value, payload)
        )

    label_value = analysis("When: May 14, 2027")
    label = next(
        item
        for item in label_value.mentions
        if item.mention_type == "structural_label"
    )
    with pytest.raises(GmailTemporalSelectionError, match="supported temporal subject"):
        validate_gmail_temporal_selection(
            label_value,
            selection_payload(
                label_value,
                association_payload(
                    label_value, subject_mention_id=label.mention_id
                ),
            ),
        )


def test_invalid_optional_lifecycle_reference_is_discarded_and_deferred() -> None:
    value = analysis("The meeting is scheduled for May 14, 2027.")
    subject = subject_mention(value)
    result = validate_gmail_temporal_selection(
        value,
        selection_payload(
            value,
            association_payload(
                value, lifecycle_mention_id=subject.mention_id
            ),
        ),
    )

    assert result.decision == "defer_ambiguous"
    assert result.associations[0].lifecycle_mention_id is None
    assert result.associations[0].repair_flags == (
        "lifecycle_mention_reference_discarded",
    )


def test_stale_selection_is_rejected_before_endpoint_rebinding() -> None:
    first = analysis("The meeting is scheduled for May 14, 2027.", chunk_id="same")
    second = analysis("The workshop deadline is June 20, 2027.", chunk_id="same")
    stale = selection_payload(first, association_payload(first))

    assert first.snapshot_fingerprint != second.snapshot_fingerprint
    assert first.expressions[0].expression_id != second.expressions[0].expression_id
    with pytest.raises(GmailTemporalSelectionError, match="snapshot"):
        validate_gmail_temporal_selection(second, stale)


@pytest.mark.parametrize(
    ("field", "invalid"),
    (
        ("expression_id", ""),
        ("expression_id", "gtl_bad:e1"),
        ("subject_mention_id", "x" * 129),
        ("selected_lead_id", "gtl_0123456789abcdef:e1"),
    ),
)
def test_evidence_ids_are_strictly_bounded_and_typed(
    field: str, invalid: str
) -> None:
    value = analysis("The meeting is scheduled for May 14, 2027.")
    payload = association_payload(value)
    payload[field] = invalid

    with pytest.raises(GmailTemporalSelectionError, match="malformed"):
        validate_gmail_temporal_selection(value, selection_payload(value, payload))


def test_contract_and_schema_expose_only_bounded_endpoint_citation() -> None:
    serialized = str(GMAIL_TEMPORAL_SELECTION_SCHEMA)
    for forbidden in (
        "timestamp",
        "start_at",
        "reason",
        "confidence",
        "normalization_option_index",
        "relation",
        "kind",
    ):
        assert forbidden not in serialized
    association_properties = GMAIL_TEMPORAL_SELECTION_SCHEMA["properties"][
        "associations"
    ]["items"]["properties"]
    assert "uniqueItems" not in GMAIL_TEMPORAL_SELECTION_SCHEMA["properties"][
        "associations"
    ]
    assert set(association_properties) == {
        "expression_id",
        "subject_mention_id",
        "lifecycle_mention_id",
        "selected_lead_id",
    }
    assert GMAIL_TEMPORAL_SELECTION_SCHEMA["additionalProperties"] is False
    assert GMAIL_TEMPORAL_SELECTION_SCHEMA["properties"]["associations"][
        "maxItems"
    ] == 8
    assert "Never author" in gmail_temporal_selection_contract()
