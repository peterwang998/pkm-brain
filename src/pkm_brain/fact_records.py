from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .db import dumps, loads
from .entities import replace_fact_entity_links
from .util import new_id, now_iso


def row_to_fact(row: Any) -> dict[str, Any]:
    event_time_kind = row_value(row, "event_time_kind")
    event_start_at = row_value(row, "event_start_at")
    event_end_at = row_value(row, "event_end_at")
    event_time_precision = row_value(row, "event_time_precision")
    event_time_expression = row_value(row, "event_time_expression")
    event_time = (
        keep_present(
            {
                "kind": event_time_kind,
                "start_at": event_start_at,
                "end_at": event_end_at,
                "precision": event_time_precision,
                "expression": event_time_expression,
            }
        )
        if event_time_kind
        else None
    )
    return {
        "id": row["id"],
        "statement": row["statement"],
        "entity_key": row["entity_key"],
        "entity_id": row_value(row, "entity_id"),
        "page_hint": row["page_hint"],
        "section_hint": row["section_hint"],
        "source_ids": loads(row["source_ids"], []),
        "observed_at": row["observed_at"],
        "confidence": row["confidence"],
        "source_spans": loads(row_value(row, "source_spans"), []),
        "evidence_quote": row_value(row, "evidence_quote"),
        "extraction_method": row_value(row, "extraction_method", "legacy"),
        "extractor_model": row_value(row, "extractor_model"),
        "effective_at": row_value(row, "effective_at"),
        "valid_from": row_value(row, "valid_from"),
        "valid_to": row_value(row, "valid_to"),
        "temporal_kind": row_value(row, "temporal_kind", "unknown"),
        "valid_time_precision": row_value(row, "valid_time_precision", "unknown"),
        "temporal_expression": row_value(row, "temporal_expression"),
        "temporal_confidence": row_value(row, "temporal_confidence"),
        "knowledge_to": row_value(row, "knowledge_to"),
        "assertion_lineage_id": row_value(row, "assertion_lineage_id", row["id"]),
        "revision_of_id": row_value(row, "revision_of_id"),
        "revision_number": row_value(row, "revision_number", 1),
        "revision_status": row_value(row, "revision_status"),
        "event_time": event_time,
        # Flat mirrors keep persistence and internal merge logic simple.  The
        # nested event_time object is the public extraction/service shape.
        "event_time_kind": event_time_kind,
        "event_start_at": event_start_at,
        "event_end_at": event_end_at,
        "event_time_precision": event_time_precision,
        "event_time_expression": event_time_expression,
        "extraction_confidence": row_value(row, "extraction_confidence"),
        "routing_confidence": row_value(row, "routing_confidence"),
        "truth_confidence": row_value(row, "truth_confidence", row["confidence"]),
        "status": row["status"],
        "supersedes_id": row["supersedes_id"],
        "conflict_group_id": row["conflict_group_id"],
        "confirmed_by_user": bool(row["confirmed_by_user"]),
        "metadata": loads(row["metadata"], {}),
        "created_at": row["created_at"],
        "last_seen_at": row["last_seen_at"],
    }


def fact_values(
    fact: dict[str, Any], fact_id: str, existing: Any | None
) -> tuple[Any, ...]:
    timestamp = now_iso()
    existing_get = existing_value_getter(existing)
    confidence = float(fact.get("confidence", existing_get("confidence", 0.0)) or 0.0)
    return (
        fact_id,
        str(fact.get("statement", existing_get("statement", ""))),
        str(fact.get("entity_key", existing_get("entity_key", "manual:fact"))),
        fact.get("entity_id", existing_get("entity_id")),
        fact.get("page_hint", existing_get("page_hint")),
        fact.get("section_hint", existing_get("section_hint")),
        dumps(fact.get("source_ids", loads(existing_get("source_ids"), []))),
        fact.get("observed_at", existing_get("observed_at")),
        confidence,
        str(fact.get("status", existing_get("status", "active"))),
        fact.get("supersedes_id", existing_get("supersedes_id")),
        fact.get("conflict_group_id", existing_get("conflict_group_id")),
        1 if fact.get("confirmed_by_user", existing_get("confirmed_by_user", 0)) else 0,
        dumps(fact.get("metadata", loads(existing_get("metadata"), {}))),
        fact.get("created_at") or existing_get("created_at", timestamp),
        fact.get("last_seen_at", timestamp),
        dumps(fact.get("source_spans", loads(existing_get("source_spans"), []))),
        fact.get("evidence_quote", existing_get("evidence_quote")),
        str(fact.get("extraction_method", existing_get("extraction_method", "legacy"))),
        fact.get("extractor_model", existing_get("extractor_model")),
        fact.get("effective_at", existing_get("effective_at")),
        fact.get("extraction_confidence", existing_get("extraction_confidence")),
        fact.get("routing_confidence", existing_get("routing_confidence")),
        fact.get("truth_confidence", existing_get("truth_confidence", confidence)),
        str(fact.get("temporal_kind") or existing_get("temporal_kind", "unknown")),
        fact.get("valid_from", existing_get("valid_from")),
        fact.get("valid_to", existing_get("valid_to")),
        str(
            fact.get("valid_time_precision")
            or existing_get("valid_time_precision", "unknown")
        ),
        fact.get("temporal_expression", existing_get("temporal_expression")),
        fact.get("temporal_confidence", existing_get("temporal_confidence")),
        fact.get("knowledge_to", existing_get("knowledge_to")),
        str(
            fact.get("assertion_lineage_id")
            or existing_get("assertion_lineage_id", fact_id)
            or fact_id
        ),
        fact.get("revision_of_id", existing_get("revision_of_id")),
        int(fact.get("revision_number", existing_get("revision_number", 1)) or 1),
        fact.get("revision_status", existing_get("revision_status")),
        event_time_value(fact, "event_time_kind", "kind", existing_get),
        event_time_value(fact, "event_start_at", "start_at", existing_get),
        event_time_value(fact, "event_end_at", "end_at", existing_get),
        event_time_value(fact, "event_time_precision", "precision", existing_get),
        event_time_value(fact, "event_time_expression", "expression", existing_get),
    )


def event_time_value(
    fact: dict[str, Any],
    flat_key: str,
    nested_key: str,
    existing_get: Any,
) -> Any:
    # An explicitly supplied nested object is the authoritative public shape.
    # This includes ``None`` (clear the event time) and omitted nested members
    # (clear stale columns left by an earlier, more complete event time).
    if "event_time" in fact:
        event_time = fact["event_time"]
        if isinstance(event_time, dict):
            return event_time.get(nested_key)
        return None
    if flat_key in fact:
        return fact[flat_key]
    return existing_get(flat_key)


def next_fact_revision(
    existing: dict[str, Any], update: dict[str, Any], revision_id: str, timestamp: str
) -> dict[str, Any]:
    """Build the next open revision while keeping lineage system-owned."""

    return {
        **existing,
        **update,
        "id": revision_id,
        "assertion_lineage_id": existing.get("assertion_lineage_id") or existing["id"],
        "revision_of_id": existing["id"],
        "revision_number": int(existing.get("revision_number") or 1) + 1,
        "revision_status": None,
        "knowledge_to": None,
        "created_at": timestamp,
        "last_seen_at": timestamp,
    }


def next_revision_timestamp(previous_created_at: str | None) -> str:
    """Return a lineage timestamp strictly later than its current revision."""

    candidate = datetime.now(timezone.utc)
    if previous_created_at:
        try:
            previous = datetime.fromisoformat(
                previous_created_at.replace("Z", "+00:00")
            )
            if previous.tzinfo is not None:
                previous = previous.astimezone(timezone.utc)
                if candidate <= previous:
                    candidate = previous + timedelta(microseconds=1)
        except ValueError:
            pass
    return candidate.isoformat()


def write_fact_row(
    conn: Any, fact: dict[str, Any], fact_id: str, existing: Any | None
) -> None:
    """Write one exact fact row; lifecycle policy belongs to the caller."""

    values = fact_values(fact, fact_id, existing)
    if existing:
        conn.execute(
            """
            UPDATE facts
            SET statement = ?, entity_key = ?, entity_id = ?, page_hint = ?, section_hint = ?,
                source_ids = ?, observed_at = ?, confidence = ?, status = ?,
                supersedes_id = ?, conflict_group_id = ?, confirmed_by_user = ?,
                metadata = ?, created_at = ?, last_seen_at = ?, source_spans = ?, evidence_quote = ?,
                extraction_method = ?, extractor_model = ?, effective_at = ?,
                extraction_confidence = ?, routing_confidence = ?, truth_confidence = ?,
                temporal_kind = ?, valid_from = ?, valid_to = ?, valid_time_precision = ?,
                temporal_expression = ?, temporal_confidence = ?, knowledge_to = ?,
                assertion_lineage_id = ?, revision_of_id = ?, revision_number = ?,
                revision_status = ?, event_time_kind = ?, event_start_at = ?,
                event_end_at = ?, event_time_precision = ?, event_time_expression = ?
            WHERE id = ?
            """,
            (*values[1:], fact_id),
        )
        return
    conn.execute(
        """
        INSERT INTO facts(
          id, statement, entity_key, entity_id, page_hint, section_hint, source_ids,
          observed_at, confidence, status, supersedes_id, conflict_group_id,
          confirmed_by_user, metadata, created_at, last_seen_at, source_spans,
          evidence_quote, extraction_method, extractor_model, effective_at,
          extraction_confidence, routing_confidence, truth_confidence,
          temporal_kind, valid_from, valid_to, valid_time_precision,
          temporal_expression, temporal_confidence, knowledge_to,
          assertion_lineage_id, revision_of_id, revision_number, revision_status,
          event_time_kind, event_start_at, event_end_at, event_time_precision,
          event_time_expression
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )


def effective_revision_status(fact: dict[str, Any]) -> str:
    status = str(fact.get("status") or "")
    if status == "revision_closed":
        return str(fact.get("revision_status") or "")
    return status


def merge_fact_inverse(
    target: dict[str, Any], fragment: dict[str, Any]
) -> dict[str, Any]:
    for key in (
        "delete_fact_ids",
        "restore_facts",
        "restore_revision_head_links",
    ):
        values = fragment.get(key)
        if values:
            target.setdefault(key, []).extend(values)
    if fragment.get("restore_fact"):
        target.setdefault("restore_facts", []).append(fragment["restore_fact"])
    return target


def stored_fact_entity_links(conn: Any, fact_id: str) -> list[dict[str, Any]]:
    return [
        {
            "entity_id": row["entity_id"],
            "is_primary": bool(row["is_primary"]),
            "mention_text": row["mention_text"],
            "mention_span": loads(row["mention_span"], None),
            "mention_kind": row["mention_kind"],
            "resolution_method": row["resolution_method"],
            "confidence": row["confidence"],
        }
        for row in conn.execute(
            """
            SELECT * FROM fact_entities
            WHERE fact_id = ?
            ORDER BY is_primary DESC, id
            """,
            (fact_id,),
        )
    ]


def write_versioned_fact(
    conn: Any,
    fact: dict[str, Any],
    fact_id: str,
    existing: Any | None,
    entity_links: list[dict[str, Any]],
    *,
    record_revision: bool = True,
) -> tuple[list[str], dict[str, Any]]:
    """Write a fact through the single copy-before-write lifecycle."""

    if existing and record_revision:
        old_fact = row_to_fact(existing)
        timestamp = next_revision_timestamp(old_fact.get("created_at"))
        if old_fact.get("knowledge_to") or old_fact.get("status") == "revision_closed":
            raise ValueError(f"stale fact revision: {fact_id}")
        old_links = stored_fact_entity_links(conn, fact_id)
        history_id = new_id("factrev")
        reserved = conn.execute(
            """
            UPDATE facts SET revision_number = revision_number + 1
            WHERE id = ? AND revision_number = ? AND knowledge_to IS NULL
            """,
            (fact_id, int(old_fact.get("revision_number") or 1)),
        )
        if reserved.rowcount != 1:
            raise ValueError(f"stale fact revision: {fact_id}")
        history = {
            **old_fact,
            "id": history_id,
            "status": "revision_closed",
            "revision_status": old_fact.get("status"),
            "knowledge_to": timestamp,
        }
        write_fact_row(conn, history, history_id, None)
        replace_fact_entity_links(conn, fact_id=history_id, links=old_links)
        fact = next_fact_revision(old_fact, fact, fact_id, timestamp)
        fact["revision_of_id"] = history_id
        touched_ids = [fact_id, history_id]
        inverse = {
            "delete_fact_ids": [history_id],
            "restore_fact": old_fact,
            "restore_revision_head_links": [{"fact_id": fact_id, "links": old_links}],
        }
    else:
        if not existing and record_revision:
            fact.update(
                {
                    "assertion_lineage_id": fact_id,
                    "revision_of_id": None,
                    "revision_number": 1,
                    "revision_status": None,
                    "knowledge_to": None,
                }
            )
        touched_ids = [fact_id]
        inverse = (
            {"restore_fact": row_to_fact(existing)}
            if existing
            else {"delete_fact_ids": [fact_id]}
        )
    write_fact_row(conn, fact, fact_id, existing)
    replace_fact_entity_links(conn, fact_id=fact_id, links=entity_links)
    return touched_ids, inverse


def revise_stored_fact(
    conn: Any,
    fact_id: str,
    updates: dict[str, Any],
    *,
    entity_links: list[dict[str, Any]] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Open a new revision without re-running entity resolution."""

    existing = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
    if not existing:
        raise ValueError(f"fact not found: {fact_id}")
    links = (
        stored_fact_entity_links(conn, fact_id)
        if entity_links is None
        else entity_links
    )
    return write_versioned_fact(
        conn,
        {**row_to_fact(existing), **updates},
        fact_id,
        existing,
        links,
    )


def slim_fact_for_context(fact: dict[str, Any]) -> dict[str, Any]:
    output = keep_present(
        {
            key: fact.get(key)
            for key in (
                "id",
                "statement",
                "entity_key",
                "page_hint",
                "section_hint",
                "observed_at",
                "created_at",
                "knowledge_to",
                "assertion_lineage_id",
                "revision_of_id",
                "revision_number",
                "revision_status",
                "temporal_kind",
                "valid_from",
                "valid_to",
                "valid_time_precision",
                "temporal_expression",
                "temporal_confidence",
                "confidence",
                "source_spans",
                "extraction_method",
                "extractor_model",
                "truth_confidence",
                "status",
                "retrieval_score",
                "authoritative",
                "contested",
                "conflict_group_id",
                "supersedes_id",
                "temporal_match",
                "currentness_reason",
                "event_time",
            )
        }
    )
    output["source_ids"] = list(fact.get("source_ids") or [])[:3]
    quote = str(fact.get("evidence_quote") or "")
    if quote:
        output["evidence_quote"] = truncate_text(quote, 1200)
    contested = fact.get("contested_facts")
    if isinstance(contested, list):
        output["contested_facts"] = [
            slim_fact_for_context(row) for row in contested[:3] if isinstance(row, dict)
        ]
    return output


def fact_citation_snapshots(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = (
        "statement",
        "status",
        "page_hint",
        "source_ids",
        "source_spans",
        "truth_confidence",
        "conflict_group_id",
        "temporal_kind",
        "valid_from",
        "valid_to",
        "valid_time_precision",
        "assertion_lineage_id",
        "revision_number",
        "knowledge_to",
        "event_time",
    )
    return [
        {
            "type": "fact",
            "fact_id": fact.get("id"),
            **{
                key: fact.get(key)
                if fact.get(key) is not None
                else ([] if key in {"source_ids", "source_spans"} else None)
                for key in keys
            },
            "contested": bool(fact.get("contested")),
        }
        for fact in facts
    ]


def row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        if hasattr(row, "keys") and key not in row.keys():
            return default
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def existing_value_getter(row: Any | None) -> Any:
    return lambda key, default=None: row_value(row, key, default) if row else default


def keep_present(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def truncate_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    suffix = " ... [truncated]"
    return value[: max_chars - len(suffix)].rstrip() + suffix
