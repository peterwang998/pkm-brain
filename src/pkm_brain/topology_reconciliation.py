from __future__ import annotations

from collections import Counter
from typing import Any

from .contracts import active_page_contracts
from .cos_actions import get_action, row_to_action
from .cos_policy import evaluate_policy
from .curation_settings import load_curation_settings
from .db import connection, dumps
from .gardener import (
    apply_gardener_judgment,
    deterministic_entity_candidates,
    deterministic_topology_candidates,
    enrich_gardener_pages,
    gardener_action_spec,
    prioritize_topology_candidates,
)
from .llm import LLMProvider
from .llm_usage import llm_usage_summary
from .paths import BrainPaths
from .policy_reconciliation import redecide_policy_actions
from .util import new_id, now_iso
from .wiki_facts import managed_fact_page_summaries


RECONCILABLE_TOPOLOGY_ACTION_TYPES = {"entity_merge", "page_merge", "page_split"}
RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


def reconcile_topology_proposals(
    paths: BrainPaths,
    *,
    dry_run: bool = True,
    critic_review: dict[str, Any] | None = None,
    gardener_llm_provider: LLMProvider | None = None,
    critic_llm_provider: LLMProvider | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    context = current_topology_context(paths)
    groups = load_open_topology_groups(paths)
    current_candidates = context["candidates"]
    admitted = [group for group in groups if group["candidate_key"] in current_candidates]
    stale = [group for group in groups if group["candidate_key"] not in current_candidates]
    admitted_candidates = [
        current_candidates[group["candidate_key"]] for group in admitted
    ]
    _, arbitration = prioritize_topology_candidates(admitted_candidates)
    base = topology_reconciliation_report(
        context,
        groups,
        admitted,
        stale,
        arbitration=arbitration,
    )
    if dry_run:
        return {"status": "dry_run", **base}
    active_run_id = run_id or new_id("topology_reconciliation")

    closed_counts: Counter[str] = Counter()
    for group in stale:
        close_topology_group(
            paths,
            group,
            outcome="no_longer_admitted",
            reason=current_admission_rejection_reason(group, context["settings"]),
        )
        closed_counts[f"stale_{group['action_type']}"] += 1

    admitted_by_key = {group["candidate_key"]: group for group in admitted}
    merge_candidates = [
        current_candidates[group["candidate_key"]]
        for group in admitted
        if group["action_type"] in {"entity_merge", "page_merge"}
    ]
    merge_phase = reconcile_candidate_phase(
        paths,
        merge_candidates,
        admitted_by_key,
        context=context,
        critic_review=critic_review,
        gardener_llm_provider=gardener_llm_provider,
        critic_llm_provider=critic_llm_provider,
        run_id=active_run_id,
        phase="merge",
        apply_arbitration=True,
    )
    closed_counts.update(merge_phase["closed_counts"])

    reserved_merge_pages = {
        str(page)
        for candidate in merge_phase["selected_candidates"]
        if candidate.get("action_type") == "page_merge"
        for page in candidate.get("page_hints") or []
    }
    refreshed_context = current_topology_context(paths)
    split_candidates: list[dict[str, Any]] = []
    for group in admitted:
        if group["action_type"] != "page_split":
            continue
        key = group["candidate_key"]
        candidate = refreshed_context["candidates"].get(key)
        candidate_pages = {
            str(page)
            for page in (candidate or {}).get("page_hints") or group["page_hints"]
        }
        if candidate is None:
            close_topology_group(
                paths,
                group,
                outcome="no_longer_admitted_after_merges",
                reason="Current page evidence no longer satisfies the split threshold after merge reconciliation.",
            )
            closed_counts["stale_after_merge_page_split"] += 1
            continue
        if candidate_pages & reserved_merge_pages:
            close_topology_group(
                paths,
                group,
                outcome="merge_preferred",
                reason="A current merge candidate owns this page; merge is preferred over splitting the same page.",
            )
            closed_counts["merge_preferred_page_split"] += 1
            continue
        split_candidates.append(candidate)

    split_phase = reconcile_candidate_phase(
        paths,
        split_candidates,
        admitted_by_key,
        context=refreshed_context,
        critic_review=critic_review,
        gardener_llm_provider=gardener_llm_provider,
        critic_llm_provider=critic_llm_provider,
        run_id=active_run_id,
        phase="split",
        apply_arbitration=False,
    )
    closed_counts.update(split_phase["closed_counts"])

    remaining = load_open_topology_groups(paths)
    all_results = [*merge_phase["results"], *split_phase["results"]]
    all_failures = [*merge_phase["failures"], *split_phase["failures"]]
    result = {
        "status": "applied",
        "run_id": active_run_id,
        **base,
        "closed_group_counts": dict(sorted(closed_counts.items())),
        "gardener_review": {
            "merge": merge_phase["gardener_summary"],
            "split": split_phase["gardener_summary"],
        },
        "policy_result_status_counts": dict(
            sorted(Counter(str(item.get("status") or "unknown") for item in all_results).items())
        ),
        "policy_result_autonomy_counts": dict(
            sorted(
                Counter(
                    str(item.get("autonomy_level") or "unknown")
                    for item in all_results
                ).items()
            )
        ),
        "failure_count": len(all_failures),
        "failures": all_failures[:25],
        "remaining_open_unique_count": len(remaining),
        "remaining_open_by_type": count_groups_by_type(remaining),
    }
    result["llm_usage"] = llm_usage_summary(
        paths, cycle_id=active_run_id, limit=1
    )
    return result


def current_topology_context(paths: BrainPaths) -> dict[str, Any]:
    settings = load_curation_settings(paths)
    pages = enrich_gardener_pages(paths, managed_fact_page_summaries(paths))
    contracts = {
        str(contract["page_hint"]): contract for contract in active_page_contracts(paths)
    }
    candidates = deterministic_topology_candidates(
        pages,
        contracts,
        suppressed_candidate_keys=set(),
        merge_aggressiveness=float(settings["merge_aggressiveness"]),
        split_aggressiveness=float(settings["split_aggressiveness"]),
        topology_review_threshold=int(settings["topology_review_threshold"]),
    )
    candidates.extend(
        deterministic_entity_candidates(
            paths,
            suppressed_candidate_keys=set(),
            merge_aggressiveness=float(settings["merge_aggressiveness"]),
            topology_review_threshold=int(settings["topology_review_threshold"]),
        )
    )
    return {
        "settings": settings,
        "pages": pages,
        "contracts": contracts,
        "candidates": {
            str(candidate["candidate_key"]): candidate
            for candidate in candidates
            if str(candidate.get("action_type") or "")
            in RECONCILABLE_TOPOLOGY_ACTION_TYPES
        },
    }


def load_open_topology_groups(paths: BrainPaths) -> list[dict[str, Any]]:
    with connection(paths.sqlite_path) as conn:
        actions = [
            row_to_action(row)
            for row in conn.execute(
                """
                SELECT *
                FROM cos_actions
                WHERE status IN ('proposed', 'needs_human')
                  AND action_type IN ('entity_merge', 'page_merge', 'page_split')
                ORDER BY created_at DESC, id DESC
                """
            )
        ]
    groups: dict[str, dict[str, Any]] = {}
    for action in actions:
        features = action.get("action_features") or {}
        key = str(features.get("candidate_key") or action["id"])
        group = groups.setdefault(
            key,
            {
                "candidate_key": key,
                "action_type": str(action["action_type"]),
                "actions": [],
                "page_hints": list(action.get("target_page_paths") or []),
            },
        )
        group["actions"].append(action)
    return list(groups.values())


def reconcile_candidate_phase(
    paths: BrainPaths,
    candidates: list[dict[str, Any]],
    groups_by_key: dict[str, dict[str, Any]],
    *,
    context: dict[str, Any],
    critic_review: dict[str, Any] | None,
    gardener_llm_provider: LLMProvider | None,
    critic_llm_provider: LLMProvider | None,
    run_id: str,
    phase: str,
    apply_arbitration: bool,
) -> dict[str, Any]:
    if not candidates:
        return empty_phase_result()
    baseline_risk = {
        str(candidate["candidate_key"]): str(candidate.get("risk_tier") or "medium")
        for candidate in candidates
    }
    judged = apply_gardener_judgment(
        candidates,
        context["pages"],
        context["contracts"],
        paths=paths,
        llm_provider=gardener_llm_provider,
        provider=None,
        usage_cycle_id=run_id,
    )
    kept = [
        preserve_deterministic_risk(candidate, baseline_risk[str(candidate["candidate_key"])])
        for candidate in judged["candidates"]
    ]
    kept_keys = {str(candidate["candidate_key"]) for candidate in kept}
    dropped = {
        str(item.get("candidate_key") or ""): item
        for item in judged["summary"].get("dropped") or []
    }
    closed_counts: Counter[str] = Counter()
    for candidate in candidates:
        key = str(candidate["candidate_key"])
        if key in kept_keys:
            continue
        judgment = dropped.get(key) or {}
        rationale = str(
            (judgment.get("llm_judgment") or {}).get("rationale")
            or "Gardener review dropped the candidate."
        )
        close_topology_group(
            paths,
            groups_by_key[key],
            outcome=f"gardener_dropped_{phase}",
            reason=rationale,
        )
        closed_counts[f"gardener_dropped_{candidate['action_type']}"] += 1

    selected = kept
    if apply_arbitration:
        selected, arbitration = prioritize_topology_candidates(kept)
        selected_keys = {str(candidate["candidate_key"]) for candidate in selected}
        for candidate in kept:
            key = str(candidate["candidate_key"])
            if key in selected_keys:
                continue
            close_topology_group(
                paths,
                groups_by_key[key],
                outcome="overlap_suppressed",
                reason="A higher-ranked merge candidate already uses one of the same pages or entities.",
            )
            closed_counts[f"overlap_suppressed_{candidate['action_type']}"] += 1
    else:
        arbitration = {
            "input_count": len(kept),
            "selected_count": len(kept),
            "suppressed_count": 0,
            "suppressed_by_reason": {},
        }

    policy_items: list[dict[str, Any]] = []
    for candidate in selected:
        key = str(candidate["candidate_key"])
        action = refresh_topology_group(
            paths,
            groups_by_key[key],
            candidate,
            run_id=run_id,
            phase=phase,
        )
        with connection(paths.sqlite_path) as conn:
            decision = evaluate_policy(
                conn,
                str(action["action_type"]),
                action.get("action_features") or {},
            )
        policy_items.append({"action": action, "decision": decision})
    decided, failures = redecide_policy_actions(
        paths,
        policy_items,
        critic_review=critic_review,
        critic_llm_provider=critic_llm_provider,
    )
    return {
        "selected_candidates": selected,
        "results": list(decided.values()),
        "failures": failures,
        "closed_counts": closed_counts,
        "gardener_summary": {
            **judged["summary"],
            "topology_arbitration": arbitration,
        },
    }


def refresh_topology_group(
    paths: BrainPaths,
    group: dict[str, Any],
    candidate: dict[str, Any],
    *,
    run_id: str,
    phase: str,
) -> dict[str, Any]:
    canonical = group["actions"][0]
    sibling_actions = group["actions"][1:]
    spec = gardener_action_spec(candidate)
    timestamp = now_iso()
    evidence = dict(canonical.get("evidence_json") or {})
    evidence["payload"] = spec["action_payload"]
    if spec["evidence"]:
        evidence.update(spec["evidence"])
    evidence["topology_reconciliation"] = {
        "version": "topology-current-settings-v1",
        "outcome": "readmitted",
        "phase": phase,
        "candidate_key": group["candidate_key"],
        "duplicate_action_ids": [str(action["id"]) for action in sibling_actions],
        "run_id": run_id,
        "reconciled_at": timestamp,
    }
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO wiki_curation_runs(
              id, source_packet_id, group_by, status, summary, created_at
            ) VALUES (?, NULL, 'cos_action', 'running', ?, ?)
            """,
            (
                run_id,
                dumps({"created_by": "topology_reconciliation"}),
                timestamp,
            ),
        )
        conn.execute(
            """
            UPDATE cos_actions
            SET run_id = ?, status = 'proposed', target_fact_ids = ?, target_page_paths = ?,
                target_contract_ids = ?, action_features = ?, proposed_by = ?,
                confidence = ?, risk_tier = ?, evidence_json = ?,
                policy_id = NULL, policy_version = NULL, policy_decision = NULL,
                autonomy_level = NULL, critic_by = NULL, critic_decision = NULL
            WHERE id = ?
            """,
            (
                run_id,
                dumps(spec["target_fact_ids"]),
                dumps(spec["target_page_paths"]),
                dumps(spec["target_contract_ids"]),
                dumps(spec["action_features"]),
                spec["proposed_by"],
                spec["confidence"],
                spec["risk_tier"],
                dumps(evidence),
                canonical["id"],
            ),
        )
        for sibling in sibling_actions:
            sibling_evidence = dict(sibling.get("evidence_json") or {})
            sibling_evidence["topology_reconciliation"] = {
                "version": "topology-current-settings-v1",
                "outcome": "duplicate_retired",
                "canonical_action_id": canonical["id"],
                "candidate_key": group["candidate_key"],
                "reconciled_at": timestamp,
            }
            conn.execute(
                "UPDATE cos_actions SET status = 'dismissed', evidence_json = ? WHERE id = ?",
                (dumps(sibling_evidence), sibling["id"]),
            )
        close_linked_topology_questions(
            conn,
            [str(action["id"]) for action in group["actions"]],
            answer={
                "decision": "topology_candidate_refreshed",
                "canonical_action_id": canonical["id"],
                "candidate_key": group["candidate_key"],
            },
            timestamp=timestamp,
        )
    return get_action(paths, str(canonical["id"]))


def close_topology_group(
    paths: BrainPaths,
    group: dict[str, Any],
    *,
    outcome: str,
    reason: str,
) -> None:
    timestamp = now_iso()
    with connection(paths.sqlite_path) as conn:
        for index, action in enumerate(group["actions"]):
            evidence = dict(action.get("evidence_json") or {})
            evidence["topology_reconciliation"] = {
                "version": "topology-current-settings-v1",
                "outcome": outcome,
                "reason": reason,
                "candidate_key": group["candidate_key"],
                "reconciled_at": timestamp,
            }
            conn.execute(
                "UPDATE cos_actions SET status = ?, evidence_json = ? WHERE id = ?",
                ("rejected" if index == 0 else "dismissed", dumps(evidence), action["id"]),
            )
        close_linked_topology_questions(
            conn,
            [str(action["id"]) for action in group["actions"]],
            answer={
                "decision": outcome,
                "reason": reason,
                "candidate_key": group["candidate_key"],
            },
            timestamp=timestamp,
        )


def close_linked_topology_questions(
    conn: Any,
    action_ids: list[str],
    *,
    answer: dict[str, Any],
    timestamp: str,
) -> None:
    if not action_ids:
        return
    placeholders = ",".join("?" for _ in action_ids)
    conn.execute(
        f"""
        UPDATE open_questions
        SET status = 'auto_resolved', answer = ?, answered_at = ?,
            decided_by = 'topology_reconciliation_v1'
        WHERE action_id IN ({placeholders})
          AND status IN ('open', 'needs_human')
        """,
        [dumps(answer), timestamp, *action_ids],
    )


def preserve_deterministic_risk(
    candidate: dict[str, Any], baseline_risk: str
) -> dict[str, Any]:
    output = dict(candidate)
    judged_risk = str(output.get("risk_tier") or baseline_risk)
    if RISK_ORDER.get(judged_risk, 1) < RISK_ORDER.get(baseline_risk, 1):
        output["risk_tier"] = baseline_risk
    return output


def current_admission_rejection_reason(
    group: dict[str, Any], settings: dict[str, Any]
) -> str:
    if group["action_type"] == "page_split":
        return (
            "Current page evidence does not satisfy the split threshold at "
            f"split aggressiveness {settings['split_aggressiveness']}."
        )
    if group["action_type"] in {"page_merge", "entity_merge"}:
        return (
            "Current evidence no longer admits this merge candidate at "
            f"merge aggressiveness {settings['merge_aggressiveness']}."
        )
    return "Current topology settings no longer admit this candidate."


def topology_reconciliation_report(
    context: dict[str, Any],
    groups: list[dict[str, Any]],
    admitted: list[dict[str, Any]],
    stale: list[dict[str, Any]],
    *,
    arbitration: dict[str, Any],
) -> dict[str, Any]:
    return {
        "settings": {
            "merge_aggressiveness": context["settings"]["merge_aggressiveness"],
            "split_aggressiveness": context["settings"]["split_aggressiveness"],
        },
        "open_action_row_count": sum(len(group["actions"]) for group in groups),
        "open_unique_candidate_count": len(groups),
        "duplicate_action_row_count": sum(
            max(0, len(group["actions"]) - 1) for group in groups
        ),
        "open_unique_by_type": count_groups_by_type(groups),
        "currently_admitted_count": len(admitted),
        "currently_admitted_by_type": count_groups_by_type(admitted),
        "no_longer_admitted_count": len(stale),
        "no_longer_admitted_by_type": count_groups_by_type(stale),
        "planned_arbitration": arbitration,
        "no_longer_admitted_examples": [
            {
                "candidate_key": group["candidate_key"],
                "action_type": group["action_type"],
                "copy_count": len(group["actions"]),
            }
            for group in stale[:25]
        ],
    }


def count_groups_by_type(groups: list[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(str(group["action_type"]) for group in groups).items()))


def empty_phase_result() -> dict[str, Any]:
    return {
        "selected_candidates": [],
        "results": [],
        "failures": [],
        "closed_counts": Counter(),
        "gardener_summary": {
            "enabled": False,
            "candidate_input_count": 0,
            "judgment_count": 0,
            "dropped_candidate_count": 0,
            "topology_arbitration": {
                "input_count": 0,
                "selected_count": 0,
                "suppressed_count": 0,
                "suppressed_by_reason": {},
            },
        },
    }
