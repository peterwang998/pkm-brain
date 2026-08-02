from __future__ import annotations

from typing import Any

from .db import connection
from .paths import BrainPaths
from .review_resolution import (
    active_resolution_for_action,
    append_exact_open_sibling_closures,
    close_exact_open_siblings,
)
from .util import now_iso


APPLIED_STATUSES = {"applied", "auto_applied"}
OPEN_ACTION_STATUSES = {"proposed", "needs_human"}


def resolve_action_from_semantic_resolution(
    paths: BrainPaths, action: dict[str, Any]
) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        return enforce_action_review_resolution(conn, action)


def enforce_action_review_resolution(
    conn: Any, action: dict[str, Any]
) -> dict[str, Any]:
    if action.get("status") in APPLIED_STATUSES:
        return action
    resolution = active_resolution_for_action(conn, action)
    disposition = str((resolution or {}).get("disposition") or "")
    if disposition not in {"keep", "reject"}:
        return action

    enforced_at = now_iso()
    source_outcome = (
        "exact_semantic_state_already_kept"
        if disposition == "keep"
        else "exact_semantic_state_previously_rejected"
    )
    closures = close_exact_open_siblings(
        conn,
        action,
        disposition=disposition,
        resolution_id=str(resolution.get("id") or ""),
        resolved_at=str(resolution.get("resolved_at") or ""),
        preserve_source_action=False,
        source_outcome=source_outcome,
        semantic_resolution_fields={
            "decided_by": resolution.get("decided_by"),
            "enforced_at": enforced_at,
        },
        question_answered_at=enforced_at,
        include_non_open_source=True,
    )
    if closures["actions"] or closures["questions"]:
        append_exact_open_sibling_closures(conn, resolution, closures)
    row = conn.execute(
        "SELECT * FROM cos_actions WHERE id = ?", (action["id"],)
    ).fetchone()
    if row is None:
        return action
    from .cos_actions import row_to_action

    return row_to_action(row)
