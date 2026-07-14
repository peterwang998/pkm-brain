from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from pkm_brain.cli import app
from pkm_brain.maintenance import managed_storage_inventory, prune_runtime_artifacts
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


runner = CliRunner()


def test_prune_runtime_artifacts_is_dry_run_by_default_and_commit_deletes(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    stale_db_backup = paths.db_dir / "brain.sqlite.pre-test.bak.gz"
    stale_db_backup.write_bytes(b"backup")
    log = paths.logs / "nightly-maintenance.out.log"
    log.write_text("x" * 32, encoding="utf-8")
    runtime_backups = paths.home.parent / "brain-runtime-backups"
    old_backup = runtime_backups / "old-backup"
    old_backup.mkdir(parents=True)
    (old_backup / "brain.sqlite").write_bytes(b"old")
    newer_backup = runtime_backups / "newer-backup"
    newer_backup.mkdir(parents=True)
    (newer_backup / "brain.sqlite").write_bytes(b"new")
    old_timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(old_backup, (old_timestamp, old_timestamp))
    os.utime(newer_backup, (old_timestamp + 1, old_timestamp + 1))
    manual_backup = paths.home / "backups" / "manual-checkpoint"
    manual_backup.mkdir(parents=True)
    (manual_backup / "README").write_text("keep", encoding="utf-8")

    dry_run = prune_runtime_artifacts(
        paths,
        keep_runtime_backups=0,
        keep_days=1,
        max_log_bytes=10,
    )
    cli_result = runner.invoke(
        app,
        [
            "maintenance",
            "prune",
            "--home",
            str(paths.home),
            "--keep-runtime-backups",
            "0",
            "--keep-days",
            "1",
            "--max-log-bytes",
            "10",
        ],
    )

    assert dry_run["dry_run"] is True
    assert dry_run["action_count"] == 4
    assert stale_db_backup.exists()
    assert old_backup.exists()
    assert newer_backup.exists()
    assert manual_backup.exists()
    assert any(item["path"] == str(manual_backup) for item in dry_run["manual_review"])
    assert log.exists()
    assert json.loads(cli_result.stdout)["dry_run"] is True

    committed = prune_runtime_artifacts(
        paths,
        commit=True,
        keep_runtime_backups=0,
        keep_days=1,
        max_log_bytes=10,
    )

    assert committed["dry_run"] is False
    assert not stale_db_backup.exists()
    assert not old_backup.exists()
    assert not newer_backup.exists()
    assert manual_backup.exists()
    assert log.exists()
    assert log.read_text(encoding="utf-8") == ""
    assert log.with_name(f"{log.name}.1").read_text(encoding="utf-8") == "x" * 32


def test_storage_inventory_classifies_managed_and_manual_roots(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    app_support = tmp_path / "AppSupport"
    runtime = app_support / "runtime" / "runtime-1"
    runtime.mkdir(parents=True)
    (runtime / "payload").write_bytes(b"runtime")
    manual = paths.home / "backups" / "checkpoint"
    manual.mkdir(parents=True)
    (manual / "payload").write_bytes(b"checkpoint")

    result = managed_storage_inventory(paths, app_support=app_support)
    roots = {item["key"]: item for item in result["roots"]}
    details = {item["key"]: item for item in result["details"]}

    assert roots["app_runtimes"]["item_count"] == 1
    assert roots["app_runtimes"]["policy"] == "process_aware_retention"
    assert details["gmail_mirror"]["path"] == str(paths.gmail_mirror_sqlite_path)
    assert details["gmail_mirror"]["policy"] == "private_rebuildable_mirror"
    assert details["user_backups"]["policy"] == "manual_review"
