from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from pkm_brain.cli import app, ui_startup_lines
from pkm_brain.ui_server import validate_ui_bind


runner = CliRunner()


def test_ui_default_bind_is_loopback() -> None:
    result = validate_ui_bind("127.0.0.1")

    assert result["host"] == "127.0.0.1"
    assert result["default_host"] == "127.0.0.1"
    assert result["warning"] is None


def test_ui_lan_bind_refuses_without_override(tmp_path: Path) -> None:
    result = runner.invoke(app, ["ui", "--host", "0.0.0.0", "--dry-run", "--home", str(tmp_path / "brain")])

    assert result.exit_code != 0
    assert "binds-to-lan" in result.output


def test_ui_lan_bind_allows_with_warning(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "ui",
            "--host",
            "0.0.0.0",
            "--port",
            "18766",
            "--i-understand-this-binds-to-lan",
            "--dry-run",
            "--home",
            str(tmp_path / "brain"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "warning" in result.output.lower()
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["host"] == "0.0.0.0"
    assert payload["port"] == 18766


def test_ui_startup_lines_print_copyable_token() -> None:
    lines = ui_startup_lines("127.0.0.1", 8765, "copy-me")

    assert lines == [
        "Brain UI listening on http://127.0.0.1:8765",
        "Token: copy-me",
    ]
    assert "Token file" not in "\n".join(lines)
