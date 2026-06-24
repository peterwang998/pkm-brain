from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .contracts import active_page_contracts
from .cos_actions import propose_action
from .paths import BrainPaths
from .wiki_facts import managed_fact_page_summaries


def generate_gardener_candidates(
    paths: BrainPaths,
    *,
    shadow: bool = True,
    max_candidates: int = 25,
) -> dict[str, Any]:
    pages = managed_fact_page_summaries(paths)
    contracts = {contract["page_hint"]: contract for contract in active_page_contracts(paths)}
    candidates = deterministic_topology_candidates(pages, contracts)[:max_candidates]
    actions: list[dict[str, Any]] = []
    if not shadow:
        for candidate in candidates:
            actions.append(propose_gardener_action(paths, candidate))
    return {
        "status": "ok",
        "shadow": shadow,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "actions": actions,
    }


def deterministic_topology_candidates(
    pages: list[dict[str, Any]], contracts: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for index, left in enumerate(pages):
        for right in pages[index + 1 :]:
            similarity = page_similarity(left["relative_path"], right["relative_path"])
            if similarity >= 0.86:
                candidates.append(
                    {
                        "action_type": "page_merge",
                        "page_hints": [left["relative_path"], right["relative_path"]],
                        "similarity": similarity,
                        "reason": "near-duplicate page hints",
                        "contracts_present": [
                            left["relative_path"] in contracts,
                            right["relative_path"] in contracts,
                        ],
                        "risk_tier": "medium",
                    }
                )
    for page in pages:
        active_count = int(page.get("active_fact_count") or 0)
        if active_count == 1 and page["relative_path"] not in contracts:
            candidates.append(
                {
                    "action_type": "rehome_fact",
                    "page_hints": [page["relative_path"]],
                    "similarity": 0.0,
                    "reason": "singleton page without contract",
                    "contracts_present": [False],
                    "risk_tier": "low",
                }
            )
    return candidates


def propose_gardener_action(paths: BrainPaths, candidate: dict[str, Any]) -> dict[str, Any]:
    action_type = str(candidate["action_type"])
    return propose_action(
        paths,
        action_type,
        action_payload={"candidate": candidate},
        action_features={
            "candidate_signal": candidate.get("reason"),
            "similarity": candidate.get("similarity"),
            "affected_page_count": len(candidate.get("page_hints") or []),
            "reversible": True,
            "eval_gate": {"suite": "topology", "passed": False},
        },
        target_page_paths=[str(path) for path in candidate.get("page_hints") or []],
        proposed_by="gardener",
        confidence=float(candidate.get("similarity") or 0.0),
        risk_tier=str(candidate.get("risk_tier") or "medium"),
    )


def page_similarity(left: str, right: str) -> float:
    left_key = Path(left).with_suffix("").as_posix()
    right_key = Path(right).with_suffix("").as_posix()
    return SequenceMatcher(None, left_key, right_key).ratio()
