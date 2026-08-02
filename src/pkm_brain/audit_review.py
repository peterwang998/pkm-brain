from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .cos_actions import (
    apply_action,
    apply_action_in_connection,
    audit_action_reviewability,
    audited_active_fact_id,
    load_action,
    propose_action_in_connection,
    record_action_audit,
    retire_open_candidate_siblings,
    target_state_hash,
)
from .db import connection, dumps
from .fact_records import row_to_fact
from .paths import BrainPaths
from .review_resolution import (
    ReviewResolutionConflict,
    action_review_identity,
    active_resolution_for_action,
    manual_question_answer_resolution_plan,
    original_question_action,
    preflight_review_resolution_revoke,
    question_resolution_snapshot,
    reconcile_question_resolution_rows,
    record_alternative_question_fact_resolutions,
    record_review_resolution,
    routed_question_keep_action,
    revoke_review_resolution,
    decoded_fact_row,
)
from .review_undo import ReviewUndoError, capture_action_state, safely_revert_action
from .util import now_iso
from .wiki_facts import apply_fact_status_action


HISTORICAL_AUDIT_ORIGINS = {"legacy_historical", "weekly_historical"}
QUESTION_REJECTION_DECISIONS = {
    "current_state",
    "dismiss",
    "keep_existing",
    "manual_answer",
    "merge_evidence",
    "reject",
    "reject_candidate",
    "support",
    "supports",
    "supports_existing",
    "temporal_update",
    "updates",
}


class AuditReviewDecisionError(ValueError):
    pass


def safely_revert_audit_action(paths: BrainPaths, action_id: str) -> dict[str, Any]:
    try:
        return safely_revert_action(paths, action_id)
    except ReviewUndoError as exc:
        raise AuditReviewDecisionError(str(exc)) from exc


def latest_action_audit(action: dict[str, Any]) -> dict[str, Any]:
    raw_evidence = action.get("evidence_json") or {}
    evidence = raw_evidence if isinstance(raw_evidence, dict) else {}
    records = evidence.get("audits")
    valid = [record for record in records or [] if isinstance(record, dict)]
    record = valid[-1] if valid else {}
    metadata = (
        record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    )
    legacy = evidence.get("audit") if isinstance(evidence.get("audit"), dict) else {}
    rationale = first_nonempty(
        metadata.get("rationale"),
        metadata.get("reason"),
        legacy.get("rationale"),
        evidence.get("reason"),
        action.get("audit_status"),
    )
    historical_priority = metadata.get("historical_priority") or {}
    return {
        "status": record.get("status") or action.get("audit_status"),
        "rationale": rationale,
        "provider": metadata.get("provider"),
        "model": metadata.get("model"),
        "audited_at": record.get("at"),
        "origin": metadata.get("audit_origin") or "legacy_historical",
        "audit_run_id": metadata.get("audit_run_id"),
        "issue_key": metadata.get("issue_key"),
        "group_key": metadata.get("group_key"),
        "historical_priority": historical_priority,
        "direct_contradiction": bool(historical_priority.get("direct_contradiction")),
        "action_type": action.get("action_type"),
        "action_status": action.get("status"),
    }


def audit_is_historical(audit: dict[str, Any]) -> bool:
    return str(audit.get("origin") or "") in HISTORICAL_AUDIT_ORIGINS


def audit_risk_tier(
    action: dict[str, Any],
    audit: dict[str, Any],
    *,
    direct_contradiction: bool | None = None,
) -> Any:
    direct = (
        bool(audit.get("direct_contradiction"))
        if direct_contradiction is None
        else direct_contradiction
    )
    if audit_is_historical(audit) and not direct:
        return "medium"
    return action.get("risk_tier")


def record_question_review_resolution(
    paths: BrainPaths,
    question: dict[str, Any],
    decision: str,
    undo_handle: dict[str, Any] | None,
) -> None:
    created_ids: list[str] = []
    normalized = decision.replace("-", "_")
    with connection(paths.sqlite_path) as conn:
        snapshot = question_resolution_snapshot(conn, question)
        handled, fact_resolutions = record_alternative_question_fact_resolutions(
            conn,
            snapshot,
            decision=normalized,
            allow_any_question=normalized == "manual_selection",
        )
        if handled:
            created_ids.extend(
                str(resolution.get("id") or "") for resolution in fact_resolutions
            )
        else:
            action = original_question_action(conn, snapshot)
            answer = (
                snapshot.get("answer")
                if isinstance(snapshot.get("answer"), dict)
                else {}
            )
            if normalized == "manual_answer":
                planned = [
                    (item["action"], str(item["disposition"]))
                    for item in manual_question_answer_resolution_plan(
                        conn, snapshot, action
                    )
                ]
            elif routed_action := routed_question_keep_action(
                conn,
                snapshot,
                action,
                decision=normalized,
            ):
                planned = [(routed_action, "keep")]
            else:
                planned = (
                    [
                        (
                            action,
                            "reject"
                            if normalized in QUESTION_REJECTION_DECISIONS
                            else "keep",
                        )
                    ]
                    if action is not None
                    else []
                )
            if not planned:
                return
            planned_by_state: dict[tuple[str, str], tuple[dict[str, Any], str]] = {}
            for planned_action, disposition in planned:
                identity = action_review_identity(planned_action)
                planned_by_state[
                    (identity["family_key"], identity["state_fingerprint"])
                ] = (planned_action, disposition)
            timestamp = str(snapshot.get("answered_at") or "") or now_iso()
            reconcile_question_resolution_rows(
                conn,
                snapshot,
                expected=[
                    (
                        family_key,
                        state_fingerprint,
                        disposition,
                    )
                    for (family_key, state_fingerprint), (
                        _planned_action,
                        disposition,
                    ) in planned_by_state.items()
                ],
                revoked_at=timestamp,
            )
            for planned_action, disposition in planned_by_state.values():
                resolution, created = record_review_resolution(
                    conn,
                    planned_action,
                    disposition=disposition,
                    source_item_kind="question",
                    source_item_id=str(question["id"]),
                    decision_payload={**answer, "decision": normalized},
                    resolved_at=timestamp,
                )
                if created:
                    created_ids.append(str(resolution.get("id") or ""))
    for resolution_id in created_ids:
        append_review_resolution_to_undo(undo_handle, resolution_id)


def decide_direct_action_queue_item(
    paths: BrainPaths,
    action: dict[str, Any],
    decision: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    normalized = decision.replace("-", "_")
    previous = capture_action_state(paths, str(action["id"]))
    resolution_ids: list[str] = []

    def record_resolution(
        conn: Any, disposition: str, reviewed_action: dict[str, Any]
    ) -> None:
        resolution, created = record_review_resolution(
            conn,
            reviewed_action,
            disposition=disposition,
            source_item_kind="action",
            source_item_id=str(action["id"]),
            decision_payload={"decision": normalized},
        )
        if created:
            resolution_ids.append(str(resolution.get("id") or ""))

    if normalized in {"approve", "apply", "accept"}:
        hook_called = False

        def approve_hook(conn: Any, reviewed_action: dict[str, Any]) -> None:
            nonlocal hook_called
            record_resolution(conn, "keep", reviewed_action)
            hook_called = True

        result = apply_action(paths, str(action["id"]), transaction_hook=approve_hook)
        if result.get("status") not in {"applied", "auto_applied"} or not hook_called:
            raise AuditReviewDecisionError(
                "action is no longer eligible for application"
            )
        undo = {
            "kind": "action_apply",
            "action_id": action["id"],
            "action": previous,
        }
    elif normalized in {"reject", "dismiss"}:
        undo = {"kind": "action_status", "action": previous}
        timestamp = now_iso()
        with connection(paths.sqlite_path) as conn:
            current = load_action(conn, str(action["id"]))
            if current["status"] in {"applied", "auto_applied", "reverted"}:
                raise AuditReviewDecisionError(
                    f"linked action is already {current['status']}: {action['id']}"
                )
            evidence = dict(current.get("evidence_json") or {})
            evidence["human_review"] = {
                "decision": "reject",
                "reason": str(payload.get("reason") or "human rejected queue action"),
                "decided_at": timestamp,
            }
            conn.execute(
                "UPDATE cos_actions SET status = 'rejected', evidence_json = ? WHERE id = ?",
                (dumps(evidence), action["id"]),
            )
            retire_open_candidate_siblings(
                conn,
                current,
                reason="candidate rejected by human review",
            )
            record_resolution(conn, "reject", current)
            result = load_action(conn, str(action["id"]))
    else:
        raise AuditReviewDecisionError(f"unsupported action decision: {decision}")
    for resolution_id in resolution_ids:
        append_review_resolution_to_undo(undo, resolution_id)
    return {
        "status": "decided",
        "item_id": action["id"],
        "result": {"action": result},
        "undo_handle": undo,
    }


def record_fact_confirmation_resolution(
    paths: BrainPaths,
    action: dict[str, Any],
    fact_id: str,
    *,
    conn: Any | None = None,
) -> str | None:
    def write(active_conn: Any) -> str | None:
        resolution, created = record_review_resolution(
            active_conn,
            action,
            disposition="keep",
            source_item_kind="fact_confirmation",
            source_item_id=fact_id,
            decision_payload={"confirmed_by_user": True},
        )
        return str(resolution.get("id") or "") if created else None

    if conn is not None:
        return write(conn)
    with connection(paths.sqlite_path) as active_conn:
        return write(active_conn)


def decide_audit_queue_item(
    paths: BrainPaths,
    action: dict[str, Any],
    decision: str,
    payload: dict[str, Any],
    *,
    previous_action_state: dict[str, Any] | None,
    record_resolution: bool = True,
) -> dict[str, Any]:
    normalized = decision.replace("-", "_")
    related_ids = stable_unique_strings(
        action.get("related_audit_action_ids") or [action.get("id")]
    )
    if normalized in {"revert", "v"} and len(related_ids) > 1:
        return decide_related_audit_rejections(
            paths,
            action,
            normalized,
            payload,
            related_ids=related_ids,
        )
    resolution_id: str | None = None
    if normalized in {"revert", "v"}:
        with connection(paths.sqlite_path) as conn:
            reviewability = audit_action_reviewability(conn, action)
        if not reviewability["revertible"]:
            raise AuditReviewDecisionError(
                "audited action no longer has a safe direct revert; review the "
                "current fact or topology state"
            )
        if reviewability.get("revert_mode") == "reject_current_fact":
            fact_id = str(reviewability.get("fact_id") or "")
            undo = {
                "kind": "audit_fact_remediation",
                "action": previous_action_state,
                "correction_action_id": "",
            }
            try:
                correction = apply_fact_status_action(
                    paths,
                    "fact_supersede",
                    [
                        {
                            "fact_id": fact_id,
                            "status": "rejected",
                            "conflict_group_id": None,
                        }
                    ],
                    proposed_by="ui_audit_reject_current_fact",
                    risk_tier="medium",
                )
                if correction.get("status") not in {"applied", "auto_applied"}:
                    raise AuditReviewDecisionError(
                        "audited fact remediation was not applied"
                    )
                correction_id = str(correction.get("id") or "").strip()
                if not correction_id:
                    raise AuditReviewDecisionError(
                        "audited fact remediation did not produce a reversible action"
                    )
                undo["correction_action_id"] = correction_id
                result = record_action_audit(
                    paths,
                    action["id"],
                    "remediated",
                    metadata={
                        "ui_rejected_current_fact": True,
                        "fact_id": fact_id,
                        "correction_action_id": correction["id"],
                    },
                )
                if result.get("audit_status") != "remediated":
                    raise AuditReviewDecisionError(
                        "audited fact remediation was not recorded"
                    )
                result_payload = {
                    "action": result,
                    "correction_action": correction,
                }
                if record_resolution:
                    resolution_id = persist_review_resolution(
                        paths,
                        action,
                        disposition="reject",
                        source_item_kind="audit",
                        source_item_id=str(action["id"]),
                        decision_payload={
                            "decision": normalized,
                            "correction_action_id": correction["id"],
                        },
                    )
            except Exception:
                rollback_audit_handle(paths, undo)
                raise
        else:
            undo = {"kind": "action_revert", "action": previous_action_state}
            try:
                result = safely_revert_audit_action(paths, action["id"])
                if result.get("status") != "reverted":
                    raise AuditReviewDecisionError(
                        "audited action could not be safely reverted"
                    )
                undo["undo_precondition_hash"] = action_target_hash(paths, result)
                result_payload = {"action": result}
                if record_resolution:
                    resolution_id = persist_review_resolution(
                        paths,
                        action,
                        disposition="reject",
                        source_item_kind="audit",
                        source_item_id=str(action["id"]),
                        decision_payload={"decision": normalized},
                    )
            except Exception:
                rollback_audit_handle(paths, undo)
                raise
    elif normalized in {"ok", "mark_ok", "mark_good"}:
        with connection(paths.sqlite_path) as conn:
            try:
                current_action = load_action(conn, str(action.get("id") or ""))
            except ValueError as exc:
                raise AuditReviewDecisionError(
                    "audited action no longer exists; refresh the review queue"
                ) from exc
            reviewability = audit_action_reviewability(conn, current_action)
        if not reviewability.get("reviewable"):
            reason = str(reviewability.get("reason") or "unknown")
            raise AuditReviewDecisionError(
                "audited action is no longer reviewable; refresh the review queue "
                f"(reason: {reason})"
            )
        action = current_action
        previous_action_state = action_undo_state(current_action)
        fact_id = str(reviewability.get("fact_id") or "").strip()
        undo = {
            "kind": "audit_mark_ok",
            "action": previous_action_state,
            "confirmation_action_id": "",
        }
        result, resolution_id, confirmation = commit_mark_good_decision(
            paths,
            action,
            decision=normalized,
            note=str(payload.get("note") or ""),
            confirmed_fact_id=fact_id,
            record_resolution=record_resolution,
        )
        undo["confirmation_action_id"] = str(
            ((confirmation or {}).get("action") or {}).get("id") or ""
        )
        result_payload = {"action": result}
        if confirmation is not None:
            result_payload["confirmation"] = confirmation
    else:
        raise AuditReviewDecisionError(f"unsupported audit decision: {decision}")
    append_review_resolution_to_undo(undo, resolution_id)
    return {
        "status": "decided",
        "item_id": action["id"],
        "result": result_payload,
        "undo_handle": undo,
    }


def decide_related_audit_rejections(
    paths: BrainPaths,
    representative: dict[str, Any],
    decision: str,
    payload: dict[str, Any],
    *,
    related_ids: list[str],
) -> dict[str, Any]:
    fact_batch = exact_fact_batch_actions(paths, related_ids)
    if fact_batch:
        return decide_related_fact_audit_rejections(
            paths,
            representative,
            decision,
            actions=fact_batch,
        )
    decisions: list[dict[str, Any]] = []
    try:
        for action_id in related_ids:
            with connection(paths.sqlite_path) as conn:
                try:
                    action = load_action(conn, action_id)
                except ValueError:
                    continue
                reviewability = audit_action_reviewability(conn, action)
            if not (
                reviewability.get("reviewable") and reviewability.get("revertible")
            ):
                continue
            decisions.append(
                decide_audit_queue_item(
                    paths,
                    action,
                    decision,
                    payload,
                    previous_action_state=action_undo_state(action),
                    record_resolution=False,
                )
            )
        if not decisions:
            raise AuditReviewDecisionError(
                "no exact sibling audit finding still has a safe remediation"
            )
        resolution_id = persist_review_resolution(
            paths,
            representative,
            disposition="reject",
            source_item_kind="audit",
            source_item_id=str(representative["id"]),
            decision_payload={
                "decision": decision,
                "related_action_ids": [item["item_id"] for item in decisions],
            },
        )
    except Exception:
        undo_audit_batch(
            paths,
            [item["undo_handle"] for item in decisions],
            restore_action=lambda state: restore_audit_action_state(paths, state),
        )
        raise
    primary = next(
        (
            item
            for item in decisions
            if item["item_id"] == str(representative.get("id") or "")
        ),
        decisions[0],
    )
    result = dict(primary["result"])
    result["related_results"] = [
        {"item_id": item["item_id"], **item["result"]} for item in decisions
    ]
    undo = {
        "kind": "audit_batch_remediation",
        "handles": [item["undo_handle"] for item in decisions],
    }
    append_review_resolution_to_undo(undo, resolution_id)
    return {
        "status": "decided",
        "item_id": representative["id"],
        "result": result,
        "undo_handle": undo,
    }


def exact_fact_batch_actions(
    paths: BrainPaths, related_ids: list[str]
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    with connection(paths.sqlite_path) as conn:
        for action_id in related_ids:
            try:
                action = load_action(conn, action_id)
            except ValueError:
                continue
            reviewability = audit_action_reviewability(conn, action)
            if not reviewability.get("reviewable"):
                continue
            if action.get("action_type") != "fact_upsert" or not reviewability.get(
                "fact_id"
            ):
                return []
            action["audit_fact_id"] = str(reviewability["fact_id"])
            actions.append(action)
    return actions


def decide_related_fact_audit_rejections(
    paths: BrainPaths,
    representative: dict[str, Any],
    decision: str,
    *,
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    action_states = [action_undo_state(action) for action in actions]
    fact_ids = stable_unique_strings(
        [action.get("audit_fact_id") for action in actions]
    )
    correction: dict[str, Any] | None = None
    remediated: list[dict[str, Any]] = []
    resolution_id: str | None = None
    try:
        correction = apply_fact_status_action(
            paths,
            "fact_supersede",
            [
                {
                    "fact_id": fact_id,
                    "status": "rejected",
                    "conflict_group_id": None,
                }
                for fact_id in fact_ids
            ],
            proposed_by="ui_audit_reject_exact_fact_batch",
            risk_tier="medium",
        )
        correction_id = str(correction.get("id") or "")
        if not correction_id or correction.get("status") != "applied":
            raise AuditReviewDecisionError("exact audit fact state is no longer active")
        for action in actions:
            result = record_action_audit(
                paths,
                action["id"],
                "remediated",
                metadata={
                    "ui_rejected_exact_fact_batch": True,
                    "fact_id": action["audit_fact_id"],
                    "correction_action_id": correction_id,
                    "related_action_ids": [item["id"] for item in actions],
                },
            )
            if result.get("audit_status") != "remediated":
                raise AuditReviewDecisionError(
                    "exact audit finding remediation was not recorded"
                )
            remediated.append(result)
        resolution_id = persist_review_resolution(
            paths,
            representative,
            disposition="reject",
            source_item_kind="audit",
            source_item_id=str(representative["id"]),
            decision_payload={
                "decision": decision,
                "correction_action_id": correction_id,
                "related_action_ids": [item["id"] for item in actions],
            },
        )
    except Exception:
        rollback_fact_audit_batch(
            paths,
            correction_action_id=str((correction or {}).get("id") or ""),
            action_states=action_states,
        )
        raise

    by_id = {str(action["id"]): action for action in remediated}
    primary = by_id.get(str(representative.get("id") or ""), remediated[0])
    undo = {
        "kind": "audit_batch_remediation",
        "handles": [
            {
                "kind": "audit_fact_batch_remediation",
                "correction_action_id": correction["id"],
                "actions": action_states,
            }
        ],
    }
    append_review_resolution_to_undo(undo, resolution_id)
    return {
        "status": "decided",
        "item_id": representative["id"],
        "result": {
            "action": primary,
            "correction_action": correction,
            "related_results": [
                {"item_id": action["id"], "action": action} for action in remediated
            ],
        },
        "undo_handle": undo,
    }


def action_undo_state(action: dict[str, Any]) -> dict[str, Any]:
    return {
        key: action.get(key)
        for key in (
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
    }


def stable_unique_strings(values: Any) -> list[str]:
    return list(
        dict.fromkeys(
            str(value).strip() for value in values or [] if str(value or "").strip()
        )
    )


def commit_mark_good_decision(
    paths: BrainPaths,
    expected_action: dict[str, Any],
    *,
    decision: str,
    note: str,
    confirmed_fact_id: str,
    record_resolution: bool,
) -> tuple[dict[str, Any], str | None, dict[str, Any] | None]:
    """Revalidate and commit an audit Keep decision under one write lock."""

    with connection(paths.sqlite_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = load_action(conn, str(expected_action.get("id") or ""))
        except ValueError as exc:
            raise AuditReviewDecisionError(
                "audited action no longer exists; refresh the review queue"
            ) from exc
        if audit_decision_precondition(current) != audit_decision_precondition(
            expected_action
        ):
            raise AuditReviewDecisionError(
                "audited action changed while it was being reviewed; refresh the "
                "review queue"
            )

        reviewability = audit_action_reviewability(conn, current)
        if not reviewability.get("reviewable"):
            reason = str(reviewability.get("reason") or "unknown")
            raise AuditReviewDecisionError(
                "audited action is no longer reviewable; refresh the review "
                f"queue (reason: {reason})"
            )

        confirmation: dict[str, Any] | None = None
        confirmation_action_id = ""
        if current.get("action_type") == "fact_upsert" and confirmed_fact_id:
            confirmation = confirm_fact_in_connection(paths, conn, confirmed_fact_id)
            confirmation_action_id = str(
                ((confirmation or {}).get("action") or {}).get("id") or ""
            )
            if not confirmation_action_id:
                raise AuditReviewDecisionError(
                    "fact confirmation did not produce a reversible action"
                )
            require_current_fact_confirmation(
                conn,
                current,
                fact_id=confirmed_fact_id,
                confirmation_action_id=confirmation_action_id,
            )

        evidence = dict(current.get("evidence_json") or {})
        audits = list(evidence.get("audits") or [])
        audits.append(
            {
                "status": "sampled_ok",
                "metadata": {"ui_marked_ok": True, "note": note},
                "at": now_iso(),
            }
        )
        evidence["audits"] = audits
        conn.execute(
            "UPDATE cos_actions SET audit_status = 'sampled_ok', evidence_json = ? "
            "WHERE id = ?",
            (dumps(evidence), current["id"]),
        )
        result = load_action(conn, str(current["id"]))
        resolution_id: str | None = None
        if record_resolution:
            resolution, created = record_review_resolution(
                conn,
                current,
                disposition="keep",
                source_item_kind="audit",
                source_item_id=str(current["id"]),
                decision_payload={
                    "decision": decision,
                    "note": note,
                    "confirmed_fact_id": confirmed_fact_id or None,
                },
            )
            if created:
                resolution_id = str(resolution.get("id") or "") or None
    return result, resolution_id, confirmation


def confirm_fact_in_connection(
    paths: BrainPaths, conn: Any, fact_id: str
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
    if row is None:
        raise AuditReviewDecisionError(f"fact not found: {fact_id}")
    fact = decoded_fact_row(row)
    confirmed = {**fact, "confirmed_by_user": True}
    proposal = propose_action_in_connection(
        conn,
        "fact_upsert",
        action_payload={"fact": confirmed},
        action_features={
            "human_confirmed": True,
            "truth_mutation": True,
            "reversible": True,
            "affected_fact_count": 1,
        },
        target_fact_ids=[fact_id],
        target_page_paths=[str(fact.get("page_hint"))] if fact.get("page_hint") else [],
        proposed_by="ui_fact_confirm",
        confidence=1.0,
        risk_tier="low",
    )
    action = apply_action_in_connection(
        paths,
        conn,
        str(proposal["id"]),
        override_semantic_rejection=True,
    )
    if action.get("status") not in {"applied", "auto_applied"}:
        raise AuditReviewDecisionError("fact confirmation action was not applied")
    persisted = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
    return {
        "fact": row_to_fact(persisted) if persisted is not None else None,
        "action": action,
    }


def audit_decision_precondition(action: dict[str, Any]) -> dict[str, Any]:
    return {
        **action_undo_state(action),
        "action_type": action.get("action_type"),
        "action_features": action.get("action_features"),
    }


def require_current_fact_confirmation(
    conn: Any,
    audited_action: dict[str, Any],
    *,
    fact_id: str,
    confirmation_action_id: str,
) -> None:
    if not fact_id or audited_action.get("action_type") != "fact_upsert":
        raise AuditReviewDecisionError(
            "audited fact changed while it was being confirmed; refresh the review "
            "queue"
        )
    try:
        confirmation = load_action(conn, confirmation_action_id)
    except ValueError as exc:
        raise AuditReviewDecisionError(
            "fact confirmation no longer exists; refresh the review queue"
        ) from exc
    if confirmation.get("status") not in {"applied", "auto_applied"}:
        raise AuditReviewDecisionError(
            "fact confirmation is no longer applied; refresh the review queue"
        )
    expected_hash = str(confirmation.get("applied_state_hash") or "")
    current_hash = target_state_hash(
        conn,
        target_fact_ids=confirmation.get("target_fact_ids") or [],
        target_contract_ids=confirmation.get("target_contract_ids") or [],
        target_page_paths=confirmation.get("target_page_paths") or [],
    )
    if not expected_hash or current_hash != expected_hash:
        raise AuditReviewDecisionError(
            "confirmed fact changed while the audit decision was being committed; "
            "refresh the review queue"
        )
    if audited_active_fact_id(conn, audited_action) != fact_id:
        raise AuditReviewDecisionError(
            "audited fact no longer matches the reviewed statement; refresh the "
            "review queue"
        )
    if active_resolution_for_action(conn, audited_action) is not None:
        raise AuditReviewDecisionError(
            "audited action was resolved elsewhere; refresh the review queue"
        )


def persist_review_resolution(
    paths: BrainPaths,
    action: dict[str, Any],
    *,
    disposition: str,
    source_item_kind: str,
    source_item_id: str,
    decision_payload: dict[str, Any],
) -> str | None:
    with connection(paths.sqlite_path) as conn:
        resolution, created = record_review_resolution(
            conn,
            action,
            disposition=disposition,
            source_item_kind=source_item_kind,
            source_item_id=source_item_id,
            decision_payload=decision_payload,
        )
    return str(resolution.get("id") or "") if created else None


def append_review_resolution_to_undo(
    undo_handle: dict[str, Any] | None, resolution_id: str | None
) -> None:
    if undo_handle is not None and resolution_id:
        undo_handle.setdefault("review_resolution_ids", []).append(resolution_id)


def revoke_queue_review_resolution(paths: BrainPaths, resolution_id: str) -> None:
    if not resolution_id:
        return
    with connection(paths.sqlite_path) as conn:
        revoke_review_resolution(conn, resolution_id)


def preflight_queue_review_resolutions(
    paths: BrainPaths, resolution_ids: list[str]
) -> None:
    """Validate every ledger undo before the UI starts changing other state."""

    try:
        with connection(paths.sqlite_path) as conn:
            for resolution_id in dict.fromkeys(resolution_ids):
                if resolution_id:
                    preflight_review_resolution_revoke(conn, resolution_id)
    except ReviewResolutionConflict as exc:
        raise AuditReviewDecisionError(str(exc)) from exc


def undo_audit_batch(
    paths: BrainPaths,
    handles: list[dict[str, Any]],
    *,
    restore_action: Callable[[dict[str, Any] | None], None],
) -> None:
    for handle in reversed(handles):
        kind = str(handle.get("kind") or "")
        if kind == "action_revert":
            state = handle.get("action") or {}
            action_id = str(state.get("id") or "")
            require_action_undo_precondition(paths, handle, state)
            if action_id:
                applied = apply_action(
                    paths,
                    action_id,
                    override_semantic_rejection=True,
                )
                if applied.get("status") not in {"applied", "auto_applied"}:
                    raise AuditReviewDecisionError(
                        "audited action could not be safely restored"
                    )
            restore_action(state)
        elif kind == "audit_fact_remediation":
            correction_id = str(handle.get("correction_action_id") or "")
            if correction_id:
                correction = safely_revert_audit_action(paths, correction_id)
                if correction.get("status") != "reverted":
                    raise AuditReviewDecisionError(
                        "audit remediation changed after review; undo was not applied"
                    )
            restore_action(handle.get("action") or {})
        elif kind == "audit_fact_batch_remediation":
            correction_id = str(handle.get("correction_action_id") or "")
            correction = safely_revert_audit_action(paths, correction_id)
            if correction.get("status") != "reverted":
                raise AuditReviewDecisionError(
                    "audit remediation changed after review; undo was not applied"
                )
            for state in handle.get("actions") or []:
                restore_action(state)
        elif kind == "audit_mark_ok":
            confirmation_id = str(handle.get("confirmation_action_id") or "")
            if confirmation_id:
                confirmation = safely_revert_audit_action(paths, confirmation_id)
                if confirmation.get("status") != "reverted":
                    raise AuditReviewDecisionError(
                        "fact confirmation changed after review; undo was not applied"
                    )
            restore_action(handle.get("action") or {})
        else:
            raise AuditReviewDecisionError(
                f"unsupported audit batch undo handle: {kind}"
            )


def rollback_fact_audit_batch(
    paths: BrainPaths,
    *,
    correction_action_id: str,
    action_states: list[dict[str, Any]],
) -> None:
    if correction_action_id:
        correction = safely_revert_audit_action(paths, correction_action_id)
        if correction.get("status") != "reverted":
            raise AuditReviewDecisionError(
                "failed to roll back exact audit fact remediation"
            )
    for state in action_states:
        restore_audit_action_state(paths, state)


def rollback_audit_handle(paths: BrainPaths, handle: dict[str, Any]) -> None:
    kind = str(handle.get("kind") or "")
    if kind == "action_revert":
        state = handle.get("action") or {}
        action_id = str(state.get("id") or "")
        with connection(paths.sqlite_path) as conn:
            current = load_action(conn, action_id) if action_id else {}
        if current.get("status") == "reverted":
            apply_action(
                paths,
                action_id,
                override_semantic_rejection=True,
            )
        restore_audit_action_state(paths, state)
        return
    if kind == "audit_fact_remediation":
        correction_id = str(handle.get("correction_action_id") or "")
        if correction_id:
            correction = safely_revert_audit_action(paths, correction_id)
            if correction.get("status") != "reverted":
                raise AuditReviewDecisionError(
                    "failed to roll back audit fact remediation"
                )
        restore_audit_action_state(paths, handle.get("action") or {})
        return
    if kind == "audit_mark_ok":
        confirmation_id = str(handle.get("confirmation_action_id") or "")
        if confirmation_id:
            confirmation = safely_revert_audit_action(paths, confirmation_id)
            if confirmation.get("status") != "reverted":
                raise AuditReviewDecisionError(
                    "failed to roll back audit fact confirmation"
                )
        restore_audit_action_state(paths, handle.get("action") or {})
        return
    raise AuditReviewDecisionError(f"unsupported audit rollback handle: {kind}")


def restore_audit_action_state(paths: BrainPaths, state: dict[str, Any] | None) -> None:
    if not state:
        return
    action_id = str(state.get("id") or "").strip()
    if not action_id:
        return
    with connection(paths.sqlite_path) as conn:
        conn.execute(
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
                action_id,
            ),
        )


def action_target_hash(paths: BrainPaths, action: dict[str, Any]) -> str:
    with connection(paths.sqlite_path) as conn:
        return target_state_hash(
            conn,
            target_fact_ids=action.get("target_fact_ids") or [],
            target_contract_ids=action.get("target_contract_ids") or [],
            target_page_paths=action.get("target_page_paths") or [],
        )


def require_action_undo_precondition(
    paths: BrainPaths,
    handle: dict[str, Any],
    state: dict[str, Any],
) -> None:
    expected = str(handle.get("undo_precondition_hash") or "")
    if not expected or action_target_hash(paths, state) != expected:
        raise AuditReviewDecisionError(
            "target state changed after review; undo was not applied"
        )


def first_nonempty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None
