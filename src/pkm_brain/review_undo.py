from __future__ import annotations

import hashlib
import json
from typing import Any

from .candidate_retirement import (
    candidate_sibling_retirement_ids,
    restore_retired_candidate_siblings,
)
from .cos_actions import (
    load_action,
    revert_action_in_connection,
    target_state_hash,
)
from .db import connection, dumps
from .paths import BrainPaths
from .review_resolution import (
    EXACT_SIBLING_CLOSURES_KEY,
    ReviewResolutionConflict,
    exact_open_sibling_restore_plan,
    exact_sibling_closure_journal,
)


class ReviewUndoError(ValueError):
    pass


ACTION_STATE_KEYS = (
    "id",
    "status",
    "audit_status",
    "target_fact_ids",
    "target_page_paths",
    "target_contract_ids",
    "evidence_json",
    "policy_decision",
    "autonomy_level",
    "inverse_action_json",
    "applied_state_hash",
    "applied_at",
    "reverted_at",
)


def capture_action_state(paths: BrainPaths, action_id: str) -> dict[str, Any] | None:
    if not action_id:
        return None
    with connection(paths.sqlite_path) as conn:
        return capture_action_state_in_connection(conn, action_id)


def capture_action_state_in_connection(
    conn: Any, action_id: str
) -> dict[str, Any] | None:
    if not action_id:
        return None
    try:
        action = load_action(conn, action_id)
    except ValueError:
        return None
    return {key: action.get(key) for key in ACTION_STATE_KEYS}


def restore_action_state(paths: BrainPaths, state: dict[str, Any] | None) -> None:
    if not state or not str(state.get("id") or "").strip():
        return
    with connection(paths.sqlite_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        restore_action_state_in_connection(conn, state)


def restore_action_state_in_connection(conn: Any, state: dict[str, Any] | None) -> None:
    if not state or not str(state.get("id") or "").strip():
        return
    try:
        current = load_action(conn, str(state["id"]))
    except ValueError as exc:
        raise ReviewUndoError(
            f"action undo target no longer exists: {state['id']}"
        ) from exc
    # Applied actions restore their retired siblings inside revert_action.
    # A rejected review decision has no action inverse, so restore its closure
    # in the same transaction as the source action state.
    if current.get("status") == "rejected":
        try:
            restore_retired_candidate_siblings(conn, current)
        except ValueError as exc:
            raise ReviewUndoError(str(exc)) from exc
    restored = conn.execute(
        """
        UPDATE cos_actions
        SET status = ?, audit_status = ?, target_fact_ids = ?,
            target_page_paths = ?, target_contract_ids = ?, evidence_json = ?,
            policy_decision = ?, autonomy_level = ?, inverse_action_json = ?,
            applied_state_hash = ?, applied_at = ?, reverted_at = ?
        WHERE id = ?
        """,
        (
            state.get("status"),
            state.get("audit_status"),
            dumps(state.get("target_fact_ids") or []),
            dumps(state.get("target_page_paths") or []),
            dumps(state.get("target_contract_ids") or []),
            dumps(state.get("evidence_json") or {}),
            state.get("policy_decision"),
            state.get("autonomy_level"),
            dumps(state.get("inverse_action_json"))
            if state.get("inverse_action_json") is not None
            else None,
            state.get("applied_state_hash"),
            state.get("applied_at"),
            state.get("reverted_at"),
            state["id"],
        ),
    )
    if restored.rowcount != 1:
        raise ReviewUndoError(f"action undo target no longer exists: {state['id']}")


def safely_revert_action(paths: BrainPaths, action_id: str) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        return safely_revert_action_in_connection(paths, conn, action_id)


def safely_revert_action_in_connection(
    paths: BrainPaths, conn: Any, action_id: str
) -> dict[str, Any]:
    try:
        action = load_action(conn, action_id)
    except ValueError as exc:
        raise ReviewUndoError(
            f"action undo target no longer exists: {action_id}"
        ) from exc
    expected = str(action.get("applied_state_hash") or "")
    current = target_state_hash(
        conn,
        target_fact_ids=action.get("target_fact_ids") or [],
        target_contract_ids=action.get("target_contract_ids") or [],
        target_page_paths=action.get("target_page_paths") or [],
    )
    if expected and current != expected:
        raise ReviewUndoError("action changed after review; undo was not applied")
    reverted = revert_action_in_connection(paths, conn, action_id)
    if reverted.get("status") != "reverted":
        raise ReviewUndoError("action changed after review; undo was not applied")
    return reverted


def seal_undo_handle(paths: BrainPaths, handle: dict[str, Any] | None) -> None:
    if not handle:
        return
    with connection(paths.sqlite_path) as conn:
        handle["undo_guard"] = {
            "version": 2,
            "resolution_closure_prefixes": resolution_closure_prefixes(conn, handle),
        }
        handle["undo_guard"]["fingerprint"] = undo_snapshot_fingerprint_in_connection(
            conn, handle
        )


def require_current_undo_handle(paths: BrainPaths, handle: dict[str, Any]) -> None:
    with connection(paths.sqlite_path) as conn:
        require_current_undo_handle_in_connection(conn, handle)


def require_current_undo_handle_in_connection(
    conn: Any, handle: dict[str, Any]
) -> None:
    guard = handle.get("undo_guard")
    expected = str((guard or {}).get("fingerprint") or "")
    if not expected or expected != undo_snapshot_fingerprint_in_connection(
        conn, handle
    ):
        raise ReviewUndoError(
            "undo target could not be safely reverted because the handle is stale "
            "or already used; no changes were made"
        )
    if (guard or {}).get("version") == 2:
        require_monotonic_resolution_closures(conn, handle)


def undo_snapshot_fingerprint(paths: BrainPaths, handle: dict[str, Any]) -> str:
    with connection(paths.sqlite_path) as conn:
        return undo_snapshot_fingerprint_in_connection(conn, handle)


def undo_snapshot_fingerprint_in_connection(conn: Any, handle: dict[str, Any]) -> str:
    identifiers = undo_identifiers(handle)
    snapshot: dict[str, Any] = {
        "handle": unguarded_handle_payload(handle),
        "actions": {},
        "questions": {},
        "memories": {},
        "resolutions": {},
    }
    if table_exists(conn, "review_resolutions"):
        tolerate_journal_appends = (handle.get("undo_guard") or {}).get(
            "version", 2
        ) == 2
        for resolution_id in sorted(identifiers["resolutions"]):
            row = conn.execute(
                "SELECT * FROM review_resolutions WHERE id = ?",
                (resolution_id,),
            ).fetchone()
            snapshot["resolutions"][resolution_id] = (
                resolution_row_without_closure_journal(row)
                if tolerate_journal_appends
                else dict(row)
                if row
                else None
            )
            if not tolerate_journal_appends:
                collect_resolution_closure_identifiers(row, identifiers)
    collect_candidate_retirement_closure_identifiers(conn, identifiers)
    for action_id in sorted(identifiers["actions"]):
        row = conn.execute(
            "SELECT * FROM cos_actions WHERE id = ?", (action_id,)
        ).fetchone()
        value = dict(row) if row else None
        if row:
            action = load_action(conn, action_id)
            value["current_target_state_hash"] = target_state_hash(
                conn,
                target_fact_ids=action.get("target_fact_ids") or [],
                target_contract_ids=action.get("target_contract_ids") or [],
                target_page_paths=action.get("target_page_paths") or [],
            )
        snapshot["actions"][action_id] = value
    for table, key in (
        ("open_questions", "questions"),
        ("memories", "memories"),
    ):
        if not table_exists(conn, table):
            continue
        for row_id in sorted(identifiers[key]):
            row = conn.execute(
                f"SELECT * FROM {table} WHERE id = ?", (row_id,)
            ).fetchone()
            snapshot[key][row_id] = dict(row) if row else None
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def resolution_row_without_closure_journal(row: Any) -> dict[str, Any] | None:
    """Fingerprint immutable receipt fields while allowing journal appends."""

    if row is None:
        return None
    result = dict(row)
    try:
        payload = json.loads(str(result.get("decision_payload") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        # Malformed payload remains in the stable fingerprint and will also be
        # rejected by the closure preflight.
        return result
    if isinstance(payload, dict):
        payload.pop(EXACT_SIBLING_CLOSURES_KEY, None)
        result["decision_payload"] = json.dumps(payload, sort_keys=True)
    return result


def resolution_closure_prefixes(
    conn: Any, handle: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    prefixes: dict[str, dict[str, Any]] = {}
    identifiers = undo_identifiers(handle)
    if not table_exists(conn, "review_resolutions"):
        return prefixes
    for resolution_id in sorted(identifiers["resolutions"]):
        row = conn.execute(
            "SELECT decision_payload FROM review_resolutions WHERE id = ?",
            (resolution_id,),
        ).fetchone()
        if row is None:
            continue
        try:
            payload = json.loads(str(row["decision_payload"] or "{}"))
            journal = exact_sibling_closure_journal(
                payload if isinstance(payload, dict) else {}, allow_missing=True
            )
        except (ReviewResolutionConflict, TypeError, ValueError) as exc:
            raise ReviewUndoError(str(exc)) from exc
        prefixes[resolution_id] = journal
    return prefixes


def require_monotonic_resolution_closures(conn: Any, handle: dict[str, Any]) -> None:
    guard = handle.get("undo_guard") or {}
    expected_prefixes = guard.get("resolution_closure_prefixes")
    if not isinstance(expected_prefixes, dict):
        raise ReviewUndoError(
            "undo target could not be safely reverted because the resolution "
            "journal guard is missing; no changes were made"
        )
    identifiers = undo_identifiers(handle)
    if set(expected_prefixes) != identifiers["resolutions"]:
        raise ReviewUndoError(
            "undo target could not be safely reverted because the resolution "
            "journal guard is stale; no changes were made"
        )
    try:
        for resolution_id in sorted(identifiers["resolutions"]):
            row = conn.execute(
                """
                SELECT decision_payload, disposition, resolved_at
                FROM review_resolutions WHERE id = ?
                """,
                (resolution_id,),
            ).fetchone()
            if row is None:
                raise ReviewResolutionConflict(
                    "review resolution changed; undo was not applied"
                )
            payload = json.loads(str(row["decision_payload"] or "{}"))
            payload = payload if isinstance(payload, dict) else {}
            current = exact_sibling_closure_journal(payload, allow_missing=True)
            expected = exact_sibling_closure_journal(
                {EXACT_SIBLING_CLOSURES_KEY: expected_prefixes[resolution_id]},
                allow_missing=False,
            )
            for key in ("actions", "questions"):
                prefix = expected[key]
                if current[key][: len(prefix)] != prefix:
                    raise ReviewResolutionConflict(
                        "review resolution rollback journal is not append-only; "
                        "undo was not applied"
                    )
            exact_open_sibling_restore_plan(
                conn,
                payload,
                resolution_id=resolution_id,
                disposition=str(row["disposition"] or ""),
                resolved_at=str(row["resolved_at"] or ""),
            )
    except (
        ReviewResolutionConflict,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise ReviewUndoError(
            "undo target could not be safely reverted because the handle is stale "
            f"or its resolution journal changed; no changes were made ({exc})"
        ) from exc


def collect_candidate_retirement_closure_identifiers(
    conn: Any, identifiers: dict[str, set[str]]
) -> None:
    pending = list(identifiers["actions"])
    visited: set[str] = set()
    while pending:
        action_id = pending.pop()
        if action_id in visited:
            continue
        visited.add(action_id)
        try:
            action = load_action(conn, action_id)
        except ValueError:
            continue
        action_ids, question_ids = candidate_sibling_retirement_ids(action)
        for sibling_id in action_ids:
            if sibling_id not in identifiers["actions"]:
                identifiers["actions"].add(sibling_id)
                pending.append(sibling_id)
        identifiers["questions"].update(question_ids)


def collect_resolution_closure_identifiers(
    row: Any, identifiers: dict[str, set[str]]
) -> None:
    if row is None:
        return
    try:
        payload = json.loads(str(row["decision_payload"] or "{}"))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return
    closures = payload.get("exact_open_sibling_closures")
    if not isinstance(closures, dict) or closures.get("version") != 1:
        return
    for state in closures.get("actions") or []:
        if isinstance(state, dict):
            add_identifier(identifiers["actions"], state.get("id"))
    for state in closures.get("questions") or []:
        if isinstance(state, dict):
            add_identifier(identifiers["questions"], state.get("id"))


def unguarded_handle_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): unguarded_handle_payload(item)
            for key, item in value.items()
            if str(key) != "undo_guard"
        }
    if isinstance(value, (list, tuple)):
        return [unguarded_handle_payload(item) for item in value]
    return value


def undo_identifiers(handle: dict[str, Any]) -> dict[str, set[str]]:
    result = {
        "actions": set(),
        "questions": set(),
        "memories": set(),
        "resolutions": set(),
    }
    collect_undo_identifiers(handle, result)
    return result


def collect_undo_identifiers(
    handle: dict[str, Any], result: dict[str, set[str]]
) -> None:
    for key in (
        "action_id",
        "new_action_id",
        "correction_action_id",
        "confirmation_action_id",
    ):
        add_identifier(result["actions"], handle.get(key))
    for key in ("action", "old_action"):
        value = handle.get(key)
        if isinstance(value, dict):
            add_identifier(result["actions"], value.get("id"))
    for value in handle.get("actions") or []:
        if isinstance(value, dict):
            add_identifier(result["actions"], value.get("id"))
    for value in handle.get("action_ids") or []:
        add_identifier(result["actions"], value)
    question = handle.get("question")
    if isinstance(question, dict):
        add_identifier(result["questions"], question.get("id"))
    memory = handle.get("memory")
    if isinstance(memory, dict):
        add_identifier(result["memories"], memory.get("id"))
    for value in handle.get("review_resolution_ids") or []:
        add_identifier(result["resolutions"], value)
    for nested in handle.get("handles") or []:
        if isinstance(nested, dict):
            collect_undo_identifiers(nested, result)


def add_identifier(target: set[str], value: Any) -> None:
    identifier = str(value or "").strip()
    if identifier:
        target.add(identifier)


def table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
    )
