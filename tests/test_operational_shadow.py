from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pkm_brain.operational_db import init_operational_db, operational_connection
from pkm_brain.operational_shadow import (
    HandledAssessment,
    ShadowDecision,
    finish_shadow_run,
    latest_briefing_snapshot,
    latest_handled_assessments,
    list_missing_reports,
    list_shadow_decisions,
    list_shadow_runs,
    persist_shadow_source_unit,
    prune_expired_briefing_snapshots,
    record_handled_assessment,
    record_missing_report,
    record_shadow_decision,
    save_briefing_snapshot,
    start_shadow_run,
)
from pkm_brain.operational_state import (
    OperationalObservation,
    SourceCursorUpdate,
    get_source_cursor,
    operational_item_id,
    reconcile_observation,
)


NOW = "2026-07-13T15:00:00+00:00"


def prepared_db(tmp_path: Path) -> tuple[Path, str]:
    db_path = tmp_path / "ops.sqlite"
    init_operational_db(db_path)
    result = reconcile_observation(
        db_path,
        OperationalObservation(
            source_type="gmail",
            account_key="gmail:test@example.com",
            stream_key="thread:test-thread",
            source_key="test-thread:reply",
            source_revision="message-1",
            source_order=1,
            source_updated_at=NOW,
            observed_at=NOW,
            item_kind="commitment",
            title="Send the draft",
            owner="operator",
            due_at="2026-07-14T17:00:00+00:00",
            confidence=0.9,
            priority=75,
            evidence_refs=(
                {
                    "thread_id": "test-thread",
                    "message_id": "message-1",
                    "source_ref": "gmail:test@example.com:test-thread:message-1",
                },
            ),
            metadata={
                "detector_version": "gmail-ops-v1",
                "message_class": "human",
                "reconciliation_status": "confirmed",
            },
        ),
        processed_at=NOW,
    )
    return db_path, result.item_id


def test_shadow_run_decisions_assessments_and_briefing_are_bounded_and_replayable(
    tmp_path: Path,
) -> None:
    db_path, item_id = prepared_db(tmp_path)
    run = start_shadow_run(
        db_path,
        mode="fixture",
        requested_sources=["calendar", "gmail", "gmail"],
        policy_version="operations-v1",
        detector_version="gmail-ops-v1",
        started_at=NOW,
        run_id="run-fixture-1",
    )
    assert run["requested_sources"] == ["calendar", "gmail"]

    decision = record_shadow_decision(
        db_path,
        run["id"],
        ShadowDecision(
            source_type="gmail",
            account_key="gmail:test@example.com",
            stream_key="thread:test-thread",
            source_key="test-thread",
            source_revision="message-1",
            disposition="surfaced",
            reason_code="direct_operator_commitment",
            item_ids=(item_id,),
            evidence_refs=(
                {
                    "thread_id": "test-thread",
                    "message_id": "message-1",
                    "source_ref": "gmail:test@example.com:test-thread:message-1",
                },
            ),
            confidence=0.9,
            metadata={"message_class": "human"},
        ),
        created_at=NOW,
    )
    assert decision["disposition"] == "surfaced"

    assessment = HandledAssessment(
        item_id=item_id,
        verdict="needs_action",
        supporting_evidence=(
            {
                "thread_id": "test-thread",
                "message_id": "message-1",
                "source_ref": "gmail:test@example.com:test-thread:message-1",
            },
        ),
        sources_checked=("gmail",),
        coverage={"gmail": {"status": "complete"}},
        policy_version="operations-v1",
        confidence=0.95,
        as_of=NOW,
    )
    first = record_handled_assessment(db_path, assessment, run_id=run["id"])
    replay = record_handled_assessment(db_path, assessment, run_id=run["id"])
    assert first["id"] == replay["id"]
    assert first["idempotent"] is False
    assert replay["idempotent"] is True
    assert latest_handled_assessments(db_path)[item_id]["verdict"] == "needs_action"

    finished = finish_shadow_run(
        db_path,
        run["id"],
        status="complete",
        coverage={
            "calendar": {"status": "complete", "fresh_at": NOW},
            "gmail": {"status": "complete", "fresh_at": NOW},
        },
        usage={"requests": 1, "estimated_input_tokens": 1200},
        counts={"surfaced": 1, "suppressed": 2},
        finished_at=NOW,
    )
    assert finished["status"] == "complete"
    assert list_shadow_runs(db_path)[0]["counts"]["surfaced"] == 1
    assert list_shadow_decisions(db_path, run_id=run["id"])[0]["item_ids"] == [
        item_id
    ]

    snapshot = save_briefing_snapshot(
        db_path,
        run_id=run["id"],
        generated_at=NOW,
        as_of=NOW,
        timezone_name="America/Los_Angeles",
        policy_version="operations-v1",
        status="complete",
        sections={"focus": [{"item_id": item_id, "title": "Send the draft"}]},
        coverage=finished["coverage"],
        counts={"focus": 1},
    )
    assert snapshot["idempotent"] is False
    assert latest_briefing_snapshot(db_path)["sections"]["focus"][0]["item_id"] == item_id


def test_shadow_tables_reject_source_bodies_and_append_only_rows(tmp_path: Path) -> None:
    db_path, item_id = prepared_db(tmp_path)
    run = start_shadow_run(
        db_path,
        mode="fixture",
        requested_sources=["gmail"],
        policy_version="operations-v1",
        started_at=NOW,
    )
    with pytest.raises(ValueError, match="source bodies"):
        record_shadow_decision(
            db_path,
            run["id"],
            ShadowDecision(
                source_type="gmail",
                account_key="gmail:test@example.com",
                stream_key="thread:test-thread",
                source_key="test-thread",
                disposition="suppressed",
                reason_code="marketing",
                metadata={"body": "must never enter ops.sqlite"},
            ),
        )

    assessment = record_handled_assessment(
        db_path,
        HandledAssessment(
            item_id=item_id,
            verdict="unknown",
            sources_checked=("gmail",),
            coverage={"gmail": {"status": "partial"}},
            confidence=0.2,
            as_of=NOW,
        ),
        run_id=run["id"],
    )
    with operational_connection(db_path, write=True) as conn:
        with pytest.raises(Exception, match="append-only"):
            conn.execute(
                "UPDATE ops_handled_assessments SET verdict = 'fulfilled' WHERE id = ?",
                (assessment["id"],),
            )


def test_missing_reports_are_bounded_and_knowledge_db_is_untouched(tmp_path: Path) -> None:
    db_path, _item_id = prepared_db(tmp_path)
    knowledge_path = tmp_path / "brain.sqlite"
    knowledge_path.write_bytes(b"knowledge-sentinel")
    before = hashlib.sha256(knowledge_path.read_bytes()).hexdigest()
    report = record_missing_report(
        db_path,
        source_type="gmail",
        source_ref="gmail:test@example.com:test-thread",
        expected_kind="deadline",
        summary="A renewal deadline should have appeared.",
        idempotency_key="missing-renewal-1",
        created_at=NOW,
    )
    assert report["status"] == "open"
    assert list_missing_reports(db_path)[0]["id"] == report["id"]
    assert hashlib.sha256(knowledge_path.read_bytes()).hexdigest() == before
    replay = record_missing_report(
        db_path,
        source_type="gmail",
        source_ref="gmail:test@example.com:test-thread",
        expected_kind="deadline",
        summary="A renewal deadline should have appeared.",
        idempotency_key="missing-renewal-1",
        created_at=NOW,
    )
    assert replay["id"] == report["id"]
    assert replay["idempotent"] is True

    with pytest.raises(ValueError, match="up to 2000"):
        record_missing_report(db_path, summary="x" * 2001)


def test_expired_briefing_snapshots_can_be_pruned(tmp_path: Path) -> None:
    db_path, _item_id = prepared_db(tmp_path)
    snapshot = save_briefing_snapshot(
        db_path,
        generated_at="2026-06-01T12:00:00+00:00",
        as_of="2026-06-01T12:00:00+00:00",
        timezone_name="UTC",
        policy_version="operations-v1",
        status="complete",
        sections={"focus": []},
        coverage={"gmail": {"status": "complete"}},
        retention_days=1,
    )
    result = prune_expired_briefing_snapshots(db_path, as_of=NOW)
    assert result["removed_snapshots"] == 1
    assert result["removed_bytes"] > 0
    assert latest_briefing_snapshot(db_path) is None
    assert snapshot["id"]


def test_shadow_source_unit_rolls_back_items_audit_and_cursor_together(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ops.sqlite"
    init_operational_db(db_path)
    run = start_shadow_run(
        db_path,
        mode="fixture",
        requested_sources=["gmail"],
        policy_version="operations-v1",
        detector_version="gmail-operations-v2",
        started_at=NOW,
    )
    observation = OperationalObservation(
        source_type="gmail",
        account_key="gmail.test",
        stream_key="mailbox",
        source_key="thread-1:reply",
        source_revision="revision-1",
        source_order=1,
        source_updated_at=NOW,
        observed_at=NOW,
        item_kind="commitment",
        title="Reply to the request",
        owner="operator",
        confidence=0.9,
        priority=70,
        evidence_refs=({"thread_id": "thread-1", "source_ref": "gmail.test:thread-1"},),
        metadata={
            "detector_version": "gmail-operations-v2",
            "message_class": "human",
            "reconciliation_status": "confirmed",
        },
    )
    cursor = SourceCursorUpdate(
        connector_id="gmail",
        source_type="gmail",
        account_key="gmail.test",
        stream_key="mailbox",
        cursor="history-2",
        metadata={"coverage_status": "complete"},
    )
    invalid_decision = ShadowDecision(
        source_type="gmail",
        account_key="gmail.test",
        stream_key="mailbox",
        source_key="thread-1",
        source_revision="revision-1",
        disposition="surfaced",
        reason_code="operational_candidate",
        item_ids=("item_does_not_exist",),
    )

    with pytest.raises(ValueError, match="unknown operational item"):
        persist_shadow_source_unit(
            db_path,
            (observation,),
            cursor_update=cursor,
            decisions=(invalid_decision,),
            run_id=str(run["id"]),
            processed_at=NOW,
        )

    with operational_connection(db_path, write=False) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ops_items").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM ops_shadow_decisions").fetchone()[0] == 0
    assert get_source_cursor(db_path, "gmail", "gmail.test", "mailbox") is None

    item_id = operational_item_id(observation)
    result = persist_shadow_source_unit(
        db_path,
        (observation,),
        cursor_update=cursor,
        decisions=(
            ShadowDecision(
                source_type="gmail",
                account_key="gmail.test",
                stream_key="mailbox",
                source_key="thread-1",
                source_revision="revision-1",
                disposition="surfaced",
                reason_code="operational_candidate",
                item_ids=(item_id,),
            ),
        ),
        handled_assessments=(
            HandledAssessment(
                item_id=item_id,
                source_revision="revision-1",
                verdict="needs_action",
                sources_checked=("gmail",),
                coverage={"gmail": {"status": "complete"}},
                as_of=NOW,
            ),
        ),
        run_id=str(run["id"]),
        processed_at=NOW,
    )
    assert result.source_unit.cursor["cursor"] == "history-2"
    assert result.decisions[0]["item_ids"] == [item_id]
    assert result.handled_assessments[0]["observation_id"]


def test_handled_assessment_never_carries_across_source_revision(
    tmp_path: Path,
) -> None:
    db_path, item_id = prepared_db(tmp_path)
    prior = record_handled_assessment(
        db_path,
        HandledAssessment(
            item_id=item_id,
            verdict="fulfilled",
            source_revision="message-1",
            sources_checked=("gmail",),
            coverage={"gmail": {"status": "complete"}},
            confidence=0.99,
            as_of=NOW,
        ),
    )
    assert latest_handled_assessments(db_path)[item_id]["id"] == prior["id"]

    reconcile_observation(
        db_path,
        OperationalObservation(
            source_type="gmail",
            account_key="gmail:test@example.com",
            stream_key="thread:test-thread",
            source_key="test-thread:reply",
            source_revision="message-2",
            source_order=2,
            source_updated_at="2026-07-13T16:00:00+00:00",
            observed_at="2026-07-13T16:00:00+00:00",
            item_kind="commitment",
            title="Send the revised draft",
            owner="operator",
            confidence=0.9,
            priority=75,
            evidence_refs=(
                {
                    "thread_id": "test-thread",
                    "message_id": "message-2",
                    "source_ref": "gmail:test@example.com:test-thread:message-2",
                },
            ),
            metadata={
                "detector_version": "gmail-ops-v1",
                "message_class": "human",
                "reconciliation_status": "confirmed",
            },
        ),
        processed_at="2026-07-13T16:00:00+00:00",
    )

    assert item_id not in latest_handled_assessments(db_path)
    with pytest.raises(ValueError, match="source revision is not current"):
        record_handled_assessment(
            db_path,
            HandledAssessment(
                item_id=item_id,
                verdict="fulfilled",
                source_revision="message-1",
                as_of="2026-07-13T16:00:00+00:00",
            ),
        )
