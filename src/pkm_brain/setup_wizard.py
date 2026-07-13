from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .paths import BrainPaths
from .scheduler.launchd import CAPTURE_SECONDARY_LABEL, SYNC_PRIMARY_LABEL, LaunchdScheduler, secondary_capture_job, sync_primary_job
from .service import BrainService
from .sync_connection import test_connection
from .sync_setup import add_peer, init_primary, init_secondary


VALID_SETUP_ROLES = {"single", "primary", "secondary"}


def build_setup_plan(
    paths: BrainPaths,
    *,
    role: str = "single",
    node_id: str | None = None,
    primary_node_id: str | None = None,
    peer_node_id: str | None = None,
    peer_host: str | None = None,
    peer_user: str | None = None,
    peer_brain_home: Path | None = None,
    peer_outbox_path: Path | None = None,
    identity_path: Path | None = None,
    secondary_outbox_path: Path | None = None,
    install_scheduler: bool = False,
    interval: int = 1800,
    repo_path: Path | None = None,
) -> dict[str, Any]:
    role = normalize_role(role)
    repo_path = (repo_path or Path.cwd()).resolve()
    planned_writes = workspace_planned_writes(paths)
    planned_validations = ["doctor"]
    planned_labels: list[str] = []
    planned_commands = [f"brain init --home {paths.home}"]

    if role == "primary":
        planned_writes.extend(
            [
                {"type": "file", "path": str(paths.sync_config_file)},
                {"type": "file", "path": str(paths.local_node_id_file)},
                {"type": "directory", "path": str(paths.inbox / "external")},
            ]
        )
        planned_commands.append(f"brain sync init-primary --node-id {node_id or '<primary-node-id>'} --home {paths.home}")
        planned_validations.append("sync doctor")
        if peer_node_id:
            planned_commands.append(f"brain sync add-peer --node-id {peer_node_id} --host {peer_host or '<host>'} --home {paths.home}")
            planned_validations.append(f"sync test-connection {peer_node_id}")
        if install_scheduler:
            planned_labels.append(SYNC_PRIMARY_LABEL)
    elif role == "secondary":
        outbox = secondary_outbox_path or paths.outbox / (node_id or "<secondary-node-id>")
        planned_writes.extend(
            [
                {"type": "file", "path": str(paths.sync_config_file)},
                {"type": "file", "path": str(paths.local_node_id_file)},
                {"type": "directory", "path": str(outbox)},
            ]
        )
        planned_commands.append(
            "brain sync init-secondary "
            f"--node-id {node_id or '<secondary-node-id>'} "
            f"--primary-node-id {primary_node_id or '<primary-node-id>'} "
            f"--home {paths.home}"
        )
        planned_validations.append("sync doctor")
        if install_scheduler:
            planned_labels.append(CAPTURE_SECONDARY_LABEL)

    if install_scheduler:
        if role == "primary":
            planned_commands.append(
                f"brain scheduler install-sync --peer {peer_node_id or '<secondary-node-id>'} "
                f"--interval {interval} --home {paths.home}"
            )
        elif role == "secondary":
            planned_commands.append(f"brain scheduler install-secondary-capture --interval {interval} --home {paths.home}")

    return {
        "brain_home": str(paths.home),
        "role": role,
        "node_id": node_id,
        "primary_node_id": primary_node_id,
        "peer": peer_plan(peer_node_id, peer_host, peer_user, peer_brain_home, peer_outbox_path, identity_path),
        "secondary_outbox_path": str(secondary_outbox_path) if secondary_outbox_path else None,
        "planned_writes": dedupe_planned_writes(planned_writes),
        "validation_steps": planned_validations,
        "planned_launch_agent_labels": planned_labels,
        "planned_commands": planned_commands,
        "scheduler": {
            "install_requested": install_scheduler,
            "interval": interval,
            "repo_path": str(repo_path),
        },
    }


def run_setup_plan(
    paths: BrainPaths,
    *,
    role: str = "single",
    node_id: str | None = None,
    primary_node_id: str | None = None,
    peer_node_id: str | None = None,
    peer_host: str | None = None,
    peer_user: str | None = None,
    peer_brain_home: Path | None = None,
    peer_outbox_path: Path | None = None,
    identity_path: Path | None = None,
    secondary_outbox_path: Path | None = None,
    install_scheduler: bool = False,
    interval: int = 1800,
    dry_run: bool = False,
    force: bool = False,
    repo_path: Path | None = None,
) -> dict[str, Any]:
    role = normalize_role(role)
    plan = build_setup_plan(
        paths,
        role=role,
        node_id=node_id,
        primary_node_id=primary_node_id,
        peer_node_id=peer_node_id,
        peer_host=peer_host,
        peer_user=peer_user,
        peer_brain_home=peer_brain_home,
        peer_outbox_path=peer_outbox_path,
        identity_path=identity_path,
        secondary_outbox_path=secondary_outbox_path,
        install_scheduler=install_scheduler,
        interval=interval,
        repo_path=repo_path,
    )
    result = {
        **plan,
        "dry_run": dry_run,
        "applied": False,
        "results": {},
        "scheduler_install_blocked": False,
        "scheduler_block_reason": None,
    }
    if dry_run:
        return result

    svc = BrainService(paths)
    svc.init_workspace()
    result["results"]["doctor"] = svc.doctor()

    if role == "primary":
        require_value(node_id, "--node-id")
        result["results"]["sync_init"] = init_primary(paths, node_id, force=force)
        if any([peer_node_id, peer_host, peer_user, peer_brain_home, peer_outbox_path, identity_path]):
            require_peer_fields(peer_node_id, peer_host, peer_user, peer_brain_home)
            result["results"]["add_peer"] = add_peer(
                paths,
                peer_node_id or "",
                peer_host or "",
                peer_user or "",
                peer_brain_home or Path(),
                outbox_path=peer_outbox_path,
                identity_path=identity_path,
            )
    elif role == "secondary":
        require_value(node_id, "--node-id")
        require_value(primary_node_id, "--primary-node-id")
        result["results"]["sync_init"] = init_secondary(
            paths,
            node_id,
            primary_node_id or "",
            outbox_path=secondary_outbox_path,
            force=force,
        )

    if role in {"primary", "secondary"}:
        sync_doctor = BrainService(paths).sync_doctor()
        result["results"]["sync_doctor"] = sync_doctor
        if not sync_doctor["ready"]:
            result["scheduler_install_blocked"] = True
            result["scheduler_block_reason"] = "sync doctor failed"

    if role == "primary" and peer_node_id and not result["scheduler_install_blocked"]:
        connection_result = test_connection(paths, peer_node_id).as_dict()
        result["results"]["test_connection"] = connection_result
        if not connection_result["ready"]:
            result["scheduler_install_blocked"] = True
            result["scheduler_block_reason"] = "sync test-connection failed"

    if install_scheduler:
        scheduler_result = maybe_install_scheduler(
            paths,
            role=role,
            peer_node_id=peer_node_id,
            interval=interval,
            repo_path=repo_path or Path.cwd(),
            blocked=result["scheduler_install_blocked"],
            block_reason=result["scheduler_block_reason"],
        )
        result["results"]["scheduler"] = scheduler_result
        if scheduler_result.get("blocked"):
            result["scheduler_install_blocked"] = True
            result["scheduler_block_reason"] = scheduler_result.get("reason")

    result["applied"] = True
    return result


def maybe_install_scheduler(
    paths: BrainPaths,
    *,
    role: str,
    peer_node_id: str | None,
    interval: int,
    repo_path: Path,
    blocked: bool,
    block_reason: str | None,
) -> dict[str, Any]:
    if blocked:
        return {"installed": False, "blocked": True, "reason": block_reason}
    uv = Path(shutil.which("uv") or "/opt/homebrew/bin/uv")
    if role == "primary":
        if not peer_node_id:
            return {"installed": False, "blocked": True, "reason": "primary scheduler requires a peer node_id"}
        job = sync_primary_job(repo_path.resolve(), paths.home, uv, peer_node_id, interval=interval)
    elif role == "secondary":
        job = secondary_capture_job(repo_path.resolve(), paths.home, uv, interval=interval)
    else:
        return {"installed": False, "blocked": True, "reason": "single-machine setup has no sync scheduler"}
    return {**LaunchdScheduler().install(job), "blocked": False}


def workspace_planned_writes(paths: BrainPaths) -> list[dict[str, str]]:
    return [
        *[{"type": "directory", "path": str(path)} for path in paths.directories()],
        {"type": "file", "path": str(paths.sqlite_path)},
        {"type": "file", "path": str(paths.config_file)},
        {"type": "file", "path": str(paths.golden_queries_file)},
    ]


def peer_plan(
    node_id: str | None,
    host: str | None,
    user: str | None,
    brain_home: Path | None,
    outbox_path: Path | None,
    identity_path: Path | None,
) -> dict[str, str | None]:
    return {
        "node_id": node_id,
        "host": host,
        "user": user,
        "brain_home": str(brain_home) if brain_home else None,
        "outbox_path": str(outbox_path) if outbox_path else None,
        "identity_path": str(identity_path) if identity_path else None,
    }


def dedupe_planned_writes(writes: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, str]] = []
    for write in writes:
        key = (write["type"], write["path"])
        if key in seen:
            continue
        seen.add(key)
        result.append(write)
    return result


def normalize_role(role: str) -> str:
    normalized = role.strip().lower()
    if normalized not in VALID_SETUP_ROLES:
        raise ValueError(f"role must be one of: {', '.join(sorted(VALID_SETUP_ROLES))}")
    return normalized


def require_value(value: str | None, flag: str) -> None:
    if value is None or not value.strip():
        raise ValueError(f"{flag} is required")


def require_peer_fields(
    peer_node_id: str | None,
    peer_host: str | None,
    peer_user: str | None,
    peer_brain_home: Path | None,
) -> None:
    require_value(peer_node_id, "--peer-node-id")
    require_value(peer_host, "--peer-host")
    require_value(peer_user, "--peer-user")
    if peer_brain_home is None:
        raise ValueError("--peer-brain-home is required")
