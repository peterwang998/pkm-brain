from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .db import loads


PERSON_ROUTE_REVIEW_REASONS = {
    "person_page_identity_ambiguous",
    "person_page_identity_mismatch",
    "person_page_identity_sensitive_mismatch",
}
_PERSON_ROUTE_GUARD = "person_page_identity_v1"
_HEALTH_FACT_RE = re.compile(
    r"\b(?:diagnos(?:ed|is)|disease|doctor|health|hospital|illness|injur(?:y|ed)|"
    r"medical|medication|prescri(?:bed|ption)|pregnan(?:cy|t)|surger(?:y|ies)|"
    r"symptom|therap(?:y|ist)|treat(?:ed|ment))\b",
    re.IGNORECASE,
)
_HONORIFICS = {
    "doctor",
    "dr",
    "miss",
    "mr",
    "mrs",
    "ms",
    "prof",
    "professor",
}


def enrich_page_identity_targets(
    conn: Any, route_targets: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Attach private canonical-person evidence used only by route guards."""

    enriched = {page: dict(target) for page, target in route_targets.items()}
    people_pages = [page for page in enriched if event_or_person_namespace(page) == "people"]
    if not people_pages:
        return enriched
    placeholders = ",".join("?" for _ in people_pages)
    rows = conn.execute(
        f"""
        SELECT f.page_hint, e.id, e.name, e.aliases
        FROM facts f
        JOIN entities e ON e.id = f.entity_id
        WHERE f.page_hint IN ({placeholders})
          AND f.status IN ('active', 'contested')
          AND e.status = 'active'
          AND e.entity_type = 'person'
        ORDER BY f.page_hint, f.created_at, f.id
        """,
        people_pages,
    )
    identities: dict[str, dict[str, set[str]]] = {}
    for row in rows:
        bucket = identities.setdefault(
            str(row["page_hint"]), {"ids": set(), "names": set()}
        )
        bucket["ids"].add(str(row["id"]))
        bucket["names"].add(str(row["name"]))
        bucket["names"].update(
            str(alias)
            for alias in loads(row["aliases"], [])
            if str(alias or "").strip()
        )
    for page_hint in people_pages:
        target = enriched[page_hint]
        identity = identities.get(page_hint, {"ids": set(), "names": set()})
        target["_primary_person_entity_ids"] = sorted(identity["ids"])
        target["_primary_person_names"] = sorted(identity["names"])
    return enriched


def guard_people_page_identity(
    candidate: dict[str, Any], route_targets: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Fail closed when a named primary person conflicts with a people page."""

    page_hint = str(candidate.get("page_hint") or "")
    if event_or_person_namespace(page_hint) != "people":
        return candidate
    if candidate_routing(candidate).get("route_destination_valid") is False:
        return candidate
    primary = primary_named_person(candidate)
    if primary is None:
        return candidate
    target = route_targets.get(page_hint)
    candidate_entity_id = str(candidate.get("entity_id") or "").strip()
    target_ids = target_person_entity_ids(target)
    surface = str(primary.get("surface") or "").strip()
    candidate_key = person_identity_key(surface)
    target_keys = {
        person_identity_key(name)
        for name in target_person_names(page_hint, target)
        if person_identity_key(name)
    }
    if candidate_key and candidate_key in target_keys:
        return with_person_route_guard(candidate, "person_page_identity_compatible")
    if not candidate_key and candidate_entity_id and candidate_entity_id in target_ids:
        return with_person_route_guard(candidate, "person_page_identity_compatible")
    sensitive = health_or_medical_fact(candidate)
    reason = (
        "person_page_identity_sensitive_mismatch"
        if sensitive
        else person_mismatch_reason(candidate_key, target_keys)
    )
    return held_person_route(candidate, reason, sensitive=sensitive)


def primary_named_person(candidate: dict[str, Any]) -> dict[str, Any] | None:
    mentions = candidate.get("entity_mentions")
    metadata = candidate_metadata(candidate)
    if not isinstance(mentions, list):
        mentions = metadata.get("model_entity_mentions")
    if isinstance(mentions, list):
        for mention in mentions:
            if not isinstance(mention, dict) or not mention.get("is_primary"):
                continue
            entity_type = str(
                mention.get("entity_type") or mention.get("type") or ""
            ).strip().lower()
            mention_kind = str(mention.get("mention_kind") or "named").strip().lower()
            if entity_type == "person" and mention_kind == "named":
                return mention
        return None
    if str(candidate.get("entity_type") or "").strip().lower() != "person":
        return None
    surface = str(
        candidate.get("entity_mention") or metadata.get("model_entity_key") or ""
    ).strip()
    return {"surface": surface} if surface else None


def target_person_names(
    page_hint: str, target: dict[str, Any] | None
) -> list[str]:
    names: list[str] = []
    if isinstance(target, dict):
        names.extend(
            str(item)
            for item in target.get("_primary_person_names") or []
            if str(item or "").strip()
        )
        canonical = str(target.get("canonical_entity") or "").strip()
        if canonical:
            names.append(canonical)
    stem = Path(page_hint).stem.replace("-", " ").replace("_", " ").strip()
    if stem:
        names.append(stem)
    return list(dict.fromkeys(names))


def target_person_entity_ids(target: dict[str, Any] | None) -> set[str]:
    if not isinstance(target, dict):
        return set()
    raw = target.get("_primary_person_entity_ids")
    return {
        str(item)
        for item in raw
        if str(item or "").strip()
    } if isinstance(raw, list) else set()


def person_identity_key(value: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", str(value or "").casefold())
    return "".join(token for token in tokens if token not in _HONORIFICS)


def person_mismatch_reason(candidate_key: str, target_keys: set[str]) -> str:
    if not candidate_key or not target_keys:
        return "person_page_identity_ambiguous"
    if any(
        candidate_key in target_key or target_key in candidate_key
        for target_key in target_keys
    ):
        return "person_page_identity_ambiguous"
    return "person_page_identity_mismatch"


def health_or_medical_fact(candidate: dict[str, Any]) -> bool:
    text = "\n".join(
        (
            str(candidate.get("statement") or ""),
            str(candidate.get("evidence_quote") or ""),
            str(candidate.get("section_hint") or ""),
        )
    )
    return bool(_HEALTH_FACT_RE.search(text))


def with_person_route_guard(
    candidate: dict[str, Any], route_resolution: str
) -> dict[str, Any]:
    routed = dict(candidate)
    metadata = candidate_metadata(routed)
    routing = candidate_routing(routed)
    routing.update(
        {
            "route_destination_valid": True,
            "route_resolution": route_resolution,
            "person_page_identity_guard": _PERSON_ROUTE_GUARD,
        }
    )
    routing.pop("route_review_reason", None)
    routing.pop("person_page_identity_guard_locked", None)
    metadata["routing"] = routing
    routed["metadata"] = metadata
    return routed


def held_person_route(
    candidate: dict[str, Any], reason: str, *, sensitive: bool
) -> dict[str, Any]:
    held = dict(candidate)
    metadata = candidate_metadata(held)
    routing = candidate_routing(held)
    routing.update(
        {
            "route_destination_valid": False,
            "route_resolution": "held_for_routing_review",
            "route_review_reason": reason,
            "person_page_identity_guard": _PERSON_ROUTE_GUARD,
            "person_page_identity_guard_locked": True,
            "person_page_identity_sensitive": sensitive,
        }
    )
    metadata["routing"] = routing
    held["metadata"] = metadata
    return held


def event_or_person_namespace(page_hint: str) -> str:
    return str(page_hint or "").strip().casefold().split("/", 1)[0]


def candidate_metadata(candidate: dict[str, Any]) -> dict[str, Any]:
    raw = candidate.get("metadata")
    return dict(raw) if isinstance(raw, dict) else {}


def candidate_routing(candidate: dict[str, Any]) -> dict[str, Any]:
    raw = candidate_metadata(candidate).get("routing")
    return dict(raw) if isinstance(raw, dict) else {}
