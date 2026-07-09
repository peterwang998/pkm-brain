from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .db import dumps, loads, rows
from .util import new_id, now_iso


AUTONOMY_ORDER = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}
TOPOLOGY_ACTION_TYPES = {"page_merge", "page_split", "rename_page", "entity_merge", "entity_split"}
HIGH_CERTAINTY_ENTITY_MERGE_SIGNALS = {
    "same_normalized_name_or_alias",
    "same_compact_name_or_alias",
}
LOW_AUTONOMY_ACTION_TYPES = {
    "fact_merge",
    "fact_supersede",
    "rehome_fact",
    "edit_contract",
    "synthesize_page",
}
MEDIUM_AUTONOMY_ACTION_TYPES = {
    *LOW_AUTONOMY_ACTION_TYPES,
    *TOPOLOGY_ACTION_TYPES,
    "display_contested",
}


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
            reason=human_policy_reason(action_type, rule, features),
        )
        return apply_eval_gate(conn, decision, features)
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


def human_policy_reason(action_type: str, rule: Any, features: dict[str, Any]) -> str:
    policy_id = str(rule["id"])
    autonomy_level = str(rule["autonomy_level"])
    risk_tier = str(features.get("risk_tier") or "").strip()
    action_label = str(action_type or "action").replace("_", " ")
    if action_type == "synthesize_page":
        return (
            f"Synthesis is derived, revertible page text; policy {policy_id} routes "
            f"{risk_tier or 'this'} synthesis to {autonomy_level}."
        )
    if action_type == "fact_upsert":
        return (
            f"Fact upsert matched {risk_tier or 'current'} evidence policy {policy_id}; "
            f"review level is {autonomy_level}."
        )
    if autonomy_level == "L3":
        return (
            f"{action_label.title()} requires human review under policy {policy_id}"
            f"{f' because risk is {risk_tier}' if risk_tier else ''}."
        )
    return (
        f"{action_label.title()} uses policy {policy_id}; autonomy level is "
        f"{autonomy_level}{f' with {risk_tier} risk' if risk_tier else ''}."
    )


def apply_eval_gate(
    conn: Any, decision: PolicyDecision, features: dict[str, Any]
) -> PolicyDecision:
    if decision.autonomy_level == "L3":
        return decision
    eval_gate = features.get("eval_gate")
    if eval_gate is None:
        return decision
    ok, reason = eval_gate_satisfied(conn, eval_gate)
    if ok:
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
        reason=f"eval gate failed; escalated to L3: {reason}",
    )


def eval_gate_satisfied(conn: Any, eval_gate: Any) -> tuple[bool, str]:
    if eval_gate is False:
        return False, "caller supplied false gate"
    if eval_gate is True:
        return False, "caller supplied boolean gate without report evidence"
    if not isinstance(eval_gate, dict):
        return False, "eval_gate must be a report-backed object"
    if eval_gate.get("passed") is False:
        return False, "caller supplied failed gate"
    suite = str(eval_gate.get("suite") or "").strip()
    if not suite:
        return False, "eval_gate.suite is required"
    report_path = eval_gate.get("report_path")
    report = load_eval_report(Path(str(report_path)).expanduser()) if report_path else latest_eval_report(conn, suite)
    if report is None:
        return False, f"no eval report found for suite {suite}"
    suite_report = report_for_suite(report, suite)
    if suite_report is None:
        return False, f"report does not include suite {suite}"
    if suite_report.get("metrics", {}).get("skipped"):
        return False, f"suite {suite} was skipped"
    if not bool(suite_report.get("passed")):
        return False, f"suite {suite} did not pass"
    if eval_gate.get("requires_labels"):
        metrics = suite_report.get("metrics", {})
        if metrics.get("label_policy") != "labeled" or int(metrics.get("label_case_count") or 0) <= 0:
            return False, f"suite {suite} does not include labeled extraction fixtures"
    return True, f"suite {suite} passed in {report.get('id') or report.get('report_path') or 'report'}"


def latest_eval_report(conn: Any, suite: str) -> dict[str, Any] | None:
    db_path = sqlite_db_path(conn)
    if db_path is None:
        return None
    reports_dir = db_path.parent.parent / "reports" / "evals"
    if not reports_dir.exists():
        return None
    candidates = sorted(
        reports_dir.glob(f"eval-{suite}-*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        candidates = sorted(
            reports_dir.glob("eval-all-*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    for path in candidates:
        report = load_eval_report(path)
        if report and report_for_suite(report, suite):
            return report
    return None


def sqlite_db_path(conn: Any) -> Path | None:
    for row in conn.execute("PRAGMA database_list"):
        name = row["name"] if hasattr(row, "keys") else row[1]
        if name == "main":
            path = row["file"] if hasattr(row, "keys") else row[2]
            return Path(str(path)) if path else None
    return None


def load_eval_report(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def report_for_suite(report: dict[str, Any], suite: str) -> dict[str, Any] | None:
    if report.get("suite") == suite and "reports" not in report:
        return report
    for item in report.get("reports") or []:
        if isinstance(item, dict) and item.get("suite") == suite:
            return item
    return None


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


def classify_action_risk(
    action_type: str,
    action_features: dict[str, Any] | None,
    *,
    explicit_risk_tier: str | None = None,
    large_topology_fact_threshold: int = 8,
) -> str:
    explicit = normalize_risk_tier(explicit_risk_tier)
    features = action_features or {}
    affected_fact_count = int_or_zero(features.get("affected_fact_count"))
    affected_page_count = int_or_zero(features.get("affected_page_count"))
    if action_type == "fact_upsert":
        if bool(features.get("fallback_route")) or not bool(features.get("quote_backed")):
            return "high"
        if bool(features.get("truth_contradiction")) or str(features.get("resolver_precheck") or "") == "residue":
            return "high"
        if features.get("fact_upsert_resolution") == "exact_duplicate_source_union":
            return "low"
        if bool(features.get("clean_fact_upsert")) and features.get("fact_upsert_resolution") == "new_clean_fact":
            return "medium"
        return "high"
    if action_type == "entity_merge" and high_certainty_entity_merge(features):
        if explicit == "high" or bool(features.get("truth_contradiction")):
            return "high"
        return explicit or "low"
    if action_type in TOPOLOGY_ACTION_TYPES:
        large = (
            bool(features.get("large_topology"))
            or bool(features.get("cross_entity_merge"))
            or bool(features.get("cross_type_merge"))
            or bool(features.get("type_mismatch"))
            or affected_fact_count >= large_topology_fact_threshold
            or affected_page_count >= large_topology_fact_threshold
            or int_or_zero(features.get("merged_entity_count")) >= large_topology_fact_threshold
        )
        if large:
            return "high"
    if explicit == "high" or bool(features.get("truth_contradiction")):
        return "high"
    confidence = features.get("confidence")
    if confidence is not None:
        try:
            if float(confidence) < 0.55:
                return "high"
        except (TypeError, ValueError):
            pass
    if explicit in {"low", "medium"}:
        return explicit
    if action_type == "canonicalize_page":
        return "low"
    if action_type == "rehome_fact" and affected_fact_count <= 1:
        return "low"
    if action_type == "synthesize_page":
        return "low"
    if action_type == "edit_contract" and not any(features.get("contracts_present") or []):
        return "low"
    if action_type == "fact_merge" and str(features.get("merge_reason") or "").startswith("exact_"):
        return "low"
    if action_type in MEDIUM_AUTONOMY_ACTION_TYPES:
        return "medium"
    return "high"


def high_certainty_entity_merge(features: dict[str, Any]) -> bool:
    merge_signal = str(features.get("merge_signal") or "")
    if merge_signal not in HIGH_CERTAINTY_ENTITY_MERGE_SIGNALS:
        return False
    return not (
        bool(features.get("cross_entity_merge"))
        or bool(features.get("cross_type_merge"))
        or bool(features.get("type_mismatch"))
    )


def normalize_risk_tier(value: str | None) -> str | None:
    risk_tier = str(value or "").strip().lower()
    return risk_tier if risk_tier in {"low", "medium", "high"} else None


def int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def promote_policy_for_autonomy(
    conn: Any,
    *,
    reason: str,
    large_topology_fact_threshold: int = 8,
) -> int:
    current = active_policy_version(conn) or 0
    new_version = current + 1
    created_at = now_iso()
    rows_to_insert = autonomy_policy_rows(new_version, created_at, large_topology_fact_threshold)
    conn.executemany(
        """
        INSERT INTO cos_policy(
          id, version, priority, match_action_types, match_predicate,
          autonomy_level, critic_required, timeout_allowed,
          timeout_after_seconds, audit_sample_rate, demotion_threshold,
          auto_revert_signals, active, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows_to_insert,
    )
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
            f"policy_v{new_version}_promotion_marker",
            new_version,
            0,
            '["*"]',
            dumps({"eq": {"policy_promotion_marker": True}}),
            "L3",
            0,
            0,
            None,
            1.0,
            None,
            dumps([]),
            1,
            created_at,
        ),
    )
    conn.execute(
        """
        INSERT INTO open_questions(
          id, kind, question, options, status, context, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id("question"),
            "policy_change",
            f"Policy promoted to version {new_version}: {reason}",
            dumps([]),
            "auto_resolved",
            dumps(
                {
                    "policy_version": new_version,
                    "large_topology_fact_threshold": large_topology_fact_threshold,
                    "reason": reason,
                }
            ),
            created_at,
        ),
    )
    return new_version


def autonomy_policy_rows(
    version: int, created_at: str, large_topology_fact_threshold: int
) -> list[tuple[Any, ...]]:
    return [
        policy_row(
            version,
            "high_risk_l3",
            5,
            ["*"],
            {"eq": {"risk_tier": "high"}},
            "L3",
            False,
            1.0,
            created_at,
        ),
        policy_row(
            version,
            "large_topology_l3",
            15,
            sorted(TOPOLOGY_ACTION_TYPES),
            {
                "any": [
                    {"eq": {"large_topology": True}},
                    {"gte": {"affected_fact_count": large_topology_fact_threshold}},
                    {"gte": {"affected_page_count": large_topology_fact_threshold}},
                    {"gte": {"merged_entity_count": large_topology_fact_threshold}},
                    {"eq": {"cross_entity_merge": True}},
                    {"eq": {"cross_type_merge": True}},
                    {"eq": {"type_mismatch": True}},
                ]
            },
            "L3",
            False,
            1.0,
            created_at,
        ),
        policy_row(
            version,
            "entity_merge_high_certainty_l1",
            10,
            ["entity_merge"],
            {
                "all": [
                    {
                        "eq": {
                            "risk_tier": "low",
                            "cross_entity_merge": False,
                            "cross_type_merge": False,
                            "type_mismatch": False,
                        }
                    },
                    {
                        "in": {
                            "merge_signal": sorted(HIGH_CERTAINTY_ENTITY_MERGE_SIGNALS)
                        }
                    },
                ]
            },
            "L1",
            False,
            0.25,
            created_at,
        ),
        policy_row(
            version,
            "canonical_l0",
            20,
            ["canonicalize_page"],
            {"eq": {"deterministic": True, "risk_tier": "low"}},
            "L0",
            False,
            0.05,
            created_at,
        ),
        policy_row(
            version,
            "fact_upsert_exact_l1",
            25,
            ["fact_upsert"],
            {
                "eq": {
                    "risk_tier": "low",
                    "fact_upsert_resolution": "exact_duplicate_source_union",
                    "quote_backed": True,
                    "fallback_route": False,
                }
            },
            "L1",
            False,
            0.25,
            created_at,
        ),
        policy_row(
            version,
            "fact_upsert_clean_l2",
            28,
            ["fact_upsert"],
            {
                "eq": {
                    "risk_tier": "medium",
                    "clean_fact_upsert": True,
                    "fact_upsert_resolution": "new_clean_fact",
                    "quote_backed": True,
                    "fallback_route": False,
                    "resolver_precheck": "passed",
                }
            },
            "L2",
            True,
            1.0,
            created_at,
        ),
        policy_row(
            version,
            "low_l1_critic",
            30,
            sorted(LOW_AUTONOMY_ACTION_TYPES),
            {"eq": {"risk_tier": "low"}},
            "L1",
            True,
            0.25,
            created_at,
        ),
        policy_row(
            version,
            "medium_l2_audit",
            40,
            sorted(MEDIUM_AUTONOMY_ACTION_TYPES),
            {"eq": {"risk_tier": "medium"}},
            "L2",
            True,
            1.0,
            created_at,
        ),
        policy_row(
            version,
            "default_l3",
            1000,
            ["*"],
            {},
            "L3",
            False,
            1.0,
            created_at,
        ),
    ]


def policy_row(
    version: int,
    slug: str,
    priority: int,
    action_types: list[str],
    predicate: dict[str, Any],
    autonomy_level: str,
    critic_required: bool,
    audit_sample_rate: float,
    created_at: str,
) -> tuple[Any, ...]:
    return (
        f"policy_v{version}_{slug}",
        version,
        priority,
        dumps(action_types),
        dumps(predicate),
        autonomy_level,
        int(critic_required),
        0,
        None,
        audit_sample_rate,
        0.1 if autonomy_level in {"L1", "L2"} else None,
        dumps(["audit_sampled_bad"] if autonomy_level in {"L1", "L2"} else []),
        1,
        created_at,
    )


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
