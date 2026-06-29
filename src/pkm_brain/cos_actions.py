from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .cos_policy import PolicyDecision, classify_action_risk, evaluate_policy
from .db import connection, dumps, loads
from .llm import LLMProvider, complete_json, cos_role_provider_configured, get_cos_role_provider
from .paths import BrainPaths
from .util import new_id, now_iso


APPLIED_STATUSES = {"applied", "auto_applied"}
CRITIC_SCHEMA = {
    "type": "object",
    "required": ["decision", "rationale"],
    "properties": {
        "decision": {"type": "string"},
        "rationale": {"type": "string"},
    },
}
ACTION_TYPE_SPECS: dict[str, dict[str, Any]] = {
    "fact_upsert": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": ["restore_fact", "delete_fact_ids"],
        "projection": "rebuild_fact_retrieval_index; managed page rendering remains a caller-triggered projection",
    },
    "rehome_fact": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": ["restore_fact_routing"],
        "projection": "rebuild_fact_retrieval_index; affected managed pages must be reprojected by caller",
    },
    "edit_contract": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": ["restore_contract", "delete_contract_ids"],
        "projection": "contract validation/page projection remains caller-triggered",
    },
    "synthesize_page": {
        "class": "derived_mutation",
        "implemented": True,
        "inverse_keys": ["delete_synthesis_ids"],
        "projection": "derived synthesis only; never source evidence",
    },
    "canonicalize_page": {
        "class": "deterministic_projection",
        "implemented": True,
        "inverse_keys": ["noop"],
        "projection": "no-op action marker for deterministic canonicalization",
    },
    "fact_merge": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": ["restore_facts"],
        "projection": "rebuild_fact_retrieval_index; affected managed pages must be reprojected by caller",
    },
    "fact_supersede": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": ["restore_facts"],
        "projection": "rebuild_fact_retrieval_index; affected managed pages must be reprojected by caller",
    },
    "resolve_conflict": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": ["restore_facts"],
        "projection": "rebuild_fact_retrieval_index; affected managed pages must be reprojected by caller",
    },
    "display_contested": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": ["restore_facts", "delete_question_ids"],
        "projection": "rebuild_fact_retrieval_index; open question residue references this action",
    },
    "page_merge": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": ["restore_facts", "restore_contracts", "restore_syntheses"],
        "projection": "moves source facts to a destination page; affected pages must be reprojected",
    },
    "page_split": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": ["restore_facts", "restore_syntheses"],
        "projection": "moves section-scoped facts to generated child pages; affected pages must be reprojected",
    },
    "rename_page": {
        "class": "ledger_mutation",
        "implemented": True,
        "inverse_keys": ["restore_facts", "restore_contracts", "restore_syntheses"],
        "projection": "renames fact routing, contracts, syntheses, and indexed wiki page path",
    },
    "archive_page": {
        "class": "deterministic_projection",
        "implemented": True,
        "inverse_keys": ["noop"],
        "projection": "records a managed-page archive projection; page markdown remains rebuildable",
    },
    "revert_page_snapshot": {
        "class": "deterministic_projection",
        "implemented": True,
        "inverse_keys": ["noop"],
        "projection": "records a guarded page snapshot revert projection",
    },
}


def propose_action(
    paths: BrainPaths,
    action_type: str,
    *,
    action_payload: dict[str, Any] | None = None,
    action_features: dict[str, Any] | None = None,
    run_id: str | None = None,
    target_fact_ids: list[str] | None = None,
    target_page_paths: list[str] | None = None,
    target_contract_ids: list[str] | None = None,
    proposed_by: str = "system",
    confidence: float | None = None,
    risk_tier: str | None = None,
    evidence: dict[str, Any] | None = None,
    decide: bool = False,
) -> dict[str, Any]:
    validate_action_type(action_type)
    action_id = new_id("cosact")
    evidence_json = dict(evidence or {})
    if action_payload is not None:
        evidence_json["payload"] = action_payload
    features = dict(action_features or {})
    features.setdefault("target_fact_ids", target_fact_ids or [])
    features.setdefault("target_page_paths", target_page_paths or [])
    features.setdefault("target_contract_ids", target_contract_ids or [])
    resolved_risk_tier = classify_action_risk(action_type, features, explicit_risk_tier=risk_tier)
    features.setdefault("risk_tier", resolved_risk_tier)
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO cos_actions(
              id, run_id, action_type, status, target_fact_ids, target_page_paths,
              target_contract_ids, action_features, proposed_by, confidence,
              risk_tier, evidence_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action_id,
                run_id,
                action_type,
                "proposed",
                dumps(target_fact_ids or []),
                dumps(target_page_paths or []),
                dumps(target_contract_ids or []),
                dumps(features),
                proposed_by,
                confidence,
                resolved_risk_tier,
                dumps(evidence_json),
                now_iso(),
            ),
        )
    if decide:
        return decide_action(paths, action_id)
    return get_action(paths, action_id)


def decide_action(
    paths: BrainPaths,
    action_id: str,
    *,
    critic_by: str | None = None,
    critic_decision: str | None = None,
    critic_llm_provider: LLMProvider | None = None,
    critic_provider: str | None = None,
) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        action = load_action(conn, action_id)
        decision = evaluate_policy(
            conn, action["action_type"], action.get("action_features") or {}
        )
    if decision.critic_required and critic_decision is None:
        review = critic_review(
            paths,
            action,
            decision,
            llm_provider=critic_llm_provider,
            provider=critic_provider,
        )
        critic_by = review["critic_by"]
        critic_decision = review["decision"]
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE cos_actions
            SET policy_id = ?, policy_version = ?, policy_decision = ?,
                autonomy_level = ?, critic_by = COALESCE(?, critic_by),
                critic_decision = COALESCE(?, critic_decision)
            WHERE id = ?
            """,
            (
                decision.policy_id,
                decision.policy_version,
                decision.policy_decision,
                decision.autonomy_level,
                critic_by,
                critic_decision,
                action_id,
            ),
        )
    if decision.autonomy_level == "L0":
        return apply_action(paths, action_id, applied_status="auto_applied")
    if decision.autonomy_level == "L1":
        if decision.critic_required and critic_decision != "agree":
            mark_needs_human(paths, action_id, decision, "critic did not agree")
            return get_action(paths, action_id)
        return apply_action(paths, action_id, applied_status="auto_applied")
    if decision.autonomy_level == "L2":
        if decision.critic_required and critic_decision != "agree":
            mark_needs_human(paths, action_id, decision, "critic did not agree")
            return get_action(paths, action_id)
        return apply_action(paths, action_id, applied_status="applied")
    mark_needs_human(paths, action_id, decision, decision.reason or "requires human decision")
    return get_action(paths, action_id)


def critic_review(
    paths: BrainPaths,
    action: dict[str, Any],
    decision: PolicyDecision,
    *,
    llm_provider: LLMProvider | None = None,
    provider: str | None = None,
) -> dict[str, str]:
    if not cos_role_provider_configured(paths, "critic", llm_provider=llm_provider, provider=provider):
        return {
            "critic_by": "critic:unconfigured",
            "decision": "unavailable",
            "rationale": "No CoS LLM provider configured for critic role",
        }
    active_provider = get_cos_role_provider(paths, "critic", provider=provider, llm_provider=llm_provider)
    parsed = complete_json(
        critic_prompt(action, decision),
        schema=CRITIC_SCHEMA,
        role="critic",
        provider=provider,
        llm_provider=active_provider,
        paths=paths,
    )
    return {
        "critic_by": critic_provider_label(active_provider),
        "decision": normalize_critic_decision(parsed.get("decision")),
        "rationale": str(parsed.get("rationale") or "")[:1000],
    }


def critic_prompt(action: dict[str, Any], decision: PolicyDecision) -> str:
    action_card = {
        "id": action.get("id"),
        "action_type": action.get("action_type"),
        "risk_tier": action.get("risk_tier"),
        "confidence": action.get("confidence"),
        "features": action.get("action_features") or {},
        "targets": {
            "fact_ids": action.get("target_fact_ids") or [],
            "page_paths": action.get("target_page_paths") or [],
            "contract_ids": action.get("target_contract_ids") or [],
        },
        "payload": action_payload(action),
        "proposed_by": action.get("proposed_by"),
    }
    policy_card = {
        "policy_id": decision.policy_id,
        "policy_version": decision.policy_version,
        "autonomy_level": decision.autonomy_level,
        "reason": decision.reason,
    }
    return (
        "Review this Chief-of-Staff action before autonomous application. "
        "Return decision 'agree' only if the action is supported by its payload, targets, policy, "
        "and risk features. Return 'disagree' if it is unsafe, unsupported, too broad, or should be human-reviewed. "
        "Do not rewrite the action.\n\n"
        f"Action:\n{action_card}\n\nPolicy:\n{policy_card}"
    )


def normalize_critic_decision(value: Any) -> str:
    decision = str(value or "").strip().lower()
    if decision in {"agree", "approve", "approved", "pass", "passed", "ok", "safe"}:
        return "agree"
    if decision in {"disagree", "reject", "rejected", "block", "blocked", "fail", "failed", "unsafe"}:
        return "disagree"
    return "unavailable"


def critic_provider_label(provider: LLMProvider | None) -> str:
    if provider is None:
        return "critic:unknown"
    model = getattr(provider, "model", None)
    if model:
        return f"{provider.name}:{model}"
    return str(provider.name)


def apply_action(
    paths: BrainPaths, action_id: str, *, applied_status: str = "applied"
) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        action = load_action(conn, action_id)
        if action["status"] in APPLIED_STATUSES:
            return action
        spec = ACTION_TYPE_SPECS.get(str(action["action_type"]))
        if spec is None:
            conn.execute(
                "UPDATE cos_actions SET status = ? WHERE id = ?",
                ("failed", action_id),
            )
            conn.commit()
            raise ValueError(f"unknown cos action type: {action['action_type']}")
        if not spec.get("implemented"):
            conn.execute(
                "UPDATE cos_actions SET status = ? WHERE id = ?",
                ("failed", action_id),
            )
            conn.commit()
            reason = spec.get("reason") or "not implemented"
            raise ValueError(
                f"action type {action['action_type']} is declared but not implemented: {reason}"
            )
        payload = action_payload(action)
        inverse: dict[str, Any] = {}
        target_fact_ids = list(action.get("target_fact_ids") or [])
        target_contract_ids = list(action.get("target_contract_ids") or [])
        target_page_paths = list(action.get("target_page_paths") or [])

        if action["action_type"] == "fact_upsert":
            fact_id, inverse = apply_fact_upsert(conn, payload)
            target_fact_ids = stable_unique([*target_fact_ids, fact_id])
        elif action["action_type"] in {"fact_supersede", "resolve_conflict"}:
            fact_ids, inverse = apply_fact_updates(conn, payload)
            target_fact_ids = stable_unique([*target_fact_ids, *fact_ids])
        elif action["action_type"] == "fact_merge":
            fact_ids, inverse = apply_fact_merge(conn, payload)
            target_fact_ids = stable_unique([*target_fact_ids, *fact_ids])
        elif action["action_type"] == "display_contested":
            fact_ids, inverse = apply_display_contested(conn, payload, action_id)
            target_fact_ids = stable_unique([*target_fact_ids, *fact_ids])
        elif action["action_type"] == "rehome_fact":
            fact_id, inverse = apply_rehome_fact(conn, payload)
            target_fact_ids = stable_unique([*target_fact_ids, fact_id])
        elif action["action_type"] == "edit_contract":
            contract_id, inverse = apply_edit_contract(conn, payload)
            target_contract_ids = stable_unique([*target_contract_ids, contract_id])
        elif action["action_type"] == "synthesize_page":
            page_hint, inverse = apply_synthesize_page(conn, payload)
            target_page_paths = stable_unique([*target_page_paths, page_hint])
        elif action["action_type"] == "page_merge":
            fact_ids, contract_ids, page_hints, inverse = apply_page_merge(conn, payload)
            target_fact_ids = stable_unique([*target_fact_ids, *fact_ids])
            target_contract_ids = stable_unique([*target_contract_ids, *contract_ids])
            target_page_paths = stable_unique([*target_page_paths, *page_hints])
        elif action["action_type"] == "page_split":
            fact_ids, page_hints, inverse = apply_page_split(conn, payload)
            target_fact_ids = stable_unique([*target_fact_ids, *fact_ids])
            target_page_paths = stable_unique([*target_page_paths, *page_hints])
        elif action["action_type"] == "rename_page":
            fact_ids, contract_ids, page_hints, inverse = apply_rename_page(conn, payload)
            target_fact_ids = stable_unique([*target_fact_ids, *fact_ids])
            target_contract_ids = stable_unique([*target_contract_ids, *contract_ids])
            target_page_paths = stable_unique([*target_page_paths, *page_hints])
        elif action["action_type"] in {"canonicalize_page", "archive_page", "revert_page_snapshot"}:
            page_hint = str(payload.get("page_hint") or "")
            if page_hint:
                target_page_paths = stable_unique([*target_page_paths, page_hint])
            inverse = {"noop": True}
        else:
            conn.execute(
                "UPDATE cos_actions SET status = ? WHERE id = ?",
                ("failed", action_id),
            )
            conn.commit()
            raise ValueError(f"unsupported implemented action type: {action['action_type']}")

        state_hash = target_state_hash(
            conn,
            target_fact_ids=target_fact_ids,
            target_contract_ids=target_contract_ids,
            target_page_paths=target_page_paths,
        )
        conn.execute(
            """
            UPDATE cos_actions
            SET status = ?, target_fact_ids = ?, target_contract_ids = ?,
                target_page_paths = ?, inverse_action_json = ?,
                applied_state_hash = ?, applied_at = ?
            WHERE id = ?
            """,
            (
                applied_status,
                dumps(target_fact_ids),
                dumps(target_contract_ids),
                dumps(target_page_paths),
                dumps(inverse),
                state_hash,
                now_iso(),
                action_id,
            ),
        )
    return get_action(paths, action_id)


def revert_action(paths: BrainPaths, action_id: str) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        action = load_action(conn, action_id)
        if action["status"] == "reverted":
            return action
        expected = action.get("applied_state_hash")
        current = target_state_hash(
            conn,
            target_fact_ids=action.get("target_fact_ids") or [],
            target_contract_ids=action.get("target_contract_ids") or [],
            target_page_paths=action.get("target_page_paths") or [],
        )
        if expected and current != expected:
            create_action_residue(
                conn,
                action,
                "revert_drift",
                "Guarded revert refused because target state drifted after apply.",
            )
            conn.execute(
                "UPDATE cos_actions SET status = ? WHERE id = ?",
                ("failed", action_id),
            )
            return load_action(conn, action_id)
        apply_inverse(conn, action.get("inverse_action_json") or {})
        conn.execute(
            """
            UPDATE cos_actions
            SET status = 'reverted', reverted_at = ?
            WHERE id = ?
            """,
            (now_iso(), action_id),
        )
    return get_action(paths, action_id)


def record_action_audit(
    paths: BrainPaths,
    action_id: str,
    audit_status: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        action = load_action(conn, action_id)
        evidence = dict(action.get("evidence_json") or {})
        audits = list(evidence.get("audits") or [])
        audits.append({"status": audit_status, "metadata": metadata or {}, "at": now_iso()})
        evidence["audits"] = audits
        conn.execute(
            """
            UPDATE cos_actions
            SET audit_status = ?, evidence_json = ?
            WHERE id = ?
            """,
            (audit_status, dumps(evidence), action_id),
        )
    return get_action(paths, action_id)


def mark_simple_autonomy_applied(
    paths: BrainPaths,
    action_id: str,
    *,
    autonomy_level: str = "L2",
    policy_decision: str = "simple_autonomy",
) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE cos_actions
            SET policy_decision = ?, autonomy_level = ?
            WHERE id = ?
            """,
            (policy_decision, autonomy_level, action_id),
        )
    return get_action(paths, action_id)


def mark_action_residue(
    paths: BrainPaths,
    action_id: str,
    *,
    kind: str,
    reason: str,
    policy_decision: str = "simple_residue",
    autonomy_level: str = "L3",
) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        action = load_action(conn, action_id)
        create_action_residue(conn, action, kind, reason)
        conn.execute(
            """
            UPDATE cos_actions
            SET status = 'needs_human', policy_decision = ?, autonomy_level = ?
            WHERE id = ?
            """,
            (policy_decision, autonomy_level, action_id),
        )
    return get_action(paths, action_id)


def apply_fact_upsert(conn: Any, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    from .wiki_facts import rebuild_fact_retrieval_index, row_to_fact

    fact = payload.get("fact") if isinstance(payload.get("fact"), dict) else payload
    fact_id = str(fact.get("id") or new_id("fact"))
    existing = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
    inverse = (
        {"restore_fact": row_to_fact(existing)}
        if existing
        else {"delete_fact_ids": [fact_id]}
    )
    values = fact_values(fact, fact_id, existing)
    if existing:
        update_values = (*values[1:13], values[14], *values[15:], fact_id)
        conn.execute(
            """
            UPDATE facts
            SET statement = ?, entity_key = ?, page_hint = ?, section_hint = ?,
                source_ids = ?, observed_at = ?, confidence = ?, status = ?,
                supersedes_id = ?, conflict_group_id = ?, confirmed_by_user = ?,
                metadata = ?, last_seen_at = ?, source_spans = ?,
                evidence_quote = ?, extraction_method = ?, extractor_model = ?,
                effective_at = ?, extraction_confidence = ?, routing_confidence = ?,
                truth_confidence = ?
            WHERE id = ?
            """,
            update_values,
        )
    else:
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, page_hint, section_hint, source_ids,
              observed_at, confidence, status, supersedes_id, conflict_group_id,
              confirmed_by_user, metadata, created_at, last_seen_at, source_spans,
              evidence_quote, extraction_method, extractor_model, effective_at,
              extraction_confidence, routing_confidence, truth_confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
    rebuild_fact_retrieval_index(conn)
    return fact_id, inverse


def fact_values(fact: dict[str, Any], fact_id: str, existing: Any | None) -> tuple[Any, ...]:
    timestamp = now_iso()
    existing_get = existing_value_getter(existing)
    confidence = float(fact.get("confidence", existing_get("confidence", 0.0)) or 0.0)
    return (
        fact_id,
        str(fact.get("statement", existing_get("statement", ""))),
        str(fact.get("entity_key", existing_get("entity_key", "manual:fact"))),
        fact.get("page_hint", existing_get("page_hint")),
        fact.get("section_hint", existing_get("section_hint")),
        dumps(fact.get("source_ids", loads(existing_get("source_ids"), []))),
        fact.get("observed_at", existing_get("observed_at")),
        confidence,
        str(fact.get("status", existing_get("status", "active"))),
        fact.get("supersedes_id", existing_get("supersedes_id")),
        fact.get("conflict_group_id", existing_get("conflict_group_id")),
        1 if fact.get("confirmed_by_user", existing_get("confirmed_by_user", 0)) else 0,
        dumps(fact.get("metadata", loads(existing_get("metadata"), {}))),
        existing_get("created_at", timestamp),
        timestamp,
        dumps(fact.get("source_spans", loads(existing_get("source_spans"), []))),
        fact.get("evidence_quote", existing_get("evidence_quote")),
        str(fact.get("extraction_method", existing_get("extraction_method", "legacy"))),
        fact.get("extractor_model", existing_get("extractor_model")),
        fact.get("effective_at", existing_get("effective_at")),
        fact.get("extraction_confidence", existing_get("extraction_confidence")),
        fact.get("routing_confidence", existing_get("routing_confidence")),
        fact.get("truth_confidence", existing_get("truth_confidence", confidence)),
    )


def apply_rehome_fact(conn: Any, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    from .wiki_facts import rebuild_fact_retrieval_index, row_to_fact

    fact_id = str(payload.get("fact_id") or "")
    if not fact_id:
        raise ValueError("rehome_fact requires fact_id")
    row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
    if not row:
        raise ValueError(f"fact not found: {fact_id}")
    old = row_to_fact(row)
    metadata = payload.get("metadata")
    metadata_sql = dumps(metadata) if isinstance(metadata, dict) else row["metadata"]
    conn.execute(
        """
        UPDATE facts
        SET page_hint = COALESCE(?, page_hint),
            entity_key = COALESCE(?, entity_key),
            section_hint = COALESCE(?, section_hint),
            metadata = ?,
            last_seen_at = ?
        WHERE id = ?
        """,
        (
            payload.get("page_hint"),
            payload.get("entity_key"),
            payload.get("section_hint"),
            metadata_sql,
            now_iso(),
            fact_id,
        ),
    )
    rebuild_fact_retrieval_index(conn)
    return (
        fact_id,
        {
            "restore_fact_routing": [
                {
                    "fact_id": fact_id,
                    "old_page_hint": old.get("page_hint"),
                    "old_entity_key": old.get("entity_key"),
                    "old_section_hint": old.get("section_hint"),
                    "old_metadata": old.get("metadata"),
                }
            ]
        },
    )


def apply_fact_updates(conn: Any, payload: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    from .wiki_facts import rebuild_fact_retrieval_index, row_to_fact

    updates = payload.get("updates") if isinstance(payload.get("updates"), list) else []
    if not updates:
        raise ValueError("fact update action requires updates")
    old_facts: list[dict[str, Any]] = []
    fact_ids: list[str] = []
    for update in updates:
        if not isinstance(update, dict):
            continue
        fact_id = str(update.get("fact_id") or "")
        if not fact_id:
            raise ValueError("fact update requires fact_id")
        row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
        if not row:
            raise ValueError(f"fact not found: {fact_id}")
        old = row_to_fact(row)
        old_facts.append(old)
        merged = dict(old)
        for key, value in update.items():
            if key != "fact_id":
                merged[key] = value
        apply_fact_upsert(conn, {"fact": merged})
        fact_ids.append(fact_id)
    rebuild_fact_retrieval_index(conn)
    return stable_unique(fact_ids), {"restore_facts": old_facts}


def apply_fact_merge(conn: Any, payload: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    from .wiki_facts import rebuild_fact_retrieval_index, row_to_fact

    keeper = payload.get("keeper_fact") if isinstance(payload.get("keeper_fact"), dict) else {}
    keeper_id = str(keeper.get("id") or "")
    superseded_fact_ids = [str(item) for item in payload.get("superseded_fact_ids") or [] if item]
    fact_ids = stable_unique([keeper_id, *superseded_fact_ids])
    if not keeper_id or not superseded_fact_ids:
        raise ValueError("fact_merge requires keeper_fact.id and superseded_fact_ids")
    old_facts: list[dict[str, Any]] = []
    for fact_id in fact_ids:
        row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
        if not row:
            raise ValueError(f"fact not found: {fact_id}")
        old_facts.append(row_to_fact(row))
    apply_fact_upsert(conn, {"fact": {**keeper, "status": keeper.get("status") or "active"}})
    for fact_id in superseded_fact_ids:
        old = next(fact for fact in old_facts if fact["id"] == fact_id)
        apply_fact_upsert(
            conn,
            {
                "fact": {
                    **old,
                    "status": "superseded",
                    "conflict_group_id": None,
                }
            },
        )
    rebuild_fact_retrieval_index(conn)
    return fact_ids, {"restore_facts": old_facts}


def apply_display_contested(
    conn: Any, payload: dict[str, Any], action_id: str
) -> tuple[list[str], dict[str, Any]]:
    from .wiki_facts import (
        ensure_open_conflict_question,
        rebuild_fact_retrieval_index,
        row_to_fact,
    )

    fact_ids = [str(item) for item in payload.get("fact_ids") or [] if item]
    conflict_group_id = str(payload.get("conflict_group_id") or new_id("factconflict"))
    if len(fact_ids) < 2:
        raise ValueError("display_contested requires at least two fact_ids")
    old_facts: list[dict[str, Any]] = []
    facts: list[dict[str, Any]] = []
    for fact_id in fact_ids:
        row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
        if not row:
            raise ValueError(f"fact not found: {fact_id}")
        fact = row_to_fact(row)
        old_facts.append(fact)
        facts.append({**fact, "status": "conflicted", "conflict_group_id": conflict_group_id})
        apply_fact_upsert(
            conn,
            {
                "fact": {
                    **fact,
                    "status": "conflicted",
                    "conflict_group_id": conflict_group_id,
                }
            },
        )
    question_id = ensure_open_conflict_question(
        conn, conflict_group_id, facts, fact_ids, action_id=action_id
    )
    rebuild_fact_retrieval_index(conn)
    inverse: dict[str, Any] = {"restore_facts": old_facts}
    if question_id:
        inverse["delete_question_ids"] = [question_id]
    return fact_ids, inverse


def apply_edit_contract(conn: Any, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    contract = payload.get("contract") if isinstance(payload.get("contract"), dict) else payload
    contract_id = str(contract.get("id") or new_id("contract"))
    existing = conn.execute(
        "SELECT * FROM page_contracts WHERE id = ?", (contract_id,)
    ).fetchone()
    inverse = (
        {"restore_contract": dict(existing)}
        if existing
        else {"delete_contract_ids": [contract_id]}
    )
    timestamp = now_iso()
    values = (
        contract_id,
        str(contract.get("page_hint") or (existing["page_hint"] if existing else "")),
        contract.get("canonical_entity", existing["canonical_entity"] if existing else None),
        contract.get("page_scope", existing["page_scope"] if existing else None),
        contract.get("retrieval_purpose", existing["retrieval_purpose"] if existing else None),
        contract.get("what_belongs_here", existing["what_belongs_here"] if existing else None),
        contract.get("what_does_not_belong_here", existing["what_does_not_belong_here"] if existing else None),
        contract.get("freshness_policy", existing["freshness_policy"] if existing else None),
        dumps(contract.get("related_pages", loads(existing["related_pages"], []) if existing else [])),
        int(contract.get("version", existing["version"] if existing else 1)),
        str(contract.get("status", existing["status"] if existing else "active")),
        existing["created_at"] if existing else timestamp,
        timestamp,
    )
    if not values[1]:
        raise ValueError("contract page_hint is required")
    conn.execute(
        """
        INSERT OR REPLACE INTO page_contracts(
          id, page_hint, canonical_entity, page_scope, retrieval_purpose,
          what_belongs_here, what_does_not_belong_here, freshness_policy,
          related_pages, version, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )
    return contract_id, inverse


def apply_synthesize_page(conn: Any, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    synthesis = payload.get("synthesis") if isinstance(payload.get("synthesis"), dict) else payload
    synthesis_id = str(synthesis.get("id") or new_id("synthesis"))
    page_hint = str(synthesis.get("page_hint") or "")
    if not page_hint:
        raise ValueError("synthesize_page requires page_hint")
    conn.execute(
        "UPDATE wiki_page_syntheses SET stale = 1 WHERE page_hint = ? AND stale = 0",
        (page_hint,),
    )
    conn.execute(
        """
        INSERT INTO wiki_page_syntheses(
          id, page_hint, synthesis_markdown, fact_ids, fact_hash,
          model, prompt_version, generated_at, stale
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            synthesis_id,
            page_hint,
            str(synthesis.get("synthesis_markdown") or ""),
            dumps(synthesis.get("fact_ids") or []),
            synthesis.get("fact_hash"),
            synthesis.get("model"),
            synthesis.get("prompt_version"),
            now_iso(),
            int(bool(synthesis.get("stale", False))),
        ),
    )
    return page_hint, {"delete_synthesis_ids": [synthesis_id]}


def apply_page_merge(conn: Any, payload: dict[str, Any]) -> tuple[list[str], list[str], list[str], dict[str, Any]]:
    from .wiki_facts import rebuild_fact_retrieval_index

    topology = topology_payload(payload)
    page_hints = stable_unique([str(item) for item in topology.get("page_hints") or [] if item])
    destination = str(topology.get("destination_page_hint") or "")
    if not destination:
        destination = choose_merge_destination(conn, page_hints)
    source_pages = [page_hint for page_hint in page_hints if page_hint != destination]
    if not destination or not source_pages:
        raise ValueError("page_merge requires at least one source page and one destination page")

    old_facts = facts_for_page_hints(conn, source_pages)
    old_contracts = contracts_for_page_hints(conn, page_hints)
    old_syntheses = syntheses_for_page_hints(conn, page_hints)
    timestamp = now_iso()
    fact_ids = [fact["id"] for fact in old_facts]
    for fact in old_facts:
        conn.execute(
            """
            UPDATE facts
            SET page_hint = ?, metadata = ?, last_seen_at = ?
            WHERE id = ?
            """,
            (
                destination,
                dumps(topology_metadata(fact.get("metadata"), "page_merge", old_page_hint=fact.get("page_hint"))),
                timestamp,
                fact["id"],
            ),
        )
    for contract in old_contracts:
        if contract["page_hint"] in source_pages:
            conn.execute(
                "UPDATE page_contracts SET status = ?, updated_at = ? WHERE id = ?",
                ("superseded", timestamp, contract["id"]),
            )
    mark_syntheses_stale(conn, page_hints)
    rebuild_fact_retrieval_index(conn)
    return (
        fact_ids,
        [str(contract["id"]) for contract in old_contracts],
        stable_unique([*source_pages, destination]),
        {
            "restore_facts": old_facts,
            "restore_contracts": old_contracts,
            "restore_syntheses": old_syntheses,
        },
    )


def apply_page_split(conn: Any, payload: dict[str, Any]) -> tuple[list[str], list[str], dict[str, Any]]:
    from .wiki_facts import rebuild_fact_retrieval_index

    topology = topology_payload(payload)
    page_hint = str(
        topology.get("page_hint")
        or topology.get("source_page_hint")
        or first_value(topology.get("page_hints"))
        or ""
    )
    if not page_hint:
        raise ValueError("page_split requires page_hint")
    facts = facts_for_page_hints(conn, [page_hint], statuses=["active"])
    movable = [
        fact
        for fact in facts
        if normalized_section(str(fact.get("section_hint") or "")) not in {"", "summary", "unsectioned"}
    ]
    if not movable:
        raise ValueError("page_split requires active non-summary facts to move")
    old_syntheses = syntheses_for_page_hints(conn, [page_hint])
    moved_pages: list[str] = []
    timestamp = now_iso()
    for fact in movable:
        destination = split_destination_page_hint(page_hint, str(fact.get("section_hint") or "section"))
        moved_pages.append(destination)
        conn.execute(
            """
            UPDATE facts
            SET page_hint = ?, metadata = ?, last_seen_at = ?
            WHERE id = ?
            """,
            (
                destination,
                dumps(topology_metadata(fact.get("metadata"), "page_split", old_page_hint=page_hint)),
                timestamp,
                fact["id"],
            ),
        )
    affected_pages = stable_unique([page_hint, *moved_pages])
    old_syntheses.extend(syntheses_for_page_hints(conn, moved_pages))
    mark_syntheses_stale(conn, affected_pages)
    rebuild_fact_retrieval_index(conn)
    return (
        [str(fact["id"]) for fact in movable],
        affected_pages,
        {
            "restore_facts": movable,
            "restore_syntheses": old_syntheses,
        },
    )


def apply_rename_page(conn: Any, payload: dict[str, Any]) -> tuple[list[str], list[str], list[str], dict[str, Any]]:
    from .wiki_facts import rebuild_fact_retrieval_index

    topology = topology_payload(payload)
    source = str(topology.get("from_page_hint") or topology.get("source_page_hint") or topology.get("old_page_hint") or "")
    destination = str(topology.get("to_page_hint") or topology.get("destination_page_hint") or topology.get("new_page_hint") or "")
    if not source or not destination:
        raise ValueError("rename_page requires source and destination page hints")
    old_facts = facts_for_page_hints(conn, [source])
    old_contracts = contracts_for_page_hints(conn, [source])
    old_syntheses = syntheses_for_page_hints(conn, [source, destination])
    timestamp = now_iso()
    conn.execute(
        """
        UPDATE facts
        SET page_hint = ?, metadata = ?, last_seen_at = ?
        WHERE page_hint = ?
        """,
        (
            destination,
            dumps(topology_metadata({}, "rename_page", old_page_hint=source)),
            timestamp,
            source,
        ),
    )
    for fact in old_facts:
        merged_metadata = topology_metadata(fact.get("metadata"), "rename_page", old_page_hint=source)
        conn.execute("UPDATE facts SET metadata = ? WHERE id = ?", (dumps(merged_metadata), fact["id"]))
    conn.execute("UPDATE page_contracts SET page_hint = ?, updated_at = ? WHERE page_hint = ?", (destination, timestamp, source))
    conn.execute("UPDATE wiki_page_syntheses SET page_hint = ?, stale = 1 WHERE page_hint = ?", (destination, source))
    conn.execute("UPDATE wiki_page_syntheses SET stale = 1 WHERE page_hint = ?", (destination,))
    conn.execute("UPDATE wiki_pages SET path = ? WHERE path = ?", (destination, source))
    rebuild_fact_retrieval_index(conn)
    return (
        [str(fact["id"]) for fact in old_facts],
        [str(contract["id"]) for contract in old_contracts],
        [source, destination],
        {
            "restore_facts": old_facts,
            "restore_contracts": old_contracts,
            "restore_syntheses": old_syntheses,
            "restore_wiki_page_paths": [{"old_path": source, "new_path": destination}],
        },
    )


def topology_payload(payload: dict[str, Any]) -> dict[str, Any]:
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else None
    return candidate or payload


def first_value(value: Any) -> Any:
    if isinstance(value, list) and value:
        return value[0]
    return None


def choose_merge_destination(conn: Any, page_hints: list[str]) -> str:
    if len(page_hints) < 2:
        return page_hints[0] if page_hints else ""
    counts = fact_counts_for_pages(conn, page_hints)
    contract_pages = set(contract_page_hints(conn, page_hints))
    return sorted(
        page_hints,
        key=lambda page_hint: (
            page_hint not in contract_pages,
            -counts.get(page_hint, 0),
            len(page_hint),
            page_hint,
        ),
    )[0]


def fact_counts_for_pages(conn: Any, page_hints: list[str]) -> dict[str, int]:
    if not page_hints:
        return {}
    placeholders = ",".join("?" for _ in page_hints)
    return {
        str(row["page_hint"]): int(row["count"])
        for row in conn.execute(
            f"""
            SELECT page_hint, COUNT(*) AS count
            FROM facts
            WHERE page_hint IN ({placeholders})
              AND status = 'active'
            GROUP BY page_hint
            """,
            page_hints,
        )
    }


def contract_page_hints(conn: Any, page_hints: list[str]) -> list[str]:
    if not page_hints:
        return []
    placeholders = ",".join("?" for _ in page_hints)
    return [
        str(row["page_hint"])
        for row in conn.execute(
            f"""
            SELECT DISTINCT page_hint
            FROM page_contracts
            WHERE page_hint IN ({placeholders})
              AND status = 'active'
            """,
            page_hints,
        )
    ]


def facts_for_page_hints(
    conn: Any,
    page_hints: list[str],
    *,
    statuses: list[str] | None = None,
) -> list[dict[str, Any]]:
    from .wiki_facts import row_to_fact

    page_hints = stable_unique(page_hints)
    if not page_hints:
        return []
    page_placeholders = ",".join("?" for _ in page_hints)
    params: list[Any] = list(page_hints)
    status_clause = ""
    if statuses:
        status_placeholders = ",".join("?" for _ in statuses)
        status_clause = f" AND status IN ({status_placeholders})"
        params.extend(statuses)
    return [
        row_to_fact(row)
        for row in conn.execute(
            f"""
            SELECT *
            FROM facts
            WHERE page_hint IN ({page_placeholders})
            {status_clause}
            ORDER BY page_hint, id
            """,
            params,
        )
    ]


def contracts_for_page_hints(conn: Any, page_hints: list[str]) -> list[dict[str, Any]]:
    page_hints = stable_unique(page_hints)
    if not page_hints:
        return []
    placeholders = ",".join("?" for _ in page_hints)
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT *
            FROM page_contracts
            WHERE page_hint IN ({placeholders})
            ORDER BY page_hint, id
            """,
            page_hints,
        )
    ]


def syntheses_for_page_hints(conn: Any, page_hints: list[str]) -> list[dict[str, Any]]:
    page_hints = stable_unique(page_hints)
    if not page_hints:
        return []
    placeholders = ",".join("?" for _ in page_hints)
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT *
            FROM wiki_page_syntheses
            WHERE page_hint IN ({placeholders})
            ORDER BY page_hint, generated_at, id
            """,
            page_hints,
        )
    ]


def mark_syntheses_stale(conn: Any, page_hints: list[str]) -> None:
    page_hints = stable_unique(page_hints)
    if not page_hints:
        return
    placeholders = ",".join("?" for _ in page_hints)
    conn.execute(
        f"UPDATE wiki_page_syntheses SET stale = 1 WHERE page_hint IN ({placeholders})",
        page_hints,
    )


def topology_metadata(metadata: Any, operation: str, *, old_page_hint: Any) -> dict[str, Any]:
    base = loads(metadata, {}) if isinstance(metadata, str) else metadata
    if not isinstance(base, dict):
        base = {}
    history = list(base.get("topology_history") or [])
    history.append({"operation": operation, "old_page_hint": old_page_hint, "at": now_iso()})
    return {**base, "topology_history": history}


def normalized_section(section: str) -> str:
    return section.strip().lower()


def split_destination_page_hint(page_hint: str, section: str) -> str:
    path = Path(page_hint)
    suffix = path.suffix or ".md"
    stem = path.stem or "page"
    section_slug = slugify_text(section)
    return (path.with_name(f"{stem}-{section_slug}{suffix}")).as_posix()


def slugify_text(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or "section"


def restore_contract(conn: Any, contract: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO page_contracts(
          id, page_hint, canonical_entity, page_scope, retrieval_purpose,
          what_belongs_here, what_does_not_belong_here, freshness_policy,
          related_pages, version, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            contract["id"],
            contract["page_hint"],
            contract.get("canonical_entity"),
            contract.get("page_scope"),
            contract.get("retrieval_purpose"),
            contract.get("what_belongs_here"),
            contract.get("what_does_not_belong_here"),
            contract.get("freshness_policy"),
            contract.get("related_pages", "[]"),
            contract.get("version", 1),
            contract.get("status", "active"),
            contract.get("created_at") or now_iso(),
            contract.get("updated_at"),
        ),
    )


def restore_synthesis(conn: Any, synthesis: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO wiki_page_syntheses(
          id, page_hint, synthesis_markdown, fact_ids, fact_hash,
          model, prompt_version, generated_at, stale
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            synthesis["id"],
            synthesis["page_hint"],
            synthesis.get("synthesis_markdown", ""),
            synthesis.get("fact_ids", "[]"),
            synthesis.get("fact_hash"),
            synthesis.get("model"),
            synthesis.get("prompt_version"),
            synthesis.get("generated_at") or now_iso(),
            int(synthesis.get("stale") or 0),
        ),
    )


def apply_inverse(conn: Any, inverse: dict[str, Any]) -> None:
    from .wiki_facts import rebuild_fact_retrieval_index

    for fact_id in inverse.get("delete_fact_ids") or []:
        conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
    for fact in inverse.get("restore_facts") or []:
        apply_fact_upsert(conn, {"fact": fact})
    if inverse.get("restore_fact"):
        apply_fact_upsert(conn, {"fact": inverse["restore_fact"]})
    for routing in inverse.get("restore_fact_routing") or []:
        conn.execute(
            """
            UPDATE facts
            SET page_hint = ?, entity_key = ?, section_hint = ?,
                metadata = COALESCE(?, metadata), last_seen_at = ?
            WHERE id = ?
            """,
            (
                routing.get("old_page_hint"),
                routing.get("old_entity_key"),
                routing.get("old_section_hint"),
                dumps(routing["old_metadata"]) if "old_metadata" in routing else None,
                now_iso(),
                routing.get("fact_id"),
            ),
        )
    for contract_id in inverse.get("delete_contract_ids") or []:
        conn.execute("DELETE FROM page_contracts WHERE id = ?", (contract_id,))
    if inverse.get("restore_contract"):
        contract = dict(inverse["restore_contract"])
        restore_contract(conn, contract)
    for contract in inverse.get("restore_contracts") or []:
        restore_contract(conn, dict(contract))
    for synthesis in inverse.get("restore_syntheses") or []:
        restore_synthesis(conn, dict(synthesis))
    for path_restore in inverse.get("restore_wiki_page_paths") or []:
        conn.execute(
            "UPDATE wiki_pages SET path = ? WHERE path = ?",
            (path_restore.get("old_path"), path_restore.get("new_path")),
        )
    for synthesis_id in inverse.get("delete_synthesis_ids") or []:
        conn.execute("DELETE FROM wiki_page_syntheses WHERE id = ?", (synthesis_id,))
    for question_id in inverse.get("delete_question_ids") or []:
        conn.execute("DELETE FROM open_questions WHERE id = ?", (question_id,))
    rebuild_fact_retrieval_index(conn)


def mark_needs_human(
    paths: BrainPaths, action_id: str, decision: PolicyDecision, reason: str
) -> None:
    with connection(paths.sqlite_path) as conn:
        action = load_action(conn, action_id)
        create_action_residue(conn, action, "policy_escalation", reason)
        conn.execute(
            """
            UPDATE cos_actions
            SET status = 'needs_human', policy_id = ?, policy_version = ?,
                policy_decision = ?, autonomy_level = ?
            WHERE id = ?
            """,
            (
                decision.policy_id,
                decision.policy_version,
                decision.policy_decision,
                decision.autonomy_level,
                action_id,
            ),
        )


def create_action_residue(
    conn: Any, action: dict[str, Any], kind: str, question: str
) -> str:
    question_id = new_id("question")
    conn.execute(
        """
        INSERT INTO open_questions(
          id, kind, entity_key, page_hint, fact_ids, question, options, status,
          context, action_id, recommended_action, risk_tier, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            question_id,
            kind,
            None,
            (action.get("target_page_paths") or [None])[0],
            dumps(action.get("target_fact_ids") or []),
            question,
            dumps([]),
            "needs_human",
            dumps({"action_id": action["id"], "action_type": action["action_type"]}),
            action["id"],
            dumps({"action_type": action["action_type"], "payload": action_payload(action)}),
            action.get("risk_tier"),
            now_iso(),
        ),
    )
    return question_id


def target_state_hash(
    conn: Any,
    *,
    target_fact_ids: list[str],
    target_contract_ids: list[str],
    target_page_paths: list[str],
) -> str:
    state: dict[str, Any] = {"facts": [], "contracts": [], "syntheses": []}
    if target_fact_ids:
        placeholders = ",".join("?" for _ in target_fact_ids)
        state["facts"] = [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM facts WHERE id IN ({placeholders}) ORDER BY id",
                target_fact_ids,
            )
        ]
    if target_contract_ids:
        placeholders = ",".join("?" for _ in target_contract_ids)
        state["contracts"] = [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM page_contracts WHERE id IN ({placeholders}) ORDER BY id",
                target_contract_ids,
            )
        ]
    if target_page_paths:
        placeholders = ",".join("?" for _ in target_page_paths)
        state["syntheses"] = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT *
                FROM wiki_page_syntheses
                WHERE page_hint IN ({placeholders})
                ORDER BY page_hint, generated_at, id
                """,
                target_page_paths,
            )
        ]
    return hashlib.sha256(dumps(state).encode("utf-8")).hexdigest()


def get_action(paths: BrainPaths, action_id: str) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        return load_action(conn, action_id)


def recent_actions(paths: BrainPaths, *, limit: int = 50) -> list[dict[str, Any]]:
    with connection(paths.sqlite_path) as conn:
        return [
            row_to_action(row)
            for row in conn.execute(
                """
                SELECT *
                FROM cos_actions
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        ]


def load_action(conn: Any, action_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM cos_actions WHERE id = ?", (action_id,)).fetchone()
    if not row:
        raise ValueError(f"cos action not found: {action_id}")
    return row_to_action(row)


def row_to_action(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "action_type": row["action_type"],
        "status": row["status"],
        "target_fact_ids": loads(row["target_fact_ids"], []),
        "target_page_paths": loads(row["target_page_paths"], []),
        "target_contract_ids": loads(row["target_contract_ids"], []),
        "action_features": loads(row["action_features"], {}),
        "proposed_by": row["proposed_by"],
        "critic_by": row["critic_by"],
        "critic_decision": row["critic_decision"],
        "confidence": row["confidence"],
        "risk_tier": row["risk_tier"],
        "policy_id": row["policy_id"],
        "policy_version": row["policy_version"],
        "policy_decision": row["policy_decision"],
        "autonomy_level": row["autonomy_level"],
        "inverse_action_json": loads(row["inverse_action_json"], {}),
        "evidence_json": loads(row["evidence_json"], {}),
        "applied_state_hash": row["applied_state_hash"],
        "audit_status": row["audit_status"],
        "created_at": row["created_at"],
        "applied_at": row["applied_at"],
        "reverted_at": row["reverted_at"],
    }


def action_payload(action: dict[str, Any]) -> dict[str, Any]:
    evidence = action.get("evidence_json") or {}
    payload = evidence.get("payload") or {}
    return payload if isinstance(payload, dict) else {}


def validate_action_type(action_type: str) -> None:
    if action_type not in ACTION_TYPE_SPECS:
        raise ValueError(f"unknown cos action type: {action_type}")


def existing_value_getter(row: Any | None) -> Any:
    def get(key: str, default: Any = None) -> Any:
        if row is None:
            return default
        try:
            value = row[key]
        except (KeyError, IndexError, TypeError):
            return default
        return default if value is None else value

    return get


def stable_unique(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output
