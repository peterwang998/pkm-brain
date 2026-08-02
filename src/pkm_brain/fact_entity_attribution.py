from __future__ import annotations

from typing import Any, Callable

from .db import dumps, loads
from .entities import (
    normalize_entity_type,
    normalize_mention_kind,
    optional_float,
)


FACT_ENTITY_ATTRIBUTION_SNAPSHOT_KEY = "entity_attribution_snapshot"
FACT_ENTITY_ATTRIBUTION_SNAPSHOT_VERSION = 1
FACT_ENTITY_ID_ORIGINS = {"derived", "explicit", "unknown"}


def fact_entity_mentions(fact: dict[str, Any]) -> list[dict[str, Any]]:
    raw_mentions = fact.get("entity_mentions")
    structured_mentions_supplied = raw_mentions is not None
    if raw_mentions is None:
        metadata = fact.get("metadata")
        if isinstance(metadata, str):
            metadata = loads(metadata, {})
        if isinstance(metadata, dict):
            raw_mentions = (
                metadata.get("model_entity_mentions")
                if "model_entity_mentions" in metadata
                else fact_entity_attribution_snapshot_mentions(metadata)
            )
            structured_mentions_supplied = raw_mentions is not None
    mentions = normalize_fact_entity_mentions(raw_mentions)
    if mentions or structured_mentions_supplied:
        return mentions
    primary = primary_entity_mention(fact)
    if not primary:
        return []
    return [
        {
            "surface": primary,
            "entity_type": normalize_entity_type(fact.get("entity_type")),
            "is_primary": True,
            "mention_span": None,
            "mention_kind": "named",
            "confidence": optional_float(
                fact.get("truth_confidence") or fact.get("confidence")
            ),
        }
    ]


def fact_entity_attribution_input_present(fact: dict[str, Any]) -> bool:
    """Whether the payload supplies an entity input consumed by fact_upsert."""

    if fact.get("entity_mentions") is not None:
        return True
    if str(fact.get("entity_id") or "").strip():
        return True
    metadata = fact.get("metadata")
    if isinstance(metadata, str):
        metadata = loads(metadata, {})
    if isinstance(metadata, dict) and (
        "model_entity_mentions" in metadata
        or bool(str(metadata.get("model_entity_key") or "").strip())
        or fact_entity_attribution_snapshot_mentions(metadata) is not None
    ):
        return True
    return any(
        bool(str(fact.get(key) or "").strip())
        for key in ("entity_mention", "entity_name")
    )


def fact_entity_attribution_snapshot_mentions(
    metadata: dict[str, Any],
) -> list[dict[str, Any]] | None:
    snapshot = metadata.get(FACT_ENTITY_ATTRIBUTION_SNAPSHOT_KEY)
    if not isinstance(snapshot, dict) or snapshot.get("version") != (
        FACT_ENTITY_ATTRIBUTION_SNAPSHOT_VERSION
    ):
        return None
    mentions = snapshot.get("mentions")
    if not isinstance(mentions, list):
        return None
    return [dict(mention) for mention in mentions if isinstance(mention, dict)]


def fact_entity_id_is_explicit(fact: dict[str, Any]) -> bool:
    """Whether ``entity_id`` is authored input rather than a resolver cache.

    A persisted fact contains the resolver-selected ID even when its source
    action did not. The attribution receipt makes that round trip lossless. A
    missing, unknown, or stale receipt remains conservative: changing an ID
    must reopen review rather than inherit an unrelated decision.
    """

    entity_id = str(fact.get("entity_id") or "").strip()
    if not entity_id:
        return False
    snapshot = fact_entity_attribution_snapshot(fact.get("metadata"))
    return not (
        snapshot is not None
        and snapshot.get("entity_id_origin") == "derived"
        and str(snapshot.get("entity_id") or "").strip() == entity_id
    )


def fact_clears_entity_attribution(fact: dict[str, Any]) -> bool:
    """Whether explicit empty attribution should clear a prior entity route.

    Empty top-level mentions and empty model/snapshot mentions are authored
    inputs, not omissions.  They therefore clear an inherited resolver route.
    A non-empty authored ``entity_id`` remains authoritative; a resolver ID
    carried by a matching derived receipt does not turn an empty mention input
    into an authored route.
    """

    return (
        fact_entity_attribution_input_present(fact)
        and not fact_entity_mentions(fact)
        and not fact_entity_id_is_explicit(fact)
    )


def fact_entity_attribution_snapshot(metadata: Any) -> dict[str, Any] | None:
    if isinstance(metadata, str):
        metadata = loads(metadata, {})
    if not isinstance(metadata, dict):
        return None
    snapshot = metadata.get(FACT_ENTITY_ATTRIBUTION_SNAPSHOT_KEY)
    if not isinstance(snapshot, dict) or snapshot.get("version") != (
        FACT_ENTITY_ATTRIBUTION_SNAPSHOT_VERSION
    ):
        return None
    return dict(snapshot)


def fact_with_entity_attribution_snapshot(
    fact: dict[str, Any],
    mentions: list[dict[str, Any]],
    existing_get: Callable[[str, Any], Any],
) -> dict[str, Any]:
    """Persist the normalized source attribution that generated entity links.

    Top-level entity mentions and legacy fallback fields are not fact columns,
    so without this additive metadata receipt their semantics disappear after
    application. Keep only resolution-relevant values; confidence, spans, and
    caller-supplied link rows remain outside semantic review identity.
    """

    existing_metadata = existing_get("metadata", {})
    if isinstance(existing_metadata, str):
        existing_metadata = loads(existing_metadata, {})
    existing_snapshot = fact_entity_attribution_snapshot(existing_metadata)
    incoming_metadata = fact.get("metadata")
    incoming_snapshot = fact_entity_attribution_snapshot(incoming_metadata)
    if not fact_entity_attribution_input_present(fact):
        if not isinstance(existing_snapshot, dict) or (
            fact_entity_attribution_snapshot_mentions(
                {FACT_ENTITY_ATTRIBUTION_SNAPSHOT_KEY: existing_snapshot}
            )
            is None
        ):
            return fact
        raw_metadata = fact.get("metadata") if "metadata" in fact else existing_metadata
        if isinstance(raw_metadata, str):
            raw_metadata = loads(raw_metadata, {})
        metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
        metadata[FACT_ENTITY_ATTRIBUTION_SNAPSHOT_KEY] = dict(existing_snapshot)
        return {**fact, "metadata": metadata}
    raw_metadata = fact.get("metadata") if "metadata" in fact else existing_metadata
    if isinstance(raw_metadata, str):
        raw_metadata = loads(raw_metadata, {})
    metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    snapshot_mentions_by_key: dict[str, dict[str, Any]] = {}
    for mention in mentions:
        is_primary = bool(mention.get("is_primary"))
        snapshot_mention = {
            key: value
            for key, value in {
                "surface": str(mention.get("surface") or "").strip(),
                "entity_identity": (
                    str(mention.get("entity_identity") or "").strip()
                    if is_primary
                    else ""
                ),
                "entity_type": mention.get("entity_type"),
                "mention_kind": mention.get("mention_kind"),
                "is_primary": is_primary,
            }.items()
            if value not in (None, "")
        }
        snapshot_mentions_by_key.setdefault(dumps(snapshot_mention), snapshot_mention)
    snapshot_mentions = sorted(
        snapshot_mentions_by_key.values(),
        key=lambda mention: (
            not bool(mention.get("is_primary")),
            dumps(mention),
        ),
    )
    if fact.get("entity_mentions") is not None:
        raw_top_level_mentions = fact.get("entity_mentions")
        metadata["model_entity_mentions"] = (
            [
                dict(mention)
                for mention in raw_top_level_mentions
                if isinstance(mention, dict)
            ]
            if isinstance(raw_top_level_mentions, list)
            else []
        )
    clears_attribution = fact_clears_entity_attribution(fact)
    entity_id = str(
        fact.get("entity_id")
        or ("" if clears_attribution else existing_get("entity_id", ""))
        or ""
    ).strip()
    if clears_attribution:
        # Presence matters to fact persistence: ``None`` prevents the
        # versioned-row merge from restoring the prior denormalized route.
        fact = {**fact, "entity_id": None}
    metadata[FACT_ENTITY_ATTRIBUTION_SNAPSHOT_KEY] = {
        "version": FACT_ENTITY_ATTRIBUTION_SNAPSHOT_VERSION,
        "mentions": snapshot_mentions,
        "entity_id_origin": entity_id_origin(
            fact,
            entity_id=entity_id,
            incoming_snapshot=incoming_snapshot,
            existing_snapshot=existing_snapshot,
        ),
        **({"entity_id": entity_id} if entity_id else {}),
    }
    return {**fact, "metadata": metadata}


def entity_id_origin(
    fact: dict[str, Any],
    *,
    entity_id: str,
    incoming_snapshot: dict[str, Any] | None,
    existing_snapshot: dict[str, Any] | None,
) -> str:
    """Classify the route ID using only provenance that survives round trips."""

    supplied_entity_id = str(fact.get("entity_id") or "").strip()
    if supplied_entity_id:
        if receipt_matches_entity_id(incoming_snapshot, supplied_entity_id):
            return str(incoming_snapshot.get("entity_id_origin"))
        return "explicit"
    if entity_id:
        if receipt_matches_entity_id(existing_snapshot, entity_id):
            return str(existing_snapshot.get("entity_id_origin"))
        return "unknown"
    return "derived"


def receipt_matches_entity_id(snapshot: dict[str, Any] | None, entity_id: str) -> bool:
    if snapshot is None:
        return False
    origin = str(snapshot.get("entity_id_origin") or "")
    return (
        origin in FACT_ENTITY_ID_ORIGINS
        and str(snapshot.get("entity_id") or "").strip() == entity_id
    )


def fact_with_resolved_entity_id_receipt(
    fact: dict[str, Any], entity_id: str | None
) -> dict[str, Any]:
    """Bind a prepared attribution receipt to the resolver's final route ID."""

    metadata = fact.get("metadata")
    if isinstance(metadata, str):
        metadata = loads(metadata, {})
    snapshot = fact_entity_attribution_snapshot(metadata)
    if snapshot is None or not isinstance(metadata, dict):
        return fact
    resolved_id = str(entity_id or "").strip()
    if resolved_id:
        snapshot["entity_id"] = resolved_id
    else:
        snapshot.pop("entity_id", None)
    metadata = dict(metadata)
    metadata[FACT_ENTITY_ATTRIBUTION_SNAPSHOT_KEY] = snapshot
    return {**fact, "metadata": metadata}


def normalize_fact_entity_mentions(raw_mentions: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_mentions, list):
        return []
    mentions: list[dict[str, Any]] = []
    primary_seen = False
    for raw in raw_mentions:
        if not isinstance(raw, dict):
            continue
        surface = str(
            raw.get("surface")
            or raw.get("mention")
            or raw.get("name")
            or raw.get("entity_key")
            or ""
        ).strip()
        if not surface:
            continue
        entity_type = normalize_entity_type(raw.get("entity_type") or raw.get("type"))
        mention_kind = normalize_mention_kind(
            raw.get("mention_kind") or raw.get("kind")
        )
        is_primary = bool(raw.get("is_primary")) and not primary_seen
        if is_primary:
            primary_seen = True
        mentions.append(
            {
                "surface": surface,
                "entity_identity": str(raw.get("entity_identity") or "").strip()
                or None,
                "entity_type": entity_type,
                "is_primary": is_primary,
                "mention_span": raw.get("mention_span")
                if isinstance(raw.get("mention_span"), dict)
                else None,
                "mention_kind": mention_kind,
                "confidence": optional_float(raw.get("confidence")),
            }
        )
    if mentions and not any(mention["is_primary"] for mention in mentions):
        mentions[0]["is_primary"] = True
    return mentions


def first_primary_entity_mention(
    mentions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not mentions:
        return None
    return next(
        (mention for mention in mentions if mention.get("is_primary")), mentions[0]
    )


def primary_entity_mention(fact: dict[str, Any]) -> str | None:
    metadata = fact.get("metadata")
    if isinstance(metadata, str):
        metadata = loads(metadata, {})
    if isinstance(metadata, dict):
        model_entity_key = str(metadata.get("model_entity_key") or "").strip()
        if model_entity_key:
            return model_entity_key
    for key in ("entity_mention", "entity_name"):
        value = str(fact.get(key) or "").strip()
        if value:
            return value
    return None
