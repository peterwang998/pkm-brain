from __future__ import annotations

from typing import Any

from .cos_actions import apply_action_in_connection, target_state_hash
from .db import connection, dumps
from .paths import BrainPaths
from .review_resolution import (
    ReviewResolutionConflict,
    preflight_review_resolution_revoke,
    revoke_review_resolution,
)
from .review_undo import (
    ReviewUndoError,
    require_current_undo_handle_in_connection,
    restore_action_state_in_connection,
    safely_revert_action_in_connection,
)
from .util import now_iso


class QueueUndoTransactionError(ValueError):
    pass


def undo_queue_database(paths: BrainPaths, handle: dict[str, Any]) -> None:
    """Undo one sealed queue decision as a single SQLite transaction.

    The stale-handle guard, every inverse mutation, and review-ledger revocation
    share one write lock. Filesystem projections are deliberately not handled
    here; callers restore them only after this transaction commits.
    """

    try:
        with connection(paths.sqlite_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            require_current_undo_handle_in_connection(conn, handle)
            resolution_ids = stable_unique_strings(
                handle.get("review_resolution_ids") or []
            )
            for resolution_id in resolution_ids:
                preflight_review_resolution_revoke(conn, resolution_id)

            undo_queue_database_in_connection(paths, conn, handle)

            for resolution_id in resolution_ids:
                revoke_review_resolution(conn, resolution_id)
    except QueueUndoTransactionError:
        raise
    except (ReviewResolutionConflict, ReviewUndoError, ValueError) as exc:
        raise QueueUndoTransactionError(str(exc)) from exc


def undo_queue_database_in_connection(
    paths: BrainPaths, conn: Any, handle: dict[str, Any]
) -> None:
    kind = str(handle.get("kind") or "").strip()
    if kind in {"question_actions", "question_route", "legacy_question_answer"}:
        action_ids = list(handle.get("action_ids") or [])
        if not action_ids and handle.get("new_action_id"):
            action_ids = [handle["new_action_id"]]
        revert_actions(paths, conn, action_ids, label="question replacement")

        action_states = list(handle.get("actions") or [])
        if not action_states and handle.get("old_action"):
            action_states = [handle["old_action"]]
        for state in action_states:
            restore_action_state_in_connection(conn, state)
        restore_question_state_in_connection(conn, handle.get("question") or {})
        return

    if kind == "question_reject":
        restore_action_state_in_connection(conn, handle.get("action") or {})
        restore_question_state_in_connection(conn, handle.get("question") or {})
        return

    if kind == "memory_status":
        restore_memory_state_in_connection(conn, handle.get("memory") or {})
        return

    if kind == "fact_correction":
        revert_actions(
            paths,
            conn,
            handle.get("action_ids") or [],
            label="fact correction",
        )
        return

    if kind == "action_apply":
        action_id = str(handle.get("action_id") or "").strip()
        if not action_id:
            raise QueueUndoTransactionError("action undo target is required")
        safely_revert_action_in_connection(paths, conn, action_id)
        restore_action_state_in_connection(conn, handle.get("action") or {})
        return

    if kind in {"action_revert", "audit_fact_remediation", "audit_mark_ok"}:
        undo_audit_handles_in_connection(paths, conn, [handle])
        return

    if kind == "audit_batch_remediation":
        undo_audit_handles_in_connection(paths, conn, handle.get("handles") or [])
        return

    if kind == "action_status":
        restore_action_state_in_connection(conn, handle.get("action") or {})
        return

    raise QueueUndoTransactionError(f"unsupported undo handle: {kind}")


def undo_audit_handles_in_connection(
    paths: BrainPaths, conn: Any, handles: list[dict[str, Any]]
) -> None:
    for handle in reversed(handles):
        kind = str(handle.get("kind") or "")
        if kind == "action_revert":
            state = handle.get("action") or {}
            require_action_undo_precondition_in_connection(conn, handle, state)
            action_id = str(state.get("id") or "").strip()
            if action_id:
                applied = apply_action_in_connection(
                    paths,
                    conn,
                    action_id,
                    override_semantic_rejection=True,
                )
                if applied.get("status") not in {"applied", "auto_applied"}:
                    raise QueueUndoTransactionError(
                        "audited action could not be safely restored"
                    )
            restore_action_state_in_connection(conn, state)
            continue

        if kind == "audit_fact_remediation":
            revert_audit_action(
                paths,
                conn,
                handle.get("correction_action_id"),
                "audit remediation changed after review; undo was not applied",
            )
            restore_action_state_in_connection(conn, handle.get("action") or {})
            continue

        if kind == "audit_fact_batch_remediation":
            revert_audit_action(
                paths,
                conn,
                handle.get("correction_action_id"),
                "audit remediation changed after review; undo was not applied",
            )
            for state in handle.get("actions") or []:
                restore_action_state_in_connection(conn, state)
            continue

        if kind == "audit_mark_ok":
            revert_audit_action(
                paths,
                conn,
                handle.get("confirmation_action_id"),
                "fact confirmation changed after review; undo was not applied",
            )
            restore_action_state_in_connection(conn, handle.get("action") or {})
            continue

        raise QueueUndoTransactionError(f"unsupported audit batch undo handle: {kind}")


def revert_actions(
    paths: BrainPaths,
    conn: Any,
    action_ids: list[Any],
    *,
    label: str,
) -> None:
    for raw_action_id in action_ids:
        action_id = str(raw_action_id or "").strip()
        if not action_id:
            continue
        try:
            safely_revert_action_in_connection(paths, conn, action_id)
        except ReviewUndoError as exc:
            raise QueueUndoTransactionError(
                f"{label} could not be safely reverted: {action_id}; {exc}"
            ) from exc


def revert_audit_action(
    paths: BrainPaths,
    conn: Any,
    raw_action_id: Any,
    failure_message: str,
) -> None:
    action_id = str(raw_action_id or "").strip()
    if not action_id:
        return
    try:
        result = safely_revert_action_in_connection(paths, conn, action_id)
    except ReviewUndoError as exc:
        raise QueueUndoTransactionError(str(exc)) from exc
    if result.get("status") != "reverted":
        raise QueueUndoTransactionError(failure_message)


def require_action_undo_precondition_in_connection(
    conn: Any, handle: dict[str, Any], state: dict[str, Any]
) -> None:
    expected = str(handle.get("undo_precondition_hash") or "")
    current = target_state_hash(
        conn,
        target_fact_ids=state.get("target_fact_ids") or [],
        target_contract_ids=state.get("target_contract_ids") or [],
        target_page_paths=state.get("target_page_paths") or [],
    )
    if not expected or current != expected:
        raise QueueUndoTransactionError(
            "target state changed after review; undo was not applied"
        )


def restore_question_state_in_connection(conn: Any, state: dict[str, Any]) -> None:
    question_id = str(state.get("id") or "").strip()
    if not question_id:
        return
    restored = conn.execute(
        """
        UPDATE open_questions
        SET status = ?, answer = ?, answered_at = ?, action_id = ?, decided_by = ?
        WHERE id = ?
        """,
        (
            state.get("status"),
            dumps(state.get("answer")) if state.get("answer") is not None else None,
            state.get("answered_at"),
            state.get("action_id"),
            state.get("decided_by"),
            question_id,
        ),
    )
    if restored.rowcount != 1:
        raise QueueUndoTransactionError(
            f"question undo target no longer exists: {question_id}"
        )


def restore_memory_state_in_connection(conn: Any, state: dict[str, Any]) -> None:
    memory_id = str(state.get("id") or "").strip()
    if not memory_id:
        return
    restored = conn.execute(
        """
        UPDATE memories
        SET status = ?, reviewed_at = ?, review_reason = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            state.get("status"),
            state.get("reviewed_at"),
            state.get("review_reason"),
            now_iso(),
            memory_id,
        ),
    )
    if restored.rowcount != 1:
        raise QueueUndoTransactionError(
            f"memory undo target no longer exists: {memory_id}"
        )


def stable_unique_strings(values: list[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            output.append(text)
            seen.add(text)
    return output
