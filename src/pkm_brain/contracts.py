from __future__ import annotations

import re
from typing import Any

from .cos_actions import apply_action, propose_action
from .db import connection, dumps, loads
from .paths import BrainPaths
from .util import new_id, now_iso
from .wiki_facts import human_title_for_path, managed_fact_page_summaries


CONTRACT_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_/-]{2,}")
CONTRACT_STOP_TOKENS = {
    "about",
    "active",
    "and",
    "atomic",
    "backed",
    "better",
    "canonical",
    "contract",
    "does",
    "doesn",
    "draft",
    "entity",
    "fact",
    "facts",
    "for",
    "from",
    "governed",
    "here",
    "include",
    "maintained",
    "managed",
    "not",
    "page",
    "policy",
    "routed",
    "source",
    "stale",
    "summary",
    "synthesis",
    "that",
    "the",
    "this",
    "uncited",
    "using",
    "what",
}


def active_page_contracts(paths: BrainPaths) -> list[dict[str, Any]]:
    with connection(paths.sqlite_path) as conn:
        return [
            row_to_contract(row)
            for row in conn.execute(
                """
                SELECT *
                FROM page_contracts
                WHERE status = 'active'
                ORDER BY page_hint, version DESC
                """
            )
        ]


def page_contract(paths: BrainPaths, page_hint: str) -> dict[str, Any] | None:
    with connection(paths.sqlite_path) as conn:
        row = conn.execute(
            """
            SELECT *
            FROM page_contracts
            WHERE page_hint = ?
              AND status = 'active'
            ORDER BY version DESC, created_at DESC
            LIMIT 1
            """,
            (page_hint,),
        ).fetchone()
    return row_to_contract(row) if row else None


def generate_initial_contracts(
    paths: BrainPaths,
    *,
    limit: int | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    summaries = managed_fact_page_summaries(paths)
    if limit is not None:
        summaries = summaries[:limit]
    existing = {contract["page_hint"] for contract in active_page_contracts(paths)}
    proposed = [
        default_contract_for_page(summary)
        for summary in summaries
        if summary["relative_path"] not in existing
    ]
    actions: list[dict[str, Any]] = []
    if apply:
        for contract in proposed:
            action = propose_action(
                paths,
                "edit_contract",
                action_payload={"contract": contract},
                action_features={
                    "deterministic": True,
                    "risk_score": 0.1,
                    "reversible": True,
                    "affected_page_count": 1,
                },
                target_page_paths=[contract["page_hint"]],
                proposed_by="contracts",
            )
            actions.append(apply_action(paths, action["id"]))
    return {
        "contracts": proposed,
        "actions": actions,
        "applied": apply,
        "existing_contract_count": len(existing),
    }


def default_contract_for_page(page: dict[str, Any]) -> dict[str, Any]:
    page_hint = str(page.get("relative_path") or "")
    title = str(page.get("title") or human_title_for_path(page_hint))
    return {
        "id": new_id("contract"),
        "page_hint": page_hint,
        "canonical_entity": title,
        "page_scope": f"Managed source-backed facts about {title}.",
        "retrieval_purpose": f"Answer factual questions about {title} using active facts and source evidence.",
        "what_belongs_here": "Atomic active facts routed to this page by entity and section.",
        "what_does_not_belong_here": "Uncited synthesis, stale draft text, and facts better governed by another page contract.",
        "freshness_policy": "Refresh when active facts change; unresolved truth conflicts stay visible as residue.",
        "related_pages": [],
        "version": 1,
        "status": "active",
    }


def validate_fact_against_contract(
    fact: dict[str, Any], contract: dict[str, Any] | None
) -> dict[str, Any]:
    if contract is None:
        return {
            "valid": False,
            "reason": "no active contract",
            "recommended_action": "propose_contract_or_escalate",
        }
    page_hint = str(fact.get("page_hint") or "")
    if page_hint != contract.get("page_hint"):
        return {
            "valid": False,
            "reason": "fact page_hint does not match contract page_hint",
            "recommended_action": "rehome_fact_or_edit_contract",
        }
    fact_tokens = contract_signal_tokens(
        fact.get("statement"),
        fact.get("entity_key"),
        fact.get("section_hint"),
    )
    belongs_tokens = contract_signal_tokens(
        contract.get("canonical_entity"),
        contract.get("page_scope"),
        contract.get("retrieval_purpose"),
        contract.get("what_belongs_here"),
        contract.get("related_pages"),
    )
    excluded_tokens = contract_signal_tokens(contract.get("what_does_not_belong_here"))
    belongs_overlap = sorted(fact_tokens & belongs_tokens)
    excluded_overlap = sorted(fact_tokens & excluded_tokens)
    score = token_overlap_score(fact_tokens, belongs_tokens)
    if len(excluded_overlap) >= 2 and score < 0.35:
        return {
            "valid": False,
            "reason": "fact content overlaps contract exclusions",
            "recommended_action": "rehome_fact_or_edit_contract",
            "contract_scope_score": round(score, 4),
            "matched_contract_terms": belongs_overlap,
            "matched_excluded_terms": excluded_overlap,
        }
    if len(belongs_tokens) >= 2 and not belongs_overlap:
        return {
            "valid": False,
            "reason": "fact content does not match contract scope",
            "recommended_action": "rehome_fact_or_edit_contract",
            "contract_scope_score": 0.0,
            "matched_contract_terms": [],
            "matched_excluded_terms": excluded_overlap,
        }
    return {
        "valid": True,
        "reason": "fact matches active contract",
        "contract_scope_score": round(score, 4),
        "matched_contract_terms": belongs_overlap,
        "matched_excluded_terms": excluded_overlap,
    }


def contract_signal_tokens(*values: Any) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            text = " ".join(str(item) for item in value)
        else:
            text = str(value)
        normalized = text.lower().replace("_", " ").replace("/", " ").replace("-", " ")
        for token in CONTRACT_TOKEN_RE.findall(normalized):
            if token in CONTRACT_STOP_TOKENS:
                continue
            tokens.add(token)
    return tokens


def token_overlap_score(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def row_to_contract(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "page_hint": row["page_hint"],
        "canonical_entity": row["canonical_entity"],
        "page_scope": row["page_scope"],
        "retrieval_purpose": row["retrieval_purpose"],
        "what_belongs_here": row["what_belongs_here"],
        "what_does_not_belong_here": row["what_does_not_belong_here"],
        "freshness_policy": row["freshness_policy"],
        "related_pages": loads(row["related_pages"], []),
        "version": row["version"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def insert_contract_direct(conn: Any, contract: dict[str, Any]) -> None:
    timestamp = now_iso()
    conn.execute(
        """
        INSERT INTO page_contracts(
          id, page_hint, canonical_entity, page_scope, retrieval_purpose,
          what_belongs_here, what_does_not_belong_here, freshness_policy,
          related_pages, version, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            contract.get("id") or new_id("contract"),
            contract["page_hint"],
            contract.get("canonical_entity"),
            contract.get("page_scope"),
            contract.get("retrieval_purpose"),
            contract.get("what_belongs_here"),
            contract.get("what_does_not_belong_here"),
            contract.get("freshness_policy"),
            dumps(contract.get("related_pages") or []),
            int(contract.get("version") or 1),
            str(contract.get("status") or "active"),
            timestamp,
            timestamp,
        ),
    )
