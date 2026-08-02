from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from .db import dumps, loads
from .util import now_iso


CANDIDATE_SIBLING_RETIREMENTS_KEY = "candidate_sibling_retirements"
CANDIDATE_SIBLING_RETIREMENTS_VERSION = 1


def action_candidate_key(action: Mapping[str, Any]) -> str:
    features = action.get("action_features")
    if isinstance(features, dict):
        key = str(features.get("candidate_key") or "").strip()
        if key:
            return key
    evidence = action.get("evidence_json")
    payload = evidence.get("payload") if isinstance(evidence, dict) else None
    if not isinstance(payload, dict):
        return ""
    candidate = payload.get("candidate")
    if isinstance(candidate, dict):
        return str(candidate.get("candidate_key") or "").strip()
    return str(payload.get("candidate_key") or "").strip()


def retire_open_candidate_siblings(
    conn: Any, action: dict[str, Any], *, reason: str
) -> list[str]:
    candidate_key = action_candidate_key(action)
    if not candidate_key:
        return []
    sibling_ids: list[str] = []
    action_states: list[dict[str, Any]] = []
    question_states: list[dict[str, Any]] = []
    retired_at = now_iso()
    rows = conn.execute(
        """
        SELECT *
        FROM cos_actions
        WHERE action_type = ?
          AND status IN ('proposed', 'needs_human')
          AND id != ?
        ORDER BY created_at, id
        """,
        (action["action_type"], action["id"]),
    )
    for row in rows:
        sibling = {
            "id": row["id"],
            "action_features": loads(row["action_features"], {}),
            "evidence_json": loads(row["evidence_json"], {}),
        }
        if action_candidate_key(sibling) != candidate_key:
            continue
        sibling_ids.append(str(sibling["id"]))
        before = dict(row)
        evidence = dict(sibling["evidence_json"] or {})
        evidence["candidate_superseded"] = {
            "by_action_id": action["id"],
            "candidate_key": candidate_key,
            "reason": reason,
            "at": retired_at,
        }
        after = {**before, "status": "dismissed", "evidence_json": dumps(evidence)}
        action_states.append(
            {
                "id": str(sibling["id"]),
                "before": {
                    "status": before.get("status"),
                    "evidence_json": before.get("evidence_json"),
                },
                "after_fingerprint": database_row_fingerprint(after),
            }
        )
        conn.execute(
            "UPDATE cos_actions SET status = 'dismissed', evidence_json = ? WHERE id = ?",
            (dumps(evidence), sibling["id"]),
        )
    if sibling_ids:
        question_states = retire_linked_questions(
            conn,
            sibling_ids=sibling_ids,
            source_action_id=str(action["id"]),
            candidate_key=candidate_key,
            reason=reason,
            retired_at=retired_at,
        )
    if action_states or question_states:
        source = conn.execute(
            "SELECT evidence_json FROM cos_actions WHERE id = ?", (action["id"],)
        ).fetchone()
        if source is None:
            raise ValueError(f"cos action not found: {action['id']}")
        source_evidence = loads(source["evidence_json"], {})
        source_evidence = (
            dict(source_evidence) if isinstance(source_evidence, dict) else {}
        )
        source_evidence[CANDIDATE_SIBLING_RETIREMENTS_KEY] = {
            "version": CANDIDATE_SIBLING_RETIREMENTS_VERSION,
            "source_action_id": str(action["id"]),
            "candidate_key": candidate_key,
            "retired_at": retired_at,
            "actions": action_states,
            "questions": question_states,
        }
        conn.execute(
            "UPDATE cos_actions SET evidence_json = ? WHERE id = ?",
            (dumps(source_evidence), action["id"]),
        )
    return sibling_ids


def retire_linked_questions(
    conn: Any,
    *,
    sibling_ids: list[str],
    source_action_id: str,
    candidate_key: str,
    reason: str,
    retired_at: str,
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in sibling_ids)
    answer = dumps(
        {
            "decision": "obsolete",
            "reason": reason,
            "superseded_by_action_id": source_action_id,
            "candidate_key": candidate_key,
        }
    )
    rows = conn.execute(
        f"""
        SELECT * FROM open_questions
        WHERE action_id IN ({placeholders})
          AND status IN ('open', 'needs_human')
        ORDER BY created_at, id
        """,
        sibling_ids,
    ).fetchall()
    states: list[dict[str, Any]] = []
    for row in rows:
        before = dict(row)
        after = {
            **before,
            "status": "dismissed",
            "answer": answer,
            "answered_at": retired_at,
            "decided_by": "candidate_deduplication",
        }
        states.append(
            {
                "id": str(row["id"]),
                "before": {
                    "status": before.get("status"),
                    "answer": before.get("answer"),
                    "answered_at": before.get("answered_at"),
                    "decided_by": before.get("decided_by"),
                },
                "after_fingerprint": database_row_fingerprint(after),
            }
        )
    conn.execute(
        f"""
        UPDATE open_questions
        SET status = 'dismissed', answer = ?, answered_at = ?,
            decided_by = 'candidate_deduplication'
        WHERE action_id IN ({placeholders})
          AND status IN ('open', 'needs_human')
        """,
        [answer, retired_at, *sibling_ids],
    )
    return states


def database_row_fingerprint(row: dict[str, Any]) -> str:
    encoded = dumps(row)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def candidate_sibling_retirement_states(
    action: Mapping[str, Any],
) -> dict[str, Any] | None:
    evidence = action.get("evidence_json")
    if not isinstance(evidence, dict):
        return None
    closure = evidence.get(CANDIDATE_SIBLING_RETIREMENTS_KEY)
    return dict(closure) if isinstance(closure, dict) else None


def candidate_sibling_retirement_ids(
    action: Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    closure = candidate_sibling_retirement_states(action) or {}
    action_ids = [
        str(state.get("id") or "")
        for state in closure.get("actions") or []
        if isinstance(state, dict) and str(state.get("id") or "").strip()
    ]
    question_ids = [
        str(state.get("id") or "")
        for state in closure.get("questions") or []
        if isinstance(state, dict) and str(state.get("id") or "").strip()
    ]
    return action_ids, question_ids


def restore_retired_candidate_siblings(conn: Any, action: Mapping[str, Any]) -> None:
    """Restore exactly the candidate rows retired by ``action``, or do nothing."""

    closure = candidate_sibling_retirement_states(action)
    if closure is None:
        return
    if closure.get("version") != CANDIDATE_SIBLING_RETIREMENTS_VERSION:
        raise ValueError("candidate sibling retirement snapshot is not supported")
    action_states = valid_states(closure.get("actions"))
    question_states = valid_states(closure.get("questions"))
    require_unchanged_retirements(conn, "cos_actions", action_states)
    require_unchanged_retirements(conn, "open_questions", question_states)
    for state in question_states:
        before = state.get("before") if isinstance(state.get("before"), dict) else {}
        conn.execute(
            """
            UPDATE open_questions
            SET status = ?, answer = ?, answered_at = ?, decided_by = ?
            WHERE id = ?
            """,
            (
                before.get("status"),
                before.get("answer"),
                before.get("answered_at"),
                before.get("decided_by"),
                state.get("id"),
            ),
        )
    for state in action_states:
        before = state.get("before") if isinstance(state.get("before"), dict) else {}
        conn.execute(
            "UPDATE cos_actions SET status = ?, evidence_json = ? WHERE id = ?",
            (before.get("status"), before.get("evidence_json"), state.get("id")),
        )


def valid_states(value: Any) -> list[dict[str, Any]]:
    return [dict(state) for state in value or [] if isinstance(state, dict)]


def require_unchanged_retirements(
    conn: Any, table: str, states: list[dict[str, Any]]
) -> None:
    for state in states:
        row_id = str(state.get("id") or "").strip()
        expected = str(state.get("after_fingerprint") or "").strip()
        row = (
            conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
            if row_id
            else None
        )
        if (
            row is None
            or not expected
            or database_row_fingerprint(dict(row)) != expected
        ):
            raise ValueError(
                "candidate sibling state changed after review; undo was not applied"
            )
