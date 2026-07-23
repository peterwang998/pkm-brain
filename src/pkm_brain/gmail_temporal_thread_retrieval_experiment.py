from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from .gmail_temporal_assertions import assess_gmail_temporal_source_assertions


EXPERIMENT_VERSION = "gmail_temporal_thread_retrieval_experiment_v2"
EXPERIMENT_MAX_DIRECT_RESULTS = 10
EXPERIMENT_LOCKED_ANCHOR_RESULTS = 5
EXPERIMENT_MAX_VERIFIED_CONTEXT_RESULTS = 3
EXPERIMENT_MAX_REVIEW_CONTEXT_RESULTS = 1
EXPERIMENT_MAX_TOTAL_CONTEXT_RESULTS = (
    EXPERIMENT_MAX_VERIFIED_CONTEXT_RESULTS + EXPERIMENT_MAX_REVIEW_CONTEXT_RESULTS
)
EXPERIMENT_MAX_EVIDENCE_SOURCES = 20_000
EXPERIMENT_MAX_SOURCE_TEXT_CHARS = 100_000

TemporalThreadIntent = Literal["lifecycle", "timeline"]
TemporalThreadIntentOverride = Literal["lifecycle", "timeline", "none"]
TemporalThreadIntentBasis = Literal["explicit", "query_classifier"]
ContextIdentityBasis = Literal["verified", "contextual_review"]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_LIFECYCLE_INTENT = re.compile(
    r"(?:"
    r"\b(?:current|latest|final)\s+(?:status|schedule|deadline)\b|"
    r"\bwhat(?:['’]s|\s+is)\s+the\s+latest\s+(?:on|for)\b|"
    r"\bwhat\s+was\b[^?\n]{0,100}\bstatus\b|"
    r"\b(?:still|was|were|is|did|has|have)\b[^?\n]{0,100}"
    r"\b(?:called\s+off|cancelled|canceled|completed|finished|postponed|"
    r"rescheduled)\b|"
    r"\b(?:meeting|launch|event|appointment|interview|review|workshop)\b"
    r"[^?\n]{0,80}\bstill\s+(?:on|happening|scheduled|planned)\b|"
    r"\bdid\b[^?\n]{0,80}\bend\s+up\s+(?:going\s+ahead|happening)\b"
    r")",
    re.IGNORECASE,
)
_TIMELINE_INTENT = re.compile(
    r"(?:"
    r"\b(?:timeline|chronology|sequence\s+of\s+(?:events|changes))\b|"
    r"\bwhat\s+happened\s+(?:with|to)\s+[^?\n]{1,100}|"
    r"\b(?:how\s+did|walk\s+me\s+through)\b"
    r"[^?\n]{0,120}\b(?:date|deadline|launch|meeting|schedule|event|"
    r"appointment|move|moved)\b|"
    r"\b(?:deadline|schedule|date)\s+(?:change|changes|history)\b|"
    r"\bhistory\s+of\s+(?:the\s+)?(?:deadline|schedule|event|appointment)\b|"
    r"\b(?:where|when)\s+did\b[^?\n]{0,80}\b(?:land|settle)\b|"
    r"\bwhat\s+date\s+did\b[^?\n]{0,80}\b(?:land|settle)\b|"
    r"\bby\s+when\b|"
    r"\bwhen\s+does\b[^?\n]{0,80}\b(?:expire|run\s+out)\b"
    r")",
    re.IGNORECASE,
)
_QUERY_STOPWORDS = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "appointment",
        "as",
        "at",
        "be",
        "by",
        "called",
        "cancelled",
        "canceled",
        "change",
        "changes",
        "chronology",
        "completed",
        "current",
        "date",
        "deadline",
        "did",
        "do",
        "end",
        "event",
        "final",
        "for",
        "from",
        "happen",
        "happened",
        "happening",
        "has",
        "have",
        "history",
        "how",
        "i",
        "in",
        "interview",
        "is",
        "it",
        "land",
        "latest",
        "launch",
        "me",
        "meeting",
        "moved",
        "my",
        "of",
        "off",
        "on",
        "out",
        "postponed",
        "project",
        "rescheduled",
        "review",
        "run",
        "schedule",
        "sequence",
        "settle",
        "status",
        "still",
        "the",
        "through",
        "timeline",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "with",
        "workshop",
    }
)


class GmailTemporalThreadRetrievalExperimentError(ValueError):
    """Raised when disabled thread-context experiment inputs are invalid."""


@dataclass(frozen=True)
class GmailTemporalVerifiedEventBinding:
    """One externally verified event key and its authorized query aliases."""

    event_identity_key: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class GmailTemporalThreadEvidence:
    """Trusted Gmail metadata used by the disabled context experiment.

    Account scope and provider thread ID are distinct trusted fields, so equal
    provider thread IDs from two Gmail accounts cannot collide. Identity keys
    are external authority. This module never derives an event identity from
    message prose. Contextual keys are review-only identities assigned
    upstream; they are never used to choose the target event.
    """

    evidence_id: str
    gmail_account_scope_id: str
    gmail_provider_thread_id: str
    available_at: str
    message_ordinal: int
    text: str
    verified_event_bindings: tuple[GmailTemporalVerifiedEventBinding, ...] = ()
    contextual_event_identity_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class GmailTemporalThreadRetrievalPlan:
    """Review-only context attachments beside an unchanged direct ranking."""

    version: Literal["gmail_temporal_thread_retrieval_plan_v2"]
    experiment_version: Literal["gmail_temporal_thread_retrieval_experiment_v2"]
    intent: TemporalThreadIntent | None
    intent_basis: TemporalThreadIntentBasis
    target_event_identity_key: str | None
    direct_ranked_evidence_ids: tuple[str, ...]
    locked_anchor_evidence_ids: tuple[str, ...]
    context_evidence_ids: tuple[str, ...]
    verified_context_evidence_ids: tuple[str, ...]
    review_context_evidence_ids: tuple[str, ...]
    answer_evidence_ids: tuple[str, ...]
    limitations: tuple[str, ...]
    production_integration_enabled: Literal[False] = False
    persisted: Literal[False] = False


@dataclass(frozen=True)
class _BoundEvidence:
    source: GmailTemporalThreadEvidence
    available_at: datetime
    thread_scope: tuple[str, str]


@dataclass(frozen=True)
class _Anchor:
    evidence_id: str
    thread_scope: tuple[str, str]
    event_identity_key: str
    available_at: datetime
    message_ordinal: int
    direct_rank: int


@dataclass(frozen=True)
class _ContextCandidate:
    evidence_id: str
    thread_scope: tuple[str, str]
    event_identity_key: str
    available_at: datetime
    message_ordinal: int
    identity_basis: ContextIdentityBasis
    signal_kinds: tuple[str, ...]
    locked_anchor_distance: int


@dataclass(frozen=True)
class _TemporalWatermark:
    thread_scope: tuple[str, str]
    available_at: datetime
    message_ordinal: int


def plan_gmail_temporal_thread_retrieval_experiment(
    *,
    query: str,
    source_available_as_of: str,
    baseline_ranked_evidence_ids: tuple[str, ...],
    evidence_sources: tuple[GmailTemporalThreadEvidence, ...],
    excluded_evidence_ids: tuple[str, ...] = (),
    temporal_intent: TemporalThreadIntentOverride | None = None,
    direct_limit: int = EXPERIMENT_MAX_DIRECT_RESULTS,
    verified_context_limit: int = EXPERIMENT_MAX_VERIFIED_CONTEXT_RESULTS,
) -> GmailTemporalThreadRetrievalPlan:
    """Attach bounded same-event context without replacing direct evidence.

    The canonical direct ranking is returned byte/order-for-order. Expansion
    activates only for an explicit temporal intent and one unique verified
    event key derived from alias-matching ranks one through five. Candidates
    must be in an anchored thread, carry exactly that verified or upstream
    contextual key, predate the source cutoff, survive exclusions, and contain
    a canonical asserted temporal signal. No facts, identities, or times are
    created here. ``verified_context_limit`` caps verified change points; the
    separately labeled review channel may add at most one more source.
    """

    _validate_query(query)
    if (
        not isinstance(direct_limit, int)
        or isinstance(direct_limit, bool)
        or not 1 <= direct_limit <= EXPERIMENT_MAX_DIRECT_RESULTS
    ):
        raise GmailTemporalThreadRetrievalExperimentError("direct_limit is invalid")
    if (
        not isinstance(verified_context_limit, int)
        or isinstance(verified_context_limit, bool)
        or not 0 <= verified_context_limit <= EXPERIMENT_MAX_VERIFIED_CONTEXT_RESULTS
    ):
        raise GmailTemporalThreadRetrievalExperimentError(
            "verified_context_limit is invalid"
        )
    cutoff = _timestamp(source_available_as_of, label="source_available_as_of")
    authority = _authority(evidence_sources)
    excluded = _excluded(excluded_evidence_ids, authority=authority)
    direct = _baseline(
        baseline_ranked_evidence_ids,
        authority=authority,
        cutoff=cutoff,
        excluded=excluded,
    )[:direct_limit]
    intent, intent_basis = _resolve_intent(query, temporal_intent)
    locked_ids = direct[: min(EXPERIMENT_LOCKED_ANCHOR_RESULTS, len(direct))]
    if intent is None or verified_context_limit == 0:
        return _plan(
            intent=intent,
            intent_basis=intent_basis,
            target_key=None,
            direct=direct,
            locked=locked_ids,
            verified_context=(),
            review_context=(),
        )

    anchors, target_key = _locked_anchor_authority(
        query=query,
        locked_ids=locked_ids,
        authority=authority,
    )
    if target_key is None:
        return _plan(
            intent=intent,
            intent_basis=intent_basis,
            target_key=None,
            direct=direct,
            locked=locked_ids,
            verified_context=(),
            review_context=(),
        )

    candidates = _context_candidates(
        direct=direct,
        authority=authority,
        cutoff=cutoff,
        excluded=excluded,
        anchors=anchors,
        target_key=target_key,
    )
    verified = tuple(item for item in candidates if item.identity_basis == "verified")
    review = tuple(
        item for item in candidates if item.identity_basis == "contextual_review"
    )
    direct_watermark = _latest_verified_direct_watermark(
        direct=direct,
        authority=authority,
        anchors=anchors,
        target_key=target_key,
    )
    if intent == "lifecycle" and direct_watermark is not None:
        verified = tuple(
            item for item in verified if _is_newer_than(item, direct_watermark)
        )
    selected_verified = _select_for_intent(
        verified,
        intent=intent,
        limit=verified_context_limit,
    )
    if intent == "lifecycle":
        review_floor = _latest_watermark(
            direct_watermark,
            tuple(_watermark(item) for item in selected_verified),
        )
        if review_floor is not None:
            review = tuple(
                item for item in review if _is_newer_than(item, review_floor)
            )
    selected_review = _select_for_intent(
        review,
        intent=intent,
        limit=EXPERIMENT_MAX_REVIEW_CONTEXT_RESULTS,
    )
    return _plan(
        intent=intent,
        intent_basis=intent_basis,
        target_key=target_key,
        direct=direct,
        locked=locked_ids,
        verified_context=tuple(item.evidence_id for item in selected_verified),
        review_context=tuple(item.evidence_id for item in selected_review),
    )


def temporal_thread_query_intent(query: str) -> TemporalThreadIntent | None:
    if not isinstance(query, str) or not query.strip() or "\x00" in query:
        return None
    if _LIFECYCLE_INTENT.search(query):
        return "lifecycle"
    if _TIMELINE_INTENT.search(query):
        return "timeline"
    return None


def _plan(
    *,
    intent: TemporalThreadIntent | None,
    intent_basis: TemporalThreadIntentBasis,
    target_key: str | None,
    direct: tuple[str, ...],
    locked: tuple[str, ...],
    verified_context: tuple[str, ...],
    review_context: tuple[str, ...],
) -> GmailTemporalThreadRetrievalPlan:
    context = verified_context + review_context
    limitations = (
        "public_synthetic_development_only_not_population_evidence",
        "context_sidecar_requires_verified_upstream_event_identity",
        "target_resolution_uses_trusted_alias_bindings_not_message_prose",
        "timeline_context_is_capped_change_point_coverage_not_exhaustive_history",
        "review_context_has_a_separate_one_item_budget_and_is_never_confirmed_evidence",
        "review_identity_context_never_selects_the_target_event",
    )
    return GmailTemporalThreadRetrievalPlan(
        version="gmail_temporal_thread_retrieval_plan_v2",
        experiment_version=EXPERIMENT_VERSION,
        intent=intent,
        intent_basis=intent_basis,
        target_event_identity_key=target_key,
        direct_ranked_evidence_ids=direct,
        locked_anchor_evidence_ids=locked,
        context_evidence_ids=context,
        verified_context_evidence_ids=verified_context,
        review_context_evidence_ids=review_context,
        answer_evidence_ids=direct + context,
        limitations=limitations,
    )


def _validate_query(query: object) -> None:
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > 4_000
        or "\x00" in query
    ):
        raise GmailTemporalThreadRetrievalExperimentError("query is invalid")


def _resolve_intent(
    query: str,
    override: TemporalThreadIntentOverride | None,
) -> tuple[TemporalThreadIntent | None, TemporalThreadIntentBasis]:
    if override is not None and override not in {"lifecycle", "timeline", "none"}:
        raise GmailTemporalThreadRetrievalExperimentError("temporal_intent is invalid")
    if override is not None:
        return (None if override == "none" else override), "explicit"
    return temporal_thread_query_intent(query), "query_classifier"


def _authority(
    sources: tuple[GmailTemporalThreadEvidence, ...],
) -> dict[str, _BoundEvidence]:
    if (
        not isinstance(sources, tuple)
        or len(sources) > EXPERIMENT_MAX_EVIDENCE_SOURCES
        or any(
            not isinstance(source, GmailTemporalThreadEvidence) for source in sources
        )
    ):
        raise GmailTemporalThreadRetrievalExperimentError(
            "evidence_sources are invalid"
        )
    output: dict[str, _BoundEvidence] = {}
    thread_ordinals: set[tuple[tuple[str, str], int]] = set()
    for source in sources:
        evidence_id = _identifier(source.evidence_id, label="evidence_id")
        thread_scope = (
            _identifier(
                source.gmail_account_scope_id,
                label="gmail_account_scope_id",
            ),
            _identifier(
                source.gmail_provider_thread_id,
                label="gmail_provider_thread_id",
            ),
        )
        if evidence_id in output:
            raise GmailTemporalThreadRetrievalExperimentError(
                "evidence_sources contain duplicate evidence IDs"
            )
        if (
            not isinstance(source.message_ordinal, int)
            or isinstance(source.message_ordinal, bool)
            or source.message_ordinal < 1
            or (thread_scope, source.message_ordinal) in thread_ordinals
        ):
            raise GmailTemporalThreadRetrievalExperimentError(
                "evidence message ordinals are invalid"
            )
        if (
            not isinstance(source.text, str)
            or len(source.text) > EXPERIMENT_MAX_SOURCE_TEXT_CHARS
            or "\x00" in source.text
        ):
            raise GmailTemporalThreadRetrievalExperimentError(
                "evidence text is invalid"
            )
        verified = _verified_event_bindings(source.verified_event_bindings)
        contextual = _identity_keys(
            source.contextual_event_identity_keys,
            label="contextual event identity keys",
        )
        if {item.event_identity_key for item in verified}.intersection(contextual):
            raise GmailTemporalThreadRetrievalExperimentError(
                "event identity key authority is ambiguous"
            )
        thread_ordinals.add((thread_scope, source.message_ordinal))
        output[evidence_id] = _BoundEvidence(
            source=source,
            available_at=_timestamp(source.available_at, label="evidence available_at"),
            thread_scope=thread_scope,
        )
    return output


def _verified_event_bindings(
    values: object,
) -> tuple[GmailTemporalVerifiedEventBinding, ...]:
    if not isinstance(values, tuple) or any(
        not isinstance(item, GmailTemporalVerifiedEventBinding) for item in values
    ):
        raise GmailTemporalThreadRetrievalExperimentError(
            "verified event bindings are invalid"
        )
    output: list[GmailTemporalVerifiedEventBinding] = []
    seen_keys: set[str] = set()
    for item in values:
        key = _identifier(item.event_identity_key, label="verified event identity key")
        if key in seen_keys or not isinstance(item.aliases, tuple) or not item.aliases:
            raise GmailTemporalThreadRetrievalExperimentError(
                "verified event bindings are invalid"
            )
        aliases: list[str] = []
        for alias in item.aliases:
            if (
                not isinstance(alias, str)
                or not alias.strip()
                or alias != alias.strip()
                or len(alias) > 256
                or any(character in alias for character in "\x00\r\n")
                or not _topic_tokens(alias)
            ):
                raise GmailTemporalThreadRetrievalExperimentError(
                    "verified event bindings are invalid"
                )
            aliases.append(alias)
        normalized_aliases = tuple(
            " ".join(sorted(_topic_tokens(alias))) for alias in aliases
        )
        if len(normalized_aliases) != len(set(normalized_aliases)):
            raise GmailTemporalThreadRetrievalExperimentError(
                "verified event bindings are invalid"
            )
        seen_keys.add(key)
        output.append(
            GmailTemporalVerifiedEventBinding(
                event_identity_key=key,
                aliases=tuple(aliases),
            )
        )
    return tuple(output)


def _identity_keys(values: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise GmailTemporalThreadRetrievalExperimentError(f"{label} are invalid")
    output = tuple(_identifier(value, label=label) for value in values)
    if len(output) != len(set(output)):
        raise GmailTemporalThreadRetrievalExperimentError(f"{label} are invalid")
    return output


def _excluded(
    values: tuple[str, ...],
    *,
    authority: dict[str, _BoundEvidence],
) -> set[str]:
    if not isinstance(values, tuple):
        raise GmailTemporalThreadRetrievalExperimentError(
            "excluded_evidence_ids are invalid"
        )
    output = tuple(_identifier(value, label="excluded evidence_id") for value in values)
    if len(output) != len(set(output)) or set(output) - set(authority):
        raise GmailTemporalThreadRetrievalExperimentError(
            "excluded_evidence_ids are invalid"
        )
    return set(output)


def _baseline(
    values: tuple[str, ...],
    *,
    authority: dict[str, _BoundEvidence],
    cutoff: datetime,
    excluded: set[str],
) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise GmailTemporalThreadRetrievalExperimentError(
            "baseline_ranked_evidence_ids are invalid"
        )
    output = tuple(_identifier(value, label="baseline evidence_id") for value in values)
    if (
        len(output) > EXPERIMENT_MAX_DIRECT_RESULTS
        or len(output) != len(set(output))
        or set(output) - set(authority)
        or set(output).intersection(excluded)
        or any(authority[value].available_at > cutoff for value in output)
    ):
        raise GmailTemporalThreadRetrievalExperimentError(
            "baseline_ranked_evidence_ids are invalid"
        )
    return output


def _identifier(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise GmailTemporalThreadRetrievalExperimentError(f"{label} is invalid")
    return value


def _timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise GmailTemporalThreadRetrievalExperimentError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GmailTemporalThreadRetrievalExperimentError(
            f"{label} is invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise GmailTemporalThreadRetrievalExperimentError(
            f"{label} must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _normalized_token(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _topic_tokens(text: str) -> frozenset[str]:
    output: set[str] = set()
    for match in _TOKEN.finditer(unicodedata.normalize("NFKC", text)):
        raw = match.group(0)
        token = _normalized_token(raw)
        if token in _QUERY_STOPWORDS or token.isdigit():
            continue
        if (
            len(token) >= 3
            or any(character.isdigit() for character in token)
            or (len(token) >= 2 and raw.isupper())
        ):
            output.add(token)
    return frozenset(output)


def _alias_matches(
    query_topics: frozenset[str],
    aliases: tuple[str, ...],
) -> bool:
    return any(
        bool(alias_topics) and alias_topics.issubset(query_topics)
        for alias in aliases
        if (alias_topics := _topic_tokens(alias))
    )


def _locked_anchor_authority(
    *,
    query: str,
    locked_ids: tuple[str, ...],
    authority: dict[str, _BoundEvidence],
) -> tuple[tuple[_Anchor, ...], str | None]:
    query_topics = _topic_tokens(query)
    matching: list[tuple[int, str, _BoundEvidence]] = []
    keys: set[str] = set()
    for rank, evidence_id in enumerate(locked_ids, start=1):
        bound = authority[evidence_id]
        source = bound.source
        matched_bindings = tuple(
            binding
            for binding in source.verified_event_bindings
            if _alias_matches(query_topics, binding.aliases)
        )
        if not matched_bindings:
            continue
        matching.append((rank, evidence_id, bound))
        keys.update(item.event_identity_key for item in matched_bindings)
    if len(keys) != 1:
        return (), None
    target_key = next(iter(keys))
    anchors = tuple(
        _Anchor(
            evidence_id=evidence_id,
            thread_scope=bound.thread_scope,
            event_identity_key=target_key,
            available_at=bound.available_at,
            message_ordinal=bound.source.message_ordinal,
            direct_rank=rank,
        )
        for rank, evidence_id, bound in matching
        if any(
            binding.event_identity_key == target_key
            and _alias_matches(query_topics, binding.aliases)
            for binding in bound.source.verified_event_bindings
        )
    )
    if not anchors:
        return (), None
    return anchors, target_key


def _context_candidates(
    *,
    direct: tuple[str, ...],
    authority: dict[str, _BoundEvidence],
    cutoff: datetime,
    excluded: set[str],
    anchors: tuple[_Anchor, ...],
    target_key: str,
) -> tuple[_ContextCandidate, ...]:
    direct_set = set(direct)
    anchored_threads = {anchor.thread_scope for anchor in anchors}
    output: list[_ContextCandidate] = []
    for evidence_id, bound in authority.items():
        source = bound.source
        if (
            evidence_id in direct_set
            or evidence_id in excluded
            or bound.available_at > cutoff
            or bound.thread_scope not in anchored_threads
        ):
            continue
        identity_basis: ContextIdentityBasis | None = None
        if (
            len(source.verified_event_bindings) == 1
            and source.verified_event_bindings[0].event_identity_key == target_key
        ):
            identity_basis = "verified"
        elif source.contextual_event_identity_keys == (target_key,):
            identity_basis = "contextual_review"
        if identity_basis is None:
            continue
        signal_kinds = _asserted_temporal_signal_kinds(source)
        if not signal_kinds:
            continue
        thread_anchors = tuple(
            anchor for anchor in anchors if anchor.thread_scope == bound.thread_scope
        )
        output.append(
            _ContextCandidate(
                evidence_id=evidence_id,
                thread_scope=bound.thread_scope,
                event_identity_key=target_key,
                available_at=bound.available_at,
                message_ordinal=source.message_ordinal,
                identity_basis=identity_basis,
                signal_kinds=signal_kinds,
                locked_anchor_distance=min(
                    abs(source.message_ordinal - anchor.message_ordinal)
                    for anchor in thread_anchors
                ),
            )
        )
    return tuple(output)


def _latest_verified_direct_watermark(
    *,
    direct: tuple[str, ...],
    authority: dict[str, _BoundEvidence],
    anchors: tuple[_Anchor, ...],
    target_key: str,
) -> _TemporalWatermark | None:
    anchored_threads = {anchor.thread_scope for anchor in anchors}
    points: list[_TemporalWatermark] = []
    for evidence_id in direct:
        bound = authority[evidence_id]
        source = bound.source
        if (
            bound.thread_scope not in anchored_threads
            or len(source.verified_event_bindings) != 1
            or source.verified_event_bindings[0].event_identity_key != target_key
            or not _asserted_temporal_signal_kinds(source)
        ):
            continue
        points.append(
            _TemporalWatermark(
                thread_scope=bound.thread_scope,
                available_at=bound.available_at,
                message_ordinal=source.message_ordinal,
            )
        )
    return _latest_watermark(None, tuple(points))


def _watermark(candidate: _ContextCandidate) -> _TemporalWatermark:
    return _TemporalWatermark(
        thread_scope=candidate.thread_scope,
        available_at=candidate.available_at,
        message_ordinal=candidate.message_ordinal,
    )


def _latest_watermark(
    initial: _TemporalWatermark | None,
    values: tuple[_TemporalWatermark, ...],
) -> _TemporalWatermark | None:
    available = values + ((initial,) if initial is not None else ())
    if not available:
        return None
    return max(
        available,
        key=lambda item: (
            item.available_at,
            item.message_ordinal,
            item.thread_scope,
        ),
    )


def _is_newer_than(
    candidate: _ContextCandidate,
    watermark: _TemporalWatermark,
) -> bool:
    if candidate.available_at != watermark.available_at:
        return candidate.available_at > watermark.available_at
    return (
        candidate.thread_scope == watermark.thread_scope
        and candidate.message_ordinal > watermark.message_ordinal
    )


def _asserted_temporal_signal_kinds(
    source: GmailTemporalThreadEvidence,
) -> tuple[str, ...]:
    assessment = assess_gmail_temporal_source_assertions(
        text=source.text,
        message_internal_at=source.available_at,
        fact_admitted=False,
        temporal_review_rescue=True,
        chunk_id=f"thread-context:{source.evidence_id}",
    )
    kinds: set[str] = {
        item.lifecycle_role
        for item in assessment.lifecycle_assertions
        if item.disposition == "asserted"
    }
    lifecycle_mentions = {item.mention_id for item in assessment.lifecycle_assertions}
    for lead in assessment.lead_assertions:
        if (
            lead.mention_id not in lifecycle_mentions
            and lead.disposition == "asserted"
            and lead.relation in {"occurrence", "deadline"}
        ):
            kinds.add(str(lead.relation))
    return tuple(sorted(kinds))


def _select_for_intent(
    candidates: tuple[_ContextCandidate, ...],
    *,
    intent: TemporalThreadIntent,
    limit: int,
) -> tuple[_ContextCandidate, ...]:
    if limit <= 0 or not candidates:
        return ()
    if intent == "lifecycle":
        ranked = sorted(
            candidates,
            key=lambda item: (
                -item.available_at.timestamp(),
                -item.message_ordinal,
                item.locked_anchor_distance,
                item.evidence_id,
            ),
        )
        return tuple(ranked[: min(limit, 1)])

    chronological = sorted(
        candidates,
        key=lambda item: (
            item.available_at,
            item.message_ordinal,
            item.locked_anchor_distance,
            item.evidence_id,
        ),
    )
    if len(chronological) <= limit:
        return tuple(chronological)
    if limit == 1:
        return (chronological[-1],)
    selected_indices = {len(chronological) - 1}
    terminal_indices = tuple(
        index
        for index, item in enumerate(chronological)
        if {"cancelled", "completed"}.intersection(item.signal_kinds)
    )
    if terminal_indices:
        selected_indices.add(terminal_indices[-1])
    if len(selected_indices) < limit:
        selected_indices.add(0)
    while len(selected_indices) < limit:
        selected_signal_kinds = {
            kind
            for index in selected_indices
            for kind in chronological[index].signal_kinds
        }
        remaining_indices = tuple(
            index
            for index in range(len(chronological))
            if index not in selected_indices
        )
        selected_indices.add(
            max(
                remaining_indices,
                key=lambda index: (
                    len(set(chronological[index].signal_kinds) - selected_signal_kinds),
                    min(abs(index - chosen) for chosen in selected_indices),
                    index,
                ),
            )
        )
    return tuple(chronological[index] for index in sorted(selected_indices))
