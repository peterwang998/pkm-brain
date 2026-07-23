from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Literal

from .gmail_temporal_leads import (
    TemporalLeadAnalysis,
    TemporalLead,
    TemporalExpression,
    TemporalMention,
    analyze_gmail_temporal_leads,
)


ASSESSMENT_VERSION = "gmail_temporal_source_assertion_assessment_v1"

AssertionDisposition = Literal["asserted", "blocked"]

_SUPPORTED_LIFECYCLE_ROLES = frozenset(
    {"scheduled", "cancelled", "completed", "rescheduled"}
)
_NEGATION_BEFORE_CUE = re.compile(
    r"\b(?:not|never|isn['’]?t|wasn['’]?t|weren['’]?t|"
    r"hasn['’]?t|hadn['’]?t|won['’]?t|cannot|can['’]?t|"
    r"ain['’]?t)"
    r"(?:\s+[A-Za-z]+){0,5}\s*$",
    re.IGNORECASE,
)
_NEGATION_WITHIN_EVIDENCE = re.compile(
    r"\b(?:not|never|isn['’]?t|wasn['’]?t|weren['’]?t|"
    r"hasn['’]?t|hadn['’]?t|won['’]?t|cannot|can['’]?t|"
    r"ain['’]?t)\b",
    re.IGNORECASE,
)
_DENIAL_BEFORE_CUE = re.compile(
    r"\b(?:"
    r"(?:do|does|did)\s+not|don['’]?t|doesn['’]?t|didn['’]?t"
    r")\s+(?:really\s+)?(?:think|believe|expect|accept|agree|confirm)\b|"
    r"\b(?:deny|denies|denied|dispute|disputes|disputed|reject|rejects|rejected)"
    r"(?:\s+(?:the\s+)?(?:claim|report|rumou?r))?(?:\s+that)?\b|"
    r"\bno\s+(?:credible\s+)?(?:evidence|confirmation|proof)\s+that\b",
    re.IGNORECASE,
)
_CONDITIONAL_BEFORE_CUE = re.compile(
    r"\b(?:if|unless|assuming|supposing|provided\s+that|in\s+case|"
    r"on\s+condition\s+that)\b",
    re.IGNORECASE,
)
_CONDITIONAL_AFTER_CUE = re.compile(
    r"\b(?:if|unless|assuming|provided\s+that|on\s+condition\s+that)\b",
    re.IGNORECASE,
)
_MODAL_BEFORE_CUE = re.compile(
    r"\b(?:might|may|could|would|should|can|must)"
    r"(?:\s+(?:still|already|now|possibly|probably))?"
    r"(?:\s+(?:be|have\s+been))?\s*$",
    re.IGNORECASE,
)
_MODAL_WITHIN_EVIDENCE = re.compile(
    r"\b(?:might|may|could|would|should|can|must)"
    r"(?:\s+(?:still|already|now|possibly|probably))?"
    r"(?:\s+(?:be|have\s+been))?\b",
    re.IGNORECASE,
)
_FUTURE_LIFECYCLE_BEFORE_CUE = re.compile(
    r"\bwill(?:\s+(?:still|now))?\s+be\s*$",
    re.IGNORECASE,
)
_PROSPECTIVE_BEFORE_CUE = re.compile(
    r"\b(?:is|are|was|were)?\s*(?:expected|projected|rumou?red)\s+to\s+be\s*$",
    re.IGNORECASE,
)
_PROSPECTIVE_WITHIN_EVIDENCE = re.compile(
    r"\b(?:expected|projected|rumou?red)\s+to\s+(?:be|occur|happen)\b",
    re.IGNORECASE,
)
_EPISTEMIC_BEFORE_CUE = re.compile(
    r"\b(?:probably|possibly|perhaps|maybe|likely|unlikely|allegedly|apparently|"
    r"presumably|supposedly|reportedly|conceivably)\b|"
    r"\b(?:is|was)\s+(?:probable|possible|likely|unlikely)\s+that\b|"
    r"\b(?:it\s+)?(?:appears?|seems?)(?:\s+(?:to|that|as\s+if))?\b|"
    r"\b(?:I|we)\s+(?:think|believe|suspect|guess|suppose|doubt)\b|"
    r"\bas\s+far\s+as\s+(?:I|we)\s+know\b",
    re.IGNORECASE,
)
_REPORTED_BEFORE_CUE = re.compile(
    r"\b(?:according\s+to|per)\s+[^,;.!?\r\n]{1,100}[,:]?|"
    r"\b(?:we|I)\s+(?:heard|were\s+told|was\s+told)\b|"
    r"\b(?:someone|they|he|she|the\s+(?:sender|report|message|notice))\s+"
    r"(?:said|says|reported|reports|claimed|claims|wrote|writes|announced)\b",
    re.IGNORECASE,
)
_NAMED_REPORTED_BEFORE_CUE = re.compile(
    r"\b(?:[A-Z][A-Za-z’'\-]*)(?:\s+[A-Z][A-Za-z’'\-]*){0,3}\s+"
    r"(?i:said|says|reported|reports|claimed|claims|wrote|writes|announced|"
    r"confirmed|confirms|told\s+(?:me|us)|tells\s+(?:me|us))\b"
)
_FALSE_THAT_BEFORE_CUE = re.compile(
    r"\b(?:(?:it|that|this)\s+(?:is|was)\s+)?"
    r"(?:false|untrue|incorrect|wrong|mistaken|not\s+true)\s+that\b|"
    r"\b(?:false|untrue|incorrect|wrong|mistaken)\s+(?:claim|report|rumou?r)"
    r"(?:\s+that)?\b",
    re.IGNORECASE,
)
_REFUTATION_AFTER_CUE = re.compile(
    r"(?:\bbut\s+)?(?:that|this|it|the\s+(?:claim|report|rumou?r))\s+"
    r"(?:is|was|remains)\s+(?:wrong|false|incorrect|untrue|mistaken)\b|"
    r"(?:[,;:—–-]\s*|\b(?:is|was)\s+)"
    r"(?:wrong|false|incorrect|untrue|mistaken)\b",
    re.IGNORECASE,
)
_REPORTED_AFTER_CUE = re.compile(
    r"(?:[,;—–-]\s*|\b(?:as|so)\s+)"
    r"(?:according\s+to\s+[^,;.!?\r\n]{1,100}|"
    r"(?:someone|they|he|she)\s+"
    r"(?:said|says|reported|reports|claimed|claims|wrote|writes)|"
    r"(?:or\s+)?so\s+(?:I|we)\s+(?:was|were)\s+told)\b",
    re.IGNORECASE,
)
_NAMED_REPORTED_AFTER_CUE = re.compile(
    r"(?:[,;—–-]\s*|\b(?:as|so)\s+)"
    r"(?:[A-Z][A-Za-z’'\-]*)(?:\s+[A-Z][A-Za-z’'\-]*){0,3}\s+"
    r"(?i:said|says|reported|reports|claimed|claims|wrote|writes|confirmed|"
    r"confirms)\b"
)
_EPISTEMIC_AFTER_CUE = re.compile(
    r"(?:[,;—–-]\s*|\b(?:is|was)\s+)"
    r"(?:probably|possibly|perhaps|maybe|allegedly|apparently|presumably|"
    r"supposedly|reportedly|conceivably|it\s+(?:appears?|seems?))\b",
    re.IGNORECASE,
)
_INTERROGATIVE_PREFIX = re.compile(
    r"\A\s*(?:was|were|is|are|has|have|had|did|does|do|can|could|"
    r"might|may|would|should|will|why|when|where|who|what|how)\b|"
    r"\b(?:do\s+you\s+know|can\s+you\s+confirm|I\s+wonder|whether)\b",
    re.IGNORECASE,
)
_UNPUNCTUATED_QUESTION_PREFIX = re.compile(
    r"\A\s*(?:was|were|is|are|has|have|had|did|does|do|can|could|"
    r"might|may|would|should|will)\s+"
    r"(?:I|we|you|they|he|she|it|this|that|the|an?\b)",
    re.IGNORECASE,
)
_CONFIRMATION_REQUEST = re.compile(
    r"\b(?:please\s+confirm|can\s+you\s+confirm|could\s+you\s+confirm|"
    r"I\s+wonder(?:\s+whether)?|whether)\b",
    re.IGNORECASE,
)
_COORDINATING_BOUNDARY = re.compile(
    r"(?:,\s*|\s+)\b(?:and|but|so|yet|then)\b\s*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GmailTemporalLifecycleAssertion:
    """Text-free semantic metadata for one lifecycle assertion candidate."""

    version: Literal["gmail_temporal_lifecycle_assertion_v1"]
    assertion_id: str
    mention_id: str
    lifecycle_role: str
    evidence_start: int
    evidence_end: int
    disposition: AssertionDisposition
    blockers: tuple[str, ...]

    @property
    def primary_blocker(self) -> str | None:
        return self.blockers[0] if self.blockers else None


@dataclass(frozen=True)
class GmailTemporalLeadAssertion:
    """Text-free semantic metadata for one canonical temporal lead."""

    version: Literal["gmail_temporal_lead_assertion_v1"]
    assertion_id: str
    lead_id: str
    expression_id: str
    mention_id: str
    relation: str | None
    kind: str | None
    evidence_start: int
    evidence_end: int
    disposition: AssertionDisposition
    blockers: tuple[str, ...]

    @property
    def primary_blocker(self) -> str | None:
        return self.blockers[0] if self.blockers else None


@dataclass(frozen=True)
class GmailTemporalSourceAssertionAssessment:
    """Pure, disabled, content-redacted semantic assertion assessment."""

    version: Literal["gmail_temporal_source_assertion_assessment_v1"]
    assessment_fingerprint: str
    source_sha256: str
    analysis_fingerprint: str
    analysis: TemporalLeadAnalysis
    lifecycle_assertions: tuple[GmailTemporalLifecycleAssertion, ...]
    lead_assertions: tuple[GmailTemporalLeadAssertion, ...]
    production_integration_enabled: Literal[False] = False
    creates_facts: Literal[False] = False
    creates_identities: Literal[False] = False
    creates_times: Literal[False] = False


def assess_gmail_temporal_source_assertions(
    *,
    text: str,
    message_internal_at: str | datetime | None,
    fact_admitted: bool,
    temporal_review_rescue: bool = False,
    chunk_id: str | None = None,
) -> GmailTemporalSourceAssertionAssessment:
    """Assess source-local lifecycle assertions without production authority.

    The lead analyzer remains the sole evidence inventory. This layer marks
    whether each lifecycle mention and canonical temporal lead is asserted by
    the current source or occurs under quoted, hypothetical, epistemic,
    attributed, denied, refuted, or interrogative language. Blocked evidence
    remains visible so review and retrieval experiments can fail closed without
    losing recall diagnostics.
    """

    analysis = analyze_gmail_temporal_leads(
        text=text,
        message_internal_at=message_internal_at,
        fact_admitted=fact_admitted,
        temporal_review_rescue=temporal_review_rescue,
        chunk_id=chunk_id,
    )
    lifecycle_assertions = tuple(
        _assess_lifecycle_mention(text, analysis, mention)
        for mention in analysis.mentions
        if mention.mention_type == "lifecycle"
        and mention.lifecycle_role in _SUPPORTED_LIFECYCLE_ROLES
    )
    expressions = {item.expression_id: item for item in analysis.expressions}
    mentions = {item.mention_id: item for item in analysis.mentions}
    lead_assertions = tuple(
        _assess_temporal_lead(
            text,
            analysis,
            lead,
            expression=expressions[lead.expression_id],
            mention=mentions[lead.mention_id],
        )
        for lead in analysis.leads
    )
    material = {
        "version": ASSESSMENT_VERSION,
        "source_sha256": analysis.source_sha256,
        "analysis_fingerprint": analysis.snapshot_fingerprint,
        "lifecycle_assertions": [asdict(item) for item in lifecycle_assertions],
        "lead_assertions": [asdict(item) for item in lead_assertions],
        "production_integration_enabled": False,
        "creates_facts": False,
        "creates_identities": False,
        "creates_times": False,
    }
    return GmailTemporalSourceAssertionAssessment(
        version=ASSESSMENT_VERSION,
        assessment_fingerprint="gtsa_" + _digest(material),
        source_sha256=analysis.source_sha256,
        analysis_fingerprint=analysis.snapshot_fingerprint,
        analysis=analysis,
        lifecycle_assertions=lifecycle_assertions,
        lead_assertions=lead_assertions,
    )


def _assess_lifecycle_mention(
    text: str,
    analysis: TemporalLeadAnalysis,
    mention: TemporalMention,
) -> GmailTemporalLifecycleAssertion:
    blockers = _source_assertion_blockers(
        text,
        start=mention.start,
        end=mention.end,
        inherited_blockers=mention.blockers,
        future_lifecycle=True,
    )
    material = {
        "version": "gmail_temporal_lifecycle_assertion_v1",
        "analysis_fingerprint": analysis.snapshot_fingerprint,
        "mention_id": mention.mention_id,
        "lifecycle_role": mention.lifecycle_role,
        "evidence_start": mention.start,
        "evidence_end": mention.end,
        "disposition": "blocked" if blockers else "asserted",
        "blockers": blockers,
    }
    return GmailTemporalLifecycleAssertion(
        version="gmail_temporal_lifecycle_assertion_v1",
        assertion_id="gtslca_" + _digest(material),
        mention_id=mention.mention_id,
        lifecycle_role=mention.lifecycle_role or "",
        evidence_start=mention.start,
        evidence_end=mention.end,
        disposition="blocked" if blockers else "asserted",
        blockers=blockers,
    )


def _assess_temporal_lead(
    text: str,
    analysis: TemporalLeadAnalysis,
    lead: TemporalLead,
    *,
    expression: TemporalExpression,
    mention: TemporalMention,
) -> GmailTemporalLeadAssertion:
    start = min(expression.start, mention.start)
    end = max(expression.end, mention.end)
    blockers = _source_assertion_blockers(
        text,
        start=start,
        end=end,
        inherited_blockers=tuple(
            {
                *expression.blockers,
                *mention.blockers,
                *lead.blockers,
            }
        ),
    )
    material = {
        "version": "gmail_temporal_lead_assertion_v1",
        "analysis_fingerprint": analysis.snapshot_fingerprint,
        "lead_id": lead.lead_id,
        "expression_id": lead.expression_id,
        "mention_id": lead.mention_id,
        "relation": lead.relation,
        "kind": lead.kind,
        "evidence_start": start,
        "evidence_end": end,
        "disposition": "blocked" if blockers else "asserted",
        "blockers": blockers,
    }
    return GmailTemporalLeadAssertion(
        version="gmail_temporal_lead_assertion_v1",
        assertion_id="gtslea_" + _digest(material),
        lead_id=lead.lead_id,
        expression_id=lead.expression_id,
        mention_id=lead.mention_id,
        relation=lead.relation,
        kind=lead.kind,
        evidence_start=start,
        evidence_end=end,
        disposition="blocked" if blockers else "asserted",
        blockers=blockers,
    )


def _source_assertion_blockers(
    text: str,
    *,
    start: int,
    end: int,
    inherited_blockers: tuple[str, ...],
    future_lifecycle: bool = False,
) -> tuple[str, ...]:
    clause_start = max(
        text.rfind("\n", 0, start),
        text.rfind(".", 0, start),
        text.rfind("!", 0, start),
        text.rfind("?", 0, start),
        text.rfind(";", 0, start),
    )
    prefix = text[clause_start + 1 : start]
    coordinating_boundaries = tuple(_COORDINATING_BOUNDARY.finditer(prefix))
    local_prefix = (
        prefix[coordinating_boundaries[-1].end() :]
        if coordinating_boundaries
        else prefix
    )
    conditional_prefix = local_prefix.rsplit(",", 1)[-1]
    evidence = text[start:end]
    hard_boundaries = tuple(
        position
        for token in (".", "!", "?", ";", "\n")
        if (position := text.find(token, end)) >= 0
    )
    clause_end = min(hard_boundaries) + 1 if hard_boundaries else len(text)
    clause_suffix = text[end:clause_end]
    suffix_boundary = _COORDINATING_BOUNDARY.search(clause_suffix)
    local_suffix = (
        clause_suffix[: suffix_boundary.start()]
        if suffix_boundary is not None
        else clause_suffix
    )
    left_evidence = local_prefix + evidence
    evidence_and_suffix = evidence + local_suffix

    found: set[str] = set()
    if "quoted_or_forwarded_context" in inherited_blockers or _inside_inline_quote(
        text, start, end
    ):
        found.add("quoted_or_forwarded_context")
    if _DENIAL_BEFORE_CUE.search(local_prefix) or _DENIAL_BEFORE_CUE.search(evidence):
        found.add("denied_assertion")
    if _NEGATION_BEFORE_CUE.search(local_prefix) or _NEGATION_WITHIN_EVIDENCE.search(
        evidence
    ):
        found.add("negated_assertion")
    if (
        _CONDITIONAL_BEFORE_CUE.search(conditional_prefix)
        or _CONDITIONAL_BEFORE_CUE.search(evidence_and_suffix)
        or _CONDITIONAL_AFTER_CUE.search(local_suffix)
    ):
        found.add("conditional_assertion")
    if ("?" in clause_suffix and _INTERROGATIVE_PREFIX.search(local_prefix)) or (
        re.match(r"\s*\?", clause_suffix)
        or _UNPUNCTUATED_QUESTION_PREFIX.search(local_prefix)
        or _CONFIRMATION_REQUEST.search(local_prefix)
        or _CONFIRMATION_REQUEST.search(evidence)
    ):
        found.add("interrogative_assertion")
    if (
        _REPORTED_BEFORE_CUE.search(local_prefix)
        or _NAMED_REPORTED_BEFORE_CUE.search(local_prefix)
        or _REPORTED_BEFORE_CUE.search(evidence_and_suffix)
        or _NAMED_REPORTED_BEFORE_CUE.search(left_evidence)
        or _NAMED_REPORTED_BEFORE_CUE.search(evidence_and_suffix)
        or _REPORTED_AFTER_CUE.search(clause_suffix)
        or _NAMED_REPORTED_AFTER_CUE.search(clause_suffix)
    ):
        found.add("reported_assertion")
    if (
        _FALSE_THAT_BEFORE_CUE.search(local_prefix)
        or _FALSE_THAT_BEFORE_CUE.search(evidence)
        or _REFUTATION_AFTER_CUE.search(clause_suffix)
    ):
        found.add("refuted_assertion")
    if (
        _MODAL_BEFORE_CUE.search(local_prefix)
        or _MODAL_WITHIN_EVIDENCE.search(evidence_and_suffix)
        or _PROSPECTIVE_BEFORE_CUE.search(local_prefix)
        or _PROSPECTIVE_WITHIN_EVIDENCE.search(evidence_and_suffix)
        or (future_lifecycle and _FUTURE_LIFECYCLE_BEFORE_CUE.search(local_prefix))
    ):
        found.add("modal_assertion")
    if (
        _EPISTEMIC_BEFORE_CUE.search(local_prefix)
        or _EPISTEMIC_BEFORE_CUE.search(evidence_and_suffix)
        or _EPISTEMIC_AFTER_CUE.search(clause_suffix)
    ):
        found.add("epistemic_assertion")

    precedence = (
        "quoted_or_forwarded_context",
        "denied_assertion",
        "negated_assertion",
        "conditional_assertion",
        "interrogative_assertion",
        "reported_assertion",
        "refuted_assertion",
        "modal_assertion",
        "epistemic_assertion",
    )
    return tuple(item for item in precedence if item in found)


def _inside_inline_quote(text: str, start: int, end: int) -> bool:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    line = text[line_start:line_end]
    relative_start = start - line_start
    relative_end = end - line_start
    patterns = (
        re.compile(r'"[^"\r\n]{1,1000}"'),
        re.compile(r"“[^”\r\n]{1,1000}”"),
        re.compile(r"‘[^’\r\n]{1,1000}’"),
        re.compile(r"(?<!\w)'[^'\r\n]{1,1000}'(?!\w)"),
    )
    return any(
        match.start() <= relative_start and relative_end <= match.end()
        for pattern in patterns
        for match in pattern.finditer(line)
    )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
