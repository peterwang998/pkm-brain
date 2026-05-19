from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from fake_transport import FakeTransport
from pkm_brain.cli import app
from pkm_brain.paths import BrainPaths
from pkm_brain.sync_connection import test_connection as run_connection_test
from pkm_brain.sync_ssh import HostKeyMismatchError
from pkm_brain.sync_setup import add_peer, init_primary


runner = CliRunner()


def primary_with_peer(tmp_path: Path) -> tuple[BrainPaths, Path]:
    paths = BrainPaths.from_value(tmp_path / "primary")
    remote_home = tmp_path / "secondary"
    init_primary(paths, "primary")
    add_peer(paths, "secondary", "secondary.local", "peter", remote_home)
    return paths, remote_home


def test_connection_passes_with_compliant_fake(tmp_path: Path) -> None:
    paths, remote_home = primary_with_peer(tmp_path)
    fake = FakeTransport(remote_node_id="secondary", remote_role="secondary", remote_home=remote_home)

    result = run_connection_test(paths, "secondary", transport=fake).as_dict()

    assert result == {
        "local_role": "primary",
        "local_node_id": "primary",
        "peer_node_id": "secondary",
        "checks": {
            "ssh": "ok",
            "remote_brain": "ok",
            "remote_role": "ok",
            "remote_outbox_probe": "ok",
            "rsync": "ok",
        },
        "ready": True,
    }


def test_connection_fails_on_remote_role_mismatch(tmp_path: Path) -> None:
    paths, remote_home = primary_with_peer(tmp_path)
    fake = FakeTransport(remote_node_id="secondary", remote_role="primary", remote_home=remote_home)

    result = run_connection_test(paths, "secondary", transport=fake).as_dict()

    assert result["ready"] is False
    assert result["checks"]["remote_role"] == "fail"


def test_connection_fails_on_remote_node_mismatch(tmp_path: Path) -> None:
    paths, remote_home = primary_with_peer(tmp_path)
    fake = FakeTransport(remote_node_id="wrong-node", remote_role="secondary", remote_home=remote_home)

    result = run_connection_test(paths, "secondary", transport=fake).as_dict()

    assert result["ready"] is False
    assert result["checks"]["remote_role"] == "fail"


def test_connection_fails_on_missing_outbox_probe(tmp_path: Path) -> None:
    paths, remote_home = primary_with_peer(tmp_path)
    fake = FakeTransport(remote_node_id="secondary", remote_role="secondary", remote_home=remote_home, outbox_probe=False)

    result = run_connection_test(paths, "secondary", transport=fake).as_dict()

    assert result["ready"] is False
    assert result["checks"]["remote_outbox_probe"] == "fail"


def test_connection_fails_on_missing_rsync(tmp_path: Path) -> None:
    paths, remote_home = primary_with_peer(tmp_path)
    fake = FakeTransport(remote_node_id="secondary", remote_role="secondary", remote_home=remote_home, remote_rsync=False)

    result = run_connection_test(paths, "secondary", transport=fake).as_dict()

    assert result["ready"] is False
    assert result["checks"]["rsync"] == "fail"


def test_connection_reports_host_key_mismatch(tmp_path: Path) -> None:
    paths, remote_home = primary_with_peer(tmp_path)
    fake = FakeTransport(remote_node_id="secondary", remote_role="secondary", remote_home=remote_home)

    def verifier(peer):
        raise HostKeyMismatchError(peer.host or "secondary.local", "SHA256:pinned", ["SHA256:observed"])

    try:
        run_connection_test(paths, "secondary", transport=fake, host_key_verifier=verifier)
    except ValueError as exc:
        assert "pinned SHA256:pinned; observed SHA256:observed" in str(exc)
    else:
        raise AssertionError("expected host key mismatch")


def test_connection_help_exposes_json_flag() -> None:
    result = runner.invoke(app, ["sync", "test-connection", "--help"])

    assert result.exit_code == 0
    assert "--json" in result.output
