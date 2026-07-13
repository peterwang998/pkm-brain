from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from typing import Any

from .cos_actions import mark_action_residue, propose_action, row_to_action
from .db import connection, dumps, loads
from .extraction import (
    decide_policy_actions,
    default_critic_review_config,
    find_exact_duplicate_fact,
    load_extraction_route_targets,
    merge_candidate_into_existing_fact,
    reroute_unrouted_candidate,
)
from .llm import LLMProvider
from .paths import BrainPaths
from .routing_coherence import fact_document_id, load_document_route_priors
from .unrouted_resolution import (
    candidate_requires_route_resolution,
    candidate_route_metadata,
    resolve_unrouted_candidate_routes,
)
from .util import now_iso
from .wiki_facts import row_to_fact


ACTIVE_QUESTION_STATUSES = {"open", "needs_human"}
APPLIED_ACTION_STATUSES = {"applied", "auto_applied"}


def reconcile_unrouted_inbox_batches(
    paths: BrainPaths,
    *,
    dry_run: bool = True,
    limit: int | None = None,
    min_score: float = 8.0,
    min_overlap: int = 2,
    critic_review: dict[str, Any] | None = None,
    llm_provider: LLMProvider | None = None,
) -> dict[str, Any]:
    batches, items, load_failures = load_unrouted_batch_items(paths, limit=limit)
    route_targets = load_extraction_route_targets(paths)
    routed, unresolved = resolve_batch_routes(
        paths,
        items,
        route_targets,
        min_score=min_score,
        min_overlap=min_overlap,
        llm_provider=llm_provider,
    )
    base = {
        "batch_question_count": len(batches),
        "fact_count": len(items),
        "routable_count": len(routed),
        "requires_human_count": len(unresolved),
        "load_failure_count": len(load_failures),
        "route_resolution_counts": dict(
            sorted(
                Counter(
                    str(candidate_route_metadata(item["candidate"]).get("route_resolution") or "deterministic")
                    for item in routed
                ).items()
            )
        ),
        "human_resolution_counts": dict(
            sorted(
                Counter(
                    str(candidate_route_metadata(item["candidate"]).get("route_resolution") or "unknown")
                    for item in unresolved
                ).items()
            )
        ),
        "preview": [batch_route_preview(item) for item in routed[:25]],
        "requires_human_preview": [batch_route_preview(item) for item in unresolved[:25]],
        "load_failures": load_failures[:25],
    }
    if dry_run:
        return {"status": "dry_run", **base}
    if base["human_resolution_counts"].get("resolver_no_decision"):
        return {
            "status": "resolver_output_incomplete",
            **base,
            "error": (
                "No changes were applied because the routing resolver omitted one or "
                "more decisions after bounded retries."
            ),
        }

    proposed = [propose_batch_route_action(paths, item) for item in routed]
    review = critic_review or {
        **default_critic_review_config(),
        "disagreement_mode": "reject",
    }
    decided = decide_policy_actions(
        paths,
        [str(action["id"]) for action in proposed],
        critic_review=review,
    )
    residue: list[dict[str, Any]] = []
    for item in unresolved:
        residue.append(
            create_batch_residue(
                paths,
                item,
                kind="unrouted_fact",
                reason="No canonical destination was sufficiently clear after route resolution.",
            )
        )
    for item, action in zip(routed, decided):
        if str(action.get("status") or "") in APPLIED_ACTION_STATUSES:
            continue
        if str(action.get("status") or "") in {"needs_human", "proposed"}:
            continue
        residue.append(
            create_batch_residue(
                paths,
                item,
                kind="weak_evidence_fact",
                reason=(
                    "The automatic rehome was not accepted by policy or critic review; "
                    "review the fact and its destination together."
                ),
            )
        )
    close_unrouted_batch_questions(
        paths,
        batches,
        items=items,
        routed=routed,
        unresolved=unresolved,
        decided=decided,
        residue=residue,
        load_failures=load_failures,
    )
    return {
        "status": "applied",
        **base,
        "applied_route_count": sum(
            str(action.get("status") or "") in APPLIED_ACTION_STATUSES
            for action in decided
        ),
        "route_action_status_counts": dict(
            sorted(Counter(str(action.get("status") or "unknown") for action in decided).items())
        ),
        "individual_residue_count": len(residue),
        "policy_review_count": sum(
            str(action.get("status") or "") in {"needs_human", "proposed"}
            for action in decided
        ),
    }


def load_unrouted_batch_items(
    paths: BrainPaths, *, limit: int | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    query = """
        SELECT * FROM open_questions
        WHERE kind = 'unrouted_inbox_batch'
          AND status IN ('open', 'needs_human')
        ORDER BY created_at, id
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (max(1, int(limit)),)
    with connection(paths.sqlite_path) as conn:
        batches = [dict(row) for row in conn.execute(query, params)]
        action_ids = [
            str(action_id)
            for batch in batches
            for action_id in (loads(batch.get("context"), {}).get("new_action_ids") or [])
            if action_id
        ]
        actions: dict[str, dict[str, Any]] = {}
        if action_ids:
            placeholders = ",".join("?" for _ in action_ids)
            actions = {
                str(row["id"]): row_to_action(row)
                for row in conn.execute(
                    f"SELECT * FROM cos_actions WHERE id IN ({placeholders})", action_ids
                )
            }
        fact_ids = [
            str(fact.get("id"))
            for action in actions.values()
            if isinstance(
                fact := ((action.get("evidence_json") or {}).get("payload") or {}).get("fact"),
                dict,
            )
            and fact.get("id")
        ]
        facts: dict[str, dict[str, Any]] = {}
        if fact_ids:
            placeholders = ",".join("?" for _ in fact_ids)
            facts = {
                str(row["id"]): row_to_fact(row)
                for row in conn.execute(
                    f"SELECT * FROM facts WHERE id IN ({placeholders})", fact_ids
                )
            }

    items: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for batch in batches:
        context = loads(batch.get("context"), {})
        for action_id in context.get("new_action_ids") or []:
            action = actions.get(str(action_id))
            if action is None:
                failures.append(
                    {"batch_question_id": batch["id"], "action_id": action_id, "reason": "missing_action"}
                )
                continue
            payload_fact = ((action.get("evidence_json") or {}).get("payload") or {}).get("fact")
            fact_id = str(payload_fact.get("id") or "") if isinstance(payload_fact, dict) else ""
            candidate = facts.get(fact_id) or payload_fact
            if not isinstance(candidate, dict):
                failures.append(
                    {"batch_question_id": batch["id"], "action_id": action_id, "reason": "missing_fact"}
                )
                continue
            items.append(
                {
                    "batch_question_id": str(batch["id"]),
                    "old_action_id": str(action_id),
                    "candidate": candidate,
                }
            )
    return batches, items, failures


def resolve_batch_routes(
    paths: BrainPaths,
    items: list[dict[str, Any]],
    route_targets: dict[str, dict[str, Any]],
    *,
    min_score: float,
    min_overlap: int,
    llm_provider: LLMProvider | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    prepared: list[dict[str, Any]] = []
    prior_cache: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        candidate = item["candidate"]
        document_id = fact_document_id(candidate)
        if document_id not in prior_cache:
            with connection(paths.sqlite_path) as conn:
                prior_cache[document_id] = load_document_route_priors(conn, document_id)
        suggestion = reroute_unrouted_candidate(
            candidate,
            route_targets,
            min_score=min_score,
            min_overlap=min_overlap,
            document_priors=prior_cache[document_id],
        )
        prepared.append(
            {
                **item,
                "candidate": candidate_with_route_suggestion(candidate, suggestion),
            }
        )
    llm_candidates = resolve_unrouted_candidate_routes(
        paths,
        [item["candidate"] for item in prepared],
        route_targets,
        llm_provider=llm_provider,
    )
    routed: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for item, candidate in zip(prepared, llm_candidates):
        target = unresolved if candidate_requires_route_resolution(candidate) else routed
        target.append({**item, "candidate": candidate})
    return routed, unresolved


def candidate_with_route_suggestion(
    candidate: dict[str, Any], suggestion: dict[str, Any] | None
) -> dict[str, Any]:
    prepared = deepcopy(candidate)
    if suggestion is None:
        return prepared
    metadata = dict(prepared.get("metadata") or {})
    routing = dict(metadata.get("routing") or {})
    suggested_routing = candidate_route_metadata(suggestion)
    routing["deterministic_route_suggestion"] = {
        "page_hint": suggestion.get("page_hint"),
        "score": suggested_routing.get("reclaim_route_score"),
        "matching_terms": suggested_routing.get("reclaim_route_overlap") or [],
        "source": suggested_routing.get("reclaim_route_source"),
        "same_source_fact_count": suggested_routing.get(
            "document_coherence_fact_count", 0
        ),
        "same_source_share": suggested_routing.get("document_coherence_share", 0.0),
    }
    metadata["routing"] = routing
    prepared["metadata"] = metadata
    return prepared


def propose_batch_route_action(paths: BrainPaths, item: dict[str, Any]) -> dict[str, Any]:
    candidate = item["candidate"]
    fact_id = str(candidate.get("id") or "")
    page_hint = str(candidate.get("page_hint") or "")
    confidence = route_action_confidence(candidate)
    duplicate = find_exact_duplicate_fact(paths, candidate)
    common_features = {
        "candidate_signal": "legacy_unrouted_batch_reconciliation",
        "candidate_key": f"legacy_batch_route:{fact_id}:{page_hint}",
        "route_resolution_validated": True,
        "route_resolution": candidate_route_metadata(candidate).get("route_resolution"),
        "reversible": True,
        "truth_mutation": False,
        "affected_fact_count": 1,
        "confidence": confidence,
        "legacy_batch_question_id": item["batch_question_id"],
        "legacy_action_id": item["old_action_id"],
    }
    if duplicate is not None and str(duplicate.get("id") or "") != fact_id:
        keeper = merge_candidate_into_existing_fact(duplicate, candidate)
        return propose_action(
            paths,
            "fact_merge",
            action_payload={"keeper_fact": keeper, "superseded_fact_ids": [fact_id]},
            action_features={**common_features, "relation": "exact_duplicate"},
            target_fact_ids=[str(keeper["id"]), fact_id],
            target_page_paths=[page_hint],
            proposed_by="unrouted_batch_reconciliation_v2",
            confidence=1.0,
            risk_tier="low",
        )
    return propose_action(
        paths,
        "rehome_fact",
        action_payload={
            "fact_id": fact_id,
            "page_hint": page_hint,
            "entity_key": candidate.get("entity_key"),
            "section_hint": candidate.get("section_hint") or "Summary",
            "metadata": candidate.get("metadata") or {},
            "fact": candidate,
        },
        action_features=common_features,
        target_fact_ids=[fact_id],
        target_page_paths=[page_hint],
        proposed_by="unrouted_batch_reconciliation_v2",
        confidence=confidence,
        risk_tier="low",
    )


def route_action_confidence(candidate: dict[str, Any]) -> float:
    routing = candidate_route_metadata(candidate)
    resolver = routing.get("route_resolver_confidence")
    if resolver is not None:
        return max(0.0, min(1.0, float(resolver)))
    return 0.9


def create_batch_residue(
    paths: BrainPaths,
    item: dict[str, Any],
    *,
    kind: str,
    reason: str,
) -> dict[str, Any]:
    candidate = item["candidate"]
    action = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": candidate},
        action_features={
            "candidate_signal": "legacy_unrouted_batch_reconciliation",
            "candidate_key": f"legacy_batch_residue:{candidate.get('id')}:{kind}",
            "clean_fact_upsert": False,
            "residue_kind": kind,
            "reversible": True,
            "truth_mutation": False,
            "affected_fact_count": 1,
            "legacy_batch_question_id": item["batch_question_id"],
            "legacy_action_id": item["old_action_id"],
        },
        target_fact_ids=[str(candidate.get("id") or "")],
        target_page_paths=[str(candidate.get("page_hint") or "")],
        proposed_by="unrouted_batch_reconciliation_v2",
        confidence=candidate.get("truth_confidence") or candidate.get("confidence"),
        risk_tier="high",
    )
    return mark_action_residue(paths, str(action["id"]), kind=kind, reason=reason)


def close_unrouted_batch_questions(
    paths: BrainPaths,
    batches: list[dict[str, Any]],
    *,
    items: list[dict[str, Any]],
    routed: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
    decided: list[dict[str, Any]],
    residue: list[dict[str, Any]],
    load_failures: list[dict[str, Any]],
) -> None:
    timestamp = now_iso()
    facts_by_batch: dict[str, int] = Counter(item["batch_question_id"] for item in items)
    routed_by_batch: dict[str, int] = Counter(item["batch_question_id"] for item in routed)
    unresolved_by_batch: dict[str, int] = Counter(
        item["batch_question_id"] for item in unresolved
    )
    failures_by_batch: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for failure in load_failures:
        failures_by_batch[str(failure["batch_question_id"])].append(failure)
    action_ids_by_batch: dict[str, list[str]] = defaultdict(list)
    for item, action in zip(routed, decided):
        action_ids_by_batch[item["batch_question_id"]].append(str(action["id"]))
    residue_ids_by_batch: dict[str, list[str]] = defaultdict(list)
    for action in residue:
        features = action.get("action_features") or {}
        residue_ids_by_batch[str(features.get("legacy_batch_question_id") or "")].append(
            str(action["id"])
        )
    with connection(paths.sqlite_path) as conn:
        for batch in batches:
            batch_id = str(batch["id"])
            answer = {
                "resolution": "unrouted_batch_reconciliation_v2",
                "fact_count": facts_by_batch.get(batch_id, 0),
                "routed_count": routed_by_batch.get(batch_id, 0),
                "requires_human_count": unresolved_by_batch.get(batch_id, 0),
                "route_action_ids": action_ids_by_batch.get(batch_id, []),
                "individual_residue_action_ids": residue_ids_by_batch.get(batch_id, []),
                "load_failures": failures_by_batch.get(batch_id, []),
            }
            conn.execute(
                """
                UPDATE open_questions
                SET status = 'auto_resolved', answer = ?, answered_at = ?,
                    decided_by = 'unrouted_batch_reconciliation_v2'
                WHERE id = ?
                """,
                (dumps(answer), timestamp, batch_id),
            )


def batch_route_preview(item: dict[str, Any]) -> dict[str, Any]:
    candidate = item["candidate"]
    routing = candidate_route_metadata(candidate)
    return {
        "batch_question_id": item["batch_question_id"],
        "fact_id": candidate.get("id"),
        "statement": str(candidate.get("statement") or "")[:220],
        "page_hint": candidate.get("page_hint"),
        "route_resolution": routing.get("route_resolution"),
        "route_confidence": routing.get("route_resolver_confidence")
        or routing.get("reclaim_route_score"),
        "proposed_page_hint": routing.get("route_resolver_proposed_page_hint"),
        "route_rationale": routing.get("route_resolver_rationale"),
    }
