from __future__ import annotations

from pkm_brain.fact_records import fact_values


def test_nested_event_time_overrides_stale_flat_and_existing_persistence_values() -> (
    None
):
    existing = {
        "event_time_kind": "planned",
        "event_start_at": "2026-05-01",
        "event_end_at": "2026-06-01",
        "event_time_precision": "month",
        "event_time_expression": "in May",
    }
    fact = {
        "statement": "The launch occurred.",
        "event_time": {
            "kind": "actual",
            "start_at": "2026-05-19T17:00:00+00:00",
            "precision": "exact",
        },
        # These are stale mirrors from the planned revision. The nested public
        # object must be authoritative for every persisted event-time column.
        **existing,
    }

    values = fact_values(fact, "fact_launch", existing)

    assert values[-5:] == (
        "actual",
        "2026-05-19T17:00:00+00:00",
        None,
        "exact",
        None,
    )


def test_explicit_null_event_time_clears_stale_flat_and_existing_values() -> None:
    existing = {
        "event_time_kind": "planned",
        "event_start_at": "2026-05-01",
        "event_end_at": "2026-06-01",
        "event_time_precision": "month",
        "event_time_expression": "in May",
    }
    fact = {
        "statement": "The launch timing is no longer asserted.",
        "event_time": None,
        **existing,
    }

    values = fact_values(fact, "fact_launch", existing)

    assert values[-5:] == (None, None, None, None, None)
