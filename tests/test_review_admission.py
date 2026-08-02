from __future__ import annotations

from pathlib import Path

from pkm_brain.db import connection, init_db
from pkm_brain.review_admission import reconcile_review_admissions


def item(
    item_id: str,
    *,
    group: str = "unrouted",
    risk: str = "medium",
) -> dict[str, object]:
    return {
        "id": item_id,
        "kind": "unrouted_fact",
        "group": group,
        "risk_tier": risk,
        "created_at": f"2026-07-11T00:00:{item_id[-2:]}+00:00",
        "approvable": True,
    }


def historical_audit_item(
    item_id: str, *, direct_contradiction: bool = False
) -> dict[str, object]:
    return {
        "id": item_id,
        "kind": "audit_flagged",
        "group": "audit",
        "risk_tier": "high",
        "maintenance_kind": "historical_audit",
        "direct_contradiction": direct_contradiction,
        "audit": {
            "origin": "legacy_historical",
            "direct_contradiction": direct_contradiction,
        },
        "created_at": f"2026-07-10T00:00:{item_id[-2:]}+00:00",
        "approvable": True,
    }


def test_existing_queue_is_grandfathered_then_new_work_is_bounded(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "brain.sqlite"
    init_db(db_path)
    existing = [item(f"existing_{index:02d}") for index in range(4)]

    with connection(db_path) as conn:
        first = reconcile_review_admissions(
            conn,
            existing,
            active_limit=5,
            daily_limit=1,
            now="2026-07-11T08:00:00+00:00",
        )
        second = reconcile_review_admissions(
            conn,
            [*existing, item("new_01"), item("new_02")],
            active_limit=5,
            daily_limit=1,
            now="2026-07-11T09:00:00+00:00",
        )

    assert set(first["states"].values()) == {"admitted"}
    assert second["states"]["new_01"] == "admitted"
    assert second["states"]["new_02"] == "deferred"
    assert second["admitted_today"] == 1


def test_hard_boundary_bypasses_capacity_and_deferred_work_promotes_next_day(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "brain.sqlite"
    init_db(db_path)
    current = [item("current_01")]

    with connection(db_path) as conn:
        reconcile_review_admissions(
            conn,
            current,
            active_limit=1,
            daily_limit=1,
            now="2026-07-11T08:00:00+00:00",
        )
        bounded = reconcile_review_admissions(
            conn,
            [*current, item("ordinary_02"), item("conflict_03", group="conflicts")],
            active_limit=1,
            daily_limit=1,
            now="2026-07-11T09:00:00+00:00",
        )
        promoted = reconcile_review_admissions(
            conn,
            [item("ordinary_02")],
            active_limit=1,
            daily_limit=1,
            now="2026-07-12T09:00:00+00:00",
        )

    assert bounded["states"]["ordinary_02"] == "deferred"
    assert bounded["states"]["conflict_03"] == "admitted"
    assert promoted["states"]["ordinary_02"] == "admitted"


def test_existing_historical_audit_backlog_is_demoted_to_dedicated_cap(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "brain.sqlite"
    init_db(db_path)
    backlog = [historical_audit_item(f"audit_{index:02d}") for index in range(8)]

    with connection(db_path) as conn:
        first = reconcile_review_admissions(
            conn,
            backlog,
            active_limit=100,
            daily_limit=25,
            historical_active_limit=5,
            now="2026-07-31T08:00:00+00:00",
        )
        second = reconcile_review_admissions(
            conn,
            backlog,
            active_limit=100,
            daily_limit=25,
            historical_active_limit=5,
            now="2026-07-31T09:00:00+00:00",
        )
        reasons = {
            row["item_key"]: row["admission_reason"]
            for row in conn.execute("SELECT * FROM review_admissions")
        }

    assert list(first["states"].values()).count("admitted") == 5
    assert list(first["states"].values()).count("deferred") == 3
    assert first["states"] == second["states"]
    assert {
        key for key, state in first["states"].items() if state == "deferred"
    } == {key for key, reason in reasons.items() if reason == "historical_capacity"}


def test_direct_historical_contradiction_is_only_historical_cap_bypass(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "brain.sqlite"
    init_db(db_path)
    capped = [historical_audit_item(f"audit_{index:02d}") for index in range(5)]
    contradiction = historical_audit_item(
        "audit_contradiction_99", direct_contradiction=True
    )

    with connection(db_path) as conn:
        result = reconcile_review_admissions(
            conn,
            [*capped, contradiction],
            active_limit=1,
            daily_limit=1,
            historical_active_limit=5,
            now="2026-07-31T08:00:00+00:00",
        )

    assert result["states"]["audit_contradiction_99"] == "admitted"
    assert list(result["states"].values()).count("admitted") == 6
