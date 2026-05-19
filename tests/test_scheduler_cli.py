from __future__ import annotations

from typer.testing import CliRunner

from pkm_brain.cli import app


runner = CliRunner()


def test_scheduler_uninstall_sync_no_longer_accepts_peer_flag() -> None:
    help_result = runner.invoke(app, ["scheduler", "uninstall-sync", "--help"])
    invalid_result = runner.invoke(app, ["scheduler", "uninstall-sync", "--peer", "secondary"])

    assert help_result.exit_code == 0, help_result.output
    assert "--peer" not in help_result.output
    assert invalid_result.exit_code != 0
