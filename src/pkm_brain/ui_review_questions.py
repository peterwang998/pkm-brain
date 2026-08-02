from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .cos_actions import apply_action, load_action, retire_open_candidate_siblings
from .db import connection, dumps
from .paths import BrainPaths
from .question_resolution import ensure_applied_question_action
from .review_undo import seal_undo_handle
from .service import BrainService
from .ui_errors import BadRequestError, NotFoundError
from .util import now_iso
from .wiki_facts import (
    answer_open_question,
    create_confirmed_page_fact,
    row_to_question,
    wiki_fact_dashboard,
)


ReviewBuilder = Callable[[BrainPaths, dict[str, list[str]]], dict[str, Any]]


def answer_wiki_question(
    paths: BrainPaths, question_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    BrainService(paths).init_workspace()
    raw_selected_fact_ids = payload.get("selected_fact_ids")
    if raw_selected_fact_ids is not None and not isinstance(
        raw_selected_fact_ids, list
    ):
        raise BadRequestError("selected_fact_ids must be an array")
    try:
        return answer_open_question(
            paths,
            question_id,
            selected_fact_id=optional_str(payload.get("selected_fact_id")),
            selected_fact_ids=[
                str(fact_id).strip()
                for fact_id in raw_selected_fact_ids or []
                if str(fact_id or "").strip()
            ]
            if raw_selected_fact_ids is not None
            else None,
            answer=optional_str(payload.get("answer")),
            overwrite_existing=bool(payload.get("overwrite_existing", False)),
        )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc


def apply_cos_question_action(
    paths: BrainPaths,
    question_id: str,
    payload: dict[str, Any],
    review_builder: ReviewBuilder,
) -> dict[str, Any]:
    BrainService(paths).init_workspace()
    question = review_question_for_decision(paths, question_id)
    action_id = str(question.get("action_id") or "").strip()
    if not action_id:
        raise BadRequestError(f"review question has no linked action: {question_id}")
    action = apply_action(
        paths,
        action_id,
        override_semantic_rejection=True,
    )
    ensure_applied_question_action(action, "selected candidate")
    answer_payload = {
        "decision": "apply_action",
        "action_id": action_id,
        "note": optional_str(payload.get("note")) or "",
    }
    mark_review_question_decided(
        paths,
        question_id,
        status="answered",
        answer=answer_payload,
        action_id=action_id,
    )
    return {
        "question": get_review_question(paths, question_id),
        "action": action,
        "review": review_builder(paths, review_query_for_question(question)),
        "dashboard": wiki_fact_dashboard(paths),
    }


def dismiss_cos_question(
    paths: BrainPaths,
    question_id: str,
    payload: dict[str, Any],
    review_builder: ReviewBuilder,
) -> dict[str, Any]:
    BrainService(paths).init_workspace()
    question = review_question_for_decision(paths, question_id)
    action_id = str(question.get("action_id") or "").strip()
    reason = optional_str(payload.get("reason")) or "human rejected review item"
    action: dict[str, Any] | None = None
    if action_id:
        action = reject_linked_review_action(paths, action_id, reason)
    answer_payload = {
        "decision": "dismiss",
        "action_id": action_id,
        "reason": reason,
    }
    mark_review_question_decided(
        paths,
        question_id,
        status="dismissed",
        answer=answer_payload,
        action_id=action_id or None,
    )
    return {
        "question": get_review_question(paths, question_id),
        "action": action,
        "review": review_builder(paths, review_query_for_question(question)),
        "dashboard": wiki_fact_dashboard(paths),
    }


def legacy_question_decision_response(decision: dict[str, Any]) -> dict[str, Any]:
    result = decision.get("result")
    response = dict(result) if isinstance(result, dict) else {}
    response["undo_handle"] = decision.get("undo_handle")
    response["queue_summary"] = decision.get("queue_summary")
    return response


def review_question_for_decision(paths: BrainPaths, question_id: str) -> dict[str, Any]:
    question = get_review_question(paths, question_id)
    if question["status"] not in {"open", "needs_human"}:
        raise BadRequestError(f"review question is already closed: {question_id}")
    return question


def review_query_for_question(question: dict[str, Any]) -> dict[str, list[str]]:
    kind = str(question.get("kind") or "").strip()
    return {"kind": [kind]} if kind else {}


def get_review_question(paths: BrainPaths, question_id: str) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        row = conn.execute(
            "SELECT * FROM open_questions WHERE id = ?", (question_id,)
        ).fetchone()
    if not row:
        raise NotFoundError(f"review question not found: {question_id}")
    return row_to_question(row)


def mark_review_question_decided(
    paths: BrainPaths,
    question_id: str,
    *,
    status: str,
    answer: dict[str, Any],
    action_id: str | None,
) -> None:
    with connection(paths.sqlite_path) as conn:
        updated = conn.execute(
            """
            UPDATE open_questions
            SET status = ?, answer = ?, answered_at = ?, action_id = COALESCE(?, action_id),
                decided_by = 'human'
            WHERE id = ?
            """,
            (status, dumps(answer), now_iso(), action_id, question_id),
        )
    if updated.rowcount != 1:
        raise NotFoundError(f"review question not found: {question_id}")


def annotate_answered_question_decision(
    paths: BrainPaths,
    result: dict[str, Any],
    *,
    original_question: dict[str, Any],
    decision: str,
) -> None:
    question = result.get("question")
    if not isinstance(question, dict):
        raise BadRequestError("question answer did not return a question")
    answer = question.get("answer")
    answer_payload = dict(answer) if isinstance(answer, dict) else {}
    answer_payload["decision"] = decision
    answer_payload["old_action_id"] = str(original_question.get("action_id") or "")
    mark_review_question_decided(
        paths,
        str(question["id"]),
        status="answered",
        answer=answer_payload,
        action_id=optional_str(question.get("action_id")),
    )
    result["question"] = get_review_question(paths, str(question["id"]))


def reject_linked_review_action(
    paths: BrainPaths, action_id: str, reason: str
) -> dict[str, Any]:
    timestamp = now_iso()
    with connection(paths.sqlite_path) as conn:
        action = load_action(conn, action_id)
        if action["status"] in {"applied", "auto_applied", "reverted"}:
            raise BadRequestError(
                f"linked action is already {action['status']}: {action_id}"
            )
        evidence = dict(action.get("evidence_json") or {})
        evidence["human_review"] = {
            "decision": "reject",
            "reason": reason,
            "decided_at": timestamp,
        }
        conn.execute(
            """
            UPDATE cos_actions
            SET status = 'rejected', evidence_json = ?
            WHERE id = ?
            """,
            (dumps(evidence), action_id),
        )
        retire_open_candidate_siblings(
            conn,
            action,
            reason="candidate rejected by human review",
        )
    with connection(paths.sqlite_path) as conn:
        return load_action(conn, action_id)


def ui_create_wiki_fact_correction(
    paths: BrainPaths, payload: dict[str, Any]
) -> dict[str, Any]:
    BrainService(paths).init_workspace()
    page_hint = str(payload.get("page_hint") or payload.get("path") or "").strip()
    if not page_hint:
        raise BadRequestError("page_hint is required")
    try:
        result = create_confirmed_page_fact(
            paths,
            page_hint,
            str(payload.get("statement") or ""),
            section_hint=str(payload.get("section_hint") or "Summary"),
            supersede_fact_ids=string_list(payload.get("supersede_fact_ids")),
            source_ids=string_list(payload.get("source_ids")),
            overwrite_existing=bool(payload.get("overwrite_existing", False)),
        )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    action_ids = [
        str(action.get("id") or "")
        for action in [result.get("supersede_action"), result.get("action")]
        if isinstance(action, dict) and str(action.get("id") or "").strip()
    ]
    result_fact = result.get("fact") if isinstance(result.get("fact"), dict) else {}
    canonical_page_hint = str(result_fact.get("page_hint") or page_hint)
    undo_handle = {
        "kind": "fact_correction",
        "action_ids": action_ids,
        "review_resolution_ids": result.pop("review_resolution_ids", []),
        "page_hints": [canonical_page_hint],
        "page_snapshot_ids": [
            str(page.get("snapshot_id"))
            for page in (result.get("curation") or {}).get("pages") or []
            if isinstance(page, dict) and page.get("snapshot_id")
        ],
    }
    seal_undo_handle(paths, undo_handle)
    result["undo_handle"] = undo_handle
    return result


def optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []
