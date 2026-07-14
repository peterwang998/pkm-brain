from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from typing import Any


BRIEFING_SECTION_ORDER = (
    "focus",
    "urgent_overflow",
    "now_and_next",
    "upcoming",
    "overdue_and_due",
    "waiting",
    "attention",
    "awareness",
    "low_confidence",
    "changed",
    "suppressed",
)
BRIEFING_SECTIONS_STORAGE_BYTES = 262_144
# Leave a small margin below SQLite's immutable storage constraint. The projection
# is intentionally a bounded briefing preview; complete item and decision history
# remains in the operational tables.
BRIEFING_SECTIONS_TARGET_BYTES = 240 * 1024
BRIEFING_SECTION_PREVIEW_LIMITS = {
    "focus": 5,
    "urgent_overflow": 20,
    "now_and_next": 20,
    "upcoming": 30,
    "overdue_and_due": 30,
    "waiting": 30,
    "attention": 30,
    "awareness": 30,
    "low_confidence": 30,
    "changed": 20,
    "suppressed": 30,
}

_TEXT_FIELD_BYTES = {
    "id": 256,
    "item_id": 256,
    "kind": 64,
    "state": 64,
    "title": 2_000,
    "details": 1_000,
    "owner": 512,
    "counterparty": 512,
    "starts_at": 128,
    "ends_at": 128,
    "due_at": 128,
    "expires_at": 128,
    "snoozed_until": 128,
    "source_timezone": 128,
    "source_type": 128,
    "account_key": 512,
    "stream_key": 512,
    "source_key": 1_024,
    "updated_at": 128,
    "created_at": 128,
    "latest_event_type": 128,
    "reconciliation_status": 128,
    "handled_verdict": 128,
    "why_now": 1_000,
    "next_move": 1_000,
    "local_evidence_route": 4_096,
    "provider_route": 4_096,
    "reason_code": 128,
    "disposition": 128,
}
_SCALAR_FIELDS = {"priority", "confidence", "handled_confidence"}
# A local evidence route plus one stable provider reference is sufficient to reopen
# the complete source-backed record without duplicating its full provenance payload
# into every briefing section where the same item may appear.
_MAX_EVIDENCE_REFS = 1
_MAX_EVIDENCE_REF_KEYS = 8
_MAX_FEEDBACK_ACTIONS = 8


def bound_briefing_projection(
    sections: Mapping[str, Any],
    *,
    counts: Mapping[str, Any] | None = None,
    maximum_bytes: int = BRIEFING_SECTIONS_TARGET_BYTES,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Return a deterministic, UI-complete briefing preview below the DB limit.

    The caller-supplied counts retain total cardinality. Projection metadata records
    how many cards were included and omitted from every section, so bounding never
    turns overflow into an implicit all-clear.
    """

    if not 1_024 <= maximum_bytes <= BRIEFING_SECTIONS_STORAGE_BYTES:
        raise ValueError("briefing projection byte budget is outside the storage limit")

    prior_counts = dict(counts or {})
    prior_projection = prior_counts.get("section_projection")
    prior_projection = (
        prior_projection if isinstance(prior_projection, Mapping) else {}
    )
    candidates: dict[str, list[dict[str, Any]]] = {}
    totals: dict[str, int] = {}
    for section in BRIEFING_SECTION_ORDER:
        raw_values = sections.get(section)
        values = (
            list(raw_values)
            if isinstance(raw_values, Sequence)
            and not isinstance(raw_values, (str, bytes, bytearray))
            else []
        )
        declared = _declared_section_total(
            section,
            counts=prior_counts,
            projection=prior_projection,
        )
        totals[section] = max(len(values), declared)
        limit = BRIEFING_SECTION_PREVIEW_LIMITS[section]
        candidates[section] = [
            card
            for value in values[:limit]
            if (card := _compact_card(value)) is not None
        ]

    projected = {section: [] for section in BRIEFING_SECTION_ORDER}

    # Focus is the contract's top-five decision surface and must win the byte budget.
    for card in candidates["focus"]:
        _append_if_fits(projected, "focus", card, maximum_bytes=maximum_bytes)

    # Fill the remaining sections fairly. This guarantees that audit and uncertainty
    # previews remain represented instead of allowing one large section to crowd out
    # every section that follows it.
    indexes = {section: 0 for section in BRIEFING_SECTION_ORDER[1:]}
    while True:
        advanced = False
        for section in BRIEFING_SECTION_ORDER[1:]:
            index = indexes[section]
            values = candidates[section]
            if index >= len(values):
                continue
            advanced = True
            indexes[section] = index + 1
            _append_if_fits(
                projected,
                section,
                values[index],
                maximum_bytes=maximum_bytes,
            )
        if not advanced:
            break

    projection_counts: dict[str, dict[str, int]] = {}
    omitted_total = 0
    for section in BRIEFING_SECTION_ORDER:
        included = len(projected[section])
        total = max(totals[section], included)
        omitted = max(0, total - included)
        projection_counts[section] = {
            "total": total,
            "included": included,
            "omitted": omitted,
        }
        omitted_total += omitted
        prior_counts.setdefault(section, total)

    prior_counts.update(
        {
            "section_projection": projection_counts,
            "briefing_sections_bytes": _json_bytes(projected),
            "briefing_sections_truncated": omitted_total > 0,
            "briefing_sections_omitted": omitted_total,
        }
    )
    return projected, prior_counts


def _declared_section_total(
    section: str,
    *,
    counts: Mapping[str, Any],
    projection: Mapping[str, Any],
) -> int:
    prior = projection.get(section)
    if isinstance(prior, Mapping):
        total = prior.get("total")
        if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
            return total
    total = counts.get(section)
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return total
    return 0


def _append_if_fits(
    projected: dict[str, list[dict[str, Any]]],
    section: str,
    card: dict[str, Any],
    *,
    maximum_bytes: int,
) -> bool:
    projected[section].append(card)
    if _json_bytes(projected) <= maximum_bytes:
        return True
    projected[section].pop()
    return False


def _compact_card(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    output: dict[str, Any] = {}
    for field, maximum in _TEXT_FIELD_BYTES.items():
        raw = value.get(field)
        if raw is None:
            if field in value:
                output[field] = None
            continue
        text = _clip_utf8(str(raw), maximum).strip()
        if text:
            output[field] = text
    for field in _SCALAR_FIELDS:
        raw = value.get(field)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        if isinstance(raw, float) and not math.isfinite(raw):
            continue
        output[field] = raw

    evidence = value.get("evidence_refs")
    if isinstance(evidence, Sequence) and not isinstance(
        evidence, (str, bytes, bytearray)
    ):
        refs = [
            ref
            for raw in evidence[:_MAX_EVIDENCE_REFS]
            if (ref := _compact_reference(raw))
        ]
        if refs:
            output["evidence_refs"] = refs

    actions = value.get("feedback_actions")
    if isinstance(actions, Sequence) and not isinstance(
        actions, (str, bytes, bytearray)
    ):
        compact_actions = list(
            dict.fromkeys(
                action
                for raw in actions[:_MAX_FEEDBACK_ACTIONS]
                if (action := _clip_utf8(str(raw), 128).strip())
            )
        )
        if compact_actions:
            output["feedback_actions"] = compact_actions
    return output or None


def _compact_reference(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    output: dict[str, Any] = {}
    for raw_key in sorted(value, key=str)[:_MAX_EVIDENCE_REF_KEYS]:
        key = _clip_utf8(str(raw_key), 128).strip()
        raw = value[raw_key]
        if not key or raw is None or isinstance(raw, (Mapping, list, tuple, set)):
            continue
        if isinstance(raw, bool):
            output[key] = raw
        elif isinstance(raw, int):
            output[key] = raw
        elif isinstance(raw, float) and math.isfinite(raw):
            output[key] = raw
        else:
            text = _clip_utf8(str(raw), 1_024).strip()
            if text:
                output[key] = text
    return output or None


def _clip_utf8(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    suffix = "…"
    prefix = encoded[: maximum_bytes - len(suffix.encode("utf-8"))]
    return prefix.decode("utf-8", errors="ignore") + suffix


def _json_bytes(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    )
