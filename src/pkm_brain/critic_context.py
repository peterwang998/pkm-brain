from __future__ import annotations

import re
from typing import Any

from .db import connection
from .gmail_sensitive_data import sanitize_gmail_model_payload
from .paths import BrainPaths
from .source_evidence import evidence_units_for_text


MAX_CRITIC_REPAIR_EVIDENCE_UNITS = 5
CRITIC_CONTEXT_RADIUS = 4


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


def stable_critic_repair_unit_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        unit_id = str(item or "").strip()
        if not unit_id or unit_id in output:
            continue
        output.append(unit_id)
        if len(output) >= MAX_CRITIC_REPAIR_EVIDENCE_UNITS:
            break
    return output


def critic_fact_source_context(
    paths: BrainPaths, action: dict[str, Any]
) -> dict[str, Any] | None:
    if action.get("action_type") != "fact_upsert":
        return None
    payload = _action_payload(action)
    fact = payload.get("fact") if isinstance(payload.get("fact"), dict) else None
    if fact is None:
        return {"available": False, "reason": "fact payload missing"}
    chunk_id = critic_fact_chunk_id(fact)
    if not chunk_id:
        return {"available": False, "reason": "fact evidence chunk missing"}
    with connection(paths.sqlite_path) as conn:
        row = conn.execute(
            """
            SELECT c.id AS chunk_id, c.document_id, c.chunk_index, c.text,
                   d.title, d.source_type
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.id = ?
            """,
            (chunk_id,),
        ).fetchone()
        if row is None:
            return {"available": False, "reason": "evidence chunk not found"}
        document_chunks = list(
            conn.execute(
                """
                SELECT id, chunk_index, text
                FROM chunks
                WHERE document_id = ?
                ORDER BY chunk_index
                """,
                (row["document_id"],),
            )
        )
    units = evidence_units_for_text(str(row["text"] or ""))
    units_by_id = {str(unit["unit_id"]): unit for unit in units}
    cited_unit_ids = critic_fact_evidence_unit_ids(fact)
    cited_units = [
        units_by_id[unit_id] for unit_id in cited_unit_ids if unit_id in units_by_id
    ]
    cited_indexes = [int(unit["unit_index"]) for unit in cited_units]
    if cited_indexes:
        context_start = max(0, min(cited_indexes) - CRITIC_CONTEXT_RADIUS)
        context_end = min(len(units), max(cited_indexes) + CRITIC_CONTEXT_RADIUS + 1)
    else:
        context_start = 0
        context_end = min(len(units), CRITIC_CONTEXT_RADIUS * 2 + 1)
    repairable_units = [
        critic_unit_card(unit, cited_unit_ids=cited_unit_ids)
        for unit in units[context_start:context_end]
    ]
    relevant_speakers = {
        str(unit.get("speaker") or "") for unit in cited_units if unit.get("speaker")
    }
    context = {
        "available": True,
        "document": {
            "document_id": str(row["document_id"]),
            "title": str(row["title"] or ""),
            "source_type": str(row["source_type"] or ""),
        },
        "repairable_chunk_id": chunk_id,
        "currently_cited_unit_ids": cited_unit_ids,
        "repairable_units": repairable_units,
        "speaker_identity_context": critic_speaker_identity_context(
            document_chunks, relevant_speakers
        ),
        "named_entity_attribution_context": critic_named_entity_context(
            document_chunks, fact
        ),
        "known_participants": critic_known_participants(document_chunks),
    }
    if str(row["source_type"] or "") == "gmail_thread":
        sanitized = sanitize_gmail_model_payload(context)
        return sanitized if isinstance(sanitized, dict) else context
    return context


def critic_fact_chunk_id(fact: dict[str, Any]) -> str:
    metadata = fact.get("metadata") if isinstance(fact.get("metadata"), dict) else {}
    evidence_units = metadata.get("evidence_units")
    if isinstance(evidence_units, list):
        for unit in evidence_units:
            if isinstance(unit, dict) and str(unit.get("chunk_id") or "").strip():
                return str(unit["chunk_id"]).strip()
    spans = fact.get("source_spans")
    if isinstance(spans, list):
        for span in spans:
            if isinstance(span, dict) and str(span.get("chunk_id") or "").strip():
                return str(span["chunk_id"]).strip()
    return ""


def critic_fact_evidence_unit_ids(fact: dict[str, Any]) -> list[str]:
    direct = fact.get("evidence_unit_ids")
    if isinstance(direct, list):
        unit_ids = stable_critic_repair_unit_ids(direct)
        if unit_ids:
            return unit_ids
    metadata = fact.get("metadata") if isinstance(fact.get("metadata"), dict) else {}
    evidence_units = metadata.get("evidence_units")
    if not isinstance(evidence_units, list):
        return []
    return stable_critic_repair_unit_ids(
        [unit.get("unit_id") for unit in evidence_units if isinstance(unit, dict)]
    )


def critic_unit_card(
    unit: dict[str, Any], *, cited_unit_ids: list[str]
) -> dict[str, Any]:
    return {
        "unit_id": str(unit["unit_id"]),
        "text": str(unit["text"]),
        "cited": str(unit["unit_id"]) in cited_unit_ids,
        **({"speaker": str(unit["speaker"])} if unit.get("speaker") else {}),
    }


def critic_speaker_identity_context(
    chunks: list[Any], relevant_speakers: set[str]
) -> list[dict[str, Any]]:
    if not relevant_speakers:
        return []
    output: list[dict[str, Any]] = []
    per_speaker_count: dict[str, int] = {}
    identity_re = re.compile(
        r"\b(?:i(?:'|\N{RIGHT SINGLE QUOTATION MARK})m|i am|this is|my name is)\b",
        re.IGNORECASE,
    )
    for chunk in chunks:
        for unit in evidence_units_for_text(str(chunk["text"] or "")):
            speaker = str(unit.get("speaker") or "")
            if speaker not in relevant_speakers:
                continue
            seen_count = per_speaker_count.get(speaker, 0)
            is_identity_unit = bool(identity_re.search(str(unit["text"])))
            if seen_count >= 2 and not is_identity_unit:
                continue
            output.append(
                {
                    "chunk_id": str(chunk["id"]),
                    "unit_id": str(unit["unit_id"]),
                    "speaker": speaker,
                    "text": str(unit["text"]),
                }
            )
            per_speaker_count[speaker] = seen_count + 1
            if len(output) >= 8:
                return output
    return output


def critic_known_participants(chunks: list[Any]) -> list[str]:
    output: list[str] = []
    in_participants = False
    for chunk in chunks:
        for raw_line in str(chunk["text"] or "").splitlines():
            line = raw_line.strip()
            if line.casefold() == "## known participants":
                in_participants = True
                continue
            if in_participants and line.startswith("## "):
                in_participants = False
            if in_participants and line.startswith("- "):
                participant = line[2:].strip()
                if participant and participant not in output:
                    output.append(participant)
            if len(output) >= 12:
                return output
    return output


def _action_payload(action: dict[str, Any]) -> dict[str, Any]:
    evidence = action.get("evidence_json") or {}
    payload = evidence.get("payload") or {}
    return payload if isinstance(payload, dict) else {}
