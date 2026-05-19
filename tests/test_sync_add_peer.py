from __future__ import annotations

import json

from typer.testing import CliRunner

import pkm_brain.cli as cli_module
from pkm_brain.cli import app
from pkm_brain.paths import BrainPaths
from pkm_brain.sync_config import load_sync_config


runner = CliRunner()


def init_primary(home: str) -> None:
    result = runner.invoke(app, ["sync", "init-primary", "--node-id", "primary", "--yes", "--home", home])
    assert result.exit_code == 0, result.output


def test_add_peer_writes_peer_config(tmp_path) -> None:
    home = str(tmp_path / "primary")
    init_primary(home)

    result = runner.invoke(
        app,
        [
            "sync",
            "add-peer",
            "--node-id",
            "secondary",
            "--host",
            "secondary.local",
            "--user",
            "peter",
            "--brain-home",
            "/remote/brain",
            "--yes",
            "--home",
            home,
        ],
    )

    assert result.exit_code == 0, result.output
    config = load_sync_config(BrainPaths.from_value(home))
    assert config.primary is not None
    assert len(config.primary.peers) == 1
    peer = config.primary.peers[0]
    assert peer.node_id == "secondary"
    assert peer.host == "secondary.local"
    assert peer.user == "peter"


def test_add_peer_can_chain_connection_test(tmp_path, monkeypatch) -> None:
    class FakeConnectionResult:
        def as_dict(self) -> dict[str, object]:
            return {
                "local_role": "primary",
                "local_node_id": "primary",
                "peer_node_id": "secondary",
                "checks": {"ssh": "ok"},
                "ready": True,
            }

    home = str(tmp_path / "primary")
    init_primary(home)
    called: list[str] = []

    def fake_test_connection(paths, peer_node_id):
        called.append(peer_node_id)
        return FakeConnectionResult()

    monkeypatch.setattr(cli_module, "run_sync_test_connection", fake_test_connection)

    result = runner.invoke(
        app,
        [
            "sync",
            "add-peer",
            "--node-id",
            "secondary",
            "--host",
            "secondary.local",
            "--user",
            "peter",
            "--brain-home",
            "/remote/brain",
            "--test-connection",
            "--yes",
            "--home",
            home,
        ],
    )

    assert result.exit_code == 0, result.output
    assert called == ["secondary"]
    payload = json.loads(result.output)
    assert payload["connection_test"]["ready"] is True


def test_add_peer_refuses_duplicate_node_id(tmp_path) -> None:
    home = str(tmp_path / "primary")
    init_primary(home)
    args = [
        "sync",
        "add-peer",
        "--node-id",
        "secondary",
        "--host",
        "secondary.local",
        "--user",
        "peter",
        "--brain-home",
        "/remote/brain",
        "--yes",
        "--home",
        home,
    ]

    first = runner.invoke(app, args)
    second = runner.invoke(app, args)

    assert first.exit_code == 0, first.output
    assert second.exit_code != 0
    assert "already exists" in second.output


def test_add_peer_refuses_secondary_workspace(tmp_path) -> None:
    home = str(tmp_path / "secondary")
    init = runner.invoke(
        app,
        [
            "sync",
            "init-secondary",
            "--node-id",
            "secondary",
            "--primary-node-id",
            "primary",
            "--yes",
            "--home",
            home,
        ],
    )
    assert init.exit_code == 0, init.output

    result = runner.invoke(
        app,
        [
            "sync",
            "add-peer",
            "--node-id",
            "other",
            "--host",
            "other.local",
            "--user",
            "peter",
            "--brain-home",
            "/remote/brain",
            "--yes",
            "--home",
            home,
        ],
    )

    assert result.exit_code != 0
    assert "requires a primary workspace" in result.output
