from __future__ import annotations

import shlex
from pathlib import Path

from .paths import BrainPaths
from .sync_config import PeerConfig
from .sync_ssh import ssh_options


PUSH_SOURCE_SUBDIRS = ("raw/", "wiki/", "memory/", "config/shared/")
PUSH_EXCLUDES = (
    "db/",
    "indexes/",
    "logs/",
    "*.sqlite",
    "*.sqlite-wal",
    "*.sqlite-shm",
    ".DS_Store",
    "cache/",
    "tmp/",
    "config/sync.yaml",
    "config/local/",
    "outbox/",
)


def build_pull(paths: BrainPaths, peer: PeerConfig, run_id: str) -> list[str]:
    require_ssh_peer(peer)
    if not peer.brain_home:
        raise ValueError(f"peer {peer.node_id} is missing brain_home")
    remote_source = remote_rsync_path(peer, peer.brain_home / "outbox" / peer.node_id)
    local_target = paths.inbox / "external" / peer.node_id / "_staging" / run_id
    return [
        "rsync",
        "-az",
        "--delete",
        "-e",
        rsync_ssh_command(paths, peer),
        ensure_trailing_slash(remote_source),
        ensure_trailing_slash(local_target),
    ]


def build_push(paths: BrainPaths, peer: PeerConfig, source_subdir: str) -> list[str]:
    require_ssh_peer(peer)
    if not peer.brain_home:
        raise ValueError(f"peer {peer.node_id} is missing brain_home")
    normalized = normalize_source_subdir(source_subdir)
    local_source = paths.home / normalized
    remote_target = remote_rsync_path(peer, peer.brain_home / normalized)
    argv = [
        "rsync",
        "-az",
        "--delete",
        "--delay-updates",
        "--partial-dir=.rsync-partial",
        "-e",
        rsync_ssh_command(paths, peer),
    ]
    for pattern in PUSH_EXCLUDES:
        argv.extend(["--exclude", pattern])
    argv.extend([ensure_trailing_slash(local_source), ensure_trailing_slash(remote_target)])
    return argv


def normalize_source_subdir(source_subdir: str) -> str:
    normalized = source_subdir.replace("\\", "/").strip("/")
    if normalized:
        normalized += "/"
    if normalized not in PUSH_SOURCE_SUBDIRS:
        raise ValueError(f"unsupported sync push source subdir: {source_subdir}")
    return normalized


def require_ssh_peer(peer: PeerConfig) -> None:
    if peer.transport != "ssh":
        raise ValueError(f"unsupported peer transport for {peer.node_id}: {peer.transport}")
    if not peer.host:
        raise ValueError(f"peer {peer.node_id} is missing host")
    if not peer.user:
        raise ValueError(f"peer {peer.node_id} is missing user")


def rsync_ssh_command(paths: BrainPaths, peer: PeerConfig) -> str:
    argv = ["ssh", *ssh_options(paths)]
    if peer.identity_path:
        argv.extend(["-i", str(peer.identity_path.expanduser())])
    return shlex.join(argv)


def remote_rsync_path(peer: PeerConfig, path: Path) -> str:
    require_ssh_peer(peer)
    return f"{peer.user}@{peer.host}:{path.expanduser()}"


def ensure_trailing_slash(path: str | Path) -> str:
    value = str(path)
    return value if value.endswith("/") else f"{value}/"
