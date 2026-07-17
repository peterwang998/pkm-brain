from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any

from .source_evidence import evidence_units_for_text, resolve_evidence_unit_ids
from .temporal import (
    canonical_event_bound,
    event_time_grounding_errors,
    event_time_signature,
    normalize_event_time_candidate,
    parse_temporal_value,
)
from .wiki_facts import entity_key_for_change, topic_for_path


def structured_source_event_candidate(
    document: dict[str, Any],
) -> dict[str, Any] | None:
    """Project trusted meeting metadata into an evidence-backed event fact."""

    if str(document.get("source_type") or "") != "hyprnote_meeting":
        return None
    if not document.get("structured_event_metadata_trusted"):
        return None
    source_start_at = str(document.get("event_started_at") or "").strip()
    if not source_start_at:
        return None
    source_end_at = str(document.get("event_ended_at") or "").strip() or None
    start_at = canonical_event_bound(source_start_at) or source_start_at
    end_at = canonical_event_bound(source_end_at) if source_end_at else None
    source_updated_at = str(document.get("source_updated_at") or "").strip() or None
    title = str(document.get("title") or "").strip()
    if not title:
        return None
    evidence = structured_source_event_evidence(
        document,
        title,
        source_start_at,
        source_end_at,
        source_updated_at,
    )
    if evidence is None:
        return None

    event_kind, event_kind_basis = structured_event_kind(start_at, source_updated_at)
    entity_surface = structured_event_identity_label(title, start_at)
    page_hint = f"events/{structured_event_occurrence_slug(title, start_at)}.md"
    section_hint = "Summary"
    mention_span = next(
        (
            unit
            for unit in evidence["evidence_units"]
            if title.casefold() in str(unit.get("text") or "").casefold()
        ),
        None,
    )
    entity_mentions = [
        {
            "surface": entity_surface,
            "entity_type": "event",
            "mention_kind": "named",
            "is_primary": True,
            "mention_span": (
                {
                    "chunk_id": mention_span["chunk_id"],
                    "start": mention_span["start"],
                    "end": mention_span["end"],
                }
                if mention_span
                else None
            ),
            "confidence": 1.0,
        }
    ]
    candidate: dict[str, Any] = {
        "statement": (
            f"{entity_surface} occurred."
            if event_kind == "actual"
            else f"{entity_surface} was scheduled."
        ),
        "entity_key": entity_key_for_change(
            topic_for_path(page_hint), page_hint, section_hint
        ),
        "entity_mention": entity_surface,
        "entity_type": "event",
        "entity_mentions": entity_mentions,
        "page_hint": page_hint,
        "section_hint": section_hint,
        "claim_class": "factual_update",
        "source_ids": evidence["source_ids"],
        "source_spans": evidence["source_spans"],
        "evidence_quote": "\n...\n".join(evidence["quotes"])[:1000],
        "evidence_unit_ids": [
            unit["unit_id"] for unit in evidence["evidence_units"]
        ],
        "observed_at": None,
        "effective_at": None,
        "valid_from": None,
        "valid_to": None,
        "temporal_kind": "unknown",
        "valid_time_precision": "unknown",
        "temporal_expression": None,
        "temporal_confidence": None,
        "event_time": {
            "kind": event_kind,
            "start_at": start_at,
            "end_at": end_at,
            "precision": structured_event_precision(start_at, end_at),
            "expression": evidence["event_expression"],
        },
        "confidence": 1.0,
        "extraction_confidence": 1.0,
        "routing_confidence": 1.0,
        "truth_confidence": 1.0,
        "extraction_method": "structured_metadata",
        "extractor_model": "structured-event-v2",
        "metadata": {
            "source": "structured_event_projection",
            "claim_class": "factual_update",
            "evidence_units": evidence["evidence_units"],
            "model_entity_key": entity_surface,
            "model_entity_mentions": entity_mentions,
            "event_session_id": document.get("event_session_id"),
            "source_updated_at": source_updated_at,
            "structured_event_kind_basis": event_kind_basis,
            "structured_event_occurrence": {
                "title_key": structured_event_title_key(title),
                "start_at": start_at,
                "end_at": end_at,
            },
            "routing": {
                "original_page_hint": page_hint,
                "normalized_page_hint": page_hint,
                "route_destination_valid": True,
                "route_target_exists": False,
                "route_resolution": "structured_event_projection",
            },
        },
    }
    candidate, errors = normalize_event_time_candidate(
        candidate,
        primary_entity_is_event=True,
    )
    errors.extend(event_time_grounding_errors(candidate, candidate["evidence_quote"]))
    return None if errors else candidate


def structured_source_event_evidence(
    document: dict[str, Any],
    title: str,
    start_at: str,
    end_at: str | None,
    source_updated_at: str | None,
) -> dict[str, Any] | None:
    for chunk in document.get("chunks") or []:
        chunk_id = str(chunk.get("chunk_id") or "")
        text = str(chunk.get("text") or "")
        units = evidence_units_for_text(text)
        start_unit = next(
            (
                unit
                for unit in units
                if "event_started_at:" in unit["text"] and start_at in unit["text"]
            ),
            None,
        )
        end_unit = (
            next(
                (
                    unit
                    for unit in units
                    if "event_ended_at:" in unit["text"] and end_at in unit["text"]
                ),
                None,
            )
            if end_at
            else None
        )
        if start_unit is None or (end_at and end_unit is None):
            continue
        title_unit = next(
            (
                unit
                for unit in units
                if title.casefold() in unit["text"].casefold()
            ),
            None,
        )
        source_updated_unit = (
            next(
                (
                    unit
                    for unit in units
                    if "source_updated_at:" in unit["text"]
                    and source_updated_at in unit["text"]
                ),
                None,
            )
            if source_updated_at
            else None
        )
        selected = [
            unit
            for unit in (title_unit, source_updated_unit, start_unit, end_unit)
            if unit
        ]
        unit_ids = stable_unique([unit["unit_id"] for unit in selected])
        resolved = resolve_evidence_unit_ids(
            text,
            chunk_id=chunk_id,
            unit_ids=unit_ids,
        )
        if not resolved["source_spans"]:
            continue
        unit_text_by_id = {unit["unit_id"]: unit["text"] for unit in units}
        time_ids = [start_unit["unit_id"]]
        if end_unit:
            time_ids.append(end_unit["unit_id"])
        return {
            **resolved,
            "evidence_units": [
                {**unit, "text": unit_text_by_id[unit["unit_id"]]}
                for unit in resolved["evidence_units"]
            ],
            "event_expression": "\n".join(
                unit_text_by_id[unit_id] for unit_id in time_ids
            ),
        }
    return None


def structured_event_kind(
    start_at: str, source_updated_at: str | None
) -> tuple[str, str]:
    """Classify trusted schedule metadata conservatively.

    Hyprnote's event bounds can exist before a session occurs.  Its captured
    source-update timestamp is deterministic occurrence evidence only once the
    underlying artifact was updated at or after the scheduled start.  Missing,
    malformed, or pre-start updates therefore remain a plan instead of being
    promoted to an occurrence by ingestion time.
    """

    start = parse_temporal_value(start_at, boundary="start")
    updated = parse_source_updated_at(source_updated_at)
    if start is not None and updated is not None and updated >= start:
        return "actual", "source_updated_at_at_or_after_event_start"
    if updated is not None:
        return "planned", "source_updated_at_before_event_start"
    return "planned", "no_trusted_post_start_occurrence_evidence"


def parse_source_updated_at(value: str | None) -> datetime | None:
    """Parse capture-native Unix seconds/milliseconds or an ISO timestamp."""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        epoch = float(text)
    except ValueError:
        return parse_temporal_value(text, boundary="start")
    if not math.isfinite(epoch):
        return None
    # Capture adapters use both Unix seconds and milliseconds.  Values beyond
    # year 33658 in seconds are unambiguously millisecond-shaped here.
    if abs(epoch) >= 100_000_000_000:
        epoch /= 1000.0
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def structured_event_identity_label(title: str, start_at: str) -> str:
    """Name one occurrence precisely enough to separate same-day sessions."""

    return f"{title} ({canonical_event_bound(start_at) or start_at})"


def structured_event_title_key(title: str) -> str:
    return " ".join(str(title or "").casefold().split())


def coalesce_structured_event_candidates(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Union exact same-occurrence projections before actions are proposed.

    The policy/critic batch is intentionally prepared before any fact action is
    applied, so database duplicate lookup cannot see an earlier candidate from
    that same batch.  Coalescing only deterministic structured projections here
    preserves ordinary extractor candidates and keeps planned and actual facts
    separate.
    """

    coalesced: list[dict[str, Any]] = []
    index_by_key: dict[tuple[Any, ...], int] = {}
    for candidate in candidates:
        key = structured_event_batch_key(candidate)
        if key is None:
            coalesced.append(candidate)
            continue
        prepared = with_structured_event_source(candidate)
        existing_index = index_by_key.get(key)
        if existing_index is None:
            index_by_key[key] = len(coalesced)
            coalesced.append(prepared)
            continue
        coalesced[existing_index] = merge_structured_event_provenance(
            coalesced[existing_index], prepared
        )
    return coalesced


def structured_event_batch_key(candidate: dict[str, Any]) -> tuple[Any, ...] | None:
    metadata = candidate.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("source") != (
        "structured_event_projection"
    ):
        return None
    occurrence = metadata.get("structured_event_occurrence")
    if not isinstance(occurrence, dict):
        return None
    title_key = str(occurrence.get("title_key") or "").strip()
    start_at = canonical_event_bound(occurrence.get("start_at"))
    end_at = canonical_event_bound(occurrence.get("end_at"))
    if not title_key or not start_at:
        return None
    # event_time_signature includes kind, preserving planned-vs-actual facts.
    return (title_key, start_at, end_at or "", *event_time_signature(candidate))


def with_structured_event_source(candidate: dict[str, Any]) -> dict[str, Any]:
    prepared = dict(candidate)
    metadata = dict(candidate.get("metadata") or {})
    source = structured_event_source_record(candidate)
    metadata["structured_event_sources"] = stable_unique_dicts(
        [*(metadata.get("structured_event_sources") or []), source]
    )
    metadata["source_document_ids"] = stable_unique(
        [
            *(metadata.get("source_document_ids") or []),
            str(metadata.get("document_id") or ""),
        ]
    )
    metadata["source_window_ids"] = stable_unique(
        [
            *(metadata.get("source_window_ids") or []),
            str(metadata.get("window_id") or ""),
        ]
    )
    metadata["event_session_ids"] = stable_unique(
        [
            *(metadata.get("event_session_ids") or []),
            str(metadata.get("event_session_id") or ""),
        ]
    )
    metadata["structured_event_source_count"] = len(
        metadata["structured_event_sources"]
    )
    prepared["metadata"] = metadata
    return prepared


def structured_event_source_record(candidate: dict[str, Any]) -> dict[str, Any]:
    metadata = candidate.get("metadata") or {}
    return {
        "document_id": metadata.get("document_id"),
        "window_id": metadata.get("window_id"),
        "event_session_id": metadata.get("event_session_id"),
        "source_updated_at": metadata.get("source_updated_at"),
        "source_ids": list(candidate.get("source_ids") or []),
        "source_spans": list(candidate.get("source_spans") or []),
        "evidence_units": list(metadata.get("evidence_units") or []),
    }


def merge_structured_event_provenance(
    existing: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    merged = dict(existing)
    merged["source_ids"] = stable_unique(
        [*(existing.get("source_ids") or []), *(candidate.get("source_ids") or [])]
    )
    merged["source_spans"] = stable_unique_dicts(
        [
            *(existing.get("source_spans") or []),
            *(candidate.get("source_spans") or []),
        ]
    )
    quotes = stable_unique(
        [existing.get("evidence_quote"), candidate.get("evidence_quote")]
    )
    merged["evidence_quote"] = "\n...\n".join(quotes)
    metadata = dict(existing.get("metadata") or {})
    incoming_metadata = dict(candidate.get("metadata") or {})
    metadata["structured_event_sources"] = stable_unique_dicts(
        [
            *(metadata.get("structured_event_sources") or []),
            *(incoming_metadata.get("structured_event_sources") or []),
        ]
    )
    for key in ("source_document_ids", "source_window_ids", "event_session_ids"):
        metadata[key] = stable_unique(
            [*(metadata.get(key) or []), *(incoming_metadata.get(key) or [])]
        )
    metadata["evidence_units"] = stable_unique_dicts(
        [
            *(metadata.get("evidence_units") or []),
            *(incoming_metadata.get("evidence_units") or []),
        ]
    )
    metadata["structured_event_source_count"] = len(
        metadata["structured_event_sources"]
    )
    merged["metadata"] = metadata
    return merged


def stable_unique_dicts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, dict):
            continue
        key = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        unique.append(value)
    return unique


def structured_event_precision(start_at: str, end_at: str | None) -> str:
    values = [value for value in (start_at, end_at) if value]
    if any("T" in value or " " in value for value in values):
        return "exact"
    shortest = min(len(value) for value in values)
    if shortest >= 10:
        return "day"
    if shortest >= 7:
        return "month"
    return "year"


def structured_event_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug[:120] or "event"


def structured_event_occurrence_slug(title: str, start_at: str) -> str:
    """Keep the full start token even when the human title is very long."""

    start_slug = structured_event_slug(start_at)
    title_slug = structured_event_slug(title)
    title_budget = max(1, 120 - len(start_slug) - 1)
    return f"{title_slug[:title_budget].rstrip('-')}-{start_slug}"


def stable_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
