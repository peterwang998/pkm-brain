from __future__ import annotations

import hashlib
from typing import Any

from .cos_policy import PolicyDecision, evaluate_policy
from .db import connection, dumps, loads
from .paths import BrainPaths
from .util import new_id, now_iso


APPLIED_STATUSES = {"applied", "auto_applied"}


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
    action_id = new_id("cosact")
    evidence_json = dict(evidence or {})
    if action_payload is not None:
        evidence_json["payload"] = action_payload
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
                dumps(action_features or {}),
                proposed_by,
                confidence,
                risk_tier,
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
) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        action = load_action(conn, action_id)
        decision = evaluate_policy(
            conn, action["action_type"], action.get("action_features") or {}
        )
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
        return apply_action(paths, action_id, applied_status="applied")
    mark_needs_human(paths, action_id, decision, decision.reason or "requires human decision")
    return get_action(paths, action_id)


def apply_action(
    paths: BrainPaths, action_id: str, *, applied_status: str = "applied"
) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        action = load_action(conn, action_id)
        if action["status"] in APPLIED_STATUSES:
            return action
        payload = action_payload(action)
        inverse: dict[str, Any] = {}
        target_fact_ids = list(action.get("target_fact_ids") or [])
        target_contract_ids = list(action.get("target_contract_ids") or [])
        target_page_paths = list(action.get("target_page_paths") or [])

        if action["action_type"] == "fact_upsert":
            fact_id, inverse = apply_fact_upsert(conn, payload)
            target_fact_ids = stable_unique([*target_fact_ids, fact_id])
        elif action["action_type"] == "rehome_fact":
            fact_id, inverse = apply_rehome_fact(conn, payload)
            target_fact_ids = stable_unique([*target_fact_ids, fact_id])
        elif action["action_type"] == "edit_contract":
            contract_id, inverse = apply_edit_contract(conn, payload)
            target_contract_ids = stable_unique([*target_contract_ids, contract_id])
        elif action["action_type"] == "synthesize_page":
            page_hint, inverse = apply_synthesize_page(conn, payload)
            target_page_paths = stable_unique([*target_page_paths, page_hint])
        elif action["action_type"] == "canonicalize_page":
            inverse = {"noop": True}
        else:
            conn.execute(
                "UPDATE cos_actions SET status = ? WHERE id = ?",
                ("failed", action_id),
            )
            raise ValueError(f"unsupported action type: {action['action_type']}")

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
    conn.execute(
        """
        UPDATE facts
        SET page_hint = COALESCE(?, page_hint),
            entity_key = COALESCE(?, entity_key),
            section_hint = COALESCE(?, section_hint),
            last_seen_at = ?
        WHERE id = ?
        """,
        (
            payload.get("page_hint"),
            payload.get("entity_key"),
            payload.get("section_hint"),
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
                }
            ]
        },
    )


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


def apply_inverse(conn: Any, inverse: dict[str, Any]) -> None:
    from .wiki_facts import rebuild_fact_retrieval_index

    for fact_id in inverse.get("delete_fact_ids") or []:
        conn.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
    if inverse.get("restore_fact"):
        apply_fact_upsert(conn, {"fact": inverse["restore_fact"]})
    for routing in inverse.get("restore_fact_routing") or []:
        conn.execute(
            """
            UPDATE facts
            SET page_hint = ?, entity_key = ?, section_hint = ?, last_seen_at = ?
            WHERE id = ?
            """,
            (
                routing.get("old_page_hint"),
                routing.get("old_entity_key"),
                routing.get("old_section_hint"),
                now_iso(),
                routing.get("fact_id"),
            ),
        )
    for contract_id in inverse.get("delete_contract_ids") or []:
        conn.execute("DELETE FROM page_contracts WHERE id = ?", (contract_id,))
    if inverse.get("restore_contract"):
        contract = dict(inverse["restore_contract"])
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
    for synthesis_id in inverse.get("delete_synthesis_ids") or []:
        conn.execute("DELETE FROM wiki_page_syntheses WHERE id = ?", (synthesis_id,))
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
