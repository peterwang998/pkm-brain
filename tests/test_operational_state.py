from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from pkm_brain.db import init_db
from pkm_brain.operational_db import init_operational_db, operational_connection
from pkm_brain.operational_state import (
    CursorConflictError,
    FeedbackConflictError,
    ObservationConflictError,
    OperationalObservation,
    SourceCursorUpdate,
    get_item,
    get_source_cursor,
    list_item_events,
    list_observations,
    reconcile_observation,
    reconcile_source_unit,
    record_item_feedback,
    save_source_cursor,
)
from pkm_brain.paths import BrainPaths


def operational_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "ops.sqlite"
    init_operational_db(db_path)
    return db_path


def calendar_observation(**overrides: object) -> OperationalObservation:
    values: dict[str, object] = {
        "source_type": "google_calendar",
        "account_key": "account-1",
        "stream_key": "primary",
        "source_key": "event-42",
        "source_revision": "etag-0001",
        "source_order": 1,
        "source_updated_at": "2026-07-13T15:00:00+00:00",
        "item_kind": "event",
        "title": "Project review",
        "observed_at": "2026-07-13T15:00:30+00:00",
        "starts_at": "2026-07-14T17:00:00+00:00",
        "ends_at": "2026-07-14T17:30:00+00:00",
        "source_timezone": "America/Los_Angeles",
        "owner": "shared",
        "confidence": 1.0,
        "evidence_refs": ({"calendar_id": "primary", "event_id": "event-42"},),
        "metadata": {"location": "Zoom"},
    }
    values.update(overrides)
    return OperationalObservation(**values)  # type: ignore[arg-type]


def test_source_key_reconciliation_creates_then_noops_exact_revision(
    tmp_path: Path,
) -> None:
    db_path = operational_db(tmp_path)
    observation = calendar_observation()

    created = reconcile_observation(
        db_path,
        observation,
        processed_at="2026-07-13T15:01:00+00:00",
        run_id="run-create",
    )
    duplicate = reconcile_observation(
        db_path,
        observation,
        processed_at="2026-07-13T15:02:00+00:00",
        run_id="run-replay",
    )

    assert created.event_type == "created"
    assert created.item_created is True
    assert created.state == "active"
    assert duplicate.item_id == created.item_id
    assert duplicate.event_type == "noop"
    assert duplicate.item_changed is False
    assert len(list_observations(db_path, item_id=created.item_id)) == 1
    events = list_item_events(db_path, created.item_id)
    assert [event["event_type"] for event in events] == ["created"]
    assert events[0]["actor"] == "connector"
    assert events[0]["run_id"] == "run-create"
    assert events[0]["reconciliation_version"] == "source-key-v1"
    hashed_fields = (
        "item_id",
        "observation_id",
        "event_type",
        "actor",
        "sequence",
        "from_state",
        "to_state",
        "source_type",
        "account_key",
        "stream_key",
        "source_key",
        "source_revision",
        "source_order",
        "run_id",
        "reconciliation_version",
        "before_state",
        "after_state",
        "metadata",
        "idempotency_key",
        "created_at",
    )
    canonical_transition = json.dumps(
        {key: events[0][key] for key in hashed_fields},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    assert (
        events[0]["transition_hash"]
        == hashlib.sha256(canonical_transition.encode("utf-8")).hexdigest()
    )

    item = get_item(db_path, created.item_id)
    assert item is not None
    assert item["account_key"] == "account-1"
    assert item["owner"] == "shared"
    assert item["source_timezone"] == "America/Los_Angeles"


def test_reconciliation_updates_reschedules_and_cancels_one_item(
    tmp_path: Path,
) -> None:
    db_path = operational_db(tmp_path)
    first = reconcile_observation(db_path, calendar_observation())
    updated = reconcile_observation(
        db_path,
        calendar_observation(
            source_revision="etag-0002",
            source_order=2,
            source_updated_at="2026-07-13T15:10:00+00:00",
            title="Project review and demo",
        ),
    )
    rescheduled = reconcile_observation(
        db_path,
        calendar_observation(
            source_revision="etag-0003",
            source_order=3,
            source_updated_at="2026-07-13T15:20:00+00:00",
            title="Project review and demo",
            starts_at="2026-07-14T18:00:00+00:00",
            ends_at="2026-07-14T18:30:00+00:00",
        ),
    )
    cancelled = reconcile_observation(
        db_path,
        calendar_observation(
            source_revision="etag-0004",
            source_order=4,
            source_updated_at="2026-07-13T15:30:00+00:00",
            title="Project review and demo",
            starts_at="2026-07-14T18:00:00+00:00",
            ends_at="2026-07-14T18:30:00+00:00",
            cancelled=True,
        ),
    )

    assert {first.item_id, updated.item_id, rescheduled.item_id, cancelled.item_id} == {
        first.item_id
    }
    assert [updated.event_type, rescheduled.event_type, cancelled.event_type] == [
        "updated",
        "rescheduled",
        "cancelled",
    ]
    item = get_item(db_path, first.item_id)
    assert item is not None
    assert item["state"] == "cancelled"
    assert item["current_observation_id"] == cancelled.observation_id
    assert [
        event["event_type"] for event in list_item_events(db_path, first.item_id)
    ] == ["created", "updated", "rescheduled", "cancelled"]


def test_out_of_order_revision_is_retained_without_reverting_projection(
    tmp_path: Path,
) -> None:
    db_path = operational_db(tmp_path)
    newest = reconcile_observation(
        db_path,
        calendar_observation(
            source_revision="etag-new",
            source_order=10,
            source_updated_at="2026-07-13T16:00:00+00:00",
            title="Newest title",
            starts_at="2026-07-14T19:00:00+00:00",
            ends_at="2026-07-14T19:30:00+00:00",
        ),
    )
    stale = reconcile_observation(
        db_path,
        calendar_observation(
            source_revision="etag-old",
            source_order=5,
            source_updated_at="2026-07-13T14:00:00+00:00",
            title="Stale title",
            starts_at="2026-07-14T16:00:00+00:00",
            cancelled=True,
        ),
    )
    ambiguous_equal = reconcile_observation(
        db_path,
        calendar_observation(
            source_revision="etag-equal",
            source_order=10,
            source_updated_at="2026-07-13T16:00:00+00:00",
            title="Ambiguous equal-order title",
            starts_at="2026-07-14T20:00:00+00:00",
            ends_at="2026-07-14T20:30:00+00:00",
        ),
    )

    assert stale.event_type == "stale_ignored"
    assert stale.item_changed is False
    assert ambiguous_equal.event_type == "stale_ignored"
    item = get_item(db_path, newest.item_id)
    assert item is not None
    assert item["title"] == "Newest title"
    assert item["starts_at"] == "2026-07-14T19:00:00+00:00"
    assert item["state"] == "active"
    assert len(list_observations(db_path, item_id=newest.item_id)) == 3
    assert [
        event["event_type"] for event in list_item_events(db_path, newest.item_id)
    ] == ["created", "stale_ignored", "stale_ignored"]


def test_same_immutable_revision_with_changed_content_is_rejected(
    tmp_path: Path,
) -> None:
    db_path = operational_db(tmp_path)
    original = calendar_observation()
    reconcile_observation(db_path, original)

    replayed_later = reconcile_observation(
        db_path,
        replace(
            original,
            observed_at="2026-07-13T17:00:00Z",
            source_order=2,
            source_updated_at="2026-07-13T15:00:00Z",
        ),
    )
    assert replayed_later.event_type == "noop"

    with pytest.raises(ObservationConflictError, match="conflicting content"):
        reconcile_observation(db_path, replace(original, title="Conflicting title"))

    observations = list_observations(db_path)
    assert len(observations) == 1
    assert observations[0]["source_order"] == 1
    assert observations[0]["payload"]["title"] == "Project review"


def test_restore_after_sticky_source_cancellation_reveals_cancelled_state(
    tmp_path: Path,
) -> None:
    db_path = operational_db(tmp_path)
    created = reconcile_observation(db_path, calendar_observation())
    record_item_feedback(
        db_path,
        created.item_id,
        "dismiss",
        idempotency_key="dismiss-before-cancel",
    )
    cancelled = reconcile_observation(
        db_path,
        calendar_observation(
            source_revision="etag-0002",
            source_order=2,
            source_updated_at="2026-07-13T16:00:00+00:00",
            cancelled=True,
        ),
    )
    restored = record_item_feedback(
        db_path,
        created.item_id,
        "restore",
        idempotency_key="restore-cancelled-source",
    )

    assert cancelled.state == "dismissed"
    assert restored["item"]["state"] == "cancelled"
    assert restored["item"]["human_override_state"] is None


def test_observations_and_item_events_are_database_enforced_append_only(
    tmp_path: Path,
) -> None:
    db_path = operational_db(tmp_path)
    result = reconcile_observation(db_path, calendar_observation())
    event = list_item_events(db_path, result.item_id)[0]

    with operational_connection(db_path, write=True) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="observations are immutable"):
            conn.execute(
                "UPDATE ops_observations SET observed_at = 'changed' WHERE id = ?",
                (result.observation_id,),
            )
    with operational_connection(db_path, write=True) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="item events are append-only"):
            conn.execute("DELETE FROM ops_item_events WHERE id = ?", (event["id"],))


def test_dismissal_is_sticky_across_source_updates_and_can_be_restored(
    tmp_path: Path,
) -> None:
    db_path = operational_db(tmp_path)
    created = reconcile_observation(db_path, calendar_observation())

    dismissed = record_item_feedback(
        db_path,
        created.item_id,
        "dismiss",
        note="Not relevant to today's briefing",
        idempotency_key="feedback-dismiss",
        created_at="2026-07-13T16:00:00+00:00",
    )
    updated = reconcile_observation(
        db_path,
        calendar_observation(
            source_revision="etag-0002",
            source_order=2,
            source_updated_at="2026-07-13T16:10:00+00:00",
            title="Updated project review",
        ),
    )
    cancelled = reconcile_observation(
        db_path,
        calendar_observation(
            source_revision="etag-0003",
            source_order=3,
            source_updated_at="2026-07-13T16:20:00+00:00",
            title="Updated project review",
            cancelled=True,
        ),
    )
    active_again = reconcile_observation(
        db_path,
        calendar_observation(
            source_revision="etag-0004",
            source_order=4,
            source_updated_at="2026-07-13T16:30:00+00:00",
            title="Updated project review",
        ),
    )
    restored = record_item_feedback(
        db_path,
        created.item_id,
        "restore",
        idempotency_key="feedback-restore",
        created_at="2026-07-13T17:00:00+00:00",
    )

    assert dismissed["item"]["state"] == "dismissed"
    assert updated.state == "dismissed"
    assert cancelled.state == "dismissed"
    assert active_again.state == "dismissed"
    assert restored["item"]["state"] == "active"
    assert restored["item"]["human_override_state"] is None
    assert [
        (event["from_state"], event["to_state"])
        for event in list_item_events(db_path, created.item_id)
    ] == [
        (None, "active"),
        ("active", "dismissed"),
        ("dismissed", "dismissed"),
        ("dismissed", "dismissed"),
        ("dismissed", "dismissed"),
        ("dismissed", "active"),
    ]


def test_resolve_is_sticky_feedback_is_idempotent_and_restore_is_validated(
    tmp_path: Path,
) -> None:
    db_path = operational_db(tmp_path)
    created = reconcile_observation(db_path, calendar_observation())
    resolved = record_item_feedback(
        db_path,
        created.item_id,
        "done",
        note="Meeting complete",
        idempotency_key="feedback-resolve",
        created_at="2026-07-14T18:00:00+00:00",
    )
    replay = record_item_feedback(
        db_path,
        created.item_id,
        "resolve",
        note="Meeting complete",
        idempotency_key="feedback-resolve",
        created_at="2026-07-14T18:00:00+00:00",
    )
    source_update = reconcile_observation(
        db_path,
        calendar_observation(
            source_revision="etag-0002",
            source_order=2,
            source_updated_at="2026-07-14T18:10:00+00:00",
            title="Meeting notes posted",
        ),
    )

    assert resolved["decision"] == "resolve"
    assert resolved["item"]["state"] == "resolved"
    assert replay["event_id"] == resolved["event_id"]
    assert replay["idempotent"] is True
    assert source_update.state == "resolved"
    assert len(list_item_events(db_path, created.item_id)) == 3

    replay_after_update = record_item_feedback(
        db_path,
        created.item_id,
        "resolve",
        note="Meeting complete",
        idempotency_key="feedback-resolve",
    )
    assert replay_after_update["item"]["title"] == "Meeting notes posted"
    resolve_event = list_item_events(db_path, created.item_id)[1]
    assert resolve_event["observation_id"] == created.observation_id
    assert resolve_event["source_revision"] == "etag-0001"

    with pytest.raises(FeedbackConflictError, match="different content"):
        record_item_feedback(
            db_path,
            created.item_id,
            "resolve",
            note="Different retry",
            idempotency_key="feedback-resolve",
        )
    with pytest.raises(ValueError, match="not valid from state resolved"):
        record_item_feedback(db_path, created.item_id, "resolve")

    restored = record_item_feedback(
        db_path,
        created.item_id,
        "restore",
        idempotency_key="feedback-resolve-restore",
    )
    assert restored["item"]["state"] == "active"


def test_human_confirmation_survives_a_source_revision(tmp_path: Path) -> None:
    db_path = operational_db(tmp_path)
    created = reconcile_observation(db_path, calendar_observation())
    confirmed = record_item_feedback(
        db_path,
        created.item_id,
        "confirm",
        idempotency_key="feedback-confirm",
        created_at="2026-07-13T16:00:00+00:00",
    )
    updated = reconcile_observation(
        db_path,
        calendar_observation(
            source_revision="etag-0002",
            source_order=2,
            source_updated_at="2026-07-13T16:10:00+00:00",
            title="Updated project review",
        ),
    )

    assert confirmed["item"]["human_confirmed_at"] == "2026-07-13T16:00:00+00:00"
    item = get_item(db_path, updated.item_id)
    assert item is not None
    assert item["human_confirmed_at"] == "2026-07-13T16:00:00+00:00"
    assert item["last_human_action_at"] == "2026-07-13T16:00:00+00:00"


def test_snooze_and_unsnooze_do_not_change_lifecycle_state(tmp_path: Path) -> None:
    db_path = operational_db(tmp_path)
    created = reconcile_observation(db_path, calendar_observation())

    snoozed = record_item_feedback(
        db_path,
        created.item_id,
        "snooze",
        snoozed_until="2026-07-15T08:00:00+00:00",
        idempotency_key="feedback-snooze",
        created_at="2026-07-14T08:00:00+00:00",
    )
    unsnoozed = record_item_feedback(
        db_path,
        created.item_id,
        "unsnooze",
        idempotency_key="feedback-unsnooze",
        created_at="2026-07-14T09:00:00+00:00",
    )

    assert snoozed["item"]["state"] == "active"
    assert snoozed["item"]["snoozed_until"] == "2026-07-15T08:00:00+00:00"
    assert unsnoozed["item"]["snoozed_until"] is None


def test_atomic_source_unit_rolls_back_observations_items_and_cursor(
    tmp_path: Path,
) -> None:
    db_path = operational_db(tmp_path)
    original = calendar_observation()
    conflicting = replace(original, title="Conflicting title")
    cursor_update = SourceCursorUpdate(
        connector_id="calendar",
        source_type="google_calendar",
        account_key="account-1",
        stream_key="primary",
        cursor="sync-1",
        last_success_at="2026-07-13T16:00:00+00:00",
    )

    with pytest.raises(ObservationConflictError):
        reconcile_source_unit(
            db_path,
            [original, conflicting],
            cursor_update=cursor_update,
        )

    assert list_observations(db_path) == []
    assert get_source_cursor(db_path, "calendar", "account-1", "primary") is None


def test_atomic_source_unit_commits_cursor_last_and_enforces_cursor_cas(
    tmp_path: Path,
) -> None:
    db_path = operational_db(tmp_path)
    result = reconcile_source_unit(
        db_path,
        [calendar_observation()],
        cursor_update=SourceCursorUpdate(
            connector_id="calendar",
            source_type="google_calendar",
            account_key="account-1",
            stream_key="primary",
            cursor="sync-1",
            expected_cursor=None,
            enforce_expected_cursor=True,
            last_success_at="2026-07-13T16:00:00+00:00",
        ),
    )

    assert len(result.reconciliations) == 1
    assert result.cursor is not None
    assert result.cursor["cursor"] == "sync-1"
    with pytest.raises(CursorConflictError, match="changed concurrently"):
        save_source_cursor(
            db_path,
            "calendar",
            "account-1",
            "primary",
            source_type="google_calendar",
            cursor="sync-2",
            expected_cursor="wrong-cursor",
            expected_generation=1,
            enforce_expected_cursor=True,
        )
    assert (
        get_source_cursor(db_path, "calendar", "account-1", "primary")["cursor"]
        == "sync-1"
    )


def test_source_unit_rejects_mixed_or_cursor_mismatched_streams(
    tmp_path: Path,
) -> None:
    db_path = operational_db(tmp_path)
    calendar = calendar_observation()
    gmail = calendar_observation(
        source_type="gmail",
        stream_key="inbox",
        source_key="thread-42",
        source_revision="history-1",
        item_kind="commitment",
        title="Send the deck",
        source_timezone=None,
        starts_at=None,
        ends_at=None,
    )

    with pytest.raises(ValueError, match="must share source_type"):
        reconcile_source_unit(db_path, [calendar, gmail])
    with pytest.raises(ValueError, match="cursor must match"):
        reconcile_source_unit(
            db_path,
            [gmail],
            cursor_update=SourceCursorUpdate(
                connector_id="calendar",
                source_type="google_calendar",
                account_key="account-1",
                stream_key="primary",
                cursor="sync-1",
            ),
        )
    assert list_observations(db_path) == []


def test_concurrent_exact_replay_creates_one_item_and_one_event(tmp_path: Path) -> None:
    db_path = operational_db(tmp_path)
    observation = calendar_observation()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda run: reconcile_observation(db_path, observation, run_id=run),
                ["concurrent-1", "concurrent-2"],
            )
        )

    assert sorted(result.event_type for result in results) == ["created", "noop"]
    assert len({result.item_id for result in results}) == 1
    assert len(list_observations(db_path)) == 1
    assert len(list_item_events(db_path, results[0].item_id)) == 1


def test_invalid_or_unbounded_observation_and_feedback_are_rejected(
    tmp_path: Path,
) -> None:
    db_path = operational_db(tmp_path)
    with pytest.raises(ValueError, match="item_kind"):
        reconcile_observation(db_path, calendar_observation(item_kind="goal"))
    with pytest.raises(ValueError, match="source bodies"):
        reconcile_observation(
            db_path,
            calendar_observation(metadata={"body": "full source payload"}),
        )
    with pytest.raises(ValueError, match="must be a reference object"):
        reconcile_observation(
            db_path,
            calendar_observation(evidence_refs=("full email body",)),
        )
    with pytest.raises(ValueError, match="unsupported keys: snippet"):
        reconcile_observation(
            db_path,
            calendar_observation(evidence_refs=({"snippet": "message text"},)),
        )
    with pytest.raises(ValueError, match="bounded non-empty text"):
        reconcile_observation(
            db_path,
            calendar_observation(evidence_refs=({"event_id": None},)),
        )
    with pytest.raises(ValueError, match="metadata v1 has unsupported keys"):
        reconcile_observation(
            db_path,
            calendar_observation(metadata={"description": "source text"}),
        )
    with pytest.raises(ValueError, match="details cannot exceed"):
        reconcile_observation(db_path, calendar_observation(details="x" * 4001))
    with pytest.raises(ValueError, match="cancelled must be a boolean"):
        reconcile_observation(db_path, calendar_observation(cancelled="false"))
    with pytest.raises(ValueError, match="confidence must be between"):
        reconcile_observation(db_path, calendar_observation(confidence="0.5"))
    with pytest.raises(ValueError, match="priority must be between"):
        reconcile_observation(db_path, calendar_observation(priority=1.5))
    with pytest.raises(ValueError, match="normalized to UTC"):
        reconcile_observation(
            db_path,
            calendar_observation(starts_at="2026-07-14T10:00:00-07:00"),
        )
    with pytest.raises(ValueError, match="valid IANA timezone"):
        reconcile_observation(
            db_path,
            calendar_observation(source_timezone="Mars/Olympus"),
        )

    tombstone = reconcile_observation(
        db_path,
        calendar_observation(
            source_key="deleted-event",
            source_revision="sync-generation-4:deleted-event",
            source_order=4,
            source_updated_at=None,
            title="Deleted calendar event",
            starts_at=None,
            ends_at=None,
            source_timezone=None,
            cancelled=True,
            metadata={"source_status": "cancelled"},
        ),
    )
    assert tombstone.state == "cancelled"
    tombstone_observation = list_observations(db_path, item_id=tombstone.item_id)[0]
    assert tombstone_observation["source_updated_at"] is None
    assert tombstone_observation["payload"]["metadata"]["schema_version"] == 1

    created = reconcile_observation(db_path, calendar_observation())
    with pytest.raises(ValueError, match="feedback decision"):
        record_item_feedback(db_path, created.item_id, "archive")
    with pytest.raises(ValueError, match="requires snoozed_until"):
        record_item_feedback(db_path, created.item_id, "snooze")


def test_source_cursor_round_trip_updates_one_stream(tmp_path: Path) -> None:
    db_path = operational_db(tmp_path)
    first = save_source_cursor(
        db_path,
        "calendar",
        "account-1",
        "primary",
        source_type="google_calendar",
        cursor="page-1",
        watermark="2026-07-13T00:00:00+00:00",
        metadata={"pages": 1},
        last_success_at="2026-07-13T15:00:00+00:00",
        updated_at="2026-07-13T15:00:00+00:00",
    )
    second = save_source_cursor(
        db_path,
        "calendar",
        "account-1",
        "primary",
        source_type="google_calendar",
        cursor="page-2",
        watermark="2026-07-14T00:00:00+00:00",
        metadata={"pages": 2},
        last_success_at="2026-07-13T16:00:00+00:00",
        expected_cursor="page-1",
        expected_generation=1,
        enforce_expected_cursor=True,
        updated_at="2026-07-13T16:00:00+00:00",
    )

    assert first["cursor"] == "page-1"
    assert second["cursor"] == "page-2"
    assert [first["generation"], second["generation"]] == [1, 2]
    assert second["metadata"] == {"pages": 2}
    assert get_source_cursor(db_path, "calendar", "account-1", "primary") == second


def test_cursor_generation_cas_protects_streams_with_no_cursor_value(
    tmp_path: Path,
) -> None:
    db_path = operational_db(tmp_path)
    first = save_source_cursor(
        db_path,
        "calendar",
        "account-1",
        "primary",
        source_type="google_calendar",
        cursor=None,
        watermark="2026-07-13T00:00:00+00:00",
    )

    assert first["generation"] == 1
    with pytest.raises(CursorConflictError, match="changed concurrently"):
        save_source_cursor(
            db_path,
            "calendar",
            "account-1",
            "primary",
            source_type="google_calendar",
            cursor=None,
            watermark="2026-07-14T00:00:00+00:00",
            expected_cursor=None,
            expected_generation=None,
        )


def test_operational_writes_do_not_touch_the_knowledge_database(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    init_db(paths.sqlite_path)
    before_hash = hashlib.sha256(paths.sqlite_path.read_bytes()).hexdigest()
    init_operational_db(paths.ops_sqlite_path)

    reconcile_observation(paths.ops_sqlite_path, calendar_observation())

    after_hash = hashlib.sha256(paths.sqlite_path.read_bytes()).hexdigest()
    assert after_hash == before_hash
    with sqlite3.connect(paths.sqlite_path) as conn:
        ops_tables = conn.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'table' AND name LIKE 'ops_%'
            """
        ).fetchone()[0]
    assert ops_tables == 0
