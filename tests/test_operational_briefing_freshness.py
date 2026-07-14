from __future__ import annotations

from pathlib import Path

from pkm_brain.gmail_operations import GMAIL_DETECTOR_VERSION
from pkm_brain.operational_briefing import build_operational_briefing
from pkm_brain.operational_db import init_operational_db
from pkm_brain.operational_shadow import finish_shadow_run, start_shadow_run


NOW = "2026-07-13T15:00:00+00:00"


def _complete_run(
    db_path: Path,
    *,
    policy_version: str,
    detector_version: str,
) -> None:
    run = start_shadow_run(
        db_path,
        mode="fixture",
        requested_sources=("calendar", "gmail"),
        policy_version=policy_version,
        detector_version=detector_version,
        started_at=NOW,
    )
    finish_shadow_run(
        db_path,
        run["id"],
        status="complete",
        coverage={
            "calendar": {"status": "complete", "fresh_at": NOW},
            "gmail": {"status": "complete", "fresh_at": NOW},
        },
        finished_at=NOW,
    )


def test_briefing_rejects_coverage_from_an_old_policy_version(tmp_path: Path) -> None:
    db_path = tmp_path / "ops.sqlite"
    init_operational_db(db_path)
    _complete_run(
        db_path,
        policy_version="operations-v1@1",
        detector_version=GMAIL_DETECTOR_VERSION,
    )

    briefing = build_operational_briefing(
        db_path,
        timezone_name="America/Los_Angeles",
        policy_version="operations-v1@2",
        as_of=NOW,
    )

    assert briefing["status"] == "partial"
    assert briefing["all_clear"] is False
    assert briefing["coverage"]["calendar"]["reason"] == "policy_version_mismatch"
    assert briefing["coverage"]["gmail"]["reason"] == "policy_version_mismatch"


def test_briefing_rejects_gmail_coverage_from_an_old_detector_version(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ops.sqlite"
    init_operational_db(db_path)
    _complete_run(
        db_path,
        policy_version="operations-v1@1",
        detector_version="gmail-operations-stale",
    )

    briefing = build_operational_briefing(
        db_path,
        timezone_name="America/Los_Angeles",
        policy_version="operations-v1@1",
        as_of=NOW,
    )

    assert briefing["status"] == "partial"
    assert briefing["all_clear"] is False
    assert briefing["coverage"]["calendar"]["status"] == "complete"
    assert briefing["coverage"]["gmail"]["reason"] == "detector_version_mismatch"
    assert (
        briefing["coverage"]["gmail"]["expected_detector_version"]
        == GMAIL_DETECTOR_VERSION
    )
