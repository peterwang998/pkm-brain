from __future__ import annotations

from typing import Any, Callable

from .db import rows
from .fact_records import effective_revision_status
from .temporal import (
    TemporalRetrievalRequest,
    fact_matches_temporal_request,
    timeline_currentness,
    timeline_sort_key,
)


FACT_CANDIDATE_MULTIPLIER = 4
FACT_SEARCH_PAGE_MIN = 32
FACT_SEARCH_HARD_CAP = 512

FactConverter = Callable[..., dict[str, Any]]
FactScorer = Callable[[dict[str, Any], str, float], dict[str, Any] | None]
FactCutter = Callable[[list[dict[str, Any]], int], list[dict[str, Any]]]


def search_temporal_facts(
    conn: Any,
    *,
    fts_query: str,
    query: str,
    limit: int,
    request: TemporalRetrievalRequest,
    truth_confidence_floor: float,
    row_to_retrieval_fact: FactConverter,
    score_fact: FactScorer,
    cut_facts: FactCutter,
) -> list[dict[str, Any]]:
    """Search facts without letting ineligible FTS rows consume the pool."""

    if limit <= 0 or request.fail_closed:
        return []
    eligible_target = min(
        FACT_SEARCH_HARD_CAP,
        max(limit * FACT_CANDIDATE_MULTIPLIER, limit),
    )
    page_size = min(
        FACT_SEARCH_HARD_CAP,
        max(FACT_SEARCH_PAGE_MIN, eligible_target),
    )
    scanned = 0
    eligible: list[dict[str, Any]] = []
    eligible_ids: set[str] = set()
    eligible_lineages: set[str] = set()
    fts_scores: dict[str, float] = {}

    while scanned < FACT_SEARCH_HARD_CAP and not enough_eligible(
        eligible,
        eligible_lineages,
        eligible_target,
        request,
    ):
        requested = min(page_size, FACT_SEARCH_HARD_CAP - scanned)
        found = rows(
            conn,
            """
            SELECT retrieval_fts.target_id AS fact_id,
                   bm25(retrieval_fts) AS score
            FROM retrieval_fts
            JOIN facts ON facts.id = retrieval_fts.target_id
            WHERE retrieval_fts MATCH ?
              AND retrieval_fts.kind = 'fact'
              AND (
                ? = 0
                OR (
                  facts.knowledge_to IS NULL
                  AND facts.status != 'revision_closed'
                )
              )
            ORDER BY score, fact_id
            LIMIT ? OFFSET ?
            """,
            (
                fts_query,
                1 if request.known_as_of is None else 0,
                requested,
                scanned,
            ),
        )
        if not found:
            break
        scanned += len(found)
        fact_ids = [str(row["fact_id"]) for row in found if row["fact_id"]]
        page_scores = {
            str(row["fact_id"]): float(row["score"] or 0.0)
            for row in found
            if row["fact_id"]
        }
        fts_scores.update(page_scores)
        facts_by_id = load_facts(conn, fact_ids, truth_confidence_floor)
        for fact_id in fact_ids:
            if fact_id in eligible_ids or fact_id not in facts_by_id:
                continue
            fact = row_to_retrieval_fact(
                facts_by_id[fact_id],
                0.0,
                fts_score=page_scores.get(fact_id, 0.0),
            )
            if not fact_is_eligible(fact, request):
                continue
            decorate_temporal_match(fact, request)
            eligible.append(fact)
            eligible_ids.add(fact_id)
            eligible_lineages.add(fact_lineage_id(fact))
        if len(found) < requested:
            break

    if request.temporal_mode == "timeline":
        eligible = latest_revisions_by_lineage(eligible)

    scored = []
    for fact in eligible:
        fact_id = str(fact.get("id") or "")
        candidate = score_fact(fact, query, fts_scores.get(fact_id, 0.0))
        if candidate is not None:
            scored.append(candidate)
    scored.sort(key=relevance_sort_key)
    selected = cut_facts(scored, limit)
    if request.temporal_mode == "timeline":
        selected.sort(key=timeline_sort_key)
    attach_contested_facts(
        conn,
        selected,
        request=request,
        truth_confidence_floor=truth_confidence_floor,
        row_to_retrieval_fact=row_to_retrieval_fact,
        fts_scores=fts_scores,
    )
    return selected


def load_facts(
    conn: Any, fact_ids: list[str], truth_confidence_floor: float
) -> dict[str, Any]:
    if not fact_ids:
        return {}
    placeholders = ",".join("?" for _ in fact_ids)
    found = rows(
        conn,
        f"""
        SELECT *
        FROM facts
        WHERE id IN ({placeholders})
          AND COALESCE(truth_confidence, confidence, 0) >= ?
        """,
        [*fact_ids, truth_confidence_floor],
    )
    return {str(row["id"]): row for row in found}


def enough_eligible(
    eligible: list[dict[str, Any]],
    lineages: set[str],
    target: int,
    request: TemporalRetrievalRequest,
) -> bool:
    if request.temporal_mode == "timeline":
        return len(lineages) >= target
    return len(eligible) >= target


def fact_is_eligible(fact: dict[str, Any], request: TemporalRetrievalRequest) -> bool:
    # Closed revisions are evidence for an explicit knowledge clock only.
    # Valid-only and ordinary timeline views use the latest open assertion so
    # a later correction cannot resurrect an obsolete belief.
    if request.known_as_of is None and (
        fact.get("knowledge_to") is not None
        or str(fact.get("status") or "") == "revision_closed"
    ):
        return False
    match_fact = fact
    if request.historical and str(fact.get("status") or "") == "revision_closed":
        status = effective_revision_status(fact)
        if status not in {"active", "conflicted", "superseded"}:
            return False
        match_fact = {**fact, "status": status}
    return fact_matches_temporal_request(match_fact, request)


def decorate_temporal_match(
    fact: dict[str, Any], request: TemporalRetrievalRequest
) -> None:
    status = effective_revision_status(fact)
    fact["authoritative"] = status == "active"
    fact["contested"] = status == "conflicted"
    fact["currentness_reason"] = timeline_currentness(
        {**fact, "status": status}
    )
    fact["temporal_match"] = {
        "bitemporal": "bitemporal",
        "current": "current_status",
        "known": "known_as_of",
        "timeline": "timeline",
        "valid": "valid_as_of",
    }[request.temporal_mode]


def fact_lineage_id(fact: dict[str, Any]) -> str:
    return str(fact.get("assertion_lineage_id") or fact.get("id") or "")


def latest_revisions_by_lineage(
    facts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for fact in facts:
        lineage_id = fact_lineage_id(fact)
        existing = latest.get(lineage_id)
        if existing is None or revision_sort_key(fact) > revision_sort_key(existing):
            latest[lineage_id] = fact
    return list(latest.values())


def revision_sort_key(fact: dict[str, Any]) -> tuple[int, str, str]:
    try:
        revision_number = int(fact.get("revision_number") or 1)
    except (TypeError, ValueError):
        revision_number = 1
    return (
        revision_number,
        str(fact.get("created_at") or ""),
        str(fact.get("id") or ""),
    )


def relevance_sort_key(fact: dict[str, Any]) -> tuple[float, float, str]:
    return (
        -float(fact.get("retrieval_score") or 0.0),
        float(fact.get("fts_score") or 0.0),
        str(fact.get("id") or ""),
    )


def attach_contested_facts(
    conn: Any,
    selected: list[dict[str, Any]],
    *,
    request: TemporalRetrievalRequest,
    truth_confidence_floor: float,
    row_to_retrieval_fact: FactConverter,
    fts_scores: dict[str, float],
) -> None:
    group_ids = sorted(
        {
            str(fact.get("conflict_group_id") or "")
            for fact in selected
            if effective_revision_status(fact) == "conflicted"
            and fact.get("conflict_group_id")
        }
    )
    if not group_ids:
        return
    placeholders = ",".join("?" for _ in group_ids)
    contested: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(
        f"""
        SELECT *
        FROM facts
        WHERE conflict_group_id IN ({placeholders})
          AND (
            status = 'conflicted'
            OR (status = 'revision_closed' AND revision_status = 'conflicted')
          )
          AND COALESCE(truth_confidence, confidence, 0) >= ?
        ORDER BY created_at, id
        """,
        [*group_ids, truth_confidence_floor],
    ):
        fact_id = str(row["id"])
        fact = row_to_retrieval_fact(
            row,
            0.0,
            fts_score=fts_scores.get(fact_id, 0.0),
        )
        if not fact_is_eligible(fact, request):
            continue
        decorate_temporal_match(fact, request)
        contested.setdefault(str(row["conflict_group_id"]), []).append(fact)
    if request.temporal_mode == "timeline":
        contested = {
            group_id: latest_revisions_by_lineage(facts)
            for group_id, facts in contested.items()
        }
    for fact in selected:
        group_id = str(fact.get("conflict_group_id") or "")
        if group_id in contested:
            fact["contested_facts"] = contested[group_id]
