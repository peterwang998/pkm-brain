from __future__ import annotations

from pathlib import Path

from pkm_brain.scheduler import ScheduledJob
from pkm_brain.scheduler import cron, systemd


def job(tmp_path: Path) -> ScheduledJob:
    return ScheduledJob(
        label="com.pkm-brain.test",
        command="brain doctor",
        interval=60,
        brain_home=tmp_path / "brain",
        repo_path=tmp_path / "repo",
        stdout_path=tmp_path / "brain" / "logs" / "out.log",
        stderr_path=tmp_path / "brain" / "logs" / "err.log",
    )


def test_systemd_scheduler_stub_has_clear_message(tmp_path: Path) -> None:
    scheduler = systemd.Scheduler()

    try:
        scheduler.install(job(tmp_path))
    except NotImplementedError as exc:
        assert "Linux scheduler not yet implemented" in str(exc)
    else:
        raise AssertionError("expected systemd scheduler stub to raise")


def test_cron_scheduler_stub_has_clear_message(tmp_path: Path) -> None:
    scheduler = cron.Scheduler()

    try:
        scheduler.install(job(tmp_path))
    except NotImplementedError as exc:
        assert "Linux scheduler not yet implemented" in str(exc)
    else:
        raise AssertionError("expected cron scheduler stub to raise")
