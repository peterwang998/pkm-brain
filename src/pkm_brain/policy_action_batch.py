from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterator

from .cos_actions import (
    critic_review as review_action_with_critic,
    decide_action,
    get_action,
)
from .cos_policy import evaluate_policy
from .db import connection, dumps
from .paths import BrainPaths
from .util import now_iso


def decide_policy_actions(
    paths: BrainPaths,
    action_ids: list[str],
    *,
    critic_review: dict[str, Any],
) -> list[dict[str, Any]]:
    if not action_ids:
        return []
    worker_count = min(
        normalized_worker_count(critic_review.get("max_workers")), len(action_ids)
    )
    timeout_seconds = optional_positive_int(critic_review.get("timeout_seconds"))
    preparations = prepare_action_reviews(
        paths,
        action_ids,
        worker_count=worker_count,
        timeout_seconds=timeout_seconds,
    )
    disagreement_mode = str(critic_review.get("disagreement_mode") or "needs_human")
    decided: list[dict[str, Any]] = []
    for action_id, preparation in zip(action_ids, preparations):
        decided.append(
            finalize_policy_action(
                paths,
                action_id,
                preparation,
                critic_timeout_seconds=timeout_seconds,
                critic_disagreement_mode=disagreement_mode,
            )
        )
    return decided


def prepare_action_reviews(
    paths: BrainPaths,
    action_ids: list[str],
    *,
    worker_count: int,
    timeout_seconds: int | None,
) -> Iterator[dict[str, Any]]:
    if worker_count <= 1:
        for action_id in action_ids:
            yield prepare_policy_action_review_safely(
                paths, action_id, critic_timeout_seconds=timeout_seconds
            )
        return
    with ThreadPoolExecutor(
        max_workers=worker_count, thread_name_prefix="brain-critic"
    ) as executor:
        futures = [
            executor.submit(
                prepare_policy_action_review_safely,
                paths,
                action_id,
                critic_timeout_seconds=timeout_seconds,
            )
            for action_id in action_ids
        ]
        for future in futures:
            try:
                yield future.result()
            except Exception as exc:
                yield {"error": exc}


def prepare_policy_action_review_safely(
    paths: BrainPaths,
    action_id: str,
    *,
    critic_timeout_seconds: int | None,
) -> dict[str, Any]:
    try:
        return prepare_policy_action_review(
            paths,
            action_id,
            critic_timeout_seconds=critic_timeout_seconds,
        )
    except Exception as exc:
        return {"error": exc}


def prepare_policy_action_review(
    paths: BrainPaths,
    action_id: str,
    *,
    critic_timeout_seconds: int | None,
) -> dict[str, Any]:
    action = get_action(paths, action_id)
    with connection(paths.sqlite_path) as conn:
        decision = evaluate_policy(
            conn, action["action_type"], action.get("action_features") or {}
        )
    if not decision.critic_required:
        return {}
    review = review_action_with_critic(
        paths,
        action,
        decision,
        timeout_seconds=critic_timeout_seconds,
    )
    return {
        "critic_by": review.get("critic_by"),
        "critic_decision": review.get("decision"),
        "critic_rationale": review.get("rationale"),
        "precomputed_critic_review": review,
    }


def finalize_policy_action(
    paths: BrainPaths,
    action_id: str,
    preparation: dict[str, Any],
    *,
    critic_timeout_seconds: int | None,
    critic_disagreement_mode: str,
) -> dict[str, Any]:
    error = preparation.get("error")
    if isinstance(error, Exception):
        return mark_policy_action_decision_failure(paths, action_id, error)
    kwargs: dict[str, Any] = {
        "critic_timeout_seconds": critic_timeout_seconds,
        "critic_disagreement_mode": critic_disagreement_mode,
    }
    if preparation.get("critic_decision") == "evidence_incomplete":
        kwargs["precomputed_critic_review"] = preparation.get(
            "precomputed_critic_review"
        )
    else:
        kwargs.update(
            {
                key: preparation[key]
                for key in ("critic_by", "critic_decision", "critic_rationale")
                if preparation.get(key) is not None
            }
        )
    try:
        return decide_action(paths, action_id, **kwargs)
    except Exception as exc:
        return mark_policy_action_decision_failure(paths, action_id, exc)


def mark_policy_action_decision_failure(
    paths: BrainPaths,
    action_id: str,
    error: Exception,
) -> dict[str, Any]:
    action = get_action(paths, action_id)
    if action.get("status") in {"applied", "auto_applied"}:
        return action
    evidence = dict(action.get("evidence_json") or {})
    evidence["decision_failure"] = {
        "error_type": type(error).__name__,
        "message": str(error)[:1000],
        "at": now_iso(),
    }
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE cos_actions SET status = 'failed', evidence_json = ? WHERE id = ?",
            (dumps(evidence), action_id),
        )
    return get_action(paths, action_id)


def normalized_worker_count(value: Any) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None
