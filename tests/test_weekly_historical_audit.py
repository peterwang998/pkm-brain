from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pkm_brain.automation as automation
import pkm_brain.cos_audit as cos_audit
import pytest
from typer.testing import CliRunner

from pkm_brain.cli import app
from pkm_brain.cos_actions import apply_action, get_action, propose_action
from pkm_brain.db import connection
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.sync_config import SecondaryConfig, SyncConfig, write_sync_config


def initialized_paths(tmp_path: Path) -> BrainPaths:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    return paths


def successful_audit(**kwargs: Any) -> dict[str, Any]:
    action_ids = list(kwargs.get("action_ids") or [])
    return {
        "status": "ok",
        "mode": "configured",
        "sampled": int(kwargs.get("limit") or 0),
        "sampled_action_ids": action_ids,
        "audited": [{"id": action_id} for action_id in action_ids],
    }


class FixedCohort(list[str]):
    def __init__(self, action_ids: list[str]) -> None:
        super().__init__(action_ids)
        self.eligible = set(action_ids)

    def mark_terminal(self, action_ids: list[str]) -> None:
        self.eligible.difference_update(action_ids)


def use_fixed_cohort(monkeypatch, action_ids: list[str] | None = None) -> FixedCohort:
    cohort = FixedCohort(action_ids or [f"action_{index}" for index in range(1, 6)])
    monkeypatch.setattr(
        automation,
        "select_weekly_historical_audit_cohort",
        lambda _paths, _limit: list(cohort),
    )
    monkeypatch.setattr(
        automation,
        "eligible_weekly_historical_action_ids",
        lambda _paths, requested: [
            action_id for action_id in requested if action_id in cohort.eligible
        ],
    )
    return cohort


def test_historical_scan_cursor_accepts_blank_sql_sort_value() -> None:
    cursor = {"sort_at": "", "action_id": "cosact_blank_applied"}

    assert automation.historical_scan_cursor(cursor) == (
        "",
        "cosact_blank_applied",
    )
    assert automation.historical_scan_cursor_value(("", "cosact_blank_applied")) == (
        cursor
    )


def test_weekly_historical_audit_uses_durable_success_due_gate(
    tmp_path: Path, monkeypatch
) -> None:
    paths = initialized_paths(tmp_path)
    calls: list[dict[str, Any]] = []

    def audit(_paths: BrainPaths, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        cohort.mark_terminal(list(kwargs.get("action_ids") or []))
        return successful_audit(**kwargs)

    cohort = use_fixed_cohort(monkeypatch)
    monkeypatch.setattr(automation, "run_sampled_audit", audit)

    first = automation.run_weekly_historical_audit(paths, if_due=True)
    second = automation.run_weekly_historical_audit(paths, if_due=True)

    assert first.status == "success"
    assert first.due is True
    assert first.skipped is False
    assert first.run_id
    assert calls == [
        {
            "limit": 5,
            "provider": None,
            "run_id": first.run_id,
            "action_ids": cohort,
            "audit_origin": "weekly_historical",
            "historical": True,
        }
    ]
    assert second.status == "skipped"
    assert second.due is False
    assert second.run_id is None


def test_failed_weekly_run_does_not_advance_due_gate(
    tmp_path: Path, monkeypatch
) -> None:
    paths = initialized_paths(tmp_path)
    calls = 0

    def fail(_paths: BrainPaths, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise RuntimeError("auditor unavailable")

    cohort = use_fixed_cohort(monkeypatch)
    monkeypatch.setattr(automation, "run_sampled_audit", fail)

    first = automation.run_weekly_historical_audit(paths, if_due=True)
    second = automation.run_weekly_historical_audit(paths, if_due=True)

    assert first.status == "failed"
    assert second.status == "failed"
    assert second.skipped is False
    assert calls == 2
    assert first.summary["cohort_action_ids"] == cohort
    assert second.summary["cohort_action_ids"] == cohort
    assert second.summary["cohort_retry_of_run_id"] == first.run_id


def test_pending_retry_includes_run_started_in_same_second_after_success(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    boundary_at = "2026-01-03T00:00:00+00:00"
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO automation_runs(
              id, job_name, started_at, finished_at, status, summary
            ) VALUES (?, ?, ?, ?, 'success', '{}')
            """,
            (
                "automation_same_second_success",
                automation.WEEKLY_HISTORICAL_AUDIT_JOB_NAME,
                boundary_at,
                boundary_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO automation_runs(
              id, job_name, started_at, finished_at, status, summary
            ) VALUES (?, ?, ?, ?, 'failed', ?)
            """,
            (
                "automation_same_second_retry",
                automation.WEEKLY_HISTORICAL_AUDIT_JOB_NAME,
                boundary_at,
                boundary_at,
                json.dumps({"cohort_action_ids": ["cosact_same_second"]}),
            ),
        )

    state = automation.pending_weekly_historical_audit_state(paths)

    assert state["run_id"] == "automation_same_second_retry"
    assert state["action_ids"] == ["cosact_same_second"]


def test_pending_retry_does_not_revive_same_second_run_before_success(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    boundary_at = "2026-01-03T00:00:00+00:00"
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO automation_runs(
              id, job_name, started_at, finished_at, status, summary
            ) VALUES (?, ?, ?, ?, 'failed', ?)
            """,
            (
                "automation_same_second_stale_failure",
                automation.WEEKLY_HISTORICAL_AUDIT_JOB_NAME,
                boundary_at,
                boundary_at,
                json.dumps({"cohort_action_ids": ["cosact_stale"]}),
            ),
        )
        conn.execute(
            """
            INSERT INTO automation_runs(
              id, job_name, started_at, finished_at, status, summary
            ) VALUES (?, ?, ?, ?, 'success', '{}')
            """,
            (
                "automation_same_second_latest_success",
                automation.WEEKLY_HISTORICAL_AUDIT_JOB_NAME,
                boundary_at,
                boundary_at,
            ),
        )

    state = automation.pending_weekly_historical_audit_state(paths)

    assert state["run_id"] is None
    assert state["action_ids"] == []


def test_oversized_summary_preserves_weekly_retry_and_cursor_state(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    action_ids = [f"cosact_control_{index}" for index in range(5)]
    window_action_ids = [f"cosact_window_{index:04d}" for index in range(512)]
    scan = {
        "cursor_version": 2,
        "start_after": None,
        "window_end": {
            "sort_at": "2026-01-02T00:00:00+00:00",
            "action_id": "cosact_window_end",
        },
        "window_action_ids": window_action_ids,
        "window_reached_end": False,
        "next_after": {
            "sort_at": "2026-01-02T00:00:00+00:00",
            "action_id": "cosact_window_end",
        },
        "scanned_action_count": 512,
        "reached_end": False,
        "advanced": True,
        "wrapped": False,
    }
    summary = {
        f"diagnostic_{index}": "x" * automation.MAX_STORED_SUMMARY_CHARS
        for index in range(automation.MAX_STORED_SUMMARY_DICT_ITEMS)
    }
    summary.update(
        {
            "cohort_action_ids": action_ids,
            "remaining_cohort_action_ids": action_ids[-2:],
            "historical_scan": scan,
        }
    )
    run_id = "automation_oversized_weekly"
    automation.record_automation_start(
        paths,
        run_id,
        automation.WEEKLY_HISTORICAL_AUDIT_JOB_NAME,
        "2026-01-03T00:00:00+00:00",
    )
    automation.record_automation_finish(
        paths,
        run_id,
        "failed",
        "2026-01-03T00:01:00+00:00",
        summary,
        "interrupted",
    )

    state = automation.pending_weekly_historical_audit_state(paths)

    assert state["action_ids"] == action_ids
    assert state["remaining_action_ids"] == action_ids[-2:]
    assert state["historical_scan"] == scan
    with connection(paths.sqlite_path) as conn:
        stored = json.loads(
            conn.execute(
                "SELECT summary FROM automation_runs WHERE id = ?", (run_id,)
            ).fetchone()["summary"]
        )
    assert stored["truncated"] is True
    assert stored["cohort_action_ids"] == action_ids
    assert stored["historical_scan"] == scan
    assert stored["historical_scan"]["window_action_ids"] == window_action_ids
    automation.record_automation_finish(
        paths,
        run_id,
        "success",
        "2026-01-03T00:02:00+00:00",
        summary,
        None,
    )
    assert automation.last_successful_weekly_historical_scan_cursor(paths) == (
        "2026-01-02T00:00:00+00:00",
        "cosact_window_end",
    )


def test_corrupted_legacy_cohort_sentinel_is_rejected_and_bounded(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    action_ids = [f"cosact_legacy_{index}" for index in range(50)]
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO automation_runs(
              id, job_name, started_at, finished_at, status, summary
            ) VALUES (?, ?, ?, ?, 'failed', ?)
            """,
            (
                "automation_legacy_corrupted",
                automation.WEEKLY_HISTORICAL_AUDIT_JOB_NAME,
                "2026-01-03T00:00:00+00:00",
                "2026-01-03T00:01:00+00:00",
                json.dumps(
                    {"cohort_action_ids": action_ids + [{"_omitted_items": 50}]}
                ),
            ),
        )

    state = automation.pending_weekly_historical_audit_state(paths)

    assert state["action_ids"] == action_ids[:5]
    assert all("omitted_items" not in action_id for action_id in state["action_ids"])


def test_weekly_historical_cli_rejects_cohorts_above_product_limit() -> None:
    result = CliRunner().invoke(
        app,
        ["automation", "weekly-historical-audit", "--limit", "6"],
    )

    assert result.exit_code == 2
    assert "1<=x<=5" in result.output


def test_pending_weekly_retry_waits_for_full_finding_capacity(
    tmp_path: Path, monkeypatch
) -> None:
    paths = initialized_paths(tmp_path)
    cohort = use_fixed_cohort(monkeypatch)
    active_findings = {"count": 0}
    calls = 0

    monkeypatch.setattr(
        automation,
        "active_historical_audit_findings",
        lambda _paths: active_findings["count"],
    )

    def audit(_paths: BrainPaths, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("auditor interrupted")
        cohort.mark_terminal(list(kwargs.get("action_ids") or []))
        return successful_audit(**kwargs)

    monkeypatch.setattr(automation, "run_sampled_audit", audit)

    first = automation.run_weekly_historical_audit(paths, if_due=True)
    active_findings["count"] = 5
    blocked = automation.run_weekly_historical_audit(paths, if_due=True)

    assert first.status == "failed"
    assert blocked.status == "failed"
    assert blocked.skipped is False
    assert blocked.summary["cohort_retry_of_run_id"] == first.run_id
    assert blocked.summary["cohort_action_ids"] == cohort
    assert blocked.summary["sample_limit"] == 0
    assert blocked.summary["audit"] == {
        "status": "skipped",
        "reason": "active historical audit finding capacity is full",
        "sampled": 0,
        "audited": [],
    }
    assert "waiting for active historical finding capacity" in str(blocked.error)
    assert (
        automation.last_successful_automation_run(
            paths, automation.WEEKLY_HISTORICAL_AUDIT_JOB_NAME
        )
        is None
    )
    assert automation.automation_due(
        paths,
        automation.WEEKLY_HISTORICAL_AUDIT_JOB_NAME,
        automation.WEEKLY_HISTORICAL_AUDIT_DUE_AFTER_HOURS,
    )
    pending_run_id, pending_cohort = automation.pending_weekly_historical_audit_cohort(
        paths
    )
    assert pending_run_id == blocked.run_id
    assert pending_cohort == cohort
    assert calls == 1

    active_findings["count"] = 0
    resumed = automation.run_weekly_historical_audit(paths, if_due=True)
    after_success = automation.run_weekly_historical_audit(paths, if_due=True)

    assert resumed.status == "success"
    assert resumed.summary["cohort_retry_of_run_id"] == blocked.run_id
    assert resumed.summary["cohort_action_ids"] == cohort
    assert calls == 2
    assert automation.pending_weekly_historical_audit_cohort(paths) == (None, [])
    assert after_success.status == "skipped"
    assert after_success.due is False


@pytest.mark.parametrize("available_capacity", [1, 2, 3, 4])
def test_partial_capacity_cannot_complete_only_a_subset_of_retry_cohort(
    tmp_path: Path,
    monkeypatch,
    available_capacity: int,
) -> None:
    paths = initialized_paths(tmp_path)
    cohort = use_fixed_cohort(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        automation,
        "active_historical_audit_findings",
        lambda _paths: (
            automation.WEEKLY_HISTORICAL_AUDIT_ACTIVE_LIMIT - available_capacity
        ),
    )

    def audit(_paths: BrainPaths, **kwargs: Any) -> dict[str, Any]:
        requested = list(kwargs.get("action_ids") or [])
        audited_ids = requested[: int(kwargs["limit"])]
        calls.append(audited_ids)
        cohort.mark_terminal(audited_ids)
        return {
            "status": "ok",
            "mode": "configured",
            "sampled": len(audited_ids),
            "sampled_action_ids": audited_ids,
            "audited": [{"id": action_id} for action_id in audited_ids],
            "missing_action_ids": [],
        }

    monkeypatch.setattr(automation, "run_sampled_audit", audit)

    results = []
    while cohort.eligible:
        result = automation.run_weekly_historical_audit(paths, if_due=True)
        results.append(result)
        assert result.summary["cohort_action_ids"] == cohort
        if cohort.eligible:
            assert result.status == "failed"
            assert result.summary["remaining_cohort_size"] == len(cohort.eligible)
            assert automation.pending_weekly_historical_audit_cohort(paths)[1] == cohort

    assert results[-1].status == "success"
    assert all(result.status == "failed" for result in results[:-1])
    assert [action_id for batch in calls for action_id in batch] == cohort
    assert automation.pending_weekly_historical_audit_cohort(paths) == (None, [])


def test_partial_capacity_retry_rechecks_real_action_terminal_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = initialized_paths(tmp_path)
    with connection(paths.sqlite_path) as conn:
        conn.executemany(
            """
            INSERT INTO cos_actions(
              id, action_type, status, target_page_paths, risk_tier,
              audit_status, created_at, applied_at
            ) VALUES (?, 'canonicalize_page', 'applied', ?, 'high',
                      'unaudited', ?, ?)
            """,
            [
                (
                    f"cosact_partial_{index}",
                    json.dumps([f"concepts/partial-{index}.md"]),
                    f"2026-01-01T00:00:0{index}+00:00",
                    f"2026-01-01T00:00:0{index}+00:00",
                )
                for index in range(5)
            ],
        )

    active_findings = {"count": 0}
    calls = 0
    monkeypatch.setattr(
        automation,
        "active_historical_audit_findings",
        lambda _paths: active_findings["count"],
    )

    def audit(_paths: BrainPaths, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("interrupt after cohort persistence")
        audited_ids = list(kwargs.get("action_ids") or [])[: int(kwargs["limit"])]
        with connection(paths.sqlite_path) as conn:
            placeholders = ",".join("?" for _ in audited_ids)
            conn.execute(
                f"UPDATE cos_actions SET audit_status = 'sampled_ok' "
                f"WHERE id IN ({placeholders})",
                audited_ids,
            )
        return {
            "status": "ok",
            "mode": "configured",
            "sampled": len(audited_ids),
            "sampled_action_ids": audited_ids,
            "audited": [{"id": action_id} for action_id in audited_ids],
            "missing_action_ids": [],
        }

    monkeypatch.setattr(automation, "run_sampled_audit", audit)

    initial = automation.run_weekly_historical_audit(paths, if_due=True)
    original_cohort = initial.summary["cohort_action_ids"]
    active_findings["count"] = 3
    first_subset = automation.run_weekly_historical_audit(paths, if_due=True)
    second_subset = automation.run_weekly_historical_audit(paths, if_due=True)
    final_subset = automation.run_weekly_historical_audit(paths, if_due=True)

    assert initial.status == "failed"
    assert len(original_cohort) == 5
    assert first_subset.status == "failed"
    assert first_subset.summary["cohort_action_ids"] == original_cohort
    assert first_subset.summary["remaining_cohort_size"] == 3
    assert second_subset.status == "failed"
    assert second_subset.summary["remaining_cohort_size"] == 1
    assert final_subset.status == "success"
    assert final_subset.summary["remaining_cohort_size"] == 0


def test_weekly_scan_cursor_crosses_resolved_window_and_wraps_safely(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = initialized_paths(tmp_path)
    action_ids = [f"cosact_cursor_{index:04d}" for index in range(520)]
    with connection(paths.sqlite_path) as conn:
        conn.executemany(
            """
            INSERT INTO cos_actions(
              id, action_type, status, target_page_paths, risk_tier,
              audit_status, created_at, applied_at
            ) VALUES (?, 'canonicalize_page', 'applied', ?, 'high',
                      'unaudited', ?, ?)
            """,
            [
                (
                    action_id,
                    json.dumps([f"concepts/cursor-{index:04d}.md"]),
                    f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}+00:00",
                    f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}+00:00",
                )
                for index, action_id in enumerate(action_ids)
            ],
        )

    manually_resolved = set(action_ids[-512:])
    monkeypatch.setattr(
        cos_audit,
        "action_is_manually_resolved",
        lambda _conn, action: str(action["id"]) in manually_resolved,
    )

    def audit(_paths: BrainPaths, **kwargs: Any) -> dict[str, Any]:
        audited_ids = list(kwargs.get("action_ids") or [])[: int(kwargs["limit"])]
        with connection(paths.sqlite_path) as conn:
            placeholders = ",".join("?" for _ in audited_ids)
            conn.execute(
                f"UPDATE cos_actions SET audit_status = 'sampled_ok' "
                f"WHERE id IN ({placeholders})",
                audited_ids,
            )
        return {
            "status": "ok",
            "mode": "configured",
            "sampled": len(audited_ids),
            "sampled_action_ids": audited_ids,
            "audited": [{"id": action_id} for action_id in audited_ids],
            "missing_action_ids": [],
        }

    monkeypatch.setattr(automation, "run_sampled_audit", audit)

    skipped_window = automation.run_weekly_historical_audit(paths)
    older_cohort = automation.run_weekly_historical_audit(paths)

    assert skipped_window.status == "success"
    assert skipped_window.summary["cohort_action_ids"] == []
    assert skipped_window.summary["historical_scan"]["scanned_action_count"] == 512
    assert skipped_window.summary["historical_scan"]["advanced"] is True
    assert older_cohort.status == "success"
    assert older_cohort.summary["cohort_action_ids"] == list(reversed(action_ids[3:8]))
    assert (
        older_cohort.summary["historical_scan"]["start_after"]
        == (skipped_window.summary["historical_scan"]["next_after"])
    )
    assert older_cohort.summary["historical_scan"]["advanced"] is False

    remaining_older_cohort = automation.run_weekly_historical_audit(paths)
    wrapped = automation.run_weekly_historical_audit(paths)
    restarted = automation.run_weekly_historical_audit(paths)

    assert remaining_older_cohort.status == "success"
    assert remaining_older_cohort.summary["cohort_action_ids"] == list(
        reversed(action_ids[:3])
    )
    assert (
        remaining_older_cohort.summary["historical_scan"]["start_after"]
        == (skipped_window.summary["historical_scan"]["next_after"])
    )
    assert remaining_older_cohort.summary["historical_scan"]["advanced"] is False
    assert wrapped.status == "success"
    assert wrapped.summary["cohort_action_ids"] == []
    assert wrapped.summary["historical_scan"]["reached_end"] is True
    assert wrapped.summary["historical_scan"]["wrapped"] is True
    assert wrapped.summary["historical_scan"]["next_after"] is None
    assert restarted.summary["historical_scan"]["start_after"] is None


def test_weekly_historical_window_membership_is_frozen_during_newer_churn(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = initialized_paths(tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    original_action_ids = [f"cosact_frozen_{index:04d}" for index in range(512)]
    with connection(paths.sqlite_path) as conn:
        conn.executemany(
            """
            INSERT INTO cos_actions(
              id, action_type, status, target_page_paths, risk_tier,
              audit_status, created_at, applied_at
            ) VALUES (?, 'canonicalize_page', 'applied', ?, 'high',
                      'unaudited', ?, ?)
            """,
            [
                (
                    action_id,
                    json.dumps([f"concepts/frozen-{index:04d}.md"]),
                    (start + timedelta(seconds=index)).isoformat(),
                    (start + timedelta(seconds=index)).isoformat(),
                )
                for index, action_id in enumerate(original_action_ids)
            ],
        )

    audited_action_ids: list[str] = []

    def audit(_paths: BrainPaths, **kwargs: Any) -> dict[str, Any]:
        action_ids = list(kwargs.get("action_ids") or [])[: int(kwargs["limit"])]
        audited_action_ids.extend(action_ids)
        with connection(paths.sqlite_path) as conn:
            placeholders = ",".join("?" for _ in action_ids)
            conn.execute(
                f"UPDATE cos_actions SET audit_status = 'sampled_ok' "
                f"WHERE id IN ({placeholders})",
                action_ids,
            )
        return {
            "status": "ok",
            "mode": "configured",
            "sampled": len(action_ids),
            "sampled_action_ids": action_ids,
            "audited": [{"id": action_id} for action_id in action_ids],
            "missing_action_ids": [],
        }

    monkeypatch.setattr(automation, "run_sampled_audit", audit)

    churn_action_ids: list[str] = []
    for cycle in range(8):
        if cycle:
            new_ids = [f"cosact_newer_{cycle:02d}_{index:02d}" for index in range(7)]
            churn_action_ids.extend(new_ids)
            with connection(paths.sqlite_path) as conn:
                conn.executemany(
                    """
                    INSERT INTO cos_actions(
                      id, action_type, status, target_page_paths, risk_tier,
                      audit_status, created_at, applied_at
                    ) VALUES (?, 'canonicalize_page', 'applied', '[]', 'high',
                              'unaudited', ?, ?)
                    """,
                    [
                        (
                            action_id,
                            (
                                start + timedelta(days=365, seconds=cycle * 10 + index)
                            ).isoformat(),
                            (
                                start + timedelta(days=365, seconds=cycle * 10 + index)
                            ).isoformat(),
                        )
                        for index, action_id in enumerate(new_ids)
                    ],
                )

        result = automation.run_weekly_historical_audit(paths)

        assert result.status == "success"
        assert len(result.summary["historical_scan"]["window_action_ids"]) == 512
        assert set(result.summary["cohort_action_ids"]).issubset(original_action_ids)

    assert len(audited_action_ids) == 40
    assert len(set(audited_action_ids)) == 40
    assert set(audited_action_ids).issubset(original_action_ids)
    with connection(paths.sqlite_path) as conn:
        untouched_churn = conn.execute(
            f"SELECT COUNT(*) AS count FROM cos_actions WHERE id IN "
            f"({','.join('?' for _ in churn_action_ids)}) "
            "AND audit_status = 'unaudited'",
            churn_action_ids,
        ).fetchone()
    assert int(untouched_churn["count"]) == len(churn_action_ids)


def test_unconfigured_weekly_auditor_does_not_advance_due_gate(
    tmp_path: Path, monkeypatch
) -> None:
    paths = initialized_paths(tmp_path)
    calls = 0

    def stub(_paths: BrainPaths, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"status": "ok", "mode": "stub", "sampled": 5, "audited": []}

    cohort = use_fixed_cohort(monkeypatch)
    monkeypatch.setattr(automation, "run_sampled_audit", stub)

    first = automation.run_weekly_historical_audit(paths, if_due=True)
    second = automation.run_weekly_historical_audit(paths, if_due=True)

    assert first.status == "failed"
    assert second.status == "failed"
    assert calls == 2
    assert first.summary["cohort_action_ids"] == cohort
    assert second.summary["cohort_action_ids"] == cohort
    assert second.summary["cohort_retry_of_run_id"] == first.run_id


def test_partial_weekly_retry_never_widens_original_five_action_cohort(
    tmp_path: Path, monkeypatch
) -> None:
    paths = initialized_paths(tmp_path)
    cohort = use_fixed_cohort(monkeypatch)
    calls: list[list[str]] = []

    def audit(_paths: BrainPaths, **kwargs: Any) -> dict[str, Any]:
        action_ids = list(kwargs.get("action_ids") or [])
        calls.append(action_ids)
        persisted_run_id, persisted_action_ids = (
            automation.pending_weekly_historical_audit_cohort(paths)
        )
        assert persisted_run_id == kwargs["run_id"]
        assert persisted_action_ids == cohort
        if len(calls) == 1:
            cohort.mark_terminal(action_ids[:4])
            return {
                "status": "incomplete",
                "mode": "configured",
                "sampled": 5,
                "sampled_action_ids": action_ids,
                "audited": [{"id": action_id} for action_id in action_ids[:4]],
                "missing_action_ids": action_ids[4:],
            }
        cohort.mark_terminal(action_ids)
        return {
            "status": "ok",
            "mode": "configured",
            "sampled": 1,
            "sampled_action_ids": action_ids[-1:],
            "audited": [{"id": action_ids[-1]}],
            "missing_action_ids": [],
        }

    monkeypatch.setattr(automation, "run_sampled_audit", audit)

    first = automation.run_weekly_historical_audit(paths, if_due=True)
    second = automation.run_weekly_historical_audit(paths, if_due=True)
    third = automation.run_weekly_historical_audit(paths, if_due=True)

    assert first.status == "failed"
    assert len(first.summary["audit"]["audited"]) == 4
    assert second.status == "success"
    assert second.summary["cohort_retry_of_run_id"] == first.run_id
    assert second.summary["cohort_action_ids"] == cohort
    assert len(set().union(*map(set, calls))) == 5
    assert calls == [cohort, cohort[-1:]]
    assert third.status == "skipped"


def test_weekly_historical_audit_respects_active_finding_capacity(
    tmp_path: Path, monkeypatch
) -> None:
    paths = initialized_paths(tmp_path)
    monkeypatch.setattr(
        automation, "active_historical_audit_findings", lambda _paths: 5
    )

    def unexpected(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("auditor should not run while historical capacity is full")

    monkeypatch.setattr(automation, "run_sampled_audit", unexpected)

    result = automation.run_weekly_historical_audit(paths)

    assert result.status == "success"
    assert result.summary["sample_limit"] == 0
    assert result.summary["audit"]["status"] == "skipped"


def test_weekly_historical_audit_is_disabled_on_secondary(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    write_sync_config(
        paths,
        SyncConfig(
            node_id="secondary-a",
            role="secondary",
            brain_home=paths.home,
            secondary=SecondaryConfig(
                primary_node_id="primary-a",
                outbox_path=paths.outbox / "secondary-a",
            ),
        ),
    )

    result = automation.run_weekly_historical_audit(paths)

    assert result.status == "skipped"
    assert result.run_id is None
    assert "secondary role skips" in str(result.reason)


def test_nightly_cos_audit_passes_exact_current_action_ids(
    tmp_path: Path, monkeypatch
) -> None:
    paths = initialized_paths(tmp_path)
    captured: dict[str, Any] = {}

    def audit(_paths: BrainPaths, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return successful_audit(**kwargs)

    monkeypatch.setattr(automation, "run_sampled_audit", audit)
    role_status = {"can_run_mutation_capable_stages": True, "role": "single"}

    result = automation.run_cos_audit(
        paths,
        role_status,
        run_id="automation_current",
        action_ids=["cosact_reused", "cosact_created"],
    )

    assert captured["run_id"] == "automation_current"
    assert captured["action_ids"] == ["cosact_reused", "cosact_created"]
    assert "action_run_id" not in captured
    assert captured["audit_origin"] == "current_run"
    assert result["stage"] == "cos_audit"


def test_current_cycle_audits_reused_prior_run_action(tmp_path: Path) -> None:
    paths = initialized_paths(tmp_path)
    candidate_key = "canonicalize_page:concepts/reused-current.md"
    prior = propose_action(
        paths,
        "canonicalize_page",
        run_id="automation_prior",
        action_payload={"page_hint": "concepts/reused-current.md"},
        action_features={"candidate_key": candidate_key, "reversible": True},
        target_page_paths=["concepts/reused-current.md"],
    )

    reused = propose_action(
        paths,
        "canonicalize_page",
        run_id="automation_current",
        action_payload={"page_hint": "concepts/reused-current.md"},
        action_features={"candidate_key": candidate_key, "reversible": True},
        target_page_paths=["concepts/reused-current.md"],
    )
    applied = apply_action(paths, reused["id"])
    action_ids = automation.current_cycle_action_ids(
        {"actions": [applied]},
        {"actions": [applied]},
        {"actions": []},
    )

    result = automation.run_cos_audit(
        paths,
        {"can_run_mutation_capable_stages": True, "role": "single"},
        run_id="automation_current",
        action_ids=action_ids,
    )

    assert reused["id"] == prior["id"]
    assert applied["run_id"] == "automation_prior"
    assert action_ids == [prior["id"]]
    assert result["sampled_action_ids"] == [prior["id"]]


def test_empty_current_cycle_action_ids_never_scan_history(tmp_path: Path) -> None:
    paths = initialized_paths(tmp_path)
    historical = apply_action(
        paths,
        propose_action(
            paths,
            "canonicalize_page",
            run_id="automation_historical",
            action_payload={"page_hint": "concepts/historical.md"},
            target_page_paths=["concepts/historical.md"],
        )["id"],
    )

    result = automation.run_cos_audit(
        paths,
        {"can_run_mutation_capable_stages": True, "role": "single"},
        run_id="automation_current",
        action_ids=[],
    )

    assert result["sampled"] == 0
    assert result["sampled_action_ids"] == []
    assert get_action(paths, historical["id"])["audit_status"] == "unaudited"
