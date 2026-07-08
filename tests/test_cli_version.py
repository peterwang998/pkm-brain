from __future__ import annotations

from typer.testing import CliRunner

from pkm_brain.cli import app
from pkm_brain.daemon import package_version


runner = CliRunner()


def test_brain_version_option() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert package_version() in result.stdout
