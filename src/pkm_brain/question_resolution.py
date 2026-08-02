from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from .cos_actions import apply_action
from .paths import BrainPaths
from .review_undo import ReviewUndoError, safely_revert_action
from .util import new_id, now_iso


class QuestionResolutionError(ValueError):
    pass


def action_ids_from_result(result: dict[str, Any]) -> list[str]:
    action_ids: list[str] = []
    if isinstance(result.get("action"), dict) and result["action"].get("id"):
        action_ids.append(str(result["action"]["id"]))
    for action in result.get("actions") or []:
        if isinstance(action, dict) and action.get("id"):
            action_ids.append(str(action["id"]))
    return action_ids


def ensure_applied_question_action(action: dict[str, Any], label: str) -> None:
    if action.get("status") not in {"applied", "auto_applied"}:
        raise QuestionResolutionError(
            f"{label} replacement did not apply (status: {action.get('status')})"
        )


def safely_revert_question_action(
    paths: BrainPaths,
    action_id: str,
) -> None:
    try:
        safely_revert_action(paths, action_id)
    except ReviewUndoError as exc:
        raise QuestionResolutionError(
            f"question replacement could not be safely reverted: {action_id}; {exc}"
        ) from exc


def undo_question_actions(
    paths: BrainPaths,
    action_ids: list[str],
) -> None:
    reverted_action_ids: list[str] = []
    try:
        for action_id in action_ids:
            safely_revert_question_action(
                paths,
                action_id,
            )
            reverted_action_ids.append(action_id)
    except Exception:
        for action_id in reversed(reverted_action_ids):
            reapplied = apply_action(
                paths,
                action_id,
                override_semantic_rejection=True,
            )
            ensure_applied_question_action(reapplied, "question undo rollback")
        raise


def finalize_question_review(
    paths: BrainPaths,
    undo_handle: dict[str, Any],
    write_resolution: Callable[[], None],
    restore_action: Callable[[BrainPaths, dict[str, Any] | None], None],
    restore_question: Callable[[BrainPaths, dict[str, Any]], None],
    revoke_resolution: Callable[[BrainPaths, str], None],
) -> None:
    try:
        write_resolution()
    except Exception as exc:
        rollback_error: Exception | None = None
        try:
            rollback_completed_question_review(
                paths,
                undo_handle,
                restore_action=restore_action,
                restore_question=restore_question,
            )
        except Exception as rollback_exc:
            rollback_error = rollback_exc
        for resolution_id in undo_handle.get("review_resolution_ids") or []:
            try:
                revoke_resolution(paths, str(resolution_id))
            except Exception as revoke_exc:
                rollback_error = rollback_error or revoke_exc
        if rollback_error is not None:
            raise QuestionResolutionError(
                "review resolution failed and the question decision could not be "
                "fully rolled back"
            ) from rollback_error
        raise QuestionResolutionError(
            "review resolution failed; the question decision was rolled back"
        ) from exc


def rollback_completed_question_review(
    paths: BrainPaths,
    undo_handle: dict[str, Any],
    *,
    restore_action: Callable[[BrainPaths, dict[str, Any] | None], None],
    restore_question: Callable[[BrainPaths, dict[str, Any]], None],
) -> None:
    action_ids = list(undo_handle.get("action_ids") or [])
    if not action_ids and undo_handle.get("new_action_id"):
        action_ids = [undo_handle["new_action_id"]]
    undo_question_actions(paths, [str(action_id) for action_id in action_ids])
    action_states = list(undo_handle.get("actions") or [])
    if not action_states and undo_handle.get("old_action"):
        action_states = [undo_handle["old_action"]]
    if not action_states and isinstance(undo_handle.get("action"), dict):
        action_states = [undo_handle["action"]]
    for state in action_states:
        restore_action(paths, state)
    restore_question(paths, undo_handle.get("question") or {})


@contextmanager
def question_replacement_guard(
    paths: BrainPaths,
    *,
    previous_question: dict[str, Any],
    previous_actions: list[dict[str, Any]],
    restore_action: Callable[[BrainPaths, dict[str, Any] | None], None],
    restore_question: Callable[[BrainPaths, dict[str, Any]], None],
) -> Iterator[list[str]]:
    applied_action_ids: list[str] = []
    try:
        yield applied_action_ids
    except Exception:
        rollback_error: Exception | None = None
        try:
            undo_question_actions(paths, list(reversed(applied_action_ids)))
        except Exception as exc:
            rollback_error = exc
        for state in previous_actions:
            try:
                restore_action(paths, state)
            except Exception as exc:
                rollback_error = rollback_error or exc
        try:
            restore_question(paths, previous_question)
        except Exception as exc:
            rollback_error = rollback_error or exc
        if rollback_error is not None:
            raise QuestionResolutionError(
                "replacement decision failed and could not be fully rolled back"
            ) from rollback_error
        raise


def supported_existing_fact(
    existing: dict[str, Any], candidate: dict[str, Any], question_id: str
) -> dict[str, Any]:
    timestamp = now_iso()
    metadata = (
        existing.get("metadata") if isinstance(existing.get("metadata"), dict) else {}
    )
    support_records = metadata.get("supporting_candidates")
    if not isinstance(support_records, list):
        support_records = []
    support_records.append(
        {
            "question_id": question_id,
            "statement": candidate.get("statement"),
            "evidence_quote": candidate.get("evidence_quote") or candidate.get("quote"),
            "source_ids": candidate.get("source_ids") or [],
            "observed_at": candidate.get("observed_at"),
            "attached_at": timestamp,
        }
    )
    source_spans = [
        *(existing.get("source_spans") or []),
        *(candidate.get("source_spans") or []),
    ]
    source_ids = list(
        dict.fromkeys(
            str(value).strip()
            for value in [
                *(existing.get("source_ids") or []),
                *(candidate.get("source_ids") or []),
            ]
            if str(value or "").strip()
        )
    )
    return {
        **existing,
        "source_ids": source_ids,
        "source_spans": source_spans,
        "metadata": {**metadata, "supporting_candidates": support_records[-25:]},
        "last_seen_at": candidate.get("observed_at") or timestamp,
    }


def temporal_update_fact(
    candidate: dict[str, Any], counterpart_ids: list[str], question_id: str
) -> dict[str, Any]:
    timestamp = now_iso()
    metadata = (
        candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    )
    return {
        **candidate,
        "id": str(candidate.get("id") or new_id("fact")),
        "status": "active",
        "supersedes_id": counterpart_ids[0],
        "metadata": {
            **metadata,
            "temporal_update": {
                "question_id": question_id,
                "superseded_fact_ids": counterpart_ids,
                "decided_at": timestamp,
            },
        },
        "last_seen_at": candidate.get("last_seen_at")
        or candidate.get("observed_at")
        or timestamp,
    }
