from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal, Sequence

from .gmail_temporal_batching import (
    GmailTemporalBatchAuthorityError,
    GmailTemporalBatchCaps,
    GmailTemporalBatchOmission,
    GmailTemporalBatchPlan,
    GmailTemporalSelectorBatch,
    VerifiedGmailTemporalBatchCitation,
    validate_gmail_temporal_batch_citation,
    validate_gmail_temporal_batch_manifest,
)
from .gmail_temporal_leads import TemporalLead, TemporalLeadAnalysis, TemporalMention
from .gmail_temporal_selection import (
    GmailTemporalSelection,
    GmailTemporalSelectionError,
    SelectedTemporalAssociation,
    SelectionDecision,
    validate_gmail_temporal_selection,
)


_VERSION = "gmail_temporal_reduction_v1"
_PLAN_VERSION = "gmail_temporal_batch_plan_v1"
_BATCH_VERSION = "gmail_temporal_selector_batch_v1"
_MAX_FINAL_ASSOCIATIONS = 8
_DECISIONS = {
    "select_for_review",
    "defer_ambiguous",
    "no_temporal_assertion",
    "reject_nonmaterial",
}
_POSITIVE_DECISIONS = {"select_for_review", "defer_ambiguous"}
_OMISSION_REASONS = {
    "fact_not_admitted",
    "scope_not_bound",
    "batch_cap_reached",
    "payload_byte_cap",
}
_DECISION_RANK = {
    "select_for_review": 0,
    "defer_ambiguous": 1,
    "no_temporal_assertion": 2,
    "reject_nonmaterial": 3,
}
_LEAD_TIER_RANK = {
    "strict_direct": 0,
    "review_resolved": 1,
    "review_fallback": 2,
    "review_ambiguous": 3,
}
_LEAD_MODE_RANK = {
    "direct_grammar": 0,
    "field_local": 1,
    "field_near": 2,
    "subject_singleton": 3,
    "subject_body_bridge": 4,
    "message_singleton": 5,
}
_MENTION_TYPE_RANK = {
    "event_title_candidate": 0,
    "event": 1,
    "event_predicate": 2,
    "deadline": 3,
    "action": 4,
    "boundary": 5,
}
_DIAGNOSTIC_ORDER = {
    value: index
    for index, value in enumerate(
        (
            "plan_expression_omission",
            "missing_batch_result",
            "unknown_batch_result",
            "invalid_batch_result",
            "duplicate_batch_result",
            "conflicting_batch_decision",
            "invalid_manifest_citation",
            "invalid_semantic_citation",
            "contradictory_negative_decision",
            "positive_decision_without_valid_citation",
            "duplicate_association",
            "overlapping_subject_alias",
            "conflicting_duplicate_association",
            "association_cap_reached",
            "interaction_isolated_association",
            "mixed_negative_decisions",
            "validator_forced_defer",
        )
    )
}


class GmailTemporalReductionError(ValueError):
    """Raised when the trusted analysis or batch plan has lost authority."""


@dataclass(frozen=True)
class GmailTemporalBatchSelectionRow:
    """One endpoint-only selector result bound to a planned batch."""

    batch_fingerprint: str
    decision: SelectionDecision
    associations: tuple[VerifiedGmailTemporalBatchCitation, ...] = ()
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalReductionDiagnostics:
    """Immutable aggregate diagnostics containing no source text or endpoint IDs."""

    planned_batch_count: int
    input_batch_result_count: int
    accepted_batch_result_count: int
    missing_batch_result_count: int
    unknown_batch_result_count: int
    invalid_batch_result_count: int
    duplicate_batch_result_count: int
    conflicting_batch_decision_count: int
    invalid_manifest_citation_count: int
    invalid_semantic_citation_count: int
    contradictory_decision_count: int
    positive_without_valid_citation_count: int
    duplicate_association_count: int
    conflicting_association_count: int
    associations_removed_by_cap: int
    interaction_isolated_association_count: int
    plan_omission_count: int
    independently_validated_association_count: int
    selected_association_count: int
    forced_defer: bool
    flags: tuple[str, ...] = ()
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalReductionResult:
    """A deterministic, review-only selection plus content-free accounting."""

    version: Literal["gmail_temporal_reduction_v1"]
    selection: GmailTemporalSelection
    diagnostics: GmailTemporalReductionDiagnostics
    routable: Literal[False] = False


@dataclass(frozen=True)
class _Candidate:
    citation: VerifiedGmailTemporalBatchCitation
    semantic: SelectedTemporalAssociation
    derived_decision: SelectionDecision
    model_decision: SelectionDecision
    batch_sequence: int


@dataclass
class _Counts:
    accepted_rows: int = 0
    unknown_rows: int = 0
    invalid_rows: int = 0
    duplicate_rows: int = 0
    conflicting_batch_decisions: int = 0
    invalid_manifest_citations: int = 0
    invalid_semantic_citations: int = 0
    contradictory_decisions: int = 0
    positive_without_valid_citations: int = 0
    duplicate_associations: int = 0
    conflicting_associations: int = 0
    removed_by_cap: int = 0
    interaction_isolated: int = 0
    independently_validated: int = 0


def reduce_gmail_temporal_batch_selections(
    *,
    analysis: TemporalLeadAnalysis,
    plan: GmailTemporalBatchPlan,
    rows: Sequence[GmailTemporalBatchSelectionRow],
) -> GmailTemporalReductionResult:
    """Merge isolated batch citations under deterministic endpoint authority.

    A malformed or semantically invalid citation is discarded on its own; it
    never invalidates a valid sibling citation.  Integrity failures in the
    trusted plan itself are fatal.  Ambiguous response coverage, contradictory
    decisions, conflicts, and truncation at the final eight-association cap are
    represented by a forced review deferral.
    """

    batches = _validate_plan_authority(analysis, plan)
    counts = _Counts()
    reasons: list[str] = []
    candidates: list[_Candidate] = []
    seen_rows: dict[str, int] = {}
    decisions_by_batch: dict[str, set[SelectionDecision]] = {}
    aggregate_decisions: list[SelectionDecision] = []

    for row in rows:
        if not isinstance(row, GmailTemporalBatchSelectionRow):
            counts.invalid_rows += 1
            reasons.append("invalid_batch_result")
            continue
        if not isinstance(row.batch_fingerprint, str):
            counts.invalid_rows += 1
            reasons.append("invalid_batch_result")
            continue
        batch = batches.get(row.batch_fingerprint)
        if batch is None:
            counts.unknown_rows += 1
            reasons.append("unknown_batch_result")
            continue
        if (
            row.decision not in _DECISIONS
            or not isinstance(row.associations, tuple)
            or row.routable is not False
        ):
            counts.invalid_rows += 1
            reasons.append("invalid_batch_result")
            continue

        counts.accepted_rows += 1
        seen_rows[row.batch_fingerprint] = seen_rows.get(row.batch_fingerprint, 0) + 1
        decisions_by_batch.setdefault(row.batch_fingerprint, set()).add(row.decision)
        aggregate_decisions.append(row.decision)
        valid_in_row = 0
        for citation in row.associations:
            verified = _manifest_validate_citation(batch, citation)
            if verified is None:
                counts.invalid_manifest_citations += 1
                reasons.append("invalid_manifest_citation")
                continue
            semantic = _semantic_validate_citation(analysis, verified)
            if semantic is None:
                counts.invalid_semantic_citations += 1
                reasons.append("invalid_semantic_citation")
                continue
            selection, association = semantic
            candidates.append(
                _Candidate(
                    citation=verified,
                    semantic=association,
                    derived_decision=selection.decision,
                    model_decision=row.decision,
                    batch_sequence=batch.sequence,
                )
            )
            counts.independently_validated += 1
            valid_in_row += 1

        if valid_in_row and row.decision not in _POSITIVE_DECISIONS:
            counts.contradictory_decisions += 1
            reasons.append("contradictory_negative_decision")
        if not valid_in_row and row.decision == "select_for_review":
            counts.positive_without_valid_citations += 1
            reasons.append("positive_decision_without_valid_citation")

    for row_count in seen_rows.values():
        if row_count > 1:
            counts.duplicate_rows += row_count - 1
    if counts.duplicate_rows:
        reasons.append("duplicate_batch_result")
    counts.conflicting_batch_decisions = sum(
        len(decisions) > 1 for decisions in decisions_by_batch.values()
    )
    if counts.conflicting_batch_decisions:
        reasons.append("conflicting_batch_decision")

    missing_rows = len(set(batches) - set(seen_rows))
    if missing_rows:
        reasons.append("missing_batch_result")
    if plan.omissions:
        reasons.append("plan_expression_omission")

    candidates = _dedupe_and_rank(candidates, analysis, counts, reasons)
    accepted: list[dict[str, object]] = []
    for index, candidate in enumerate(candidates):
        if len(accepted) >= _MAX_FINAL_ASSOCIATIONS:
            counts.removed_by_cap = len(candidates) - index
            reasons.append("association_cap_reached")
            break
        raw = _citation_payload(candidate.citation)
        try:
            validate_gmail_temporal_selection(
                analysis,
                {
                    "analysis_fingerprint": analysis.snapshot_fingerprint,
                    "decision": "defer_ambiguous",
                    "associations": [*accepted, raw],
                },
            )
        except GmailTemporalSelectionError:
            counts.interaction_isolated += 1
            reasons.append("interaction_isolated_association")
            continue
        accepted.append(raw)

    requested_decision = _requested_decision(
        analysis=analysis,
        accepted=accepted,
        candidates=candidates,
        aggregate_decisions=aggregate_decisions,
        force_defer=bool(reasons),
        reasons=reasons,
    )
    try:
        selection = validate_gmail_temporal_selection(
            analysis,
            {
                "analysis_fingerprint": analysis.snapshot_fingerprint,
                "decision": requested_decision,
                "associations": accepted,
            },
        )
    except GmailTemporalSelectionError as exc:
        raise GmailTemporalReductionError(
            "final reduced selection failed deterministic validation"
        ) from exc

    if (
        selection.requested_decision == "select_for_review"
        and selection.decision == "defer_ambiguous"
    ):
        reasons.append("validator_forced_defer")

    flags = _ordered_diagnostics(reasons)
    diagnostics = GmailTemporalReductionDiagnostics(
        planned_batch_count=len(plan.batches),
        input_batch_result_count=len(rows),
        accepted_batch_result_count=counts.accepted_rows,
        missing_batch_result_count=missing_rows,
        unknown_batch_result_count=counts.unknown_rows,
        invalid_batch_result_count=counts.invalid_rows,
        duplicate_batch_result_count=counts.duplicate_rows,
        conflicting_batch_decision_count=counts.conflicting_batch_decisions,
        invalid_manifest_citation_count=counts.invalid_manifest_citations,
        invalid_semantic_citation_count=counts.invalid_semantic_citations,
        contradictory_decision_count=counts.contradictory_decisions,
        positive_without_valid_citation_count=(
            counts.positive_without_valid_citations
        ),
        duplicate_association_count=counts.duplicate_associations,
        conflicting_association_count=counts.conflicting_associations,
        associations_removed_by_cap=counts.removed_by_cap,
        interaction_isolated_association_count=counts.interaction_isolated,
        plan_omission_count=len(plan.omissions),
        independently_validated_association_count=counts.independently_validated,
        selected_association_count=len(selection.associations),
        forced_defer=bool(flags),
        flags=flags,
    )
    return GmailTemporalReductionResult(
        version=_VERSION,
        selection=selection,
        diagnostics=diagnostics,
    )


def _validate_plan_authority(
    analysis: TemporalLeadAnalysis,
    plan: GmailTemporalBatchPlan,
) -> dict[str, GmailTemporalSelectorBatch]:
    if (
        not isinstance(analysis, TemporalLeadAnalysis)
        or not isinstance(plan, GmailTemporalBatchPlan)
        or not isinstance(plan.caps, GmailTemporalBatchCaps)
        or not isinstance(plan.batches, tuple)
        or not isinstance(plan.covered_expression_ids, tuple)
        or not isinstance(plan.omissions, tuple)
        or plan.version != _PLAN_VERSION
        or plan.analysis_fingerprint != analysis.snapshot_fingerprint
        or plan.source_sha256 != analysis.source_sha256
        or plan.routable is not False
    ):
        raise GmailTemporalReductionError(
            "batch plan does not match the current analysis authority"
        )

    batches: dict[str, GmailTemporalSelectorBatch] = {}
    covered: list[str] = []
    analysis_expressions = {item.expression_id: item for item in analysis.expressions}
    analysis_mentions = {item.mention_id: item for item in analysis.mentions}
    analysis_leads = {item.lead_id: item for item in analysis.leads}
    if (
        len(analysis_expressions) != len(analysis.expressions)
        or len(analysis_mentions) != len(analysis.mentions)
        or len(analysis_leads) != len(analysis.leads)
    ):
        raise GmailTemporalReductionError(
            "analysis endpoint identifiers are not unique"
        )
    for expected_sequence, batch in enumerate(plan.batches, start=1):
        if not isinstance(batch, GmailTemporalSelectorBatch):
            raise GmailTemporalReductionError(
                "batch plan contains an invalid endpoint packet"
            )
        try:
            validate_gmail_temporal_batch_manifest(batch)
        except GmailTemporalBatchAuthorityError as exc:
            raise GmailTemporalReductionError(
                "batch plan contains an invalid endpoint manifest"
            ) from exc
        fingerprint = batch.manifest.batch_fingerprint
        if (
            fingerprint in batches
            or batch.version != _BATCH_VERSION
            or batch.sequence != expected_sequence
            or batch.manifest.analysis_fingerprint != plan.analysis_fingerprint
            or batch.manifest.source_sha256 != plan.source_sha256
            or batch.routable is not False
        ):
            raise GmailTemporalReductionError(
                "batch plan contains a stale or duplicate endpoint packet"
            )
        for endpoint in batch.expressions:
            source = analysis_expressions.get(endpoint.expression_id)
            if source is None or (
                endpoint.field,
                endpoint.start,
                endpoint.end,
                endpoint.form,
                endpoint.resolution_status,
            ) != (
                source.field,
                source.start,
                source.end,
                source.form,
                source.resolution_status,
            ):
                raise GmailTemporalReductionError(
                    "batch expression endpoint is not bound to the analysis"
                )
        for endpoint in batch.mentions:
            source = analysis_mentions.get(endpoint.mention_id)
            if source is None or (
                endpoint.field,
                endpoint.start,
                endpoint.end,
                endpoint.mention_type,
                endpoint.lifecycle_role,
            ) != (
                source.field,
                source.start,
                source.end,
                source.mention_type,
                source.lifecycle_role,
            ):
                raise GmailTemporalReductionError(
                    "batch mention endpoint is not bound to the analysis"
                )
        for hint in batch.lead_hints:
            source = analysis_leads.get(hint.lead_id)
            if source is None or (
                hint.expression_id,
                hint.mention_id,
                hint.association_mode,
                hint.confidence_tier,
                hint.blockers,
                hint.risk_features,
            ) != (
                source.expression_id,
                source.mention_id,
                source.association_mode,
                source.confidence_tier,
                source.blockers,
                source.risk_features,
            ):
                raise GmailTemporalReductionError(
                    "batch lead hint is not bound to the analysis"
                )
        batches[fingerprint] = batch
        covered.extend(batch.manifest.expression_ids)

    if tuple(covered) != plan.covered_expression_ids:
        raise GmailTemporalReductionError("batch plan coverage manifest is invalid")
    omitted_ids = tuple(item.expression_id for item in plan.omissions)
    inventoried = set(analysis_expressions)
    if (
        len(covered) != len(set(covered))
        or len(omitted_ids) != len(set(omitted_ids))
        or set(covered) & set(omitted_ids)
        or set(covered) | set(omitted_ids) != inventoried
    ):
        raise GmailTemporalReductionError(
            "batch plan does not account for every expression exactly once"
        )
    for omission in plan.omissions:
        if not isinstance(omission, GmailTemporalBatchOmission):
            raise GmailTemporalReductionError(
                "batch omission is not bound to the analysis"
            )
        source = analysis_expressions.get(omission.expression_id)
        if (
            source is None
            or omission.field != source.field
            or not isinstance(omission.segment_id, str)
            or not omission.segment_id
            or omission.reason not in _OMISSION_REASONS
        ):
            raise GmailTemporalReductionError(
                "batch omission is not bound to the analysis"
            )

    material = {
        "schema": _PLAN_VERSION,
        "analysis_fingerprint": plan.analysis_fingerprint,
        "source_sha256": plan.source_sha256,
        "caps": asdict(plan.caps),
        "batch_fingerprints": [
            item.manifest.batch_fingerprint for item in plan.batches
        ],
        "omissions": [asdict(item) for item in plan.omissions],
        "routable": False,
    }
    expected = "gtp_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()
    if plan.plan_fingerprint != expected:
        raise GmailTemporalReductionError("batch plan fingerprint is invalid")
    return batches


def _manifest_validate_citation(
    batch: GmailTemporalSelectorBatch,
    citation: object,
) -> VerifiedGmailTemporalBatchCitation | None:
    if not isinstance(citation, VerifiedGmailTemporalBatchCitation):
        return None
    if citation.routable is not False:
        return None
    try:
        return validate_gmail_temporal_batch_citation(
            batch,
            batch_fingerprint=citation.batch_fingerprint,
            expression_id=citation.expression_id,
            subject_mention_id=citation.subject_mention_id,
            lifecycle_mention_id=citation.lifecycle_mention_id,
            selected_lead_id=citation.selected_lead_id,
        )
    except (GmailTemporalBatchAuthorityError, TypeError):
        return None


def _semantic_validate_citation(
    analysis: TemporalLeadAnalysis,
    citation: VerifiedGmailTemporalBatchCitation,
) -> tuple[GmailTemporalSelection, SelectedTemporalAssociation] | None:
    try:
        selection = validate_gmail_temporal_selection(
            analysis,
            {
                "analysis_fingerprint": analysis.snapshot_fingerprint,
                "decision": "select_for_review",
                "associations": [_citation_payload(citation)],
            },
        )
    except (GmailTemporalSelectionError, TypeError):
        return None
    if len(selection.associations) != 1:
        return None
    return selection, selection.associations[0]


def _dedupe_and_rank(
    candidates: list[_Candidate],
    analysis: TemporalLeadAnalysis,
    counts: _Counts,
    reasons: list[str],
) -> list[_Candidate]:
    ranked = sorted(candidates, key=lambda item: _candidate_rank(item, analysis))
    mentions = {item.mention_id: item for item in analysis.mentions}
    unique: list[_Candidate] = []
    for candidate in ranked:
        matching_indices = [
            index
            for index, item in enumerate(unique)
            if _same_subject_binding(item, candidate, mentions)
        ]
        if not matching_indices:
            unique.append(candidate)
            continue

        matched = [unique[index] for index in matching_indices]
        counts.duplicate_associations += len(matched)
        reasons.append("duplicate_association")
        if any(
            item.citation.subject_mention_id
            != candidate.citation.subject_mention_id
            for item in matched
        ):
            reasons.append("overlapping_subject_alias")
        conflicts = sum(
            _semantic_signature(item) != _semantic_signature(candidate)
            for item in matched
        )
        if conflicts:
            counts.conflicting_associations += conflicts
            reasons.append("conflicting_duplicate_association")

        canonical = min(
            (*matched, candidate),
            key=lambda item: (
                0
                if mentions[
                    item.citation.subject_mention_id
                ].mention_type
                == "event_title_candidate"
                else 1,
                _candidate_rank(item, analysis),
            ),
        )
        for index in reversed(matching_indices):
            del unique[index]
        unique.append(canonical)

    return sorted(unique, key=lambda item: _candidate_rank(item, analysis))


def _same_subject_binding(
    first: _Candidate,
    second: _Candidate,
    mentions: dict[str, TemporalMention],
) -> bool:
    if first.citation.expression_id != second.citation.expression_id:
        return False
    if first.citation.subject_mention_id == second.citation.subject_mention_id:
        return True
    first_mention = mentions[first.citation.subject_mention_id]
    second_mention = mentions[second.citation.subject_mention_id]
    types = {first_mention.mention_type, second_mention.mention_type}
    return (
        first_mention.field == second_mention.field
        and "event_title_candidate" in types
        and bool(types & {"event", "event_predicate"})
        and first_mention.start < second_mention.end
        and second_mention.start < first_mention.end
    )


def _candidate_rank(
    candidate: _Candidate,
    analysis: TemporalLeadAnalysis,
) -> tuple[object, ...]:
    expressions = {item.expression_id: item for item in analysis.expressions}
    mentions = {item.mention_id: item for item in analysis.mentions}
    expression = expressions[candidate.citation.expression_id]
    mention = mentions[candidate.citation.subject_mention_id]
    lead = _matching_lead(analysis, candidate.semantic.selected_lead_id)
    return (
        0 if candidate.derived_decision == "select_for_review" else 1,
        _DECISION_RANK[candidate.model_decision],
        len(candidate.semantic.blockers),
        len(candidate.semantic.risk_features),
        len(candidate.semantic.repair_flags),
        _LEAD_TIER_RANK.get(lead.confidence_tier, 99) if lead else 99,
        _LEAD_MODE_RANK.get(lead.association_mode, 99) if lead else 99,
        lead.gap_chars if lead else 1_000_000,
        _MENTION_TYPE_RANK.get(mention.mention_type, 99),
        candidate.batch_sequence,
        expression.start,
        mention.start,
        candidate.citation.expression_id,
        candidate.citation.subject_mention_id,
    )


def _matching_lead(
    analysis: TemporalLeadAnalysis,
    lead_id: str | None,
) -> TemporalLead | None:
    if lead_id is None:
        return None
    return next((item for item in analysis.leads if item.lead_id == lead_id), None)


def _semantic_signature(candidate: _Candidate) -> tuple[object, ...]:
    return (
        candidate.citation.lifecycle_mention_id,
        candidate.semantic.relation,
        candidate.semantic.kind,
        candidate.semantic.lifecycle,
        candidate.semantic.normalized_value,
    )


def _requested_decision(
    *,
    analysis: TemporalLeadAnalysis,
    accepted: list[dict[str, object]],
    candidates: list[_Candidate],
    aggregate_decisions: list[SelectionDecision],
    force_defer: bool,
    reasons: list[str],
) -> SelectionDecision:
    if accepted:
        if force_defer:
            return "defer_ambiguous"
        if any(item.model_decision == "select_for_review" for item in candidates):
            return "select_for_review"
        return "defer_ambiguous"
    if force_defer:
        return "defer_ambiguous"
    if not analysis.expressions:
        return "no_temporal_assertion"
    decisions = set(aggregate_decisions)
    if decisions == {"reject_nonmaterial"}:
        return "reject_nonmaterial"
    if decisions == {"no_temporal_assertion"}:
        return "no_temporal_assertion"
    if len(decisions) > 1 and not decisions & _POSITIVE_DECISIONS:
        reasons.append("mixed_negative_decisions")
    return "defer_ambiguous"


def _citation_payload(
    citation: VerifiedGmailTemporalBatchCitation,
) -> dict[str, object]:
    return {
        "expression_id": citation.expression_id,
        "subject_mention_id": citation.subject_mention_id,
        "lifecycle_mention_id": citation.lifecycle_mention_id,
        "selected_lead_id": citation.selected_lead_id,
    }


def _ordered_diagnostics(values: list[str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            set(values),
            key=lambda value: (_DIAGNOSTIC_ORDER.get(value, 999), value),
        )
    )


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
