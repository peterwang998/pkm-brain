from __future__ import annotations

import fcntl
import json
import os
import plistlib
import hashlib
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .audit import audit_memories, provenance_check
from .connectors import (
    connector_ids_for_agent,
    run_connector_capture,
    runtime_settings,
)
from .cos_actions import reviewable_bad_audit_actions
from .cos_audit import (
    HISTORICAL_AUDIT_CANDIDATE_SCAN_CAP,
    load_audit_sample,
    run_sampled_audit,
    select_historical_audit_cohort,
)
from .db import connection, dumps, loads
from .extraction import extract_recent_documents
from .gardener import generate_gardener_candidates
from .gmail_knowledge import reconcile_gmail_document_revisions
from .google_cache import GoogleEvidenceCache
from .indexes import lancedb_stats, optimize_vectors, should_optimize_vectors
from .llm import (
    CODEX_DEFAULT_MODEL,
    DEFAULT_LLM_PROVIDER,
    OPENAI_DEFAULT_MODEL,
    get_provider,
)
from .llm_usage import llm_usage_summary
from .memory_proposals import (
    propose_failure_memories_from_sources,
    propose_memories_from_lineage,
)
from .paths import BrainPaths
from .queue_summary import review_queue_summary
from .service import BrainService
from .sync_config import load_sync_config
from .synthesizer import generate_page_syntheses
from .util import new_id, now_iso
from .wiki import lint_wiki


LAUNCH_AGENT_LABEL = "com.pkm-brain.agent-log-ingest"
NIGHTLY_LAUNCH_AGENT_LABEL = "com.pkm-brain.nightly-maintenance"
NIGHTLY_JOB_NAME = "nightly-maintenance"
WEEKLY_HISTORICAL_AUDIT_JOB_NAME = "weekly-historical-audit"
WEEKLY_HISTORICAL_AUDIT_DUE_AFTER_HOURS = 168
WEEKLY_HISTORICAL_AUDIT_LIMIT = 5
WEEKLY_HISTORICAL_AUDIT_ACTIVE_LIMIT = 5
MAX_STORED_ERROR_CHARS = 4000
MAX_STORED_ERROR_LIST_ITEMS = 20
MAX_STORED_SUMMARY_CHARS = 4000
MAX_STORED_SUMMARY_LIST_ITEMS = 50
MAX_STORED_SUMMARY_DICT_ITEMS = 100
MAX_STORED_SUMMARY_DEPTH = 10
MAX_STORED_SUMMARY_BYTES = 256_000
MAX_PENDING_WEEKLY_HISTORICAL_RUN_SCAN = 100
AGENT_LOG_INGEST_MAX_CHANGED_DOCUMENTS = 16
AGENT_LOG_INGEST_MAX_CHANGED_SOURCE_BYTES = 64 * 1024 * 1024
ERROR_FIELD_NAMES = {"error", "errors", "stderr", "traceback"}
COS_SECONDARY_SKIP_REASON = (
    "secondary role skips CoS mutation-capable stages by default"
)
TRUTH_RESIDUE_KINDS = {"conflict"}
NIGHTLY_AUTOMATION_FLAGS = {
    "--agent",
    "--due-after-hours",
    "--help",
    "--home",
    "--hyprnote-root",
    "--if-due",
    "--include-hyprnote",
    "--llm-wiki",
    "--no-llm-wiki",
    "--provider",
    "--with-llm-memory-proposals",
}
TRUTH_ACTION_TYPES = {
    "display_contested",
    "fact_merge",
    "fact_supersede",
    "fact_upsert",
    "resolve_conflict",
}
TIMEOUT_TO_UNCERTAINTY_ACTION_TYPES = {
    "archive_page",
    "canonicalize_page",
    "edit_contract",
    "page_merge",
    "page_split",
    "rehome_fact",
    "rename_page",
    "revert_page_snapshot",
    "synthesize_page",
}


@dataclass(frozen=True)
class AutomationResult:
    started_at: str
    capture: dict[str, Any]
    ingest: dict[str, Any] | None
    skipped: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class NightlyMaintenanceResult:
    run_id: str | None
    started_at: str
    finished_at: str | None
    status: str
    due: bool
    skipped: bool
    reason: str | None
    summary: dict[str, Any]
    error: str | None = None


@dataclass(frozen=True)
class WeeklyHistoricalAuditResult:
    run_id: str | None
    started_at: str
    finished_at: str | None
    status: str
    due: bool
    skipped: bool
    reason: str | None
    summary: dict[str, Any]
    error: str | None = None


@dataclass(frozen=True)
class SecondaryTickResult:
    started_at: str
    capture: dict[str, Any]
    ingest: dict[str, Any] | None
    index_status: dict[str, Any] | None
    skipped: bool = False
    reason: str | None = None


def run_bounded_scheduled_ingest(service: BrainService):
    return service.ingest(
        max_changed_documents=AGENT_LOG_INGEST_MAX_CHANGED_DOCUMENTS,
        max_changed_source_bytes=AGENT_LOG_INGEST_MAX_CHANGED_SOURCE_BYTES,
    )


def run_agent_log_ingest(
    paths: BrainPaths,
    agent: str = "all",
    codex_state: Path | None = None,
    claude_projects: Path | None = None,
    opencode_db: Path | None = None,
    hyprnote_root: Path | None = None,
    include_hyprnote: bool = False,
) -> AutomationResult:
    service = BrainService(paths)
    service.init_workspace()
    lock_path = paths.logs / "agent-log-ingest.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return AutomationResult(
                now_iso(),
                {},
                None,
                skipped=True,
                reason="another run is already active",
            )
        connector_ids = connector_ids_for_agent(
            agent, include_hyprnote=include_hyprnote
        )
        explicit_agent = agent != "all"
        capture_result = run_connector_capture(
            paths,
            connector_ids=connector_ids,
            respect_enabled=not explicit_agent,
            respect_cadence=not explicit_agent,
            settings_overrides=runtime_settings(
                codex_state=codex_state,
                claude_projects=claude_projects,
                opencode_db=opencode_db,
                hyprnote_root=hyprnote_root,
            ),
        )
        ingest_result = run_bounded_scheduled_ingest(service)
        return AutomationResult(
            started_at=now_iso(),
            capture=capture_result.as_dict(),
            ingest=ingest_result.__dict__,
        )


def run_gmail_knowledge_ingest(
    paths: BrainPaths,
    *,
    source_home: Path | None = None,
    batch_size: str | int = "500",
    respect_enabled: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Capture and ingest Gmail revisions without touching the provider sync path."""

    service = BrainService(paths)
    service.init_workspace()
    lock_path = paths.logs / "gmail-knowledge-ingest.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {
                "status": "skipped",
                "reason": "another Gmail Knowledge run is already active",
            }
        settings: dict[str, Any] = {"batch_size": str(batch_size)}
        if source_home is not None:
            settings["source_home"] = str(source_home)
        capture_result = run_connector_capture(
            paths,
            connector_ids=["gmail"],
            respect_enabled=respect_enabled,
            respect_cadence=False,
            dry_run=dry_run,
            export_outbox=False,
            settings_overrides={"gmail": settings},
        )
        capture = capture_result.as_dict()
        runs = capture.get("connector_results") or []
        gmail_run = runs[0] if runs else {}
        if gmail_run.get("status") == "skipped":
            return {
                "status": "skipped",
                "reason": gmail_run.get("reason"),
                "capture": _redact_gmail_capture_artifacts(capture),
            }
        capture_failed = gmail_run.get("status") == "failed"
        preflight = gmail_run.get("preflight") or {}
        preflight_failed = capture_failed and preflight.get("ok") is False
        capture_has_output = int(gmail_run.get("captured") or 0) > 0
        if not runs or preflight_failed or (capture_failed and not capture_has_output):
            return {
                "status": "failed",
                "reason": (
                    "Gmail capture preflight failed"
                    if preflight_failed
                    else "Gmail capture did not complete"
                ),
                "capture": _redact_gmail_capture_artifacts(capture),
            }
        if dry_run:
            return {
                "status": "partial" if capture.get("errors") else "success",
                "dry_run": True,
                "capture": _redact_gmail_capture_artifacts(capture),
            }
        gmail_inbox = paths.inbox / "documents" / "gmail"
        try:
            ingest_result = service.ingest(source=gmail_inbox)
        except Exception as exc:
            return {
                "status": "failed",
                "reason": f"Gmail ingest failed ({type(exc).__name__})",
                "capture": _redact_gmail_capture_artifacts(capture),
            }
        vector_writes = ingest_result.vector_writes or {}
        vector_incomplete = (
            int(vector_writes.get("attempted") or 0) > 0
            and vector_writes.get("status") != "ok"
        )
        try:
            reconciliation = reconcile_gmail_document_revisions(paths)
        except Exception as exc:
            return {
                "status": "partial",
                "reason": f"Gmail revision reconciliation failed ({type(exc).__name__})",
                "capture": _redact_gmail_capture_artifacts(capture),
                "ingest": ingest_result.__dict__,
            }
        partial = bool(
            capture.get("errors")
            or ingest_result.errors
            or vector_incomplete
            or reconciliation.errors
            or reconciliation.held_documents
        )
        return {
            "status": "partial" if partial else "success",
            "capture": _redact_gmail_capture_artifacts(capture),
            "ingest": ingest_result.__dict__,
            "revision_reconciliation": reconciliation.as_dict(),
        }


def _redact_gmail_capture_artifacts(capture: dict[str, Any]) -> dict[str, Any]:
    """Keep Gmail thread identifiers out of CLI, daemon, and job-result output."""

    redacted = dict(capture)
    for field in ("artifacts", "outbox_artifacts"):
        values = redacted.pop(field, None)
        if isinstance(values, list):
            redacted[f"{field[:-1]}_count"] = len(values)
    connector_results: list[dict[str, Any]] = []
    for item in redacted.get("connector_results") or []:
        connector = dict(item)
        for field in ("artifacts", "outbox_artifacts"):
            values = connector.pop(field, None)
            if isinstance(values, list):
                connector[f"{field[:-1]}_count"] = len(values)
        connector_results.append(connector)
    if "connector_results" in redacted:
        redacted["connector_results"] = connector_results
    return redacted


def run_secondary_tick(
    paths: BrainPaths,
    agent: str = "all",
    codex_state: Path | None = None,
    claude_projects: Path | None = None,
    opencode_db: Path | None = None,
    hyprnote_root: Path | None = None,
    include_hyprnote: bool = False,
) -> SecondaryTickResult:
    service = BrainService(paths)
    service.init_workspace()
    lock_path = paths.logs / "secondary-tick.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return SecondaryTickResult(
                now_iso(),
                {},
                None,
                None,
                skipped=True,
                reason="another secondary tick is already active",
            )
        connector_ids = connector_ids_for_agent(
            agent, include_hyprnote=include_hyprnote
        )
        explicit_agent = agent != "all"
        capture_result = run_connector_capture(
            paths,
            connector_ids=connector_ids,
            respect_enabled=not explicit_agent,
            respect_cadence=not explicit_agent,
            export_outbox=True,
            settings_overrides=runtime_settings(
                codex_state=codex_state,
                claude_projects=claude_projects,
                opencode_db=opencode_db,
                hyprnote_root=hyprnote_root,
            ),
        )
        ingest_result = run_bounded_scheduled_ingest(service)
        status = index_status(paths, service)
        return SecondaryTickResult(
            started_at=now_iso(),
            capture=capture_result.as_dict(),
            ingest=ingest_result.__dict__,
            index_status=status,
        )


def run_nightly_maintenance(
    paths: BrainPaths,
    if_due: bool = False,
    due_after_hours: int = 20,
    agent: str = "all",
    codex_state: Path | None = None,
    claude_projects: Path | None = None,
    opencode_db: Path | None = None,
    hyprnote_root: Path | None = None,
    include_hyprnote: bool = False,
    with_llm_memory_proposals: bool = False,
    llm_wiki: bool = True,
    provider: str | None = None,
) -> NightlyMaintenanceResult:
    service = BrainService(paths)
    service.init_workspace()
    lock_path = paths.logs / "nightly-maintenance.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = now_iso()

    with lock_path.open("w") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return NightlyMaintenanceResult(
                run_id=None,
                started_at=started_at,
                finished_at=now_iso(),
                status="skipped",
                due=False,
                skipped=True,
                reason="another nightly run is already active",
                summary={},
            )

        if with_llm_memory_proposals:
            try:
                get_provider(provider)
            except Exception as exc:
                return NightlyMaintenanceResult(
                    run_id=None,
                    started_at=started_at,
                    finished_at=now_iso(),
                    status="failed",
                    due=True,
                    skipped=False,
                    reason=None,
                    summary={
                        "with_llm_memory_proposals": with_llm_memory_proposals,
                    },
                    error=str(exc),
                )

        if if_due and not nightly_due(paths, due_after_hours):
            return NightlyMaintenanceResult(
                run_id=None,
                started_at=started_at,
                finished_at=now_iso(),
                status="skipped",
                due=False,
                skipped=True,
                reason=f"last successful nightly run is less than {due_after_hours} hours old",
                summary={"due_after_hours": due_after_hours},
            )

        run_id = new_id("automation")
        record_automation_start(paths, run_id, NIGHTLY_JOB_NAME, started_at)
        summary: dict[str, Any] = {}
        status = "success"
        error: str | None = None
        try:
            summary["google_evidence_retention"] = run_google_evidence_cache_retention(
                paths
            )
            cos_role = cos_role_status(paths)
            connector_ids = connector_ids_for_agent(
                agent, include_hyprnote=include_hyprnote
            )
            explicit_agent = agent != "all"
            capture_result = run_connector_capture(
                paths,
                connector_ids=connector_ids,
                respect_enabled=not explicit_agent,
                respect_cadence=not explicit_agent,
                settings_overrides=runtime_settings(
                    codex_state=codex_state,
                    claude_projects=claude_projects,
                    opencode_db=opencode_db,
                    hyprnote_root=hyprnote_root,
                ),
            )
            summary["capture"] = capture_result.as_dict()

            ingest_result = run_bounded_scheduled_ingest(service)
            summary["ingest"] = ingest_result.__dict__

            summary["cos_role"] = cos_role

            summary["cos_extraction"] = run_cos_extraction(
                paths, cos_role, run_id=run_id
            )
            summary["cos_extraction_shadow"] = summary["cos_extraction"]

            summary["cos_gardener"] = run_cos_gardener(paths, cos_role, run_id=run_id)
            summary["cos_gardener_shadow"] = summary["cos_gardener"]

            summary["cos_synthesis"] = run_cos_synthesis(
                paths, cos_role, enabled=llm_wiki, run_id=run_id
            )
            summary["cos_synthesis_shadow"] = summary["cos_synthesis"]
            summary["cos_current_action_ids"] = current_cycle_action_ids(
                summary["cos_extraction"],
                summary["cos_gardener"],
                summary["cos_synthesis"],
            )

            summary["index_status"] = index_status(paths, service)
            summary["telemetry_retention"] = service.compact_retrieval_events(
                dry_run=False
            )
            summary["index_maintenance"] = run_index_maintenance(paths)
            summary["cos_timeout_sweep"] = run_cos_timeout_sweep(paths)
            summary["cos_audit"] = run_cos_audit(
                paths,
                cos_role,
                run_id=run_id,
                action_ids=summary["cos_current_action_ids"],
            )
            summary["queue_summary"] = review_queue_summary(paths)
            summary["provenance_check"] = provenance_check(paths)
            summary["wiki_lint"] = lint_wiki(paths)
            if with_llm_memory_proposals:
                summary["memory_proposals"] = propose_failure_memories_from_sources(
                    paths, provider_name=provider
                )
                summary["lineage_memory_proposals"] = propose_memories_from_lineage(
                    paths, provider_name=provider
                )
            summary["memory_audit"] = audit_memories(paths)
            if summary["memory_audit"].get("errors"):
                summary["memory_audit"]["nightly_severity"] = "warning"
                summary["memory_audit"].setdefault("warnings", []).append(
                    "memory audit errors are warning-tier for nightly; run `brain memory audit` for details"
                )

            errors = (
                summary["capture"].get("errors", [])
                + summary["ingest"].get("errors", [])
                + summary["google_evidence_retention"].get("errors", [])
                + summary["telemetry_retention"].get("errors", [])
                + summary["index_maintenance"].get("errors", [])
                + summary["provenance_check"].get("errors", [])
                + summary["wiki_lint"].get("errors", [])
            )
            if errors:
                status = "failed"
                error = "; ".join(str(item) for item in errors[:10])
        except Exception as exc:
            status = "failed"
            error = str(exc)
        summary["llm_usage"] = llm_usage_summary(paths, cycle_id=run_id, limit=1)
        finished_at = now_iso()
        record_automation_finish(paths, run_id, status, finished_at, summary, error)
        return NightlyMaintenanceResult(
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            due=True,
            skipped=False,
            reason=None,
            summary=summary,
            error=error,
        )


def run_weekly_historical_audit(
    paths: BrainPaths,
    *,
    if_due: bool = False,
    due_after_hours: int = WEEKLY_HISTORICAL_AUDIT_DUE_AFTER_HOURS,
    limit: int = WEEKLY_HISTORICAL_AUDIT_LIMIT,
    active_limit: int = WEEKLY_HISTORICAL_AUDIT_ACTIVE_LIMIT,
    provider: str | None = None,
) -> WeeklyHistoricalAuditResult:
    """Audit a small, durable-cadence sample of older applied actions."""

    service = BrainService(paths)
    service.init_workspace()
    lock_path = paths.logs / "weekly-historical-audit.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = now_iso()

    with lock_path.open("w") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return WeeklyHistoricalAuditResult(
                run_id=None,
                started_at=started_at,
                finished_at=now_iso(),
                status="skipped",
                due=False,
                skipped=True,
                reason="another weekly historical audit is already active",
                summary={},
            )

        role_status = cos_role_status(paths)
        if not role_status.get("can_run_mutation_capable_stages"):
            return WeeklyHistoricalAuditResult(
                run_id=None,
                started_at=started_at,
                finished_at=now_iso(),
                status="skipped",
                due=False,
                skipped=True,
                reason=str(role_status.get("reason") or COS_SECONDARY_SKIP_REASON),
                summary={"cos_role": role_status},
            )

        if if_due and not automation_due(
            paths,
            WEEKLY_HISTORICAL_AUDIT_JOB_NAME,
            due_after_hours,
        ):
            return WeeklyHistoricalAuditResult(
                run_id=None,
                started_at=started_at,
                finished_at=now_iso(),
                status="skipped",
                due=False,
                skipped=True,
                reason=(
                    "last successful weekly historical audit is less than "
                    f"{due_after_hours} hours old"
                ),
                summary={"due_after_hours": due_after_hours, "cos_role": role_status},
            )

        retry_state = pending_weekly_historical_audit_state(paths)
        retry_run_id = retry_state.get("run_id")
        retry_cohort = list(retry_state.get("action_ids") or [])
        run_id = new_id("automation")
        record_automation_start(
            paths,
            run_id,
            WEEKLY_HISTORICAL_AUDIT_JOB_NAME,
            started_at,
        )
        requested_limit = max(0, limit)
        cohort_limit = min(requested_limit, WEEKLY_HISTORICAL_AUDIT_LIMIT)
        summary: dict[str, Any] = {
            "cos_role": role_status,
            "due_after_hours": due_after_hours,
            "requested_limit": requested_limit,
            "cohort_limit": cohort_limit,
            "active_limit": max(0, active_limit),
            "cohort_retry_of_run_id": retry_run_id,
        }
        status = "success"
        error: str | None = None
        try:
            active_before = active_historical_audit_findings(paths)
            available_capacity = max(0, max(0, active_limit) - active_before)
            requested_sample_limit = min(cohort_limit, available_capacity)
            cohort_action_ids = list(retry_cohort)
            historical_scan = dict(retry_state.get("historical_scan") or {})
            if not cohort_action_ids and requested_sample_limit > 0:
                cohort_selection = select_weekly_historical_audit_cohort(
                    paths,
                    requested_sample_limit,
                )
                cohort_action_ids, historical_scan = (
                    normalize_weekly_historical_cohort_selection(
                        paths,
                        cohort_selection,
                    )
                )
            elif not historical_scan:
                historical_scan = unadvanced_weekly_historical_scan(paths)
            remaining_action_ids = eligible_weekly_historical_action_ids(
                paths,
                cohort_action_ids,
            )
            sample_limit = min(len(remaining_action_ids), available_capacity)
            summary.update(
                {
                    "active_findings_before": active_before,
                    "available_capacity": available_capacity,
                    "sample_limit": sample_limit,
                    "cohort_action_ids": cohort_action_ids,
                    "cohort_size": len(cohort_action_ids),
                    "remaining_cohort_action_ids": remaining_action_ids,
                    "remaining_cohort_size": len(remaining_action_ids),
                    "historical_scan": historical_scan,
                }
            )
            # Store the cohort before invoking the auditor because each judgment is
            # committed independently. A crashed or incomplete attempt can then
            # retry this exact cohort instead of widening one weekly pass on every
            # hourly due check.
            record_automation_progress(paths, run_id, summary)
            if sample_limit == 0:
                summary["audit"] = {
                    "status": "skipped",
                    "reason": (
                        "active historical audit finding capacity is full"
                        if remaining_action_ids and available_capacity == 0
                        else "no eligible historical actions"
                    ),
                    "sampled": 0,
                    "audited": [],
                }
                if remaining_action_ids:
                    status = "failed"
                    error = (
                        "weekly historical audit retry is waiting for active "
                        "historical finding capacity"
                        if available_capacity == 0
                        else "weekly historical audit cohort remains incomplete"
                    )
            else:
                audit = run_sampled_audit(
                    paths,
                    limit=sample_limit,
                    provider=provider,
                    run_id=run_id,
                    action_ids=remaining_action_ids,
                    audit_origin="weekly_historical",
                    historical=True,
                )
                summary["audit"] = audit
                if audit.get("mode") != "configured":
                    status = "failed"
                    error = "weekly historical audit has no configured auditor"
                else:
                    remaining_action_ids = eligible_weekly_historical_action_ids(
                        paths,
                        cohort_action_ids,
                    )
                    summary["remaining_cohort_action_ids"] = remaining_action_ids
                    summary["remaining_cohort_size"] = len(remaining_action_ids)
                if audit.get("mode") == "configured" and audit.get("status") != "ok":
                    status = "failed"
                    error = (
                        "weekly historical audit did not complete all sampled actions"
                    )
                elif audit.get("mode") == "configured" and remaining_action_ids:
                    status = "failed"
                    error = (
                        "weekly historical audit cohort remains incomplete; "
                        f"{len(remaining_action_ids)} eligible action(s) are pending"
                    )
            summary["active_findings_after"] = active_historical_audit_findings(paths)
            summary["queue_summary"] = review_queue_summary(paths)
        except Exception as exc:
            status = "failed"
            error = str(exc)
        summary["llm_usage"] = llm_usage_summary(paths, cycle_id=run_id, limit=1)
        finished_at = now_iso()
        record_automation_finish(paths, run_id, status, finished_at, summary, error)
        return WeeklyHistoricalAuditResult(
            run_id=run_id,
            started_at=started_at,
            finished_at=finished_at,
            status=status,
            due=True,
            skipped=False,
            reason=None,
            summary=summary,
            error=error,
        )


def run_google_evidence_cache_retention(paths: BrainPaths) -> dict[str, Any]:
    """Prune an existing private Google evidence cache without creating one."""

    cache_root = paths.home / "cache" / "google-evidence"
    if not cache_root.exists() and not cache_root.is_symlink():
        return {
            "status": "not_configured",
            "configured": False,
            "removed_files": 0,
            "removed_bytes": 0,
            "retained_files": 0,
            "errors": [],
            "note": "Google evidence cache is absent; nightly cleanup was a no-op",
        }
    try:
        result = GoogleEvidenceCache.for_paths(paths).prune().as_dict()
    except Exception as exc:
        return {
            "status": "failed",
            "configured": True,
            "removed_files": 0,
            "removed_bytes": 0,
            "retained_files": 0,
            "errors": [str(exc)],
        }
    errors = list(result.get("errors", []))
    return {
        "status": "failed" if errors else "ok",
        "configured": True,
        **result,
        "errors": errors,
    }


def cos_role_status(paths: BrainPaths) -> dict[str, Any]:
    try:
        config = load_sync_config(paths)
    except FileNotFoundError:
        return {
            "role": "single",
            "configured": False,
            "can_run_mutation_capable_stages": True,
            "reason": "sync config absent; preserving single-machine behavior",
        }
    except Exception as exc:
        return {
            "role": "unknown",
            "configured": True,
            "can_run_mutation_capable_stages": False,
            "reason": f"sync config invalid: {exc}",
        }
    can_run = config.role == "primary"
    return {
        "role": config.role,
        "node_id": config.node_id,
        "configured": True,
        "can_run_mutation_capable_stages": can_run,
        "reason": (
            "primary role may run shadow CoS stages"
            if can_run
            else COS_SECONDARY_SKIP_REASON
        ),
    }


def cos_stage_skipped(
    stage: str, role_status: dict[str, Any], *, mode: str
) -> dict[str, Any]:
    return {
        "stage": stage,
        "status": "skipped",
        "mode": mode,
        "role": role_status.get("role"),
        "reason": role_status.get("reason") or COS_SECONDARY_SKIP_REASON,
    }


def run_cos_extraction(
    paths: BrainPaths, role_status: dict[str, Any], *, run_id: str | None = None
) -> dict[str, Any]:
    if not role_status.get("can_run_mutation_capable_stages"):
        return cos_stage_skipped("cos_extraction", role_status, mode="policy")
    result = extract_recent_documents(paths, limit=10, shadow=False, run_id=run_id)
    status = "skipped" if result.get("status") == "skipped" else "policy"
    return {
        **result,
        "stage": "cos_extraction",
        "status": status,
        "mode": "policy",
        "role": role_status.get("role"),
        "note": "Policy-driven extraction; proposed actions pass through cos_actions policy.",
    }


def run_cos_gardener(
    paths: BrainPaths,
    role_status: dict[str, Any],
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    if not role_status.get("can_run_mutation_capable_stages"):
        return cos_stage_skipped("cos_gardener", role_status, mode="policy")
    result = generate_gardener_candidates(paths, shadow=False, run_id=run_id)
    return {
        **result,
        "stage": "cos_gardener",
        "status": "policy",
        "mode": "policy",
        "role": role_status.get("role"),
        "note": "Policy-driven gardener; proposed actions pass through cos_actions policy.",
    }


def run_cos_synthesis(
    paths: BrainPaths,
    role_status: dict[str, Any],
    *,
    enabled: bool,
    run_id: str | None = None,
) -> dict[str, Any]:
    if not enabled:
        return {
            "stage": "cos_synthesis",
            "status": "skipped",
            "mode": "policy",
            "role": role_status.get("role"),
            "reason": "LLM wiki synthesis disabled by --no-llm-wiki",
        }
    if not role_status.get("can_run_mutation_capable_stages"):
        return cos_stage_skipped("cos_synthesis", role_status, mode="policy")
    result = generate_page_syntheses(paths, shadow=False, run_id=run_id)
    status = "skipped" if result.get("status") == "skipped" else "policy"
    return {
        **result,
        "stage": "cos_synthesis",
        "status": status,
        "mode": "policy",
        "role": role_status.get("role"),
        "note": "Policy-driven synthesis; proposed actions pass through cos_actions policy.",
    }


def run_cos_once(paths: BrainPaths, *, llm_wiki: bool = True) -> dict[str, Any]:
    service = BrainService(paths)
    service.init_workspace()
    run_id = new_id("cosrun")
    role_status = cos_role_status(paths)
    result = {
        "run_id": run_id,
        "cos_role": role_status,
        "cos_extraction": run_cos_extraction(paths, role_status, run_id=run_id),
        "cos_gardener": run_cos_gardener(paths, role_status, run_id=run_id),
        "cos_synthesis": run_cos_synthesis(
            paths, role_status, enabled=llm_wiki, run_id=run_id
        ),
        "cos_timeout_sweep": run_cos_timeout_sweep(paths),
    }
    result["cos_current_action_ids"] = current_cycle_action_ids(
        result["cos_extraction"],
        result["cos_gardener"],
        result["cos_synthesis"],
    )
    result["cos_audit"] = run_cos_audit(
        paths,
        role_status,
        run_id=run_id,
        action_ids=result["cos_current_action_ids"],
    )
    result["llm_usage"] = llm_usage_summary(paths, cycle_id=run_id, limit=1)
    return result


def run_cos_timeout_sweep(
    paths: BrainPaths, *, now: str | None = None, limit: int = 100
) -> dict[str, Any]:
    service = BrainService(paths)
    service.init_workspace()
    sweep_at = now or now_iso()
    resolved: list[dict[str, Any]] = []
    skipped_truth: list[dict[str, Any]] = []
    skipped_ineligible: list[dict[str, Any]] = []
    with connection(paths.sqlite_path) as conn:
        candidates = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM open_questions
                WHERE status IN ('open', 'needs_human')
                  AND auto_resolve_after IS NOT NULL
                  AND auto_resolve_after <= ?
                ORDER BY auto_resolve_after ASC, created_at ASC
                LIMIT ?
                """,
                (sweep_at, max(1, limit)),
            )
        ]
        for question in candidates:
            action_type = residue_action_type(question)
            summary = {
                "id": question["id"],
                "kind": question["kind"],
                "action_id": question.get("action_id"),
                "action_type": action_type,
            }
            if (
                str(question["kind"]) in TRUTH_RESIDUE_KINDS
                or action_type in TRUTH_ACTION_TYPES
            ):
                skipped_truth.append(summary)
                continue
            if action_type and action_type not in TIMEOUT_TO_UNCERTAINTY_ACTION_TYPES:
                skipped_ineligible.append(summary)
                continue
            answer = {
                "resolution": "uncertainty",
                "reason": "human review timeout elapsed without applying an action",
                "action_type": action_type,
            }
            conn.execute(
                """
                UPDATE open_questions
                SET status = 'timeout_resolved',
                    answer = ?,
                    decided_by = 'timeout_uncertainty',
                    answered_at = ?
                WHERE id = ?
                """,
                (dumps(answer), sweep_at, question["id"]),
            )
            action_id = question.get("action_id")
            if action_id:
                conn.execute(
                    """
                    UPDATE cos_actions
                    SET status = 'timed_out'
                    WHERE id = ? AND status = 'needs_human'
                    """,
                    (action_id,),
                )
            resolved.append(summary)
    return {
        "stage": "cos_timeout_sweep",
        "status": "ok",
        "checked": len(candidates),
        "resolved_count": len(resolved),
        "skipped_truth_count": len(skipped_truth),
        "skipped_ineligible_count": len(skipped_ineligible),
        "resolved": resolved,
        "skipped_truth": skipped_truth,
        "skipped_ineligible": skipped_ineligible,
    }


def residue_action_type(question: dict[str, Any]) -> str | None:
    for field in ("recommended_action", "context"):
        value = question.get(field)
        if not value:
            continue
        try:
            parsed = json.loads(str(value))
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict) and parsed.get("action_type"):
            return str(parsed["action_type"])
    return None


def run_cos_audit(
    paths: BrainPaths,
    role_status: dict[str, Any],
    *,
    run_id: str | None = None,
    action_ids: list[str] | None = None,
) -> dict[str, Any]:
    if not role_status.get("can_run_mutation_capable_stages"):
        return cos_stage_skipped("cos_audit", role_status, mode="stub")
    scope = (
        {"action_ids": action_ids}
        if action_ids is not None
        else {"action_run_id": run_id}
    )
    result = run_sampled_audit(
        paths, run_id=run_id, audit_origin="current_run", **scope
    )
    return {
        **result,
        "stage": "cos_audit",
        "role": role_status.get("role"),
    }


def current_cycle_action_ids(*stage_results: dict[str, Any]) -> list[str]:
    """Return the exact actions touched by the current extraction/gardening cycle."""

    action_ids: list[str] = []
    seen: set[str] = set()
    for result in stage_results:
        actions = result.get("actions") if isinstance(result, dict) else None
        if not isinstance(actions, list):
            continue
        for action in actions:
            if not isinstance(action, dict):
                continue
            action_id = str(action.get("id") or "").strip()
            if not action_id or action_id in seen:
                continue
            seen.add(action_id)
            action_ids.append(action_id)
    return action_ids


def index_status(
    paths: BrainPaths, service: BrainService | None = None
) -> dict[str, Any]:
    service = service or BrainService(paths)
    service.init_workspace()
    with connection(paths.sqlite_path) as conn:
        docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        fts = conn.execute("SELECT COUNT(*) FROM chunk_fts").fetchone()[0]
        run = conn.execute(
            "SELECT * FROM ingestion_runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    lancedb_exists = paths.lancedb_path.exists() and any(paths.lancedb_path.iterdir())
    lancedb = lancedb_stats(paths.lancedb_path)
    return {
        "documents": docs,
        "chunks": chunks,
        "fts_rows": fts,
        "lancedb_exists": lancedb_exists,
        "lancedb": lancedb,
        "embedding_provider": service.embedding_provider.name,
        "embedding": service.embedding_provider.status(check_available=False),
        "last_run": dict(run) if run else None,
    }


def run_index_maintenance(paths: BrainPaths) -> dict[str, Any]:
    try:
        fts_result = BrainService(paths).optimize_fts_indexes()
        before = lancedb_stats(paths.lancedb_path)
        if not should_optimize_vectors(before):
            vector_result = {
                "status": "skipped",
                "reason": "below LanceDB optimization thresholds",
                "before": before,
                "errors": [],
            }
        else:
            vector_result = optimize_vectors(
                paths.lancedb_path, cleanup_older_than_days=1
            )
        return {
            "status": "ok"
            if vector_result.get("status") == "ok" or fts_result.get("status") == "ok"
            else "skipped",
            "vectors": vector_result,
            "fts": fts_result,
            "errors": [],
        }
    except Exception as exc:
        return {"status": "failed", "error": str(exc), "errors": [str(exc)]}


def nightly_due(paths: BrainPaths, due_after_hours: int) -> bool:
    return automation_due(paths, NIGHTLY_JOB_NAME, due_after_hours)


def automation_due(paths: BrainPaths, job_name: str, due_after_hours: int) -> bool:
    last_success = last_successful_automation_run(paths, job_name)
    if not last_success:
        return True
    finished_at = parse_iso_datetime(last_success)
    return datetime.now(finished_at.tzinfo) - finished_at >= timedelta(
        hours=due_after_hours
    )


def active_historical_audit_findings(paths: BrainPaths) -> int:
    """Count unresolved semantic findings created by historical audit passes."""

    with connection(paths.sqlite_path) as conn:
        actions = reviewable_bad_audit_actions(conn)
    return sum(
        1
        for action in actions
        if action_audit_origin(action) not in {"manual", "current_run"}
    )


def action_audit_origin(action: dict[str, Any]) -> str:
    evidence = action.get("evidence_json") or {}
    audits = evidence.get("audits") if isinstance(evidence, dict) else None
    if not isinstance(audits, list):
        return "legacy_historical"
    for audit in reversed(audits):
        if not isinstance(audit, dict):
            continue
        metadata = audit.get("metadata")
        if isinstance(metadata, dict) and metadata.get("audit_origin"):
            return str(metadata["audit_origin"])
    return "legacy_historical"


def last_successful_automation_run(paths: BrainPaths, job_name: str) -> str | None:
    with connection(paths.sqlite_path) as conn:
        row = conn.execute(
            """
            SELECT finished_at
            FROM automation_runs
            WHERE job_name = ? AND status = 'success' AND finished_at IS NOT NULL
            ORDER BY finished_at DESC
            LIMIT 1
            """,
            (job_name,),
        ).fetchone()
    return str(row["finished_at"]) if row else None


def pending_weekly_historical_audit_cohort(
    paths: BrainPaths,
) -> tuple[str | None, list[str]]:
    """Return the newest unfinished cohort since the last successful weekly run."""

    state = pending_weekly_historical_audit_state(paths)
    return state.get("run_id"), list(state.get("action_ids") or [])


def pending_weekly_historical_audit_state(paths: BrainPaths) -> dict[str, Any]:
    """Return the exact unfinished cohort and its unchanged scan window."""

    with connection(paths.sqlite_path) as conn:
        rows = conn.execute(
            """
            WITH latest_success AS (
              SELECT rowid AS boundary_rowid, finished_at
              FROM automation_runs
              WHERE job_name = ?
                AND status = 'success'
                AND finished_at IS NOT NULL
              ORDER BY rowid DESC
              LIMIT 1
            )
            SELECT pending.id, pending.summary
            FROM automation_runs AS pending
            LEFT JOIN latest_success AS success ON 1 = 1
            WHERE pending.job_name = ?
              AND pending.status IN ('failed', 'running')
              AND (
                success.boundary_rowid IS NULL
                OR pending.rowid > success.boundary_rowid
              )
            ORDER BY pending.rowid DESC
            LIMIT ?
            """,
            (
                WEEKLY_HISTORICAL_AUDIT_JOB_NAME,
                WEEKLY_HISTORICAL_AUDIT_JOB_NAME,
                MAX_PENDING_WEEKLY_HISTORICAL_RUN_SCAN,
            ),
        ).fetchall()
    for row in rows:
        summary = loads(row["summary"], {})
        if not isinstance(summary, dict):
            continue
        raw_action_ids = summary.get("cohort_action_ids")
        if not isinstance(raw_action_ids, list):
            audit = summary.get("audit")
            raw_action_ids = (
                audit.get("sampled_action_ids") if isinstance(audit, dict) else None
            )
        if not isinstance(raw_action_ids, list):
            continue
        action_ids = normalized_action_ids(
            raw_action_ids,
            max_items=WEEKLY_HISTORICAL_AUDIT_LIMIT,
        )
        if action_ids:
            historical_scan = summary.get("historical_scan")
            remaining_action_ids = normalized_action_ids(
                summary.get("remaining_cohort_action_ids", action_ids),
                max_items=WEEKLY_HISTORICAL_AUDIT_LIMIT,
            )
            cohort = set(action_ids)
            return {
                "run_id": str(row["id"]),
                "action_ids": action_ids,
                "remaining_action_ids": [
                    action_id
                    for action_id in remaining_action_ids
                    if action_id in cohort
                ],
                "historical_scan": (
                    dict(historical_scan) if isinstance(historical_scan, dict) else {}
                ),
            }
    return {
        "run_id": None,
        "action_ids": [],
        "remaining_action_ids": [],
        "historical_scan": {},
    }


def select_weekly_historical_audit_cohort(
    paths: BrainPaths,
    limit: int,
) -> dict[str, Any]:
    """Select one semantic cohort without skipping its bounded scan window."""

    scan_state = last_successful_weekly_historical_scan_state(paths)
    scan_after = scan_state["scan_after"]
    retained_window_action_ids = list(scan_state["window_action_ids"])
    with connection(paths.sqlite_path) as conn:
        selection = select_historical_audit_cohort(
            conn,
            limit,
            scan_after=scan_after,
            window_action_ids=retained_window_action_ids,
        )
    action_ids = [str(action["id"]) for action in selection["actions"]]
    if retained_window_action_ids:
        window_action_ids = retained_window_action_ids
        window_end = scan_state["window_end"]
        reached_end = bool(scan_state["window_reached_end"])
    else:
        window_action_ids = normalized_historical_window_action_ids(
            selection.get("window_action_ids")
        )
        window_end = selection.get("window_end")
        reached_end = bool(selection.get("reached_end"))
    # A non-empty cohort pins the window. It may contain only five of many
    # eligible rows, so advancing to the window tail here would skip the rest.
    if action_ids:
        next_after = scan_after
        next_window_action_ids = window_action_ids
    elif reached_end:
        next_after = None
        next_window_action_ids = []
    else:
        next_after = window_end
        next_window_action_ids = []
    return {
        "action_ids": action_ids,
        "historical_scan": {
            "cursor_version": 2,
            "start_after": historical_scan_cursor_value(scan_after),
            "window_end": historical_scan_cursor_value(window_end),
            "window_action_ids": next_window_action_ids,
            "window_reached_end": reached_end,
            "next_after": historical_scan_cursor_value(next_after),
            "scanned_action_count": int(selection.get("scanned_action_count") or 0),
            "selected_action_count": len(action_ids),
            "reached_end": reached_end,
            "advanced": not action_ids and next_after != scan_after,
            "wrapped": not action_ids and reached_end and scan_after is not None,
        },
    }


def normalize_weekly_historical_cohort_selection(
    paths: BrainPaths,
    selection: Any,
) -> tuple[list[str], dict[str, Any]]:
    """Accept current structured selection and legacy/test list-shaped results."""

    if isinstance(selection, dict):
        raw_action_ids = selection.get("action_ids")
        raw_scan = selection.get("historical_scan")
        action_ids = normalized_action_ids(
            raw_action_ids,
            max_items=WEEKLY_HISTORICAL_AUDIT_LIMIT,
        )
        if isinstance(raw_scan, dict):
            return action_ids, dict(raw_scan)
    else:
        action_ids = normalized_action_ids(
            selection,
            max_items=WEEKLY_HISTORICAL_AUDIT_LIMIT,
        )
    scan = unadvanced_weekly_historical_scan(paths)
    scan["selected_action_count"] = len(action_ids)
    return action_ids, scan


def normalized_action_ids(
    value: Any,
    *,
    max_items: int | None = None,
) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    action_ids = list(
        dict.fromkeys(
            action_id.strip()
            for action_id in value
            if isinstance(action_id, str) and action_id.strip()
        )
    )
    if max_items is None:
        return action_ids
    return action_ids[: max(0, max_items)]


def normalized_historical_window_action_ids(value: Any) -> list[str]:
    return normalized_action_ids(
        value,
        max_items=HISTORICAL_AUDIT_CANDIDATE_SCAN_CAP,
    )


def eligible_weekly_historical_action_ids(
    paths: BrainPaths,
    action_ids: list[str],
) -> list[str]:
    """Return cohort IDs that remain unresolved and eligible for an audit."""

    normalized = normalized_action_ids(action_ids)
    if not normalized:
        return []
    with connection(paths.sqlite_path) as conn:
        actions = load_audit_sample(
            conn,
            len(normalized),
            action_ids=normalized,
            historical=True,
        )
    eligible = {str(action["id"]) for action in actions}
    return [action_id for action_id in normalized if action_id in eligible]


def last_successful_weekly_historical_scan_cursor(
    paths: BrainPaths,
) -> tuple[str, str] | None:
    return last_successful_weekly_historical_scan_state(paths)["scan_after"]


def last_successful_weekly_historical_scan_state(
    paths: BrainPaths,
) -> dict[str, Any]:
    """Return the next cursor and any exact frozen window from the last success."""

    with connection(paths.sqlite_path) as conn:
        row = conn.execute(
            """
            SELECT summary
            FROM automation_runs
            WHERE job_name = ?
              AND status = 'success'
              AND finished_at IS NOT NULL
            ORDER BY rowid DESC
            LIMIT 1
            """,
            (WEEKLY_HISTORICAL_AUDIT_JOB_NAME,),
        ).fetchone()
    summary = loads(row["summary"], {}) if row else {}
    scan = summary.get("historical_scan") if isinstance(summary, dict) else None
    if not isinstance(scan, dict):
        scan = {}
    window_action_ids = normalized_historical_window_action_ids(
        scan.get("window_action_ids")
    )
    return {
        "scan_after": historical_scan_cursor(scan.get("next_after")),
        "window_action_ids": window_action_ids,
        "window_end": (
            historical_scan_cursor(scan.get("window_end"))
            if window_action_ids
            else None
        ),
        "window_reached_end": (
            bool(scan.get("window_reached_end")) if window_action_ids else False
        ),
    }


def unadvanced_weekly_historical_scan(paths: BrainPaths) -> dict[str, Any]:
    scan_state = last_successful_weekly_historical_scan_state(paths)
    scan_after = scan_state["scan_after"]
    cursor = historical_scan_cursor_value(scan_after)
    return {
        "cursor_version": 2,
        "start_after": cursor,
        "window_end": historical_scan_cursor_value(scan_state["window_end"]),
        "window_action_ids": list(scan_state["window_action_ids"]),
        "window_reached_end": bool(scan_state["window_reached_end"]),
        "next_after": cursor,
        "scanned_action_count": 0,
        "reached_end": False,
        "advanced": False,
        "wrapped": False,
    }


def historical_scan_cursor(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        return None
    raw_sort_at = value.get("sort_at")
    raw_action_id = value.get("action_id")
    if raw_sort_at is None or raw_action_id is None:
        return None
    sort_at = str(raw_sort_at)
    action_id = str(raw_action_id)
    # The keyset expression uses SQL COALESCE semantics, where an empty
    # applied_at is a real sort value rather than a missing timestamp.
    if not action_id:
        return None
    return sort_at, action_id


def historical_scan_cursor_value(
    cursor: tuple[str, str] | None,
) -> dict[str, str] | None:
    if cursor is None:
        return None
    return {"sort_at": cursor[0], "action_id": cursor[1]}


def parse_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def record_automation_start(
    paths: BrainPaths, run_id: str, job_name: str, started_at: str
) -> None:
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO automation_runs(id, job_name, started_at, status)
            VALUES (?, ?, ?, ?)
            """,
            (run_id, job_name, started_at, "running"),
        )


def record_automation_progress(
    paths: BrainPaths, run_id: str, summary: dict[str, Any]
) -> None:
    """Persist resumable automation state without marking the run complete."""

    compacted_summary = bounded_automation_summary(summary)
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE automation_runs
            SET summary = ?
            WHERE id = ?
            """,
            (dumps(compacted_summary), run_id),
        )


def record_automation_finish(
    paths: BrainPaths,
    run_id: str,
    status: str,
    finished_at: str,
    summary: dict[str, Any],
    error: str | None,
) -> None:
    compacted_summary = bounded_automation_summary(summary)
    compacted_error = compact_error_text(error) if error is not None else None
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE automation_runs
            SET finished_at = ?, status = ?, summary = ?, error = ?
            WHERE id = ?
            """,
            (finished_at, status, dumps(compacted_summary), compacted_error, run_id),
        )


def compact_automation_errors(value: Any) -> Any:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, nested in value.items():
            if normalized_error_key(key) in ERROR_FIELD_NAMES:
                output[key] = compact_error_value(nested)
            else:
                output[key] = compact_automation_errors(nested)
        return output
    if isinstance(value, list):
        return [compact_automation_errors(item) for item in value]
    return value


def bounded_automation_summary(summary: dict[str, Any]) -> dict[str, Any]:
    control_state = durable_automation_control_state(summary)
    compacted = compact_automation_summary(compact_automation_errors(summary))
    compacted.update(control_state)
    serialized = dumps(compacted)
    byte_count = len(serialized.encode("utf-8"))
    if byte_count <= MAX_STORED_SUMMARY_BYTES:
        return compacted
    fallback = {
        "truncated": True,
        "original_bytes": byte_count,
        "keys": sorted(str(key) for key in summary)[:MAX_STORED_SUMMARY_DICT_ITEMS],
    }
    for key in ("status", "reason", "queue_summary", "telemetry_retention", "errors"):
        if key in compacted:
            fallback[key] = compacted[key]
    fallback.update(control_state)
    return fallback


def durable_automation_control_state(summary: dict[str, Any]) -> dict[str, Any]:
    """Keep small retry/cursor state intact when diagnostics are pruned."""

    output: dict[str, Any] = {}
    for key in ("cohort_action_ids", "remaining_cohort_action_ids"):
        if key in summary:
            output[key] = normalized_action_ids(
                summary.get(key),
                max_items=WEEKLY_HISTORICAL_AUDIT_LIMIT,
            )
    if "cohort_retry_of_run_id" in summary:
        retry_run_id = summary.get("cohort_retry_of_run_id")
        output["cohort_retry_of_run_id"] = (
            compact_error_text(retry_run_id, max_chars=MAX_STORED_SUMMARY_CHARS)
            if isinstance(retry_run_id, str)
            else None
        )
    if "historical_scan" in summary:
        output["historical_scan"] = durable_historical_scan_state(
            summary.get("historical_scan")
        )
    return output


def durable_historical_scan_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    output: dict[str, Any] = {}
    if isinstance(value.get("cursor_version"), int):
        output["cursor_version"] = max(0, int(value["cursor_version"]))
    for key in ("start_after", "window_end", "next_after"):
        if key not in value:
            continue
        cursor = historical_scan_cursor(value.get(key))
        output[key] = historical_scan_cursor_value(cursor)
    if "window_action_ids" in value:
        output["window_action_ids"] = normalized_historical_window_action_ids(
            value.get("window_action_ids")
        )
    for key in ("scanned_action_count", "selected_action_count"):
        if isinstance(value.get(key), int):
            output[key] = max(0, int(value[key]))
    for key in ("window_reached_end", "reached_end", "advanced", "wrapped"):
        if isinstance(value.get(key), bool):
            output[key] = value[key]
    return output


def compact_automation_summary(value: Any, *, depth: int = 0) -> Any:
    if depth >= MAX_STORED_SUMMARY_DEPTH:
        return "[maximum summary depth reached]"
    if isinstance(value, dict):
        items = list(value.items())
        output = {
            str(key): compact_automation_summary(nested, depth=depth + 1)
            for key, nested in items[:MAX_STORED_SUMMARY_DICT_ITEMS]
        }
        omitted = len(items) - len(output)
        if omitted > 0:
            output["_omitted_keys"] = omitted
        return output
    if isinstance(value, list):
        output = [
            compact_automation_summary(item, depth=depth + 1)
            for item in value[:MAX_STORED_SUMMARY_LIST_ITEMS]
        ]
        omitted = len(value) - len(output)
        if omitted > 0:
            output.append({"_omitted_items": omitted})
        return output
    if isinstance(value, str):
        return compact_error_text(value, max_chars=MAX_STORED_SUMMARY_CHARS)
    return value


def compact_error_value(value: Any) -> Any:
    if isinstance(value, str):
        return compact_error_text(value)
    if isinstance(value, list):
        output = [
            compact_error_value(item) for item in value[:MAX_STORED_ERROR_LIST_ITEMS]
        ]
        omitted = len(value) - len(output)
        if omitted > 0:
            output.append(f"[omitted {omitted} additional error item(s)]")
        return output
    if isinstance(value, dict):
        return {key: compact_error_value(nested) for key, nested in value.items()}
    return value


def compact_error_text(text: str, max_chars: int = MAX_STORED_ERROR_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    head_chars = max(1, int(max_chars * 0.7))
    tail_chars = max(1, max_chars - head_chars - 120)
    omitted = len(text) - head_chars - tail_chars
    return (
        text[:head_chars].rstrip()
        + f"\n[truncated {omitted} chars; sha256={digest}]\n"
        + text[-tail_chars:].lstrip()
    )


def normalized_error_key(key: str) -> str:
    return key.lower().replace("-", "_")


def launch_agent_path() -> Path:
    return Path("~/Library/LaunchAgents").expanduser() / f"{LAUNCH_AGENT_LABEL}.plist"


def nightly_launch_agent_path() -> Path:
    return (
        Path("~/Library/LaunchAgents").expanduser()
        / f"{NIGHTLY_LAUNCH_AGENT_LABEL}.plist"
    )


def render_launch_agent(
    repo_path: Path,
    brain_home: Path,
    uv_path: Path,
    interval: int = 600,
    include_hyprnote: bool = False,
) -> dict[str, Any]:
    args = [
        str(uv_path),
        "run",
        "brain",
        "automation",
        "run-agent-log-ingest",
        "--home",
        str(brain_home),
    ]
    if include_hyprnote:
        args.append("--include-hyprnote")
    command = f"cd {shlex.quote(str(repo_path))} && {shlex.join(args)}"
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": ["/bin/zsh", "-lc", command],
        "StartInterval": interval,
        "RunAtLoad": True,
        "StandardOutPath": str(brain_home / "logs" / "launchagent.out.log"),
        "StandardErrorPath": str(brain_home / "logs" / "launchagent.err.log"),
        "WorkingDirectory": str(repo_path),
    }


def render_nightly_launch_agent(
    repo_path: Path,
    brain_home: Path,
    uv_path: Path,
    interval: int = 3600,
    due_after_hours: int = 20,
    with_llm_memory_proposals: bool = False,
    llm_wiki: bool = True,
    provider: str | None = None,
) -> dict[str, Any]:
    args = [
        str(uv_path),
        "run",
        "brain",
        "automation",
        "nightly",
        "--if-due",
        "--due-after-hours",
        str(due_after_hours),
        "--home",
        str(brain_home),
    ]
    if with_llm_memory_proposals:
        args.append("--with-llm-memory-proposals")
    if not llm_wiki:
        args.append("--no-llm-wiki")
    llm_provider = provider or (
        DEFAULT_LLM_PROVIDER if with_llm_memory_proposals else None
    )
    if with_llm_memory_proposals:
        if llm_provider:
            args.extend(["--provider", llm_provider])
    command = f"cd {shlex.quote(str(repo_path))} && {shlex.join(args)}"
    plist = {
        "Label": NIGHTLY_LAUNCH_AGENT_LABEL,
        "ProgramArguments": ["/bin/zsh", "-lc", command],
        "StartInterval": interval,
        "RunAtLoad": True,
        "StandardOutPath": str(brain_home / "logs" / "nightly-maintenance.out.log"),
        "StandardErrorPath": str(brain_home / "logs" / "nightly-maintenance.err.log"),
        "WorkingDirectory": str(repo_path),
    }
    environment = {}
    if with_llm_memory_proposals:
        if llm_provider:
            environment["PKM_BRAIN_LLM_PROVIDER"] = llm_provider
        if llm_provider == "openai":
            environment["PKM_BRAIN_OPENAI_MODEL"] = os.environ.get(
                "PKM_BRAIN_OPENAI_MODEL", OPENAI_DEFAULT_MODEL
            )
            if os.environ.get("PKM_BRAIN_OPENAI_MODEL_FALLBACKS"):
                environment["PKM_BRAIN_OPENAI_MODEL_FALLBACKS"] = os.environ[
                    "PKM_BRAIN_OPENAI_MODEL_FALLBACKS"
                ]
        if llm_provider == "codex":
            environment["PKM_BRAIN_CODEX_MODEL"] = os.environ.get(
                "PKM_BRAIN_CODEX_MODEL", CODEX_DEFAULT_MODEL
            )
            if os.environ.get("PKM_BRAIN_CODEX_MODEL_FALLBACKS"):
                environment["PKM_BRAIN_CODEX_MODEL_FALLBACKS"] = os.environ[
                    "PKM_BRAIN_CODEX_MODEL_FALLBACKS"
                ]
            codex_bin = os.environ.get("PKM_BRAIN_CODEX_BIN") or shutil.which("codex")
            if codex_bin:
                environment["PKM_BRAIN_CODEX_BIN"] = codex_bin
            environment["PKM_BRAIN_CODEX_CWD"] = str(repo_path)
    if llm_wiki:
        codex_bin = os.environ.get("PKM_BRAIN_CODEX_BIN") or shutil.which("codex")
        if codex_bin:
            environment.setdefault("PKM_BRAIN_CODEX_BIN", codex_bin)
            environment.setdefault("PKM_BRAIN_CODEX_CWD", str(repo_path))
    if environment:
        plist["EnvironmentVariables"] = environment
    return plist


def install_launch_agent(
    repo_path: Path,
    brain_home: Path,
    uv_path: Path,
    interval: int = 600,
    include_hyprnote: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    plist = render_launch_agent(
        repo_path, brain_home, uv_path, interval, include_hyprnote=include_hyprnote
    )
    path = launch_agent_path()
    if dry_run:
        return {"path": str(path), "plist": plist, "installed": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    brain_home.joinpath("logs").mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist, sort_keys=False))
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], check=True)
    subprocess.run(
        ["launchctl", "enable", f"gui/{uid}/{LAUNCH_AGENT_LABEL}"], check=True
    )
    return {"path": str(path), "plist": plist, "installed": True}


def install_nightly_launch_agent(
    repo_path: Path,
    brain_home: Path,
    uv_path: Path,
    interval: int = 3600,
    due_after_hours: int = 20,
    with_llm_memory_proposals: bool = False,
    llm_wiki: bool = True,
    provider: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    plist = render_nightly_launch_agent(
        repo_path,
        brain_home,
        uv_path,
        interval,
        due_after_hours,
        with_llm_memory_proposals=with_llm_memory_proposals,
        llm_wiki=llm_wiki,
        provider=provider,
    )
    path = nightly_launch_agent_path()
    if dry_run:
        return {"path": str(path), "plist": plist, "installed": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    brain_home.joinpath("logs").mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist, sort_keys=False))
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], check=True)
    subprocess.run(
        ["launchctl", "enable", f"gui/{uid}/{NIGHTLY_LAUNCH_AGENT_LABEL}"], check=True
    )
    return {"path": str(path), "plist": plist, "installed": True}


def uninstall_launch_agent() -> dict[str, Any]:
    path = launch_agent_path()
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if path.exists():
        path.unlink()
    return {"path": str(path), "installed": False}


def uninstall_nightly_launch_agent() -> dict[str, Any]:
    path = nightly_launch_agent_path()
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if path.exists():
        path.unlink()
    return {"path": str(path), "installed": False}


def launch_agent_status() -> dict[str, Any]:
    path = launch_agent_path()
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    proc = subprocess.run(
        ["launchctl", "print", f"gui/{uid}/{LAUNCH_AGENT_LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "path": str(path),
        "plist_exists": path.exists(),
        "loaded": proc.returncode == 0,
        "launchctl_output": proc.stdout if proc.returncode == 0 else proc.stderr,
    }


def nightly_launch_agent_status() -> dict[str, Any]:
    path = nightly_launch_agent_path()
    uid = subprocess.check_output(["id", "-u"], text=True).strip()
    proc = subprocess.run(
        ["launchctl", "print", f"gui/{uid}/{NIGHTLY_LAUNCH_AGENT_LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "path": str(path),
        "plist_exists": path.exists(),
        "loaded": proc.returncode == 0,
        "launchctl_output": proc.stdout if proc.returncode == 0 else proc.stderr,
        "plist_validation": validate_nightly_launch_agent_plist(path),
    }


def validate_nightly_launch_agent_plist(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "status": "missing",
            "valid": False,
            "unknown_flags": [],
            "warnings": ["plist does not exist"],
        }
    try:
        plist = plistlib.loads(path.read_bytes())
    except Exception as exc:
        return {
            "status": "invalid",
            "valid": False,
            "unknown_flags": [],
            "warnings": [f"could not parse plist: {exc}"],
        }
    args = plist.get("ProgramArguments")
    if not isinstance(args, list) or not args:
        return {
            "status": "invalid",
            "valid": False,
            "unknown_flags": [],
            "warnings": ["ProgramArguments is missing or empty"],
        }
    command = str(args[-1])
    tokens = shlex.split(command)
    nightly_index = find_nightly_command_index(tokens)
    if nightly_index is None:
        return {
            "status": "invalid",
            "valid": False,
            "unknown_flags": [],
            "warnings": [
                "ProgramArguments does not contain `brain automation nightly`"
            ],
            "command": command,
        }
    command_tokens = tokens[nightly_index + 3 :]
    unknown_flags = sorted(
        {
            token.split("=", 1)[0]
            for token in command_tokens
            if token.startswith("--")
            and token.split("=", 1)[0] not in NIGHTLY_AUTOMATION_FLAGS
        }
    )
    warnings = [f"unknown nightly flag: {flag}" for flag in unknown_flags]
    return {
        "status": "ok" if not unknown_flags else "warning",
        "valid": not unknown_flags,
        "unknown_flags": unknown_flags,
        "warnings": warnings,
        "command": command,
    }


def find_nightly_command_index(tokens: list[str]) -> int | None:
    for index in range(0, max(0, len(tokens) - 2)):
        if tokens[index : index + 3] == ["brain", "automation", "nightly"]:
            return index
    return None


def as_jsonable(
    result: (
        AutomationResult
        | NightlyMaintenanceResult
        | SecondaryTickResult
        | WeeklyHistoricalAuditResult
    ),
) -> dict[str, Any]:
    return json.loads(json.dumps(result.__dict__))
