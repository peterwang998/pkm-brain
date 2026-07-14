from __future__ import annotations

import os
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pkm_brain.gmail_mirror as gmail_mirror_module
import pytest

from pkm_brain.gmail_mirror import (
    GMAIL_MIRROR_QUEUE_STATES,
    GmailMirrorCheckpointUpdate,
    GmailMirrorGenerationConflict,
    GmailMirrorQuarantineInput,
    GmailMirrorSecurityError,
    GmailMirrorStore,
    GmailMirrorThreadInput,
)
from pkm_brain.google_normalization import NormalizedGmailMessage, NormalizedGmailThread


NOW = datetime(2026, 7, 14, 19, tzinfo=timezone.utc)


def private_mirror_path(tmp_path: Path) -> Path:
    root = tmp_path / "gmail-mirror"
    root.mkdir(mode=0o700)
    return root / "gmail-mirror.sqlite"


def message(
    thread_id: str = "thread-1",
    *,
    revision: str = "revision-1",
    body: str = "Please send the board deck.",
) -> NormalizedGmailMessage:
    return NormalizedGmailMessage(
        message_id=f"message-{revision}",
        thread_id=thread_id,
        internal_date="1784055600000",
        timestamp="2026-07-14T19:00:00+00:00",
        from_addresses=("leader@example.com",),
        to_addresses=("operator@example.com",),
        cc_addresses=(),
        subject="Board deck",
        date_header="Tue, 14 Jul 2026 12:00:00 -0700",
        internet_message_id=f"<{revision}@example.com>",
        in_reply_to=None,
        references=(),
        label_ids=("INBOX",),
        outgoing=False,
        operator_authored=False,
        body=body,
        body_kind="text/plain",
        attachment_count=0,
        quoted_chars_removed=12,
        truncated=False,
    )


def thread(
    thread_id: str = "thread-1",
    *,
    revision: str = "revision-1",
    body: str = "Please send the board deck.",
) -> NormalizedGmailThread:
    normalized_message = message(thread_id, revision=revision, body=body)
    return NormalizedGmailThread(
        thread_id=thread_id,
        history_id=revision,
        source_revision=revision,
        subject="Board deck",
        created_at="2026-07-14T19:00:00+00:00",
        updated_at="2026-07-14T19:00:00+00:00",
        message_class="human",
        messages=(normalized_message,),
        body_chars=len(body),
        attachment_count=0,
        quoted_chars_removed=12,
        truncated=False,
    )


def mirror_input(value: NormalizedGmailThread) -> GmailMirrorThreadInput:
    return GmailMirrorThreadInput(
        thread=value,
        raw_payload={
            "id": value.thread_id,
            "historyId": value.history_id,
            "messages": [{"id": item.message_id} for item in value.messages],
        },
    )


def checkpoint_update(
    *,
    history_id: str = "mailbox-history-1",
    expected_generation: int | None = None,
    updated_at: datetime = NOW,
) -> GmailMirrorCheckpointUpdate:
    return GmailMirrorCheckpointUpdate(
        account_key="gmail.primary",
        history_id=history_id,
        mode="full" if expected_generation is None else "incremental",
        coverage_complete=True,
        reset_required=False,
        continuation_page_token=None,
        baseline_history_id=None,
        pending_thread_ids=(),
        continuation_history_id=None,
        expected_generation=expected_generation,
        last_success_at=updated_at.isoformat(),
        updated_at=updated_at.isoformat(),
    )


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_initial_sync_is_private_atomic_and_round_trips_normalized_thread(
    tmp_path: Path,
) -> None:
    db_path = private_mirror_path(tmp_path)
    store = GmailMirrorStore(db_path)
    store.initialize()
    store.initialize()

    source = thread()
    result = store.apply_sync_unit(
        checkpoint_update(),
        (mirror_input(source),),
    )

    assert result.inserted_revisions == 1
    assert result.current_updates == 1
    assert result.tombstones == 0
    assert result.queued == 1
    assert result.superseded == 0
    assert result.checkpoint.history_id == "mailbox-history-1"
    assert result.checkpoint.generation == 1
    assert result.checkpoint.last_sequence == 1
    current = store.get_current_revision("gmail.primary", "thread-1")
    assert current is not None
    assert current.thread == source
    assert current.raw_payload == mirror_input(source).raw_payload
    assert current.tombstoned is False
    pending = store.list_pending_triage("gmail.primary", as_of=NOW.isoformat())
    assert len(pending) == 1
    assert pending[0].thread == source
    assert pending[0].state == "pending"
    assert mode(db_path.parent) == 0o700
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        if path.exists():
            assert mode(path) == 0o600


def test_initialize_upgrades_existing_schema_one_mirror_with_quarantine_table(
    tmp_path: Path,
) -> None:
    db_path = private_mirror_path(tmp_path)
    store = GmailMirrorStore(db_path)
    store.initialize()
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE gmail_mirror_quarantine")
        conn.execute(
            """
            CREATE TABLE gmail_mirror_quarantine (
              account_key TEXT NOT NULL,
              thread_id TEXT NOT NULL,
              source_revision TEXT,
              failure_fingerprint TEXT NOT NULL,
              stage TEXT NOT NULL,
              error TEXT NOT NULL,
              payload_sha256 TEXT NOT NULL,
              occurrence_count INTEGER NOT NULL,
              first_seen_at TEXT NOT NULL,
              last_seen_at TEXT NOT NULL,
              resolved_at TEXT,
              PRIMARY KEY(account_key, thread_id, failure_fingerprint)
            )
            """
        )

    store.initialize()

    with sqlite3.connect(db_path) as conn:
        columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(gmail_mirror_quarantine)"
            ).fetchall()
        }
    assert {
        "retry_count",
        "next_retry_at",
        "last_retry_at",
        "last_parser_version",
    }.issubset(columns)
    assert store.quarantine_counts("gmail.primary") == {
        "total_count": 0,
        "unresolved_count": 0,
    }


def test_checkpoint_generation_conflict_rolls_back_whole_unit(
    tmp_path: Path,
) -> None:
    store = GmailMirrorStore(private_mirror_path(tmp_path))
    store.initialize()
    original = thread("thread-existing", revision="revision-existing")
    store.apply_sync_unit(checkpoint_update(), (mirror_input(original),))

    with pytest.raises(GmailMirrorGenerationConflict):
        store.apply_sync_unit(
            checkpoint_update(
                history_id="mailbox-history-stale",
                expected_generation=None,
            ),
            (mirror_input(thread("thread-stale", revision="revision-stale")),),
        )
    assert store.get_current_revision("gmail.primary", "thread-stale") is None

    conflicting = GmailMirrorThreadInput(
        thread=original,
        raw_payload={"id": "thread-existing", "historyId": "changed-under-same-revision"},
    )
    result = store.apply_sync_unit(
        checkpoint_update(
            history_id="mailbox-history-2",
            expected_generation=1,
        ),
        (
            mirror_input(thread("thread-new", revision="revision-new")),
            conflicting,
        ),
    )

    assert result.quarantined == 1
    assert store.get_current_revision("gmail.primary", "thread-new") is not None
    checkpoint = store.get_checkpoint("gmail.primary")
    assert checkpoint is not None
    assert checkpoint.generation == 2
    assert checkpoint.history_id == "mailbox-history-2"


def test_conflicting_thread_is_quarantined_while_peer_and_checkpoint_commit(
    tmp_path: Path,
) -> None:
    store = GmailMirrorStore(private_mirror_path(tmp_path))
    store.initialize()
    original = thread("thread-existing", revision="revision-existing")
    store.apply_sync_unit(checkpoint_update(), (mirror_input(original),))
    conflicting = GmailMirrorThreadInput(
        thread=original,
        raw_payload={
            "id": "thread-existing",
            "historyId": "revision-existing",
            "messages": [{"id": "provider-content-changed"}],
        },
    )
    peer = thread("thread-good", revision="revision-good")

    result = store.apply_sync_unit(
        checkpoint_update(
            history_id="mailbox-history-2",
            expected_generation=1,
            updated_at=NOW + timedelta(minutes=10),
        ),
        (conflicting, mirror_input(peer)),
    )

    assert result.quarantined == 1
    assert result.inserted_revisions == 1
    assert result.current_updates == 1
    assert result.checkpoint.history_id == "mailbox-history-2"
    assert result.checkpoint.generation == 2
    assert result.checkpoint.last_sequence == 2
    assert store.get_current_revision("gmail.primary", "thread-good") is not None
    current = store.get_current_revision("gmail.primary", "thread-existing")
    assert current is not None
    assert current.raw_payload == mirror_input(original).raw_payload
    assert store.quarantine_counts("gmail.primary") == {
        "total_count": 1,
        "unresolved_count": 1,
    }
    counts = store.triage_counts("gmail.primary")
    assert counts["quarantined_count"] == 1
    assert counts["backlog_count"] == 3


def test_oversized_thread_is_quarantined_without_rolling_back_peer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        gmail_mirror_module,
        "GMAIL_MIRROR_MAX_RAW_PAYLOAD_BYTES",
        500,
    )
    store = GmailMirrorStore(private_mirror_path(tmp_path))
    store.initialize()
    good = thread("thread-good", revision="revision-good")
    oversized_thread = thread("thread-large", revision="revision-large")
    oversized = GmailMirrorThreadInput(
        thread=oversized_thread,
        raw_payload={
            "id": "thread-large",
            "historyId": "revision-large",
            "oversized": "x" * 2_000,
        },
    )

    result = store.apply_sync_unit(
        checkpoint_update(history_id="mailbox-history-complete"),
        (mirror_input(good), oversized),
    )

    assert result.quarantined == 1
    assert result.inserted_revisions == 1
    assert result.checkpoint.history_id == "mailbox-history-complete"
    assert result.checkpoint.last_sequence == 1
    assert store.get_current_revision("gmail.primary", "thread-good") is not None
    assert store.get_current_revision("gmail.primary", "thread-large") is None
    assert store.triage_counts("gmail.primary")["backlog_count"] == 2


def test_malformed_quarantine_metadata_cannot_wedge_valid_peer(
    tmp_path: Path,
) -> None:
    store = GmailMirrorStore(private_mirror_path(tmp_path))
    store.initialize()
    good = thread("thread-good", revision="revision-good")

    result = store.apply_sync_unit(
        checkpoint_update(history_id="mailbox-history-complete"),
        (mirror_input(good),),
        quarantined_threads=(
            GmailMirrorQuarantineInput(
                thread_id="",
                source_revision="\x00bad-revision",
                stage="",
                error="",
                payload_sha256="not-a-digest",
            ),
        ),
    )

    assert result.quarantined == 1
    assert result.checkpoint.history_id == "mailbox-history-complete"
    assert store.get_current_revision("gmail.primary", "thread-good") is not None
    assert store.quarantine_counts("gmail.primary")["unresolved_count"] == 1


def test_quarantine_retry_backoff_parser_upgrade_and_restart_are_durable(
    tmp_path: Path,
) -> None:
    db_path = private_mirror_path(tmp_path)
    store = GmailMirrorStore(db_path)
    store.initialize()
    initial_failure = GmailMirrorQuarantineInput(
        thread_id="thread-bad",
        source_revision="revision-bad",
        stage="normalize",
        error="ValueError: malformed message",
        payload_sha256="a" * 64,
        parser_version="parser-v1",
    )
    store.apply_sync_unit(
        checkpoint_update(history_id="mailbox-history-1"),
        (),
        quarantined_threads=(initial_failure,),
        parser_version="parser-v1",
    )

    assert store.list_due_quarantine_retries(
        "gmail.primary",
        parser_version="parser-v1",
        as_of=(NOW + timedelta(minutes=9, seconds=59)).isoformat(),
    ) == ()
    first_due = store.list_due_quarantine_retries(
        "gmail.primary",
        parser_version="parser-v1",
        as_of=(NOW + timedelta(minutes=10)).isoformat(),
    )
    assert len(first_due) == 1
    assert first_due[0].thread_id == "thread-bad"
    assert first_due[0].retry_count == 0

    retry_failure = GmailMirrorQuarantineInput(
        thread_id="thread-bad",
        source_revision="revision-bad",
        stage="mirror",
        error="ValueError: payload remains oversized",
        payload_sha256="b" * 64,
        retry_attempt=True,
        parser_version="parser-v1",
    )
    store.apply_sync_unit(
        checkpoint_update(
            history_id="mailbox-history-1",
            expected_generation=1,
            updated_at=NOW + timedelta(minutes=10),
        ),
        (),
        quarantined_threads=(retry_failure,),
        quarantine_retry=True,
        parser_version="parser-v1",
    )

    assert store.list_due_quarantine_retries(
        "gmail.primary",
        parser_version="parser-v1",
        as_of=(NOW + timedelta(minutes=29, seconds=59)).isoformat(),
    ) == ()
    restarted = GmailMirrorStore(db_path)
    due_after_restart = restarted.list_due_quarantine_retries(
        "gmail.primary",
        parser_version="parser-v1",
        as_of=(NOW + timedelta(minutes=30)).isoformat(),
    )
    assert len(due_after_restart) == 1
    assert due_after_restart[0].retry_count == 1
    assert due_after_restart[0].last_retry_at == (
        NOW + timedelta(minutes=10)
    ).isoformat()
    # A parser release bypasses the old parser's backoff once, allowing fixed
    # normalization code to clear the durable quarantine promptly.
    parser_upgrade_due = restarted.list_due_quarantine_retries(
        "gmail.primary",
        parser_version="parser-v2",
        as_of=(NOW + timedelta(minutes=11)).isoformat(),
    )
    assert [item.thread_id for item in parser_upgrade_due] == ["thread-bad"]
    counts = restarted.triage_counts("gmail.primary")
    assert counts["quarantined_count"] == 1
    assert counts["backlog_count"] == 1


def test_new_revision_supersedes_pending_work_and_tombstone_is_queued(
    tmp_path: Path,
) -> None:
    store = GmailMirrorStore(private_mirror_path(tmp_path))
    store.initialize()
    first = thread(revision="revision-1")
    store.apply_sync_unit(checkpoint_update(), (mirror_input(first),))
    second = thread(revision="revision-2", body="The deck is now due tomorrow.")

    second_result = store.apply_sync_unit(
        checkpoint_update(
            history_id="mailbox-history-2",
            expected_generation=1,
            updated_at=NOW + timedelta(minutes=10),
        ),
        (mirror_input(second),),
    )

    assert second_result.queued == 1
    assert second_result.superseded == 1
    assert store.get_revision(
        "gmail.primary", "thread-1", "revision-1"
    ).thread == first
    pending = store.list_pending_triage(
        "gmail.primary",
        as_of=(NOW + timedelta(minutes=10)).isoformat(),
    )
    assert [item.source_revision for item in pending] == ["revision-2"]
    counts = store.triage_counts("gmail.primary")
    assert counts["pending"] == 1
    assert counts["superseded"] == 1
    assert counts["backlog_count"] == 1
    assert counts["total_count"] == 2
    assert set(counts).issuperset(GMAIL_MIRROR_QUEUE_STATES)

    tombstone_result = store.apply_sync_unit(
        checkpoint_update(
            history_id="mailbox-history-3",
            expected_generation=2,
            updated_at=NOW + timedelta(minutes=20),
        ),
        (),
        missing_thread_ids=("thread-1",),
    )

    assert tombstone_result.tombstones == 1
    assert tombstone_result.queued == 1
    assert tombstone_result.superseded == 1
    current = store.get_current_revision("gmail.primary", "thread-1")
    assert current is not None
    assert current.tombstoned is True
    assert current.thread is None
    assert current.raw_payload is None
    pending = store.list_pending_triage(
        "gmail.primary",
        as_of=(NOW + timedelta(minutes=20)).isoformat(),
    )
    assert len(pending) == 1
    assert pending[0].tombstoned is True


def test_triage_claim_defer_retry_finish_and_generation_cas(tmp_path: Path) -> None:
    store = GmailMirrorStore(private_mirror_path(tmp_path))
    store.initialize()
    store.apply_sync_unit(checkpoint_update(), (mirror_input(thread()),))

    claimed = store.claim_pending_triage(
        "gmail.primary",
        claimed_at=NOW.isoformat(),
        lease_seconds=60,
    )
    assert len(claimed) == 1
    assert claimed[0].state == "processing"
    assert claimed[0].generation == 2
    assert claimed[0].attempt_count == 1
    with pytest.raises(GmailMirrorGenerationConflict):
        store.finish_triage(
            "gmail.primary",
            "thread-1",
            "revision-1",
            expected_generation=1,
            state="failed",
            error="stale worker",
            updated_at=(NOW + timedelta(seconds=1)).isoformat(),
        )

    retry_at = NOW + timedelta(hours=1)
    deferred = store.finish_triage(
        "gmail.primary",
        "thread-1",
        "revision-1",
        expected_generation=claimed[0].generation,
        state="deferred",
        available_at=retry_at.isoformat(),
        error="daily budget exhausted",
        updated_at=(NOW + timedelta(minutes=1)).isoformat(),
    )
    assert deferred.state == "deferred"
    assert store.list_pending_triage(
        "gmail.primary",
        as_of=(retry_at - timedelta(seconds=1)).isoformat(),
    ) == ()
    assert len(
        store.list_pending_triage("gmail.primary", as_of=retry_at.isoformat())
    ) == 1

    reclaimed = store.claim_pending_triage(
        "gmail.primary",
        claimed_at=retry_at.isoformat(),
    )[0]
    completed = store.finish_triage(
        "gmail.primary",
        "thread-1",
        "revision-1",
        expected_generation=reclaimed.generation,
        state="completed",
        detector_version="gmail-operations-v6",
        policy_version="personal@1",
        updated_at=(retry_at + timedelta(minutes=1)).isoformat(),
    )
    assert completed.state == "completed"
    assert completed.detector_version == "gmail-operations-v6"
    assert completed.policy_version == "personal@1"
    counts = store.triage_counts("gmail.primary")
    assert counts["completed"] == 1
    assert counts["backlog_count"] == 0


def test_failed_current_revision_is_immediately_retryable_and_superseded(
    tmp_path: Path,
) -> None:
    store = GmailMirrorStore(private_mirror_path(tmp_path))
    store.initialize()
    store.apply_sync_unit(checkpoint_update(), (mirror_input(thread()),))
    first_claim = store.claim_pending_triage(
        "gmail.primary",
        claimed_at=NOW.isoformat(),
    )[0]
    failed = store.finish_triage(
        "gmail.primary",
        "thread-1",
        "revision-1",
        expected_generation=first_claim.generation,
        state="failed",
        error="provider unavailable",
        updated_at=(NOW + timedelta(seconds=1)).isoformat(),
    )

    assert failed.state == "failed"
    assert [
        item.state
        for item in store.list_pending_triage(
            "gmail.primary",
            as_of=(NOW + timedelta(seconds=2)).isoformat(),
        )
    ] == ["failed"]
    retry = store.claim_pending_triage(
        "gmail.primary",
        claimed_at=(NOW + timedelta(seconds=2)).isoformat(),
        detector_version="detector-v1",
        policy_version="policy@1",
    )[0]
    assert retry.state == "processing"
    assert retry.attempt_count == 2

    newer = thread(revision="revision-2", body="Updated request")
    result = store.apply_sync_unit(
        checkpoint_update(
            history_id="mailbox-history-2",
            expected_generation=1,
            updated_at=NOW + timedelta(minutes=1),
        ),
        (mirror_input(newer),),
    )
    assert result.superseded == 1
    assert store.triage_counts("gmail.primary")["failed"] == 0
    assert [
        item.source_revision
        for item in store.list_pending_triage(
            "gmail.primary",
            as_of=(NOW + timedelta(minutes=1)).isoformat(),
        )
    ] == ["revision-2"]


def test_restored_identical_revision_is_requeued_with_new_mirror_order(
    tmp_path: Path,
) -> None:
    store = GmailMirrorStore(private_mirror_path(tmp_path))
    store.initialize()
    original = thread()
    store.apply_sync_unit(checkpoint_update(), (mirror_input(original),))
    first_claim = store.claim_pending_triage(
        "gmail.primary",
        claimed_at=NOW.isoformat(),
    )[0]
    store.finish_triage(
        "gmail.primary",
        "thread-1",
        "revision-1",
        expected_generation=first_claim.generation,
        state="completed",
        detector_version="detector-v1",
        policy_version="policy@1",
        updated_at=(NOW + timedelta(seconds=1)).isoformat(),
    )
    store.apply_sync_unit(
        checkpoint_update(
            history_id="mailbox-history-2",
            expected_generation=1,
            updated_at=NOW + timedelta(minutes=1),
        ),
        (),
        missing_thread_ids=("thread-1",),
    )
    tombstone = store.claim_pending_triage(
        "gmail.primary",
        claimed_at=(NOW + timedelta(minutes=1)).isoformat(),
    )[0]
    store.finish_triage(
        "gmail.primary",
        "thread-1",
        tombstone.source_revision,
        expected_generation=tombstone.generation,
        state="completed",
        detector_version="detector-v1",
        policy_version="policy@1",
        updated_at=(NOW + timedelta(minutes=1, seconds=1)).isoformat(),
    )

    restored = store.apply_sync_unit(
        checkpoint_update(
            history_id="mailbox-history-3",
            expected_generation=2,
            updated_at=NOW + timedelta(minutes=2),
        ),
        (mirror_input(original),),
    )

    assert restored.inserted_revisions == 0
    assert restored.current_updates == 1
    assert restored.queued == 1
    pending = store.list_pending_triage(
        "gmail.primary",
        as_of=(NOW + timedelta(minutes=2)).isoformat(),
    )
    assert len(pending) == 1
    assert pending[0].source_revision == "revision-1"
    assert pending[0].mirror_sequence == 3
    assert pending[0].attempt_count == 0


def test_expired_lease_is_reclaimed_and_new_revision_fences_old_worker(
    tmp_path: Path,
) -> None:
    store = GmailMirrorStore(private_mirror_path(tmp_path))
    store.initialize()
    store.apply_sync_unit(checkpoint_update(), (mirror_input(thread()),))
    first_claim = store.claim_pending_triage(
        "gmail.primary",
        claimed_at=NOW.isoformat(),
        lease_seconds=60,
    )[0]
    second_claim = store.claim_pending_triage(
        "gmail.primary",
        claimed_at=(NOW + timedelta(seconds=61)).isoformat(),
        lease_seconds=60,
    )[0]
    assert second_claim.attempt_count == 2
    assert second_claim.generation == first_claim.generation + 2

    newer = thread(revision="revision-2", body="Updated request")
    store.apply_sync_unit(
        checkpoint_update(
            history_id="mailbox-history-2",
            expected_generation=1,
            updated_at=NOW + timedelta(minutes=2),
        ),
        (mirror_input(newer),),
    )
    with pytest.raises(GmailMirrorGenerationConflict):
        store.finish_triage(
            "gmail.primary",
            "thread-1",
            "revision-1",
            expected_generation=second_claim.generation,
            state="completed",
            detector_version="gmail-operations-v6",
            policy_version="personal@1",
            updated_at=(NOW + timedelta(minutes=3)).isoformat(),
        )
    pending = store.list_pending_triage(
        "gmail.primary",
        as_of=(NOW + timedelta(minutes=3)).isoformat(),
    )
    assert [item.source_revision for item in pending] == ["revision-2"]


def test_context_change_reclaims_completed_current_revision(tmp_path: Path) -> None:
    store = GmailMirrorStore(private_mirror_path(tmp_path))
    store.initialize()
    store.apply_sync_unit(checkpoint_update(), (mirror_input(thread()),))
    claimed = store.claim_pending_triage(
        "gmail.primary",
        claimed_at=NOW.isoformat(),
        detector_version="detector-v1",
        policy_version="policy@1",
    )[0]
    store.finish_triage(
        "gmail.primary",
        "thread-1",
        "revision-1",
        expected_generation=claimed.generation,
        state="completed",
        detector_version="detector-v1",
        policy_version="policy@1",
        updated_at=(NOW + timedelta(minutes=1)).isoformat(),
    )

    assert store.claim_pending_triage(
        "gmail.primary",
        claimed_at=(NOW + timedelta(minutes=2)).isoformat(),
        detector_version="detector-v1",
        policy_version="policy@1",
    ) == ()
    reclassified = store.claim_pending_triage(
        "gmail.primary",
        claimed_at=(NOW + timedelta(minutes=3)).isoformat(),
        detector_version="detector-v1",
        policy_version="policy@2",
    )
    assert len(reclassified) == 1
    assert reclassified[0].state == "processing"
    assert reclassified[0].attempt_count == 2
    with pytest.raises(ValueError, match="provided together"):
        store.claim_pending_triage(
            "gmail.primary",
            detector_version="detector-v2",
        )


def test_fetch_result_checkpoint_translation_preserves_only_valid_resume_state() -> None:
    completed = SimpleNamespace(
        mode="full",
        coverage_complete=True,
        next_history_id="history-10",
        reset_required=False,
        continuation_page_token=None,
        baseline_history_id="initial-baseline",
        pending_thread_ids=(),
        continuation_history_id=None,
    )
    complete_update = GmailMirrorCheckpointUpdate.from_fetch_result(
        "gmail.primary",
        completed,
        previous=None,
        updated_at=NOW.isoformat(),
    )
    assert complete_update.history_id == "history-10"
    assert complete_update.baseline_history_id is None
    assert complete_update.last_success_at == NOW.isoformat()

    previous_record = SimpleNamespace(history_id="history-10", generation=4)
    partial = SimpleNamespace(
        mode="incremental",
        coverage_complete=False,
        next_history_id=None,
        reset_required=False,
        continuation_page_token="history-page-2",
        baseline_history_id=None,
        pending_thread_ids=("thread-2",),
        continuation_history_id="history-11",
    )
    partial_update = GmailMirrorCheckpointUpdate.from_fetch_result(
        "gmail.primary",
        partial,
        previous=previous_record,
        updated_at=(NOW + timedelta(minutes=10)).isoformat(),
    )
    assert partial_update.history_id == "history-10"
    assert partial_update.expected_generation == 4
    assert partial_update.pending_thread_ids == ("thread-2",)
    assert partial_update.continuation_history_id == "history-11"
    assert partial_update.last_success_at is None


def test_reset_flag_survives_partial_full_pages_and_clears_on_completion(
    tmp_path: Path,
) -> None:
    store = GmailMirrorStore(private_mirror_path(tmp_path))
    store.initialize()
    first_page = SimpleNamespace(
        mode="full",
        coverage_complete=False,
        next_history_id=None,
        reset_required=True,
        continuation_page_token="reset-page-2",
        baseline_history_id="reset-baseline",
        pending_thread_ids=(),
        continuation_history_id=None,
    )
    first = store.apply_sync_unit(
        GmailMirrorCheckpointUpdate.from_fetch_result(
            "gmail.primary",
            first_page,
            previous=None,
            updated_at=NOW.isoformat(),
        ),
        (),
    ).checkpoint
    assert first.reset_required is True

    resumed_page = SimpleNamespace(
        mode="full",
        coverage_complete=False,
        next_history_id=None,
        reset_required=False,
        continuation_page_token="reset-page-3",
        baseline_history_id="reset-baseline",
        pending_thread_ids=(),
        continuation_history_id=None,
    )
    resumed = store.apply_sync_unit(
        GmailMirrorCheckpointUpdate.from_fetch_result(
            "gmail.primary",
            resumed_page,
            previous=first,
            updated_at=(NOW + timedelta(minutes=1)).isoformat(),
        ),
        (),
    ).checkpoint
    assert resumed.reset_required is True

    complete_page = SimpleNamespace(
        mode="full",
        coverage_complete=True,
        next_history_id="reset-baseline",
        reset_required=False,
        continuation_page_token=None,
        baseline_history_id="reset-baseline",
        pending_thread_ids=(),
        continuation_history_id=None,
    )
    completed = store.apply_sync_unit(
        GmailMirrorCheckpointUpdate.from_fetch_result(
            "gmail.primary",
            complete_page,
            previous=resumed,
            updated_at=(NOW + timedelta(minutes=2)).isoformat(),
        ),
        (),
    ).checkpoint
    assert completed.coverage_complete is True
    assert completed.reset_required is False


def test_mirror_rejects_symlinks_and_non_private_parent_or_database(
    tmp_path: Path,
) -> None:
    public_parent = tmp_path / "public"
    public_parent.mkdir(mode=0o755)
    os.chmod(public_parent, 0o755)
    with pytest.raises(GmailMirrorSecurityError, match="owner-only"):
        GmailMirrorStore(public_parent / "mirror.sqlite").initialize()

    private_parent = tmp_path / "private"
    private_parent.mkdir(mode=0o700)
    outside = private_parent / "outside.sqlite"
    outside.write_bytes(b"")
    os.chmod(outside, 0o600)
    linked = private_parent / "linked.sqlite"
    linked.symlink_to(outside)
    with pytest.raises(GmailMirrorSecurityError, match="symlink"):
        GmailMirrorStore(linked).initialize()

    insecure = private_parent / "insecure.sqlite"
    insecure.write_bytes(b"")
    os.chmod(insecure, 0o644)
    with pytest.raises(GmailMirrorSecurityError, match="owner-only"):
        GmailMirrorStore(insecure).initialize()
