from __future__ import annotations

from typing import Any

from .entities import (
    ENTITY_TYPES,
    MENTION_KINDS,
    normalize_entity_name,
    normalize_entity_type,
    normalize_mention_kind,
    optional_float,
)


def normalize_extracted_entity_mentions(
    item: dict[str, Any],
    valid_spans: list[dict[str, Any]],
    chunk_context_by_id: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Keep valid mentions and report malformed annotations separately."""

    raw_mentions = item.get("entities")
    if raw_mentions is None:
        raw_mentions = item.get("entity_mentions")
    if raw_mentions is None:
        return [], []
    if not isinstance(raw_mentions, list):
        return [], ["entities must be an array"]
    mentions: list[dict[str, Any]] = []
    errors: list[str] = []
    primary_seen = False
    for index, raw in enumerate(raw_mentions):
        if not isinstance(raw, dict):
            errors.append(f"entities[{index}] must be an object")
            continue
        surface = str(
            raw.get("surface")
            or raw.get("mention")
            or raw.get("name")
            or raw.get("entity_key")
            or ""
        ).strip()
        if not surface:
            errors.append(f"entities[{index}].surface is required")
            continue
        raw_type = (
            raw.get("type")
            if raw.get("type") is not None
            else raw.get("entity_type")
        )
        entity_type = normalize_entity_type(str(raw_type or ""))
        if entity_type is None:
            errors.append(
                f"entities[{index}].type must be one of: "
                f"{', '.join(sorted(ENTITY_TYPES))}"
            )
            continue
        raw_kind = (
            raw.get("mention_kind")
            if raw.get("mention_kind") is not None
            else raw.get("kind")
        )
        mention_kind = normalize_mention_kind(raw_kind)
        if raw_kind is not None and mention_kind is None:
            errors.append(
                f"entities[{index}].mention_kind must be one of: "
                f"{', '.join(sorted(MENTION_KINDS))}"
            )
            continue
        is_primary = raw_entity_mention_is_primary(raw) and not primary_seen
        if is_primary:
            primary_seen = True
        mentions.append(
            {
                "surface": surface,
                "entity_type": entity_type,
                "mention_kind": mention_kind,
                "is_primary": is_primary,
                "mention_span": derive_mention_span(
                    surface, raw, valid_spans, chunk_context_by_id
                ),
                "confidence": optional_float(raw.get("confidence")),
            }
        )
    if mentions and not any(mention["is_primary"] for mention in mentions):
        mentions[0]["is_primary"] = True
    return dedupe_entity_mentions(mentions), errors


def raw_entity_mention_is_primary(raw: dict[str, Any]) -> bool:
    if "is_primary" in raw:
        return bool(raw.get("is_primary"))
    role = str(raw.get("role") or "").strip().lower()
    return role in {"primary", "main", "subject"}


def derive_mention_span(
    surface: str,
    raw: dict[str, Any],
    valid_spans: list[dict[str, Any]],
    chunk_context_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    chunk_ids = stable_unique_strings(
        [
            str(raw.get("chunk_id") or "").strip(),
            *[str(span.get("chunk_id") or "").strip() for span in valid_spans],
        ]
    )
    for chunk_id in chunk_ids:
        chunk_context = chunk_context_by_id.get(chunk_id)
        if chunk_context is None:
            continue
        span = find_quote_span(str(chunk_context["text"]), surface)
        if span is None:
            span = find_casefold_span(str(chunk_context["text"]), surface)
        if span is not None:
            start, end = span
            return {"chunk_id": chunk_id, "start": start, "end": end}
    return None


def find_casefold_span(text: str, quote: str) -> tuple[int, int] | None:
    stripped = quote.strip()
    if not stripped:
        return None
    start = text.casefold().find(stripped.casefold())
    if start < 0:
        return None
    return start, start + len(stripped)


def dedupe_entity_mentions(mentions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for mention in mentions:
        key = (
            normalize_entity_name(mention.get("surface")),
            mention.get("entity_type"),
            mention.get("mention_kind"),
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(mention)
    if output and not any(mention["is_primary"] for mention in output):
        output[0]["is_primary"] = True
    return output


def primary_entity_surface(item: dict[str, Any], mentions: list[dict[str, Any]]) -> str:
    for mention in mentions:
        if mention.get("is_primary"):
            return str(mention.get("surface") or "").strip()
    return str(item.get("entity_key") or item.get("entity_mention") or "").strip()


def primary_entity_type(mentions: list[dict[str, Any]]) -> str | None:
    for mention in mentions:
        if mention.get("is_primary"):
            return normalize_entity_type(mention.get("entity_type"))
    return None


def stable_unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def find_quote_span(text: str, quote: str) -> tuple[int, int] | None:
    stripped = quote.strip()
    if not stripped:
        return None
    exact_start = text.find(stripped)
    if exact_start >= 0:
        return exact_start, exact_start + len(stripped)
    normalized_text, index_map = normalize_with_index_map(text)
    normalized_quote, _ = normalize_with_index_map(stripped)
    if not normalized_quote:
        return None
    normalized_start = normalized_text.find(normalized_quote)
    if normalized_start < 0:
        return None
    normalized_end = normalized_start + len(normalized_quote) - 1
    if normalized_start >= len(index_map) or normalized_end >= len(index_map):
        return None
    return index_map[normalized_start], index_map[normalized_end] + 1


def normalize_with_index_map(value: str) -> tuple[str, list[int]]:
    output: list[str] = []
    index_map: list[int] = []
    in_whitespace = False
    for index, char in enumerate(value):
        if char.isspace():
            if output and not in_whitespace:
                output.append(" ")
                index_map.append(index)
            in_whitespace = True
            continue
        output.append(char)
        index_map.append(index)
        in_whitespace = False
    if output and output[-1] == " ":
        output.pop()
        index_map.pop()
    return "".join(output), index_map
