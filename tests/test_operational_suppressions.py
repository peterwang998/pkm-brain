from __future__ import annotations

from pathlib import Path

import pytest

from pkm_brain.operational_db import init_operational_db
from pkm_brain.operational_meeting_packets import (
    MEETING_PACKET_CONTENT_VERSION,
    list_meetings_needing_packets,
    load_current_meeting_packet,
    meeting_packet_readiness,
    save_meeting_packet,
)
from pkm_brain.operational_state import OperationalObservation, reconcile_observation
from pkm_brain.operational_suppressions import (
    active_calendar_series_keys,
    list_calendar_series_suppressions,
    restore_calendar_series,
    suppress_calendar_series,
)


def _event(
    source_key: str,
    *,
    title: str = "Family time",
    series: str | None = "family-weekly",
    revision: str = "etag-1",
    order: int = 1,
    starts_at: str = "2026-07-14T18:00:00+00:00",
    ends_at: str = "2026-07-14T19:00:00+00:00",
    all_day: bool = False,
    transparency: str | None = None,
) -> OperationalObservation:
    metadata = {
        "all_day": all_day,
        "reconciliation_status": "confirmed",
    }
    if series:
        metadata["recurring_event_id"] = series
    if transparency:
        metadata["transparency"] = transparency
    return OperationalObservation(
        source_type="calendar",
        account_key="calendar.personal",
        stream_key="primary",
        source_key=source_key,
        source_revision=revision,
        source_order=order,
        source_updated_at="2026-07-14T10:00:00+00:00",
        observed_at="2026-07-14T10:00:00+00:00",
        item_kind="event",
        title=title,
        owner="operator",
        starts_at=starts_at,
        ends_at=ends_at,
        source_timezone="America/Los_Angeles",
        confidence=1.0,
        evidence_refs=(
            {
                "event_id": source_key,
                "source_ref": f"calendar.personal:primary:{source_key}",
                "source_revision": revision,
            },
        ),
        metadata=metadata,
    )


def test_calendar_series_suppression_is_bulk_reversible_and_durable(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ops.sqlite"
    init_operational_db(db_path)
    first = reconcile_observation(db_path, _event("family:2026-07-14"))
    reconcile_observation(
        db_path,
        _event(
            "family:2026-07-21",
            starts_at="2026-07-21T18:00:00+00:00",
            ends_at="2026-07-21T19:00:00+00:00",
        ),
    )

    rule = suppress_calendar_series(
        db_path,
        first.item_id,
        updated_at="2026-07-14T17:00:00+00:00",
        as_of="2026-07-14T17:00:00+00:00",
    )
    listed = list_calendar_series_suppressions(
        db_path,
        as_of="2026-07-14T17:00:00+00:00",
    )

    assert rule["active"] is True
    assert rule["hidden_count"] == 2
    assert rule["next_starts_at"] == "2026-07-14T18:00:00+00:00"
    assert [(item["label"], item["hidden_count"]) for item in listed] == [
        ("Family time", 2)
    ]
    assert active_calendar_series_keys(db_path) == {
        ("calendar.personal", "family-weekly")
    }

    restored = restore_calendar_series(
        db_path,
        rule["id"],
        updated_at="2026-07-14T17:01:00+00:00",
        as_of="2026-07-14T17:00:00+00:00",
    )
    assert restored["active"] is False
    assert list_calendar_series_suppressions(
        db_path,
        as_of="2026-07-14T17:00:00+00:00",
    ) == []
    assert active_calendar_series_keys(db_path) == set()


def test_calendar_series_count_includes_current_and_future_but_not_past(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ops.sqlite"
    init_operational_db(db_path)
    reconcile_observation(
        db_path,
        _event(
            "family:past",
            starts_at="2026-07-14T15:00:00+00:00",
            ends_at="2026-07-14T16:00:00+00:00",
        ),
    )
    current = reconcile_observation(
        db_path,
        _event(
            "family:current",
            starts_at="2026-07-14T16:30:00+00:00",
            ends_at="2026-07-14T17:30:00+00:00",
        ),
    )
    reconcile_observation(
        db_path,
        _event(
            "family:future",
            starts_at="2026-07-14T18:00:00+00:00",
            ends_at="2026-07-14T19:00:00+00:00",
        ),
    )
    as_of = "2026-07-14T17:00:00+00:00"

    rule = suppress_calendar_series(
        db_path,
        current.item_id,
        updated_at=as_of,
        as_of=as_of,
    )
    listed = list_calendar_series_suppressions(db_path, as_of=as_of)

    assert rule["hidden_count"] == 2
    assert rule["next_starts_at"] == "2026-07-14T18:00:00+00:00"
    assert listed[0]["hidden_count"] == 2
    assert listed[0]["next_starts_at"] == "2026-07-14T18:00:00+00:00"

    restored = restore_calendar_series(
        db_path,
        rule["id"],
        updated_at="2026-07-14T17:01:00+00:00",
        as_of=as_of,
    )
    assert restored["hidden_count"] == 2
    assert restored["next_starts_at"] == "2026-07-14T18:00:00+00:00"


def test_calendar_series_suppression_rejects_one_off_event(tmp_path: Path) -> None:
    db_path = tmp_path / "ops.sqlite"
    init_operational_db(db_path)
    item = reconcile_observation(db_path, _event("one-off", series=None))

    with pytest.raises(ValueError, match="not part of a recurring series"):
        suppress_calendar_series(db_path, item.item_id)


def test_meeting_packet_cache_is_revision_bound_and_skips_hidden_series(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ops.sqlite"
    init_operational_db(db_path)
    meeting = reconcile_observation(
        db_path,
        _event(
            "meeting:2026-07-14",
            title="Project review",
            series="project-weekly",
        ),
    )
    hidden = reconcile_observation(
        db_path,
        _event(
            "family:2026-07-14",
            starts_at="2026-07-14T20:00:00+00:00",
            ends_at="2026-07-14T21:00:00+00:00",
        ),
    )
    suppress_calendar_series(db_path, hidden.item_id)

    assert list_meetings_needing_packets(
        db_path,
        as_of="2026-07-14T17:00:00+00:00",
    ) == [meeting.item_id]

    save_meeting_packet(
        db_path,
        meeting.item_id,
        {"schema_version": 1, "item_id": meeting.item_id, "title": "Legacy packet"},
        generated_at="2026-07-14T17:00:00+00:00",
    )
    assert load_current_meeting_packet(db_path, meeting.item_id) is None
    assert list_meetings_needing_packets(
        db_path,
        as_of="2026-07-14T17:00:00+00:00",
    ) == [meeting.item_id]

    packet = {
        "schema_version": 1,
        "content_version": MEETING_PACKET_CONTENT_VERSION,
        "item_id": meeting.item_id,
        "title": "Project review",
    }
    saved = save_meeting_packet(
        db_path,
        meeting.item_id,
        packet,
        generated_at="2026-07-14T17:00:00+00:00",
    )
    assert saved["idempotent"] is False
    loaded = load_current_meeting_packet(db_path, meeting.item_id)
    assert loaded is not None
    assert loaded["title"] == "Project review"
    assert loaded["prepared_in_advance"] is True
    assert list_meetings_needing_packets(
        db_path,
        as_of="2026-07-14T17:00:00+00:00",
    ) == []

    reconcile_observation(
        db_path,
        _event(
            "meeting:2026-07-14",
            title="Project review moved",
            series="project-weekly",
            revision="etag-2",
            order=2,
        ),
    )
    assert load_current_meeting_packet(db_path, meeting.item_id) is None
    assert list_meetings_needing_packets(
        db_path,
        as_of="2026-07-14T17:00:00+00:00",
    ) == [meeting.item_id]


def test_proactive_meeting_gate_uses_72_hours_and_skips_non_meetings(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ops.sqlite"
    init_operational_db(db_path)
    timed = reconcile_observation(
        db_path,
        _event(
            "timed-check-in",
            title="Cruise checkin appointment at 2pm",
            series=None,
            starts_at="2026-07-17T17:00:00+00:00",
            ends_at="2026-07-17T17:30:00+00:00",
        ),
    )
    all_day = reconcile_observation(
        db_path,
        _event(
            "multi-day-cruise",
            title="Cruise",
            series=None,
            starts_at="2026-07-14T00:00:00+00:00",
            ends_at="2026-07-18T00:00:00+00:00",
            all_day=True,
            transparency="transparent",
        ),
    )
    transparent = reconcile_observation(
        db_path,
        _event(
            "focus-block",
            title="Focus block",
            series=None,
            starts_at="2026-07-15T18:00:00+00:00",
            ends_at="2026-07-15T19:00:00+00:00",
            transparency="transparent",
        ),
    )
    family_time = reconcile_observation(
        db_path,
        _event(
            "family-time-before-suppression",
            title="Family Time",
            series="family-daily",
            starts_at="2026-07-15T01:00:00+00:00",
            ends_at="2026-07-15T03:00:00+00:00",
        ),
    )
    reconcile_observation(
        db_path,
        _event(
            "family-time-prefixed-before-suppression",
            title="Family time — dinner",
            series="family-dinner-daily",
            starts_at="2026-07-15T03:00:00+00:00",
            ends_at="2026-07-15T05:00:00+00:00",
        ),
    )
    reconcile_observation(
        db_path,
        _event(
            "later-meeting",
            title="Later meeting",
            series=None,
            starts_at="2026-07-17T19:00:01+00:00",
            ends_at="2026-07-17T20:00:01+00:00",
        ),
    )

    assert list_meetings_needing_packets(
        db_path,
        as_of="2026-07-14T18:00:00+00:00",
    ) == [timed.item_id]

    for item in (all_day, transparent, family_time):
        save_meeting_packet(
            db_path,
            item.item_id,
            {
                "schema_version": 1,
                "content_version": MEETING_PACKET_CONTENT_VERSION,
                "item_id": item.item_id,
                "title": "On-demand brief remains supported",
            },
            generated_at="2026-07-14T18:00:00+00:00",
        )
        assert load_current_meeting_packet(db_path, item.item_id) is not None

    assert meeting_packet_readiness(db_path).isdisjoint(
        {all_day.item_id, transparent.item_id, family_time.item_id}
    )
