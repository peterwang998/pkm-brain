from __future__ import annotations

from typing import Any

from .db import dumps, loads
from .event_routing import (
    candidate_source_ids,
    candidate_routing,
    event_route_targets_for_pages,
    extraction_event_candidate,
    guard_event_candidate_routes,
)


def apply_event_fact_action_precheck(conn: Any, action: dict[str, Any]) -> None:
    """Persist the event-route guard fields needed before policy evaluation."""

    if str(action.get("action_type") or "") != "fact_upsert":
        return
    payload = _action_payload(action)
    fact = payload.get("fact") if isinstance(payload.get("fact"), dict) else None
    if not isinstance(fact, dict):
        return
    if not extraction_event_candidate(fact):
        return
    siblings = _accepted_source_sibling_facts(conn, action, fact)
    pages = [str(item.get("page_hint") or "") for item in [*siblings, fact]]
    targets = event_route_targets_for_pages(conn, pages)
    guarded = guard_event_candidate_routes(
        [fact], targets, accepted_anchor_candidates=siblings
    )[0]
    current_page = str(fact.get("page_hint") or "")
    guarded_page = str(guarded.get("page_hint") or "")
    if guarded_page and guarded_page != current_page:
        targets = event_route_targets_for_pages(conn, [*pages, guarded_page])
        guarded = guard_event_candidate_routes(
            [fact], targets, accepted_anchor_candidates=siblings
        )[0]
        guarded_page = str(guarded.get("page_hint") or "")
    routing = candidate_routing(guarded)
    held = routing.get("route_destination_valid") is False
    identity_guard_present = bool(
        routing.get("event_temporal_identity_guard")
        or routing.get("person_page_identity_guard")
    )

    evidence = dict(action.get("evidence_json") or {})
    evidence["payload"] = {**payload, "fact": guarded}
    features = dict(action.get("action_features") or {})
    features.update(
        {
            "route_destination_valid": not held,
            "route_target_exists": routing.get("route_target_exists"),
            "route_resolution": routing.get("route_resolution"),
            "route_review_reason": routing.get("route_review_reason"),
            "route_identity_guard": ("held" if held else "passed")
            if identity_guard_present
            else None,
            "event_route_temporal_identity_guard": (
                "held" if held else "passed"
            )
            if routing.get("event_temporal_identity_guard")
            else None,
            "person_page_identity_guard": routing.get(
                "person_page_identity_guard"
            ),
            "person_page_identity_sensitive": bool(
                routing.get("person_page_identity_sensitive")
            ),
            "target_page_paths": [guarded_page] if guarded_page else [],
        }
    )
    if held:
        features.update(
            {
                "clean_fact_upsert": False,
                "resolver_precheck": "residue",
                "simple_decision": "residue",
                "residue_kind": "route_identity_review",
                "risk_tier": "high",
            }
        )
    target_page_paths = [guarded_page] if guarded_page else []
    risk_tier = "high" if held else str(action.get("risk_tier") or "high")
    conn.execute(
        """
        UPDATE cos_actions
        SET target_page_paths = ?, action_features = ?, risk_tier = ?,
            evidence_json = ?
        WHERE id = ?
        """,
        (
            dumps(target_page_paths),
            dumps(features),
            risk_tier,
            dumps(evidence),
            action["id"],
        ),
    )
def _action_payload(action: dict[str, Any]) -> dict[str, Any]:
    evidence = action.get("evidence_json") or {}
    payload = evidence.get("payload") or {}
    return payload if isinstance(payload, dict) else {}


def _accepted_source_sibling_facts(
    conn: Any, action: dict[str, Any], fact: dict[str, Any]
) -> list[dict[str, Any]]:
    """Load only accepted same-run facts sharing trusted source identifiers.

    Proposed, held, rejected, or uncriticized siblings cannot establish an
    occurrence for another action.  Requiring both terminal application and
    critic agreement makes this boundary independent of action ordering.
    """

    run_id = str(action.get("run_id") or "").strip()
    source_ids = candidate_source_ids(fact)
    if not run_id or not source_ids:
        return []
    output: list[dict[str, Any]] = []
    for row in conn.execute(
        """
        SELECT evidence_json
        FROM cos_actions
        WHERE run_id = ?
          AND action_type = 'fact_upsert'
          AND id != ?
          AND status IN ('applied', 'auto_applied')
          AND applied_at IS NOT NULL
          AND critic_decision = 'agree'
        ORDER BY created_at, id
        LIMIT 200
        """,
        (run_id, action["id"]),
    ):
        evidence = loads(row["evidence_json"], {})
        payload = evidence.get("payload") if isinstance(evidence, dict) else None
        sibling = payload.get("fact") if isinstance(payload, dict) else None
        if not isinstance(sibling, dict):
            continue
        if source_ids & candidate_source_ids(sibling):
            output.append(sibling)
    return output
