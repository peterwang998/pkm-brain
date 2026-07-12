from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .db import loads


DEFAULT_ROUTE_PRIOR_CONFIDENCE = 0.75
NON_COHERENT_PAGE_HINTS = {"concepts/extracted-facts.md"}
NON_COHERENT_PREFIXES = ("inbox/", "references/", "wiki/references/")


def fact_document_id(fact: dict[str, Any]) -> str:
    metadata = fact.get("metadata")
    if isinstance(metadata, str):
        metadata = loads(metadata, {})
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get("document_id") or "").strip()


def route_priors_from_facts(
    facts: Iterable[dict[str, Any]],
    *,
    min_confidence: float = DEFAULT_ROUTE_PRIOR_CONFIDENCE,
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"fact_ids": [], "confidences": []}
    )
    for fact in facts:
        page_hint = str(fact.get("page_hint") or "").strip()
        confidence = optional_float(fact.get("routing_confidence"))
        if (
            not coherent_page_hint(page_hint)
            or confidence is None
            or confidence < min_confidence
        ):
            continue
        fact_id = str(fact.get("id") or fact.get("fact_id") or "").strip()
        if fact_id and fact_id in buckets[page_hint]["fact_ids"]:
            continue
        if fact_id:
            buckets[page_hint]["fact_ids"].append(fact_id)
        buckets[page_hint]["confidences"].append(confidence)
    total = sum(len(bucket["confidences"]) for bucket in buckets.values())
    if not total:
        return []
    priors = []
    for page_hint, bucket in buckets.items():
        count = len(bucket["confidences"])
        priors.append(
            {
                "page_hint": page_hint,
                "fact_count": count,
                "fact_ids": bucket["fact_ids"][:10],
                "average_confidence": round(sum(bucket["confidences"]) / count, 4),
                "share": round(count / total, 4),
            }
        )
    priors.sort(
        key=lambda prior: (
            int(prior["fact_count"]),
            float(prior["average_confidence"]),
            str(prior["page_hint"]),
        ),
        reverse=True,
    )
    return priors


def load_document_route_priors(
    conn: Any,
    document_id: str,
    *,
    min_confidence: float = DEFAULT_ROUTE_PRIOR_CONFIDENCE,
) -> list[dict[str, Any]]:
    normalized = str(document_id or "").strip()
    if not normalized:
        return []
    facts = [
        dict(row)
        for row in conn.execute(
            """
            SELECT DISTINCT f.id, f.page_hint, f.routing_confidence
            FROM facts f
            WHERE f.status = 'active'
              AND (
                json_extract(
                  CASE WHEN json_valid(f.metadata) THEN f.metadata ELSE '{}'
                  END,
                  '$.document_id'
                ) = ?
                OR EXISTS (
                  SELECT 1
                  FROM json_each(
                    CASE WHEN json_valid(f.source_ids) THEN f.source_ids ELSE '[]' END
                  ) source
                  JOIN chunks c ON source.value = ('chunk:' || c.id)
                  WHERE c.document_id = ?
                )
              )
            """,
            (normalized, normalized),
        )
    ]
    return route_priors_from_facts(facts, min_confidence=min_confidence)


def coherence_bonus(prior: dict[str, Any]) -> float:
    count = max(0, int(prior.get("fact_count") or 0))
    share = max(0.0, min(1.0, float(prior.get("share") or 0.0)))
    if count < 2 or share < 0.5:
        return 0.0
    return round(min(6.0, 2.0 + (count * share)), 4)


def strong_document_prior(prior: dict[str, Any]) -> bool:
    return (
        int(prior.get("fact_count") or 0) >= 3
        and float(prior.get("share") or 0.0) >= 0.75
    )


def coherent_page_hint(page_hint: str) -> bool:
    normalized = str(page_hint or "").strip()
    return bool(
        normalized
        and normalized not in NON_COHERENT_PAGE_HINTS
        and not normalized.startswith(NON_COHERENT_PREFIXES)
    )


def optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
