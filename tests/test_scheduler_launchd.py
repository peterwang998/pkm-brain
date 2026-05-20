from __future__ import annotations

import plistlib
import shlex
from pathlib import Path

from pkm_brain.scheduler.launchd import (
    CAPTURE_SECONDARY_LABEL,
    SYNC_PRIMARY_LABEL,
    LaunchdScheduler,
    secondary_capture_job,
    sync_primary_job,
)


def test_install_sync_dry_run_renders_launchagent_plist(tmp_path: Path) -> None:
    job = sync_primary_job(
        repo_path=tmp_path / "repo",
        brain_home=tmp_path / "brain",
        uv_path=Path("/opt/homebrew/bin/uv"),
        peer="secondary",
        interval=1800,
    )

    result = LaunchdScheduler().install(job, dry_run=True)
    decoded = plistlib.loads(plistlib.dumps(result["plist"]))

    assert decoded["Label"] == SYNC_PRIMARY_LABEL
    assert decoded["StartInterval"] == 1800
    command = decoded["ProgramArguments"][-1]
    assert "brain sync run secondary --if-reachable" in command
    assert f"--home {tmp_path}/brain" in command
    assert decoded["StandardOutPath"].endswith("sync-primary.out.log")


def test_install_sync_dry_run_quotes_paths_with_spaces(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo with space"
    brain_home = tmp_path / "brain with space"
    job = sync_primary_job(
        repo_path=repo_path,
        brain_home=brain_home,
        uv_path=Path("/opt/homebrew/bin/uv"),
        peer="secondary",
        interval=1800,
    )

    command = LaunchdScheduler().install(job, dry_run=True)["plist"]["ProgramArguments"][-1]

    assert f"cd {shlex.quote(str(repo_path))}" in command
    assert f"--home {shlex.quote(str(brain_home))}" in command


def test_install_secondary_capture_dry_run_renders_launchagent_plist(tmp_path: Path) -> None:
    job = secondary_capture_job(
        repo_path=tmp_path / "repo",
        brain_home=tmp_path / "brain",
        uv_path=Path("/opt/homebrew/bin/uv"),
        interval=600,
    )

    result = LaunchdScheduler().install(job, dry_run=True)
    decoded = plistlib.loads(plistlib.dumps(result["plist"]))

    assert decoded["Label"] == CAPTURE_SECONDARY_LABEL
    assert decoded["StartInterval"] == 600
    assert "brain automation secondary-tick" in decoded["ProgramArguments"][-1]
    assert decoded["StandardErrorPath"].endswith("capture-secondary.err.log")


def test_install_secondary_capture_dry_run_quotes_paths_with_spaces(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo with space"
    brain_home = tmp_path / "brain with space"
    job = secondary_capture_job(
        repo_path=repo_path,
        brain_home=brain_home,
        uv_path=Path("/opt/homebrew/bin/uv"),
        interval=600,
    )

    command = LaunchdScheduler().install(job, dry_run=True)["plist"]["ProgramArguments"][-1]

    assert f"cd {shlex.quote(str(repo_path))}" in command
    assert f"--home {shlex.quote(str(brain_home))}" in command
