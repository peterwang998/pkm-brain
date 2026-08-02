from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

from .candidate_retirement import database_row_fingerprint
from .fact_entity_attribution import (
    fact_entity_id_is_explicit,
    fact_entity_mentions,
)
from .util import new_id, now_iso


RESOLUTION_TABLE = "review_resolutions"
EXACT_SIBLING_CLOSURES_KEY = "exact_open_sibling_closures"
EXACT_SIBLING_CLOSURES_VERSION = 2
SQLITE_ID_BATCH_SIZE = 400
ACTIVE_FACT_STATUSES = {"active", "contested", "conflicted", "needs_confirmation"}
FACT_TEMPORAL_FIELDS = (
    "temporal_kind",
    "valid_from",
    "valid_to",
    "valid_time_precision",
    "temporal_expression",
    "effective_at",
)
VOLATILE_PAYLOAD_KEYS = {
    "applied_at",
    "audited_at",
    "created_at",
    "generated_at",
    "last_seen_at",
    "observed_at",
    "reviewed_at",
    "updated_at",
}
QUESTION_REPLACEMENT_DECISIONS = {
    "current_state",
    "manual_answer",
    "merge_evidence",
    "new_page",
    "route",
    "support",
    "supports",
    "supports_existing",
    "temporal_update",
    "updates",
}
QUESTION_ROUTE_DECISIONS = {"new_page", "route"}
QUESTION_REJECT_DECISIONS = {
    *QUESTION_REPLACEMENT_DECISIONS.difference(QUESTION_ROUTE_DECISIONS),
    "dismiss",
    "keep_existing",
    "reject",
    "reject_candidate",
}
QUESTION_KEEP_DECISIONS = {
    "accept",
    "apply",
    "apply_action",
    "approve",
    "both_true",
    "candidate",
    "candidate_wins",
    "contested",
}
QUESTION_RESULT_ACTION_PROPOSERS = {
    "question_answer",
    "ui_queue_contested",
    "ui_queue_route",
    "ui_queue_supports_existing",
    "ui_queue_temporal_update",
}


class ReviewResolutionConflict(ValueError):
    """A resolution could not be revoked without overwriting newer review state."""


def action_review_identity(action: Mapping[str, Any]) -> dict[str, str]:
    action_type = normalize_text(action.get("action_type")) or "unknown"
    payload = action_payload(action)
    fact = payload.get("fact") if isinstance(payload.get("fact"), dict) else None
    if action_type == "fact_upsert" and fact is not None:
        return fact_review_identity(fact)

    features = mapping(action.get("action_features"))
    candidate_key = normalize_text(
        features.get("candidate_key") or payload.get("candidate_key")
    )
    targets = {
        "facts": stable_strings(action.get("target_fact_ids")),
        "pages": stable_strings(action.get("target_page_paths")),
        "contracts": stable_strings(action.get("target_contract_ids")),
    }
    family_state = {
        "action_type": action_type,
        "candidate_key": candidate_key,
        "targets": targets,
    }
    if not candidate_key:
        family_state["payload"] = canonical_payload(payload)
    family_key = f"action:{digest(family_state)}"
    state = {
        **family_state,
        "payload": canonical_payload(payload),
    }
    return {
        "family_key": family_key,
        "portable_key": family_key,
        "state_fingerprint": digest(state),
        "group_key": family_key,
    }


def fact_review_identity(fact: Mapping[str, Any]) -> dict[str, str]:
    statement = normalize_text(fact.get("statement"))
    source_ids = stable_strings(decoded_json(fact.get("source_ids"), []))
    source_spans = canonical_source_spans(decoded_json(fact.get("source_spans"), []))
    quote = normalize_text(fact.get("evidence_quote") or fact.get("quote"))
    metadata = mapping(fact.get("metadata"))
    portable_entity_state = canonical_fact_entity_state(fact)
    strict_entity_state = dict(portable_entity_state)
    entity_key = normalize_text(fact.get("entity_key"))
    if entity_key:
        strict_entity_state["entity_key"] = entity_key
    entity_id = normalize_text(fact.get("entity_id"))
    if entity_id and fact_entity_id_is_explicit(dict(fact)):
        strict_entity_state["entity_id"] = entity_id
    temporal = {
        field: canonical_payload(fact.get(field))
        for field in FACT_TEMPORAL_FIELDS
        if temporal_value_present(fact.get(field))
    }
    raw_event_time = mapping(fact.get("event_time"))
    event_time = {
        "kind": fact.get("event_time_kind") or raw_event_time.get("kind"),
        "start_at": fact.get("event_start_at") or raw_event_time.get("start_at"),
        "end_at": fact.get("event_end_at") or raw_event_time.get("end_at"),
        "precision": fact.get("event_time_precision")
        or raw_event_time.get("precision"),
        "expression": fact.get("event_time_expression")
        or raw_event_time.get("expression"),
    }
    event_time = {
        key: canonical_payload(value)
        for key, value in event_time.items()
        if temporal_value_present(value)
    }
    if event_time:
        temporal["event_time"] = event_time
    event_refs = canonical_repeatable_rows(
        metadata.get("temporal_references")
        or metadata.get("event_references")
        or fact.get("temporal_references")
        or []
    )
    family_state = {
        "kind": "fact",
        "statement": statement,
        # Route keys and entity IDs can be installation-local.  Family and
        # portable identity use only source attribution that survives reroutes,
        # export, and import.
        "entity": portable_entity_state,
    }
    evidence_state = {
        "source_ids": source_ids,
        "source_spans": source_spans,
        "evidence_quote": quote,
    }
    state = {
        **family_state,
        **evidence_state,
        # Exact local review state still distinguishes route keys and authored
        # entity IDs. Resolver-derived IDs are installation-local caches whose
        # provenance is retained in the attribution receipt.
        "entity": strict_entity_state,
        "temporal": temporal,
        "event_references": event_refs,
    }
    # A source group measures evidence diversity.  Attribution belongs in the
    # semantic family, but must not split two candidates from the same source.
    source_group = evidence_state if any(evidence_state.values()) else family_state
    return {
        "family_key": f"fact:{digest(family_state)}",
        "portable_key": f"fact:{digest({**family_state, **evidence_state})}",
        "state_fingerprint": digest(state),
        "group_key": f"source:{digest(source_group)}",
    }


def action_payload(action: Mapping[str, Any]) -> dict[str, Any]:
    evidence = mapping(action.get("evidence_json"))
    return mapping(evidence.get("payload"))


def resolution_table_exists(conn: Any) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (RESOLUTION_TABLE,),
        ).fetchone()
    )


def active_resolution_for_action(
    conn: Any, action: Mapping[str, Any]
) -> dict[str, Any] | None:
    if not resolution_table_exists(conn):
        return None
    identity = action_review_identity(action)
    row = conn.execute(
        """
        SELECT *
        FROM review_resolutions
        WHERE family_key = ? AND state_fingerprint = ? AND revoked_at IS NULL
        ORDER BY resolved_at DESC, id DESC
        LIMIT 1
        """,
        (identity["family_key"], identity["state_fingerprint"]),
    ).fetchone()
    return plain_row(row)


def record_review_resolution(
    conn: Any,
    action: Mapping[str, Any],
    *,
    disposition: str,
    source_item_kind: str,
    source_item_id: str,
    decided_by: str = "human",
    decision_payload: Mapping[str, Any] | None = None,
    resolved_at: str | None = None,
    resolution_id: str | None = None,
    cleanup_existing: bool = False,
) -> tuple[dict[str, Any], bool]:
    identity = action_review_identity(action)
    timestamp = resolved_at or now_iso()
    payload = dict(decision_payload or {})
    # This key contains system-authored rollback state. Never accept a caller's
    # version of it through a question answer or API decision payload.
    payload.pop(EXACT_SIBLING_CLOSURES_KEY, None)
    # This pointer is also system-owned.  A caller must not be able to make an
    # unrelated or attacker-selected receipt the predecessor restored by Undo.
    payload.pop("superseded_resolution_id", None)
    existing = active_resolution_for_action(conn, action)
    if existing is not None:
        if existing.get("disposition") == disposition:
            if cleanup_existing:
                # Migration/backfill may encounter a valid ledger row created by
                # an older release alongside review cards that release did not
                # retire.  Repair those cards idempotently, but do not graft the
                # repair onto the historical receipt: no live undo handle owns
                # this maintenance operation, and changing that receipt would
                # silently change the scope of an older undo.
                close_exact_open_siblings(
                    conn,
                    action,
                    resolution_id=str(existing["id"]),
                    disposition=disposition,
                    resolved_at=str(existing.get("resolved_at") or timestamp),
                    preserve_source_action=False,
                    outcome="historical_exact_candidate_closed",
                )
            return existing, False
        if resolved_at and str(existing.get("resolved_at") or "") > timestamp:
            return existing, False
        conn.execute(
            "UPDATE review_resolutions SET revoked_at = ? WHERE id = ?",
            (timestamp, existing["id"]),
        )
        payload["superseded_resolution_id"] = existing["id"]
    row_id = resolution_id or new_id("resolution")
    conn.execute(
        """
        INSERT INTO review_resolutions(
          id, family_key, portable_key, state_fingerprint, group_key,
          disposition, source_item_kind, source_item_id, decision_payload,
          decided_by, resolved_at, revoked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """,
        (
            row_id,
            identity["family_key"],
            identity["portable_key"],
            identity["state_fingerprint"],
            identity["group_key"],
            disposition,
            source_item_kind,
            source_item_id,
            json.dumps(payload, sort_keys=True),
            decided_by,
            timestamp,
        ),
    )
    closures = close_exact_open_siblings(
        conn,
        action,
        resolution_id=row_id,
        disposition=disposition,
        resolved_at=timestamp,
    )
    if closures["actions"] or closures["questions"]:
        payload[EXACT_SIBLING_CLOSURES_KEY] = closures
        conn.execute(
            "UPDATE review_resolutions SET decision_payload = ? WHERE id = ?",
            (json.dumps(payload, sort_keys=True), row_id),
        )
    row = conn.execute(
        "SELECT * FROM review_resolutions WHERE id = ?", (row_id,)
    ).fetchone()
    return plain_row(row) or {}, True


def close_exact_open_siblings(
    conn: Any,
    action: Mapping[str, Any],
    *,
    resolution_id: str,
    disposition: str,
    resolved_at: str,
    preserve_source_action: bool = True,
    outcome: str = "exact_open_candidate_closed",
    source_outcome: str | None = None,
    semantic_resolution_fields: Mapping[str, Any] | None = None,
    question_answered_at: str | None = None,
    include_non_open_source: bool = False,
) -> dict[str, Any]:
    closures: dict[str, Any] = {
        "version": EXACT_SIBLING_CLOSURES_VERSION,
        "actions": [],
        "questions": [],
    }
    if not table_exists(conn, "cos_actions"):
        return closures
    identity = action_review_identity(action)
    source_action_id = normalize_text(action.get("id"))
    closed_ids: list[str] = []
    rows = list(
        conn.execute(
            """
        SELECT * FROM cos_actions
        WHERE status IN ('proposed', 'needs_human')
        ORDER BY created_at, id
        """
        )
    )
    open_ids = {str(row["id"]) for row in rows}
    if include_non_open_source and source_action_id not in open_ids:
        source_row = conn.execute(
            """
            SELECT * FROM cos_actions
            WHERE id = ?
              AND status NOT IN ('applied', 'auto_applied', 'rejected')
            """,
            (source_action_id,),
        ).fetchone()
        if source_row is not None:
            rows.append(source_row)
    for row in rows:
        candidate = decoded_action_row(row)
        candidate_identity = action_review_identity(candidate)
        if (
            candidate_identity["family_key"] != identity["family_key"]
            or candidate_identity["state_fingerprint"] != identity["state_fingerprint"]
        ):
            continue
        candidate_id = str(candidate.get("id") or "")
        if (
            preserve_source_action
            and disposition == "keep"
            and candidate_id == source_action_id
        ):
            continue
        evidence = mapping(candidate.get("evidence_json"))
        before = dict(row)
        evidence = dict(evidence)
        semantic_resolution = {
            "resolution_id": resolution_id,
            "disposition": disposition,
            "resolved_at": resolved_at,
        }
        semantic_resolution.update(dict(semantic_resolution_fields or {}))
        semantic_resolution["outcome"] = (
            source_outcome
            if candidate_id == source_action_id and source_outcome
            else outcome
        )
        evidence["semantic_resolution"] = semantic_resolution
        encoded_evidence = json.dumps(evidence, sort_keys=True)
        after = {**before, "status": "rejected", "evidence_json": encoded_evidence}
        closures["actions"].append(
            {
                "id": candidate_id,
                "before": {
                    "status": before.get("status"),
                    "evidence_json": before.get("evidence_json"),
                },
                "after_fingerprint": database_row_fingerprint(after),
            }
        )
        updated = conn.execute(
            """
            UPDATE cos_actions
            SET status = 'rejected', evidence_json = ?
            WHERE id = ? AND status = ?
            """,
            (encoded_evidence, candidate_id, before.get("status")),
        )
        if updated.rowcount != 1:
            raise ReviewResolutionConflict(
                "a related review action changed; candidate was not suppressed"
            )
        closed_ids.append(candidate_id)
    if closed_ids and table_exists(conn, "open_questions"):
        answered_at = question_answered_at or resolved_at
        answer = json.dumps(
            {
                "decision": "exact_semantic_state_already_resolved",
                "disposition": disposition,
                "resolution_id": resolution_id,
            },
            sort_keys=True,
        )
        for start in range(0, len(closed_ids), SQLITE_ID_BATCH_SIZE):
            batch = closed_ids[start : start + SQLITE_ID_BATCH_SIZE]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"""
                SELECT *
                FROM open_questions
                WHERE action_id IN ({placeholders})
                  AND status IN ('open', 'needs_human')
                ORDER BY created_at, id
                """,
                batch,
            ).fetchall()
            for row in rows:
                before = dict(row)
                after = {
                    **before,
                    "status": "auto_resolved",
                    "answer": answer,
                    "answered_at": answered_at,
                    "decided_by": "semantic_review_resolution",
                }
                closures["questions"].append(
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
                SET status = 'auto_resolved', answer = ?, answered_at = ?,
                    decided_by = 'semantic_review_resolution'
                WHERE action_id IN ({placeholders})
                  AND status IN ('open', 'needs_human')
                """,
                [answer, answered_at, *batch],
            )
    return closures


def append_exact_open_sibling_closures(
    conn: Any,
    resolution: Mapping[str, Any],
    closures: Mapping[str, Any],
) -> dict[str, Any]:
    """Append newly enforced exact candidates to a resolution's Undo journal.

    Semantic enforcement can happen long after the human decision and after its
    UI Undo handle was issued.  The receipt is therefore an append-only journal:
    every later suppressed row is captured by the same resolution that caused
    the suppression, and Undo can restore the complete set.
    """

    resolution_id = normalize_text(resolution.get("id"))
    if not resolution_id:
        raise ReviewResolutionConflict(
            "review resolution journal is invalid; candidate was not suppressed"
        )
    row = conn.execute(
        "SELECT * FROM review_resolutions WHERE id = ? AND revoked_at IS NULL",
        (resolution_id,),
    ).fetchone()
    if row is None:
        raise ReviewResolutionConflict(
            "review resolution changed; candidate was not suppressed"
        )
    payload = decoded_json(row["decision_payload"], {})
    payload = dict(payload) if isinstance(payload, Mapping) else {}
    existing = exact_sibling_closure_journal(payload, allow_missing=True)
    # Validate both the existing journal and the proposed delta before writing
    # a replacement receipt.  This makes the update append-only rather than a
    # way to repair, reorder, or silently discard earlier rollback state.
    delta = exact_sibling_closure_journal(
        {EXACT_SIBLING_CLOSURES_KEY: dict(closures)}, allow_missing=False
    )
    merged = merge_exact_sibling_closure_journals(existing, delta)
    payload[EXACT_SIBLING_CLOSURES_KEY] = merged
    exact_open_sibling_restore_plan(
        conn,
        payload,
        resolution_id=resolution_id,
        disposition=str(row["disposition"] or ""),
        resolved_at=str(row["resolved_at"] or ""),
    )
    updated_receipt = conn.execute(
        """
        UPDATE review_resolutions SET decision_payload = ?
        WHERE id = ? AND revoked_at IS NULL
        """,
        (json.dumps(payload, sort_keys=True), resolution_id),
    )
    if updated_receipt.rowcount != 1:
        raise ReviewResolutionConflict(
            "review resolution changed; candidate was not suppressed"
        )
    updated = conn.execute(
        "SELECT * FROM review_resolutions WHERE id = ?", (resolution_id,)
    ).fetchone()
    return plain_row(updated) or {}


def exact_sibling_closure_journal(
    decision_payload: Mapping[str, Any], *, allow_missing: bool
) -> dict[str, Any]:
    raw = decision_payload.get(EXACT_SIBLING_CLOSURES_KEY)
    if raw is None and allow_missing:
        return {
            "version": EXACT_SIBLING_CLOSURES_VERSION,
            "actions": [],
            "questions": [],
        }
    if not isinstance(raw, Mapping) or raw.get("version") != (
        EXACT_SIBLING_CLOSURES_VERSION
    ):
        raise ReviewResolutionConflict(
            "review resolution rollback journal is incomplete or unsupported; "
            "undo was not applied"
        )
    journal: dict[str, Any] = {
        "version": EXACT_SIBLING_CLOSURES_VERSION,
        "actions": [],
        "questions": [],
    }
    for key in ("actions", "questions"):
        states = raw.get(key)
        if not isinstance(states, list):
            raise ReviewResolutionConflict(
                "review resolution rollback journal is malformed; undo was not applied"
            )
        seen: set[str] = set()
        for raw_state in states:
            state = dict(raw_state) if isinstance(raw_state, Mapping) else {}
            row_id = normalize_text(state.get("id"))
            before = state.get("before")
            fingerprint = normalize_text(state.get("after_fingerprint"))
            if (
                not row_id
                or row_id in seen
                or not isinstance(before, Mapping)
                or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
            ):
                raise ReviewResolutionConflict(
                    "review resolution rollback journal is malformed; undo was not applied"
                )
            seen.add(row_id)
            journal[key].append(state)
    return journal


def merge_exact_sibling_closure_journals(
    existing: Mapping[str, Any], delta: Mapping[str, Any]
) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "version": EXACT_SIBLING_CLOSURES_VERSION,
        "actions": list(existing.get("actions") or []),
        "questions": list(existing.get("questions") or []),
    }
    for key in ("actions", "questions"):
        existing_ids = {
            normalize_text(state.get("id"))
            for state in merged[key]
            if isinstance(state, Mapping)
        }
        delta_states = list(delta.get(key) or [])
        if any(
            normalize_text(state.get("id")) in existing_ids
            for state in delta_states
            if isinstance(state, Mapping)
        ):
            raise ReviewResolutionConflict(
                "review resolution rollback journal is not append-only; undo was not applied"
            )
        merged[key].extend(delta_states)
    return merged


def revoke_review_resolution(
    conn: Any,
    resolution_id: str,
    *,
    revoked_at: str | None = None,
    restore_superseded: bool = True,
) -> None:
    conn.execute("SAVEPOINT revoke_review_resolution")
    try:
        plan = review_resolution_revoke_plan(
            conn,
            resolution_id,
            restore_superseded=restore_superseded,
        )
        if plan is None:
            conn.execute("RELEASE SAVEPOINT revoke_review_resolution")
            return

        revoked = conn.execute(
            """
            UPDATE review_resolutions
            SET revoked_at = ?
            WHERE id = ? AND revoked_at IS NULL
            """,
            (revoked_at or now_iso(), resolution_id),
        )
        if revoked.rowcount != 1:
            raise ReviewResolutionConflict(
                "review resolution changed; undo was not applied"
            )
        apply_exact_open_sibling_restore(conn, plan["sibling_restore"])
        superseded_id = str(plan.get("superseded_id") or "")
        if superseded_id:
            restored = conn.execute(
                """
                UPDATE review_resolutions
                SET revoked_at = NULL
                WHERE id = ? AND revoked_at IS NOT NULL
                """,
                (superseded_id,),
            )
            if restored.rowcount != 1:
                raise ReviewResolutionConflict(
                    "review resolution history changed; undo was not applied"
                )
        conn.execute("RELEASE SAVEPOINT revoke_review_resolution")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT revoke_review_resolution")
        conn.execute("RELEASE SAVEPOINT revoke_review_resolution")
        raise


def preflight_review_resolution_revoke(
    conn: Any,
    resolution_id: str,
    *,
    restore_superseded: bool = True,
) -> None:
    """Raise before a larger undo starts when a resolution cannot be revoked."""

    review_resolution_revoke_plan(
        conn,
        resolution_id,
        restore_superseded=restore_superseded,
    )


def review_resolution_revoke_plan(
    conn: Any,
    resolution_id: str,
    *,
    restore_superseded: bool,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT family_key, state_fingerprint, decision_payload, disposition,
               resolved_at, revoked_at
        FROM review_resolutions WHERE id = ?
        """,
        (resolution_id,),
    ).fetchone()
    if row is None or row["revoked_at"] is not None:
        return None
    payload = decoded_json(row["decision_payload"], {})
    sibling_restore = exact_open_sibling_restore_plan(
        conn,
        payload,
        resolution_id=resolution_id,
        disposition=str(row["disposition"] or ""),
        resolved_at=str(row["resolved_at"] or ""),
    )
    superseded_id = str(payload.get("superseded_resolution_id") or "").strip()
    if superseded_id and restore_superseded:
        superseded = conn.execute(
            """
            SELECT family_key, state_fingerprint, decision_payload,
                   disposition, resolved_at, revoked_at
            FROM review_resolutions WHERE id = ?
            """,
            (superseded_id,),
        ).fetchone()
        if (
            superseded is None
            or superseded["revoked_at"] is None
            or superseded["family_key"] != row["family_key"]
            or superseded["state_fingerprint"] != row["state_fingerprint"]
        ):
            raise ReviewResolutionConflict(
                "review resolution history changed; undo was not applied"
            )
        superseded_payload = decoded_json(superseded["decision_payload"], {})
        exact_open_sibling_restore_plan(
            conn,
            superseded_payload if isinstance(superseded_payload, Mapping) else {},
            resolution_id=superseded_id,
            disposition=str(superseded["disposition"] or ""),
            resolved_at=str(superseded["resolved_at"] or ""),
        )
    return {
        "sibling_restore": sibling_restore,
        "superseded_id": superseded_id if restore_superseded else "",
    }


def restore_exact_open_siblings(
    conn: Any,
    decision_payload: Mapping[str, Any],
    *,
    resolution_id: str,
    disposition: str,
    resolved_at: str,
) -> None:
    """Restore the closure set, refusing to overwrite any newer review state."""

    restore_plan = exact_open_sibling_restore_plan(
        conn,
        decision_payload,
        resolution_id=resolution_id,
        disposition=disposition,
        resolved_at=resolved_at,
    )
    apply_exact_open_sibling_restore(conn, restore_plan)


def exact_open_sibling_restore_plan(
    conn: Any,
    decision_payload: Mapping[str, Any],
    *,
    resolution_id: str,
    disposition: str,
    resolved_at: str,
) -> dict[str, list[dict[str, Any]]]:
    """Validate and return the complete sibling restore plan without mutating."""

    has_journal = EXACT_SIBLING_CLOSURES_KEY in decision_payload
    closures = exact_sibling_closure_journal(
        decision_payload, allow_missing=not has_journal
    )
    require_complete_exact_sibling_journal(
        conn,
        closures,
        resolution_id=resolution_id,
    )
    if not has_journal:
        return {"questions": [], "actions": []}
    question_states: list[dict[str, Any]] = []
    for raw in closures.get("questions") or []:
        state = mapping(raw)
        question_id = normalize_text(state.get("id"))
        current = conn.execute(
            "SELECT * FROM open_questions WHERE id = ?",
            (question_id,),
        ).fetchone()
        expected_fingerprint = normalize_text(state.get("after_fingerprint"))
        expected_answer = {
            "decision": "exact_semantic_state_already_resolved",
            "disposition": disposition,
            "resolution_id": resolution_id,
        }
        before = mapping(state.get("before"))
        if (
            current is None
            or not expected_fingerprint
            or normalize_text(before.get("status")) not in {"open", "needs_human"}
            or current["status"] != "auto_resolved"
            or current["decided_by"] != "semantic_review_resolution"
            or decoded_json(current["answer"], {}) != expected_answer
            or database_row_fingerprint(dict(current)) != expected_fingerprint
        ):
            raise ReviewResolutionConflict(
                "a related review question changed; undo was not applied"
            )
        question_states.append(state)
    action_states: list[dict[str, Any]] = []
    for raw in closures.get("actions") or []:
        state = mapping(raw)
        action_id = normalize_text(state.get("id"))
        current = conn.execute(
            "SELECT * FROM cos_actions WHERE id = ?",
            (action_id,),
        ).fetchone()
        expected_fingerprint = normalize_text(state.get("after_fingerprint"))
        before = mapping(state.get("before"))
        semantic = (
            mapping(decoded_json(current["evidence_json"], {})).get(
                "semantic_resolution"
            )
            if current is not None
            else {}
        )
        semantic = mapping(semantic)
        if (
            current is None
            or not expected_fingerprint
            or normalize_text(before.get("status"))
            in {"", "applied", "auto_applied", "rejected"}
            or current["status"] != "rejected"
            or normalize_text(semantic.get("resolution_id")) != resolution_id
            or normalize_text(semantic.get("disposition")) != disposition
            or normalize_text(semantic.get("resolved_at")) != resolved_at
            or normalize_text(semantic.get("outcome"))
            not in {
                "exact_open_candidate_closed",
                "exact_semantic_state_already_kept",
                "exact_semantic_state_previously_rejected",
            }
            or database_row_fingerprint(dict(current)) != expected_fingerprint
        ):
            raise ReviewResolutionConflict(
                "a related review action changed; undo was not applied"
            )
        action_states.append(state)

    return {"questions": question_states, "actions": action_states}


def require_complete_exact_sibling_journal(
    conn: Any,
    closures: Mapping[str, Any],
    *,
    resolution_id: str,
) -> None:
    """Refuse a receipt that dropped rows previously suppressed by it."""

    action_ids = {
        normalize_text(state.get("id"))
        for state in closures.get("actions") or []
        if isinstance(state, Mapping)
    }
    for row in conn.execute(
        "SELECT id, evidence_json FROM cos_actions WHERE status = 'rejected'"
    ):
        evidence = mapping(row["evidence_json"])
        semantic = mapping(evidence.get("semantic_resolution"))
        if (
            normalize_text(semantic.get("resolution_id")) == resolution_id
            and normalize_text(semantic.get("outcome"))
            != "historical_exact_candidate_closed"
            and normalize_text(row["id"]) not in action_ids
        ):
            raise ReviewResolutionConflict(
                "review resolution rollback journal is incomplete; undo was not applied"
            )
    if not action_ids or not table_exists(conn, "open_questions"):
        return
    question_ids = {
        normalize_text(state.get("id"))
        for state in closures.get("questions") or []
        if isinstance(state, Mapping)
    }
    for start in range(0, len(action_ids), SQLITE_ID_BATCH_SIZE):
        batch = sorted(action_ids)[start : start + SQLITE_ID_BATCH_SIZE]
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"""
            SELECT id, answer FROM open_questions
            WHERE action_id IN ({placeholders})
              AND status = 'auto_resolved'
              AND decided_by = 'semantic_review_resolution'
            """,
            batch,
        ):
            answer = mapping(row["answer"])
            if (
                normalize_text(answer.get("resolution_id")) == resolution_id
                and normalize_text(row["id"]) not in question_ids
            ):
                raise ReviewResolutionConflict(
                    "review resolution rollback journal is incomplete; undo was not applied"
                )


def apply_exact_open_sibling_restore(
    conn: Any, restore_plan: Mapping[str, list[dict[str, Any]]]
) -> None:
    """Apply a previously validated sibling restore plan."""

    for state in restore_plan.get("questions") or []:
        before = mapping(state.get("before"))
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
                normalize_text(state.get("id")),
            ),
        )
    for state in restore_plan.get("actions") or []:
        action_id = normalize_text(state.get("id"))
        before = mapping(state.get("before"))
        conn.execute(
            "UPDATE cos_actions SET status = ?, evidence_json = ? WHERE id = ?",
            (
                before.get("status"),
                before.get("evidence_json"),
                action_id,
            ),
        )


def action_targets_confirmed_fact(conn: Any, action: Mapping[str, Any]) -> bool:
    if normalize_text(action.get("action_type")) != "fact_upsert":
        return False
    payload = action_payload(action)
    fact = payload.get("fact") if isinstance(payload.get("fact"), dict) else None
    if fact is None:
        return False
    statement = normalize_text(fact.get("statement"))
    fact_ids = stable_strings([fact.get("id"), *(action.get("target_fact_ids") or [])])
    if not statement or not fact_ids:
        return False
    for start in range(0, len(fact_ids), 400):
        fact_id_batch = fact_ids[start : start + 400]
        placeholders = ",".join("?" for _ in fact_id_batch)
        for row in conn.execute(
            f"""
            SELECT *
            FROM facts
            WHERE id IN ({placeholders})
            """,
            fact_id_batch,
        ):
            persisted = decoded_fact_row(row)
            if (
                normalize_text(persisted.get("statement")) == statement
                and normalize_text(row["status"]) in ACTIVE_FACT_STATUSES
                and bool(row["confirmed_by_user"])
                and confirmed_fact_review_state_matches(fact, persisted)
            ):
                return True
    return False


def action_is_manually_resolved(conn: Any, action: Mapping[str, Any]) -> bool:
    resolution = active_resolution_for_action(conn, action)
    return bool(
        (resolution and resolution.get("disposition") == "keep")
        or action_targets_confirmed_fact(conn, action)
    )


def backfill_review_resolutions(
    conn: Any, *, restore_superseded_on_revoke: bool = True
) -> dict[str, int]:
    if not resolution_table_exists(conn):
        return {
            "confirmed_facts": 0,
            "human_questions": 0,
            "unmatched_human_questions": 0,
            "audit_decisions": 0,
        }
    counts = {
        "confirmed_facts": 0,
        "human_questions": 0,
        "unmatched_human_questions": 0,
        "audit_decisions": 0,
    }

    fact_columns = table_columns(conn, "facts")
    if {
        "id",
        "statement",
        "status",
        "confirmed_by_user",
        "created_at",
    }.issubset(fact_columns):
        for row in conn.execute(
            """
            SELECT * FROM facts
            WHERE confirmed_by_user = 1
              AND status IN ('active', 'contested', 'conflicted', 'needs_confirmation')
            ORDER BY created_at, id
            """
        ):
            fact = decoded_fact_row(row)
            action = synthetic_fact_action(fact)
            _, created = record_review_resolution(
                conn,
                action,
                disposition="keep",
                source_item_kind="fact_confirmation",
                source_item_id=str(fact.get("id") or ""),
                decided_by="human",
                decision_payload={"confirmed_by_user": True},
                resolved_at=str(
                    fact.get("last_seen_at") or fact.get("created_at") or now_iso()
                ),
                resolution_id=deterministic_resolution_id(
                    f"confirmed_fact:{fact.get('id')}", action
                ),
                cleanup_existing=True,
            )
            counts["confirmed_facts"] += int(created)

    question_columns = table_columns(conn, "open_questions")
    action_columns = table_columns(conn, "cos_actions")
    if {
        "id",
        "status",
        "answer",
        "answered_at",
        "action_id",
        "decided_by",
    }.issubset(question_columns) and {
        "id",
        "created_at",
    }.issubset(action_columns):
        rows = conn.execute(
            """
            SELECT *
            FROM open_questions q
            WHERE q.status IN ('answered', 'dismissed')
            ORDER BY q.answered_at, q.id
            """
        )
        for row in rows:
            question = decoded_question_row(row)
            action = original_question_action(conn, question)
            if not question_has_human_provenance(conn, question, action):
                continue
            handled, fact_resolutions = record_alternative_question_fact_resolutions(
                conn,
                question,
                decision=normalized_question_decision(question),
                deterministic_ids=True,
                allow_any_question=(
                    normalized_question_decision(question) == "manual_selection"
                ),
                restore_superseded_on_revoke=restore_superseded_on_revoke,
                cleanup_existing=True,
            )
            if handled:
                counts["human_questions"] += len(fact_resolutions)
                continue
            routed_action = routed_question_keep_action(conn, question, action)
            if routed_action is not None:
                action = routed_action
            manual_plan = manual_question_answer_resolution_plan(conn, question, action)
            if manual_plan:
                timestamp = str(question.get("answered_at") or now_iso())
                decision_payload = mapping(question.get("answer"))
                if normalize_text(question.get("decided_by")).lower() != "human":
                    decision_payload = {
                        **decision_payload,
                        "backfill_provenance": "legacy_human_resolution",
                    }
                expected = []
                for item in manual_plan:
                    identity = action_review_identity(item["action"])
                    expected.append(
                        (
                            identity["family_key"],
                            identity["state_fingerprint"],
                            str(item["disposition"]),
                        )
                    )
                reconcile_question_resolution_rows(
                    conn,
                    question,
                    expected=expected,
                    revoked_at=timestamp,
                    restore_superseded_on_revoke=restore_superseded_on_revoke,
                )
                for item in manual_plan:
                    resolution_action = item["action"]
                    identity = action_review_identity(resolution_action)
                    _, created = record_review_resolution(
                        conn,
                        resolution_action,
                        disposition=str(item["disposition"]),
                        source_item_kind="question",
                        source_item_id=str(question["id"]),
                        decided_by="human",
                        decision_payload=decision_payload,
                        resolved_at=timestamp,
                        resolution_id=deterministic_resolution_id(
                            "question:"
                            f"{question['id']}:manual_answer:"
                            f"{identity['family_key']}:"
                            f"{identity['state_fingerprint']}",
                            resolution_action,
                        ),
                        cleanup_existing=True,
                    )
                    counts["human_questions"] += int(created)
                continue
            disposition = question_review_disposition(question, action)
            if action is None or disposition is None:
                counts["unmatched_human_questions"] += 1
                continue
            decision_payload = mapping(question.get("answer"))
            if normalize_text(question.get("decided_by")).lower() != "human":
                decision_payload = {
                    **decision_payload,
                    "backfill_provenance": "legacy_human_resolution",
                }
            identity = action_review_identity(action)
            timestamp = str(question.get("answered_at") or now_iso())
            reconcile_question_resolution_rows(
                conn,
                question,
                expected=[
                    (
                        identity["family_key"],
                        identity["state_fingerprint"],
                        disposition,
                    )
                ],
                revoked_at=timestamp,
                restore_superseded_on_revoke=restore_superseded_on_revoke,
            )
            _, created = record_review_resolution(
                conn,
                action,
                disposition=disposition,
                source_item_kind="question",
                source_item_id=str(question["id"]),
                decided_by="human",
                decision_payload=decision_payload,
                resolved_at=timestamp,
                resolution_id=deterministic_resolution_id(
                    f"question:{question['id']}", action
                ),
                cleanup_existing=True,
            )
            counts["human_questions"] += int(created)

    if {"id", "created_at"}.issubset(action_columns):
        for row in conn.execute("SELECT * FROM cos_actions ORDER BY created_at, id"):
            action = decoded_action_row(row)
            decision = prior_audit_resolution(action)
            if decision is None:
                continue
            disposition, resolved_at, payload = decision
            if disposition == "reject" and active_route_keep_covers_action(
                conn, action
            ):
                # Routing retires the old proposal row with a human-rejection
                # marker, but accepts the semantic fact. Do not let the generic
                # action-history backfill overwrite that higher-fidelity Keep.
                continue
            _, created = record_review_resolution(
                conn,
                action,
                disposition=disposition,
                source_item_kind="audit",
                source_item_id=str(action.get("id") or ""),
                decided_by="human",
                decision_payload=payload,
                resolved_at=resolved_at,
                resolution_id=deterministic_resolution_id(
                    f"audit:{action.get('id')}", action
                ),
                cleanup_existing=True,
            )
            counts["audit_decisions"] += int(created)
    return counts


def original_question_action(
    conn: Any, question: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Recover the reviewed candidate, never an action created by its decision."""

    answer = mapping(question.get("answer"))
    context = mapping(question.get("context"))
    decision = normalized_question_decision(question)
    current_action_id = normalize_text(question.get("action_id"))
    references = [
        answer.get("old_action_id"),
        context.get("candidate_action_id"),
        context.get("action_id"),
        *candidate_option_action_ids(question),
    ]
    if decision not in QUESTION_REPLACEMENT_DECISIONS | {"both_true", "contested"}:
        references.extend([answer.get("action_id"), current_action_id])
    for raw_action_id in ordered_strings(references):
        action = action_by_id(conn, raw_action_id)
        if action is None:
            continue
        if raw_action_id == current_action_id and question_result_action(
            action, str(question.get("id") or "")
        ):
            continue
        return action

    recommended = mapping(question.get("recommended_action"))
    synthetic = synthetic_recommended_question_action(question, recommended)
    if synthetic is not None:
        return synthetic
    return synthetic_candidate_option_action(question)


def routed_question_keep_action(
    conn: Any,
    question: Mapping[str, Any],
    original_action: Mapping[str, Any] | None,
    *,
    decision: str = "",
) -> dict[str, Any] | None:
    """Return the semantic fact accepted by a human routing decision.

    A route changes where an accepted fact is projected, not whether the fact is
    true. ``page_hint`` is therefore deliberately absent from fact review
    identity. Prefer the applied route-result action for provenance, while
    retaining the original/synthetic action as a legacy and actionless fallback;
    both identify the same semantic fact for a normal route-only replacement.
    """

    normalized = normalize_question_decision(decision) or normalized_question_decision(
        question
    )
    if normalized not in QUESTION_ROUTE_DECISIONS:
        return None
    answer = mapping(question.get("answer"))
    original_identity = (
        action_review_identity(original_action) if original_action is not None else None
    )
    for action_id in ordered_strings(
        [answer.get("new_action_id"), question.get("action_id")]
    ):
        result = action_by_id(conn, action_id)
        result_identity = action_review_identity(result) if result is not None else None
        if (
            result is not None
            and normalize_text(result.get("action_type")) == "fact_upsert"
            and normalize_text(result.get("status")) in {"applied", "auto_applied"}
            and (
                original_identity is None
                or (
                    result_identity is not None
                    and result_identity["family_key"] == original_identity["family_key"]
                    and result_identity["state_fingerprint"]
                    == original_identity["state_fingerprint"]
                )
            )
            and (
                action_id == normalize_text(answer.get("new_action_id"))
                or question_result_action(result, normalize_text(question.get("id")))
            )
        ):
            return result
    return dict(original_action) if original_action is not None else None


def question_resolution_snapshot(
    conn: Any, original: Mapping[str, Any]
) -> dict[str, Any]:
    """Combine the final answer with candidate provenance captured before mutation."""

    question_id = normalize_text(original.get("id"))
    row = (
        conn.execute(
            "SELECT * FROM open_questions WHERE id = ?", (question_id,)
        ).fetchone()
        if question_id
        else None
    )
    current = decoded_question_row(row) if row else dict(original)
    for key in (
        "kind",
        "fact_ids",
        "page_hint",
        "context",
        "options",
        "recommended_action",
    ):
        if key in original:
            current[key] = original.get(key)
    # Decision flows can replace this with a result action. The caller's snapshot
    # still names the reviewed action, including an intentional blank action id.
    current["action_id"] = original.get("action_id")
    return current


def record_alternative_question_fact_resolutions(
    conn: Any,
    question: Mapping[str, Any],
    *,
    decision: str = "",
    deterministic_ids: bool = False,
    allow_any_question: bool = False,
    restore_superseded_on_revoke: bool = True,
    cleanup_existing: bool = False,
) -> tuple[bool, list[dict[str, Any]]]:
    plan = alternative_question_fact_resolution_plan(
        conn,
        question,
        decision=decision,
        allow_any_question=allow_any_question,
    )
    if plan is None:
        return False, []
    timestamp = str(question.get("answered_at") or now_iso())
    answer = mapping(question.get("answer"))
    resolutions: list[dict[str, Any]] = []
    expected = [
        (
            action_review_identity(item["action"])["family_key"],
            action_review_identity(item["action"])["state_fingerprint"],
            str(item["disposition"]),
        )
        for item in plan
    ]
    reconcile_question_resolution_rows(
        conn,
        question,
        expected=expected,
        revoked_at=timestamp,
        restore_superseded_on_revoke=restore_superseded_on_revoke,
    )
    for item in plan:
        action = item["action"]
        identity = action_review_identity(action)
        resolution, created = record_review_resolution(
            conn,
            action,
            disposition=str(item["disposition"]),
            source_item_kind="question",
            source_item_id=str(question.get("id") or ""),
            decided_by="human",
            decision_payload={
                **answer,
                "decision": normalize_question_decision(decision)
                or normalized_question_decision(question),
                "resolved_fact_ids": item["fact_ids"],
                "selected_fact_ids": item["selected_fact_ids"],
            },
            resolved_at=timestamp,
            resolution_id=(
                deterministic_resolution_id(
                    "question:"
                    f"{question.get('id')}:fact_state:"
                    f"{identity['family_key']}:{identity['state_fingerprint']}",
                    action,
                )
                if deterministic_ids
                else None
            ),
            cleanup_existing=cleanup_existing,
        )
        if created:
            resolutions.append(resolution)
    return True, resolutions


def alternative_question_fact_resolution_plan(
    conn: Any,
    question: Mapping[str, Any],
    *,
    decision: str = "",
    allow_any_question: bool = False,
) -> list[dict[str, Any]] | None:
    if not allow_any_question and not alternative_fact_question(question):
        return None
    fact_ids = ordered_strings(question.get("fact_ids"))
    if not fact_ids:
        return []
    answer = mapping(question.get("answer"))
    answer_selected = answer.get("selected_fact_ids")
    selected_ids = set(
        ordered_strings(
            [
                answer.get("selected_fact_id"),
                *(answer_selected if isinstance(answer_selected, list) else []),
            ]
        )
    )
    normalized = normalize_question_decision(decision) or normalized_question_decision(
        question
    )
    keep_all = normalized in {"both_true", "contested"}
    reject_all = normalize_text(question.get("status")).lower() == "dismissed" or (
        normalized in QUESTION_REJECT_DECISIONS and normalized != "manual_answer"
    )
    has_selection = bool(selected_ids) or legacy_human_answer_shape(answer)
    if not (keep_all or reject_all or has_selection):
        return []

    load_ids = ordered_strings([*fact_ids, *selected_ids])
    placeholders = ",".join("?" for _ in load_ids)
    facts = {
        str(row["id"]): decoded_fact_row(row)
        for row in conn.execute(
            f"SELECT * FROM facts WHERE id IN ({placeholders})", load_ids
        )
    }
    manual_selected_ids = [
        fact_id
        for fact_id in sorted(selected_ids.difference(fact_ids))
        if not reject_all
        and trusted_question_manual_fact(
            facts.get(fact_id), str(question.get("id") or "")
        )
    ]
    resolution_fact_ids = ordered_strings([*fact_ids, *manual_selected_ids])

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for fact_id in resolution_fact_ids:
        fact = facts.get(fact_id)
        if fact is None:
            continue
        action = synthetic_fact_action(fact)
        identity = action_review_identity(action)
        key = (identity["family_key"], identity["state_fingerprint"])
        group = grouped.setdefault(
            key,
            {
                "action": action,
                "fact_ids": [],
                "selected_fact_ids": [],
            },
        )
        group["fact_ids"].append(fact_id)
        if fact_id in selected_ids:
            group["selected_fact_ids"].append(fact_id)
            # Prefer the selected exact copy as the evidence representative.
            group["action"] = action

    plan: list[dict[str, Any]] = []
    for group in grouped.values():
        selected_exact_copy = bool(group["selected_fact_ids"])
        disposition = (
            "keep" if keep_all or (not reject_all and selected_exact_copy) else "reject"
        )
        plan.append({**group, "disposition": disposition})
    return plan


def trusted_question_manual_fact(
    fact: Mapping[str, Any] | None, question_id: str
) -> bool:
    if not fact or not bool(fact.get("confirmed_by_user")):
        return False
    source_ids = set(stable_strings(decoded_json(fact.get("source_ids"), [])))
    metadata = mapping(fact.get("metadata"))
    return bool(
        normalize_text(question_id)
        and (
            f"manual:question:{question_id}" in source_ids
            or normalize_text(metadata.get("question_id")) == question_id
        )
    )


def manual_question_answer_resolution_plan(
    conn: Any,
    question: Mapping[str, Any],
    original_action: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Reject every displaced fact state and keep the manual replacement."""

    answer = mapping(question.get("answer"))
    manual_fact_id = normalize_text(answer.get("selected_fact_id"))
    row = (
        conn.execute("SELECT * FROM facts WHERE id = ?", (manual_fact_id,)).fetchone()
        if manual_fact_id
        else None
    )
    manual_fact = decoded_fact_row(row) if row else None
    question_id = normalize_text(question.get("id"))
    if not trusted_question_manual_fact(manual_fact, question_id):
        return []
    corrected_fact_ids = [
        fact_id
        for fact_id in ordered_strings(question.get("fact_ids"))
        if fact_id != manual_fact_id
    ]
    corrected_facts: dict[str, dict[str, Any]] = {}
    if corrected_fact_ids:
        placeholders = ",".join("?" for _ in corrected_fact_ids)
        corrected_facts = {
            str(fact["id"]): fact
            for fact in (
                decoded_fact_row(row)
                for row in conn.execute(
                    f"SELECT * FROM facts WHERE id IN ({placeholders})",
                    corrected_fact_ids,
                )
            )
        }
    planned: list[dict[str, Any]] = []
    if original_action is not None:
        planned.append({"action": dict(original_action), "disposition": "reject"})
    planned.extend(
        {
            "action": synthetic_fact_action(corrected_facts[fact_id]),
            "disposition": "reject",
        }
        for fact_id in corrected_fact_ids
        if fact_id in corrected_facts
    )
    planned.append(
        {
            "action": synthetic_fact_action(manual_fact or {}),
            "disposition": "keep",
        }
    )
    by_state: dict[tuple[str, str], dict[str, Any]] = {}
    for item in planned:
        identity = action_review_identity(item["action"])
        by_state[(identity["family_key"], identity["state_fingerprint"])] = item
    return list(by_state.values())


def alternative_fact_question(question: Mapping[str, Any]) -> bool:
    if normalize_text(question.get("kind")).lower() != "conflict":
        return False
    return not any(
        isinstance(option, Mapping)
        and normalize_text(option.get("option_type")).lower() == "candidate_fact"
        for option in question.get("options") or []
    )


def normalize_question_decision(value: Any) -> str:
    return normalize_text(value).lower().replace("-", "_")


def reconcile_question_resolution_rows(
    conn: Any,
    question: Mapping[str, Any],
    *,
    expected: list[tuple[str, str, str]],
    revoked_at: str,
    restore_superseded_on_revoke: bool = True,
) -> None:
    question_id = normalize_text(question.get("id"))
    if not question_id:
        return
    expected_states = set(expected)
    active_rows = list(
        conn.execute(
            """
            SELECT id, family_key, state_fingerprint, disposition
            FROM review_resolutions
            WHERE source_item_kind = 'question' AND source_item_id = ?
              AND revoked_at IS NULL
            """,
            (question_id,),
        )
    )
    if expected_states:
        wrong_ids = [
            str(row["id"])
            for row in active_rows
            if (
                str(row["family_key"]),
                str(row["state_fingerprint"]),
                str(row["disposition"]),
            )
            not in expected_states
        ]
    else:
        current = conn.execute(
            "SELECT * FROM open_questions WHERE id = ?", (question_id,)
        ).fetchone()
        current_question = decoded_question_row(current) if current else {}
        result_action = action_by_id(
            conn, normalize_text(current_question.get("action_id"))
        )
        if not question_result_action(result_action, question_id):
            return
        result_identity = action_review_identity(result_action or {})
        wrong_ids = [
            str(row["id"])
            for row in active_rows
            if str(row["family_key"]) == result_identity["family_key"]
            and str(row["state_fingerprint"]) == result_identity["state_fingerprint"]
        ]
    for resolution_id in wrong_ids:
        revoke_review_resolution(
            conn,
            resolution_id,
            revoked_at=revoked_at,
            restore_superseded=restore_superseded_on_revoke,
        )


def question_has_human_provenance(
    conn: Any,
    question: Mapping[str, Any],
    original_action: Mapping[str, Any] | None,
) -> bool:
    decided_by = normalize_text(question.get("decided_by")).lower()
    if decided_by:
        return decided_by == "human"
    if not normalize_text(question.get("answered_at")):
        return False

    answer = mapping(question.get("answer"))
    decision = normalized_question_decision(question)
    current = action_by_id(conn, normalize_text(question.get("action_id")))
    if legacy_human_answer_shape(answer) and question_result_action(
        current, str(question.get("id") or "")
    ):
        return True
    if (
        normalize_text(question.get("status")).lower() == "dismissed"
        and original_action
        and action_has_human_rejection(original_action)
    ):
        return True
    if decision in QUESTION_REPLACEMENT_DECISIONS | {"both_true", "contested"}:
        return bool(
            original_action
            and (
                action_has_human_rejection(original_action)
                or question_result_action(current, str(question.get("id") or ""))
            )
        )
    if decision in {"dismiss", "keep_existing", "reject", "reject_candidate"}:
        return bool(original_action and action_has_human_rejection(original_action))
    if decision in QUESTION_KEEP_DECISIONS:
        answer_action_id = normalize_text(answer.get("action_id"))
        return bool(
            original_action
            and normalize_text(original_action.get("status"))
            in {"applied", "auto_applied"}
            and (
                not answer_action_id
                or answer_action_id == normalize_text(original_action.get("id"))
            )
        )
    return False


def question_review_disposition(
    question: Mapping[str, Any], action: Mapping[str, Any] | None
) -> str | None:
    if normalize_text(question.get("status")).lower() == "dismissed":
        return "reject"
    decision = normalized_question_decision(question)
    if decision in QUESTION_ROUTE_DECISIONS:
        return "keep"
    if decision in QUESTION_REJECT_DECISIONS:
        return "reject"
    if decision in QUESTION_KEEP_DECISIONS:
        return "keep"

    answer = mapping(question.get("answer"))
    if legacy_human_answer_shape(answer):
        selected_many = answer.get("selected_fact_ids")
        selected = stable_strings(
            [
                answer.get("selected_fact_id"),
                *(selected_many if isinstance(selected_many, list) else []),
            ]
        )
        candidate_ids = action_candidate_fact_ids(action or {})
        if not candidate_ids:
            return None
        return "keep" if set(selected).intersection(candidate_ids) else "reject"
    # Explicit human provenance predating structured answers used answered=keep.
    if normalize_text(question.get("decided_by")).lower() == "human":
        return "keep"
    return None


def normalized_question_decision(question: Mapping[str, Any]) -> str:
    answer = mapping(question.get("answer"))
    return normalize_text(answer.get("decision")).lower().replace("-", "_")


def candidate_option_action_ids(question: Mapping[str, Any]) -> list[str]:
    return [
        normalize_text(option.get("action_id"))
        for option in question.get("options") or []
        if isinstance(option, Mapping)
        and normalize_text(option.get("option_type")).lower() == "candidate_fact"
        and normalize_text(option.get("action_id"))
    ]


def action_by_id(conn: Any, action_id: str) -> dict[str, Any] | None:
    if not action_id:
        return None
    row = conn.execute(
        "SELECT * FROM cos_actions WHERE id = ?", (action_id,)
    ).fetchone()
    return decoded_action_row(row) if row else None


def question_result_action(action: Mapping[str, Any] | None, question_id: str) -> bool:
    if not action:
        return False
    if normalize_text(action.get("proposed_by")) in QUESTION_RESULT_ACTION_PROPOSERS:
        return True
    payload = action_payload(action)
    return bool(
        question_id
        and normalize_text(payload.get("question_id")) == normalize_text(question_id)
        and normalize_text(action.get("proposed_by")) == "question_answer"
    )


def action_has_human_rejection(action: Mapping[str, Any]) -> bool:
    human_review = mapping(mapping(action.get("evidence_json")).get("human_review"))
    return normalize_text(human_review.get("decision")).lower() in {
        "dismiss",
        "reject",
    }


def legacy_human_answer_shape(answer: Mapping[str, Any]) -> bool:
    selected_many = answer.get("selected_fact_ids")
    if isinstance(selected_many, list) and stable_strings(selected_many):
        return True
    return bool(
        normalize_text(answer.get("selected_fact_id"))
        and ("answer" in answer or "superseded_fact_ids" in answer)
    )


def action_candidate_fact_ids(action: Mapping[str, Any]) -> set[str]:
    fact = mapping(action_payload(action).get("fact"))
    fact_id = normalize_text(fact.get("id"))
    if fact_id:
        return {fact_id}
    return set(stable_strings(action.get("target_fact_ids")))


def synthetic_recommended_question_action(
    question: Mapping[str, Any], recommended: Mapping[str, Any]
) -> dict[str, Any] | None:
    action_type = normalize_text(recommended.get("action_type"))
    payload = mapping(recommended.get("payload"))
    fact = mapping(payload.get("fact"))
    if action_type != "fact_upsert" or not complete_synthetic_fact(fact):
        return None
    return synthetic_question_fact_action(question, fact)


def synthetic_candidate_option_action(
    question: Mapping[str, Any],
) -> dict[str, Any] | None:
    for option in question.get("options") or []:
        if not isinstance(option, Mapping):
            continue
        if normalize_text(option.get("option_type")).lower() != "candidate_fact":
            continue
        fact = dict(option)
        fact.pop("option_type", None)
        fact.pop("action_id", None)
        fact.pop("label", None)
        if complete_synthetic_fact(fact, require_full_shape=True):
            return synthetic_question_fact_action(question, fact)
    return None


def complete_synthetic_fact(
    fact: Mapping[str, Any], *, require_full_shape: bool = False
) -> bool:
    if not normalize_text(fact.get("statement")):
        return False
    has_evidence = bool(
        stable_strings(fact.get("source_ids"))
        and (
            normalize_text(fact.get("evidence_quote") or fact.get("quote"))
            or decoded_json(fact.get("source_spans"), [])
        )
    )
    if not has_evidence:
        return False
    if not require_full_shape:
        return True
    return bool(
        normalize_text(fact.get("id"))
        and normalize_text(fact.get("entity_key"))
        and isinstance(fact.get("metadata"), (dict, str))
    )


def synthetic_question_fact_action(
    question: Mapping[str, Any], fact: Mapping[str, Any]
) -> dict[str, Any]:
    fact_id = normalize_text(fact.get("id"))
    page_hint = normalize_text(fact.get("page_hint") or question.get("page_hint"))
    return {
        "action_type": "fact_upsert",
        "target_fact_ids": [fact_id] if fact_id else [],
        "target_page_paths": [page_hint] if page_hint else [],
        "target_contract_ids": [],
        "action_features": {},
        "evidence_json": {"payload": {"fact": dict(fact)}},
    }


def decoded_question_row(row: Any) -> dict[str, Any]:
    values = {key: row[key] for key in row.keys()}
    return {
        "id": values.get("id"),
        "kind": values.get("kind"),
        "status": values.get("status"),
        "answer": decoded_json(values.get("answer"), None),
        "answered_at": values.get("answered_at"),
        "action_id": values.get("action_id"),
        "decided_by": values.get("decided_by"),
        "fact_ids": decoded_json(values.get("fact_ids"), []),
        "page_hint": values.get("page_hint"),
        "context": decoded_json(values.get("context"), {}),
        "options": decoded_json(values.get("options"), []),
        "recommended_action": decoded_json(values.get("recommended_action"), {}),
    }


def prior_audit_resolution(
    action: Mapping[str, Any],
) -> tuple[str, str, dict[str, Any]] | None:
    evidence = mapping(action.get("evidence_json"))
    human_review = mapping(evidence.get("human_review"))
    if normalize_text(human_review.get("decision")) in {"reject", "dismiss"}:
        return (
            "reject",
            str(human_review.get("decided_at") or now_iso()),
            dict(human_review),
        )
    records = [row for row in evidence.get("audits") or [] if isinstance(row, dict)]
    for record in reversed(records):
        metadata = mapping(record.get("metadata"))
        if metadata.get("ui_marked_ok"):
            return "keep", str(record.get("at") or now_iso()), dict(metadata)
        if metadata.get("ui_rejected_current_fact"):
            return "reject", str(record.get("at") or now_iso()), dict(metadata)
    return None


def active_route_keep_covers_action(conn: Any, action: Mapping[str, Any]) -> bool:
    resolution = active_resolution_for_action(conn, action)
    if not (
        resolution
        and normalize_text(resolution.get("disposition")) == "keep"
        and normalize_text(resolution.get("source_item_kind")) == "question"
    ):
        return False
    question_id = normalize_text(resolution.get("source_item_id"))
    row = conn.execute(
        "SELECT * FROM open_questions WHERE id = ?", (question_id,)
    ).fetchone()
    if row is None:
        return False
    decision = normalized_question_decision(decoded_question_row(row))
    return decision in QUESTION_ROUTE_DECISIONS


def decoded_action_row(row: Any) -> dict[str, Any]:
    values = {key: row[key] for key in row.keys()}
    return {
        "id": values.get("id"),
        "run_id": values.get("run_id"),
        "action_type": values.get("action_type"),
        "status": values.get("status"),
        "target_fact_ids": decoded_json(values.get("target_fact_ids"), []),
        "target_page_paths": decoded_json(values.get("target_page_paths"), []),
        "target_contract_ids": decoded_json(values.get("target_contract_ids"), []),
        "action_features": decoded_json(values.get("action_features"), {}),
        "proposed_by": values.get("proposed_by"),
        "evidence_json": decoded_json(values.get("evidence_json"), {}),
        "audit_status": values.get("audit_status"),
        "created_at": values.get("created_at"),
        "applied_at": values.get("applied_at"),
        "reverted_at": values.get("reverted_at"),
    }


def decoded_fact_row(row: Any) -> dict[str, Any]:
    result = {key: row[key] for key in row.keys()}
    for key, default in (
        ("source_ids", []),
        ("source_spans", []),
        ("metadata", {}),
    ):
        if key in result:
            result[key] = decoded_json(result[key], default)
    return result


def synthetic_fact_action(fact: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "action_type": "fact_upsert",
        "target_fact_ids": [str(fact.get("id") or "")],
        "target_page_paths": [],
        "target_contract_ids": [],
        "action_features": {},
        "evidence_json": {"payload": {"fact": dict(fact)}},
    }


def deterministic_resolution_id(prefix: str, action: Mapping[str, Any]) -> str:
    identity = action_review_identity(action)
    return f"resolution_{digest({'prefix': prefix, **identity})[:24]}"


def canonical_source_spans(value: Any) -> list[dict[str, Any]]:
    spans = []
    for raw in value or []:
        if not isinstance(raw, Mapping):
            continue
        spans.append(
            keep_present(
                {
                    "source_id": normalize_text(
                        first_present(raw, "source_id", "document_id")
                    ),
                    "chunk_id": normalize_text(raw.get("chunk_id")),
                    "start": integer_or_none(
                        first_present(raw, "start", "start_char", "start_offset")
                    ),
                    "end": integer_or_none(
                        first_present(raw, "end", "end_char", "end_offset")
                    ),
                    "quote": normalize_text(
                        first_present(raw, "quote", "evidence_quote")
                    ),
                    "page": integer_or_none(first_present(raw, "page", "page_number")),
                    "line_start": integer_or_none(raw.get("line_start")),
                    "line_end": integer_or_none(raw.get("line_end")),
                }
            )
        )
    return sorted(
        spans,
        key=lambda row: json.dumps(row, sort_keys=True, separators=(",", ":")),
    )


def canonical_fact_entity_state(fact: Mapping[str, Any]) -> dict[str, Any]:
    """Return the portable entity attribution that ``fact_upsert`` honors.

    ``fact_with_entity_links`` consumes normalized entity mentions, not caller-
    supplied ``entity_links`` or ``fact_entities`` rows.  Reuse that exact
    normalization path so top-level mentions shadow model metadata, legacy
    ``model_entity_key``/``entity_mention``/``entity_name`` fallbacks behave the
    same way, aliases for type/kind collapse, and the first mention becomes the
    primary when no explicit primary is present.

    Secondary mention order is not semantic after resolution, but primary
    selection is.  We therefore assign primary status before sorting and exact
    deduplication.  Confidence and mention spans are evidence/provenance jitter,
    not entity identity; raw mention keys that normalization discards are also
    intentionally absent.
    """

    from .entities import normalize_entity_name

    mentions_by_key: dict[str, dict[str, Any]] = {}
    for mention in fact_entity_mentions(dict(fact)):
        surface = normalize_entity_name(mention.get("surface"))
        if not surface:
            continue
        is_primary = bool(mention.get("is_primary"))
        resolution_name = normalize_entity_name(
            mention.get("entity_identity")
            if is_primary and mention.get("entity_identity")
            else mention.get("surface")
        )
        association = keep_present(
            {
                "surface": surface,
                "resolution_name": resolution_name,
                "entity_type": normalize_text(mention.get("entity_type")),
                "mention_kind": normalize_text(mention.get("mention_kind")),
                "is_primary": is_primary,
            }
        )
        key = json.dumps(
            association,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        mentions_by_key.setdefault(key, association)

    result = keep_present(
        {"mentions": [mentions_by_key[key] for key in sorted(mentions_by_key)]}
    )
    return result


def confirmed_fact_review_state_matches(
    candidate: Mapping[str, Any], persisted: Mapping[str, Any]
) -> bool:
    """Compare a candidate with a confirmed row across entity-resolution shapes.

    Applying a fact can add a resolver-derived ``entity_id`` even when the
    proposed action did not author one. Ignore that one-sided cache only when a
    matching receipt proves it was derived. Explicit and unknown IDs, and every
    entity key, remain strict and therefore reopen review.
    """

    candidate_fingerprint = fact_review_identity(candidate)["state_fingerprint"]
    if candidate_fingerprint == fact_review_identity(persisted)["state_fingerprint"]:
        return True
    candidate_entity_id = normalize_text(candidate.get("entity_id"))
    persisted_entity_id = normalize_text(persisted.get("entity_id"))
    if (
        candidate_entity_id
        or not persisted_entity_id
        or fact_entity_id_is_explicit(dict(persisted))
    ):
        return False
    portable_persisted = dict(persisted)
    portable_persisted["entity_id"] = None
    return (
        candidate_fingerprint
        == fact_review_identity(portable_persisted)["state_fingerprint"]
    )


def canonical_repeatable_rows(value: Any) -> list[Any]:
    """Canonicalize set-like relation rows without making row order semantic.

    Exact duplicate rows do not represent an additional temporal relationship,
    so they collapse to one entry.  The complete canonical row remains in the
    identity, which means changing a relationship's meaningful content still
    changes the state fingerprint.
    """
    decoded = decoded_json(value, value) if isinstance(value, str) else value
    if decoded in (None, "", [], {}):
        return []
    raw_rows = decoded if isinstance(decoded, (list, tuple, set)) else [decoded]
    rows_by_key: dict[str, Any] = {}
    for raw in raw_rows:
        row = canonical_payload(raw)
        key = json.dumps(
            row,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        rows_by_key.setdefault(key, row)
    return [rows_by_key[key] for key in sorted(rows_by_key)]


def canonical_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): canonical_payload(nested)
            for key, nested in sorted(value.items(), key=lambda item: str(item[0]))
            if str(key) not in VOLATILE_PAYLOAD_KEYS
        }
    if isinstance(value, (list, tuple, set)):
        return [canonical_payload(item) for item in value]
    if isinstance(value, str):
        return normalize_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return normalize_text(value)


def digest(value: Any) -> str:
    payload = json.dumps(
        canonical_payload(value),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def stable_strings(value: Any) -> list[str]:
    raw = value if isinstance(value, (list, tuple, set)) else [value]
    return sorted({normalize_text(item) for item in raw if normalize_text(item)})


def ordered_strings(value: Any) -> list[str]:
    raw = value if isinstance(value, (list, tuple, set)) else [value]
    return list(
        dict.fromkeys(normalize_text(item) for item in raw if normalize_text(item))
    )


def mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        decoded = decoded_json(value, {})
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return {}


def decoded_json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def integer_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def first_present(value: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in value and value.get(key) is not None:
            return value.get(key)
    return None


def keep_present(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item not in (None, "", [], {})}


def temporal_value_present(value: Any) -> bool:
    if value in (None, "", [], {}):
        return False
    return normalize_text(value).lower() != "unknown"


def table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    )


def table_columns(conn: Any, table: str) -> set[str]:
    if not table_exists(conn, table):
        return set()
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def plain_row(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    if hasattr(row, "keys"):
        result = {key: row[key] for key in row.keys()}
    else:
        return None
    if "decision_payload" in result:
        result["decision_payload"] = decoded_json(result["decision_payload"], {})
    return result
