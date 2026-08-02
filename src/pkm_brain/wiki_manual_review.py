from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .cos_actions import propose_action
from .db import connection, dumps
from .paths import BrainPaths
from .review_undo import ReviewUndoError, safely_revert_action
from .review_resolution import synthetic_fact_action
from .util import new_id, now_iso, stable_unique
from .wiki import lint_wiki


@dataclass(frozen=True)
class WikiManualReviewHooks:
    """Wiki projection callbacks kept outside the manual-review state machine."""

    apply_action: Callable[..., dict[str, Any]]
    apply_fact_status_action: Callable[..., dict[str, Any]]
    canonical_page_hint_for_fact: Callable[[str], str]
    compact_statement: Callable[[Any, int], str]
    curate_managed_pages: Callable[..., dict[str, Any]]
    entity_key_for_change: Callable[[str, str, str], str]
    get_fact: Callable[[BrainPaths, str], dict[str, Any]]
    get_question: Callable[[BrainPaths, str], dict[str, Any]]
    managed_fact_page_review: Callable[[BrainPaths, str], dict[str, Any]]
    record_wiki_page_snapshot: Callable[..., str]
    record_review_resolution: Callable[..., tuple[dict[str, Any], bool]]
    row_to_fact: Callable[[Any], dict[str, Any]]
    row_to_page_snapshot: Callable[[Any], dict[str, Any]]
    row_to_question: Callable[[Any], dict[str, Any]]
    safe_fact_wiki_path: Callable[[BrainPaths, str], Path]
    sync_restored_page_projection_metadata: Callable[[BrainPaths, Path, str], None]
    topic_for_path: Callable[[str], str]
    wiki_fact_dashboard: Callable[[BrainPaths], dict[str, Any]]


def curate_after_committed_mutation(
    paths: BrainPaths,
    page_hints: list[str],
    *,
    hooks: WikiManualReviewHooks,
    overwrite_existing: bool,
) -> tuple[dict[str, Any], list[str]]:
    """Keep a committed truth edit visible when its derived projection fails."""

    try:
        page_states = capture_page_projection_states(paths, page_hints, hooks=hooks)
    except Exception as exc:
        return projection_failure_result(
            {
                "pages": [],
                "lint": None,
                "lint_errors": [],
            },
            [
                "managed-page projection was not attempted because its "
                f"preflight snapshot failed: {type(exc).__name__}: {exc}"
            ],
        )
    try:
        curation = hooks.curate_managed_pages(
            paths,
            page_hints=page_hints,
            overwrite_existing=overwrite_existing,
        )
    except Exception as exc:
        pages, recovery_reasons = preserve_partial_projection_snapshots(
            paths,
            page_states,
            hooks=hooks,
            projection_error=exc,
        )
        return projection_failure_result(
            {
                "pages": pages,
                "lint": None,
                "lint_errors": [],
            },
            [f"{type(exc).__name__}: {exc}", *recovery_reasons],
        )
    failure_reasons = structured_projection_failure_reasons(curation, page_hints)
    if failure_reasons:
        return projection_failure_result(curation, failure_reasons)
    return curation, []


def capture_page_projection_states(
    paths: BrainPaths,
    page_hints: list[str],
    *,
    hooks: WikiManualReviewHooks,
) -> list[dict[str, Any]]:
    """Capture exact target-file state before a fallible managed projection."""

    states: list[dict[str, Any]] = []
    for page_hint in stable_unique(
        str(value).strip() for value in page_hints if str(value or "").strip()
    ):
        try:
            target = hooks.safe_fact_wiki_path(paths, page_hint)
        except ValueError:
            continue
        states.append(
            {
                "page_hint": page_hint,
                "target": target,
                "before_markdown": (
                    target.read_text(encoding="utf-8", errors="replace")
                    if target.exists()
                    else None
                ),
            }
        )
    return states


def preserve_partial_projection_snapshots(
    paths: BrainPaths,
    page_states: list[dict[str, Any]],
    *,
    hooks: WikiManualReviewHooks,
    projection_error: Exception,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Make every file mutation before a projection exception safely undoable.

    A curator can write a page and then fail during lint, indexing, or snapshot
    persistence. Record the observed before/after pair after the exception so a
    queue Undo handle can restore it. If that durable record cannot be written,
    compensate the file mutation immediately instead of leaving an untracked
    projection behind.
    """

    pages: list[dict[str, Any]] = []
    recovery_reasons: list[str] = []
    for state in page_states:
        page_hint = str(state["page_hint"])
        target = state["target"]
        before_markdown = state.get("before_markdown")
        after_markdown = (
            target.read_text(encoding="utf-8", errors="replace")
            if target.exists()
            else None
        )
        page_result: dict[str, Any] = {
            "page_hint": page_hint,
            "path": str(target),
            "relative_path": page_hint,
            "written": False,
            "reason": "projection failure left no untracked page change",
            "recovery_status": "unchanged_or_compensated",
        }
        if after_markdown == before_markdown:
            pages.append(page_result)
            continue
        page_result["written"] = after_markdown is not None
        try:
            snapshot_id = hooks.record_wiki_page_snapshot(
                paths,
                page_hint,
                before_markdown=before_markdown,
                after_markdown=after_markdown,
                reason="managed_projection_exception",
                metadata={
                    "projection_error": (
                        f"{type(projection_error).__name__}: {projection_error}"
                    )
                },
            )
        except Exception as snapshot_exc:
            try:
                compensate_partial_page_projection(
                    paths,
                    target,
                    before_markdown,
                    hooks=hooks,
                )
            except Exception as compensation_exc:
                page_result.update(
                    {
                        "reason": "partial managed page could not be recovered",
                        "recovery_status": "failed",
                    }
                )
                recovery_reasons.append(
                    f"{page_hint}: snapshot persistence failed "
                    f"({type(snapshot_exc).__name__}: {snapshot_exc}) and "
                    f"compensation failed "
                    f"({type(compensation_exc).__name__}: {compensation_exc})"
                )
            else:
                page_result.update(
                    {
                        "written": False,
                        "reason": "partial managed page was compensated",
                        "recovery_status": "compensated",
                    }
                )
                recovery_reasons.append(
                    f"{page_hint}: snapshot persistence failed; "
                    "the partial page write was compensated"
                )
        else:
            page_result.update(
                {
                    "snapshot_id": snapshot_id,
                    "reason": "projection failed after the managed page changed",
                    "recovery_status": "snapshot_recorded",
                }
            )
        pages.append(page_result)
    return pages, recovery_reasons


def compensate_partial_page_projection(
    paths: BrainPaths,
    target: Path,
    before_markdown: str | None,
    *,
    hooks: WikiManualReviewHooks,
) -> None:
    """Restore a partial projection when its durable snapshot cannot be saved."""

    if before_markdown is None:
        if target.exists():
            target.unlink()
        with connection(paths.sqlite_path) as conn:
            conn.execute("DELETE FROM wiki_pages WHERE path = ?", (str(target),))
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(before_markdown, encoding="utf-8")
    hooks.sync_restored_page_projection_metadata(paths, target, before_markdown)


def structured_projection_failure_reasons(
    curation: dict[str, Any], page_hints: list[str]
) -> list[str]:
    reasons = [str(error) for error in curation.get("lint_errors") or []]
    pages = curation.get("pages")
    page_results = pages if isinstance(pages, list) else []
    for page_hint in stable_unique(
        str(value).strip() for value in page_hints if str(value or "").strip()
    ):
        page = next(
            (
                candidate
                for candidate in page_results
                if isinstance(candidate, dict)
                and str(
                    candidate.get("page_hint") or candidate.get("relative_path") or ""
                )
                == page_hint
            ),
            None,
        )
        if page is None:
            reasons.append(f"{page_hint}: projection returned no target-page result")
            continue
        reason = str(page.get("reason") or "").strip()
        if page.get("written") is not True:
            reasons.append(reason or f"{page_hint}: managed page was not written")
        elif reason:
            reasons.append(reason)
    return stable_unique(reason for reason in reasons if reason)


def projection_failure_result(
    curation: dict[str, Any], reasons: list[str]
) -> tuple[dict[str, Any], list[str]]:
    cause = "; ".join(reasons)
    warning = (
        "Knowledge changes were committed, but managed-page projection failed; "
        f"retry page regeneration. Cause: {cause}"
    )
    return (
        {
            **curation,
            "projection_status": "failed",
            "retryable": True,
            "warning": warning,
        },
        [warning],
    )


def answer_open_question(
    paths: BrainPaths,
    question_id: str,
    *,
    hooks: WikiManualReviewHooks,
    selected_fact_id: str | None = None,
    selected_fact_ids: list[str] | None = None,
    answer: str | None = None,
    overwrite_existing: bool = False,
) -> dict[str, Any]:
    timestamp = now_iso()
    with connection(paths.sqlite_path) as conn:
        question_row = conn.execute(
            "SELECT * FROM open_questions WHERE id = ?", (question_id,)
        ).fetchone()
        if not question_row:
            raise ValueError(f"open question not found: {question_id}")
        question = hooks.row_to_question(question_row)
        if question["status"] not in {"open", "needs_human"}:
            raise ValueError(f"question is not open: {question_id}")
        fact_ids = [str(fact_id) for fact_id in question.get("fact_ids") or []]
        page_hint = question.get("page_hint")
    actions: list[dict[str, Any]] = []
    selection_requested = selected_fact_id is not None or selected_fact_ids is not None
    selected_ids = stable_unique(
        str(fact_id).strip()
        for fact_id in [selected_fact_id, *(selected_fact_ids or [])]
        if str(fact_id or "").strip()
    )
    if selection_requested:
        if not selected_ids:
            raise ValueError("selected_fact_ids must contain at least one fact")
        invalid_ids = [fact_id for fact_id in selected_ids if fact_id not in fact_ids]
        if invalid_ids:
            raise ValueError("selected_fact_ids contains a fact outside the question")
        updates = [
            *[
                {
                    "fact_id": fact_id,
                    "status": "active",
                    "confirmed_by_user": True,
                    "conflict_group_id": None,
                }
                for fact_id in fact_ids
                if fact_id in selected_ids
            ],
            *[
                {
                    "fact_id": fact_id,
                    "status": "retracted",
                    "conflict_group_id": None,
                }
                for fact_id in fact_ids
                if fact_id not in selected_ids
            ],
        ]
        action = hooks.apply_action(
            paths,
            propose_action(
                paths,
                "resolve_conflict",
                action_payload={"updates": updates, "question_id": question_id},
                action_features={
                    "human_confirmed": True,
                    "truth_mutation": True,
                    "reversible": True,
                    "affected_fact_count": len(updates),
                },
                target_fact_ids=fact_ids,
                target_page_paths=[str(page_hint)] if page_hint else [],
                proposed_by="question_answer",
                confidence=1.0,
                risk_tier="medium",
            )["id"],
        )
        require_applied_answer_action(action, "fact selection")
        actions.append(action)
        answer_payload = {
            "selected_fact_ids": selected_ids,
            "selected_fact_id": selected_ids[0] if len(selected_ids) == 1 else None,
            "superseded_fact_ids": [
                fact_id for fact_id in fact_ids if fact_id not in selected_ids
            ],
            "answer": answer or "",
        }
    else:
        answer_text = hooks.compact_statement(answer or "", 1000)
        if not answer_text:
            raise ValueError("answer or selected_fact_id is required")
        manual_fact_id = new_id("fact")
        upsert_action = hooks.apply_action(
            paths,
            propose_action(
                paths,
                "fact_upsert",
                action_payload={
                    "fact": {
                        "id": manual_fact_id,
                        "statement": answer_text,
                        "entity_key": question.get("entity_key")
                        or f"manual:{question_id}",
                        "page_hint": page_hint,
                        "section_hint": "Summary",
                        "source_ids": [f"manual:question:{question_id}"],
                        "observed_at": timestamp,
                        "confidence": 1.0,
                        "status": "active",
                        "confirmed_by_user": True,
                        "metadata": {"question_id": question_id, "answer": answer_text},
                        "created_at": timestamp,
                        "last_seen_at": timestamp,
                    }
                },
                action_features={
                    "human_confirmed": True,
                    "truth_mutation": True,
                    "reversible": True,
                    "affected_fact_count": 1,
                },
                target_fact_ids=[manual_fact_id],
                target_page_paths=[str(page_hint)] if page_hint else [],
                proposed_by="question_answer",
                confidence=1.0,
                risk_tier="medium",
            )["id"],
        )
        require_applied_answer_action(upsert_action, "manual answer")
        actions.append(upsert_action)
        if fact_ids:
            try:
                supersede_action = hooks.apply_action(
                    paths,
                    propose_action(
                        paths,
                        "fact_supersede",
                        action_payload={
                            "updates": [
                                {
                                    "fact_id": fact_id,
                                    "status": "retracted",
                                    "conflict_group_id": None,
                                }
                                for fact_id in fact_ids
                            ],
                            "question_id": question_id,
                        },
                        action_features={
                            "human_confirmed": True,
                            "truth_mutation": True,
                            "reversible": True,
                            "affected_fact_count": len(fact_ids),
                        },
                        target_fact_ids=fact_ids,
                        target_page_paths=[str(page_hint)] if page_hint else [],
                        proposed_by="question_answer",
                        confidence=1.0,
                        risk_tier="medium",
                    )["id"],
                )
                require_applied_answer_action(
                    supersede_action, "manual answer supersession"
                )
            except Exception:
                rollback_question_answer_actions(paths, actions)
                raise
            actions.append(supersede_action)
        answer_payload = {"selected_fact_id": manual_fact_id, "answer": answer_text}
    try:
        with connection(paths.sqlite_path) as conn:
            conn.execute(
                """
                UPDATE open_questions
                SET status = 'answered', answer = ?, answered_at = ?, action_id = ?,
                    decided_by = 'human'
                WHERE id = ?
                """,
                (
                    dumps(answer_payload),
                    timestamp,
                    actions[-1]["id"] if actions else None,
                    question_id,
                ),
            )
    except Exception:
        rollback_question_answer_actions(paths, actions)
        raise
    curation, warnings = curate_after_committed_mutation(
        paths,
        [str(page_hint)] if page_hint else [],
        hooks=hooks,
        overwrite_existing=overwrite_existing,
    )
    result = {
        "question": hooks.get_question(paths, question_id),
        "actions": actions,
        "curation": curation,
        "dashboard": hooks.wiki_fact_dashboard(paths),
    }
    if warnings:
        result.update(
            {
                "status": "committed_with_projection_warning",
                "warnings": warnings,
            }
        )
    return result


def require_applied_answer_action(action: dict[str, Any], label: str) -> None:
    if action.get("status") not in {"applied", "auto_applied"}:
        raise ValueError(
            f"{label} action did not apply (status: {action.get('status')})"
        )


def rollback_question_answer_actions(
    paths: BrainPaths, actions: list[dict[str, Any]]
) -> None:
    rollback_error: Exception | None = None
    for action in reversed(actions):
        try:
            safely_revert_action(paths, str(action.get("id") or ""))
        except ReviewUndoError as exc:
            rollback_error = rollback_error or exc
    if rollback_error is not None:
        raise ValueError(
            "question answer failed and could not be fully rolled back"
        ) from rollback_error


def create_confirmed_page_fact(
    paths: BrainPaths,
    page_hint: str,
    statement: str,
    *,
    hooks: WikiManualReviewHooks,
    section_hint: str = "Summary",
    supersede_fact_ids: list[str] | None = None,
    source_ids: list[str] | None = None,
    overwrite_existing: bool = False,
) -> dict[str, Any]:
    page_hint = hooks.canonical_page_hint_for_fact(str(page_hint or "").strip())
    statement = hooks.compact_statement(statement, 1000)
    if not page_hint:
        raise ValueError("page_hint is required")
    if not statement:
        raise ValueError("statement is required")
    section_hint = str(section_hint or "Summary").strip() or "Summary"
    supersede_fact_ids = stable_unique(
        str(fact_id) for fact_id in supersede_fact_ids or []
    )
    timestamp = now_iso()
    fact_id = new_id("fact")
    effective_source_ids = stable_unique(
        [*(source_ids or []), f"manual:chief-of-staff:{timestamp}"]
    )
    entity_key = hooks.entity_key_for_change(
        hooks.topic_for_path(page_hint), page_hint, section_hint
    )
    old_facts_by_id: dict[str, dict[str, Any]] = {}
    with connection(paths.sqlite_path) as conn:
        if supersede_fact_ids:
            placeholders = ",".join("?" for _ in supersede_fact_ids)
            found_facts = [
                hooks.row_to_fact(row)
                for row in conn.execute(
                    f"""
                    SELECT *
                    FROM facts
                    WHERE id IN ({placeholders})
                      AND page_hint = ?
                      AND status != 'retracted' AND knowledge_to IS NULL
                    """,
                    (*supersede_fact_ids, page_hint),
                )
            ]
            old_facts_by_id = {str(fact["id"]): fact for fact in found_facts}
            missing = sorted(set(supersede_fact_ids) - set(old_facts_by_id))
            if missing:
                raise ValueError(
                    f"supersede facts not found on page: {', '.join(missing)}"
                )
    old_facts = [old_facts_by_id[fact_id] for fact_id in supersede_fact_ids]
    resolution_ids: list[str] = []
    applied_action_ids: list[str] = []

    def record_correction_resolution(
        conn: Any,
        reviewed_action: dict[str, Any],
        *,
        disposition: str,
        corrected_away_fact_id: str | None = None,
    ) -> None:
        resolution, created = hooks.record_review_resolution(
            conn,
            reviewed_action,
            disposition=disposition,
            source_item_kind="wiki_correction",
            source_item_id=fact_id,
            decision_payload={
                "replacement_fact_id": fact_id,
                "corrected_away_fact_id": corrected_away_fact_id,
                "page_hint": page_hint,
            },
            resolved_at=timestamp,
        )
        if created:
            resolution_ids.append(str(resolution.get("id") or ""))

    proposal = propose_action(
        paths,
        "fact_upsert",
        action_payload={
            "fact": {
                "id": fact_id,
                "statement": statement,
                "entity_key": entity_key,
                "page_hint": page_hint,
                "section_hint": section_hint,
                "source_ids": effective_source_ids,
                "observed_at": timestamp,
                "confidence": 1.0,
                "status": "active",
                "supersedes_id": supersede_fact_ids[0] if supersede_fact_ids else None,
                "confirmed_by_user": True,
                "source_spans": [],
                "extraction_method": "manual",
                "truth_confidence": 1.0,
                "metadata": {
                    "source": "chief_of_staff_correction",
                    "supersede_fact_ids": supersede_fact_ids,
                },
                "created_at": timestamp,
                "last_seen_at": timestamp,
            }
        },
        action_features={
            "human_confirmed": True,
            "truth_mutation": bool(supersede_fact_ids),
            "reversible": True,
            "affected_fact_count": 1 + len(supersede_fact_ids),
        },
        target_fact_ids=[fact_id],
        target_page_paths=[page_hint],
        proposed_by="chief_of_staff_correction",
        confidence=1.0,
        risk_tier="medium" if supersede_fact_ids else "low",
        override_semantic_rejection=True,
    )

    def record_standalone_keep(conn: Any, reviewed_action: dict[str, Any]) -> None:
        record_correction_resolution(
            conn,
            reviewed_action,
            disposition="keep",
        )

    try:
        action = hooks.apply_action(
            paths,
            str(proposal["id"]),
            override_semantic_rejection=True,
            transaction_hook=record_standalone_keep if not supersede_fact_ids else None,
        )
        require_applied_answer_action(action, "manual correction")
        applied_action_ids.append(str(action["id"]))
        supersede_action = None
        if supersede_fact_ids:

            def record_correction_set(
                conn: Any, _reviewed_action: dict[str, Any]
            ) -> None:
                record_correction_resolution(conn, action, disposition="keep")
                for old_fact in old_facts:
                    record_correction_resolution(
                        conn,
                        synthetic_fact_action(old_fact),
                        disposition="reject",
                        corrected_away_fact_id=str(old_fact["id"]),
                    )

            supersede_action = hooks.apply_fact_status_action(
                paths,
                "fact_supersede",
                [
                    {
                        "fact_id": old_fact_id,
                        "status": "retracted",
                        "conflict_group_id": None,
                    }
                    for old_fact_id in supersede_fact_ids
                ],
                proposed_by="chief_of_staff_correction",
                risk_tier="medium",
                override_semantic_rejection=True,
                transaction_hook=record_correction_set,
            )
            require_applied_answer_action(
                supersede_action, "manual correction supersession"
            )
            applied_action_ids.append(str(supersede_action["id"]))
    except Exception:
        rollback_error: Exception | None = None
        for applied_action_id in reversed(applied_action_ids):
            try:
                safely_revert_action(paths, applied_action_id)
            except ReviewUndoError as exc:
                rollback_error = rollback_error or exc
        with connection(paths.sqlite_path) as conn:
            conn.execute(
                """
                UPDATE cos_actions SET status = 'failed'
                WHERE id = ? AND status IN ('proposed', 'needs_human')
                """,
                (proposal["id"],),
            )
        if rollback_error is not None:
            raise ValueError(
                "manual correction failed and could not be fully rolled back"
            ) from rollback_error
        raise
    curation, warnings = curate_after_committed_mutation(
        paths,
        [page_hint],
        hooks=hooks,
        overwrite_existing=overwrite_existing,
    )
    result = {
        "fact": hooks.get_fact(paths, fact_id),
        "action": action,
        "supersede_action": supersede_action,
        "review_resolution_ids": resolution_ids,
        "curation": curation,
        "review": (
            hooks.managed_fact_page_review(paths, page_hint) if not warnings else None
        ),
        "dashboard": hooks.wiki_fact_dashboard(paths),
    }
    if warnings:
        result.update(
            {
                "status": "committed_with_projection_warning",
                "warnings": warnings,
            }
        )
    return result


def _page_projection_restore_state(
    paths: BrainPaths,
    snapshot_id: str,
    *,
    hooks: WikiManualReviewHooks,
) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    with connection(paths.sqlite_path) as conn:
        row = conn.execute(
            "SELECT * FROM wiki_page_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
    if not row:
        raise ValueError(f"wiki page snapshot not found: {snapshot_id}")
    snapshot = hooks.row_to_page_snapshot(row)
    page_hint = str(snapshot["page_path"])
    target = hooks.safe_fact_wiki_path(paths, page_hint)
    current_exists = target.exists()
    current_markdown = (
        target.read_text(encoding="utf-8", errors="replace") if current_exists else None
    )
    if current_exists != bool(snapshot["after_exists"]) or current_markdown != (
        snapshot.get("after_markdown") if snapshot["after_exists"] else None
    ):
        return (
            snapshot,
            target,
            {
                "snapshot_id": snapshot_id,
                "page_hint": page_hint,
                "restorable": False,
                "reason": (
                    "managed page changed after the answer; "
                    "projection was not overwritten"
                ),
            },
        )
    return (
        snapshot,
        target,
        {
            "snapshot_id": snapshot_id,
            "page_hint": page_hint,
            "restorable": True,
        },
    )


def preflight_wiki_page_projection_restore(
    paths: BrainPaths,
    snapshot_id: str,
    *,
    hooks: WikiManualReviewHooks,
) -> dict[str, Any]:
    """Read whether a projection snapshot can still be restored without mutation."""

    _snapshot, _target, result = _page_projection_restore_state(
        paths, snapshot_id, hooks=hooks
    )
    return result


def restore_wiki_page_projection_from_snapshot(
    paths: BrainPaths,
    snapshot_id: str,
    *,
    hooks: WikiManualReviewHooks,
) -> dict[str, Any]:
    """Restore a derived page only when it still matches the answered projection."""

    snapshot, target, preflight = _page_projection_restore_state(
        paths, snapshot_id, hooks=hooks
    )
    if not preflight["restorable"]:
        return {
            **preflight,
            "restored": False,
        }
    if snapshot["before_exists"]:
        target.parent.mkdir(parents=True, exist_ok=True)
        before_markdown = str(snapshot.get("before_markdown") or "")
        target.write_text(before_markdown, encoding="utf-8")
    elif target.exists():
        target.unlink()
    lint_result = lint_wiki(paths)
    if snapshot["before_exists"]:
        hooks.sync_restored_page_projection_metadata(paths, target, before_markdown)
    else:
        with connection(paths.sqlite_path) as conn:
            conn.execute("DELETE FROM wiki_pages WHERE path = ?", (str(target),))
    return {
        "snapshot_id": snapshot_id,
        "page_hint": preflight["page_hint"],
        "restored": True,
        "lint": lint_result,
    }
