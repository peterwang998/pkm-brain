from __future__ import annotations

from pathlib import Path

from .paths import BrainPaths
from .service import BrainService
from .sync_config import PeerConfig, PrimaryConfig, SecondaryConfig, SyncConfig, load_sync_config, write_sync_config
from .sync_ssh import HostKeyCandidate, fingerprint, write_pinned_host_key


def init_primary(paths: BrainPaths, node_id: str, force: bool = False) -> dict[str, object]:
    ensure_can_write_sync_config(paths, force)
    BrainService(paths).init_workspace()
    (paths.inbox / "external").mkdir(parents=True, exist_ok=True)
    write_local_node_id(paths, node_id)
    config = SyncConfig(
        node_id=node_id,
        role="primary",
        brain_home=paths.home,
        primary=PrimaryConfig(peers=[]),
    )
    path = write_sync_config(paths, config)
    return {"path": str(path), "config": config.as_dict()}


def init_secondary(
    paths: BrainPaths,
    node_id: str,
    primary_node_id: str,
    outbox_path: Path | None = None,
    force: bool = False,
) -> dict[str, object]:
    ensure_can_write_sync_config(paths, force)
    BrainService(paths).init_workspace()
    resolved_outbox = (outbox_path or (paths.outbox / node_id)).expanduser().resolve()
    resolved_outbox.mkdir(parents=True, exist_ok=True)
    write_local_node_id(paths, node_id)
    config = SyncConfig(
        node_id=node_id,
        role="secondary",
        brain_home=paths.home,
        secondary=SecondaryConfig(
            primary_node_id=primary_node_id,
            outbox_enabled=True,
            outbox_path=resolved_outbox,
        ),
    )
    path = write_sync_config(paths, config)
    return {"path": str(path), "config": config.as_dict()}


def add_peer(
    paths: BrainPaths,
    node_id: str,
    host: str,
    user: str,
    brain_home: Path,
    outbox_path: Path | None = None,
    identity_path: Path | None = None,
    host_key_candidate: HostKeyCandidate | None = None,
) -> dict[str, object]:
    config = load_sync_config(paths)
    if config.role != "primary" or not config.primary:
        raise ValueError("sync add-peer requires a primary workspace")
    if any(peer.node_id == node_id for peer in config.primary.peers):
        raise ValueError(f"peer already exists: {node_id}")

    host_key_fingerprint = None
    if host_key_candidate:
        write_pinned_host_key(paths, host_key_candidate)
        host_key_fingerprint = fingerprint(host_key_candidate)

    peer = PeerConfig(
        node_id=node_id,
        role="secondary",
        host=host,
        user=user,
        brain_home=brain_home.expanduser(),
        outbox_path=outbox_path.expanduser() if outbox_path else None,
        transport="ssh",
        trust="lan-only",
        identity_path=identity_path.expanduser() if identity_path else None,
        host_key_fingerprint=host_key_fingerprint,
    )
    updated = SyncConfig(
        node_id=config.node_id,
        role="primary",
        brain_home=config.brain_home,
        primary=PrimaryConfig(peers=[*config.primary.peers, peer]),
    )
    path = write_sync_config(paths, updated)
    return {"path": str(path), "peer": peer.as_dict(), "config": updated.as_dict()}


def ensure_can_write_sync_config(paths: BrainPaths, force: bool) -> None:
    if paths.sync_config_file.exists() and not force:
        raise ValueError(f"sync config already exists: {paths.sync_config_file}")


def write_local_node_id(paths: BrainPaths, node_id: str) -> None:
    paths.config_local.mkdir(parents=True, exist_ok=True)
    paths.local_node_id_file.write_text(f"{node_id}\n", encoding="utf-8")
