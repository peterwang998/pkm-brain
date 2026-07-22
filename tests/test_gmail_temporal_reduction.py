from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, asdict, replace

import pytest

from pkm_brain.gmail_temporal_batching import (
    GmailTemporalBatchCaps,
    GmailTemporalSelectorBatch,
    VerifiedGmailTemporalBatchCitation,
    plan_gmail_temporal_selector_batches,
    validate_gmail_temporal_batch_citation,
)
from pkm_brain.gmail_temporal_leads import (
    TemporalLeadAnalysis,
    analyze_gmail_temporal_leads,
)
from pkm_brain.gmail_temporal_reduction import (
    GmailTemporalBatchSelectionRow,
    GmailTemporalReductionError,
    reduce_gmail_temporal_batch_selections,
)


ANCHOR = "2027-05-01T10:00:00-07:00"


def analyze_and_plan(
    text: str,
    *,
    admitted: bool = True,
    rescue: bool = False,
    caps: GmailTemporalBatchCaps | None = None,
):
    analysis = analyze_gmail_temporal_leads(
        text=text,
        message_internal_at=ANCHOR,
        fact_admitted=admitted,
        temporal_review_rescue=rescue,
        chunk_id="synthetic-reduction-message",
    )
    plan = plan_gmail_temporal_selector_batches(
        text=text,
        analysis=analysis,
        caps=caps,
    )
    return analysis, plan


def subject_citation(
    analysis: TemporalLeadAnalysis,
    batch: GmailTemporalSelectorBatch,
    *,
    lifecycle: bool = False,
) -> VerifiedGmailTemporalBatchCitation:
    expression_id = batch.expressions[0].expression_id
    lead = next(
        item
        for item in batch.lead_hints
        if item.expression_id == expression_id
        and next(
            mention
            for mention in analysis.mentions
            if mention.mention_id == item.mention_id
        ).mention_type
        in {
            "event",
            "event_title_candidate",
            "event_predicate",
            "deadline",
            "action",
            "boundary",
        }
    )
    lifecycle_id = None
    if lifecycle:
        lifecycle_id = next(
            item.mention_id
            for item in batch.mentions
            if item.mention_type == "lifecycle"
        )
    return validate_gmail_temporal_batch_citation(
        batch,
        batch_fingerprint=batch.manifest.batch_fingerprint,
        expression_id=expression_id,
        subject_mention_id=lead.mention_id,
        lifecycle_mention_id=lifecycle_id,
        selected_lead_id=lead.lead_id,
    )


def citation_for_mention_type(
    analysis: TemporalLeadAnalysis,
    batch: GmailTemporalSelectorBatch,
    mention_type: str,
) -> VerifiedGmailTemporalBatchCitation:
    expression_id = batch.expressions[0].expression_id
    mention_ids = {
        item.mention_id
        for item in analysis.mentions
        if item.mention_type == mention_type
    }
    lead = next(
        item
        for item in batch.lead_hints
        if item.expression_id == expression_id and item.mention_id in mention_ids
    )
    return validate_gmail_temporal_batch_citation(
        batch,
        batch_fingerprint=batch.manifest.batch_fingerprint,
        expression_id=expression_id,
        subject_mention_id=lead.mention_id,
        selected_lead_id=lead.lead_id,
    )


def selection_row(
    batch: GmailTemporalSelectorBatch,
    *associations: VerifiedGmailTemporalBatchCitation,
    decision: str = "select_for_review",
) -> GmailTemporalBatchSelectionRow:
    return GmailTemporalBatchSelectionRow(
        batch_fingerprint=batch.manifest.batch_fingerprint,
        decision=decision,  # type: ignore[arg-type]
        associations=tuple(associations),
    )


def test_reducer_merges_batches_deterministically_and_is_immutable() -> None:
    text = (
        "Alpha meeting is scheduled for May 14, 2027. "
        "Beta workshop is scheduled for May 15, 2027."
    )
    analysis, plan = analyze_and_plan(text)
    rows = tuple(
        selection_row(batch, subject_citation(analysis, batch))
        for batch in plan.batches
    )

    result = reduce_gmail_temporal_batch_selections(
        analysis=analysis,
        plan=plan,
        rows=rows,
    )
    repeated = reduce_gmail_temporal_batch_selections(
        analysis=analysis,
        plan=plan,
        rows=tuple(reversed(rows)),
    )

    assert result == repeated
    assert result.version == "gmail_temporal_reduction_v1"
    assert result.selection.decision == "select_for_review"
    assert len(result.selection.associations) == 2
    assert result.diagnostics.independently_validated_association_count == 2
    assert result.diagnostics.selected_association_count == 2
    assert result.diagnostics.flags == ()
    assert result.routable is result.selection.routable is False
    with pytest.raises(FrozenInstanceError):
        result.diagnostics.forced_defer = True  # type: ignore[misc]


def test_bad_manifest_citation_isolated_without_erasing_valid_sibling() -> None:
    text = "The meeting is scheduled for May 14, 2027."
    analysis, plan = analyze_and_plan(text)
    batch = plan.batches[0]
    valid = subject_citation(analysis, batch)
    invalid = replace(valid, subject_mention_id="gtl_0123456789abcdef:m99")

    result = reduce_gmail_temporal_batch_selections(
        analysis=analysis,
        plan=plan,
        rows=(selection_row(batch, invalid, valid),),
    )

    assert len(result.selection.associations) == 1
    assert result.selection.associations[0].subject_mention_id == (
        valid.subject_mention_id
    )
    assert result.selection.decision == "defer_ambiguous"
    assert result.diagnostics.invalid_manifest_citation_count == 1
    assert result.diagnostics.independently_validated_association_count == 1
    assert "invalid_manifest_citation" in result.diagnostics.flags


def test_bad_semantic_citation_isolated_after_exact_manifest_validation() -> None:
    text = "The meeting is scheduled for May 14, 2027."
    analysis, plan = analyze_and_plan(text)
    batch = plan.batches[0]
    valid = subject_citation(analysis, batch)
    lifecycle = next(
        item for item in batch.mentions if item.mention_type == "lifecycle"
    )
    invalid = validate_gmail_temporal_batch_citation(
        batch,
        batch_fingerprint=batch.manifest.batch_fingerprint,
        expression_id=batch.expressions[0].expression_id,
        subject_mention_id=lifecycle.mention_id,
    )

    result = reduce_gmail_temporal_batch_selections(
        analysis=analysis,
        plan=plan,
        rows=(selection_row(batch, invalid, valid),),
    )

    assert len(result.selection.associations) == 1
    assert result.selection.associations[0].subject_mention_id == (
        valid.subject_mention_id
    )
    assert result.selection.decision == "defer_ambiguous"
    assert result.diagnostics.invalid_semantic_citation_count == 1
    assert result.diagnostics.independently_validated_association_count == 1


@pytest.mark.parametrize(
    "decision",
    ("no_temporal_assertion", "reject_nonmaterial"),
)
def test_negative_row_cannot_erase_valid_citation_but_forces_defer(
    decision: str,
) -> None:
    analysis, plan = analyze_and_plan(
        "The meeting is scheduled for May 14, 2027."
    )
    batch = plan.batches[0]
    citation = subject_citation(analysis, batch)

    result = reduce_gmail_temporal_batch_selections(
        analysis=analysis,
        plan=plan,
        rows=(selection_row(batch, citation, decision=decision),),
    )

    assert len(result.selection.associations) == 1
    assert result.selection.decision == "defer_ambiguous"
    assert result.diagnostics.contradictory_decision_count == 1
    assert "contradictory_negative_decision" in result.diagnostics.flags


def test_conflicting_duplicate_is_deduped_and_forced_to_defer() -> None:
    analysis, plan = analyze_and_plan(
        "The meeting is scheduled for May 14, 2027."
    )
    batch = plan.batches[0]
    without_lifecycle = subject_citation(analysis, batch)
    with_lifecycle = subject_citation(analysis, batch, lifecycle=True)

    result = reduce_gmail_temporal_batch_selections(
        analysis=analysis,
        plan=plan,
        rows=(selection_row(batch, without_lifecycle, with_lifecycle),),
    )

    assert len(result.selection.associations) == 1
    assert result.selection.decision == "defer_ambiguous"
    assert result.diagnostics.duplicate_association_count == 1
    assert result.diagnostics.conflicting_association_count == 1
    assert "conflicting_duplicate_association" in result.diagnostics.flags


def test_exact_duplicate_is_deduped_and_forced_to_defer() -> None:
    analysis, plan = analyze_and_plan(
        "The meeting is scheduled for May 14, 2027."
    )
    batch = plan.batches[0]
    citation = subject_citation(analysis, batch)

    result = reduce_gmail_temporal_batch_selections(
        analysis=analysis,
        plan=plan,
        rows=(selection_row(batch, citation, citation),),
    )

    assert len(result.selection.associations) == 1
    assert result.selection.decision == "defer_ambiguous"
    assert result.diagnostics.duplicate_association_count == 1
    assert result.diagnostics.conflicting_association_count == 0
    assert "duplicate_association" in result.diagnostics.flags


def test_specific_event_title_dedupes_its_overlapping_generic_noun() -> None:
    analysis, plan = analyze_and_plan(
        "Subject: Orchid Interview\n\nPlease join us on May 14, 2027."
    )
    batch = plan.batches[0]
    title = citation_for_mention_type(
        analysis,
        batch,
        "event_title_candidate",
    )
    generic = citation_for_mention_type(analysis, batch, "event")

    result = reduce_gmail_temporal_batch_selections(
        analysis=analysis,
        plan=plan,
        rows=(selection_row(batch, generic, title),),
    )

    assert len(result.selection.associations) == 1
    assert result.selection.associations[0].subject_mention_id == (
        title.subject_mention_id
    )
    assert result.selection.decision == "defer_ambiguous"
    assert result.diagnostics.duplicate_association_count == 1
    assert "overlapping_subject_alias" in result.diagnostics.flags


def test_specific_event_title_collapses_all_overlapping_generic_nouns() -> None:
    analysis, plan = analyze_and_plan(
        "Subject: Orchid Interview Planning Meeting\n\nWhen: May 14, 2027"
    )
    batch = plan.batches[0]
    title = citation_for_mention_type(
        analysis,
        batch,
        "event_title_candidate",
    )
    title_mention = next(
        item for item in batch.mentions if item.mention_id == title.subject_mention_id
    )
    expression_id = batch.expressions[0].expression_id
    generic = tuple(
        validate_gmail_temporal_batch_citation(
            batch,
            batch_fingerprint=batch.manifest.batch_fingerprint,
            expression_id=expression_id,
            subject_mention_id=mention.mention_id,
            selected_lead_id=None,
        )
        for mention in batch.mentions
        if mention.mention_type == "event"
        and mention.start < title_mention.end
        and title_mention.start < mention.end
    )

    result = reduce_gmail_temporal_batch_selections(
        analysis=analysis,
        plan=plan,
        rows=(selection_row(batch, *generic, title),),
    )

    assert len(generic) == 2
    assert len(result.selection.associations) == 1
    assert result.selection.associations[0].subject_mention_id == (
        title.subject_mention_id
    )
    assert result.diagnostics.duplicate_association_count == 2
    assert result.selection.decision == "defer_ambiguous"


def test_deterministic_ranking_caps_at_eight_and_forces_defer() -> None:
    text = " ".join(
        f"Meeting {number} is scheduled for July {number}, 2027."
        for number in range(1, 10)
    )
    analysis, plan = analyze_and_plan(text)
    rows = tuple(
        selection_row(batch, subject_citation(analysis, batch))
        for batch in plan.batches
    )

    first = reduce_gmail_temporal_batch_selections(
        analysis=analysis,
        plan=plan,
        rows=rows,
    )
    reversed_result = reduce_gmail_temporal_batch_selections(
        analysis=analysis,
        plan=plan,
        rows=tuple(reversed(rows)),
    )

    assert first == reversed_result
    assert len(first.selection.associations) == 8
    assert first.selection.decision == "defer_ambiguous"
    assert first.diagnostics.associations_removed_by_cap == 1
    assert "association_cap_reached" in first.diagnostics.flags
    expected = {item.expressions[0].expression_id for item in plan.batches[:8]}
    assert {item.expression_id for item in first.selection.associations} == expected


def test_missing_result_and_plan_omission_each_force_defer() -> None:
    text = (
        "Alpha meeting is scheduled for May 14, 2027. "
        "Beta workshop is scheduled for May 15, 2027."
    )
    analysis, complete = analyze_and_plan(text)
    first_batch = complete.batches[0]
    missing = reduce_gmail_temporal_batch_selections(
        analysis=analysis,
        plan=complete,
        rows=(selection_row(first_batch, subject_citation(analysis, first_batch)),),
    )

    assert missing.selection.decision == "defer_ambiguous"
    assert missing.diagnostics.missing_batch_result_count == 1
    assert "missing_batch_result" in missing.diagnostics.flags

    limited_analysis, limited = analyze_and_plan(
        text,
        caps=GmailTemporalBatchCaps(max_batches=1),
    )
    limited_batch = limited.batches[0]
    omitted = reduce_gmail_temporal_batch_selections(
        analysis=limited_analysis,
        plan=limited,
        rows=(
            selection_row(
                limited_batch,
                subject_citation(limited_analysis, limited_batch),
            ),
        ),
    )

    assert omitted.selection.decision == "defer_ambiguous"
    assert omitted.diagnostics.plan_omission_count == 1
    assert "plan_expression_omission" in omitted.diagnostics.flags


def test_duplicate_conflicting_batch_rows_force_defer_but_preserve_citation() -> None:
    analysis, plan = analyze_and_plan(
        "The meeting is scheduled for May 14, 2027."
    )
    batch = plan.batches[0]
    citation = subject_citation(analysis, batch)

    result = reduce_gmail_temporal_batch_selections(
        analysis=analysis,
        plan=plan,
        rows=(
            selection_row(batch, citation),
            selection_row(batch, decision="no_temporal_assertion"),
        ),
    )

    assert len(result.selection.associations) == 1
    assert result.selection.decision == "defer_ambiguous"
    assert result.diagnostics.duplicate_batch_result_count == 1
    assert result.diagnostics.conflicting_batch_decision_count == 1


def test_rescue_reuses_validator_to_remain_low_deferred_and_nonroutable() -> None:
    analysis, plan = analyze_and_plan(
        "The meeting is scheduled for May 14, 2027.",
        admitted=False,
        rescue=True,
    )
    batch = plan.batches[0]

    result = reduce_gmail_temporal_batch_selections(
        analysis=analysis,
        plan=plan,
        rows=(selection_row(batch, subject_citation(analysis, batch)),),
    )

    assert result.selection.requested_decision == "select_for_review"
    assert result.selection.decision == "defer_ambiguous"
    assert result.selection.confidence == "low"
    assert result.selection.routable is result.routable is False
    assert "temporal_review_rescue_only" in result.selection.blockers
    assert result.diagnostics.forced_defer is True
    assert "validator_forced_defer" in result.diagnostics.flags


def test_mutated_manifest_is_fatal_even_when_the_row_has_no_citations() -> None:
    analysis, plan = analyze_and_plan(
        "The meeting is scheduled for May 14, 2027."
    )
    forged_batch = replace(plan.batches[0], expressions=())
    forged_plan = replace(plan, batches=(forged_batch,))

    with pytest.raises(GmailTemporalReductionError, match="manifest"):
        reduce_gmail_temporal_batch_selections(
            analysis=analysis,
            plan=forged_plan,
            rows=(selection_row(forged_batch),),
        )


def test_diagnostics_are_content_free() -> None:
    text = "Secret Orchid meeting is scheduled for May 14, 2027."
    analysis, plan = analyze_and_plan(text)
    batch = plan.batches[0]
    valid = subject_citation(analysis, batch)
    invalid = replace(valid, subject_mention_id="gtl_0123456789abcdef:m99")

    result = reduce_gmail_temporal_batch_selections(
        analysis=analysis,
        plan=plan,
        rows=(selection_row(batch, valid, invalid),),
    )
    serialized = json.dumps(asdict(result.diagnostics), sort_keys=True)

    assert "Secret" not in serialized
    assert "Orchid" not in serialized
    assert "gtl_" not in serialized
    assert "gtb_" not in serialized
    assert "gta_" not in serialized
