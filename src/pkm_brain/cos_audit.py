from __future__ import annotations

import hashlib
import json
from typing import Any

from .cos_actions import (
    action_payload,
    record_action_audit,
    revert_action,
    row_to_action,
)
from .cos_policy import demote_policy_version
from .db import connection, loads
from .llm import (
    LLMProvider,
    LLMProviderError,
    complete_json,
    cos_role_provider_configured,
    get_cos_role_provider,
)
from .paths import BrainPaths


COS_AUDIT_STUB_NOTE = "Sampled audit is not executed; no independent critic/auditor provider is configured."
COS_AUDIT_CONFIGURED_NOTE = "Independent auditor configured; sampled actions are judged before audit status is recorded."
AUDITOR_SCHEMA = {
    "type": "object",
    "required": ["audits"],
    "properties": {
        "audits": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["action_id", "decision", "rationale"],
            },
        }
    },
}
OK_DECISIONS = {"ok", "pass", "passed", "correct", "safe", "sampled_ok"}
BAD_DECISIONS = {
    "bad",
    "fail",
    "failed",
    "incorrect",
    "unsafe",
    "unsupported",
    "policy_violation",
    "sampled_bad",
}
AUDITOR_MAX_ACTIONS_PER_BATCH = 8
AUDITOR_MAX_BATCH_CHARS = 180_000
AUDITOR_MAX_CARD_CHARS = 48_000
AUDITOR_MAX_PAYLOAD_CHARS = 16_000


def run_sampled_audit(
    paths: BrainPaths,
    *,
    limit: int = 25,
    auto_revert_bad: bool = False,
    llm_provider: LLMProvider | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        actions = load_audit_sample(conn, limit)
    if not auditor_configured(paths, llm_provider=llm_provider, provider=provider):
        return {
            "status": "ok",
            "mode": "stub",
            "note": COS_AUDIT_STUB_NOTE,
            "sampled": len(actions),
            "audited": [],
            "bad_action_ids": [],
            "unscoped_bad_action_ids": [],
            "missing_action_ids": [action["id"] for action in actions],
            "batch_count": 0,
            "audit_errors": [],
            "demoted_policy_version": None,
            "reverted": [],
        }

    audited: list[dict[str, Any]] = []
    bad_action_ids: list[str] = []
    missing_action_ids: list[str] = []
    audit_errors: list[dict[str, Any]] = []
    batch_count = 0
    audit_provider = llm_provider
    if actions:
        audit_provider = get_cos_role_provider(
            paths, "auditor", provider=provider, llm_provider=llm_provider
        )
        cards = build_auditor_cards(paths, actions)
        judgments: dict[str, dict[str, Any]] = {}
        for batch in auditor_card_batches(cards):
            batch_count += 1
            try:
                parsed = complete_json(
                    auditor_prompt(batch),
                    schema=AUDITOR_SCHEMA,
                    provider=provider,
                    role="auditor",
                    llm_provider=audit_provider,
                    paths=paths,
                )
            except LLMProviderError as exc:
                audit_errors.append(
                    {
                        "action_ids": [str(card["action_id"]) for card in batch],
                        "error": clipped(exc, 1000),
                    }
                )
                continue
            judgments.update(auditor_judgments_by_action_id(parsed))
    else:
        judgments = {}
    for action in actions:
        judgment = normalize_auditor_judgment(judgments.get(action["id"]))
        if judgment is None:
            missing_action_ids.append(action["id"])
            continue
        audit_status = str(judgment["audit_status"])
        audited.append(
            record_action_audit(
                paths,
                action["id"],
                audit_status,
                metadata={
                    "source": "auditor_llm",
                    "decision": judgment["decision"],
                    "raw_decision": judgment["raw_decision"],
                    "rationale": judgment["rationale"],
                    "confidence": judgment.get("confidence"),
                    "risk_tier": judgment.get("risk_tier"),
                    "provider": auditor_provider_name(
                        paths, llm_provider=audit_provider, provider=provider
                    ),
                    "model": getattr(audit_provider, "model", None),
                },
            )
        )
        if audit_status == "sampled_bad":
            bad_action_ids.append(action["id"])
    demoted_version = None
    demotion_evidence: list[dict[str, Any]] = []
    unscoped_bad_action_ids: list[str] = []
    if bad_action_ids:
        with connection(paths.sqlite_path) as conn:
            unscoped_bad_action_ids = [
                str(action["id"])
                for action in load_actions_for_audit_ids(conn, bad_action_ids)
                if not action.get("policy_id") or action.get("policy_version") is None
            ]
            demotion_evidence = demotion_threshold_breaches(conn, bad_action_ids)
            if demotion_evidence:
                demoted_version = demote_policy_version(
                    conn,
                    reason=(
                        f"{len(bad_action_ids)} sampled actions were bad; "
                        f"{len(demotion_evidence)} policy group(s) exceeded demotion threshold"
                    ),
                    policy_groups=demotion_evidence,
                )
    reverted: list[dict[str, Any]] = []
    if auto_revert_bad:
        for action_id in bad_action_ids:
            reverted.append(revert_action(paths, action_id))
    return {
        "status": "incomplete" if missing_action_ids else "ok",
        "mode": "configured",
        "note": COS_AUDIT_CONFIGURED_NOTE,
        "sampled": len(actions),
        "audited": audited,
        "bad_action_ids": bad_action_ids,
        "unscoped_bad_action_ids": unscoped_bad_action_ids,
        "missing_action_ids": missing_action_ids,
        "batch_count": batch_count,
        "audit_errors": audit_errors,
        "demotion_evidence": demotion_evidence,
        "demoted_policy_version": demoted_version,
        "reverted": reverted,
    }


def load_audit_sample(conn: Any, limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    candidates = [
        row_to_action(row)
        for row in conn.execute(
            """
            SELECT *
            FROM cos_actions
            WHERE status IN ('applied', 'auto_applied')
              AND audit_status = 'unaudited'
            ORDER BY
              CASE COALESCE(risk_tier, '')
                WHEN 'high' THEN 0
                WHEN 'medium' THEN 1
                ELSE 2
              END,
              applied_at DESC
            LIMIT ?
            """,
            (max(limit * 10, limit),),
        )
    ]
    selected: list[dict[str, Any]] = []
    for action in candidates:
        if action_in_audit_sample(conn, action):
            selected.append(action)
        if len(selected) >= limit:
            break
    return selected


def action_in_audit_sample(conn: Any, action: dict[str, Any]) -> bool:
    if action.get("risk_tier") == "high":
        return True
    rate = action_audit_sample_rate(conn, action)
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    return stable_sample_fraction(str(action["id"])) < rate


def action_audit_sample_rate(conn: Any, action: dict[str, Any]) -> float:
    policy_id = action.get("policy_id")
    policy_version = action.get("policy_version")
    if not policy_id or policy_version is None:
        return 1.0
    row = conn.execute(
        """
        SELECT audit_sample_rate
        FROM cos_policy
        WHERE id = ? AND version = ?
        """,
        (policy_id, policy_version),
    ).fetchone()
    if not row or row["audit_sample_rate"] is None:
        return 1.0
    return min(1.0, max(0.0, float(row["audit_sample_rate"])))


def demotion_threshold_breaches(
    conn: Any, bad_action_ids: list[str]
) -> list[dict[str, Any]]:
    bad_actions = load_actions_for_audit_ids(conn, bad_action_ids)
    policy_groups = {
        (action.get("policy_id"), action.get("policy_version"))
        for action in bad_actions
        if action.get("policy_id") and action.get("policy_version") is not None
    }
    breaches: list[dict[str, Any]] = []
    for policy_id, policy_version in sorted(policy_groups, key=lambda item: str(item)):
        stats = audit_stats_for_policy_group(conn, policy_id, policy_version)
        if stats["audited_count"] <= 0:
            continue
        if stats["bad_rate"] > stats["demotion_threshold"]:
            breaches.append(stats)
    return breaches


def load_actions_for_audit_ids(
    conn: Any, action_ids: list[str]
) -> list[dict[str, Any]]:
    if not action_ids:
        return []
    placeholders = ",".join("?" for _ in action_ids)
    return [
        row_to_action(row)
        for row in conn.execute(
            f"SELECT * FROM cos_actions WHERE id IN ({placeholders})",
            action_ids,
        )
    ]


def audit_stats_for_policy_group(
    conn: Any, policy_id: str | None, policy_version: int | None
) -> dict[str, Any]:
    if policy_id is None or policy_version is None:
        rows = conn.execute(
            """
            SELECT audit_status
            FROM cos_actions
            WHERE policy_id IS NULL
              AND policy_version IS NULL
              AND audit_status IN ('sampled_ok', 'sampled_bad')
            """
        ).fetchall()
        threshold = 0.0
    else:
        rows = conn.execute(
            """
            SELECT audit_status
            FROM cos_actions
            WHERE policy_id = ?
              AND policy_version = ?
              AND audit_status IN ('sampled_ok', 'sampled_bad')
            """,
            (policy_id, policy_version),
        ).fetchall()
        threshold = policy_demotion_threshold(conn, str(policy_id), int(policy_version))
    audited_count = len(rows)
    bad_count = sum(1 for row in rows if row["audit_status"] == "sampled_bad")
    bad_rate = bad_count / audited_count if audited_count else 0.0
    return {
        "policy_id": policy_id,
        "policy_version": policy_version,
        "audited_count": audited_count,
        "bad_count": bad_count,
        "bad_rate": round(bad_rate, 4),
        "demotion_threshold": threshold,
    }


def policy_demotion_threshold(conn: Any, policy_id: str, policy_version: int) -> float:
    row = conn.execute(
        """
        SELECT demotion_threshold
        FROM cos_policy
        WHERE id = ? AND version = ?
        """,
        (policy_id, policy_version),
    ).fetchone()
    if not row or row["demotion_threshold"] is None:
        return 0.0
    return min(1.0, max(0.0, float(row["demotion_threshold"])))


def stable_sample_fraction(value: str) -> float:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64)


def auditor_configured(
    paths: BrainPaths,
    *,
    llm_provider: LLMProvider | None,
    provider: str | None,
) -> bool:
    return cos_role_provider_configured(
        paths, "auditor", llm_provider=llm_provider, provider=provider
    )


def auditor_provider_name(
    paths: BrainPaths,
    *,
    llm_provider: LLMProvider | None,
    provider: str | None,
) -> str | None:
    if llm_provider is not None:
        return getattr(llm_provider, "name", None)
    if provider:
        return provider
    configured_provider = get_cos_role_provider(paths, "auditor")
    return (
        getattr(configured_provider, "name", None)
        if configured_provider is not None
        else None
    )


def build_auditor_cards(
    paths: BrainPaths, actions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    with connection(paths.sqlite_path) as conn:
        return [
            bounded_auditor_card(auditor_candidate_card(conn, action))
            for action in actions
        ]


def auditor_card_batches(
    cards: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for card in cards:
        candidate = [*current, card]
        if current and (
            len(candidate) > AUDITOR_MAX_ACTIONS_PER_BATCH
            or len(auditor_prompt(candidate)) > AUDITOR_MAX_BATCH_CHARS
        ):
            batches.append(current)
            current = [card]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


def bounded_auditor_card(card: dict[str, Any]) -> dict[str, Any]:
    compact = compact_audit_value(card)
    if json_size(compact) <= AUDITOR_MAX_CARD_CHARS:
        return compact

    target_state = card.get("target_state") or {}
    bounded = {
        "action_id": card.get("action_id"),
        "action_type": card.get("action_type"),
        "status": card.get("status"),
        "proposed_by": card.get("proposed_by"),
        "confidence": card.get("confidence"),
        "risk_tier": card.get("risk_tier"),
        "policy": compact_audit_value(card.get("policy")),
        "critic": compact_audit_value(card.get("critic")),
        "features": compact_audit_value(card.get("features")),
        "payload": bounded_auditor_payload(card.get("payload")),
        "targets": compact_audit_value(card.get("targets")),
        "target_state": {
            "facts": compact_audit_value(list(target_state.get("facts") or [])[:12]),
            "contracts": compact_audit_value(
                list(target_state.get("contracts") or [])[:8]
            ),
            "syntheses": compact_audit_value(
                list(target_state.get("syntheses") or [])[:4]
            ),
            "counts": {
                key: len(list(target_state.get(key) or []))
                for key in ("facts", "contracts", "syntheses")
            },
        },
        "inverse_action": compact_audit_value(card.get("inverse_action")),
        "applied_state_hash": card.get("applied_state_hash"),
        "card_truncated": True,
    }
    if json_size(bounded) <= AUDITOR_MAX_CARD_CHARS:
        return bounded

    bounded["target_state"] = {
        "counts": bounded["target_state"]["counts"],
        "note": "Target state omitted to fit the auditor input budget.",
    }
    bounded["payload"] = bounded_auditor_payload(card.get("payload"), max_chars=8_000)
    if json_size(bounded) <= AUDITOR_MAX_CARD_CHARS:
        return bounded

    return {
        "action_id": card.get("action_id"),
        "action_type": card.get("action_type"),
        "status": card.get("status"),
        "risk_tier": card.get("risk_tier"),
        "policy": compact_audit_value(card.get("policy")),
        "critic": compact_audit_value(card.get("critic")),
        "applied_state_hash": card.get("applied_state_hash"),
        "details_excerpt": clipped(
            json.dumps(bounded, ensure_ascii=True, sort_keys=True), 32_000
        ),
        "card_truncated": True,
        "note": "Action details truncated to fit the auditor input budget.",
    }


def bounded_auditor_payload(
    payload: Any, *, max_chars: int = AUDITOR_MAX_PAYLOAD_CHARS
) -> Any:
    compact = compact_audit_value(payload)
    if json_size(compact) <= max_chars:
        return compact
    if isinstance(payload, dict) and isinstance(payload.get("fact"), dict):
        fact = payload["fact"]
        return {
            "fact": {
                key: compact_audit_value(fact.get(key))
                for key in (
                    "id",
                    "statement",
                    "entity_key",
                    "page_hint",
                    "section_hint",
                    "source_ids",
                    "source_spans",
                    "evidence_quote",
                    "evidence_unit_ids",
                    "confidence",
                    "truth_confidence",
                    "extraction_confidence",
                    "routing_confidence",
                    "extraction_method",
                    "extractor_model",
                )
                if fact.get(key) is not None
            },
            "truncated": True,
            "note": "Nonessential fact metadata omitted to fit the auditor input budget.",
        }
    serialized = json.dumps(compact, ensure_ascii=True, sort_keys=True)
    return {
        "json_excerpt": clipped(serialized, max_chars),
        "truncated": True,
    }


def compact_audit_value(value: Any, *, depth: int = 0) -> Any:
    if isinstance(value, str):
        return clipped(value, 4_000)
    if isinstance(value, list):
        items = [compact_audit_value(item, depth=depth + 1) for item in value[:40]]
        if len(value) > 40:
            items.append({"omitted_item_count": len(value) - 40})
        return items
    if isinstance(value, dict):
        items = list(value.items())
        compact = {
            str(key): compact_audit_value(item, depth=depth + 1)
            for key, item in items[:60]
        }
        if len(items) > 60:
            compact["_omitted_key_count"] = len(items) - 60
        return compact
    return value


def json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=True, sort_keys=True))


def auditor_candidate_card(conn: Any, action: dict[str, Any]) -> dict[str, Any]:
    target_fact_ids = list(action.get("target_fact_ids") or [])
    target_contract_ids = list(action.get("target_contract_ids") or [])
    target_page_paths = list(action.get("target_page_paths") or [])
    return {
        "action_id": action["id"],
        "action_type": action["action_type"],
        "status": action["status"],
        "proposed_by": action.get("proposed_by"),
        "confidence": action.get("confidence"),
        "risk_tier": action.get("risk_tier"),
        "policy": {
            "id": action.get("policy_id"),
            "version": action.get("policy_version"),
            "decision": action.get("policy_decision"),
            "autonomy_level": action.get("autonomy_level"),
            "rule": load_policy_rule(conn, action),
        },
        "critic": {
            "by": action.get("critic_by"),
            "decision": action.get("critic_decision"),
        },
        "features": action.get("action_features") or {},
        "payload": action_payload(action),
        "targets": {
            "fact_ids": target_fact_ids,
            "page_paths": target_page_paths,
            "contract_ids": target_contract_ids,
        },
        "target_state": {
            "facts": load_fact_cards(conn, target_fact_ids),
            "contracts": load_contract_cards(conn, target_contract_ids),
            "syntheses": load_synthesis_cards(conn, target_page_paths),
        },
        "inverse_action": action.get("inverse_action_json") or {},
        "applied_state_hash": action.get("applied_state_hash"),
    }


def load_policy_rule(conn: Any, action: dict[str, Any]) -> dict[str, Any] | None:
    policy_id = action.get("policy_id")
    policy_version = action.get("policy_version")
    if not policy_id or policy_version is None:
        return None
    row = conn.execute(
        """
        SELECT id, version, priority, match_action_types, match_predicate,
               autonomy_level, critic_required, timeout_allowed,
               timeout_after_seconds, audit_sample_rate, demotion_threshold,
               auto_revert_signals
        FROM cos_policy
        WHERE id = ? AND version = ?
        """,
        (policy_id, policy_version),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "version": row["version"],
        "priority": row["priority"],
        "match_action_types": loads(row["match_action_types"], []),
        "match_predicate": loads(row["match_predicate"], {}),
        "autonomy_level": row["autonomy_level"],
        "critic_required": bool(row["critic_required"]),
        "timeout_allowed": bool(row["timeout_allowed"]),
        "timeout_after_seconds": row["timeout_after_seconds"],
        "audit_sample_rate": row["audit_sample_rate"],
        "demotion_threshold": row["demotion_threshold"],
        "auto_revert_signals": loads(row["auto_revert_signals"], []),
    }


def load_fact_cards(conn: Any, fact_ids: list[str]) -> list[dict[str, Any]]:
    if not fact_ids:
        return []
    placeholders = ",".join("?" for _ in fact_ids)
    return [
        {
            "id": row["id"],
            "statement": clipped(row["statement"], 1000),
            "entity_key": row["entity_key"],
            "page_hint": row["page_hint"],
            "section_hint": row["section_hint"],
            "source_ids": loads(row["source_ids"], []),
            "confidence": row["confidence"],
            "truth_confidence": row["truth_confidence"],
            "status": row["status"],
            "source_spans": loads(row["source_spans"], [])[:5],
            "evidence_quote": clipped(row["evidence_quote"], 500),
            "extraction_method": row["extraction_method"],
        }
        for row in conn.execute(
            f"SELECT * FROM facts WHERE id IN ({placeholders}) ORDER BY id",
            fact_ids,
        )
    ]


def load_contract_cards(conn: Any, contract_ids: list[str]) -> list[dict[str, Any]]:
    if not contract_ids:
        return []
    placeholders = ",".join("?" for _ in contract_ids)
    return [
        {
            "id": row["id"],
            "page_hint": row["page_hint"],
            "canonical_entity": row["canonical_entity"],
            "page_scope": clipped(row["page_scope"], 800),
            "retrieval_purpose": clipped(row["retrieval_purpose"], 800),
            "what_belongs_here": clipped(row["what_belongs_here"], 1000),
            "what_does_not_belong_here": clipped(
                row["what_does_not_belong_here"], 1000
            ),
            "related_pages": loads(row["related_pages"], []),
            "version": row["version"],
            "status": row["status"],
        }
        for row in conn.execute(
            f"SELECT * FROM page_contracts WHERE id IN ({placeholders}) ORDER BY id",
            contract_ids,
        )
    ]


def load_synthesis_cards(conn: Any, page_hints: list[str]) -> list[dict[str, Any]]:
    if not page_hints:
        return []
    placeholders = ",".join("?" for _ in page_hints)
    return [
        {
            "id": row["id"],
            "page_hint": row["page_hint"],
            "fact_ids": loads(row["fact_ids"], []),
            "fact_hash": row["fact_hash"],
            "model": row["model"],
            "prompt_version": row["prompt_version"],
            "stale": bool(row["stale"]),
            "synthesis_markdown": clipped(row["synthesis_markdown"], 1200),
        }
        for row in conn.execute(
            f"""
            SELECT *
            FROM wiki_page_syntheses
            WHERE page_hint IN ({placeholders})
            ORDER BY page_hint, generated_at DESC, id
            LIMIT 10
            """,
            page_hints,
        )
    ]


def auditor_prompt(cards: list[dict[str, Any]]) -> str:
    return (
        "You are the independent auditor for PKM Brain chief-of-staff actions.\n"
        "Judge already-applied actions using only the action cards. Check whether each action "
        "was supported by evidence, consistent with policy and contracts, safe for its autonomy "
        "level, and reversible when it claims to be reversible.\n"
        "Return one audit per action_id. Use decision='ok' only when the action is adequately "
        "supported. Use decision='bad' for unsupported, unsafe, incorrect, policy-violating, "
        "or unverifiable actions. Include a concise rationale.\n\n"
        f"Action cards:\n{json.dumps(cards, ensure_ascii=True, sort_keys=True)}"
    )


def auditor_judgments_by_action_id(parsed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    judgments: dict[str, dict[str, Any]] = {}
    for item in parsed.get("audits") or []:
        if not isinstance(item, dict):
            continue
        action_id = str(item.get("action_id") or "")
        if action_id:
            judgments[action_id] = item
    return judgments


def normalize_auditor_judgment(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    raw_decision = str(item.get("decision") or "").strip().lower()
    if raw_decision in OK_DECISIONS:
        decision = "ok"
        audit_status = "sampled_ok"
    elif raw_decision in BAD_DECISIONS:
        decision = "bad"
        audit_status = "sampled_bad"
    else:
        decision = "bad"
        audit_status = "sampled_bad"
    confidence = item.get("confidence")
    try:
        normalized_confidence = min(1.0, max(0.0, float(confidence)))
    except (TypeError, ValueError):
        normalized_confidence = None
    risk_tier = item.get("risk_tier")
    return {
        "decision": decision,
        "raw_decision": raw_decision,
        "audit_status": audit_status,
        "rationale": clipped(item.get("rationale") or item.get("reason") or "", 1000),
        "confidence": normalized_confidence,
        "risk_tier": risk_tier if risk_tier in {"low", "medium", "high"} else None,
    }


def clipped(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else f"{text[:limit]}..."
