from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke_gmail_temporal_ledger_public.py"


def _run(action: str, root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), action, "--root", str(root)],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def _report(completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert completed.stderr == ""
    assert completed.stdout.count("\n") == 1
    value = json.loads(completed.stdout)
    assert isinstance(value, dict)
    return value


def _assert_static_safety(report: dict[str, object]) -> None:
    assert report["version"] == "gmail_temporal_ledger_public_smoke_v1"
    assert report["classification"] == "public_synthetic"
    assert report["gmail_provider_calls"] == 0
    assert report["network_calls"] == 0
    assert report["external_model_calls"] == 0
    assert report["private_data_accessed"] is False
    assert report["production_home_accessed"] is False
    assert report["production_pipeline_scope_used"] is False
    assert report["independent_invocations_verified"] is False
    assert report["semantic_metrics_evaluated"] is False
    assert report["release_claim"] is False


def test_public_temporal_ledger_smoke_runs_across_fresh_python_process(
    tmp_path: Path,
) -> None:
    smoke_root = tmp_path / "public-ledger-smoke"

    completed = _run("run", smoke_root)

    assert completed.returncode == 0, completed.stdout
    report = _report(completed)
    _assert_static_safety(report)
    assert report["action"] == "run"
    assert report["status"] == "passed"
    assert report["counts"] == {
        "runs": 3,
        "artifacts": 3,
        "heads": 1,
        "executions": 0,
        "components": 0,
    }
    checks = report["checks"]
    assert isinstance(checks, dict)
    assert checks == {
        "cas_conflict_rejected_without_residue": True,
        "coordinated_recovery_verified": True,
        "dedicated_root_marked": True,
        "fresh_process_exact_replay": True,
        "fresh_python_process_used": True,
        "initial_head_generation": 1,
        "initial_projection_persisted": True,
        "post_clear_head_generation": 5,
        "restored_daemon_not_started": True,
        "restored_home_quarantined": True,
        "rollback_applied": True,
        "rollback_replay_idempotent": True,
        "source_bound_head_clear_applied": True,
        "stale_source_restore_rejected": True,
        "superseded_source_marked_stale": True,
        "temporal_rows_restored_exactly": True,
    }
    serialized = completed.stdout
    for private_or_content_value in (
        str(smoke_root),
        "Project Atlas",
        "public-owner@example.test",
        "public-temporal-message-1",
        "public-temporal-ledger-thread",
        "gtrr_",
        "gtmsg_",
    ):
        assert private_or_content_value not in serialized

    marker = smoke_root / "PUBLIC-SYNTHETIC-TEMPORAL-LEDGER-SMOKE.json"
    assert stat.S_IMODE(smoke_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    restored_quarantine = (
        smoke_root / "restored-home" / "config" / "local" / "restore-quarantine.json"
    )
    assert restored_quarantine.is_file()
    assert json.loads(restored_quarantine.read_text())["status"] == "quarantined"


def test_initialize_and_resume_commands_reopen_the_same_ledger(
    tmp_path: Path,
) -> None:
    smoke_root = tmp_path / "two-process-ledger-smoke"

    initialized = _run("initialize", smoke_root)
    assert initialized.returncode == 0, initialized.stdout
    initialized_report = _report(initialized)
    _assert_static_safety(initialized_report)
    assert initialized_report["action"] == "initialize"
    assert initialized_report["status"] == "passed"
    assert initialized_report["counts"] == {
        "runs": 1,
        "artifacts": 1,
        "heads": 1,
        "executions": 0,
        "components": 0,
    }

    resumed = _run("resume", smoke_root)
    assert resumed.returncode == 0, resumed.stdout
    resumed_report = _report(resumed)
    _assert_static_safety(resumed_report)
    assert resumed_report["action"] == "resume"
    assert resumed_report["status"] == "passed"
    assert resumed_report["checks"]["fresh_process_exact_replay"] is True  # type: ignore[index]
    assert resumed_report["checks"]["temporal_rows_restored_exactly"] is True  # type: ignore[index]


def test_resume_fails_closed_when_the_synthetic_source_was_mutated(
    tmp_path: Path,
) -> None:
    smoke_root = tmp_path / "tampered-ledger-smoke"
    initialized = _run("initialize", smoke_root)
    assert initialized.returncode == 0
    source_files = tuple((smoke_root / "home" / "inbox").rglob("*.md"))
    assert len(source_files) == 1
    source_files[0].write_text(
        source_files[0].read_text(encoding="utf-8") + "tampered\n",
        encoding="utf-8",
    )
    os.chmod(source_files[0], 0o600)

    resumed = _run("resume", smoke_root)

    assert resumed.returncode == 2
    report = _report(resumed)
    _assert_static_safety(report)
    assert report == {
        "version": "gmail_temporal_ledger_public_smoke_v1",
        "action": "resume",
        "status": "failed",
        "fatal": True,
        "error_buckets": {"public_temporal_ledger_smoke_failed": 1},
        "classification": "public_synthetic",
        "gmail_provider_calls": 0,
        "network_calls": 0,
        "external_model_calls": 0,
        "private_data_accessed": False,
        "production_home_accessed": False,
        "production_pipeline_scope_used": False,
        "independent_invocations_verified": False,
        "semantic_metrics_evaluated": False,
        "release_claim": False,
    }
    assert str(smoke_root) not in resumed.stdout
    assert "tampered" not in resumed.stdout
    assert not (smoke_root / "recovery-set").exists()
    assert not (smoke_root / "restored-home").exists()


def test_smoke_refuses_to_reuse_an_existing_unmarked_root(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir(mode=0o700)
    sentinel = existing / "sentinel"
    sentinel.write_text("preserve", encoding="utf-8")

    completed = _run("run", existing)

    assert completed.returncode == 2
    report = _report(completed)
    _assert_static_safety(report)
    assert report["status"] == "failed"
    assert sentinel.read_text(encoding="utf-8") == "preserve"
    assert tuple(existing.iterdir()) == (sentinel,)


def test_resume_refuses_a_redirected_smoke_home(tmp_path: Path) -> None:
    smoke_root = tmp_path / "redirected-home-smoke"
    initialized = _run("initialize", smoke_root)
    assert initialized.returncode == 0
    redirected_home = tmp_path / "redirected-home"
    (smoke_root / "home").rename(redirected_home)
    (smoke_root / "home").symlink_to(redirected_home, target_is_directory=True)

    resumed = _run("resume", smoke_root)

    assert resumed.returncode == 2
    report = _report(resumed)
    _assert_static_safety(report)
    assert report["status"] == "failed"
    assert not (smoke_root / "recovery-set").exists()
    assert not (smoke_root / "restored-home").exists()


def test_resume_refuses_a_redirected_smoke_member(tmp_path: Path) -> None:
    smoke_root = tmp_path / "redirected-source-smoke"
    initialized = _run("initialize", smoke_root)
    assert initialized.returncode == 0
    source_files = tuple((smoke_root / "home" / "inbox").rglob("*.md"))
    assert len(source_files) == 1
    redirected_source = tmp_path / "redirected-source.md"
    source_files[0].rename(redirected_source)
    source_files[0].symlink_to(redirected_source)

    resumed = _run("resume", smoke_root)

    assert resumed.returncode == 2
    report = _report(resumed)
    _assert_static_safety(report)
    assert report["status"] == "failed"
    assert not (smoke_root / "recovery-set").exists()
    assert not (smoke_root / "restored-home").exists()


def test_run_refuses_a_root_overlapping_configured_production_home(
    tmp_path: Path,
) -> None:
    production_home = tmp_path / "declared-production"
    production_home.mkdir()
    smoke_root = production_home / "public-smoke"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "run", "--root", str(smoke_root)],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={**os.environ, "BRAIN_HOME": str(production_home)},
    )

    assert completed.returncode == 2
    report = _report(completed)
    _assert_static_safety(report)
    assert report["status"] == "failed"
    assert not smoke_root.exists()
