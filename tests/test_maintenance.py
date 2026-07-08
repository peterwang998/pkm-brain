from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from typer.testing import CliRunner

from pkm_brain.cli import app
from pkm_brain.maintenance import prune_runtime_artifacts
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


runner = CliRunner()


def test_prune_runtime_artifacts_is_dry_run_by_default_and_commit_deletes(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    stale_db_backup = paths.db_dir / "brain.sqlite.pre-test.bak.gz"
    stale_db_backup.write_bytes(b"backup")
    log = paths.logs / "nightly-maintenance.out.log"
    log.write_text("x" * 32, encoding="utf-8")
    old_backup = paths.home / "backups" / "old-backup"
    old_backup.mkdir(parents=True)
    (old_backup / "brain.sqlite").write_bytes(b"old")
    newer_backup = paths.home / "backups" / "newer-backup"
    newer_backup.mkdir(parents=True)
    (newer_backup / "brain.sqlite").write_bytes(b"new")
    old_timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    os.utime(old_backup, (old_timestamp, old_timestamp))
    os.utime(newer_backup, (old_timestamp + 1, old_timestamp + 1))

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
    assert log.exists()
    assert log.read_text(encoding="utf-8") == ""
    assert log.with_name(f"{log.name}.1").read_text(encoding="utf-8") == "x" * 32
