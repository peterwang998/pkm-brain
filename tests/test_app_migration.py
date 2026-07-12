from __future__ import annotations

import plistlib
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

from pkm_brain.app_migration import (
    CAPTURE_SECONDARY_LABEL,
    LAUNCH_AGENT_LABEL,
    NIGHTLY_LAUNCH_AGENT_LABEL,
    SYNC_PRIMARY_LABEL,
    build_migration_plan,
    detect_launch_agents,
    install_runtime_shims,
    retire_launch_agents,
)
from pkm_brain.paths import BrainPaths


def write_plist(directory: Path, label: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{label}.plist"
    with path.open("wb") as fh:
        plistlib.dump({"Label": label, "ProgramArguments": ["/bin/true"], "StartInterval": 600}, fh)
    return path


def test_migration_plan_detects_primary_and_secondary_launch_agents(tmp_path: Path) -> None:
    launch_agents = tmp_path / "LaunchAgents"
    for label in (
        LAUNCH_AGENT_LABEL,
        NIGHTLY_LAUNCH_AGENT_LABEL,
        SYNC_PRIMARY_LABEL,
        CAPTURE_SECONDARY_LABEL,
    ):
        write_plist(launch_agents, label)
    home = tmp_path / "brain"
    home.mkdir()

    detected = detect_launch_agents(launch_agents)
    plan = build_migration_plan(
        BrainPaths.from_value(home),
        app_support_dir=tmp_path / "AppSupport",
        launch_agents_dir=launch_agents,
    )

    labels = {item["label"] for item in detected}
    assert labels == {
        LAUNCH_AGENT_LABEL,
        NIGHTLY_LAUNCH_AGENT_LABEL,
        SYNC_PRIMARY_LABEL,
        CAPTURE_SECONDARY_LABEL,
    }
    assert plan["state"] == "migrate"
    assert any(item["role_set"] == "secondary" for item in detected)


def test_retire_launch_agents_moves_plists_and_writes_rollback(tmp_path: Path) -> None:
    launch_agents = tmp_path / "LaunchAgents"
    app_support = tmp_path / "AppSupport"
    plist = write_plist(launch_agents, CAPTURE_SECONDARY_LABEL)
    commands: list[list[str]] = []

    def fake_runner(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    result = retire_launch_agents(
        app_support_dir=app_support,
        launch_agents_dir=launch_agents,
        dry_run=False,
        runner=fake_runner,
    )

    backup = app_support / "migration/plists-backup" / plist.name
    rollback = app_support / "migration/plists-backup/rollback.sh"
    assert result["dry_run"] is False
    assert commands == [["launchctl", "bootout", f"gui/{os.getuid()}/{CAPTURE_SECONDARY_LABEL}"]]
    assert not plist.exists()
    assert backup.exists()
    assert rollback.exists()
    assert stat.S_IMODE(rollback.stat().st_mode) & stat.S_IXUSR
    text = rollback.read_text(encoding="utf-8")
    assert "launchctl bootstrap" in text
    assert CAPTURE_SECONDARY_LABEL in text


def test_retire_launch_agents_dry_run_does_not_move_plists(tmp_path: Path) -> None:
    launch_agents = tmp_path / "LaunchAgents"
    app_support = tmp_path / "AppSupport"
    plist = write_plist(launch_agents, LAUNCH_AGENT_LABEL)

    result = retire_launch_agents(
        app_support_dir=app_support,
        launch_agents_dir=launch_agents,
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert plist.exists()
    assert not (app_support / "migration").exists()


def test_install_runtime_shims_points_to_current_runtime(tmp_path: Path) -> None:
    result = install_runtime_shims(tmp_path / "AppSupport")

    assert result["ok"] is True
    brain = tmp_path / "AppSupport/bin/brain"
    brain_mcp = tmp_path / "AppSupport/bin/brain-mcp"
    assert brain.exists()
    assert brain_mcp.exists()
    assert "runtime/current/bin/brain" in brain.read_text(encoding="utf-8")
    assert "runtime/current/bin/brain-mcp" in brain_mcp.read_text(encoding="utf-8")
    assert stat.S_IMODE(brain.stat().st_mode) == 0o755
