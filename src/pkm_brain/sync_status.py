from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .db import connection, loads
from .paths import BrainPaths
from .sync_config import PeerConfig, load_sync_config
from .sync_connection import ProductionTransport, SubprocessResult, Transport, quote_path, run_remote
from .util import file_sha256


CANONICAL_SUBDIRS = ("raw", "wiki", "memory", "config/shared")
CANONICAL_EXCLUDED_PARTS = {"db", "indexes", "logs", "outbox", "config/local"}


def sync_status(paths: BrainPaths, transport: Transport | None = None) -> dict[str, Any]:
    try:
        config = load_sync_config(paths)
    except FileNotFoundError:
        return {"configured": False, "role": None, "node_id": None, "peers": [], "warnings": ["sync config missing"]}

    peers = config.primary.peers if config.primary else []
    transport = transport or ProductionTransport()
    peer_statuses = [status_for_peer(paths, peer, transport) for peer in peers]
    warnings = [warning for peer_status in peer_statuses for warning in peer_status.get("warnings", [])]
    return {
        "configured": True,
        "role": config.role,
        "node_id": config.node_id,
        "peers": peer_statuses,
        "warnings": warnings,
    }


def status_for_peer(paths: BrainPaths, peer: PeerConfig, transport: Transport) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        last_successful_pull = latest_sync_row(
            conn,
            peer.node_id,
            "direction IN ('pull', 'run') AND status IN ('ok', 'ok_with_remote_ingest_failure')",
        )
        last_successful_push = latest_sync_row(
            conn,
            peer.node_id,
            "direction IN ('push', 'run') AND status IN ('ok', 'ok_with_remote_ingest_failure')",
        )
        last_failed_run = latest_sync_row(conn, peer.node_id, "status = 'failed'")
        last_run = latest_sync_row(conn, peer.node_id, "1=1")

    primary_hash = canonical_manifest_hash(paths.home)
    remote_snapshot = remote_sync_snapshot(paths, peer, transport)
    remote_hash = remote_snapshot.get("canonical_manifest_hash")
    mirror_current = remote_hash is not None and primary_hash == remote_hash
    warnings: list[str] = []
    if remote_snapshot.get("error"):
        warnings.append(f"remote status unavailable for {peer.node_id}: {remote_snapshot['error']}")
    if remote_hash is not None and primary_hash != remote_hash:
        warnings.append(f"mirror divergence for {peer.node_id}: primary canonical hash differs from remote mirror")
    pending_outbox_count = remote_snapshot.get("pending_outbox_count")
    return {
        "peer_node_id": peer.node_id,
        "host": peer.host,
        "last_run": last_run,
        "last_successful_pull": last_successful_pull,
        "last_successful_push": last_successful_push,
        "last_failed_run": summarize_failed_run(last_failed_run),
        "pending_outbox_count": pending_outbox_count,
        "canonical_manifest_hash": primary_hash,
        "remote_manifest_hash": remote_hash,
        "remote_outbox_path": remote_snapshot.get("outbox_path"),
        "mirror_current": mirror_current,
        "warnings": warnings,
    }


def latest_sync_row(conn: Any, peer_node_id: str, where: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"""
        SELECT *
        FROM sync_runs
        WHERE peer_node_id = ? AND {where}
        ORDER BY COALESCE(finished_at, started_at) DESC
        LIMIT 1
        """,
        (peer_node_id,),
    ).fetchone()
    return row_to_sync_run(row) if row else None


def row_to_sync_run(row: Any) -> dict[str, Any]:
    payload = dict(row)
    payload["errors"] = loads(payload.get("errors"), [])
    return payload


def summarize_failed_run(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    errors = row.get("errors") or []
    return {
        "id": row["id"],
        "direction": row["direction"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "error_summary": "; ".join(str(error) for error in errors[:3]),
        "errors": errors,
    }


def remote_sync_snapshot(paths: BrainPaths, peer: PeerConfig, transport: Transport) -> dict[str, Any]:
    if not peer.brain_home:
        return {"error": "peer is missing brain_home"}
    try:
        completed = run_remote(paths, peer, f"brain sync mirror-hash --json --home {quote_path(peer.brain_home)}", transport)
    except Exception as exc:
        return {"error": str(exc)}
    if completed.returncode != 0:
        return {"error": remote_error_detail(completed)}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"error": f"invalid remote mirror-hash JSON: {exc}"}
    return payload if isinstance(payload, dict) else {"error": "remote mirror-hash JSON must be an object"}


def remote_error_detail(completed: SubprocessResult) -> str:
    return completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"


def local_sync_snapshot(paths: BrainPaths) -> dict[str, Any]:
    outbox_path = local_secondary_outbox_path(paths)
    return {
        "brain_home": str(paths.home),
        "canonical_manifest_hash": canonical_manifest_hash(paths.home),
        "pending_outbox_count": local_pending_outbox_manifest_count(paths, outbox_path),
        "outbox_path": str(outbox_path) if outbox_path else None,
    }


def local_secondary_outbox_path(paths: BrainPaths) -> Path | None:
    try:
        config = load_sync_config(paths)
    except FileNotFoundError:
        return None
    if config.role == "secondary" and config.secondary and config.secondary.outbox_path:
        return config.secondary.outbox_path
    if config.role == "secondary":
        return paths.outbox / config.node_id
    return None


def local_pending_outbox_manifest_count(paths: BrainPaths, outbox_path: Path | None = None) -> int | None:
    if outbox_path:
        return manifest_row_count(outbox_path / "manifest.jsonl")
    if not paths.outbox.exists():
        return None
    total = 0
    found = False
    for manifest in sorted(paths.outbox.glob("*/manifest.jsonl")):
        count = manifest_row_count(manifest)
        if count is None:
            continue
        found = True
        total += count
    return total if found else None


def manifest_row_count(manifest: Path) -> int | None:
    if not manifest.exists():
        return None
    count = 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if line.strip():
            count += 1
    return count


def canonical_manifest_hash(home: Path | None) -> str | None:
    if home is None:
        return None
    root = home.expanduser()
    if not root.exists():
        return None
    h = hashlib.sha256()
    seen = False
    for subdir in CANONICAL_SUBDIRS:
        base = root / subdir
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or canonical_path_excluded(path, root):
                continue
            relative = path.relative_to(root).as_posix()
            h.update(relative.encode("utf-8"))
            h.update(b"\0")
            h.update(file_sha256(path).encode("ascii"))
            h.update(b"\n")
            seen = True
    return h.hexdigest() if seen else hashlib.sha256(b"").hexdigest()


def canonical_path_excluded(path: Path, root: Path) -> bool:
    relative_parts = path.relative_to(root).parts
    joined = "/".join(relative_parts)
    if any(part in CANONICAL_EXCLUDED_PARTS for part in relative_parts):
        return True
    if joined.startswith("config/sync.yaml") or joined.startswith("config/local/"):
        return True
    return path.name == ".DS_Store" or ".sqlite" in path.name


def sync_conflicts(paths: BrainPaths) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT logical_source_key,
                       COUNT(DISTINCT origin_node_id) AS origin_count,
                       GROUP_CONCAT(DISTINCT origin_node_id) AS origins,
                       GROUP_CONCAT(id) AS document_ids,
                       GROUP_CONCAT(source_path) AS source_paths
                FROM documents
                WHERE logical_source_key IS NOT NULL
                GROUP BY logical_source_key
                HAVING COUNT(DISTINCT origin_node_id) > 1
                ORDER BY logical_source_key
                """
            )
        ]
    conflicts = [
        {
            "logical_source_key": row["logical_source_key"],
            "origin_count": row["origin_count"],
            "origins": sorted(filter(None, str(row["origins"] or "").split(","))),
            "document_ids": sorted(filter(None, str(row["document_ids"] or "").split(","))),
            "source_paths": sorted(set(filter(None, str(row["source_paths"] or "").split(",")))),
        }
        for row in rows
    ]
    return {"conflicts": conflicts, "count": len(conflicts)}


def format_status_table_rows(status: dict[str, Any]) -> list[list[str]]:
    rows: list[list[str]] = []
    for peer in status.get("peers", []):
        pull = peer.get("last_successful_pull") or {}
        push = peer.get("last_successful_push") or {}
        failed = peer.get("last_failed_run") or {}
        rows.append(
            [
                str(peer.get("peer_node_id") or ""),
                str((pull.get("finished_at") or pull.get("started_at") or "never")),
                str((push.get("finished_at") or push.get("started_at") or "never")),
                str(failed.get("error_summary") or ""),
                "yes" if peer.get("mirror_current") else "unknown" if peer.get("remote_manifest_hash") is None else "no",
            ]
        )
    return rows
