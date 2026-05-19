from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .paths import BrainPaths
from .service import BrainService
from .sync_config import PeerConfig, load_sync_config
from .sync_connection import ProductionTransport, SubprocessResult, Transport, quote_path, run_remote
from .sync_rsync import PUSH_SOURCE_SUBDIRS, build_pull, build_push
from .util import file_sha256, new_id


@dataclass(frozen=True)
class SyncPullResult:
    peer_node_id: str
    run_id: str
    status: str
    staging_path: str
    promoted: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ingest: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class SyncPushResult:
    peer_node_id: str
    status: str
    pushed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class SyncRunResult:
    peer_node_id: str
    status: str
    pull: dict[str, Any] | None = None
    push: dict[str, Any] | None = None
    remote_ingest: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def sync_pull(
    paths: BrainPaths,
    peer_node_id: str,
    transport: Transport | None = None,
    run_id: str | None = None,
    run_ingest: bool = True,
) -> SyncPullResult:
    service = BrainService(paths, prefer_model_embeddings=False)
    service.init_workspace()
    peer = load_primary_peer(paths, peer_node_id)
    transport = transport or ProductionTransport()
    run_id = run_id or new_id("sync")
    staging = paths.inbox / "external" / peer.node_id / "_staging" / run_id
    staging.mkdir(parents=True, exist_ok=True)

    rsync_result = transport.rsync(build_pull(paths, peer, run_id))
    if rsync_result.returncode != 0:
        return SyncPullResult(
            peer_node_id=peer.node_id,
            run_id=run_id,
            status="failed",
            staging_path=str(staging),
            errors=[rsync_error("pull", rsync_result)],
        )

    try:
        promoted, rejected, errors = validate_and_promote_staging(paths, peer.node_id, staging)
    except ValueError as exc:
        return SyncPullResult(
            peer_node_id=peer.node_id,
            run_id=run_id,
            status="failed",
            staging_path=str(staging),
            errors=[str(exc)],
        )
    ingest_result = None
    if run_ingest:
        live_external = paths.inbox / "external" / peer.node_id
        ingest_result = service.ingest(live_external, origin_node_id=peer.node_id).as_dict()
    status = "ok"
    if errors:
        status = "ok_with_rejections"
    if ingest_result and ingest_result.get("errors"):
        status = "ok_with_ingest_errors"
    return SyncPullResult(
        peer_node_id=peer.node_id,
        run_id=run_id,
        status=status,
        staging_path=str(staging),
        promoted=promoted,
        rejected=rejected,
        errors=errors,
        ingest=ingest_result,
    )


def sync_push(paths: BrainPaths, peer_node_id: str, transport: Transport | None = None) -> SyncPushResult:
    service = BrainService(paths, prefer_model_embeddings=False)
    service.init_workspace()
    service.export_all_memories()
    peer = load_primary_peer(paths, peer_node_id)
    transport = transport or ProductionTransport()
    pushed: list[str] = []
    errors: list[str] = []
    for source_subdir in PUSH_SOURCE_SUBDIRS:
        result = transport.rsync(build_push(paths, peer, source_subdir))
        if result.returncode != 0:
            errors.append(rsync_error(f"push {source_subdir}", result))
            return SyncPushResult(peer.node_id, "failed", pushed=pushed, errors=errors)
        pushed.append(source_subdir)
    return SyncPushResult(peer.node_id, "ok", pushed=pushed, errors=errors)


def sync_run(
    paths: BrainPaths,
    peer_node_id: str,
    transport: Transport | None = None,
    remote_ingest: bool = True,
) -> SyncRunResult:
    transport = transport or ProductionTransport()
    peer = load_primary_peer(paths, peer_node_id)
    errors: list[str] = []
    pull = sync_pull(paths, peer_node_id, transport=transport)
    if pull.status == "failed":
        errors.extend(pull.errors)
        return SyncRunResult(peer_node_id, "failed", pull=pull.as_dict(), errors=errors)

    push = sync_push(paths, peer_node_id, transport=transport)
    if push.status != "ok":
        errors.extend(push.errors)
        return SyncRunResult(peer_node_id, "failed", pull=pull.as_dict(), push=push.as_dict(), errors=errors)

    remote_result = None
    status = "ok"
    if remote_ingest:
        command = f"brain ingest --home {quote_path(peer.brain_home)}"
        completed = run_remote(paths, peer, command, transport)
        remote_result = {
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        if completed.returncode != 0:
            status = "ok_with_remote_ingest_failure"
            errors.append(completed.stderr or completed.stdout or "remote ingest failed")
    return SyncRunResult(
        peer_node_id,
        status,
        pull=pull.as_dict(),
        push=push.as_dict(),
        remote_ingest=remote_result,
        errors=errors,
    )


def load_primary_peer(paths: BrainPaths, peer_node_id: str) -> PeerConfig:
    config = load_sync_config(paths)
    if config.role != "primary" or not config.primary:
        raise ValueError("sync transport commands require a primary workspace")
    peer = next((candidate for candidate in config.primary.peers if candidate.node_id == peer_node_id), None)
    if not peer:
        raise ValueError(f"peer not found: {peer_node_id}")
    return peer


def validate_and_promote_staging(paths: BrainPaths, peer_node_id: str, staging: Path) -> tuple[list[str], list[str], list[str]]:
    manifest = staging / "manifest.jsonl"
    if not manifest.exists():
        raise ValueError(f"missing manifest: {manifest}")

    live_external = paths.inbox / "external" / peer_node_id
    promoted: list[str] = []
    rejected: list[str] = []
    errors: list[str] = []
    for row in read_manifest(manifest):
        try:
            relative_path = safe_manifest_relative_path(str(row.get("relative_path") or ""))
        except ValueError as exc:
            errors.append(str(exc))
            continue
        expected_hash = row.get("content_hash") or row.get("sha256")
        staged_file = staging / relative_path
        if not staged_file.exists():
            errors.append(f"manifest file missing: {relative_path}")
            write_rejection(staging, relative_path, {"reason": "missing_file", "expected_hash": expected_hash})
            rejected.append(str(relative_path))
            continue
        observed_hash = file_sha256(staged_file)
        if expected_hash and observed_hash != expected_hash:
            reject_file(staging, relative_path, expected_hash=str(expected_hash), observed_hash=observed_hash)
            rejected.append(str(relative_path))
            errors.append(f"hash mismatch: {relative_path}")
            continue
        target = live_external / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        staged_file.replace(target)
        promoted.append(str(relative_path))

    try:
        manifest.unlink()
    except OSError:
        pass
    remove_empty_staging_dirs(staging)
    return promoted, rejected, errors


def read_manifest(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON manifest row: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"{path}:{line_number}: manifest row must be an object")
        rows.append(parsed)
    return rows


def safe_manifest_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe manifest relative_path: {value}")
    if path.parts and path.parts[0] in {"_staging", "_quarantine", "_rejected"}:
        raise ValueError(f"reserved manifest relative_path: {value}")
    return path


def reject_file(staging: Path, relative_path: PurePosixPath, expected_hash: str, observed_hash: str) -> None:
    source = staging / relative_path
    rejected = staging / "_rejected" / relative_path
    rejected.parent.mkdir(parents=True, exist_ok=True)
    source.replace(rejected)
    write_rejection(
        staging,
        relative_path,
        {
            "reason": "hash_mismatch",
            "expected_hash": expected_hash,
            "observed_hash": observed_hash,
        },
    )


def write_rejection(staging: Path, relative_path: PurePosixPath, payload: dict[str, Any]) -> None:
    error_path = staging / "_rejected" / PurePosixPath(f"{relative_path}.error.json")
    error_path.parent.mkdir(parents=True, exist_ok=True)
    error_path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def remove_empty_staging_dirs(staging: Path) -> None:
    if not staging.exists():
        return
    for path in sorted(staging.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    try:
        staging.rmdir()
    except OSError:
        pass


def rsync_error(action: str, result: SubprocessResult) -> str:
    detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    return f"{action} failed: {detail}"
