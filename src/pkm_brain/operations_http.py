from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .connector_auth import connector_auth_status
from .db import connection
from .google_cache import GoogleEvidenceCache
from .maintenance import managed_storage_inventory
from .operational_briefing import build_meeting_packet
from .operational_meeting_packets import load_current_meeting_packet
from .operations_policy import load_operations_policy
from .paths import BrainPaths
from .service import BrainService
from .shadow_setup import shadow_policy_status


class OperationsHTTPBadRequest(ValueError):
    pass


class OperationsHTTPNotFound(ValueError):
    pass


def operations_runs_payload(paths: BrainPaths) -> dict[str, Any]:
    BrainService(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        output: dict[str, Any] = {}
        for table in ("automation_runs", "ingestion_runs"):
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            output[table] = (
                [
                    dict(row)
                    for row in conn.execute(
                        f"SELECT * FROM {table} ORDER BY started_at DESC LIMIT 100"
                    )
                ]
                if exists
                else []
            )
    return output


def operations_storage_payload(paths: BrainPaths) -> dict[str, Any]:
    BrainService(paths).init_workspace()
    configured = str(os.environ.get("PKM_BRAIN_APP_SUPPORT") or "").strip()
    app_support = Path(configured).expanduser() if configured else None
    return managed_storage_inventory(paths, app_support=app_support)


def shadow_setup_payload(paths: BrainPaths) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "policy": shadow_policy_status(paths),
        "connectors": {
            connector_id: connector_auth_status(paths, connector_id)
            for connector_id in ("calendar", "gmail")
        },
        "mode": "shadow_read_only",
        "automatic_schedule_enabled": False,
        "approved_defaults": {
            "calendar": "owned primary calendar only",
            "gmail": "read-only thread content",
            "raw_cache_days": 7,
            "normalized_evidence_days": 30,
            "fetch_attachments": False,
            "strip_quoted_history": True,
            "external_writes": False,
        },
    }


def operations_evidence_payload(
    paths: BrainPaths,
    query: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    allowed = {"source_type", "account_key", "source_ref", "source_revision"}
    unknown = sorted(set(query) - allowed)
    if unknown:
        raise OperationsHTTPBadRequest(
            "unsupported evidence query fields: " + ", ".join(unknown)
        )
    duplicated = sorted(key for key, values in query.items() if len(values) != 1)
    if duplicated:
        raise OperationsHTTPBadRequest(
            "evidence query fields must appear exactly once: "
            + ", ".join(duplicated)
        )
    source_type = _first(query, "source_type").casefold()
    account_key = _first(query, "account_key")
    source_ref = _first(query, "source_ref")
    source_revision = _first(query, "source_revision") or None
    if source_type not in {"calendar", "gmail"}:
        raise OperationsHTTPBadRequest("source_type must be calendar or gmail")
    if (
        not account_key
        or len(account_key) > 512
        or not source_ref
        or len(source_ref) > 4_000
    ):
        raise OperationsHTTPBadRequest("account_key and bounded source_ref are required")
    if source_revision is not None and len(source_revision) > 1_024:
        raise OperationsHTTPBadRequest("source_revision is too long")
    policy = load_operations_policy(paths)
    expected_account = (
        policy.sources.calendar.account_key
        if source_type == "calendar"
        else policy.sources.gmail.account_key
    )
    prefix = (
        f"{expected_account}:primary:"
        if source_type == "calendar"
        else f"{expected_account}:"
    )
    if account_key != expected_account or not source_ref.startswith(prefix):
        raise OperationsHTTPNotFound(
            "evidence reference does not match its account and source"
        )
    if not (paths.home / "cache" / "google-evidence").is_dir():
        raise OperationsHTTPNotFound("retained local evidence is unavailable")
    evidence = GoogleEvidenceCache.for_paths(paths).read_normalized(
        source_type,
        source_ref,
        source_revision=source_revision,
    )
    if evidence is None:
        raise OperationsHTTPNotFound("retained local evidence is unavailable")
    return {
        "schema_version": 1,
        "source_type": source_type,
        "account_key": account_key,
        "source_ref": source_ref,
        "source_revision": source_revision,
        "retention_days": policy.privacy.normalized_evidence_days,
        "evidence": evidence,
    }


def operations_meeting_packet_payload(
    paths: BrainPaths,
    item_id: str,
) -> dict[str, Any]:
    normalized = item_id.strip()
    if not normalized or len(normalized) > 256:
        raise OperationsHTTPBadRequest("a bounded operational item id is required")
    prepared = load_current_meeting_packet(paths.ops_sqlite_path, normalized)
    if prepared is not None:
        return prepared
    return build_meeting_packet(paths, normalized)


def _first(query: Mapping[str, Sequence[str]], key: str) -> str:
    values = query.get(key) or ()
    return str(values[0]).strip() if values else ""
