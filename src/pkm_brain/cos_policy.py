from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .db import dumps, loads, rows
from .util import new_id, now_iso


AUTONOMY_ORDER = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}


@dataclass(frozen=True)
class PolicyDecision:
    policy_id: str | None
    policy_version: int | None
    policy_decision: str
    autonomy_level: str
    critic_required: bool = False
    audit_sample_rate: float = 0.0
    timeout_allowed: bool = False
    timeout_after_seconds: int | None = None
    reason: str = ""


def evaluate_policy(
    conn: Any, action_type: str, action_features: dict[str, Any] | None
) -> PolicyDecision:
    features = action_features or {}
    flat = flatten_features(features)
    for rule in active_policy_rules(conn):
        action_types = loads(rule["match_action_types"], ["*"])
        if "*" not in action_types and action_type not in action_types:
            continue
        predicate = loads(rule["match_predicate"], {})
        if not predicate_matches(predicate, flat):
            continue
        decision = PolicyDecision(
            policy_id=rule["id"],
            policy_version=int(rule["version"]),
            policy_decision="matched",
            autonomy_level=str(rule["autonomy_level"]),
            critic_required=bool(rule["critic_required"]),
            audit_sample_rate=float(rule["audit_sample_rate"] or 0.0),
            timeout_allowed=bool(rule["timeout_allowed"]),
            timeout_after_seconds=rule["timeout_after_seconds"],
            reason=f"matched policy {rule['id']}",
        )
        return apply_eval_gate(decision, features)
    return PolicyDecision(
        policy_id=None,
        policy_version=active_policy_version(conn),
        policy_decision="default_l3",
        autonomy_level="L3",
        reason="no active policy rule matched",
    )


def active_policy_version(conn: Any) -> int | None:
    row = conn.execute(
        "SELECT MAX(version) AS version FROM cos_policy WHERE active = 1"
    ).fetchone()
    return int(row["version"]) if row and row["version"] is not None else None


def active_policy_rules(conn: Any) -> list[Any]:
    version = active_policy_version(conn)
    if version is None:
        return []
    return rows(
        conn,
        """
        SELECT *
        FROM cos_policy
        WHERE active = 1
          AND version = ?
        ORDER BY priority, id
        """,
        (version,),
    )


def apply_eval_gate(
    decision: PolicyDecision, features: dict[str, Any]
) -> PolicyDecision:
    if decision.autonomy_level == "L3":
        return decision
    eval_gate = features.get("eval_gate")
    failed = eval_gate is False or (
        isinstance(eval_gate, dict) and eval_gate.get("passed") is False
    )
    if not failed:
        return decision
    return PolicyDecision(
        policy_id=decision.policy_id,
        policy_version=decision.policy_version,
        policy_decision="eval_gate_failed",
        autonomy_level="L3",
        critic_required=False,
        audit_sample_rate=1.0,
        timeout_allowed=False,
        timeout_after_seconds=None,
        reason="eval gate failed; escalated to L3",
    )


def predicate_matches(predicate: dict[str, Any], features: dict[str, Any]) -> bool:
    if not predicate:
        return True
    if "all" in predicate:
        values = predicate["all"]
        return isinstance(values, list) and all(
            predicate_matches(item, features) for item in values if isinstance(item, dict)
        )
    if "any" in predicate:
        values = predicate["any"]
        return isinstance(values, list) and any(
            predicate_matches(item, features) for item in values if isinstance(item, dict)
        )
    for op in ("eq", "in", "lt", "lte", "gt", "gte", "exists"):
        comparisons = predicate.get(op)
        if comparisons is None:
            continue
        if not isinstance(comparisons, dict):
            return False
        for key, expected in comparisons.items():
            actual = features.get(str(key))
            if not comparison_matches(op, actual, expected):
                return False
    return True


def comparison_matches(op: str, actual: Any, expected: Any) -> bool:
    if op == "eq":
        return actual == expected
    if op == "in":
        if not isinstance(expected, list):
            return False
        return actual in expected
    if op == "exists":
        exists = actual is not None
        return exists is bool(expected)
    if actual is None:
        return False
    try:
        left = float(actual)
        right = float(expected)
    except (TypeError, ValueError):
        return False
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    if op == "gt":
        return left > right
    if op == "gte":
        return left >= right
    return False


def flatten_features(
    value: dict[str, Any], prefix: str = "", output: dict[str, Any] | None = None
) -> dict[str, Any]:
    result = output if output is not None else {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, dict):
            flatten_features(item, path, result)
        else:
            result[path] = item
            if not prefix:
                result[str(key)] = item
    return result


def demote_policy_version(
    conn: Any, *, reason: str, demote_to: str = "L3"
) -> int:
    current = active_policy_version(conn) or 0
    new_version = current + 1
    created_at = now_iso()
    for rule in active_policy_rules(conn):
        conn.execute(
            """
            INSERT INTO cos_policy(
              id, version, priority, match_action_types, match_predicate,
              autonomy_level, critic_required, timeout_allowed,
              timeout_after_seconds, audit_sample_rate, demotion_threshold,
              auto_revert_signals, active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("policy"),
                new_version,
                int(rule["priority"]),
                rule["match_action_types"],
                rule["match_predicate"],
                demote_to
                if AUTONOMY_ORDER.get(str(rule["autonomy_level"]), 3)
                < AUTONOMY_ORDER.get(demote_to, 3)
                else rule["autonomy_level"],
                0 if demote_to == "L3" else int(rule["critic_required"]),
                0 if demote_to == "L3" else int(rule["timeout_allowed"]),
                None if demote_to == "L3" else rule["timeout_after_seconds"],
                max(float(rule["audit_sample_rate"] or 0.0), 1.0),
                rule["demotion_threshold"],
                rule["auto_revert_signals"],
                1,
                created_at,
            ),
        )
    conn.execute(
        """
        INSERT INTO cos_policy(
          id, version, priority, match_action_types, match_predicate,
          autonomy_level, critic_required, timeout_allowed, audit_sample_rate,
          auto_revert_signals, active, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id("policy"),
            new_version,
            0,
            '["*"]',
            dumps({"eq": {"policy_demoted": True}}),
            "L3",
            0,
            0,
            1.0,
            dumps([]),
            1,
            created_at,
        ),
    )
    return new_version
