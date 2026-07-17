from __future__ import annotations

from typing import Any

from .temporal import temporal_merge_compatible
from .temporal_enrichment import strip_event_time


def event_time_for_resolved_primary_event(
    conn: Any, fact: dict[str, Any], links: list[dict[str, Any]]
) -> dict[str, Any]:
    """Fail open on the fact when its optional event entity did not resolve."""

    if not isinstance(fact.get("event_time"), dict):
        return fact
    primary = next((link for link in links if link.get("is_primary")), None)
    entity = None
    if primary is not None:
        entity = conn.execute(
            "SELECT entity_type, status FROM entities WHERE id = ?",
            (str(primary.get("entity_id") or ""),),
        ).fetchone()
    if (
        entity is not None
        and str(entity["entity_type"] or "") == "event"
        and str(entity["status"] or "active") == "active"
    ):
        return fact
    stripped = strip_event_time(fact)
    metadata = (
        dict(stripped.get("metadata"))
        if isinstance(stripped.get("metadata"), dict)
        else {}
    )
    metadata["event_time_persistence_warning"] = (
        "event_time stripped because the primary entity did not resolve to an active event"
    )
    stripped["metadata"] = metadata
    return stripped


def reject_incompatible_fact_merge(
    keeper: dict[str, Any], facts: list[dict[str, Any]]
) -> None:
    incompatible_ids = [
        str(fact["id"])
        for fact in facts
        if fact["id"] != keeper["id"] and not temporal_merge_compatible(keeper, fact)
    ]
    if incompatible_ids:
        raise ValueError(
            "fact_merge requires temporally compatible facts; incompatible: "
            + ", ".join(incompatible_ids)
        )
