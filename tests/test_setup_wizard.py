from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from pkm_brain.cli import app
from pkm_brain.paths import BrainPaths
from pkm_brain.scheduler.launchd import CAPTURE_SECONDARY_LABEL, SYNC_PRIMARY_LABEL
from pkm_brain.setup_wizard import run_setup_plan


runner = CliRunner()


def test_setup_dry_run_writes_nothing_and_prints_plan(tmp_path: Path) -> None:
    home = tmp_path / "brain"

    result = runner.invoke(
        app,
        [
            "setup",
            "--role",
            "primary",
            "--node-id",
            "primary",
            "--peer-node-id",
            "secondary",
            "--install-scheduler",
            "--dry-run",
            "--json",
            "--home",
            str(home),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["role"] == "primary"
    assert payload["node_id"] == "primary"
    assert SYNC_PRIMARY_LABEL in payload["planned_launch_agent_labels"]
    assert "sync doctor" in payload["validation_steps"]
    assert "sync test-connection secondary" in payload["validation_steps"]
    assert payload["planned_writes"]
    assert not home.exists()


def test_init_wizard_alias_supports_setup_json_dry_run(tmp_path: Path) -> None:
    home = tmp_path / "brain"

    result = runner.invoke(app, ["init", "--wizard", "--dry-run", "--json", "--home", str(home)])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["role"] == "single"
    assert "planned_writes" in payload
    assert not home.exists()


def test_failed_sync_doctor_blocks_scheduler_install(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")

    result = run_setup_plan(
        paths,
        role="secondary",
        node_id="secondary",
        primary_node_id="primary",
        secondary_outbox_path=tmp_path / "outbox-without-node-id",
        install_scheduler=True,
    )

    assert result["applied"] is True
    assert result["scheduler_install_blocked"] is True
    assert result["scheduler_block_reason"] == "sync doctor failed"
    assert result["results"]["scheduler"]["blocked"] is True
    assert CAPTURE_SECONDARY_LABEL in result["planned_launch_agent_labels"]


def test_setup_never_writes_private_key_material(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")

    result = run_setup_plan(paths, role="single")

    assert result["applied"] is True
    private_key_markers = ["BEGIN OPENSSH PRIVATE KEY", "BEGIN RSA PRIVATE KEY", "PRIVATE KEY-----"]
    for path in paths.home.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert not any(marker in text for marker in private_key_markers)
