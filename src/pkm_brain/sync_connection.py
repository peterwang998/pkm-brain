from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .paths import BrainPaths
from .sync_config import PeerConfig, load_sync_config
from .sync_ssh import build_ssh_argv


CONNECTION_CHECK_KEYS = ["ssh", "remote_brain", "remote_role", "remote_outbox_probe", "rsync"]


@dataclass(frozen=True)
class SubprocessResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Transport(Protocol):
    def run(self, host: str, argv: list[str]) -> SubprocessResult:
        ...

    def rsync(self, args: list[str]) -> SubprocessResult:
        ...


class ProductionTransport:
    def run(self, host: str, argv: list[str]) -> SubprocessResult:
        completed = subprocess.run(argv, check=False, capture_output=True, text=True)
        return SubprocessResult(completed.returncode, completed.stdout, completed.stderr)

    def rsync(self, args: list[str]) -> SubprocessResult:
        completed = subprocess.run(args, check=False, capture_output=True, text=True)
        return SubprocessResult(completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True)
class ConnectionTestResult:
    local_role: str
    local_node_id: str
    peer_node_id: str
    checks: dict[str, str]
    ready: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "local_role": self.local_role,
            "local_node_id": self.local_node_id,
            "peer_node_id": self.peer_node_id,
            "checks": self.checks,
            "ready": self.ready,
        }


def test_connection(
    paths: BrainPaths,
    peer_node_id: str,
    transport: Transport | None = None,
) -> ConnectionTestResult:
    config = load_sync_config(paths)
    if config.role != "primary" or not config.primary:
        raise ValueError("sync test-connection requires a primary workspace")
    peer = next((candidate for candidate in config.primary.peers if candidate.node_id == peer_node_id), None)
    if not peer:
        raise ValueError(f"peer not found: {peer_node_id}")
    if not peer.host:
        raise ValueError(f"peer {peer_node_id} is missing host")
    transport = transport or ProductionTransport()
    checks = {key: "not_run" for key in CONNECTION_CHECK_KEYS}

    if run_remote(paths, peer, "true", transport).returncode != 0:
        checks["ssh"] = "fail"
        return connection_result(config.role, config.node_id, peer_node_id, checks)
    checks["ssh"] = "ok"

    if run_remote(paths, peer, "command -v brain", transport).returncode != 0:
        checks["remote_brain"] = "fail"
        return connection_result(config.role, config.node_id, peer_node_id, checks)
    checks["remote_brain"] = "ok"

    doctor = run_remote(paths, peer, f"brain sync doctor --json --home {quote_path(peer.brain_home)}", transport)
    if doctor.returncode != 0 or not remote_doctor_matches(peer, doctor.stdout):
        checks["remote_role"] = "fail"
    else:
        checks["remote_role"] = "ok"

    outbox_probe = run_remote(paths, peer, outbox_probe_command(peer), transport)
    checks["remote_outbox_probe"] = "ok" if outbox_probe.returncode == 0 else "fail"

    local_rsync = transport.rsync(["rsync", "--version"])
    remote_rsync = run_remote(paths, peer, "rsync --version", transport)
    checks["rsync"] = "ok" if local_rsync.returncode == 0 and remote_rsync.returncode == 0 else "fail"

    return connection_result(config.role, config.node_id, peer_node_id, checks)


def run_remote(paths: BrainPaths, peer: PeerConfig, command: str, transport: Transport) -> SubprocessResult:
    return transport.run(peer.host or "", build_ssh_argv(paths, peer, command))


def connection_result(local_role: str, local_node_id: str, peer_node_id: str, checks: dict[str, str]) -> ConnectionTestResult:
    return ConnectionTestResult(
        local_role=local_role,
        local_node_id=local_node_id,
        peer_node_id=peer_node_id,
        checks=checks,
        ready=all(status == "ok" for status in checks.values()),
    )


def remote_doctor_matches(peer: PeerConfig, stdout: str) -> bool:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return False
    expected_home = str(peer.brain_home.expanduser()) if peer.brain_home else None
    return (
        payload.get("role") == "secondary"
        and payload.get("node_id") == peer.node_id
        and (expected_home is None or str(Path(str(payload.get("brain_home"))).expanduser()) == expected_home)
    )


def outbox_probe_command(peer: PeerConfig) -> str:
    if not peer.brain_home:
        raise ValueError(f"peer {peer.node_id} is missing brain_home")
    outbox = peer.brain_home.expanduser() / "outbox" / peer.node_id
    probe = f"_probe-{peer.node_id}"
    probe_path = outbox / probe
    return (
        f"mkdir -p {quote_path(outbox)} && "
        f"printf ok > {quote_path(probe_path)} && "
        f"test \"$(cat {quote_path(probe_path)})\" = ok && "
        f"rm {quote_path(probe_path)}"
    )


def quote_path(path: Path | None) -> str:
    if path is None:
        return "''"
    return shlex.quote(str(path.expanduser()))
