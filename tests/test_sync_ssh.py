from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from pkm_brain.paths import BrainPaths
from pkm_brain.sync_config import PeerConfig
from pkm_brain.sync_ssh import (
    HostKeyMismatchError,
    HostKeyCandidate,
    build_ssh_argv,
    fingerprint,
    pinned_known_hosts_path,
    verify_peer_host_key_fingerprint,
    write_pinned_host_key,
)


def test_fingerprint_returns_sha256_form(tmp_path: Path) -> None:
    if not shutil.which("ssh-keygen"):
        pytest.skip("ssh-keygen is required for fingerprint validation")
    key_path = tmp_path / "test-key"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    public_key = key_path.with_suffix(".pub").read_text(encoding="utf-8")

    value = fingerprint(public_key)

    assert value.startswith("SHA256:")


def test_pinned_known_hosts_written_under_config_local(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    candidate = HostKeyCandidate("secondary.local", "ssh-ed25519", "AAAATEST", "secondary.local ssh-ed25519 AAAATEST")

    path = write_pinned_host_key(paths, candidate)

    assert path == paths.config_local / "known_hosts"
    assert pinned_known_hosts_path(paths) == path
    assert path.read_text(encoding="utf-8") == "secondary.local ssh-ed25519 AAAATEST\n"


def test_ssh_argv_uses_pinned_known_hosts_and_batch_mode(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    peer = PeerConfig(
        node_id="secondary",
        host="secondary.local",
        user="peter",
        brain_home=Path("/remote/brain"),
        identity_path=Path("/keys/brain"),
    )

    argv = build_ssh_argv(paths, peer, "true")

    joined = " ".join(argv)
    assert "StrictHostKeyChecking=yes" in argv
    assert "BatchMode=yes" in argv
    assert f"UserKnownHostsFile={paths.config_local / 'known_hosts'}" in argv
    assert "~/.ssh/known_hosts" not in joined
    assert argv[-2:] == ["peter@secondary.local", "true"]


def test_verify_peer_host_key_fingerprint_passes_on_match() -> None:
    peer = PeerConfig(
        node_id="secondary",
        host="secondary.local",
        user="peter",
        host_key_fingerprint="SHA256:pinned",
    )
    candidate = HostKeyCandidate("secondary.local", "ssh-ed25519", "AAAATEST", "secondary.local ssh-ed25519 AAAATEST")

    verify_peer_host_key_fingerprint(peer, fetcher=lambda host: [candidate], fingerprinter=lambda key: "SHA256:pinned")


def test_verify_peer_host_key_fingerprint_reports_pinned_vs_observed() -> None:
    peer = PeerConfig(
        node_id="secondary",
        host="secondary.local",
        user="peter",
        host_key_fingerprint="SHA256:pinned",
    )
    candidate = HostKeyCandidate("secondary.local", "ssh-ed25519", "AAAATEST", "secondary.local ssh-ed25519 AAAATEST")

    with pytest.raises(HostKeyMismatchError, match="pinned SHA256:pinned; observed SHA256:observed"):
        verify_peer_host_key_fingerprint(peer, fetcher=lambda host: [candidate], fingerprinter=lambda key: "SHA256:observed")
