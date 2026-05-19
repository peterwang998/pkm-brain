from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from pkm_brain.cli import app
from pkm_brain.paths import BrainPaths
from pkm_brain.sync_config import load_sync_config


runner = CliRunner()


def test_init_primary_yes_writes_valid_config(tmp_path: Path) -> None:
    home = tmp_path / "primary"

    result = runner.invoke(app, ["sync", "init-primary", "--node-id", "primary-test", "--yes", "--home", str(home)])

    assert result.exit_code == 0, result.output
    paths = BrainPaths.from_value(home)
    config = load_sync_config(paths)
    assert config.role == "primary"
    assert config.node_id == "primary-test"
    assert paths.local_node_id_file.read_text(encoding="utf-8") == "primary-test\n"
    assert (paths.inbox / "external").is_dir()


def test_init_primary_refuses_existing_config_without_force(tmp_path: Path) -> None:
    home = tmp_path / "primary"
    first = runner.invoke(app, ["sync", "init-primary", "--node-id", "primary-test", "--yes", "--home", str(home)])
    second = runner.invoke(app, ["sync", "init-primary", "--node-id", "primary-test", "--yes", "--home", str(home)])

    assert first.exit_code == 0, first.output
    assert second.exit_code != 0
    assert "already exists" in second.output


def test_init_secondary_requires_primary_node_id_with_yes(tmp_path: Path) -> None:
    home = tmp_path / "secondary"

    result = runner.invoke(app, ["sync", "init-secondary", "--node-id", "secondary-test", "--yes", "--home", str(home)])

    assert result.exit_code != 0
    assert "--primary-node-id is required with --yes" in result.output


def test_init_secondary_writes_outbox_config(tmp_path: Path) -> None:
    home = tmp_path / "secondary"

    result = runner.invoke(
        app,
        [
            "sync",
            "init-secondary",
            "--node-id",
            "secondary-test",
            "--primary-node-id",
            "primary-test",
            "--yes",
            "--home",
            str(home),
        ],
    )

    assert result.exit_code == 0, result.output
    paths = BrainPaths.from_value(home)
    config = load_sync_config(paths)
    assert config.role == "secondary"
    assert config.secondary is not None
    assert config.secondary.primary_node_id == "primary-test"
    assert config.secondary.outbox_path == paths.outbox / "secondary-test"
    assert config.secondary.outbox_path.is_dir()
