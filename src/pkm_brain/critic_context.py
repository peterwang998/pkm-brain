from __future__ import annotations

from typing import Any

from .source_evidence import evidence_units_for_text


def critic_named_entity_context(
    chunks: list[Any], fact: dict[str, Any]
) -> list[dict[str, Any]]:
    """Expose bounded cross-chunk named-entity anchors as attribution only."""

    metadata = fact.get("metadata") if isinstance(fact.get("metadata"), dict) else {}
    mentions = metadata.get("model_entity_mentions")
    if not isinstance(mentions, list):
        return []
    chunks_by_id = {str(chunk["id"]): chunk for chunk in chunks}
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for mention in mentions:
        if not isinstance(mention, dict):
            continue
        if str(mention.get("mention_kind") or "").strip().lower() != "named":
            continue
        surface = str(mention.get("surface") or "").strip()
        span = mention.get("mention_span")
        if not surface or not isinstance(span, dict):
            continue
        chunk_id = str(span.get("chunk_id") or "").strip()
        chunk = chunks_by_id.get(chunk_id)
        if chunk is None:
            continue
        try:
            mention_start = int(span.get("start"))
            mention_end = int(span.get("end"))
        except (TypeError, ValueError):
            continue
        for unit in evidence_units_for_text(str(chunk["text"] or "")):
            overlaps = (
                int(unit["start"]) < mention_end and int(unit["end"]) > mention_start
            )
            if not overlaps and surface.casefold() not in str(unit["text"]).casefold():
                continue
            key = (surface.casefold(), chunk_id, str(unit["unit_id"]))
            if key in seen:
                continue
            seen.add(key)
            output.append(
                {
                    "entity": surface,
                    "chunk_id": chunk_id,
                    "unit_id": str(unit["unit_id"]),
                    "text": str(unit["text"]),
                }
            )
            if len(output) >= 8:
                return output
            break
    return output
