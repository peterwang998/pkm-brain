from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .connector_auth import connector_auth_status
from .db import connection
from .gmail_mirror import GmailMirrorStore
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


def shadow_setup_payload(
    paths: BrainPaths,
    *,
    scheduler_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    mirror = _gmail_mirror_status(paths)
    scheduled_sync = gmail_mirror_sync_schedule_status(scheduler_state)
    mirror["scheduled_sync"] = scheduled_sync
    return {
        "schema_version": 1,
        "policy": shadow_policy_status(paths),
        "connectors": {
            connector_id: connector_auth_status(paths, connector_id)
            for connector_id in ("calendar", "gmail")
        },
        "mode": "shadow_read_only",
        "automatic_schedule_enabled": bool(scheduled_sync.get("enabled")),
        "gmail_mirror": mirror,
        "approved_defaults": {
            "calendar": "owned primary calendar only",
            "gmail": "read-only thread content",
            "raw_cache_days": 7,
            "normalized_evidence_days": 30,
            "fetch_attachments": False,
            "strip_quoted_history": True,
            "external_writes": False,
            "gmail_sync_cadence_seconds": 600,
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
            "evidence query fields must appear exactly once: " + ", ".join(duplicated)
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
        raise OperationsHTTPBadRequest(
            "account_key and bounded source_ref are required"
        )
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
    evidence = None
    evidence_origin = "retained_cache"
    resolved_revision = source_revision
    gmail_current_fallback = None
    if source_type == "gmail" and paths.gmail_mirror_sqlite_path.is_file():
        thread_id = source_ref.removeprefix(prefix)
        store = GmailMirrorStore(paths.gmail_mirror_sqlite_path)
        if source_revision is not None:
            revision = store.get_revision(account_key, thread_id, source_revision)
            if revision is None:
                gmail_current_fallback = store.get_current_revision(
                    account_key,
                    thread_id,
                )
        else:
            revision = store.get_current_revision(account_key, thread_id)
        if (
            revision is not None
            and not revision.tombstoned
            and revision.thread is not None
        ):
            evidence = revision.thread.as_dict()
            evidence_origin = "gmail_mirror"
            resolved_revision = revision.source_revision
    if evidence is None and (paths.home / "cache" / "google-evidence").is_dir():
        evidence = GoogleEvidenceCache.for_paths(paths).read_normalized(
            source_type,
            source_ref,
            source_revision=source_revision,
        )
    if (
        evidence is None
        and gmail_current_fallback is not None
        and not gmail_current_fallback.tombstoned
        and gmail_current_fallback.thread is not None
    ):
        evidence = gmail_current_fallback.thread.as_dict()
        evidence_origin = "gmail_mirror_current_fallback"
        resolved_revision = gmail_current_fallback.source_revision
    if evidence is None:
        raise OperationsHTTPNotFound("retained local evidence is unavailable")
    revision_matches = source_revision is None or source_revision == resolved_revision
    return {
        "schema_version": 1,
        "source_type": source_type,
        "account_key": account_key,
        "source_ref": source_ref,
        "source_revision": resolved_revision,
        "requested_source_revision": source_revision,
        "revision_matches": revision_matches,
        "retention_days": policy.privacy.normalized_evidence_days,
        "evidence_origin": evidence_origin,
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


def _gmail_mirror_status(paths: BrainPaths) -> dict[str, Any]:
    try:
        policy = load_operations_policy(paths)
    except Exception as exc:
        return {
            "source_enabled": None,
            "mailbox_status": "unavailable",
            "triage_status": "unknown",
            "triage_pending_count": None,
            "sync_cadence_seconds": 600,
            "message": f"Gmail mirror policy is unavailable: {exc}",
        }
    if not policy.sources.gmail.enabled:
        return {
            "source_enabled": False,
            "mailbox_status": "disabled",
            "triage_status": "disabled",
            "triage_pending_count": 0,
            "sync_cadence_seconds": 600,
        }
    mirror_path = paths.gmail_mirror_sqlite_path
    if not mirror_path.exists() and not mirror_path.is_symlink():
        return {
            "source_enabled": True,
            "mailbox_status": "not_initialized",
            "triage_status": "idle",
            "triage_pending_count": 0,
            "sync_cadence_seconds": 600,
        }
    try:
        store = GmailMirrorStore(mirror_path)
        checkpoint = store.get_checkpoint(policy.sources.gmail.account_key)
        counts = store.triage_counts(policy.sources.gmail.account_key)
    except Exception as exc:
        return {
            "source_enabled": True,
            "mailbox_status": "unavailable",
            "triage_status": "unknown",
            "triage_pending_count": None,
            "sync_cadence_seconds": 600,
            "message": str(exc),
        }
    if checkpoint is None:
        mailbox_status = "not_initialized"
        last_success_at = None
    elif checkpoint.coverage_complete:
        mailbox_status = "synchronized"
        last_success_at = checkpoint.last_success_at
    elif checkpoint.reset_required:
        mailbox_status = "resyncing"
        last_success_at = checkpoint.last_success_at
    else:
        mailbox_status = "partial"
        last_success_at = checkpoint.last_success_at
    backlog = int(counts["backlog_count"])
    return {
        "source_enabled": True,
        "mailbox_status": mailbox_status,
        "mailbox_last_success_at": last_success_at,
        "triage_status": "backlogged" if backlog else "current",
        "triage_pending_count": backlog,
        "triage_counts": counts,
        "sync_cadence_seconds": 600,
    }


def gmail_mirror_sync_schedule_status(
    scheduler_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    unavailable = {
        "available": False,
        "job_id": "gmail_mirror_sync",
        "enabled": None,
        "paused": None,
        "paused_until": None,
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "next_due_at": None,
        "running": None,
    }
    if not isinstance(scheduler_state, Mapping):
        return unavailable
    jobs = scheduler_state.get("jobs")
    if not isinstance(jobs, Sequence) or isinstance(jobs, (str, bytes)):
        return unavailable
    job = next(
        (
            value
            for value in jobs
            if isinstance(value, Mapping)
            and str(value.get("id") or "") == "gmail_mirror_sync"
        ),
        None,
    )
    if job is None:
        return unavailable
    paused_until = _optional_string(scheduler_state.get("paused_until"))
    return {
        "available": True,
        "job_id": "gmail_mirror_sync",
        "enabled": bool(job.get("enabled")),
        "paused": _pause_is_active(paused_until),
        "paused_until": paused_until,
        "last_run_at": _optional_string(job.get("last_run_at")),
        "last_status": _optional_string(job.get("last_status")),
        "last_error": _optional_string(job.get("last_error")),
        "next_due_at": _optional_string(job.get("next_due_at")),
        "running": bool(job.get("running")),
    }


def _pause_is_active(paused_until: str | None) -> bool:
    if paused_until is None:
        return False
    try:
        parsed = datetime.fromisoformat(paused_until)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc) > datetime.now(timezone.utc)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None
