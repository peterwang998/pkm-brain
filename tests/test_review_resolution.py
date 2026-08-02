from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import pkm_brain.audit_review as audit_review
from pkm_brain.audit_review import (
    decide_direct_action_queue_item,
    record_question_review_resolution,
    revoke_queue_review_resolution,
)
from pkm_brain.cos_actions import (
    FACT_ENTITY_ATTRIBUTION_SNAPSHOT_KEY,
    apply_action,
    get_action,
    propose_action,
    record_action_audit,
)
from pkm_brain.cos_audit import load_audit_sample, run_sampled_audit
from pkm_brain.candidate_retirement import database_row_fingerprint
from pkm_brain.db import connection
from pkm_brain.paths import BrainPaths
from pkm_brain.review_resolution import (
    ReviewResolutionConflict,
    active_resolution_for_action,
    action_is_manually_resolved,
    action_targets_confirmed_fact,
    backfill_review_resolutions,
    confirmed_fact_review_state_matches,
    fact_review_identity,
    preflight_review_resolution_revoke,
    record_review_resolution,
    revoke_review_resolution,
)
from pkm_brain.review_undo import (
    ReviewUndoError,
    require_current_undo_handle,
    seal_undo_handle,
)
from pkm_brain.service import BrainService
from pkm_brain.ui_errors import BadRequestError
from pkm_brain.ui_server import ui_confirm_fact, ui_queue_undo


def initialized_paths(tmp_path: Path) -> BrainPaths:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    return paths


def fact_payload(
    *,
    source_id: str = "document:launch",
    event_start_at: str = "2026-08-03T16:00:00+00:00",
) -> dict[str, Any]:
    return {
        "fact": {
            "id": "fact_launch",
            "statement": "The launch is scheduled for August 3.",
            "entity_key": "event:launch",
            "source_ids": [source_id],
            "source_spans": [{"chunk_id": "chunk_launch", "start": 0, "end": 45}],
            "evidence_quote": "The launch is scheduled for August 3.",
            "temporal_kind": "unknown",
            "valid_time_precision": "unknown",
            "event_time_kind": "scheduled_for",
            "event_start_at": event_start_at,
            "event_time_precision": "minute",
        }
    }


def attributed_fact_payload() -> dict[str, Any]:
    payload = fact_payload()
    mentions = [
        {
            "surface": "Launch",
            "entity_identity": "Launch (2026-08-03)",
            "entity_type": "event",
            "mention_kind": "named",
            "is_primary": True,
            "confidence": 0.95,
        },
        {
            "surface": "Acme",
            "entity_type": "organization",
            "mention_kind": "named",
            "is_primary": False,
            "confidence": 0.91,
        },
    ]
    payload["fact"].update(
        {
            "entity_key": "event:launch",
            # fact_upsert derives its links from entity_mentions; these caller-
            # supplied rows are intentionally ignored by mutation and review
            # identity alike.
            "entity_links": [
                {"entity_id": "ignored_launch", "is_primary": True},
                {"entity_id": "ignored_acme", "is_primary": False},
            ],
            "entity_mentions": mentions,
            "metadata": {"model_entity_mentions": json.loads(json.dumps(mentions))},
        }
    )
    return payload


def applied_action(
    paths: BrainPaths,
    *,
    run_id: str,
    payload: dict[str, Any],
    action_type: str = "fact_upsert",
    target_fact_ids: list[str] | None = None,
    target_page_paths: list[str] | None = None,
) -> dict[str, Any]:
    action = propose_action(
        paths,
        action_type,
        run_id=run_id,
        action_payload=payload,
        target_fact_ids=target_fact_ids or [],
        target_page_paths=target_page_paths or [],
        risk_tier="high",
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE cos_actions
            SET status = 'applied', applied_at = '2026-07-31T12:00:00+00:00'
            WHERE id = ?
            """,
            (action["id"],),
        )
    return get_action(paths, action["id"])


def direct_contract_review_actions(
    paths: BrainPaths,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = {
        "contract": {
            "id": "contract_atomic_review",
            "page_hint": "concepts/atomic-review.md",
            "canonical_entity": "concept:atomic-review",
            "page_scope": "review",
            "retrieval_purpose": "Keep the decision and resolution atomic.",
            "what_belongs_here": "Atomic review evidence.",
            "what_does_not_belong_here": "Unrelated evidence.",
            "freshness_policy": "manual",
            "related_pages": [],
            "version": 1,
            "status": "active",
        }
    }
    actions = [
        propose_action(
            paths,
            "edit_contract",
            action_payload=payload,
            action_features={
                "candidate_key": f"atomic-direct-review-{index}",
                "reversible": True,
            },
            target_page_paths=["concepts/atomic-review.md"],
            proposed_by=f"atomic_review_{index}",
            risk_tier="low",
        )
        for index in range(2)
    ]
    with connection(paths.sqlite_path) as conn:
        sibling_features = dict(actions[1]["action_features"])
        sibling_features["candidate_key"] = "atomic-direct-review-0"
        conn.execute(
            "UPDATE cos_actions SET action_features = ? WHERE id = ?",
            (json.dumps(sibling_features, sort_keys=True), actions[1]["id"]),
        )
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, question, status, context, created_at, action_id
            ) VALUES (
              'question_atomic_direct_review', 'action_review',
              'Should this exact sibling be applied?', 'needs_human', '{}',
              '2026-07-31T12:00:00+00:00', ?
            )
            """,
            (actions[1]["id"],),
        )
    return actions[0], actions[1]


def direct_review_rows(paths: BrainPaths) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        return {
            "actions": [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM cos_actions
                    WHERE proposed_by LIKE 'atomic_review_%'
                    ORDER BY proposed_by
                    """
                )
            ],
            "question": dict(
                conn.execute(
                    "SELECT * FROM open_questions WHERE id = ?",
                    ("question_atomic_direct_review",),
                ).fetchone()
            ),
            "contract_count": conn.execute(
                "SELECT COUNT(*) FROM page_contracts WHERE id = ?",
                ("contract_atomic_review",),
            ).fetchone()[0],
            "resolution_count": conn.execute(
                "SELECT COUNT(*) FROM review_resolutions"
            ).fetchone()[0],
        }


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_direct_action_and_resolution_write_are_one_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
) -> None:
    paths = initialized_paths(tmp_path)
    action, _sibling = direct_contract_review_actions(paths)
    before = direct_review_rows(paths)

    def fail_resolution_write(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected semantic resolution failure")

    monkeypatch.setattr(audit_review, "record_review_resolution", fail_resolution_write)
    with pytest.raises(RuntimeError, match="injected semantic resolution failure"):
        decide_direct_action_queue_item(paths, action, decision, {})

    assert direct_review_rows(paths) == before


def test_direct_action_approval_commits_resolution_with_action(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    action, sibling = direct_contract_review_actions(paths)

    result = decide_direct_action_queue_item(paths, action, "approve", {})

    assert result["result"]["action"]["status"] == "applied"
    assert result["undo_handle"]["kind"] == "action_apply"
    assert len(result["undo_handle"]["review_resolution_ids"]) == 1
    with connection(paths.sqlite_path) as conn:
        resolution = active_resolution_for_action(conn, action)
        contract_count = conn.execute(
            "SELECT COUNT(*) FROM page_contracts WHERE id = ?",
            ("contract_atomic_review",),
        ).fetchone()[0]
        sibling_status = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (sibling["id"],)
        ).fetchone()["status"]
        question_status = conn.execute(
            "SELECT status FROM open_questions WHERE id = ?",
            ("question_atomic_direct_review",),
        ).fetchone()["status"]
    assert resolution is not None
    assert resolution["disposition"] == "keep"
    assert contract_count == 1
    assert sibling_status in {"dismissed", "rejected"}
    assert question_status in {"dismissed", "auto_resolved"}


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_direct_action_undo_restores_exact_candidate_proposed_after_decision(
    tmp_path: Path, decision: str
) -> None:
    paths = initialized_paths(tmp_path)
    action, _sibling = direct_contract_review_actions(paths)
    result = decide_direct_action_queue_item(paths, action, decision, {})
    handle = result["undo_handle"]
    seal_undo_handle(paths, handle)

    late = propose_action(
        paths,
        action["action_type"],
        action_payload=dict(action["evidence_json"]["payload"]),
        action_features=dict(action["action_features"]),
        target_contract_ids=list(action["target_contract_ids"]),
        target_page_paths=list(action["target_page_paths"]),
        proposed_by="late_exact_candidate",
        risk_tier="low",
    )

    assert late["status"] == "rejected"
    assert late["evidence_json"]["semantic_resolution"]["disposition"] == (
        "keep" if decision == "approve" else "reject"
    )
    # The original handle remains valid because the system-owned journal grew
    # only by an authenticated suffix.
    require_current_undo_handle(paths, handle)
    ui_queue_undo(paths, {"undo_handle": handle})

    with connection(paths.sqlite_path) as conn:
        late_status = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (late["id"],)
        ).fetchone()["status"]
        resolution = conn.execute(
            "SELECT revoked_at FROM review_resolutions WHERE id = ?",
            (handle["review_resolution_ids"][0],),
        ).fetchone()
    assert late_status == "proposed"
    assert resolution["revoked_at"] is not None


def test_undo_guard_refuses_removed_late_resolution_journal_suffix(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    action, _sibling = direct_contract_review_actions(paths)
    result = decide_direct_action_queue_item(paths, action, "reject", {})
    handle = result["undo_handle"]
    seal_undo_handle(paths, handle)
    late = propose_action(
        paths,
        action["action_type"],
        action_payload=dict(action["evidence_json"]["payload"]),
        action_features=dict(action["action_features"]),
        target_contract_ids=list(action["target_contract_ids"]),
        target_page_paths=list(action["target_page_paths"]),
        proposed_by="late_removed_suffix",
        risk_tier="low",
    )
    assert late["status"] == "rejected"

    resolution_id = handle["review_resolution_ids"][0]
    with connection(paths.sqlite_path) as conn:
        row = conn.execute(
            "SELECT decision_payload FROM review_resolutions WHERE id = ?",
            (resolution_id,),
        ).fetchone()
        payload = json.loads(row["decision_payload"])
        payload.pop("exact_open_sibling_closures")
        conn.execute(
            "UPDATE review_resolutions SET decision_payload = ? WHERE id = ?",
            (json.dumps(payload, sort_keys=True), resolution_id),
        )

    with pytest.raises(ReviewUndoError, match="resolution journal"):
        require_current_undo_handle(paths, handle)
    assert get_action(paths, late["id"])["status"] == "rejected"


def test_revoking_resolution_restores_exact_sibling_and_linked_question(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    payload = fact_payload()
    source = propose_action(
        paths,
        "fact_upsert",
        action_payload=payload,
        target_fact_ids=["fact_source"],
        proposed_by="resolution_source",
        risk_tier="high",
    )
    sibling = propose_action(
        paths,
        "fact_upsert",
        action_payload=payload,
        target_fact_ids=["fact_sibling"],
        proposed_by="resolution_sibling",
        risk_tier="high",
    )
    previous_answer = {"draft": "preserve this pre-resolution state"}
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE cos_actions SET status = 'needs_human' WHERE id = ?",
            (sibling["id"],),
        )
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, question, status, answer, context, created_at, action_id
            ) VALUES (
              'question_exact_sibling', 'action_review', 'Review the duplicate?',
              'needs_human', ?, '{}', '2026-07-31T12:00:00+00:00', ?
            )
            """,
            (json.dumps(previous_answer, sort_keys=True), sibling["id"]),
        )
        resolution, created = record_review_resolution(
            conn,
            source,
            disposition="keep",
            source_item_kind="action",
            source_item_id=source["id"],
            decision_payload={
                "exact_open_sibling_closures": {
                    "version": 1,
                    "actions": [{"id": "caller_forged_action"}],
                }
            },
            resolved_at="2026-07-31T12:01:00+00:00",
        )

        assert created is True
        closures = resolution["decision_payload"]["exact_open_sibling_closures"]
        assert [item["id"] for item in closures["actions"]] == [sibling["id"]]
        assert [item["id"] for item in closures["questions"]] == [
            "question_exact_sibling"
        ]
        assert (
            conn.execute(
                "SELECT status FROM cos_actions WHERE id = ?", (sibling["id"],)
            ).fetchone()["status"]
            == "rejected"
        )
        assert (
            conn.execute(
                "SELECT status FROM open_questions WHERE id = 'question_exact_sibling'"
            ).fetchone()["status"]
            == "auto_resolved"
        )

        revoke_review_resolution(conn, resolution["id"])

        restored_action = conn.execute(
            "SELECT status, evidence_json FROM cos_actions WHERE id = ?",
            (sibling["id"],),
        ).fetchone()
        restored_question = conn.execute(
            """
            SELECT status, answer, answered_at, decided_by
            FROM open_questions WHERE id = 'question_exact_sibling'
            """
        ).fetchone()
    assert restored_action["status"] == "needs_human"
    assert "semantic_resolution" not in json.loads(restored_action["evidence_json"])
    assert restored_question["status"] == "needs_human"
    assert json.loads(restored_question["answer"]) == previous_answer
    assert restored_question["answered_at"] is None
    assert restored_question["decided_by"] is None


@pytest.mark.parametrize("drift_target", ["action", "question"])
def test_resolution_revoke_is_all_or_nothing_after_one_sided_drift(
    tmp_path: Path, drift_target: str
) -> None:
    paths = initialized_paths(tmp_path)
    source = propose_action(
        paths,
        "fact_upsert",
        action_payload=fact_payload(),
        target_fact_ids=["fact_source"],
        proposed_by="resolution_source",
        risk_tier="high",
    )
    sibling = propose_action(
        paths,
        "fact_upsert",
        action_payload=fact_payload(),
        target_fact_ids=["fact_sibling"],
        proposed_by="resolution_sibling",
        risk_tier="high",
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, question, status, context, created_at, action_id
            ) VALUES (
              'question_changed_sibling', 'action_review', 'Review the duplicate?',
              'open', '{}', '2026-07-31T12:00:00+00:00', ?
            )
            """,
            (sibling["id"],),
        )
        resolution, _created = record_review_resolution(
            conn,
            source,
            disposition="reject",
            source_item_kind="action",
            source_item_id=source["id"],
            resolved_at="2026-07-31T12:01:00+00:00",
        )
        closed_action = conn.execute(
            "SELECT evidence_json FROM cos_actions WHERE id = ?", (sibling["id"],)
        ).fetchone()
        changed_evidence = json.loads(closed_action["evidence_json"])
        changed_evidence["human_review"] = {"decision": "keep_after_resolution"}
        if drift_target == "action":
            conn.execute(
                "UPDATE cos_actions SET evidence_json = ? WHERE id = ?",
                (json.dumps(changed_evidence, sort_keys=True), sibling["id"]),
            )
        else:
            conn.execute(
                """
                UPDATE open_questions
                SET status = 'answered', answer = '{"decision":"manual_override"}',
                    answered_at = '2026-07-31T12:02:00+00:00', decided_by = 'human'
                WHERE id = 'question_changed_sibling'
                """
            )

        with pytest.raises(ReviewResolutionConflict, match="changed"):
            revoke_review_resolution(conn, resolution["id"])

        changed_action = conn.execute(
            "SELECT status, evidence_json FROM cos_actions WHERE id = ?",
            (sibling["id"],),
        ).fetchone()
        changed_question = conn.execute(
            "SELECT status, answer, decided_by FROM open_questions WHERE id = ?",
            ("question_changed_sibling",),
        ).fetchone()
        still_active = conn.execute(
            "SELECT revoked_at FROM review_resolutions WHERE id = ?",
            (resolution["id"],),
        ).fetchone()
    # A direct/internal revoke fails explicitly and rolls back the ledger and
    # the complete captured closure set when either side drifted.
    assert still_active["revoked_at"] is None
    assert changed_action["status"] == "rejected"
    if drift_target == "action":
        assert json.loads(changed_action["evidence_json"]) == changed_evidence
        assert changed_question["status"] == "auto_resolved"
        assert changed_question["decided_by"] == "semantic_review_resolution"
    else:
        semantic = json.loads(changed_action["evidence_json"])["semantic_resolution"]
        assert semantic["resolution_id"] == resolution["id"]
        assert changed_question["status"] == "answered"
        assert json.loads(changed_question["answer"]) == {"decision": "manual_override"}
        assert changed_question["decided_by"] == "human"


@pytest.mark.parametrize("drift_target", ["action", "question"])
def test_resolution_revoke_fingerprints_the_complete_retired_row(
    tmp_path: Path, drift_target: str
) -> None:
    paths = initialized_paths(tmp_path)
    source = propose_action(
        paths,
        "fact_upsert",
        action_payload=fact_payload(),
        target_fact_ids=["fact_full_row_source"],
        proposed_by="full_row_source",
        risk_tier="high",
    )
    sibling = propose_action(
        paths,
        "fact_upsert",
        action_payload=fact_payload(),
        target_fact_ids=["fact_full_row_sibling"],
        proposed_by="full_row_sibling",
        risk_tier="high",
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, question, status, context, created_at, action_id
            ) VALUES (
              'question_full_row_sibling', 'action_review', 'Original wording?',
              'open', '{}', '2026-07-31T12:00:00+00:00', ?
            )
            """,
            (sibling["id"],),
        )
        resolution, _ = record_review_resolution(
            conn,
            source,
            disposition="reject",
            source_item_kind="action",
            source_item_id=source["id"],
        )
        if drift_target == "action":
            # policy_decision was not one of the formerly hand-checked fields.
            conn.execute(
                "UPDATE cos_actions SET policy_decision = 'new_review' WHERE id = ?",
                (sibling["id"],),
            )
        else:
            # Question copy is likewise outside the fields restored by Undo.
            conn.execute(
                "UPDATE open_questions SET question = 'New wording?' WHERE id = ?",
                ("question_full_row_sibling",),
            )

        with pytest.raises(ReviewResolutionConflict, match="changed"):
            revoke_review_resolution(conn, resolution["id"])
        assert (
            conn.execute(
                "SELECT revoked_at FROM review_resolutions WHERE id = ?",
                (resolution["id"],),
            ).fetchone()["revoked_at"]
            is None
        )


def test_resolution_revoke_refuses_legacy_incomplete_closure_journal(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    action = applied_action(
        paths,
        run_id="run_legacy_closure",
        payload=fact_payload(),
        target_fact_ids=["fact_legacy_closure"],
    )
    with connection(paths.sqlite_path) as conn:
        resolution, _ = record_review_resolution(
            conn,
            action,
            disposition="keep",
            source_item_kind="audit",
            source_item_id=action["id"],
        )
        payload = dict(resolution["decision_payload"])
        payload["exact_open_sibling_closures"] = {
            "version": 1,
            "actions": [],
            "questions": [],
        }
        conn.execute(
            "UPDATE review_resolutions SET decision_payload = ? WHERE id = ?",
            (json.dumps(payload, sort_keys=True), resolution["id"]),
        )

        with pytest.raises(ReviewResolutionConflict, match="incomplete or unsupported"):
            revoke_review_resolution(conn, resolution["id"])
        assert (
            conn.execute(
                "SELECT revoked_at FROM review_resolutions WHERE id = ?",
                (resolution["id"],),
            ).fetchone()["revoked_at"]
            is None
        )


def test_resolution_revoke_rejects_forged_journal_suffix_even_with_matching_hash(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    source = applied_action(
        paths,
        run_id="run_forged_suffix_source",
        payload=fact_payload(),
        target_fact_ids=["fact_forged_suffix_source"],
    )
    unrelated = propose_action(
        paths,
        "canonicalize_page",
        action_payload={"page_hint": "concepts/unrelated.md"},
        target_page_paths=["concepts/unrelated.md"],
        override_semantic_rejection=True,
    )
    with connection(paths.sqlite_path) as conn:
        resolution, _ = record_review_resolution(
            conn,
            source,
            disposition="keep",
            source_item_kind="audit",
            source_item_id=source["id"],
        )
        conn.execute(
            "UPDATE cos_actions SET status = 'rejected' WHERE id = ?",
            (unrelated["id"],),
        )
        row = conn.execute(
            "SELECT * FROM cos_actions WHERE id = ?", (unrelated["id"],)
        ).fetchone()
        payload = dict(resolution["decision_payload"])
        payload["exact_open_sibling_closures"] = {
            "version": 2,
            "actions": [
                {
                    "id": unrelated["id"],
                    "before": {
                        "status": "proposed",
                        "evidence_json": row["evidence_json"],
                    },
                    "after_fingerprint": database_row_fingerprint(dict(row)),
                }
            ],
            "questions": [],
        }
        conn.execute(
            "UPDATE review_resolutions SET decision_payload = ? WHERE id = ?",
            (json.dumps(payload, sort_keys=True), resolution["id"]),
        )

        with pytest.raises(ReviewResolutionConflict, match="action changed"):
            revoke_review_resolution(conn, resolution["id"])
        assert (
            conn.execute(
                "SELECT status FROM cos_actions WHERE id = ?", (unrelated["id"],)
            ).fetchone()["status"]
            == "rejected"
        )


def test_resolution_revoke_drift_does_not_activate_superseded_decision(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    source = applied_action(
        paths,
        run_id="run_atomic_resolution_chain",
        payload=fact_payload(),
        target_fact_ids=["fact_source"],
    )
    with connection(paths.sqlite_path) as conn:
        previous, _ = record_review_resolution(
            conn,
            source,
            disposition="keep",
            source_item_kind="audit",
            source_item_id="audit_keep",
            resolved_at="2026-07-31T12:00:00+00:00",
            resolution_id="resolution_atomic_previous",
        )

    sibling = propose_action(
        paths,
        "fact_upsert",
        action_payload=fact_payload(),
        target_fact_ids=["fact_sibling"],
        proposed_by="legacy_backlog",
        risk_tier="high",
        override_semantic_rejection=True,
    )
    with connection(paths.sqlite_path) as conn:
        evidence = dict(sibling["evidence_json"])
        evidence.pop("semantic_resolution", None)
        conn.execute(
            "UPDATE cos_actions SET status = 'proposed', evidence_json = ? WHERE id = ?",
            (json.dumps(evidence, sort_keys=True), sibling["id"]),
        )
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, question, status, context, created_at, action_id
            ) VALUES (
              'question_atomic_chain', 'action_review', 'Review the duplicate?',
              'open', '{}', '2026-07-31T12:00:30+00:00', ?
            )
            """,
            (sibling["id"],),
        )
        latest, _ = record_review_resolution(
            conn,
            source,
            disposition="reject",
            source_item_kind="audit",
            source_item_id="audit_reject",
            resolved_at="2026-07-31T12:01:00+00:00",
            resolution_id="resolution_atomic_latest",
        )
        conn.execute(
            """
            UPDATE open_questions
            SET status = 'answered', answer = '{"decision":"manual_override"}',
                answered_at = '2026-07-31T12:02:00+00:00', decided_by = 'human'
            WHERE id = 'question_atomic_chain'
            """
        )

        with pytest.raises(ReviewResolutionConflict, match="question changed"):
            revoke_review_resolution(conn, latest["id"])

        rows = {
            row["id"]: row["revoked_at"]
            for row in conn.execute(
                """
                SELECT id, revoked_at FROM review_resolutions
                WHERE id IN (?, ?)
                """,
                (previous["id"], latest["id"]),
            )
        }
        sibling_state = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (sibling["id"],)
        ).fetchone()
    assert rows[latest["id"]] is None
    assert rows[previous["id"]] is not None
    assert sibling_state["status"] == "rejected"


def test_ui_undo_preflights_resolution_history_before_restoring_action(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    source = applied_action(
        paths,
        run_id="run_resolution_preflight",
        payload=fact_payload(),
        target_fact_ids=["fact_source"],
    )
    with connection(paths.sqlite_path) as conn:
        previous, _ = record_review_resolution(
            conn,
            source,
            disposition="keep",
            source_item_kind="audit",
            source_item_id="audit_preflight_keep",
            resolution_id="resolution_preflight_previous",
        )
        latest, _ = record_review_resolution(
            conn,
            source,
            disposition="reject",
            source_item_kind="audit",
            source_item_id="audit_preflight_reject",
            resolution_id="resolution_preflight_latest",
        )
        conn.execute(
            "UPDATE review_resolutions SET family_key = 'corrupt:history' WHERE id = ?",
            (previous["id"],),
        )

    handle = {
        "kind": "action_status",
        "action": audit_review.action_undo_state(source),
        "review_resolution_ids": [latest["id"]],
    }
    seal_undo_handle(paths, handle)
    with pytest.raises(BadRequestError, match="history changed"):
        ui_queue_undo(paths, {"undo_handle": handle})

    with connection(paths.sqlite_path) as conn:
        current_action = conn.execute(
            "SELECT status FROM cos_actions WHERE id = ?", (source["id"],)
        ).fetchone()
        latest_state = conn.execute(
            "SELECT revoked_at FROM review_resolutions WHERE id = ?", (latest["id"],)
        ).fetchone()
        previous_state = conn.execute(
            "SELECT revoked_at FROM review_resolutions WHERE id = ?",
            (previous["id"],),
        ).fetchone()
    assert current_action["status"] == source["status"]
    assert latest_state["revoked_at"] is None
    assert previous_state["revoked_at"] is not None


def test_backfill_cleans_same_disposition_siblings_without_rewriting_receipt(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    source = applied_action(
        paths,
        run_id="run_existing_resolution_backfill",
        payload=fact_payload(),
        target_fact_ids=["fact_source"],
    )
    record_action_audit(
        paths,
        source["id"],
        "sampled_ok",
        metadata={"ui_marked_ok": True, "note": "already reviewed"},
    )
    source = get_action(paths, source["id"])
    with connection(paths.sqlite_path) as conn:
        resolution, created = record_review_resolution(
            conn,
            source,
            disposition="keep",
            source_item_kind="audit",
            source_item_id=source["id"],
            decision_payload={"original_receipt": True},
            resolved_at="2026-07-31T12:01:00+00:00",
        )
    assert created is True

    sibling = propose_action(
        paths,
        "fact_upsert",
        action_payload=fact_payload(),
        target_fact_ids=["fact_stale_sibling"],
        proposed_by="legacy_backlog",
        risk_tier="high",
        # Simulate a pre-journal stale card repaired by migration/backfill.
        override_semantic_rejection=True,
    )
    with connection(paths.sqlite_path) as conn:
        evidence = dict(sibling["evidence_json"])
        evidence.pop("semantic_resolution", None)
        conn.execute(
            "UPDATE cos_actions SET status = 'needs_human', evidence_json = ? WHERE id = ?",
            (json.dumps(evidence, sort_keys=True), sibling["id"]),
        )
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, question, status, context, created_at, action_id
            ) VALUES (
              'question_stale_backfill', 'action_review', 'Review stale duplicate?',
              'needs_human', '{}', '2026-07-31T12:02:00+00:00', ?
            )
            """,
            (sibling["id"],),
        )

        same, same_created = record_review_resolution(
            conn,
            source,
            disposition="keep",
            source_item_kind="audit",
            source_item_id=source["id"],
        )
        assert same_created is False
        assert same["id"] == resolution["id"]
        assert (
            conn.execute(
                "SELECT status FROM cos_actions WHERE id = ?", (sibling["id"],)
            ).fetchone()["status"]
            == "needs_human"
        )

        first_counts = backfill_review_resolutions(conn)
        repaired_action = conn.execute(
            "SELECT status, evidence_json FROM cos_actions WHERE id = ?",
            (sibling["id"],),
        ).fetchone()
        repaired_question = conn.execute(
            "SELECT status, answer FROM open_questions WHERE id = ?",
            ("question_stale_backfill",),
        ).fetchone()
        receipt_after_first = conn.execute(
            "SELECT decision_payload FROM review_resolutions WHERE id = ?",
            (resolution["id"],),
        ).fetchone()["decision_payload"]

        second_counts = backfill_review_resolutions(conn)
        receipt_after_second = conn.execute(
            "SELECT decision_payload FROM review_resolutions WHERE id = ?",
            (resolution["id"],),
        ).fetchone()["decision_payload"]

    assert first_counts["audit_decisions"] == 0
    assert second_counts["audit_decisions"] == 0
    assert repaired_action["status"] == "rejected"
    semantic = json.loads(repaired_action["evidence_json"])["semantic_resolution"]
    assert semantic == {
        "resolution_id": resolution["id"],
        "disposition": "keep",
        "resolved_at": "2026-07-31T12:01:00+00:00",
        "outcome": "historical_exact_candidate_closed",
    }
    assert repaired_question["status"] == "auto_resolved"
    assert json.loads(repaired_question["answer"])["resolution_id"] == resolution["id"]
    assert json.loads(receipt_after_first) == {"original_receipt": True}
    assert receipt_after_second == receipt_after_first


def test_resolution_undo_guard_covers_closed_siblings_and_questions(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    source = propose_action(
        paths,
        "fact_upsert",
        action_payload=fact_payload(),
        target_fact_ids=["fact_source"],
        proposed_by="resolution_source",
        risk_tier="high",
    )
    sibling = propose_action(
        paths,
        "fact_upsert",
        action_payload=fact_payload(),
        target_fact_ids=["fact_sibling"],
        proposed_by="resolution_sibling",
        risk_tier="high",
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, question, status, context, created_at, action_id
            ) VALUES (
              'question_guarded_sibling', 'action_review', 'Review the duplicate?',
              'open', '{}', '2026-07-31T12:00:00+00:00', ?
            )
            """,
            (sibling["id"],),
        )
        resolution, _created = record_review_resolution(
            conn,
            source,
            disposition="keep",
            source_item_kind="action",
            source_item_id=source["id"],
        )
    handle = {
        "kind": "action_status",
        "action": {"id": source["id"]},
        "review_resolution_ids": [resolution["id"]],
    }
    seal_undo_handle(paths, handle)
    require_current_undo_handle(paths, handle)

    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE open_questions
            SET status = 'answered', answer = '{"decision":"manual_override"}',
                decided_by = 'human'
            WHERE id = 'question_guarded_sibling'
            """
        )

    with pytest.raises(ReviewUndoError, match="handle is stale"):
        require_current_undo_handle(paths, handle)


@pytest.mark.parametrize(
    ("stale_update", "reason"),
    [
        ({"audit_status": "sampled_ok"}, "audit_not_flagged"),
        ({"status": "reverted"}, "action_no_longer_applied"),
    ],
)
def test_audit_mark_good_refuses_stale_action_state(
    tmp_path: Path,
    stale_update: dict[str, str],
    reason: str,
) -> None:
    paths = initialized_paths(tmp_path)
    action = applied_action(
        paths,
        run_id="run_stale_audit_mark_good",
        action_type="canonicalize_page",
        payload={"page_hint": "concepts/stale-audit.md"},
    )
    record_action_audit(
        paths,
        action["id"],
        "sampled_bad",
        metadata={"rationale": "Review the original applied state."},
    )
    stale_action = get_action(paths, action["id"])
    assignments = ", ".join(f"{column} = ?" for column in stale_update)
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            f"UPDATE cos_actions SET {assignments} WHERE id = ?",
            (*stale_update.values(), action["id"]),
        )
        resolution_count_before = conn.execute(
            "SELECT COUNT(*) FROM review_resolutions"
        ).fetchone()[0]

    with pytest.raises(
        audit_review.AuditReviewDecisionError,
        match=rf"no longer reviewable.*{reason}",
    ):
        audit_review.decide_audit_queue_item(
            paths,
            stale_action,
            "mark_good",
            {},
            previous_action_state=audit_review.action_undo_state(stale_action),
        )

    with connection(paths.sqlite_path) as conn:
        current = conn.execute(
            "SELECT status, audit_status FROM cos_actions WHERE id = ?",
            (action["id"],),
        ).fetchone()
        resolution_count_after = conn.execute(
            "SELECT COUNT(*) FROM review_resolutions"
        ).fetchone()[0]
    assert current["status"] == stale_update.get("status", "applied")
    assert current["audit_status"] == stale_update.get("audit_status", "sampled_bad")
    assert resolution_count_after == resolution_count_before


def test_concurrent_audit_mark_good_commits_one_confirmation_and_keep(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    action = apply_action(
        paths,
        propose_action(
            paths,
            "fact_upsert",
            run_id="run_atomic_audit_mark_good",
            action_payload=fact_payload(),
            action_features={"truth_mutation": False, "reversible": True},
            target_fact_ids=["fact_launch"],
            proposed_by="test",
            risk_tier="low",
        )["id"],
    )
    record_action_audit(
        paths,
        action["id"],
        "sampled_bad",
        metadata={"rationale": "Review the current fact before confirming it."},
    )
    reviewed = get_action(paths, action["id"])
    barrier = threading.Barrier(2)

    def decide() -> dict[str, Any] | Exception:
        barrier.wait()
        try:
            return audit_review.decide_audit_queue_item(
                paths,
                reviewed,
                "mark_good",
                {},
                previous_action_state=audit_review.action_undo_state(reviewed),
            )
        except Exception as exc:  # the losing reviewer must refresh
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: decide(), range(2)))

    successes = [item for item in outcomes if isinstance(item, dict)]
    failures = [item for item in outcomes if isinstance(item, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], audit_review.AuditReviewDecisionError)

    with connection(paths.sqlite_path) as conn:
        audited = conn.execute(
            "SELECT audit_status FROM cos_actions WHERE id = ?", (action["id"],)
        ).fetchone()
        fact = conn.execute(
            "SELECT confirmed_by_user FROM facts WHERE id = 'fact_launch'"
        ).fetchone()
        confirmation_rows = conn.execute(
            "SELECT status FROM cos_actions WHERE proposed_by = 'ui_fact_confirm'"
        ).fetchall()
        active_resolution_count = conn.execute(
            "SELECT COUNT(*) FROM review_resolutions WHERE revoked_at IS NULL"
        ).fetchone()[0]
    assert audited["audit_status"] == "sampled_ok"
    assert bool(fact["confirmed_by_user"]) is True
    assert [row["status"] for row in confirmation_rows] == ["applied"]
    assert active_resolution_count == 1


def test_audit_mark_good_rolls_back_confirmation_when_keep_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = initialized_paths(tmp_path)
    action = apply_action(
        paths,
        propose_action(
            paths,
            "fact_upsert",
            action_payload=fact_payload(),
            action_features={"truth_mutation": False, "reversible": True},
            target_fact_ids=["fact_launch"],
            proposed_by="test",
            risk_tier="low",
        )["id"],
    )
    record_action_audit(paths, action["id"], "sampled_bad")
    reviewed = get_action(paths, action["id"])

    def fail_keep_write(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], bool]:
        raise RuntimeError("simulated keep write failure")

    monkeypatch.setattr(audit_review, "record_review_resolution", fail_keep_write)
    with pytest.raises(RuntimeError, match="simulated keep write failure"):
        audit_review.decide_audit_queue_item(
            paths,
            reviewed,
            "mark_good",
            {},
            previous_action_state=audit_review.action_undo_state(reviewed),
        )

    with connection(paths.sqlite_path) as conn:
        audited = conn.execute(
            "SELECT audit_status FROM cos_actions WHERE id = ?", (action["id"],)
        ).fetchone()
        fact = conn.execute(
            "SELECT confirmed_by_user FROM facts WHERE id = 'fact_launch'"
        ).fetchone()
        confirmations = conn.execute(
            "SELECT COUNT(*) FROM cos_actions WHERE proposed_by = 'ui_fact_confirm'"
        ).fetchone()[0]
    assert audited["audit_status"] == "sampled_bad"
    assert bool(fact["confirmed_by_user"]) is False
    assert confirmations == 0


def test_audit_fact_remediation_refuses_semantically_rejected_correction(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    action = apply_action(
        paths,
        propose_action(
            paths,
            "fact_upsert",
            action_payload=fact_payload(),
            action_features={"truth_mutation": False, "reversible": True},
            target_fact_ids=["fact_launch"],
            proposed_by="test",
            risk_tier="low",
        )["id"],
    )
    record_action_audit(paths, action["id"], "sampled_bad")
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE facts SET page_hint = 'concepts/current-home.md' WHERE id = ?",
            ("fact_launch",),
        )

    correction_update = {
        "fact_id": "fact_launch",
        "status": "rejected",
        "conflict_group_id": None,
    }
    blocker = propose_action(
        paths,
        "fact_supersede",
        action_payload={"updates": [correction_update]},
        target_fact_ids=["fact_launch"],
        target_page_paths=["concepts/current-home.md"],
        proposed_by="prior_human_review",
        risk_tier="medium",
        override_semantic_rejection=True,
    )
    with connection(paths.sqlite_path) as conn:
        blocker_resolution, _ = record_review_resolution(
            conn,
            blocker,
            disposition="reject",
            source_item_kind="action",
            source_item_id=blocker["id"],
        )

    reviewed = get_action(paths, action["id"])
    with pytest.raises(
        audit_review.AuditReviewDecisionError,
        match="remediation was not applied",
    ):
        audit_review.decide_audit_queue_item(
            paths,
            reviewed,
            "revert",
            {},
            previous_action_state=audit_review.action_undo_state(reviewed),
        )

    with connection(paths.sqlite_path) as conn:
        audited = conn.execute(
            "SELECT audit_status FROM cos_actions WHERE id = ?", (action["id"],)
        ).fetchone()
        fact = conn.execute(
            "SELECT status FROM facts WHERE id = 'fact_launch'"
        ).fetchone()
        attempted = conn.execute(
            """
            SELECT status FROM cos_actions
            WHERE proposed_by = 'ui_audit_reject_current_fact'
            """
        ).fetchone()
        audit_resolution_count = conn.execute(
            """
            SELECT COUNT(*) FROM review_resolutions
            WHERE source_item_kind = 'audit' AND source_item_id = ?
            """,
            (action["id"],),
        ).fetchone()[0]
        preflight_review_resolution_revoke(conn, blocker_resolution["id"])
    assert audited["audit_status"] == "sampled_bad"
    assert fact["status"] == "active"
    assert attempted["status"] == "rejected"
    assert audit_resolution_count == 0


def test_confirmed_fact_suppresses_exact_regenerated_audit_action(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, source_ids, source_spans,
              evidence_quote, confidence, status, confirmed_by_user,
              metadata, created_at, last_seen_at, temporal_kind,
              valid_time_precision, event_time_kind, event_start_at,
              event_time_precision
            ) VALUES (
              'fact_launch', 'The launch is scheduled for August 3.',
              'event:launch', '["document:launch"]',
              '[{"chunk_id":"chunk_launch","start":0,"end":45}]',
              'The launch is scheduled for August 3.', 0.94, 'active', 1,
              '{}', '2026-07-30T12:00:00+00:00',
              '2026-07-30T12:00:00+00:00', 'unknown', 'unknown',
              'scheduled_for', '2026-08-03T16:00:00+00:00', 'minute'
            )
            """
        )
    regenerated = applied_action(
        paths,
        run_id="run_regenerated",
        payload=fact_payload(),
        target_fact_ids=["fact_launch"],
    )

    with connection(paths.sqlite_path) as conn:
        assert action_is_manually_resolved(conn, regenerated) is True
        assert load_audit_sample(conn, 25, action_run_id="run_regenerated") == []


def test_confirmed_fact_lookup_chunks_oversized_target_sets(tmp_path: Path) -> None:
    paths = initialized_paths(tmp_path)
    matching_fact_id = "fact_bulk_08399"
    candidate_fact = fact_payload()["fact"]
    candidate_fact["id"] = "fact_bulk_candidate"
    target_fact_ids = [f"fact_bulk_{index:05d}" for index in range(8_400)]

    class BindLimitedConnection:
        def __init__(self, conn: Any) -> None:
            self.conn = conn
            self.fact_lookup_bind_counts: list[int] = []

        def execute(self, sql: str, params: Any = ()) -> Any:
            if "FROM facts" in sql and "WHERE id IN" in sql:
                bind_count = len(params)
                self.fact_lookup_bind_counts.append(bind_count)
                if bind_count > 400:
                    raise AssertionError(f"fact lookup used {bind_count} SQLite binds")
            return self.conn.execute(sql, params)

    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, source_ids, source_spans,
              evidence_quote, confidence, status, confirmed_by_user,
              metadata, created_at, last_seen_at, temporal_kind,
              valid_time_precision, event_time_kind, event_start_at,
              event_time_precision
            ) VALUES (
              ?, 'The launch is scheduled for August 3.',
              'event:launch', '["document:launch"]',
              '[{"chunk_id":"chunk_launch","start":0,"end":45}]',
              'The launch is scheduled for August 3.', 0.94, 'active', 1,
              '{}', '2026-07-30T12:00:00+00:00',
              '2026-07-30T12:00:00+00:00', 'unknown', 'unknown',
              'scheduled_for', '2026-08-03T16:00:00+00:00', 'minute'
            )
            """,
            (matching_fact_id,),
        )
        limited = BindLimitedConnection(conn)
        matching_action = {
            "action_type": "fact_upsert",
            "target_fact_ids": target_fact_ids,
            "evidence_json": {"payload": {"fact": candidate_fact}},
        }
        assert action_targets_confirmed_fact(limited, matching_action) is True

        nonmatching_action = {
            **matching_action,
            "target_fact_ids": [f"fact_missing_{index:05d}" for index in range(8_400)],
        }
        assert action_targets_confirmed_fact(limited, nonmatching_action) is False

    assert len(limited.fact_lookup_bind_counts) > 2
    assert max(limited.fact_lookup_bind_counts) <= 400


def test_fact_confirmation_is_bound_to_evidence_and_temporal_state(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)

    def apply_payload(
        payload: dict[str, Any],
        *,
        run_id: str,
        human_confirmed: bool = False,
    ) -> dict[str, Any]:
        return apply_action(
            paths,
            propose_action(
                paths,
                "fact_upsert",
                run_id=run_id,
                action_payload=payload,
                action_features={"human_confirmed": human_confirmed},
                target_fact_ids=["fact_launch"],
                risk_tier="high",
            )["id"],
        )

    apply_payload(fact_payload(), run_id="run_initial")
    confirmed_payload = fact_payload()
    confirmed_payload["fact"]["confirmed_by_user"] = True
    apply_payload(
        confirmed_payload,
        run_id="run_human_confirmation",
        human_confirmed=True,
    )

    jitter_payload = fact_payload()
    jitter_payload["fact"].update(
        {
            "confidence": 0.42,
            "last_seen_at": "2026-08-01T12:00:00+00:00",
        }
    )
    jitter = apply_payload(jitter_payload, run_id="run_process_jitter")
    with connection(paths.sqlite_path) as conn:
        assert bool(
            conn.execute(
                "SELECT confirmed_by_user FROM facts WHERE id = 'fact_launch'"
            ).fetchone()["confirmed_by_user"]
        )
        assert load_audit_sample(conn, 25, action_run_id="run_process_jitter") == []

    evidence_payload = fact_payload(source_id="document:launch-update")
    changed_evidence = apply_payload(
        evidence_payload,
        run_id="run_changed_evidence_applied",
    )
    with connection(paths.sqlite_path) as conn:
        assert not bool(
            conn.execute(
                "SELECT confirmed_by_user FROM facts WHERE id = 'fact_launch'"
            ).fetchone()["confirmed_by_user"]
        )
        assert [
            action["id"]
            for action in load_audit_sample(
                conn, 25, action_run_id="run_changed_evidence_applied"
            )
        ] == [changed_evidence["id"]]

    human_time_payload = fact_payload(
        source_id="document:launch-update",
        event_start_at="2026-08-04T16:00:00+00:00",
    )
    human_time_payload["fact"]["confirmed_by_user"] = True
    apply_payload(
        human_time_payload,
        run_id="run_human_changed_time",
        human_confirmed=True,
    )
    with connection(paths.sqlite_path) as conn:
        assert bool(
            conn.execute(
                "SELECT confirmed_by_user FROM facts WHERE id = 'fact_launch'"
            ).fetchone()["confirmed_by_user"]
        )

    changed_time = apply_payload(
        fact_payload(
            source_id="document:launch-update",
            event_start_at="2026-08-05T16:00:00+00:00",
        ),
        run_id="run_changed_time_applied",
    )
    with connection(paths.sqlite_path) as conn:
        assert not bool(
            conn.execute(
                "SELECT confirmed_by_user FROM facts WHERE id = 'fact_launch'"
            ).fetchone()["confirmed_by_user"]
        )
        assert [
            action["id"]
            for action in load_audit_sample(
                conn, 25, action_run_id="run_changed_time_applied"
            )
        ] == [changed_time["id"]]
    assert jitter["status"] == "applied"


def test_resolution_is_exact_and_material_evidence_or_event_time_reopens(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    original = applied_action(
        paths,
        run_id="run_original",
        payload=fact_payload(),
        target_fact_ids=["fact_launch"],
    )
    with connection(paths.sqlite_path) as conn:
        _resolution, created = record_review_resolution(
            conn,
            original,
            disposition="keep",
            source_item_kind="audit",
            source_item_id=original["id"],
        )
    assert created is True

    exact = applied_action(
        paths,
        run_id="run_exact",
        payload=fact_payload(),
        target_fact_ids=["fact_launch"],
    )
    changed_evidence = applied_action(
        paths,
        run_id="run_changed_evidence",
        payload=fact_payload(source_id="document:launch-update"),
        target_fact_ids=["fact_launch"],
    )
    changed_time = applied_action(
        paths,
        run_id="run_changed_time",
        payload=fact_payload(event_start_at="2026-08-04T16:00:00+00:00"),
        target_fact_ids=["fact_launch"],
    )

    with connection(paths.sqlite_path) as conn:
        assert action_is_manually_resolved(conn, exact) is True
        assert action_is_manually_resolved(conn, changed_evidence) is False
        assert action_is_manually_resolved(conn, changed_time) is False
        assert load_audit_sample(conn, 25, action_run_id="run_exact") == []
        assert [
            action["id"]
            for action in load_audit_sample(
                conn, 25, action_run_id="run_changed_evidence"
            )
        ] == [changed_evidence["id"]]
        assert [
            action["id"]
            for action in load_audit_sample(conn, 25, action_run_id="run_changed_time")
        ] == [changed_time["id"]]


@pytest.mark.parametrize("reference_field", ["temporal_references", "event_references"])
def test_temporal_reference_set_order_and_duplicates_do_not_reopen_resolution(
    tmp_path: Path,
    reference_field: str,
) -> None:
    paths = initialized_paths(tmp_path)
    scheduled_for = {
        "relation": "scheduled_for",
        "entity_key": "event:launch",
        "start_at": "2026-08-03T16:00:00+00:00",
        "precision": "minute",
    }
    deadline = {
        "relation": "deadline",
        "entity_key": "task:launch-checklist",
        "end_at": "2026-08-02T23:59:00+00:00",
        "precision": "minute",
    }
    original_payload = fact_payload()
    original_payload["fact"]["metadata"] = {reference_field: [scheduled_for, deadline]}
    original = applied_action(
        paths,
        run_id=f"run_{reference_field}_original",
        payload=original_payload,
        target_fact_ids=["fact_launch"],
    )
    with connection(paths.sqlite_path) as conn:
        record_review_resolution(
            conn,
            original,
            disposition="keep",
            source_item_kind="audit",
            source_item_id=original["id"],
        )

    reordered_payload = json.loads(json.dumps(original_payload))
    reordered_payload["fact"]["metadata"][reference_field] = [
        deadline,
        scheduled_for,
        scheduled_for,
    ]
    reordered = applied_action(
        paths,
        run_id=f"run_{reference_field}_reordered",
        payload=reordered_payload,
        target_fact_ids=["fact_launch"],
    )
    changed_payload = json.loads(json.dumps(reordered_payload))
    changed_payload["fact"]["metadata"][reference_field][0]["end_at"] = (
        "2026-08-03T23:59:00+00:00"
    )
    changed = applied_action(
        paths,
        run_id=f"run_{reference_field}_changed",
        payload=changed_payload,
        target_fact_ids=["fact_launch"],
    )

    assert (
        fact_review_identity(original_payload["fact"])["state_fingerprint"]
        == fact_review_identity(reordered_payload["fact"])["state_fingerprint"]
    )
    assert (
        fact_review_identity(original_payload["fact"])["state_fingerprint"]
        != fact_review_identity(changed_payload["fact"])["state_fingerprint"]
    )
    with connection(paths.sqlite_path) as conn:
        assert action_is_manually_resolved(conn, reordered) is True
        assert action_is_manually_resolved(conn, changed) is False
        assert (
            load_audit_sample(
                conn,
                25,
                action_run_id=f"run_{reference_field}_reordered",
            )
            == []
        )
        assert [
            action["id"]
            for action in load_audit_sample(
                conn,
                25,
                action_run_id=f"run_{reference_field}_changed",
            )
        ] == [changed["id"]]


def test_entity_attribution_matches_fact_upsert_normalization(tmp_path: Path) -> None:
    paths = initialized_paths(tmp_path)
    original_payload = attributed_fact_payload()
    original = applied_action(
        paths,
        run_id="run_entity_original",
        payload=original_payload,
        target_fact_ids=["fact_launch"],
    )
    with connection(paths.sqlite_path) as conn:
        record_review_resolution(
            conn,
            original,
            disposition="keep",
            source_item_kind="audit",
            source_item_id=original["id"],
        )

    reordered_payload = json.loads(json.dumps(original_payload))
    mentions = reordered_payload["fact"]["entity_mentions"]
    reordered_payload["fact"]["entity_links"] = [
        {"entity_id": "wholly_ignored", "is_primary": False}
    ]
    reordered_payload["fact"]["entity_mentions"] = [mentions[1], mentions[0]]
    reordered_payload["fact"]["entity_mentions"][0].update(
        {
            "confidence": 0.12,
            "mention_span": {"chunk_id": "jitter", "start": 90, "end": 94},
            # entity_key is ignored when the higher-precedence surface exists.
            "entity_key": "organization:ignored",
        }
    )
    # Top-level entity_mentions shadow the model copy during fact mutation.
    reordered_payload["fact"]["metadata"]["model_entity_mentions"] = [
        {"surface": "Ignored metadata", "is_primary": True}
    ]
    reordered = applied_action(
        paths,
        run_id="run_entity_reordered",
        payload=reordered_payload,
        target_fact_ids=["fact_launch"],
    )

    changed_payloads: list[dict[str, Any]] = []
    changed_primary_id = json.loads(json.dumps(reordered_payload))
    changed_primary_id["fact"]["entity_id"] = "entity_other_launch"
    changed_payloads.append(changed_primary_id)
    changed_entity_key = json.loads(json.dumps(reordered_payload))
    changed_entity_key["fact"]["entity_key"] = "event:other-launch"
    changed_payloads.append(changed_entity_key)
    changed_secondary = json.loads(json.dumps(reordered_payload))
    changed_secondary["fact"]["entity_mentions"][0]["surface"] = "OtherCo"
    changed_payloads.append(changed_secondary)
    changed_type = json.loads(json.dumps(reordered_payload))
    changed_type["fact"]["entity_mentions"][0]["entity_type"] = "person"
    changed_payloads.append(changed_type)
    changed_kind = json.loads(json.dumps(reordered_payload))
    changed_kind["fact"]["entity_mentions"][0]["mention_kind"] = "generic"
    changed_payloads.append(changed_kind)
    changed_primary = json.loads(json.dumps(reordered_payload))
    for mention in changed_primary["fact"]["entity_mentions"]:
        mention["is_primary"] = mention["surface"] == "Acme"
    changed_payloads.append(changed_primary)

    original_identity = fact_review_identity(original_payload["fact"])
    assert original_identity == fact_review_identity(reordered_payload["fact"])
    explicit_id_identity = fact_review_identity(changed_primary_id["fact"])
    assert explicit_id_identity["family_key"] == original_identity["family_key"]
    assert explicit_id_identity["portable_key"] == original_identity["portable_key"]
    assert (
        explicit_id_identity["state_fingerprint"]
        != original_identity["state_fingerprint"]
    )
    changed_key_identity = fact_review_identity(changed_entity_key["fact"])
    assert changed_key_identity["family_key"] == original_identity["family_key"]
    assert changed_key_identity["portable_key"] == original_identity["portable_key"]
    for changed_payload in changed_payloads:
        changed_identity = fact_review_identity(changed_payload["fact"])
        assert (
            original_identity["state_fingerprint"]
            != changed_identity["state_fingerprint"]
        )
        assert original_identity["group_key"] == changed_identity["group_key"]

    implicit_primary = json.loads(json.dumps(original_payload))
    for mention in implicit_primary["fact"]["entity_mentions"]:
        mention.pop("is_primary", None)
    reversed_implicit_primary = json.loads(json.dumps(implicit_primary))
    reversed_implicit_primary["fact"]["entity_mentions"].reverse()
    assert (
        fact_review_identity(implicit_primary["fact"])["state_fingerprint"]
        != fact_review_identity(reversed_implicit_primary["fact"])["state_fingerprint"]
    )

    changed_actions = [
        applied_action(
            paths,
            run_id=f"run_entity_changed_{index}",
            payload=payload,
            target_fact_ids=["fact_launch"],
        )
        for index, payload in enumerate(changed_payloads)
    ]
    with connection(paths.sqlite_path) as conn:
        assert action_is_manually_resolved(conn, reordered) is True
        assert all(
            not action_is_manually_resolved(conn, action) for action in changed_actions
        )


def test_entity_fallbacks_types_and_mention_kinds_use_mutation_semantics() -> None:
    def fallback_fact(field: str) -> dict[str, Any]:
        fact = fact_payload()["fact"]
        fact["entity_type"] = "company"
        if field == "model_entity_key":
            fact["metadata"] = {"model_entity_key": "Acme_Corp"}
        else:
            fact[field] = "Acme Corp" if field == "entity_mention" else "ACME-CORP"
        return fact

    fallback_identities = [
        fact_review_identity(fallback_fact(field))
        for field in ("model_entity_key", "entity_mention", "entity_name")
    ]
    assert fallback_identities[1:] == fallback_identities[:-1]

    structured = fallback_fact("model_entity_key")
    structured["entity_mentions"] = [
        {
            "surface": "ACME corp",
            "type": "org",
            "kind": "proper name",
        }
    ]
    # Shadowed fallbacks and model mentions do not affect mutation semantics.
    structured["entity_mention"] = "Ignored fallback"
    structured["entity_name"] = "Also ignored"
    structured["metadata"]["model_entity_mentions"] = [
        {"surface": "Ignored model mention", "is_primary": True}
    ]
    assert fact_review_identity(structured) == fallback_identities[0]

    changed_type = json.loads(json.dumps(structured))
    changed_type["entity_mentions"][0]["type"] = "person"
    changed_kind = json.loads(json.dumps(structured))
    changed_kind["entity_mentions"][0]["kind"] = "generic"
    assert (
        fact_review_identity(changed_type)["state_fingerprint"]
        != (fact_review_identity(structured)["state_fingerprint"])
    )
    assert (
        fact_review_identity(changed_kind)["state_fingerprint"]
        != (fact_review_identity(structured)["state_fingerprint"])
    )


def test_apply_persist_confirm_regenerate_entity_attribution(tmp_path: Path) -> None:
    paths = initialized_paths(tmp_path)
    initial_payload = attributed_fact_payload()
    fact = initial_payload["fact"]
    fact.update(
        {
            "statement": "Launch is partnering with Acme.",
            "entity_key": "project:launch",
            "evidence_quote": "Launch is partnering with Acme.",
            "confirmed_by_user": True,
        }
    )
    # Exercise the lossy legacy shape directly: top-level mentions with no
    # redundant model_entity_mentions copy in metadata.
    fact.pop("metadata", None)
    for key in ("event_time_kind", "event_start_at", "event_time_precision"):
        fact.pop(key, None)
    initial = propose_action(
        paths,
        "fact_upsert",
        run_id="run_entity_initial_apply",
        action_payload=initial_payload,
        action_features={"human_confirmed": True},
        target_fact_ids=["fact_launch"],
        risk_tier="high",
    )
    initial = apply_action(
        paths,
        initial["id"],
        allow_llm_entity_resolution=False,
    )
    assert initial["status"] == "applied"

    regenerated_payload = json.loads(json.dumps(initial_payload))
    regenerated_payload["fact"].pop("confirmed_by_user", None)
    regenerated_payload["fact"]["entity_mentions"].reverse()
    regenerated_payload["fact"]["entity_links"] = [
        {"entity_id": "ignored_regenerated_link", "is_primary": True}
    ]
    regenerated = propose_action(
        paths,
        "fact_upsert",
        run_id="run_entity_regenerated",
        action_payload=regenerated_payload,
        target_fact_ids=["fact_launch"],
        risk_tier="high",
    )
    regenerated = apply_action(
        paths,
        regenerated["id"],
        allow_llm_entity_resolution=False,
    )

    with connection(paths.sqlite_path) as conn:
        persisted = conn.execute(
            """
            SELECT entity_id, confirmed_by_user, metadata
            FROM facts WHERE id = 'fact_launch'
            """
        ).fetchone()
        links = conn.execute(
            """
            SELECT is_primary, mention_text, mention_kind
            FROM fact_entities WHERE fact_id = 'fact_launch'
            ORDER BY is_primary DESC, mention_text
            """
        ).fetchall()
        assert persisted["entity_id"]
        assert bool(persisted["confirmed_by_user"])
        snapshot = json.loads(persisted["metadata"])[
            FACT_ENTITY_ATTRIBUTION_SNAPSHOT_KEY
        ]
        assert snapshot == {
            "version": 1,
            "entity_id": persisted["entity_id"],
            "entity_id_origin": "derived",
            "mentions": [
                {
                    "surface": "Launch",
                    "entity_identity": "Launch (2026-08-03)",
                    "entity_type": "event",
                    "mention_kind": "named",
                    "is_primary": True,
                },
                {
                    "surface": "Acme",
                    "entity_type": "organization",
                    "mention_kind": "named",
                    "is_primary": False,
                },
            ],
        }
        assert [(row["mention_text"], row["mention_kind"]) for row in links] == [
            ("Launch", "named"),
            ("Acme", "named"),
        ]
        assert [bool(row["is_primary"]) for row in links] == [True, False]
        assert action_targets_confirmed_fact(conn, regenerated) is True
        assert (
            load_audit_sample(
                conn,
                25,
                action_run_id="run_entity_regenerated",
            )
            == []
        )

        explicit_wrong_id = json.loads(json.dumps(regenerated))
        explicit_wrong_id["evidence_json"]["payload"]["fact"]["entity_id"] = (
            "entity_other_launch"
        )
        assert action_targets_confirmed_fact(conn, explicit_wrong_id) is False

    changed_payload = json.loads(json.dumps(regenerated_payload))
    for key in ("entity_mentions",):
        for mention in changed_payload["fact"][key]:
            if not mention.get("is_primary"):
                mention["surface"] = "OtherCo"
    changed = propose_action(
        paths,
        "fact_upsert",
        run_id="run_entity_changed_attribution",
        action_payload=changed_payload,
        target_fact_ids=["fact_launch"],
        risk_tier="high",
    )
    changed = apply_action(
        paths,
        changed["id"],
        allow_llm_entity_resolution=False,
    )

    with connection(paths.sqlite_path) as conn:
        persisted = conn.execute(
            "SELECT confirmed_by_user FROM facts WHERE id = 'fact_launch'"
        ).fetchone()
        assert not bool(persisted["confirmed_by_user"])
        assert action_targets_confirmed_fact(conn, changed) is False
        assert [
            action["id"]
            for action in load_audit_sample(
                conn,
                25,
                action_run_id="run_entity_changed_attribution",
            )
        ] == [changed["id"]]


def test_confirmed_derived_entity_id_suppresses_new_fact_id_but_explicit_id_reopens(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    payload = attributed_fact_payload()
    payload["fact"].pop("metadata", None)
    original = propose_action(
        paths,
        "fact_upsert",
        action_payload=payload,
        target_fact_ids=["fact_launch"],
        risk_tier="high",
    )
    apply_action(paths, original["id"], allow_llm_entity_resolution=False)
    confirmed = ui_confirm_fact(paths, "fact_launch")["fact"]

    regenerated_fact = json.loads(json.dumps(confirmed))
    regenerated_fact["id"] = "fact_launch_regenerated"
    regenerated_fact["confirmed_by_user"] = False
    regenerated = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": regenerated_fact},
        target_fact_ids=["fact_launch_regenerated"],
        risk_tier="high",
    )
    assert regenerated["status"] == "rejected"
    assert regenerated["evidence_json"]["semantic_resolution"]["disposition"] == (
        "keep"
    )

    wrong_entity = json.loads(json.dumps(regenerated_fact))
    wrong_entity["entity_id"] = "entity_explicitly_wrong"
    changed = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": wrong_entity},
        target_fact_ids=["fact_launch_wrong_entity"],
        risk_tier="high",
    )
    assert changed["status"] == "proposed"


def test_confirmed_fact_bridge_requires_matching_derived_id_receipt() -> None:
    candidate = fact_payload()["fact"]

    def routed(origin: str, *, receipt_id: str = "entity_launch") -> dict[str, Any]:
        return {
            **candidate,
            "entity_id": "entity_launch",
            "metadata": {
                FACT_ENTITY_ATTRIBUTION_SNAPSHOT_KEY: {
                    "version": 1,
                    "mentions": [],
                    "entity_id": receipt_id,
                    "entity_id_origin": origin,
                }
            },
        }

    assert confirmed_fact_review_state_matches(candidate, routed("derived")) is True
    assert confirmed_fact_review_state_matches(candidate, routed("explicit")) is False
    assert confirmed_fact_review_state_matches(candidate, routed("unknown")) is False
    assert (
        confirmed_fact_review_state_matches(
            candidate, routed("derived", receipt_id="entity_other")
        )
        is False
    )
    missing_key = {**candidate, "entity_key": None}
    assert confirmed_fact_review_state_matches(missing_key, candidate) is False


def test_fact_upsert_rejects_unknown_explicit_entity_id(tmp_path: Path) -> None:
    paths = initialized_paths(tmp_path)
    payload = attributed_fact_payload()
    payload["fact"]["entity_id"] = "entity_missing_explicit"
    action = propose_action(
        paths,
        "fact_upsert",
        action_payload=payload,
        target_fact_ids=["fact_launch"],
        risk_tier="high",
    )

    with pytest.raises(ValueError, match="unknown explicit entity_id"):
        apply_action(paths, action["id"], allow_llm_entity_resolution=False)

    with connection(paths.sqlite_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM facts WHERE id = 'fact_launch'"
            ).fetchone()[0]
            == 0
        )


def test_unrelated_metadata_update_preserves_entity_attribution_snapshot(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    initial_payload = attributed_fact_payload()
    fact = initial_payload["fact"]
    fact.update(
        {
            "statement": "Launch is partnering with Acme.",
            "entity_key": "project:launch",
            "evidence_quote": "Launch is partnering with Acme.",
            "confirmed_by_user": True,
        }
    )
    fact.pop("metadata", None)
    for key in ("event_time_kind", "event_start_at", "event_time_precision"):
        fact.pop(key, None)
    initial = propose_action(
        paths,
        "fact_upsert",
        action_payload=initial_payload,
        action_features={"human_confirmed": True},
        target_fact_ids=["fact_launch"],
        risk_tier="high",
    )
    apply_action(paths, initial["id"], allow_llm_entity_resolution=False)

    metadata_update = json.loads(json.dumps(initial_payload))
    metadata_update["fact"].pop("entity_mentions", None)
    metadata_update["fact"].pop("confirmed_by_user", None)
    metadata_update["fact"]["metadata"] = {"unrelated": "new value"}
    update = propose_action(
        paths,
        "fact_upsert",
        action_payload=metadata_update,
        target_fact_ids=["fact_launch"],
        risk_tier="high",
    )
    apply_action(paths, update["id"], allow_llm_entity_resolution=False)

    with connection(paths.sqlite_path) as conn:
        persisted = conn.execute(
            "SELECT metadata, confirmed_by_user FROM facts WHERE id = 'fact_launch'"
        ).fetchone()
        metadata = json.loads(persisted["metadata"])
        links = conn.execute(
            """
            SELECT is_primary, mention_text, mention_kind
            FROM fact_entities WHERE fact_id = 'fact_launch'
            ORDER BY is_primary DESC, mention_text
            """
        ).fetchall()
    assert metadata["unrelated"] == "new value"
    assert len(metadata[FACT_ENTITY_ATTRIBUTION_SNAPSHOT_KEY]["mentions"]) == 2
    assert bool(persisted["confirmed_by_user"])
    assert [
        (bool(row["is_primary"]), row["mention_text"], row["mention_kind"])
        for row in links
    ] == [(True, "Launch", "named"), (False, "Acme", "named")]

    clear_update = json.loads(json.dumps(metadata_update))
    clear_update["fact"]["entity_mentions"] = []
    clear_update["fact"]["metadata"]["model_entity_key"] = "Stale Launch"
    clear_update["fact"]["metadata"]["model_entity_mentions"] = [
        {
            "surface": "Stale Launch",
            "entity_type": "event",
            "mention_kind": "named",
            "is_primary": True,
        }
    ]
    clear = propose_action(
        paths,
        "fact_upsert",
        action_payload=clear_update,
        target_fact_ids=["fact_launch"],
        risk_tier="high",
    )
    clear = apply_action(paths, clear["id"], allow_llm_entity_resolution=False)
    assert clear["status"] == "applied"
    with connection(paths.sqlite_path) as conn:
        cleared = conn.execute(
            "SELECT * FROM facts WHERE id = 'fact_launch'"
        ).fetchone()
        cleared_metadata = json.loads(cleared["metadata"])
        cleared_links = conn.execute(
            "SELECT entity_id FROM fact_entities WHERE fact_id = 'fact_launch'"
        ).fetchall()
    assert cleared["entity_id"] is None
    assert cleared_links == []
    assert cleared_metadata[FACT_ENTITY_ATTRIBUTION_SNAPSHOT_KEY]["mentions"] == []
    assert "entity_id" not in cleared_metadata[FACT_ENTITY_ATTRIBUTION_SNAPSHOT_KEY]
    assert cleared_metadata["model_entity_mentions"] == []
    assert fact_review_identity(clear_update["fact"]) == fact_review_identity(
        dict(cleared)
    )
    assert (
        fact_review_identity(clear_update["fact"])["state_fingerprint"]
        != fact_review_identity(initial_payload["fact"])["state_fingerprint"]
    )


@pytest.mark.parametrize("clear_shape", ["top_level", "model_metadata"])
def test_empty_attribution_inputs_clear_inherited_entity_route(
    tmp_path: Path,
    clear_shape: str,
) -> None:
    paths = initialized_paths(tmp_path)
    initial_payload = attributed_fact_payload()
    initial = propose_action(
        paths,
        "fact_upsert",
        action_payload=initial_payload,
        target_fact_ids=["fact_launch"],
        risk_tier="high",
    )
    apply_action(paths, initial["id"], allow_llm_entity_resolution=False)

    clear_payload = json.loads(json.dumps(initial_payload))
    if clear_shape == "top_level":
        clear_payload["fact"]["entity_mentions"] = []
        # The authoritative top-level input must shadow stale model output.
        clear_payload["fact"]["metadata"]["model_entity_mentions"] = [
            {"surface": "Stale Launch", "is_primary": True}
        ]
    else:
        clear_payload["fact"].pop("entity_mentions")
        clear_payload["fact"]["metadata"]["model_entity_mentions"] = []
    clear = propose_action(
        paths,
        "fact_upsert",
        action_payload=clear_payload,
        target_fact_ids=["fact_launch"],
        risk_tier="high",
    )
    apply_action(paths, clear["id"], allow_llm_entity_resolution=False)

    with connection(paths.sqlite_path) as conn:
        persisted = conn.execute(
            "SELECT * FROM facts WHERE id = 'fact_launch'"
        ).fetchone()
        links = conn.execute(
            "SELECT entity_id FROM fact_entities WHERE fact_id = 'fact_launch'"
        ).fetchall()
    snapshot = json.loads(persisted["metadata"])[FACT_ENTITY_ATTRIBUTION_SNAPSHOT_KEY]
    assert persisted["entity_id"] is None
    assert links == []
    assert snapshot == {
        "version": 1,
        "mentions": [],
        "entity_id_origin": "derived",
    }
    assert fact_review_identity(clear_payload["fact"]) == fact_review_identity(
        dict(persisted)
    )


@pytest.mark.parametrize("clear_shape", ["top_level", "model_metadata"])
def test_empty_attribution_retains_authored_entity_id(
    tmp_path: Path,
    clear_shape: str,
) -> None:
    paths = initialized_paths(tmp_path)
    initial_payload = attributed_fact_payload()
    initial = propose_action(
        paths,
        "fact_upsert",
        action_payload=initial_payload,
        target_fact_ids=["fact_launch"],
        risk_tier="high",
    )
    apply_action(paths, initial["id"], allow_llm_entity_resolution=False)
    with connection(paths.sqlite_path) as conn:
        entity_id = conn.execute(
            "SELECT entity_id FROM facts WHERE id = 'fact_launch'"
        ).fetchone()["entity_id"]

    update_payload = json.loads(json.dumps(initial_payload))
    if clear_shape == "top_level":
        update_payload["fact"]["entity_mentions"] = []
    else:
        update_payload["fact"].pop("entity_mentions")
        update_payload["fact"]["metadata"]["model_entity_mentions"] = []
    update_payload["fact"]["entity_id"] = entity_id
    update = propose_action(
        paths,
        "fact_upsert",
        action_payload=update_payload,
        target_fact_ids=["fact_launch"],
        risk_tier="high",
    )
    apply_action(paths, update["id"], allow_llm_entity_resolution=False)

    with connection(paths.sqlite_path) as conn:
        persisted = conn.execute(
            "SELECT * FROM facts WHERE id = 'fact_launch'"
        ).fetchone()
        links = conn.execute(
            """
            SELECT entity_id, is_primary
            FROM fact_entities WHERE fact_id = 'fact_launch'
            """
        ).fetchall()
    snapshot = json.loads(persisted["metadata"])[FACT_ENTITY_ATTRIBUTION_SNAPSHOT_KEY]
    assert persisted["entity_id"] == entity_id
    assert [(row["entity_id"], bool(row["is_primary"])) for row in links] == [
        (entity_id, True)
    ]
    assert snapshot == {
        "version": 1,
        "mentions": [],
        "entity_id_origin": "explicit",
        "entity_id": entity_id,
    }
    assert fact_review_identity(update_payload["fact"]) == fact_review_identity(
        dict(persisted)
    )


def test_legacy_span_changes_reopen_but_confidence_jitter_does_not(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    original_payload = fact_payload()
    original_payload["fact"]["source_spans"] = [
        {
            "source_id": "document:launch",
            "start_char": 4,
            "end_char": 49,
            "quote": "The launch is scheduled for August 3.",
        }
    ]
    original_payload["fact"]["temporal_confidence"] = 0.91
    original = applied_action(
        paths,
        run_id="run_legacy_span",
        payload=original_payload,
        target_fact_ids=["fact_launch"],
    )
    with connection(paths.sqlite_path) as conn:
        record_review_resolution(
            conn,
            original,
            disposition="keep",
            source_item_kind="audit",
            source_item_id=original["id"],
        )

    jitter_payload = json.loads(json.dumps(original_payload))
    jitter_payload["fact"]["temporal_confidence"] = 0.42
    changed_span_payload = json.loads(json.dumps(original_payload))
    changed_span_payload["fact"]["source_spans"][0]["start_char"] = 8
    jitter = applied_action(
        paths,
        run_id="run_confidence_jitter",
        payload=jitter_payload,
        target_fact_ids=["fact_launch"],
    )
    changed_span = applied_action(
        paths,
        run_id="run_changed_span",
        payload=changed_span_payload,
        target_fact_ids=["fact_launch"],
    )

    with connection(paths.sqlite_path) as conn:
        assert action_is_manually_resolved(conn, jitter) is True
        assert action_is_manually_resolved(conn, changed_span) is False


def test_rejected_state_is_blocked_before_reapplication(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    original = applied_action(
        paths,
        run_id="run_rejected_original",
        payload=fact_payload(),
        target_fact_ids=["fact_launch"],
    )
    with connection(paths.sqlite_path) as conn:
        record_review_resolution(
            conn,
            original,
            disposition="reject",
            source_item_kind="audit",
            source_item_id=original["id"],
        )

    regenerated = propose_action(
        paths,
        "fact_upsert",
        run_id="run_regenerated_reject",
        action_payload=fact_payload(),
        target_fact_ids=["fact_launch"],
        risk_tier="high",
        decide=True,
    )

    assert regenerated["status"] == "rejected"
    assert regenerated["evidence_json"]["semantic_resolution"]["disposition"] == (
        "reject"
    )


@pytest.mark.parametrize("stale_status", ["failed", "reverted", "dismissed"])
def test_rejected_state_blocks_every_non_applied_status(
    tmp_path: Path, stale_status: str
) -> None:
    paths = initialized_paths(tmp_path)
    original = applied_action(
        paths,
        run_id="run_reject_boundary_original",
        payload=fact_payload(),
        target_fact_ids=["fact_launch"],
    )
    with connection(paths.sqlite_path) as conn:
        record_review_resolution(
            conn,
            original,
            disposition="reject",
            source_item_kind="audit",
            source_item_id=original["id"],
        )
    candidate = propose_action(
        paths,
        "fact_upsert",
        run_id=f"run_reject_boundary_{stale_status}",
        action_payload=fact_payload(),
        target_fact_ids=["fact_launch"],
        override_semantic_rejection=True,
    )
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE cos_actions SET status = ? WHERE id = ?",
            (stale_status, candidate["id"]),
        )

    blocked = apply_action(paths, candidate["id"])

    assert blocked["status"] == "rejected"
    assert blocked["evidence_json"]["semantic_resolution"]["disposition"] == "reject"


def test_exact_keep_closes_future_duplicate_instead_of_resurfacing(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    payload = {
        "contract": {
            "id": "contract_keep",
            "page_hint": "projects/keep.md",
            "page_type": "project",
            "canonical_entity_key": "project:keep",
            "route_policy": {},
            "section_schema": [],
            "write_policy": {},
            "status": "active",
        }
    }
    original = propose_action(
        paths,
        "edit_contract",
        action_payload=payload,
        target_contract_ids=["contract_keep"],
        override_semantic_rejection=True,
    )
    original = apply_action(paths, original["id"])
    with connection(paths.sqlite_path) as conn:
        record_review_resolution(
            conn,
            original,
            disposition="keep",
            source_item_kind="action",
            source_item_id=original["id"],
        )

    duplicate = propose_action(
        paths,
        "edit_contract",
        action_payload=payload,
        target_contract_ids=["contract_keep"],
        risk_tier="high",
        decide=True,
    )

    assert duplicate["status"] == "rejected"
    assert duplicate["evidence_json"]["semantic_resolution"]["disposition"] == "keep"
    assert (
        duplicate["evidence_json"]["semantic_resolution"]["outcome"]
        == "exact_semantic_state_already_kept"
    )


def test_question_replacement_variants_record_semantic_candidate_state(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    decisions = [
        ("route", "keep"),
        ("new_page", "keep"),
        ("supports", "reject"),
        ("supports_existing", "reject"),
        ("merge_evidence", "reject"),
        ("temporal_update", "reject"),
        ("updates", "reject"),
        ("current_state", "reject"),
    ]
    for index, (decision, expected_disposition) in enumerate(decisions):
        payload = fact_payload(source_id=f"document:question-{index}")
        payload["fact"]["statement"] = f"Question replacement candidate {index}."
        payload["fact"]["evidence_quote"] = payload["fact"]["statement"]
        action = applied_action(
            paths,
            run_id=f"run_question_{index}",
            payload=payload,
            target_fact_ids=[f"fact_question_{index}"],
        )
        undo: dict[str, Any] = {}

        record_question_review_resolution(
            paths,
            {"id": f"question_{index}", "action_id": action["id"]},
            decision,
            undo,
        )

        with connection(paths.sqlite_path) as conn:
            resolution = active_resolution_for_action(conn, action)
        assert resolution is not None
        assert resolution["disposition"] == expected_disposition
        assert undo["review_resolution_ids"] == [resolution["id"]]


def test_alternative_question_records_each_fact_state_and_undo_revokes_all(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    selected = {
        "id": "fact_selected",
        "statement": "The selected alternative is current.",
        "entity_key": "concepts:alternatives:summary",
        "page_hint": "concepts/alternatives.md",
        "source_ids": ["document:selected"],
        "source_spans": [{"source_id": "document:selected", "start": 0, "end": 36}],
        "evidence_quote": "The selected alternative is current.",
        "confidence": 0.9,
        "status": "active",
        "metadata": {},
    }
    duplicate = {**selected, "id": "fact_selected_duplicate"}
    losing = {
        **selected,
        "id": "fact_losing",
        "statement": "The losing alternative is current.",
        "source_ids": ["document:losing"],
        "source_spans": [{"source_id": "document:losing", "start": 0, "end": 34}],
        "evidence_quote": "The losing alternative is current.",
    }
    for index, fact in enumerate((selected, duplicate, losing)):
        apply_action(
            paths,
            propose_action(
                paths,
                "fact_upsert",
                action_payload={"fact": fact},
                action_features={"human_confirmed": True},
                target_fact_ids=[fact["id"]],
                target_page_paths=[fact["page_hint"]],
                proposed_by=f"test_alternative_{index}",
                risk_tier="low",
                override_semantic_rejection=True,
            )["id"],
            override_semantic_rejection=True,
        )
    result_action = applied_action(
        paths,
        run_id="run_alternative_result",
        action_type="resolve_conflict",
        payload={"updates": [], "question_id": "question_alternatives"},
        target_fact_ids=[fact["id"] for fact in (selected, duplicate, losing)],
    )
    fact_ids = [fact["id"] for fact in (selected, duplicate, losing)]
    original_question = {
        "id": "question_alternatives",
        "kind": "conflict",
        "fact_ids": fact_ids,
        "options": [
            {"fact_id": fact["id"], "statement": fact["statement"]}
            for fact in (selected, duplicate, losing)
        ],
        "action_id": None,
    }
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, fact_ids, question, options, status, answer, context,
              created_at, answered_at, action_id, decided_by
            ) VALUES (
              'question_alternatives', 'conflict', ?, 'What is true?', ?,
              'answered', ?, '{}', '2026-07-31T12:00:00+00:00',
              '2026-07-31T12:01:00+00:00', ?, 'human'
            )
            """,
            (
                json.dumps(fact_ids),
                json.dumps(original_question["options"]),
                json.dumps(
                    {
                        "selected_fact_ids": [selected["id"]],
                        "selected_fact_id": selected["id"],
                        "superseded_fact_ids": [duplicate["id"], losing["id"]],
                        "answer": "",
                    },
                    sort_keys=True,
                ),
                result_action["id"],
            ),
        )

    undo: dict[str, Any] = {}
    record_question_review_resolution(
        paths,
        original_question,
        "select_fact",
        undo,
    )

    assert len(undo["review_resolution_ids"]) == 2
    selected_candidate = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": duplicate},
        target_fact_ids=[duplicate["id"]],
        risk_tier="high",
    )
    losing_candidate = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": losing},
        target_fact_ids=[losing["id"]],
        risk_tier="high",
    )
    assert selected_candidate["status"] == "rejected"
    assert (
        selected_candidate["evidence_json"]["semantic_resolution"]["disposition"]
        == "keep"
    )
    assert losing_candidate["status"] == "rejected"
    assert (
        losing_candidate["evidence_json"]["semantic_resolution"]["disposition"]
        == "reject"
    )
    with connection(paths.sqlite_path) as conn:
        assert active_resolution_for_action(conn, result_action) is None
    for resolution_id in undo["review_resolution_ids"]:
        revoke_queue_review_resolution(paths, resolution_id)
    with connection(paths.sqlite_path) as conn:
        assert active_resolution_for_action(conn, selected_candidate) is None
        assert active_resolution_for_action(conn, losing_candidate) is None


def test_free_text_question_rejects_originals_and_keeps_manual_fact(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    question_id = "question_free_text"
    originals = [
        {
            "id": f"fact_original_{index}",
            "statement": f"Original alternative {index}.",
            "entity_key": "concepts:free-text:summary",
            "page_hint": "concepts/free-text.md",
            "source_ids": [f"document:original-{index}"],
            "source_spans": [
                {
                    "source_id": f"document:original-{index}",
                    "start": 0,
                    "end": 23,
                }
            ],
            "evidence_quote": f"Original alternative {index}.",
            "confidence": 0.8,
            "status": "active",
            "metadata": {},
        }
        for index in range(2)
    ]
    manual = {
        "id": "fact_manual_answer",
        "statement": "The human supplied a replacement truth.",
        "entity_key": "concepts:free-text:summary",
        "page_hint": "concepts/free-text.md",
        "source_ids": [f"manual:question:{question_id}"],
        "source_spans": [],
        "evidence_quote": "The human supplied a replacement truth.",
        "confidence": 1.0,
        "status": "active",
        "confirmed_by_user": True,
        "metadata": {
            "question_id": question_id,
            "answer": "The human supplied a replacement truth.",
        },
    }
    for fact in [*originals, manual]:
        apply_action(
            paths,
            propose_action(
                paths,
                "fact_upsert",
                action_payload={"fact": fact},
                action_features={"human_confirmed": fact is manual},
                target_fact_ids=[fact["id"]],
                target_page_paths=[fact["page_hint"]],
                risk_tier="low",
                override_semantic_rejection=True,
            )["id"],
            override_semantic_rejection=True,
        )
    result_action = applied_action(
        paths,
        run_id="run_free_text_result",
        action_type="fact_supersede",
        payload={
            "updates": [
                {"fact_id": fact["id"], "status": "retracted"} for fact in originals
            ],
            "question_id": question_id,
        },
        target_fact_ids=[fact["id"] for fact in originals],
    )
    original_question = {
        "id": question_id,
        "kind": "conflict",
        "fact_ids": [fact["id"] for fact in originals],
        "options": [
            {"fact_id": fact["id"], "statement": fact["statement"]}
            for fact in originals
        ],
        "action_id": None,
    }
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, fact_ids, question, options, status, answer, context,
              created_at, answered_at, action_id, decided_by
            ) VALUES (?, 'conflict', ?, 'What is true?', ?, 'answered', ?, '{}',
                      '2026-07-31T12:00:00+00:00',
                      '2026-07-31T12:01:00+00:00', ?, NULL)
            """,
            (
                question_id,
                json.dumps(original_question["fact_ids"]),
                json.dumps(original_question["options"]),
                json.dumps(
                    {
                        "selected_fact_id": manual["id"],
                        "answer": manual["statement"],
                    },
                    sort_keys=True,
                ),
                result_action["id"],
            ),
        )

    undo: dict[str, Any] = {}
    record_question_review_resolution(paths, original_question, "answer", undo)

    assert len(undo["review_resolution_ids"]) == 3
    for original in originals:
        regenerated = propose_action(
            paths,
            "fact_upsert",
            action_payload={"fact": original},
            target_fact_ids=[original["id"]],
            risk_tier="high",
        )
        assert (
            regenerated["evidence_json"]["semantic_resolution"]["disposition"]
            == "reject"
        )
    regenerated_manual = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": {**manual, "id": "fact_manual_regenerated"}},
        target_fact_ids=["fact_manual_regenerated"],
        risk_tier="high",
    )
    assert (
        regenerated_manual["evidence_json"]["semantic_resolution"]["disposition"]
        == "keep"
    )


def test_free_text_candidate_question_rejects_candidate_and_existing_fact(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    question_id = "question_asymmetric_manual_answer"
    candidate = fact_payload(source_id="document:asymmetric-candidate")["fact"]
    candidate.update(
        {
            "id": "fact_asymmetric_candidate",
            "statement": "The candidate says the workflow is still pending.",
            "evidence_quote": "The candidate says the workflow is still pending.",
        }
    )
    existing = json.loads(json.dumps(candidate))
    existing.update(
        {
            "id": "fact_asymmetric_existing",
            "statement": "The existing fact says the workflow is complete.",
            "source_ids": ["document:asymmetric-existing"],
            "evidence_quote": "The existing fact says the workflow is complete.",
        }
    )
    manual = json.loads(json.dumps(candidate))
    manual.update(
        {
            "id": "fact_asymmetric_manual",
            "statement": "The human supplied the corrected workflow state.",
            "source_ids": [f"manual:question:{question_id}"],
            "source_spans": [],
            "evidence_quote": "",
            "confirmed_by_user": True,
            "metadata": {
                "question_id": question_id,
                "answer": "The human supplied the corrected workflow state.",
            },
        }
    )
    for fact in (existing, manual):
        apply_action(
            paths,
            propose_action(
                paths,
                "fact_upsert",
                action_payload={"fact": fact},
                action_features={"human_confirmed": fact is manual},
                target_fact_ids=[fact["id"]],
                risk_tier="low",
                override_semantic_rejection=True,
            )["id"],
            override_semantic_rejection=True,
        )
    candidate_action = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": candidate},
        target_fact_ids=[candidate["id"]],
        risk_tier="high",
        override_semantic_rejection=True,
    )
    original_question = {
        "id": question_id,
        "kind": "fact_conflict_review",
        # Candidate-review questions asymmetrically store only existing facts here.
        "fact_ids": [existing["id"]],
        "options": [
            {
                "option_type": "candidate_fact",
                "action_id": candidate_action["id"],
                **candidate,
            },
            {"option_type": "existing_fact", **existing},
        ],
        "context": {"candidate_action_id": candidate_action["id"]},
        "action_id": candidate_action["id"],
    }
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, fact_ids, question, options, status, answer, context,
              created_at, answered_at, action_id, decided_by
            ) VALUES (?, 'fact_conflict_review', ?, 'What is true?', ?,
                      'answered', ?, ?, '2026-07-31T12:00:00+00:00',
                      '2026-07-31T12:01:00+00:00', ?, 'human')
            """,
            (
                question_id,
                json.dumps(original_question["fact_ids"]),
                json.dumps(original_question["options"], sort_keys=True),
                json.dumps(
                    {
                        "decision": "manual_answer",
                        "selected_fact_id": manual["id"],
                        "answer": manual["statement"],
                    },
                    sort_keys=True,
                ),
                json.dumps(original_question["context"], sort_keys=True),
                candidate_action["id"],
            ),
        )

    undo: dict[str, Any] = {}
    record_question_review_resolution(paths, original_question, "manual_answer", undo)

    assert len(undo["review_resolution_ids"]) == 3
    with connection(paths.sqlite_path) as conn:
        candidate_resolution = active_resolution_for_action(conn, candidate_action)
    assert candidate_resolution is not None
    assert candidate_resolution["disposition"] == "reject"
    for fact, expected in ((existing, "reject"), (manual, "keep")):
        regenerated = propose_action(
            paths,
            "fact_upsert",
            action_payload={"fact": {**fact, "id": f"{fact['id']}_regenerated"}},
            target_fact_ids=[f"{fact['id']}_regenerated"],
            risk_tier="high",
        )
        assert (
            regenerated["evidence_json"]["semantic_resolution"]["disposition"]
            == expected
        )


def test_actionless_routed_candidate_records_semantic_keep_resolution(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    candidate = {
        "id": "fact_unrouted",
        "statement": "The actionless candidate needs a durable route.",
        "entity_key": "concepts:actionless:summary",
        "page_hint": "",
        "source_ids": ["document:actionless"],
        "source_spans": [{"source_id": "document:actionless", "start": 0, "end": 47}],
        "evidence_quote": "The actionless candidate needs a durable route.",
        "confidence": 0.91,
        "status": "active",
        "metadata": {},
    }
    replacement = {**candidate, "page_hint": "concepts/actionless.md"}
    result_action = applied_action(
        paths,
        run_id="run_actionless_route",
        payload={"fact": replacement},
        target_fact_ids=[candidate["id"]],
        target_page_paths=[replacement["page_hint"]],
    )
    original_question = {
        "id": "question_actionless",
        "kind": "unrouted_fact",
        "fact_ids": [candidate["id"]],
        "options": [{"option_type": "candidate_fact", **candidate}],
        "recommended_action": {
            "action_type": "rehome_fact",
            "payload": {"fact": candidate},
        },
        "action_id": None,
    }
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, fact_ids, question, options, status, answer, context,
              recommended_action, created_at, answered_at, action_id, decided_by
            ) VALUES (
              'question_actionless', 'unrouted_fact', '["fact_unrouted"]',
              'Where should this fact go?', ?, 'answered', ?, '{}', ?,
              '2026-07-31T12:00:00+00:00',
              '2026-07-31T12:01:00+00:00', ?, 'human'
            )
            """,
            (
                json.dumps(original_question["options"], sort_keys=True),
                json.dumps(
                    {
                        "decision": "route",
                        "page_hint": replacement["page_hint"],
                        "new_action_id": result_action["id"],
                        "old_action_id": "",
                    },
                    sort_keys=True,
                ),
                json.dumps(original_question["recommended_action"], sort_keys=True),
                result_action["id"],
            ),
        )

    undo: dict[str, Any] = {}
    record_question_review_resolution(paths, original_question, "route", undo)
    regenerated = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": replacement},
        target_fact_ids=[candidate["id"]],
        target_page_paths=[replacement["page_hint"]],
        risk_tier="high",
    )

    assert len(undo["review_resolution_ids"]) == 1
    assert regenerated["status"] == "rejected"
    assert regenerated["evidence_json"]["semantic_resolution"]["disposition"] == (
        "keep"
    )


def test_routed_result_identity_mismatch_cannot_keep_unrelated_fact(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    candidate = {
        "id": "fact_route_original",
        "statement": "The original fact needs a route.",
        "entity_key": "concepts:route-original:summary",
        "page_hint": "",
        "source_ids": ["document:route-original"],
        "source_spans": [
            {"source_id": "document:route-original", "start": 0, "end": 32}
        ],
        "evidence_quote": "The original fact needs a route.",
        "confidence": 0.91,
        "status": "active",
        "metadata": {},
    }
    unrelated = {
        **candidate,
        "id": "fact_route_unrelated",
        "statement": "An unrelated applied fact.",
        "entity_key": "concepts:route-unrelated:summary",
        "source_ids": ["document:route-unrelated"],
        "source_spans": [
            {"source_id": "document:route-unrelated", "start": 0, "end": 26}
        ],
        "evidence_quote": "An unrelated applied fact.",
        "page_hint": "concepts/unrelated.md",
    }
    unrelated_action = applied_action(
        paths,
        run_id="run_unrelated_route_result",
        payload={"fact": unrelated},
        target_fact_ids=[unrelated["id"]],
        target_page_paths=[unrelated["page_hint"]],
    )
    original_question = {
        "id": "question_route_identity_mismatch",
        "kind": "unrouted_fact",
        "fact_ids": [candidate["id"]],
        "options": [{"option_type": "candidate_fact", **candidate}],
        "recommended_action": {
            "action_type": "rehome_fact",
            "payload": {"fact": candidate},
        },
        "action_id": None,
    }
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, fact_ids, question, options, status, answer, context,
              recommended_action, created_at, answered_at, action_id, decided_by
            ) VALUES (?, 'unrouted_fact', ?, 'Where should this fact go?', ?,
                      'answered', ?, '{}', ?, ?, ?, ?, 'human')
            """,
            (
                original_question["id"],
                json.dumps(original_question["fact_ids"]),
                json.dumps(original_question["options"], sort_keys=True),
                json.dumps(
                    {
                        "decision": "route",
                        "page_hint": "concepts/original.md",
                        "new_action_id": unrelated_action["id"],
                        "old_action_id": "",
                    },
                    sort_keys=True,
                ),
                json.dumps(original_question["recommended_action"], sort_keys=True),
                "2026-07-31T12:00:00+00:00",
                "2026-07-31T12:01:00+00:00",
                unrelated_action["id"],
            ),
        )

    undo: dict[str, Any] = {}
    record_question_review_resolution(paths, original_question, "route", undo)
    original_regenerated = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": candidate},
        target_fact_ids=[candidate["id"]],
        risk_tier="high",
    )
    unrelated_regenerated = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": {**unrelated, "id": "fact_route_unrelated_copy"}},
        target_fact_ids=["fact_route_unrelated_copy"],
        risk_tier="high",
    )

    assert len(undo["review_resolution_ids"]) == 1
    assert original_regenerated["status"] == "rejected"
    assert (
        original_regenerated["evidence_json"]["semantic_resolution"]["disposition"]
        == "keep"
    )
    assert unrelated_regenerated["status"] == "proposed"
    with connection(paths.sqlite_path) as conn:
        assert active_resolution_for_action(conn, unrelated_action) is None


def test_opposite_resolution_chain_only_undoes_the_current_decision(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    action = applied_action(
        paths,
        run_id="run_resolution_chain",
        payload=fact_payload(),
        target_fact_ids=["fact_launch"],
    )

    with connection(paths.sqlite_path) as conn:
        keep, keep_created = record_review_resolution(
            conn,
            action,
            disposition="keep",
            source_item_kind="audit",
            source_item_id="audit_keep_original",
            resolved_at="2026-07-31T12:01:00+00:00",
            resolution_id="resolution_keep_original",
        )
        reject, reject_created = record_review_resolution(
            conn,
            action,
            disposition="reject",
            source_item_kind="audit",
            source_item_id="audit_reject",
            decision_payload={"superseded_resolution_id": "caller_forged_parent"},
            resolved_at="2026-07-31T12:02:00+00:00",
            resolution_id="resolution_reject",
        )
        latest_keep, latest_keep_created = record_review_resolution(
            conn,
            action,
            disposition="keep",
            source_item_kind="audit",
            source_item_id="audit_keep_latest",
            resolved_at="2026-07-31T12:03:00+00:00",
            resolution_id="resolution_keep_latest",
        )

        assert keep_created is True
        assert reject_created is True
        assert latest_keep_created is True
        assert reject["decision_payload"]["superseded_resolution_id"] == keep["id"]
        assert (
            latest_keep["decision_payload"]["superseded_resolution_id"] == reject["id"]
        )
        assert active_resolution_for_action(conn, action)["id"] == latest_keep["id"]

        # An obsolete undo is a no-op: it must neither replace the current
        # decision nor attempt to reactivate its already superseded parent.
        revoke_review_resolution(
            conn,
            reject["id"],
            revoked_at="2026-07-31T12:04:00+00:00",
        )
        assert active_resolution_for_action(conn, action)["id"] == latest_keep["id"]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM review_resolutions WHERE revoked_at IS NULL"
            ).fetchone()[0]
            == 1
        )

        # Undoing the current decision restores exactly its immediate
        # predecessor. A still-older undo remains inert while that predecessor
        # is current, preserving the partial unique index invariant.
        revoke_review_resolution(
            conn,
            latest_keep["id"],
            revoked_at="2026-07-31T12:05:00+00:00",
        )
        assert active_resolution_for_action(conn, action)["id"] == reject["id"]
        revoke_review_resolution(
            conn,
            keep["id"],
            revoked_at="2026-07-31T12:06:00+00:00",
        )
        assert active_resolution_for_action(conn, action)["id"] == reject["id"]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM review_resolutions WHERE revoked_at IS NULL"
            ).fetchone()[0]
            == 1
        )

        revoke_review_resolution(
            conn,
            reject["id"],
            revoked_at="2026-07-31T12:07:00+00:00",
        )
        assert active_resolution_for_action(conn, action)["id"] == keep["id"]


def test_reject_resolution_does_not_hide_already_applied_exact_sibling(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    representative = applied_action(
        paths,
        run_id="run_representative",
        payload=fact_payload(),
        target_fact_ids=["fact_launch"],
    )
    sibling = applied_action(
        paths,
        run_id="run_sibling",
        payload=fact_payload(),
        target_fact_ids=["fact_launch_sibling"],
    )
    with connection(paths.sqlite_path) as conn:
        record_review_resolution(
            conn,
            representative,
            disposition="reject",
            source_item_kind="audit",
            source_item_id=representative["id"],
        )
        conn.execute(
            "UPDATE cos_actions SET status = 'reverted' WHERE id = ?",
            (representative["id"],),
        )
        assert action_is_manually_resolved(conn, sibling) is False
        selected = load_audit_sample(conn, 25, action_run_id="run_sibling")

    assert [action["id"] for action in selected] == [sibling["id"]]


def test_reject_resolution_closes_and_blocks_preexisting_open_exact_siblings(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    actions = [
        propose_action(
            paths,
            "fact_upsert",
            action_payload=fact_payload(),
            target_fact_ids=["fact_launch"],
            risk_tier="high",
        )
        for _ in range(2)
    ]
    with connection(paths.sqlite_path) as conn:
        for index, action in enumerate(actions):
            conn.execute(
                """
                INSERT INTO open_questions(
                  id, kind, fact_ids, question, options, status, context,
                  action_id, created_at
                ) VALUES (?, 'policy_escalation', '[]', ?, '[]',
                          'needs_human', '{}', ?, ?)
                """,
                (
                    f"question_exact_sibling_{index}",
                    "Review exact semantic candidate.",
                    action["id"],
                    f"2026-07-31T12:00:0{index}+00:00",
                ),
            )
        record_review_resolution(
            conn,
            actions[0],
            disposition="reject",
            source_item_kind="question",
            source_item_id="question_exact_sibling_0",
        )
        statuses = [
            row["status"]
            for row in conn.execute("SELECT status FROM cos_actions ORDER BY rowid")
        ]
        question_statuses = [
            row["status"]
            for row in conn.execute(
                """
                SELECT status FROM open_questions
                WHERE id LIKE 'question_exact_sibling_%'
                ORDER BY id
                """
            )
        ]

    assert statuses == ["rejected", "rejected"]
    assert question_statuses == ["auto_resolved", "auto_resolved"]
    assert apply_action(paths, actions[1]["id"])["status"] == "rejected"


def test_current_run_scope_never_falls_back_to_historical_actions(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    historical_action = applied_action(
        paths,
        run_id="run_historical",
        action_type="canonicalize_page",
        payload={"page_hint": "concepts/historical.md"},
        target_page_paths=["concepts/historical.md"],
    )

    current = run_sampled_audit(
        paths,
        action_run_id="run_current_with_no_actions",
        audit_origin="current_run",
        llm_provider=StaticAuditorProvider({}),
    )

    assert current["sampled"] == 0
    assert current["audited"] == []
    assert current["scope"] == {
        "action_run_id": "run_current_with_no_actions",
        "explicit_action_count": 0,
        "kind": "current_or_explicit",
        "audit_origin": "current_run",
    }
    assert get_action(paths, historical_action["id"])["audit_status"] == "unaudited"

    weekly = run_sampled_audit(
        paths,
        historical=True,
        audit_origin="weekly_historical",
        run_id="audit_weekly_2026_07_31",
        llm_provider=StaticAuditorProvider({historical_action["id"]: "ok"}),
    )

    assert weekly["sampled"] == 1
    assert weekly["scope"]["kind"] == "historical"
    assert weekly["scope"]["audit_origin"] == "weekly_historical"
    audit = weekly["audited"][0]["evidence_json"]["audits"][-1]
    assert audit["metadata"]["audit_origin"] == "weekly_historical"
    assert audit["metadata"]["audit_run_id"] == "audit_weekly_2026_07_31"


class StaticAuditorProvider:
    name = "static-auditor"
    model = "static-model"

    def __init__(self, decisions: dict[str, str]) -> None:
        self.decisions = decisions

    def complete(self, _prompt: str) -> str:
        return json.dumps(
            {
                "audits": [
                    {
                        "action_id": action_id,
                        "decision": decision,
                        "rationale": f"Static audit decision: {decision}.",
                        "confidence": 0.99,
                    }
                    for action_id, decision in self.decisions.items()
                ]
            }
        )
