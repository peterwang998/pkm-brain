from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .db import connection
from .paths import BrainPaths
from .service import BrainService
from .sync_config import PeerConfig, SyncConfig, load_sync_config
from .sync_connection import Transport, test_connection
from .sync_status import sync_status
from .sync_transfer import sync_run


EXPECTED_MIGRATIONS = {1, 2}
EXPECTED_SYNC_RUN_COLUMNS = [
    "id",
    "peer_node_id",
    "direction",
    "started_at",
    "finished_at",
    "status",
    "files_pulled",
    "files_pushed",
    "bytes_pulled",
    "bytes_pushed",
    "primary_ingest_run_id",
    "remote_ingest_status",
    "errors",
]
MANUAL_ACCEPTANCE_STEPS = [
    "Configure laptop as Primary.",
    "Configure LAN-only secondary machine as Secondary.",
    "Run brain sync test-connection <secondary-node-id>.",
    "Start a Codex run on the Secondary, including a Codex Mobile-triggered run.",
    "Run brain automation secondary-tick on Secondary.",
    "Run brain sync run <secondary-node-id> on Primary.",
    "Confirm Primary has Secondary files under inbox/external/<secondary-node-id>/.",
    "Confirm Primary ingest result has no errors.",
    "Confirm retrieve-context finds the Secondary session.",
    "Confirm canonical raw/wiki/memory/config/shared content pushed to Secondary.",
    "Confirm Secondary retrieval sees the pushed source after rebuild.",
]


@dataclass(frozen=True)
class AcceptanceCheck:
    name: str
    status: str
    message: str
    detail: Any = None

    def as_dict(self) -> dict[str, Any]:
        payload = {"name": self.name, "status": self.status, "message": self.message}
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


def run_acceptance_report(
    paths: BrainPaths,
    *,
    peer_node_id: str | None = None,
    test_connection_now: bool = True,
    run_sync_now: bool = False,
    retrieval_phrase: str | None = None,
    transport: Transport | None = None,
) -> dict[str, Any]:
    service = BrainService(paths, prefer_model_embeddings=False)
    service.init_workspace()
    checks: list[AcceptanceCheck] = []
    report: dict[str, Any] = {
        "home": str(paths.home),
        "peer_node_id": None,
        "checks": [],
        "schema_migrations": [],
        "sync_runs_columns": [],
        "sync_doctor": None,
        "sync_status": None,
        "connection_test": None,
        "sync_run": None,
        "retrieval": None,
        "manual_steps": MANUAL_ACCEPTANCE_STEPS,
    }

    audit_schema(paths, checks, report)
    config = audit_sync_doctor_and_config(paths, service, checks, report)
    peer = select_acceptance_peer(config, peer_node_id, checks)
    if peer:
        report["peer_node_id"] = peer.node_id

    try:
        report["sync_status"] = sync_status(paths, transport=transport)
        checks.append(AcceptanceCheck("sync_status", "ok", "sync status rendered"))
    except Exception as exc:
        checks.append(AcceptanceCheck("sync_status", "fail", str(exc)))

    if peer and test_connection_now:
        try:
            connection_result = test_connection(paths, peer.node_id, transport=transport).as_dict()
            report["connection_test"] = connection_result
            status = "ok" if connection_result["ready"] else "fail"
            message = "connection ready" if connection_result["ready"] else "connection validation failed"
            checks.append(AcceptanceCheck("connection_test", status, message, connection_result["checks"]))
        except Exception as exc:
            checks.append(AcceptanceCheck("connection_test", "fail", str(exc)))
    elif peer:
        checks.append(AcceptanceCheck("connection_test", "skipped", "connection test skipped by request"))
    else:
        checks.append(AcceptanceCheck("connection_test", "skipped", "no selected peer"))

    if run_sync_now:
        if not peer:
            checks.append(AcceptanceCheck("sync_run", "fail", "cannot run sync without a selected peer"))
        else:
            try:
                result = sync_run(paths, peer.node_id, transport=transport).as_dict()
            except Exception as exc:
                checks.append(AcceptanceCheck("sync_run", "fail", str(exc)))
            else:
                report["sync_run"] = result
                status = "ok" if result["status"] == "ok" else "fail"
                checks.append(AcceptanceCheck("sync_run", status, f"sync run status: {result['status']}"))
    else:
        checks.append(AcceptanceCheck("sync_run", "skipped", "pass --run-sync to execute pull/push acceptance"))

    if retrieval_phrase:
        try:
            retrieval = service.retrieve_context(task=retrieval_phrase, mode="compact")
        except Exception as exc:
            checks.append(AcceptanceCheck("retrieval", "fail", str(exc)))
        else:
            report["retrieval"] = {
                "task": retrieval_phrase,
                "retrieval_event_id": retrieval.get("retrieval_event_id"),
                "supporting_chunks": len(retrieval.get("supporting_chunks") or []),
                "relevant_wiki_pages": len(retrieval.get("relevant_wiki_pages") or []),
            }
            found = bool((retrieval.get("supporting_chunks") or []) or (retrieval.get("relevant_wiki_pages") or []))
            checks.append(
                AcceptanceCheck(
                    "retrieval",
                    "ok" if found else "fail",
                    "retrieval found candidate context" if found else "retrieval found no candidate context",
                    report["retrieval"],
                )
            )
    else:
        checks.append(AcceptanceCheck("retrieval", "skipped", "pass --retrieval-phrase to verify Secondary session retrieval"))

    report["checks"] = [check.as_dict() for check in checks]
    report["ready"] = all(check.status != "fail" for check in checks)
    report["complete"] = acceptance_complete(report)
    return report


def audit_schema(paths: BrainPaths, checks: list[AcceptanceCheck], report: dict[str, Any]) -> None:
    with connection(paths.sqlite_path) as conn:
        try:
            migrations = [dict(row) for row in conn.execute("SELECT version, applied_at FROM schema_migrations ORDER BY version")]
        except sqlite3.OperationalError as exc:
            migrations = []
            checks.append(AcceptanceCheck("schema_migrations", "fail", str(exc)))
        else:
            versions = {int(row["version"]) for row in migrations}
            missing = sorted(EXPECTED_MIGRATIONS - versions)
            if missing:
                checks.append(AcceptanceCheck("schema_migrations", "fail", f"missing migration versions: {missing}", migrations))
            else:
                checks.append(AcceptanceCheck("schema_migrations", "ok", "required migration versions present", migrations))
        report["schema_migrations"] = migrations

        sync_runs_columns = [row["name"] for row in conn.execute("PRAGMA table_info(sync_runs)")]
        report["sync_runs_columns"] = sync_runs_columns
        missing_columns = [column for column in EXPECTED_SYNC_RUN_COLUMNS if column not in sync_runs_columns]
        if missing_columns:
            checks.append(AcceptanceCheck("sync_runs_schema", "fail", f"missing columns: {missing_columns}", sync_runs_columns))
        else:
            checks.append(AcceptanceCheck("sync_runs_schema", "ok", "sync_runs has expected V1 columns", sync_runs_columns))

        missing_identity = conn.execute(
            "SELECT COUNT(*) AS count FROM documents WHERE origin_node_id IS NULL OR logical_source_key IS NULL"
        ).fetchone()["count"]
        if missing_identity:
            checks.append(AcceptanceCheck("origin_identity", "fail", f"{missing_identity} documents missing origin identity"))
        else:
            checks.append(AcceptanceCheck("origin_identity", "ok", "all documents have origin identity"))


def audit_sync_doctor_and_config(
    paths: BrainPaths,
    service: BrainService,
    checks: list[AcceptanceCheck],
    report: dict[str, Any],
) -> SyncConfig | None:
    try:
        doctor = service.sync_doctor()
    except Exception as exc:
        checks.append(AcceptanceCheck("sync_doctor", "fail", str(exc)))
        return None
    report["sync_doctor"] = doctor
    checks.append(
        AcceptanceCheck(
            "sync_doctor",
            "ok" if doctor.get("ready") else "fail",
            "sync doctor ready" if doctor.get("ready") else "sync doctor is not ready",
            doctor,
        )
    )
    try:
        config = load_sync_config(paths)
    except Exception as exc:
        checks.append(AcceptanceCheck("sync_config", "fail", str(exc)))
        return None
    checks.append(AcceptanceCheck("sync_config", "ok", f"configured as {config.role}/{config.node_id}"))
    checks.append(
        AcceptanceCheck(
            "primary_role",
            "ok" if config.role == "primary" else "fail",
            "acceptance runner is on Primary" if config.role == "primary" else f"acceptance must run on Primary, found {config.role}",
        )
    )
    return config


def select_acceptance_peer(
    config: SyncConfig | None,
    peer_node_id: str | None,
    checks: list[AcceptanceCheck],
) -> PeerConfig | None:
    if not config or not config.primary:
        checks.append(AcceptanceCheck("peer_configured", "fail", "Primary peer config is unavailable"))
        return None
    peers = config.primary.peers
    if peer_node_id:
        peer = next((candidate for candidate in peers if candidate.node_id == peer_node_id), None)
        if not peer:
            checks.append(AcceptanceCheck("peer_configured", "fail", f"peer not found: {peer_node_id}"))
            return None
        checks.append(AcceptanceCheck("peer_configured", "ok", f"selected peer {peer.node_id}", peer.as_dict()))
        return peer
    if len(peers) == 1:
        peer = peers[0]
        checks.append(AcceptanceCheck("peer_configured", "ok", f"selected only configured peer {peer.node_id}", peer.as_dict()))
        return peer
    if not peers:
        checks.append(AcceptanceCheck("peer_configured", "fail", "no Secondary peer configured"))
    else:
        checks.append(AcceptanceCheck("peer_configured", "fail", "multiple peers configured; pass --peer"))
    return None


def acceptance_complete(report: dict[str, Any]) -> bool:
    if not report["ready"]:
        return False
    check_statuses = {check["name"]: check["status"] for check in report["checks"]}
    return (
        check_statuses.get("connection_test") == "ok"
        and check_statuses.get("sync_run") == "ok"
        and check_statuses.get("retrieval") == "ok"
    )
