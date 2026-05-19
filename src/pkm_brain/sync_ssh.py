from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .paths import BrainPaths
from .sync_config import PeerConfig


@dataclass(frozen=True)
class HostKeyCandidate:
    host: str
    key_type: str
    key: str
    line: str


def pinned_known_hosts_path(home: str | Path | BrainPaths) -> Path:
    paths = home if isinstance(home, BrainPaths) else BrainPaths.from_value(home)
    return paths.config_local / "known_hosts"


def fetch_host_keys(host: str) -> list[HostKeyCandidate]:
    completed = subprocess.run(
        ["ssh-keyscan", "-t", "ed25519,rsa", "-T", "5", host],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0 and not completed.stdout.strip():
        raise RuntimeError(completed.stderr.strip() or f"ssh-keyscan failed for {host}")
    candidates: list[HostKeyCandidate] = []
    for line in completed.stdout.splitlines():
        candidate = parse_host_key_line(line)
        if candidate:
            candidates.append(candidate)
    return candidates


def parse_host_key_line(line: str) -> HostKeyCandidate | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    parts = stripped.split()
    if len(parts) < 3:
        return None
    return HostKeyCandidate(host=parts[0], key_type=parts[1], key=parts[2], line=" ".join(parts[:3]))


def fingerprint(candidate_or_line: HostKeyCandidate | str) -> str:
    line = candidate_or_line.line if isinstance(candidate_or_line, HostKeyCandidate) else candidate_or_line.strip()
    completed = subprocess.run(
        ["ssh-keygen", "-lf", "-"],
        input=f"{line}\n",
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "ssh-keygen fingerprint failed")
    parts = completed.stdout.split()
    for part in parts:
        if part.startswith("SHA256:"):
            return part
    raise RuntimeError(f"could not parse SHA256 fingerprint from ssh-keygen output: {completed.stdout.strip()}")


def write_pinned_host_key(home: str | Path | BrainPaths, candidate: HostKeyCandidate) -> Path:
    path = pinned_known_hosts_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    if candidate.line not in existing:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{candidate.line}\n")
    return path


def ssh_options(home: str | Path | BrainPaths) -> list[str]:
    known_hosts = pinned_known_hosts_path(home)
    return [
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
    ]


def build_ssh_argv(home: str | Path | BrainPaths, peer: PeerConfig, command: str) -> list[str]:
    if not peer.host:
        raise ValueError(f"peer {peer.node_id} is missing host")
    if not peer.user:
        raise ValueError(f"peer {peer.node_id} is missing user")
    argv = ["ssh", *ssh_options(home)]
    if peer.identity_path:
        argv.extend(["-i", str(peer.identity_path)])
    argv.extend([f"{peer.user}@{peer.host}", command])
    return argv


def first_host_key_with_fingerprint(host: str) -> tuple[HostKeyCandidate, str]:
    candidates = fetch_host_keys(host)
    if not candidates:
        raise RuntimeError(f"no host keys found for {host}")
    candidate = candidates[0]
    return candidate, fingerprint(candidate)
