from __future__ import annotations

from typing import Any

from .entities import normalize_entity_type, normalize_mention_kind
from .temporal import event_time_signature


def strip_predicate_validity(candidate: dict[str, Any]) -> dict[str, Any]:
    """Discard invalid optional validity without discarding its base fact."""

    return {
        **candidate,
        "effective_at": None,
        "valid_from": None,
        "valid_to": None,
        "temporal_kind": "unknown",
        "valid_time_precision": "unknown",
        "temporal_expression": None,
        "temporal_confidence": None,
    }


def strip_event_time(candidate: dict[str, Any]) -> dict[str, Any]:
    """Discard malformed event indexing while retaining the assertion."""

    return {
        **candidate,
        "event_time": None,
        "event_time_kind": None,
        "event_start_at": None,
        "event_end_at": None,
        "event_time_precision": None,
        "event_time_expression": None,
    }


def temporal_enrichment_warning(
    index: int,
    statement: str,
    *,
    enrichment: str,
    reasons: list[str],
) -> dict[str, Any]:
    clipped = statement if len(statement) <= 160 else f"{statement[:157]}..."
    return {
        "index": index,
        "statement": clipped,
        "enrichment": enrichment,
        "reasons": reasons,
    }


def primary_named_event_is_grounded(
    mentions: list[dict[str, Any]], cited_spans: list[dict[str, Any]]
) -> bool:
    """Require event timing to attach to one named event cited by this fact."""

    primary = next(
        (mention for mention in mentions if mention.get("is_primary")), None
    )
    if primary is None:
        return False
    if normalize_entity_type(primary.get("entity_type")) != "event":
        return False
    if normalize_mention_kind(primary.get("mention_kind")) != "named":
        return False
    mention_span = primary.get("mention_span")
    if not isinstance(mention_span, dict):
        return False
    try:
        mention_start = int(mention_span["start"])
        mention_end = int(mention_span["end"])
    except (KeyError, TypeError, ValueError):
        return False
    return any(
        str(span.get("chunk_id") or "")
        == str(mention_span.get("chunk_id") or "")
        and int(span.get("start") or 0) <= mention_start
        and mention_end <= int(span.get("end") or 0)
        for span in cited_spans
    )


def candidate_dedupe_key(candidate: dict[str, Any]) -> tuple[Any, ...]:
    spans = tuple(
        (span.get("chunk_id"), span.get("start"), span.get("end"))
        for span in candidate.get("source_spans") or []
    )
    return (
        candidate.get("statement"),
        candidate.get("page_hint"),
        spans,
        event_time_signature(candidate),
    )
