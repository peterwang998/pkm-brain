from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from typing import Literal, Sequence

from .gmail_temporal_leads import (
    TemporalExpression,
    TemporalLead,
    TemporalLeadAnalysis,
    TemporalMention,
)


BatchField = Literal["subject", "body", "message"]
BatchContextRole = Literal["local", "subject_bridge"]
BatchMentionRole = Literal["local", "subject_bridge"]
BatchOmissionReason = Literal[
    "fact_not_admitted",
    "scope_not_bound",
    "batch_cap_reached",
    "payload_byte_cap",
]

_PLAN_VERSION = "gmail_temporal_batch_plan_v1"
_BATCH_VERSION = "gmail_temporal_selector_batch_v1"
_MANIFEST_VERSION = "gmail_temporal_batch_manifest_v1"
_FINGERPRINT_RE = re.compile(r"gta_[0-9a-f]{64}\Z")
_EXPRESSION_ID_RE = re.compile(r"gtl_[0-9a-f]{16}:e[1-9][0-9]*\Z")
_MENTION_ID_RE = re.compile(r"gtl_[0-9a-f]{16}:m[1-9][0-9]*\Z")
_LEAD_ID_RE = re.compile(r"gtl_[0-9a-f]{16}:l[1-9][0-9]*\Z")
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?;](?:[\"')\]]+)?(?=\s|$)")
_FORWARD_MARKER_RE = re.compile(
    r"^(?:-{2,}\s*(?:original message|forwarded message)\s*-{2,}|"
    r"on .{1,200} wrote:)\s*$",
    re.IGNORECASE,
)
_SUBJECT_BRIDGE_TYPES = frozenset(
    {
        "event",
        "event_title_candidate",
        "event_predicate",
        "deadline",
        "action",
    }
)
_MENTION_TYPE_RANK = {
    "event_title_candidate": 0,
    "event": 1,
    "event_predicate": 2,
    "deadline": 3,
    "action": 4,
    "lifecycle": 5,
    "boundary": 6,
    "structural_label": 7,
    "artifact": 8,
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


class GmailTemporalBatchingError(ValueError):
    """Raised when batch planning cannot preserve its evidence authority."""


class GmailTemporalBatchAuthorityError(ValueError):
    """Raised when a citation exceeds one batch's endpoint manifest."""


@dataclass(frozen=True)
class GmailTemporalBatchCaps:
    """Hard limits for canonical selector payloads."""

    max_payload_bytes: int = 12_000
    max_expressions_per_batch: int = 1
    max_mentions_per_batch: int = 16
    max_batches: int = 64
    max_lead_hints_per_batch: int = 4
    overlap_chars: int = 120
    max_local_context_chars: int = 2_400
    max_subject_context_chars: int = 320

    def __post_init__(self) -> None:
        positive = (
            "max_payload_bytes",
            "max_expressions_per_batch",
            "max_mentions_per_batch",
            "max_batches",
            "max_lead_hints_per_batch",
            "max_local_context_chars",
            "max_subject_context_chars",
        )
        for name in positive:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.overlap_chars, bool)
            or not isinstance(self.overlap_chars, int)
            or self.overlap_chars < 0
        ):
            raise ValueError("overlap_chars must be a non-negative integer")


@dataclass(frozen=True)
class GmailTemporalBatchContext:
    """An exact source slice; ``surface`` always equals ``text[start:end]``."""

    context_id: str
    role: BatchContextRole
    field: BatchField
    start: int
    end: int
    surface: str


@dataclass(frozen=True)
class GmailTemporalBatchExpression:
    """A recognized expression endpoint with its exact verified surface."""

    expression_id: str
    field: BatchField
    start: int
    end: int
    surface: str
    form: str
    resolution_status: str


@dataclass(frozen=True)
class GmailTemporalBatchMention:
    """A local or subject-bridge mention endpoint."""

    mention_id: str
    candidate_role: BatchMentionRole
    field: BatchField
    start: int
    end: int
    surface: str
    mention_type: str
    lifecycle_role: str | None


@dataclass(frozen=True)
class GmailTemporalBatchLeadHint:
    """A bounded ranking hint, never an endpoint-authority boundary."""

    lead_id: str
    expression_id: str
    mention_id: str
    association_mode: str
    confidence_tier: str
    blockers: tuple[str, ...]
    risk_features: tuple[str, ...]


@dataclass(frozen=True)
class GmailTemporalBatchManifest:
    """The exact endpoint subset an external reply may cite."""

    version: Literal["gmail_temporal_batch_manifest_v1"]
    batch_fingerprint: str
    analysis_fingerprint: str
    source_sha256: str
    expression_ids: tuple[str, ...]
    mention_ids: tuple[str, ...]
    lead_ids: tuple[str, ...]


@dataclass(frozen=True)
class GmailTemporalSelectorBatch:
    """One immutable, byte-bounded endpoint-selection packet."""

    version: Literal["gmail_temporal_selector_batch_v1"]
    sequence: int
    field: BatchField
    segment_id: str
    segment_start: int
    segment_end: int
    manifest: GmailTemporalBatchManifest
    contexts: tuple[GmailTemporalBatchContext, ...]
    expressions: tuple[GmailTemporalBatchExpression, ...]
    mentions: tuple[GmailTemporalBatchMention, ...]
    lead_hints: tuple[GmailTemporalBatchLeadHint, ...]
    payload_bytes: int
    omitted_mention_count: int
    omitted_lead_hint_count: int
    diagnostics: tuple[str, ...] = ()
    routable: Literal[False] = False


@dataclass(frozen=True)
class GmailTemporalBatchOmission:
    """Content-free accounting for an expression absent from every batch."""

    expression_id: str
    field: BatchField
    segment_id: str
    reason: BatchOmissionReason


@dataclass(frozen=True)
class GmailTemporalBatchPlan:
    """A complete immutable partition of covered and omitted expressions."""

    version: Literal["gmail_temporal_batch_plan_v1"]
    analysis_fingerprint: str
    source_sha256: str
    plan_fingerprint: str
    caps: GmailTemporalBatchCaps
    batches: tuple[GmailTemporalSelectorBatch, ...]
    covered_expression_ids: tuple[str, ...]
    omissions: tuple[GmailTemporalBatchOmission, ...]
    routable: Literal[False] = False


@dataclass(frozen=True)
class VerifiedGmailTemporalBatchCitation:
    """A citation proven to be contained by one exact batch manifest."""

    batch_fingerprint: str
    expression_id: str
    subject_mention_id: str
    lifecycle_mention_id: str | None
    selected_lead_id: str | None
    routable: Literal[False] = False


@dataclass(frozen=True)
class _FieldRange:
    name: BatchField
    start: int
    end: int


@dataclass(frozen=True)
class _Segment:
    segment_id: str
    field: BatchField
    start: int
    end: int


def plan_gmail_temporal_selector_batches(
    *,
    text: str,
    analysis: TemporalLeadAnalysis,
    caps: GmailTemporalBatchCaps | None = None,
) -> GmailTemporalBatchPlan:
    """Partition one analysis into deterministic, endpoint-only selector batches.

    The function is pure: it performs no model call, persistence, routing, or
    admission mutation. Canonical payload size includes all exact source surfaces
    delivered to a selector. Every inventoried expression is either present in
    exactly one batch or represented by a content-free omission record.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    limits = caps or GmailTemporalBatchCaps()
    _validate_analysis(text, analysis)
    source_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    fields = _field_ranges(text, analysis)
    segments = _segments(text, fields, analysis, source_sha256)
    expression_segments = {
        expression.expression_id: _segment_for_span(
            segments,
            field=expression.field,
            start=expression.start,
            end=expression.end,
        )
        for expression in analysis.expressions
    }
    mention_segments = {
        mention.mention_id: _segment_for_span(
            segments,
            field=mention.field,
            start=mention.start,
            end=mention.end,
        )
        for mention in analysis.mentions
    }

    gate_reason: BatchOmissionReason | None = None
    if analysis.association_admission_basis == "none":
        gate_reason = "fact_not_admitted"
    elif not analysis.scope_bound:
        gate_reason = "scope_not_bound"
    if gate_reason is not None:
        omissions = tuple(
            GmailTemporalBatchOmission(
                expression_id=item.expression_id,
                field=item.field,
                segment_id=expression_segments[item.expression_id].segment_id,
                reason=gate_reason,
            )
            for item in analysis.expressions
        )
        return _finalize_plan(
            analysis=analysis,
            source_sha256=source_sha256,
            caps=limits,
            batches=(),
            omissions=omissions,
        )

    by_segment: dict[str, list[TemporalExpression]] = {}
    for expression in sorted(
        analysis.expressions,
        key=lambda item: (item.start, item.end, item.expression_id),
    ):
        segment = expression_segments[expression.expression_id]
        by_segment.setdefault(segment.segment_id, []).append(expression)

    pending: list[tuple[_Segment, tuple[TemporalExpression, ...]]] = []
    for segment in segments:
        values = by_segment.get(segment.segment_id, [])
        for index in range(0, len(values), limits.max_expressions_per_batch):
            pending.append(
                (
                    segment,
                    tuple(values[index : index + limits.max_expressions_per_batch]),
                )
            )

    batches: list[GmailTemporalSelectorBatch] = []
    omissions_list: list[GmailTemporalBatchOmission] = []
    cursor = 0
    while cursor < len(pending):
        segment, expressions = pending[cursor]
        cursor += 1
        if len(batches) >= limits.max_batches:
            omissions_list.extend(
                _omissions_for(expressions, segment, "batch_cap_reached")
            )
            for later_segment, later_expressions in pending[cursor:]:
                omissions_list.extend(
                    _omissions_for(
                        later_expressions,
                        later_segment,
                        "batch_cap_reached",
                    )
                )
            break
        batch = _build_batch(
            text=text,
            analysis=analysis,
            source_sha256=source_sha256,
            segment=segment,
            expressions=expressions,
            mention_segments=mention_segments,
            fields=fields,
            caps=limits,
            sequence=len(batches) + 1,
        )
        if batch is not None:
            batches.append(batch)
            continue
        if len(expressions) > 1:
            midpoint = len(expressions) // 2
            pending[cursor:cursor] = [
                (segment, expressions[:midpoint]),
                (segment, expressions[midpoint:]),
            ]
            continue
        omissions_list.extend(_omissions_for(expressions, segment, "payload_byte_cap"))

    return _finalize_plan(
        analysis=analysis,
        source_sha256=source_sha256,
        caps=limits,
        batches=tuple(batches),
        omissions=tuple(omissions_list),
    )


def gmail_temporal_selector_batch_payload(
    batch: GmailTemporalSelectorBatch,
) -> str:
    """Return the canonical JSON whose UTF-8 size is ``batch.payload_bytes``."""

    payload = _batch_payload_dict(batch)
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def validate_gmail_temporal_batch_citation(
    batch: GmailTemporalSelectorBatch,
    *,
    batch_fingerprint: str,
    expression_id: str,
    subject_mention_id: str,
    lifecycle_mention_id: str | None = None,
    selected_lead_id: str | None = None,
) -> VerifiedGmailTemporalBatchCitation:
    """Reject a selector citation outside the exact batch endpoint subset."""

    _validate_batch_manifest_integrity(batch)
    if batch_fingerprint != batch.manifest.batch_fingerprint:
        raise GmailTemporalBatchAuthorityError(
            "citation does not match the presented batch fingerprint"
        )
    if expression_id not in batch.manifest.expression_ids:
        raise GmailTemporalBatchAuthorityError(
            "citation references an expression outside the batch manifest"
        )
    if subject_mention_id not in batch.manifest.mention_ids:
        raise GmailTemporalBatchAuthorityError(
            "citation references a subject mention outside the batch manifest"
        )
    if (
        lifecycle_mention_id is not None
        and lifecycle_mention_id not in batch.manifest.mention_ids
    ):
        raise GmailTemporalBatchAuthorityError(
            "citation references a lifecycle mention outside the batch manifest"
        )
    if selected_lead_id is not None:
        if selected_lead_id not in batch.manifest.lead_ids:
            raise GmailTemporalBatchAuthorityError(
                "citation references a lead outside the batch manifest"
            )
        lead = next(
            item for item in batch.lead_hints if item.lead_id == selected_lead_id
        )
        if lead.expression_id != expression_id or lead.mention_id != subject_mention_id:
            raise GmailTemporalBatchAuthorityError(
                "selected lead does not match the cited expression and subject"
            )
    return VerifiedGmailTemporalBatchCitation(
        batch_fingerprint=batch_fingerprint,
        expression_id=expression_id,
        subject_mention_id=subject_mention_id,
        lifecycle_mention_id=lifecycle_mention_id,
        selected_lead_id=selected_lead_id,
    )


def validate_gmail_temporal_batch_manifest(
    batch: GmailTemporalSelectorBatch,
) -> None:
    """Verify that one immutable endpoint packet matches its signed manifest.

    This is the citation-free form of the same authority check performed by
    :func:`validate_gmail_temporal_batch_citation`.  Reducers use it to reject a
    mutated or rebound batch even when the corresponding selector row contains
    no associations.
    """

    if not isinstance(batch, GmailTemporalSelectorBatch):
        raise GmailTemporalBatchAuthorityError("batch endpoint packet is invalid")
    _validate_batch_manifest_integrity(batch)


def _build_batch(
    *,
    text: str,
    analysis: TemporalLeadAnalysis,
    source_sha256: str,
    segment: _Segment,
    expressions: tuple[TemporalExpression, ...],
    mention_segments: dict[str, _Segment],
    fields: tuple[_FieldRange, ...],
    caps: GmailTemporalBatchCaps,
    sequence: int,
) -> GmailTemporalSelectorBatch | None:
    expression_hull = (
        min(item.start for item in expressions),
        max(item.end for item in expressions),
    )
    local_start, local_end, local_trimmed = _initial_local_context(
        segment=segment,
        expression_hull=expression_hull,
        fields=fields,
        caps=caps,
    )
    local_mentions = tuple(
        item
        for item in analysis.mentions
        if mention_segments[item.mention_id].segment_id == segment.segment_id
        and local_start <= item.start
        and item.end <= local_end
    )
    subject_field = next((item for item in fields if item.name == "subject"), None)
    subject_context: tuple[int, int] | None = None
    subject_trimmed = False
    bridge_mentions: tuple[TemporalMention, ...] = ()
    if segment.field == "body" and subject_field is not None:
        subject_context, subject_trimmed = _subject_context(
            subject_field,
            caps.max_subject_context_chars,
        )
        bridge_mentions = tuple(
            item
            for item in analysis.mentions
            if item.field == "subject"
            and subject_context[0] <= item.start
            and item.end <= subject_context[1]
        )

    ranked_mentions = _rank_mentions(
        expressions=expressions,
        local_mentions=local_mentions,
        bridge_mentions=bridge_mentions,
    )
    selected_mentions = list(ranked_mentions[: caps.max_mentions_per_batch])
    selected_mentions = _ensure_subject_bridge(
        selected_mentions,
        ranked_mentions,
        limit=caps.max_mentions_per_batch,
    )
    relevant_mentions = {
        item.mention_id
        for item in analysis.mentions
        if mention_segments[item.mention_id].segment_id == segment.segment_id
    }
    relevant_mentions.update(item.mention_id for item in bridge_mentions)

    expression_views = tuple(
        GmailTemporalBatchExpression(
            expression_id=item.expression_id,
            field=item.field,
            start=item.start,
            end=item.end,
            surface=text[item.start : item.end],
            form=item.form,
            resolution_status=item.resolution_status,
        )
        for item in expressions
    )
    diagnostics: list[str] = []
    if local_trimmed:
        diagnostics.append("local_context_trimmed")
    if subject_trimmed:
        diagnostics.append("subject_bridge_context_trimmed")

    contexts = _contexts(
        text=text,
        source_sha256=source_sha256,
        segment=segment,
        local=(local_start, local_end),
        subject=subject_context,
    )
    mention_views = _mention_views(
        text,
        selected_mentions,
        local_field=segment.field,
    )
    lead_hints = list(
        _lead_hints(
            analysis.leads,
            expression_ids={item.expression_id for item in expressions},
            mention_ids={item.mention_id for item in selected_mentions},
        )[: caps.max_lead_hints_per_batch]
    )
    relevant_leads = {
        item.lead_id
        for item in analysis.leads
        if item.expression_id in {value.expression_id for value in expressions}
        and item.mention_id in relevant_mentions
    }

    def finalize() -> GmailTemporalSelectorBatch:
        selected_mention_ids = {item.mention_id for item in mention_views}
        selected_lead_ids = {item.lead_id for item in lead_hints}
        local_diagnostics = list(diagnostics)
        if relevant_mentions - selected_mention_ids:
            local_diagnostics.append("mention_candidates_truncated")
        if relevant_leads - selected_lead_ids:
            local_diagnostics.append("lead_hints_truncated")
        return _finalize_batch(
            analysis=analysis,
            source_sha256=source_sha256,
            sequence=sequence,
            segment=segment,
            contexts=contexts,
            expressions=expression_views,
            mentions=tuple(mention_views),
            lead_hints=tuple(lead_hints),
            omitted_mention_count=len(relevant_mentions - selected_mention_ids),
            omitted_lead_hint_count=len(relevant_leads - selected_lead_ids),
            diagnostics=_ordered_unique(local_diagnostics),
        )

    batch = finalize()
    if batch.payload_bytes <= caps.max_payload_bytes:
        return batch

    if lead_hints:
        diagnostics.append("lead_hints_removed_for_byte_cap")
        lead_hints.clear()
        batch = finalize()
        if batch.payload_bytes <= caps.max_payload_bytes:
            return batch

    tight_local = _tight_context_span(
        spans=[(item.start, item.end) for item in expressions]
        + [
            (item.start, item.end)
            for item in selected_mentions
            if item.field == segment.field
        ],
        field=next(item for item in fields if item.name == segment.field),
        padding=min(caps.overlap_chars, 48),
    )
    tight_subject = _tight_subject_context(
        selected_mentions,
        subject_field=subject_field if segment.field == "body" else None,
        padding=min(caps.overlap_chars, 24),
    )
    if (local_start, local_end) != tight_local or subject_context != tight_subject:
        diagnostics.append("payload_context_trimmed")
        if subject_context is not None and tight_subject is None:
            diagnostics.append("subject_bridge_removed_for_byte_cap")
        contexts = _contexts(
            text=text,
            source_sha256=source_sha256,
            segment=segment,
            local=tight_local,
            subject=tight_subject,
        )
        batch = finalize()
        if batch.payload_bytes <= caps.max_payload_bytes:
            return batch

    while selected_mentions:
        removed = selected_mentions.pop()
        mention_views = _mention_views(
            text,
            selected_mentions,
            local_field=segment.field,
        )
        lead_hints[:] = [
            item for item in lead_hints if item.mention_id != removed.mention_id
        ]
        diagnostics.append("mentions_removed_for_byte_cap")
        if removed.field == "subject" and segment.field == "body":
            diagnostics.append("subject_bridge_removed_for_byte_cap")
        tight_local = _tight_context_span(
            spans=[(item.start, item.end) for item in expressions]
            + [
                (item.start, item.end)
                for item in selected_mentions
                if item.field == segment.field
            ],
            field=next(item for item in fields if item.name == segment.field),
            padding=0,
        )
        tight_subject = _tight_subject_context(
            selected_mentions,
            subject_field=subject_field if segment.field == "body" else None,
            padding=0,
        )
        contexts = _contexts(
            text=text,
            source_sha256=source_sha256,
            segment=segment,
            local=tight_local,
            subject=tight_subject,
        )
        batch = finalize()
        if batch.payload_bytes <= caps.max_payload_bytes:
            return batch

    contexts = _contexts(
        text=text,
        source_sha256=source_sha256,
        segment=segment,
        local=expression_hull,
        subject=None,
    )
    diagnostics.append("minimal_expression_context_only")
    batch = finalize()
    if batch.payload_bytes <= caps.max_payload_bytes:
        return batch
    return None


def _finalize_batch(
    *,
    analysis: TemporalLeadAnalysis,
    source_sha256: str,
    sequence: int,
    segment: _Segment,
    contexts: tuple[GmailTemporalBatchContext, ...],
    expressions: tuple[GmailTemporalBatchExpression, ...],
    mentions: tuple[GmailTemporalBatchMention, ...],
    lead_hints: tuple[GmailTemporalBatchLeadHint, ...],
    omitted_mention_count: int,
    omitted_lead_hint_count: int,
    diagnostics: tuple[str, ...],
) -> GmailTemporalSelectorBatch:
    material = _batch_manifest_material(
        analysis_fingerprint=analysis.snapshot_fingerprint,
        source_sha256=source_sha256,
        sequence=sequence,
        field=segment.field,
        segment_id=segment.segment_id,
        segment_start=segment.start,
        segment_end=segment.end,
        contexts=contexts,
        expressions=expressions,
        mentions=mentions,
        lead_hints=lead_hints,
        omitted_mention_count=omitted_mention_count,
        omitted_lead_hint_count=omitted_lead_hint_count,
        diagnostics=diagnostics,
    )
    fingerprint = "gtb_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()
    manifest = GmailTemporalBatchManifest(
        version=_MANIFEST_VERSION,
        batch_fingerprint=fingerprint,
        analysis_fingerprint=analysis.snapshot_fingerprint,
        source_sha256=source_sha256,
        expression_ids=tuple(item.expression_id for item in expressions),
        mention_ids=tuple(item.mention_id for item in mentions),
        lead_ids=tuple(item.lead_id for item in lead_hints),
    )
    batch = GmailTemporalSelectorBatch(
        version=_BATCH_VERSION,
        sequence=sequence,
        field=segment.field,
        segment_id=segment.segment_id,
        segment_start=segment.start,
        segment_end=segment.end,
        manifest=manifest,
        contexts=contexts,
        expressions=expressions,
        mentions=mentions,
        lead_hints=lead_hints,
        payload_bytes=0,
        omitted_mention_count=omitted_mention_count,
        omitted_lead_hint_count=omitted_lead_hint_count,
        diagnostics=diagnostics,
    )
    payload_bytes = len(gmail_temporal_selector_batch_payload(batch).encode("utf-8"))
    return replace(batch, payload_bytes=payload_bytes)


def _finalize_plan(
    *,
    analysis: TemporalLeadAnalysis,
    source_sha256: str,
    caps: GmailTemporalBatchCaps,
    batches: tuple[GmailTemporalSelectorBatch, ...],
    omissions: tuple[GmailTemporalBatchOmission, ...],
) -> GmailTemporalBatchPlan:
    covered = tuple(
        item.expression_id for batch in batches for item in batch.expressions
    )
    omitted_ids = tuple(item.expression_id for item in omissions)
    inventoried = {item.expression_id for item in analysis.expressions}
    accounted = set(covered) | set(omitted_ids)
    if (
        len(covered) != len(set(covered))
        or len(omitted_ids) != len(set(omitted_ids))
        or set(covered) & set(omitted_ids)
        or accounted != inventoried
    ):
        raise GmailTemporalBatchingError(
            "batch plan did not account for every expression exactly once"
        )
    material = {
        "schema": _PLAN_VERSION,
        "analysis_fingerprint": analysis.snapshot_fingerprint,
        "source_sha256": source_sha256,
        "caps": asdict(caps),
        "batch_fingerprints": [item.manifest.batch_fingerprint for item in batches],
        "omissions": [asdict(item) for item in omissions],
        "routable": False,
    }
    plan_fingerprint = "gtp_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()
    return GmailTemporalBatchPlan(
        version=_PLAN_VERSION,
        analysis_fingerprint=analysis.snapshot_fingerprint,
        source_sha256=source_sha256,
        plan_fingerprint=plan_fingerprint,
        caps=caps,
        batches=batches,
        covered_expression_ids=covered,
        omissions=omissions,
    )


def _batch_payload_dict(batch: GmailTemporalSelectorBatch) -> dict[str, object]:
    return {
        "version": batch.version,
        "manifest_version": batch.manifest.version,
        "analysis_fingerprint": batch.manifest.analysis_fingerprint,
        "batch_fingerprint": batch.manifest.batch_fingerprint,
        "source_sha256": batch.manifest.source_sha256,
        "sequence": batch.sequence,
        "segment": {
            "segment_id": batch.segment_id,
            "field": batch.field,
            "start": batch.segment_start,
            "end": batch.segment_end,
        },
        "contexts": [asdict(item) for item in batch.contexts],
        "expressions": [asdict(item) for item in batch.expressions],
        "mentions": [asdict(item) for item in batch.mentions],
        "lead_hints": [asdict(item) for item in batch.lead_hints],
        "authority": {
            "expression_ids": list(batch.manifest.expression_ids),
            "mention_ids": list(batch.manifest.mention_ids),
            "lead_ids": list(batch.manifest.lead_ids),
        },
        "diagnostics": {
            "omitted_mention_count": batch.omitted_mention_count,
            "omitted_lead_hint_count": batch.omitted_lead_hint_count,
            "flags": list(batch.diagnostics),
        },
        "routable": False,
    }


def _batch_manifest_material(
    *,
    analysis_fingerprint: str,
    source_sha256: str,
    sequence: int,
    field: BatchField,
    segment_id: str,
    segment_start: int,
    segment_end: int,
    contexts: tuple[GmailTemporalBatchContext, ...],
    expressions: tuple[GmailTemporalBatchExpression, ...],
    mentions: tuple[GmailTemporalBatchMention, ...],
    lead_hints: tuple[GmailTemporalBatchLeadHint, ...],
    omitted_mention_count: int,
    omitted_lead_hint_count: int,
    diagnostics: tuple[str, ...],
) -> dict[str, object]:
    return {
        "schema": _MANIFEST_VERSION,
        "analysis_fingerprint": analysis_fingerprint,
        "source_sha256": source_sha256,
        "sequence": sequence,
        "field": field,
        "segment_id": segment_id,
        "segment_start": segment_start,
        "segment_end": segment_end,
        "contexts": [asdict(item) for item in contexts],
        "expressions": [asdict(item) for item in expressions],
        "mentions": [asdict(item) for item in mentions],
        "lead_hints": [asdict(item) for item in lead_hints],
        "omitted_mention_count": omitted_mention_count,
        "omitted_lead_hint_count": omitted_lead_hint_count,
        "diagnostics": list(diagnostics),
        "routable": False,
    }


def _validate_batch_manifest_integrity(
    batch: GmailTemporalSelectorBatch,
) -> None:
    if (
        not isinstance(batch, GmailTemporalSelectorBatch)
        or batch.version != _BATCH_VERSION
        or batch.routable is not False
        or isinstance(batch.sequence, bool)
        or not isinstance(batch.sequence, int)
        or batch.sequence < 1
    ):
        raise GmailTemporalBatchAuthorityError("batch endpoint packet is invalid")
    if batch.manifest.version != _MANIFEST_VERSION:
        raise GmailTemporalBatchAuthorityError("batch manifest version is invalid")
    if batch.manifest.expression_ids != tuple(
        item.expression_id for item in batch.expressions
    ):
        raise GmailTemporalBatchAuthorityError(
            "batch expression manifest does not match its endpoint packet"
        )
    if batch.manifest.mention_ids != tuple(item.mention_id for item in batch.mentions):
        raise GmailTemporalBatchAuthorityError(
            "batch mention manifest does not match its endpoint packet"
        )
    if batch.manifest.lead_ids != tuple(item.lead_id for item in batch.lead_hints):
        raise GmailTemporalBatchAuthorityError(
            "batch lead manifest does not match its hint packet"
        )
    material = _batch_manifest_material(
        analysis_fingerprint=batch.manifest.analysis_fingerprint,
        source_sha256=batch.manifest.source_sha256,
        sequence=batch.sequence,
        field=batch.field,
        segment_id=batch.segment_id,
        segment_start=batch.segment_start,
        segment_end=batch.segment_end,
        contexts=batch.contexts,
        expressions=batch.expressions,
        mentions=batch.mentions,
        lead_hints=batch.lead_hints,
        omitted_mention_count=batch.omitted_mention_count,
        omitted_lead_hint_count=batch.omitted_lead_hint_count,
        diagnostics=batch.diagnostics,
    )
    expected = "gtb_" + hashlib.sha256(_canonical_bytes(material)).hexdigest()
    if batch.manifest.batch_fingerprint != expected:
        raise GmailTemporalBatchAuthorityError(
            "batch manifest fingerprint does not match its packet"
        )
    actual_payload_bytes = len(
        gmail_temporal_selector_batch_payload(batch).encode("utf-8")
    )
    if batch.payload_bytes != actual_payload_bytes:
        raise GmailTemporalBatchAuthorityError(
            "batch payload byte count does not match its packet"
        )


def _contexts(
    *,
    text: str,
    source_sha256: str,
    segment: _Segment,
    local: tuple[int, int],
    subject: tuple[int, int] | None,
) -> tuple[GmailTemporalBatchContext, ...]:
    values = [
        GmailTemporalBatchContext(
            context_id=_context_id(source_sha256, "local", *local),
            role="local",
            field=segment.field,
            start=local[0],
            end=local[1],
            surface=text[local[0] : local[1]],
        )
    ]
    if subject is not None and subject[0] < subject[1]:
        values.append(
            GmailTemporalBatchContext(
                context_id=_context_id(source_sha256, "subject_bridge", *subject),
                role="subject_bridge",
                field="subject",
                start=subject[0],
                end=subject[1],
                surface=text[subject[0] : subject[1]],
            )
        )
    return tuple(values)


def _mention_views(
    text: str,
    mentions: Sequence[TemporalMention | GmailTemporalBatchMention],
    *,
    local_field: BatchField,
) -> list[GmailTemporalBatchMention]:
    output: list[GmailTemporalBatchMention] = []
    for item in mentions:
        if isinstance(item, GmailTemporalBatchMention):
            output.append(item)
            continue
        role: BatchMentionRole = (
            "local" if item.field == local_field else "subject_bridge"
        )
        output.append(
            GmailTemporalBatchMention(
                mention_id=item.mention_id,
                candidate_role=role,
                field=item.field,
                start=item.start,
                end=item.end,
                surface=text[item.start : item.end],
                mention_type=item.mention_type,
                lifecycle_role=item.lifecycle_role,
            )
        )
    return output


def _lead_hints(
    leads: tuple[TemporalLead, ...],
    *,
    expression_ids: set[str],
    mention_ids: set[str],
) -> tuple[GmailTemporalBatchLeadHint, ...]:
    eligible = [
        item
        for item in leads
        if item.expression_id in expression_ids and item.mention_id in mention_ids
    ]
    eligible.sort(
        key=lambda item: (
            _LEAD_TIER_RANK.get(item.confidence_tier, 99),
            _LEAD_MODE_RANK.get(item.association_mode, 99),
            item.gap_chars,
            item.lead_id,
        )
    )
    return tuple(
        GmailTemporalBatchLeadHint(
            lead_id=item.lead_id,
            expression_id=item.expression_id,
            mention_id=item.mention_id,
            association_mode=item.association_mode,
            confidence_tier=item.confidence_tier,
            blockers=item.blockers,
            risk_features=item.risk_features,
        )
        for item in eligible
    )


def _rank_mentions(
    *,
    expressions: tuple[TemporalExpression, ...],
    local_mentions: tuple[TemporalMention, ...],
    bridge_mentions: tuple[TemporalMention, ...],
) -> tuple[TemporalMention, ...]:
    def rank(item: TemporalMention) -> tuple[int, int, int, int, str]:
        role_rank = 0 if item.field != "subject" else 1
        type_rank = _MENTION_TYPE_RANK.get(item.mention_type, 50)
        distance = min(
            _span_distance(item.start, item.end, value.start, value.end)
            for value in expressions
        )
        return role_rank, type_rank, distance, item.start, item.mention_id

    values = {item.mention_id: item for item in (*local_mentions, *bridge_mentions)}
    return tuple(sorted(values.values(), key=rank))


def _ensure_subject_bridge(
    selected: list[TemporalMention],
    ranked: tuple[TemporalMention, ...],
    *,
    limit: int,
) -> list[TemporalMention]:
    if any(item.field == "subject" for item in selected):
        return selected
    bridge = next(
        (
            item
            for item in ranked
            if item.field == "subject" and item.mention_type in _SUBJECT_BRIDGE_TYPES
        ),
        next((item for item in ranked if item.field == "subject"), None),
    )
    if bridge is None:
        return selected
    if len(selected) >= limit:
        selected[-1] = bridge
    else:
        selected.append(bridge)
    return sorted(
        {item.mention_id: item for item in selected}.values(),
        key=lambda item: (item.field == "subject", item.start, item.mention_id),
    )


def _initial_local_context(
    *,
    segment: _Segment,
    expression_hull: tuple[int, int],
    fields: tuple[_FieldRange, ...],
    caps: GmailTemporalBatchCaps,
) -> tuple[int, int, bool]:
    field = next(item for item in fields if item.name == segment.field)
    desired_start = max(field.start, segment.start - caps.overlap_chars)
    desired_end = min(field.end, segment.end + caps.overlap_chars)
    if desired_end - desired_start <= caps.max_local_context_chars:
        return desired_start, desired_end, False
    start, end = _bounded_span(
        lower=field.start,
        upper=field.end,
        required=expression_hull,
        limit=max(
            caps.max_local_context_chars, expression_hull[1] - expression_hull[0]
        ),
    )
    return start, end, True


def _subject_context(
    field: _FieldRange,
    limit: int,
) -> tuple[tuple[int, int], bool]:
    if field.end - field.start <= limit:
        return (field.start, field.end), False
    return (field.start, field.start + limit), True


def _tight_context_span(
    *,
    spans: list[tuple[int, int]],
    field: _FieldRange,
    padding: int,
) -> tuple[int, int]:
    start = max(field.start, min(item[0] for item in spans) - padding)
    end = min(field.end, max(item[1] for item in spans) + padding)
    return start, end


def _tight_subject_context(
    mentions: Sequence[TemporalMention | GmailTemporalBatchMention],
    *,
    subject_field: _FieldRange | None,
    padding: int,
) -> tuple[int, int] | None:
    if subject_field is None:
        return None
    subject_mentions = [item for item in mentions if item.field == "subject"]
    if not subject_mentions:
        return None
    return (
        max(
            subject_field.start, min(item.start for item in subject_mentions) - padding
        ),
        min(subject_field.end, max(item.end for item in subject_mentions) + padding),
    )


def _bounded_span(
    *,
    lower: int,
    upper: int,
    required: tuple[int, int],
    limit: int,
) -> tuple[int, int]:
    required_length = required[1] - required[0]
    if required_length >= limit:
        return required
    spare = limit - required_length
    start = max(lower, required[0] - spare // 2)
    end = min(upper, start + limit)
    start = max(lower, end - limit)
    return start, end


def _field_ranges(
    text: str,
    analysis: TemporalLeadAnalysis,
) -> tuple[_FieldRange, ...]:
    if not text.startswith("Subject: "):
        return (_FieldRange("message", 0, len(text)),)
    line_end = text.find("\n")
    if line_end < 0:
        values: list[_FieldRange] = [
            _FieldRange("subject", len("Subject: "), len(text))
        ]
    else:
        body_start = line_end
        while body_start < len(text) and text[body_start] == "\n":
            body_start += 1
        values = [_FieldRange("subject", len("Subject: "), line_end)]
        if body_start < len(text):
            values.append(_FieldRange("body", body_start, len(text)))
    if any(
        item.field == "message" for item in (*analysis.expressions, *analysis.mentions)
    ):
        values.append(_FieldRange("message", 0, len(text)))
    return tuple(values)


def _segments(
    text: str,
    fields: tuple[_FieldRange, ...],
    analysis: TemporalLeadAnalysis,
    source_sha256: str,
) -> tuple[_Segment, ...]:
    endpoint_spans = [
        (item.field, item.start, item.end)
        for item in (*analysis.expressions, *analysis.mentions)
    ]
    output: list[_Segment] = []
    for field in fields:
        boundaries = {field.start, field.end}
        if field.name != "subject":
            for match in _SENTENCE_BOUNDARY_RE.finditer(text, field.start, field.end):
                boundaries.add(match.end())
            boundaries.update(_quote_boundaries(text, field))
        safe_boundaries = sorted(
            value
            for value in boundaries
            if not any(
                endpoint_field == field.name and start < value < end
                for endpoint_field, start, end in endpoint_spans
            )
        )
        for start, end in zip(safe_boundaries, safe_boundaries[1:]):
            start, end = _trim_span(text, start, end)
            if start >= end:
                continue
            index = len(output) + 1
            output.append(
                _Segment(
                    segment_id=f"gts_{source_sha256[:16]}:s{index}",
                    field=field.name,
                    start=start,
                    end=end,
                )
            )
    return tuple(output)


def _quote_boundaries(text: str, field: _FieldRange) -> set[int]:
    boundaries: set[int] = set()
    cursor = field.start
    previous_quoted = False
    for line in text[field.start : field.end].splitlines(keepends=True):
        stripped = line.lstrip()
        quoted = stripped.startswith(">")
        marker = bool(_FORWARD_MARKER_RE.fullmatch(line.strip()))
        if marker or quoted != previous_quoted:
            boundaries.add(cursor)
        previous_quoted = quoted
        cursor += len(line)
    return boundaries


def _segment_for_span(
    segments: tuple[_Segment, ...],
    *,
    field: BatchField,
    start: int,
    end: int,
) -> _Segment:
    for segment in segments:
        if segment.field == field and segment.start <= start and end <= segment.end:
            return segment
    raise GmailTemporalBatchingError(
        "an endpoint could not be assigned to one deterministic segment"
    )


def _validate_analysis(text: str, analysis: TemporalLeadAnalysis) -> None:
    if not _FINGERPRINT_RE.fullmatch(analysis.snapshot_fingerprint):
        raise GmailTemporalBatchingError("analysis fingerprint is malformed")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != analysis.source_sha256:
        raise GmailTemporalBatchingError(
            "analysis source fingerprint does not match selector text"
        )
    fields = _field_ranges(text, analysis)
    expression_ids: set[str] = set()
    mention_ids: set[str] = set()
    lead_ids: set[str] = set()
    for item in analysis.expressions:
        _validate_endpoint_span(text, fields, item)
        if not _EXPRESSION_ID_RE.fullmatch(item.expression_id):
            raise GmailTemporalBatchingError("expression ID is malformed")
        if item.expression_id in expression_ids:
            raise GmailTemporalBatchingError("expression IDs must be unique")
        expression_ids.add(item.expression_id)
    for item in analysis.mentions:
        _validate_endpoint_span(text, fields, item)
        if not _MENTION_ID_RE.fullmatch(item.mention_id):
            raise GmailTemporalBatchingError("mention ID is malformed")
        if item.mention_id in mention_ids:
            raise GmailTemporalBatchingError("mention IDs must be unique")
        mention_ids.add(item.mention_id)
    for item in analysis.leads:
        if not _LEAD_ID_RE.fullmatch(item.lead_id):
            raise GmailTemporalBatchingError("lead ID is malformed")
        if item.lead_id in lead_ids:
            raise GmailTemporalBatchingError("lead IDs must be unique")
        if (
            item.expression_id not in expression_ids
            or item.mention_id not in mention_ids
        ):
            raise GmailTemporalBatchingError("lead references an unknown endpoint")
        lead_ids.add(item.lead_id)


def _validate_endpoint_span(
    text: str,
    fields: tuple[_FieldRange, ...],
    item: TemporalExpression | TemporalMention,
) -> None:
    if (
        isinstance(item.start, bool)
        or isinstance(item.end, bool)
        or not isinstance(item.start, int)
        or not isinstance(item.end, int)
        or item.start < 0
        or item.start >= item.end
        or item.end > len(text)
    ):
        raise GmailTemporalBatchingError("endpoint span is outside the source text")
    expected: BatchField = "message"
    for field in fields:
        if field.name == "message" and len(fields) > 1:
            continue
        if field.start <= item.start and item.end <= field.end:
            expected = field.name
            break
    if item.field != expected:
        raise GmailTemporalBatchingError(
            "endpoint field does not match its source span"
        )


def _omissions_for(
    expressions: tuple[TemporalExpression, ...],
    segment: _Segment,
    reason: BatchOmissionReason,
) -> list[GmailTemporalBatchOmission]:
    return [
        GmailTemporalBatchOmission(
            expression_id=item.expression_id,
            field=item.field,
            segment_id=segment.segment_id,
            reason=reason,
        )
        for item in expressions
    ]


def _context_id(source_sha256: str, role: str, start: int, end: int) -> str:
    material = f"{source_sha256}\0{role}\0{start}\0{end}".encode("utf-8")
    return "gtc_" + hashlib.sha256(material).hexdigest()[:24]


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _span_distance(
    first_start: int,
    first_end: int,
    second_start: int,
    second_end: int,
) -> int:
    if first_end <= second_start:
        return second_start - first_end
    if second_end <= first_start:
        return first_start - second_end
    return 0


def _ordered_unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
