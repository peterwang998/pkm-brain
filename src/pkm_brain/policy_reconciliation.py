from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .cos_actions import critic_review as review_action
from .cos_actions import decide_action, row_to_action
from .cos_policy import active_policy_version, evaluate_policy
from .db import connection, dumps, retry_sqlite_lock
from .llm import LLMProvider
from .paths import BrainPaths
from .util import now_iso


ACTIVE_QUESTION_STATUSES = {"open", "needs_human"}
OPEN_ACTION_STATUSES = {"proposed", "needs_human"}
TERMINAL_ACTION_STATUSES = {
    "applied",
    "auto_applied",
    "dismissed",
    "failed",
    "rejected",
    "reverted",
}


def reconcile_policy_escalations(
    paths: BrainPaths,
    *,
    dry_run: bool = True,
    limit: int | None = None,
    critic_review: dict[str, Any] | None = None,
    critic_llm_provider: LLMProvider | None = None,
) -> dict[str, Any]:
    question_rows = load_policy_escalation_questions(paths, limit=limit)
    grouped, missing_actions = classify_policy_escalations(paths, question_rows)
    eligible = [item for item in grouped if item["outcome"] == "redecide"]
    retained = [item for item in grouped if item["outcome"] == "retain_l3"]
    stale = [item for item in grouped if item["outcome"] == "close_stale"]
    preview = [policy_reconciliation_preview(item) for item in grouped[:25]]
    base = {
        "active_policy_version": current_policy_version(paths),
        "inspected_question_count": len(question_rows),
        "candidate_action_count": len(grouped),
        "eligible_action_count": len(eligible),
        "retained_l3_action_count": len(retained),
        "stale_question_count": sum(len(item["question_ids"]) for item in stale),
        "missing_action_question_count": len(missing_actions),
        "by_action_type": dict(
            sorted(Counter(item["action"]["action_type"] for item in grouped).items())
        ),
        "by_current_autonomy": dict(
            sorted(
                Counter(
                    item["decision"].autonomy_level
                    for item in grouped
                    if item.get("decision") is not None
                ).items()
            )
        ),
        "preview": preview,
        "missing_action_examples": missing_actions[:25],
    }
    if dry_run:
        return {"status": "dry_run", **base}

    timestamp = now_iso()
    close_stale_policy_questions(paths, stale, timestamp=timestamp)
    decided, failures = redecide_policy_actions(
        paths,
        eligible,
        critic_review=critic_review,
        critic_llm_provider=critic_llm_provider,
    )
    resolved: list[dict[str, Any]] = []
    retained_after_recheck: list[dict[str, Any]] = []
    for item in eligible:
        action_id = str(item["action"]["id"])
        result = decided.get(action_id)
        if result is None:
            continue
        if str(result.get("status") or "") not in TERMINAL_ACTION_STATUSES:
            retained_after_recheck.append(
                {
                    "action_id": action_id,
                    "status": result.get("status"),
                    "autonomy_level": result.get("autonomy_level"),
                }
            )
            continue
        close_redecided_policy_questions(
            paths,
            item,
            result,
            timestamp=timestamp,
        )
        resolved.append(result)

    return {
        "status": "applied",
        **base,
        "resolved_action_count": len(resolved),
        "resolved_question_count": sum(
            len(item["question_ids"])
            for item in eligible
            if str(item["action"]["id"]) in {
                str(result["id"]) for result in resolved
            }
        ),
        "closed_stale_question_count": sum(
            len(item["question_ids"]) for item in stale
        ),
        "result_status_counts": dict(
            sorted(Counter(str(result["status"]) for result in resolved).items())
        ),
        "critic_decision_counts": dict(
            sorted(
                Counter(
                    str(result.get("critic_decision") or "not_required")
                    for result in resolved
                ).items()
            )
        ),
        "retained_after_recheck": retained_after_recheck[:25],
        "failure_count": len(failures),
        "failures": failures[:25],
    }


def load_policy_escalation_questions(
    paths: BrainPaths, *, limit: int | None
) -> list[dict[str, Any]]:
    query = """
        SELECT id, action_id, created_at
        FROM open_questions
        WHERE kind = 'policy_escalation'
          AND status IN ('open', 'needs_human')
        ORDER BY created_at, id
    """
    params: list[Any] = []
    if limit is not None:
        query += " LIMIT ?"
        params.append(max(1, int(limit)))
    with connection(paths.sqlite_path) as conn:
        return [dict(row) for row in conn.execute(query, params)]


def classify_policy_escalations(
    paths: BrainPaths, question_rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    questions_by_action: dict[str, list[str]] = {}
    missing_actions: list[dict[str, Any]] = []
    for question in question_rows:
        action_id = str(question.get("action_id") or "").strip()
        if not action_id:
            missing_actions.append(
                {"question_id": str(question["id"]), "reason": "missing_action_id"}
            )
            continue
        questions_by_action.setdefault(action_id, []).append(str(question["id"]))
    if not questions_by_action:
        return [], missing_actions

    placeholders = ",".join("?" for _ in questions_by_action)
    grouped: list[dict[str, Any]] = []
    with connection(paths.sqlite_path) as conn:
        action_rows = {
            str(row["id"]): row_to_action(row)
            for row in conn.execute(
                f"SELECT * FROM cos_actions WHERE id IN ({placeholders})",
                list(questions_by_action),
            )
        }
        for action_id, question_ids in questions_by_action.items():
            action = action_rows.get(action_id)
            if action is None:
                missing_actions.extend(
                    {"question_id": question_id, "action_id": action_id, "reason": "missing_action"}
                    for question_id in question_ids
                )
                continue
            status = str(action.get("status") or "")
            decision = None
            if status in OPEN_ACTION_STATUSES:
                decision = evaluate_policy(
                    conn,
                    str(action["action_type"]),
                    action.get("action_features") or {},
                )
                outcome = (
                    "redecide" if decision.autonomy_level != "L3" else "retain_l3"
                )
            else:
                outcome = "close_stale"
            grouped.append(
                {
                    "action": action,
                    "question_ids": question_ids,
                    "decision": decision,
                    "outcome": outcome,
                }
            )
    return grouped, missing_actions


def redecide_policy_actions(
    paths: BrainPaths,
    eligible: list[dict[str, Any]],
    *,
    critic_review: dict[str, Any] | None,
    critic_llm_provider: LLMProvider | None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    if not eligible:
        return {}, []
    review = critic_review or {}
    workers = min(max(1, int(review.get("max_workers") or 4)), len(eligible))
    timeout_seconds = review.get("timeout_seconds")
    disagreement_mode = str(review.get("disagreement_mode") or "reject")
    reviews: dict[str, dict[str, str]] = {}
    decided: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    review_items = [item for item in eligible if item["decision"].critic_required]
    if review_items:
        with ThreadPoolExecutor(
            max_workers=min(workers, len(review_items)),
            thread_name_prefix="policy-reconcile",
        ) as executor:
            futures = {
                executor.submit(
                    review_action,
                    paths,
                    item["action"],
                    item["decision"],
                    llm_provider=critic_llm_provider,
                    timeout_seconds=timeout_seconds,
                ): str(item["action"]["id"])
                for item in review_items
            }
            for future in as_completed(futures):
                action_id = futures[future]
                try:
                    reviews[action_id] = future.result()
                except Exception as exc:
                    failures.append(
                        {"action_id": action_id, "phase": "critic", "error": str(exc)[:500]}
                    )

    failed_action_ids = {item["action_id"] for item in failures}
    for item in eligible:
        action_id = str(item["action"]["id"])
        if action_id in failed_action_ids:
            continue
        critic_result = reviews.get(action_id)
        try:
            decided[action_id] = retry_sqlite_lock(
                lambda: decide_action(
                    paths,
                    action_id,
                    critic_by=critic_result.get("critic_by")
                    if critic_result
                    else None,
                    critic_decision=critic_result.get("decision")
                    if critic_result
                    else None,
                    critic_rationale=critic_result.get("rationale")
                    if critic_result
                    else None,
                    critic_timeout_seconds=timeout_seconds,
                    critic_disagreement_mode=disagreement_mode,
                )
            )
        except Exception as exc:
            failures.append(
                {"action_id": action_id, "phase": "decision", "error": str(exc)[:500]}
            )
    return decided, failures


def close_redecided_policy_questions(
    paths: BrainPaths,
    item: dict[str, Any],
    result: dict[str, Any],
    *,
    timestamp: str,
) -> None:
    action = item["action"]
    decision = item["decision"]
    answer = {
        "decision": "redecided_under_current_policy",
        "action_id": action["id"],
        "action_status": result.get("status"),
        "previous_policy_id": action.get("policy_id"),
        "previous_policy_version": action.get("policy_version"),
        "current_policy_id": result.get("policy_id"),
        "current_policy_version": result.get("policy_version"),
        "current_autonomy_level": result.get("autonomy_level"),
        "critic_decision": result.get("critic_decision"),
    }
    with connection(paths.sqlite_path) as conn:
        evidence = dict(result.get("evidence_json") or {})
        evidence["policy_reconciliation"] = {
            **answer,
            "question_ids": item["question_ids"],
            "eligible_autonomy_level": decision.autonomy_level,
            "reconciled_at": timestamp,
        }
        conn.execute(
            "UPDATE cos_actions SET evidence_json = ? WHERE id = ?",
            (dumps(evidence), action["id"]),
        )
        update_policy_questions(
            conn,
            item["question_ids"],
            status="auto_resolved",
            answer=answer,
            timestamp=timestamp,
        )


def close_stale_policy_questions(
    paths: BrainPaths, stale: list[dict[str, Any]], *, timestamp: str
) -> None:
    with connection(paths.sqlite_path) as conn:
        for item in stale:
            update_policy_questions(
                conn,
                item["question_ids"],
                status="auto_resolved",
                answer={
                    "decision": "stale_policy_question_closed",
                    "action_id": item["action"]["id"],
                    "action_status": item["action"].get("status"),
                },
                timestamp=timestamp,
            )


def update_policy_questions(
    conn: Any,
    question_ids: list[str],
    *,
    status: str,
    answer: dict[str, Any],
    timestamp: str,
) -> None:
    if not question_ids:
        return
    placeholders = ",".join("?" for _ in question_ids)
    conn.execute(
        f"""
        UPDATE open_questions
        SET status = ?, answer = ?, answered_at = ?,
            decided_by = 'policy_reconciliation_v1'
        WHERE id IN ({placeholders})
          AND status IN ('open', 'needs_human')
        """,
        [status, dumps(answer), timestamp, *question_ids],
    )


def policy_reconciliation_preview(item: dict[str, Any]) -> dict[str, Any]:
    action = item["action"]
    payload = (action.get("evidence_json") or {}).get("payload") or {}
    fact = payload.get("fact") if isinstance(payload, dict) else None
    decision = item.get("decision")
    return {
        "action_id": action["id"],
        "question_count": len(item["question_ids"]),
        "action_type": action["action_type"],
        "action_status": action.get("status"),
        "statement": str((fact or {}).get("statement") or "")[:220],
        "previous_policy_id": action.get("policy_id"),
        "previous_policy_version": action.get("policy_version"),
        "current_policy_id": decision.policy_id if decision else None,
        "current_policy_version": decision.policy_version if decision else None,
        "current_autonomy_level": decision.autonomy_level if decision else None,
        "current_policy_reason": decision.reason if decision else None,
        "outcome": item["outcome"],
    }


def current_policy_version(paths: BrainPaths) -> int | None:
    with connection(paths.sqlite_path) as conn:
        return active_policy_version(conn)
