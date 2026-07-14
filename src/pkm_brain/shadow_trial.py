from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .gmail_operations import (
    GMAIL_DETECTOR_VERSION,
    GmailDetectionBatchResult,
    GmailThreadDetection,
    budget_from_operations_policy,
    detect_gmail_threads,
)
from .gmail_llm import get_gmail_provider, gmail_detector_token_ceiling
from .gmail_mirror import (
    GmailMirrorCheckpoint,
    GmailMirrorGenerationConflict,
    GmailMirrorStore,
)
from .gmail_sync import GmailMirrorSynchronizer
from .google_api import GoogleAPIClient, GoogleQuotaBudget, GoogleTokenManager
from .google_cache import GoogleEvidenceCache
from .google_normalization import NormalizedCalendarEvent, NormalizedGmailThread
from .google_sources import (
    GmailThreadReader,
    GoogleCalendarReader,
    calendar_event_is_inactive,
    calendar_occurrence_key,
)
from .llm import LLMProvider
from .llm_usage import ProviderUsageRecord, capture_provider_usage
from .operational_briefing import build_operational_briefing
from .operational_budget import DailyBudgetExceeded
from .operational_db import operational_connection
from .operational_meeting_packets import precompose_upcoming_meeting_packets
from .operational_service import OperationalService
from .operational_shadow import HandledAssessment, ShadowDecision
from .operational_state import (
    MAX_PENDING_THREAD_IDS_BYTES,
    OperationalObservation,
    SourceCursorUpdate,
    get_source_cursor,
    operational_item_id,
)
from .operations_policy import OperationsPolicy, load_operations_policy
from .paths import BrainPaths


CALENDAR_CONNECTOR_ID = "calendar"
GMAIL_CONNECTOR_ID = "gmail"
CALENDAR_STREAM = "primary"
GMAIL_STREAM = "mailbox"
GMAIL_MANUAL_RUN_MAX_THREADS = 200
CALENDAR_INITIAL_PAST_DAYS = 14
CALENDAR_INITIAL_FUTURE_DAYS = 90
CALENDAR_ADAPTER_VERSION = "google-calendar-operations-v1"
BRIEFING_FRESHNESS_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class ShadowTrialResult:
    run: dict[str, Any]
    briefing: dict[str, Any]
    coverage: dict[str, Any]
    usage: dict[str, Any]
    counts: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "run": self.run,
            "briefing": self.briefing,
            "coverage": self.coverage,
            "usage": self.usage,
            "counts": self.counts,
        }


class ShadowTrialRunner:
    """Run the read-only Calendar/Gmail operational trial behind the daemon lease."""

    def __init__(
        self,
        paths: BrainPaths,
        operational_service: OperationalService,
        *,
        calendar_reader: GoogleCalendarReader | None = None,
        gmail_reader: GmailThreadReader | None = None,
        llm_provider: LLMProvider | None = None,
        cache: GoogleEvidenceCache | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.paths = paths
        self.operational_service = operational_service
        self._calendar_reader = calendar_reader
        self._gmail_reader = gmail_reader
        self._llm_provider = llm_provider
        self.cache = cache or GoogleEvidenceCache.for_paths(paths)
        self.gmail_mirror = GmailMirrorStore(paths.gmail_mirror_sqlite_path)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def run_live(
        self,
        *,
        sources: Sequence[str] | None = None,
        policy: OperationsPolicy | None = None,
    ) -> ShadowTrialResult:
        active_policy = policy or load_operations_policy(self.paths)
        requested = _requested_sources(active_policy, sources)
        if not requested:
            raise ValueError("at least one enabled shadow source is required")
        self.operational_service.initialize()
        started = _utc(self._now()).replace(microsecond=0)
        run = self.operational_service.start_shadow_run(
            mode="live",
            requested_sources=requested,
            policy_version=active_policy.version_ref,
            detector_version=(
                GMAIL_DETECTOR_VERSION if GMAIL_CONNECTOR_ID in requested else None
            ),
            started_at=started.isoformat(),
        )
        coverage: dict[str, Any] = {}
        usage: dict[str, Any] = {}
        counts: Counter[str] = Counter()
        errors: list[str] = []
        try:
            return self._run_started(
                active_policy=active_policy,
                requested=requested,
                run=run,
                started=started,
                coverage=coverage,
                usage=usage,
                counts=counts,
                errors=errors,
            )
        except BaseException as exc:
            terminal_status = "failed" if isinstance(exc, Exception) else "stopped"
            message = f"{type(exc).__name__}: {exc}"[:4000]
            failure_coverage = dict(coverage)
            failure_coverage["system"] = {
                "status": "unavailable",
                "fresh_at": None,
                "reason": "shadow_run_interrupted",
            }
            failure_error = "\n".join(dict.fromkeys([*errors, message]))[:4000]
            try:
                self.operational_service.finish_shadow_run(
                    str(run["id"]),
                    status=terminal_status,
                    coverage=failure_coverage,
                    usage=usage,
                    counts=dict(counts),
                    error=failure_error,
                    hard_stop_reason="shadow_run_interrupted",
                    finished_at=_utc(self._now()).replace(microsecond=0).isoformat(),
                )
            except Exception:
                # Preserve the initiating failure. A restarted controller also
                # recognizes and recovers any row left running here.
                pass
            raise

    def _run_started(
        self,
        *,
        active_policy: OperationsPolicy,
        requested: Sequence[str],
        run: Mapping[str, Any],
        started: datetime,
        coverage: dict[str, Any],
        usage: dict[str, Any],
        counts: Counter[str],
        errors: list[str],
    ) -> ShadowTrialResult:
        for source in requested:
            try:
                if source == CALENDAR_CONNECTOR_ID:
                    source_coverage, source_usage, source_counts = self._run_calendar(
                        active_policy,
                        run_id=str(run["id"]),
                        started=started,
                    )
                elif source == GMAIL_CONNECTOR_ID:
                    source_coverage, source_usage, source_counts = self._run_gmail(
                        active_policy,
                        run_id=str(run["id"]),
                        started=started,
                    )
                else:
                    raise ValueError(f"unsupported shadow source: {source}")
                coverage[source] = source_coverage
                usage[source] = source_usage
                counts.update(source_counts)
            except DailyBudgetExceeded as exc:
                message = f"{type(exc).__name__}: {exc}"[:1000]
                coverage[source] = {
                    "status": "partial",
                    "fresh_at": None,
                    "deferred_count": 1,
                    "reason": "daily_budget_exhausted",
                    "error": message,
                }
                usage[source] = {}
                errors.append(f"{source}: {message}")
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"[:1000]
                coverage[source] = {
                    "status": "unavailable",
                    "fresh_at": None,
                    "deferred_count": 0,
                    "error": message,
                }
                usage[source] = {}
                errors.append(f"{source}: {message}")
        retention = self.cache.prune(now=started)
        usage["retention"] = retention.as_dict()
        if retention.errors:
            coverage["retention"] = {
                "status": "partial",
                "fresh_at": started.isoformat(),
                "error_count": len(retention.errors),
            }
            errors.append(
                f"retention: {len(retention.errors)} cache entries could not be pruned"
            )
        try:
            usage["briefing_retention"] = (
                self.operational_service.prune_expired_briefing_snapshots(
                    as_of=started.isoformat()
                )
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:1000]
            coverage["retention"] = {
                "status": "partial",
                "fresh_at": started.isoformat(),
                "error": message,
            }
            errors.append(f"briefing retention: {message}")
        if CALENDAR_CONNECTOR_ID in requested:
            try:
                usage["meeting_preparation"] = precompose_upcoming_meeting_packets(
                    self.paths,
                    self.operational_service,
                    as_of=started.isoformat(),
                )
            except Exception as exc:
                # Preparation is a derived local projection. A failure must not
                # invalidate otherwise complete read-only source coverage.
                usage["meeting_preparation"] = {
                    "status": "partial",
                    "prepared_count": 0,
                    "error": f"{type(exc).__name__}: {exc}"[:1000],
                }
        statuses = {
            str(value.get("status") or "unavailable")
            for value in coverage.values()
        }
        if statuses and statuses <= {"complete"}:
            run_status = "complete"
        elif "complete" in statuses or "partial" in statuses:
            run_status = "partial"
        else:
            run_status = "failed"
        finished = _utc(self._now()).replace(microsecond=0)
        run_context = {
            **dict(run),
            "status": run_status,
            "coverage": coverage,
            "usage": usage,
            "counts": dict(counts),
            "error": "\n".join(errors)[:4000] or None,
            "finished_at": finished.isoformat(),
        }
        briefing = build_operational_briefing(
            self.paths.ops_sqlite_path,
            timezone_name=active_policy.operator.timezone,
            policy_version=active_policy.version_ref,
            as_of=finished.isoformat(),
            provider_accounts={
                "calendar": active_policy.operator.calendar.email,
                "gmail": active_policy.operator.gmail.email,
            },
            required_sources=_enabled_sources(active_policy),
            fresh_after_seconds=BRIEFING_FRESHNESS_SECONDS,
            run_context=run_context,
        )
        snapshot = self.operational_service.save_briefing_snapshot(
            run_id=str(run["id"]),
            generated_at=str(briefing["generated_at"]),
            as_of=str(briefing["as_of"]),
            timezone_name=active_policy.operator.timezone,
            policy_version=active_policy.version_ref,
            status=str(briefing["status"]),
            sections=briefing["sections"],
            coverage=briefing["coverage"],
            counts=briefing["counts"],
            retention_days=active_policy.privacy.normalized_evidence_days,
        )
        briefing["briefing_id"] = snapshot["id"]
        finished_run = self.operational_service.finish_shadow_run(
            str(run["id"]),
            status=run_status,
            coverage=coverage,
            usage=usage,
            counts=dict(counts),
            error="\n".join(errors)[:4000] or None,
            finished_at=finished.isoformat(),
        )
        return ShadowTrialResult(
            run=finished_run,
            briefing=briefing,
            coverage=coverage,
            usage=usage,
            counts=dict(counts),
        )

    def _calendar_reader_for(
        self,
        policy: OperationsPolicy,
        *,
        run_id: str,
        started: datetime,
    ) -> GoogleCalendarReader:
        if self._calendar_reader is not None:
            return self._calendar_reader
        tokens = GoogleTokenManager(
            self.paths,
            CALENDAR_CONNECTOR_ID,
            expected_email=policy.operator.calendar.email,
            expected_subject=policy.operator.calendar.provider_subject,
            require_exact_scopes=True,
        )
        client = GoogleAPIClient(
            CALENDAR_CONNECTOR_ID,
            tokens,
            quota=GoogleQuotaBudget(
                requests_per_second=2.0,
                on_acquire=self._api_budget_reserver(
                    source_type=CALENDAR_CONNECTOR_ID,
                    limit=policy.budgets.calendar.requests_per_day,
                    policy=policy,
                    run_id=run_id,
                    started=started,
                ),
            ),
        )
        return GoogleCalendarReader(
            client,
            calendar_id="primary",
            max_pages=min(20, policy.budgets.calendar.requests_per_day),
        )

    def _api_budget_reserver(
        self,
        *,
        source_type: str,
        limit: int,
        policy: OperationsPolicy,
        run_id: str,
        started: datetime,
    ) -> Callable[[int], None]:
        local_day = started.astimezone(ZoneInfo(policy.operator.timezone)).date().isoformat()

        def reserve(quota_units: int) -> None:
            self.operational_service.reserve_daily_budgets(
                source_type=source_type,
                reservations={
                    "api_requests": (1, limit),
                    "api_quota_units": (quota_units, None),
                },
                local_day=local_day,
                policy_version=policy.version_ref,
                run_id=run_id,
                created_at=started.isoformat(),
            )

        return reserve

    def _run_calendar(
        self,
        policy: OperationsPolicy,
        *,
        run_id: str,
        started: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
        account_key = policy.sources.calendar.account_key
        current = get_source_cursor(
            self.paths.ops_sqlite_path,
            CALENDAR_CONNECTOR_ID,
            account_key,
            CALENDAR_STREAM,
        )
        local_zone = ZoneInfo(policy.operator.timezone)
        local_now = started.astimezone(local_zone)
        cursor_metadata = dict((current or {}).get("metadata") or {})
        continuation_page_token = _optional_cursor_text(
            cursor_metadata.get("continuation_page_token")
        )
        continuation_mode = _continuation_mode(cursor_metadata)
        resume_full_rebuild = continuation_mode == "full"
        prior_reset_rebuild = bool(cursor_metadata.get("reset_rebuild"))
        prior_reset_started_generation = _optional_cursor_generation(
            cursor_metadata.get("reset_started_generation")
        )
        prior_reset_seen_item_ids, prior_reset_seen_overflow = _reset_seen_item_ids(
            cursor_metadata
        )
        if (
            prior_reset_rebuild
            and resume_full_rebuild
            and prior_reset_started_generation is None
        ):
            raise ValueError("Calendar reset continuation is missing its start generation")
        time_min = (
            _required_cursor_text(cursor_metadata, "window_start")
            if continuation_page_token
            else (local_now - timedelta(days=CALENDAR_INITIAL_PAST_DAYS)).isoformat()
        )
        time_max = (
            _required_cursor_text(cursor_metadata, "window_end")
            if continuation_page_token
            else (local_now + timedelta(days=CALENDAR_INITIAL_FUTURE_DAYS)).isoformat()
        )
        result = self._calendar_reader_for(
            policy,
            run_id=run_id,
            started=started,
        ).fetch(
            time_min=time_min,
            time_max=time_max,
            sync_token=(
                None
                if resume_full_rebuild
                else str(current["cursor"])
                if current and current.get("cursor")
                else None
            ),
            timezone_name=policy.operator.timezone,
            continuation_page_token=continuation_page_token,
        )
        next_generation = int((current or {}).get("generation") or 0) + 1
        if result.reset_required:
            reset_scan_active = True
            reset_started_generation = next_generation
            reset_seen_item_ids: set[str] = set()
            reset_seen_overflow = False
        elif prior_reset_rebuild and resume_full_rebuild and result.mode == "full":
            reset_scan_active = True
            reset_started_generation = prior_reset_started_generation
            reset_seen_item_ids = prior_reset_seen_item_ids
            reset_seen_overflow = prior_reset_seen_overflow
        else:
            reset_scan_active = False
            reset_started_generation = None
            reset_seen_item_ids = set()
            reset_seen_overflow = False
        raw_by_id = {
            str(raw.get("id") or ""): raw
            for raw in result.raw_events
            if str(raw.get("id") or "")
        }
        observations: list[OperationalObservation] = []
        decisions: list[ShadowDecision] = []
        for event in result.events:
            raw = raw_by_id.get(event.event_id) or {}
            source_ref = _calendar_source_ref(account_key, event.event_id)
            self.cache.write_raw(CALENDAR_CONNECTOR_ID, source_ref, raw, cached_at=started)
            observation = _calendar_observation(
                event,
                account_key=account_key,
                source_order=next_generation,
                source_ref=source_ref,
                checkpoint=result.next_sync_token
                or str((current or {}).get("cursor") or "full"),
                observed_at=started.isoformat(),
                default_timezone=policy.operator.timezone,
            )
            self.cache.write_normalized(
                CALENDAR_CONNECTOR_ID,
                source_ref,
                event.as_dict(),
                cached_at=started,
                source_revision=observation.source_revision,
            )
            observations.append(observation)
            decisions.append(
                ShadowDecision(
                    source_type="calendar",
                    account_key=account_key,
                    stream_key=CALENDAR_STREAM,
                    source_key=observation.source_key,
                    source_revision=observation.source_revision,
                    disposition=(
                        "suppressed" if _calendar_suppressed(event) else "surfaced"
                    ),
                    reason_code=(
                        "calendar_cancelled_or_declined"
                        if _calendar_suppressed(event)
                        else "calendar_schedule"
                    ),
                    item_ids=(operational_item_id(observation),),
                    evidence_refs=tuple(observation.evidence_refs),
                    confidence=1.0,
                    metadata={"subject": event.title},
                )
            )
        active_calendar_items = _active_calendar_items(
            self.paths.ops_sqlite_path,
            account_key=account_key,
        )
        fetched_keys = {_calendar_source_key(event) for event in result.events}
        unresolved_revalidation_ids = {
            str(item["id"])
            for item in active_calendar_items
            if str(item.get("current_source_revision") or "").startswith(
                "calendar-revalidation-"
            )
            and str(item["source_key"]) not in fetched_keys
            and not _human_confirmed_current_observation(item)
        }
        calendar_revalidation_ids: set[str] = set()
        if reset_scan_active:
            seen_on_page = {
                operational_item_id(observation) for observation in observations
            }
            seen_on_page.update(
                str(item["id"])
                for item in active_calendar_items
                if str(item["source_key"]) in fetched_keys
            )
            reset_seen_item_ids, overflowed = _merge_reset_seen_item_ids(
                reset_seen_item_ids,
                seen_on_page,
            )
            reset_seen_overflow = reset_seen_overflow or overflowed
        if reset_scan_active and result.coverage_complete and not reset_seen_overflow:
            for active_item in active_calendar_items:
                if str(active_item["source_key"]) in fetched_keys:
                    continue
                if str(active_item["id"]) in reset_seen_item_ids:
                    continue
                boundary = active_item.get("ends_at") or active_item.get("starts_at")
                if boundary and datetime.fromisoformat(
                    str(boundary).replace("Z", "+00:00")
                ) < datetime.fromisoformat(time_min.replace("Z", "+00:00")):
                    continue
                observation = _calendar_revalidation_observation(
                    active_item,
                    account_key=account_key,
                    source_order=next_generation,
                    observed_at=started.isoformat(),
                    checkpoint=result.next_sync_token or "calendar-reset",
                    default_timezone=policy.operator.timezone,
                )
                observations.append(observation)
                decisions.append(
                    ShadowDecision(
                        source_type="calendar",
                        account_key=account_key,
                        stream_key=CALENDAR_STREAM,
                        source_key=observation.source_key,
                        source_revision=observation.source_revision,
                        disposition="error",
                        reason_code="calendar_reset_revalidation",
                        item_ids=(operational_item_id(observation),),
                        evidence_refs=tuple(observation.evidence_refs),
                        confidence=0.0,
                        metadata={"subject": observation.title},
                    )
                )
                calendar_revalidation_ids.add(str(active_item["id"]))
        unresolved_revalidation_ids.update(calendar_revalidation_ids)
        durable_reset_seen_overflow = (
            reset_seen_overflow
            if reset_scan_active
            else prior_reset_seen_overflow
        )
        deferred_count = (
            len(unresolved_revalidation_ids)
            + int(durable_reset_seen_overflow)
            + int(not result.coverage_complete)
        )
        provider_cursor_advanced = bool(
            result.coverage_complete and result.next_sync_token
        )
        source_complete = result.coverage_complete and deferred_count == 0
        reset_scan_in_progress = reset_scan_active and not result.coverage_complete
        cursor_update = SourceCursorUpdate(
            connector_id=CALENDAR_CONNECTOR_ID,
            source_type="calendar",
            account_key=account_key,
            stream_key=CALENDAR_STREAM,
            cursor=(
                result.next_sync_token
                if provider_cursor_advanced
                else None
                if reset_scan_active or result.mode == "full"
                else str(current["cursor"])
                if current and current.get("cursor")
                else None
            ),
            metadata={
                "coverage_status": (
                    "complete" if source_complete else "partial"
                ),
                "deferred_count": deferred_count,
                "full_sync": result.mode == "full",
                "item_count": len(result.events),
                "page_count": result.pages_fetched,
                "continuation_page_token": result.continuation_page_token,
                "continuation_mode": (
                    result.mode if not result.coverage_complete else None
                ),
                "reset_rebuild": reset_scan_in_progress,
                "reset_started_generation": (
                    reset_started_generation
                    if reset_scan_in_progress
                    else None
                ),
                "reset_seen_item_ids": (
                    _encode_reset_seen_item_ids(reset_seen_item_ids)
                    if reset_scan_in_progress
                    else None
                ),
                "reset_seen_overflow": durable_reset_seen_overflow,
                "window_start": time_min,
                "window_end": time_max,
            },
            last_success_at=(started.isoformat() if source_complete else None),
            expected_cursor=(
                str(current["cursor"]) if current and current.get("cursor") else None
            ),
            expected_generation=(int(current["generation"]) if current else None),
        )
        atomic_result = self.operational_service.persist_shadow_source_unit(
            observations,
            cursor_update=cursor_update,
            decisions=decisions,
            processed_at=started.isoformat(),
            run_id=run_id,
        )
        source_result = atomic_result.source_unit
        item_ids = [item.item_id for item in source_result.reconciliations]
        coverage_status = "complete" if source_complete else "partial"
        coverage = {
            "status": coverage_status,
            "fresh_at": started.isoformat(),
            "mode": result.mode,
            "reset_required": result.reset_required,
            "item_count": len(result.events),
            "pages": result.pages_fetched,
            "deferred_count": deferred_count,
            "cursor_advanced": provider_cursor_advanced,
            "reset_tracking_overflow": durable_reset_seen_overflow,
        }
        usage = {
            "api_pages": result.pages_fetched,
            "events": len(result.events),
            "llm_calls": 0,
        }
        counts = {
            "calendar_events": len(result.events),
            "calendar_items": len(set(item_ids)),
            "calendar_cancelled": sum(
                _calendar_suppressed(event) for event in result.events
            ),
        }
        return coverage, usage, counts

    def _run_gmail(
        self,
        policy: OperationsPolicy,
        *,
        run_id: str,
        started: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
        account_key = policy.sources.gmail.account_key
        self.gmail_mirror.initialize()
        sync_outcome = None
        checkpoint = self.gmail_mirror.get_checkpoint(account_key, GMAIL_STREAM)
        if checkpoint is None:
            # The manual trial is allowed to bootstrap an empty mirror so the
            # first-run UX remains useful. Once a checkpoint exists, provider
            # polling belongs exclusively to the independent mirror scheduler.
            sync_outcome = GmailMirrorSynchronizer(
                self.paths,
                self.operational_service,
                store=self.gmail_mirror,
                reader=self._gmail_reader,
                now=self._now,
            ).sync(policy=policy, run_id=run_id)
            checkpoint = sync_outcome.checkpoint
        assert checkpoint is not None

        claimed = self.gmail_mirror.claim_pending_triage(
            account_key,
            limit=GMAIL_MANUAL_RUN_MAX_THREADS,
            claimed_at=started.isoformat(),
            detector_version=GMAIL_DETECTOR_VERSION,
            policy_version=policy.version_ref,
        )
        thread_items = tuple(item for item in claimed if not item.tombstoned)
        tombstone_items = tuple(item for item in claimed if item.tombstoned)
        threads = tuple(
            item.thread for item in thread_items if item.thread is not None
        )
        if len(threads) != len(thread_items):
            raise RuntimeError("Gmail mirror triage item is missing normalized content")

        # Keep the legacy evidence cache warm while local evidence routes move
        # to the durable mirror. This is a local copy operation, not a provider
        # read, and it preserves existing briefing evidence links during rollout.
        for item in thread_items:
            assert item.thread is not None
            source_ref = _gmail_source_ref(account_key, item.thread_id)
            revision = self.gmail_mirror.get_revision(
                account_key,
                item.thread_id,
                item.source_revision,
            )
            if revision is not None and revision.raw_payload is not None:
                self.cache.write_raw(
                    GMAIL_CONNECTOR_ID,
                    source_ref,
                    revision.raw_payload,
                    cached_at=started,
                )
            self.cache.write_normalized(
                GMAIL_CONNECTOR_ID,
                source_ref,
                item.thread.as_dict(),
                cached_at=started,
                source_revision=item.source_revision,
            )

        current = get_source_cursor(
            self.paths.ops_sqlite_path,
            GMAIL_CONNECTOR_ID,
            account_key,
            GMAIL_STREAM,
        )
        decided = _decided_gmail_revisions(
            self.paths.ops_sqlite_path,
            account_key=account_key,
            detector_version=GMAIL_DETECTOR_VERSION,
            policy_version=policy.version_ref,
        )
        cached_items = tuple(
            item
            for item in thread_items
            if item.last_error in {None, "triage lease expired"}
            and (item.thread_id, item.source_revision) in decided
        )
        cached_keys = {
            (item.thread_id, item.source_revision) for item in cached_items
        }
        pending_items = tuple(
            item
            for item in thread_items
            if (item.thread_id, item.source_revision) not in cached_keys
        )
        pending_threads = tuple(
            item.thread for item in pending_items if item.thread is not None
        )
        cached_threads = tuple(
            item.thread for item in cached_items if item.thread is not None
        )
        cached_thread_count = len(cached_items)
        active_items = _active_gmail_items_by_thread(
            self.paths.ops_sqlite_path,
            account_key=account_key,
        )
        retained_provider_observations = _retained_gmail_provider_observations(
            self.paths.ops_sqlite_path,
            account_key=account_key,
            threads=cached_threads,
            active_items=active_items,
        )
        budgeted_provider: _DurablyBudgetedDetectorProvider | None = None
        if pending_threads:
            budgeted_provider = _DurablyBudgetedDetectorProvider(
                self._llm_provider
                or get_gmail_provider(self.paths, run_id=run_id),
                operational_service=self.operational_service,
                policy=policy,
                run_id=run_id,
                started=started,
            )
            detector = detect_gmail_threads(
                pending_threads,
                operator_emails=(policy.operator.gmail.email,),
                timezone_name=policy.operator.timezone,
                policy_version=policy.version_ref,
                budget=budget_from_operations_policy(policy),
                llm_provider=budgeted_provider,
                active_items_by_thread=active_items,
                responsibility_context={
                    "owned": list(policy.responsibility.owned),
                    "shared": list(policy.responsibility.shared),
                    "adjacent": list(policy.responsibility.adjacent),
                    "out_of_area_action": policy.responsibility.out_of_area_action,
                    "unknown_action": policy.responsibility.unknown_action,
                    "direct_obligations_remain_eligible": (
                        policy.responsibility.direct_obligations_remain_eligible
                    ),
                    "high_consequence_categories": list(
                        policy.responsibility.high_consequence_categories
                    ),
                    "high_consequence_remains_eligible": (
                        policy.responsibility.high_consequence_remains_eligible
                    ),
                    "high_consequence_never_auto_suppress": (
                        policy.responsibility.high_consequence_never_auto_suppress
                    ),
                },
            )
        else:
            detector = GmailDetectionBatchResult(
                detections=(),
                requests=0,
                estimated_input_tokens=0,
                estimated_output_tokens=0,
                deferred_count=0,
                model_thread_count=0,
                deterministic_suppressed_count=0,
                coverage_complete=True,
            )
        detector_complete = detector.coverage_complete
        observations: list[OperationalObservation] = []
        observation_bindings: list[
            tuple[NormalizedGmailThread, GmailThreadDetection, Any]
        ] = []
        thread_by_id = {thread.thread_id: thread for thread in pending_threads}
        triage_by_thread = {item.thread_id: item for item in pending_items}
        for detection in detector.detections:
            thread = thread_by_id[detection.thread_id]
            for candidate in detection.candidates:
                observation = _gmail_observation(
                    thread,
                    detection,
                    candidate,
                    account_key=account_key,
                    source_order=triage_by_thread[thread.thread_id].mirror_sequence,
                    observed_at=started.isoformat(),
                    source_ref=_gmail_source_ref(account_key, thread.thread_id),
                    source_timezone=policy.operator.timezone,
                    detector_version=GMAIL_DETECTOR_VERSION,
                    policy_version=policy.version_ref,
                )
                observations.append(observation)
                observation_bindings.append((thread, detection, candidate))
        observations.extend(retained_provider_observations)
        revalidation_reasons: dict[str, str] = {
            item.thread_id: "gmail_thread_missing" for item in tombstone_items
        }
        revalidation_bindings: list[
            tuple[str, Mapping[str, Any], OperationalObservation]
        ] = []
        tombstone_by_thread = {item.thread_id: item for item in tombstone_items}
        for thread_id, reason_code in sorted(revalidation_reasons.items()):
            tombstone = tombstone_by_thread[thread_id]
            for active_item in active_items.get(thread_id, ()):
                observation = _gmail_revalidation_observation(
                    active_item,
                    account_key=account_key,
                    thread_id=thread_id,
                    source_order=tombstone.mirror_sequence,
                    observed_at=started.isoformat(),
                    source_timezone=policy.operator.timezone,
                    checkpoint=tombstone.source_revision,
                    reason_code=reason_code,
                )
                observations.append(observation)
                revalidation_bindings.append((thread_id, active_item, observation))
        existing_unresolved_revalidation_ids = {
            str(item["id"])
            for thread_items in active_items.values()
            for item in thread_items
            if str(item.get("current_source_revision") or "").startswith(
                "gmail-revalidation-"
            )
            and not _human_confirmed_current_observation(item)
            and str(item["id"])
            not in {operational_item_id(value) for value in observations}
        }
        unresolved_revalidation_ids = existing_unresolved_revalidation_ids | {
            str(active_item["id"])
            for _thread_id, active_item, _observation in revalidation_bindings
        }

        detection_by_thread = {
            detection.thread_id: detection for detection in detector.detections
        }
        items_to_defer: dict[tuple[str, str], str] = {}
        for item in pending_items:
            detection = detection_by_thread.get(item.thread_id)
            if detection is None:
                items_to_defer[(item.thread_id, item.source_revision)] = (
                    "detector produced no result for the mirrored thread"
                )
            elif detection.disposition in {"deferred", "error"} or detection.error:
                items_to_defer[(item.thread_id, item.source_revision)] = str(
                    detection.error or detection.reason_code
                )[:4000]
        claimed_backlog = _gmail_triage_backlog(
            self.gmail_mirror,
            account_key=account_key,
        )
        triage_pending_count = max(0, claimed_backlog - len(claimed)) + len(
            items_to_defer
        )
        triage_status = (
            "complete"
            if triage_pending_count == 0
            and detector_complete
            and not unresolved_revalidation_ids
            else "partial"
        )
        source_complete = (
            checkpoint.coverage_complete
            and triage_status == "complete"
            and not unresolved_revalidation_ids
        )
        deferred_count = (
            triage_pending_count
            + len(unresolved_revalidation_ids)
            + int(bool(revalidation_reasons) and not unresolved_revalidation_ids)
            + int(not checkpoint.coverage_complete and triage_pending_count == 0)
        )
        provider_cursor_advanced = _gmail_checkpoint_differs_from_projection(
            current,
            checkpoint,
        )
        cursor_update = SourceCursorUpdate(
            connector_id=GMAIL_CONNECTOR_ID,
            source_type="gmail",
            account_key=account_key,
            stream_key=GMAIL_STREAM,
            cursor=checkpoint.history_id,
            metadata={
                "coverage_status": "complete" if source_complete else "partial",
                "deferred_count": deferred_count,
                "full_sync": checkpoint.mode == "full",
                "item_count": len(claimed),
                "page_count": (
                    sync_outcome.fetch.pages_fetched if sync_outcome is not None else 0
                ),
                "continuation_page_token": checkpoint.continuation_page_token,
                "baseline_history_id": checkpoint.baseline_history_id,
                "pending_thread_ids": (
                    _encode_pending_thread_ids(checkpoint.pending_thread_ids)
                    if checkpoint.pending_thread_ids
                    else None
                ),
                "continuation_history_id": checkpoint.continuation_history_id,
                "continuation_mode": (
                    checkpoint.mode if not checkpoint.coverage_complete else None
                ),
                "reset_rebuild": bool(
                    checkpoint.reset_required and not checkpoint.coverage_complete
                ),
                "reset_started_generation": None,
                "reset_seen_item_ids": None,
                "reset_seen_overflow": False,
            },
            # Mailbox freshness is owned by the mirror, never by Luna triage.
            last_success_at=checkpoint.last_success_at,
            expected_cursor=(
                str(current["cursor"]) if current and current.get("cursor") else None
            ),
            expected_generation=(int(current["generation"]) if current else None),
        )
        item_ids_by_thread: dict[str, list[str]] = {}
        assessments: list[HandledAssessment] = []
        for binding, observation in zip(observation_bindings, observations):
            thread, _detection, candidate = binding
            item_id = operational_item_id(observation)
            item_ids_by_thread.setdefault(thread.thread_id, []).append(item_id)
            supporting = tuple(
                {
                    "account_key": account_key,
                    "thread_id": thread.thread_id,
                    "message_id": message_id,
                    "source_ref": _gmail_source_ref(account_key, thread.thread_id),
                    "source_revision": thread.source_revision or _gmail_revision(thread),
                }
                for message_id in candidate.evidence_message_ids
            )
            assessments.append(
                HandledAssessment(
                    item_id=item_id,
                    verdict=candidate.handled_verdict,
                    source_revision=observation.source_revision,
                    supporting_evidence=supporting,
                    sources_checked=("gmail",),
                    coverage={
                        "gmail": {
                            "status": (
                                "complete"
                                if checkpoint.coverage_complete
                                else "partial"
                            )
                        }
                    },
                    method_version=GMAIL_DETECTOR_VERSION,
                    policy_version=policy.version_ref,
                    confidence=candidate.handled_confidence,
                    as_of=started.isoformat(),
                )
            )
        for _thread_id, _active_item, observation in revalidation_bindings:
            item_id = operational_item_id(observation)
            assessments.append(
                HandledAssessment(
                    item_id=item_id,
                    verdict="unknown",
                    source_revision=observation.source_revision,
                    supporting_evidence=tuple(observation.evidence_refs),
                    sources_checked=("gmail",),
                    coverage={"gmail": {"status": "partial"}},
                    method_version=GMAIL_DETECTOR_VERSION,
                    policy_version=policy.version_ref,
                    confidence=0.0,
                    as_of=started.isoformat(),
                )
            )
        decisions: list[ShadowDecision] = []
        for detection in detector.detections:
            thread = thread_by_id[detection.thread_id]
            evidence_refs = tuple(
                {
                    "account_key": account_key,
                    "thread_id": thread.thread_id,
                    "message_id": message.message_id,
                    "source_ref": _gmail_source_ref(account_key, thread.thread_id),
                    "source_revision": thread.source_revision or _gmail_revision(thread),
                }
                for message in thread.messages[-8:]
            )
            decisions.append(
                ShadowDecision(
                    source_type="gmail",
                    account_key=account_key,
                    stream_key=GMAIL_STREAM,
                    source_key=thread.thread_id,
                    source_revision=thread.source_revision or _gmail_revision(thread),
                    disposition=detection.disposition,
                    reason_code=detection.reason_code,
                    item_ids=tuple(item_ids_by_thread.get(thread.thread_id, ())),
                    evidence_refs=evidence_refs,
                    confidence=detection.confidence,
                    metadata={
                        "detector_version": GMAIL_DETECTOR_VERSION,
                        "message_class": thread.message_class,
                        "policy_version": policy.version_ref,
                        "subject": thread.subject or "Email thread",
                    },
                )
            )
        for thread_id, reason_code in sorted(revalidation_reasons.items()):
            tombstone = tombstone_by_thread[thread_id]
            matching = [
                observation
                for candidate_thread_id, _active_item, observation in revalidation_bindings
                if candidate_thread_id == thread_id
            ]
            source_ref = _gmail_source_ref(account_key, thread_id)
            decisions.append(
                ShadowDecision(
                    source_type="gmail",
                    account_key=account_key,
                    stream_key=GMAIL_STREAM,
                    source_key=thread_id,
                    source_revision=(
                        matching[0].source_revision
                        if matching
                        else tombstone.source_revision
                    ),
                    disposition="error",
                    reason_code=reason_code,
                    item_ids=tuple(operational_item_id(item) for item in matching),
                    evidence_refs=tuple(
                        item.evidence_refs[0]
                        for item in matching
                        if item.evidence_refs
                    )
                    or (
                        {
                            "account_key": account_key,
                            "thread_id": thread_id,
                            "source_ref": source_ref,
                            "source_revision": tombstone.source_revision,
                        },
                    ),
                    confidence=0.0,
                    metadata={"subject": "Gmail thread requires revalidation"},
                )
            )
        atomic_result = self.operational_service.persist_shadow_source_unit(
            observations,
            cursor_update=cursor_update,
            decisions=decisions,
            handled_assessments=assessments,
            processed_at=started.isoformat(),
            run_id=run_id,
        )
        reconciliations = list(atomic_result.source_unit.reconciliations)

        # Only acknowledge a queue revision after its operational decision (or
        # tombstone revalidation) has committed. Provider/model failures remain
        # durable deferred work; they never rewind the mailbox checkpoint.
        retry_at = (started + timedelta(minutes=10)).isoformat()
        for item in claimed:
            key = (item.thread_id, item.source_revision)
            try:
                if key in items_to_defer:
                    self.gmail_mirror.finish_triage(
                        account_key,
                        item.thread_id,
                        item.source_revision,
                        expected_generation=item.generation,
                        state="deferred",
                        updated_at=started.isoformat(),
                        available_at=retry_at,
                        error=items_to_defer[key],
                    )
                else:
                    self.gmail_mirror.finish_triage(
                        account_key,
                        item.thread_id,
                        item.source_revision,
                        expected_generation=item.generation,
                        state="completed",
                        updated_at=started.isoformat(),
                        detector_version=GMAIL_DETECTOR_VERSION,
                        policy_version=policy.version_ref,
                    )
            except GmailMirrorGenerationConflict:
                # A concurrent sync superseded this immutable revision. Its new
                # current revision already owns another queue row.
                continue

        triage_pending_count = _gmail_triage_backlog(
            self.gmail_mirror,
            account_key=account_key,
        )
        triage_status = (
            "complete"
            if triage_pending_count == 0
            and detector_complete
            and not unresolved_revalidation_ids
            else "partial"
        )
        source_complete = (
            checkpoint.coverage_complete
            and triage_status == "complete"
            and not unresolved_revalidation_ids
        )
        status = "complete" if source_complete else "partial"
        mailbox_status = _gmail_mailbox_status(checkpoint)
        coverage = {
            "status": status,
            "fresh_at": checkpoint.last_success_at or checkpoint.updated_at,
            "mailbox_status": mailbox_status,
            "mailbox_last_success_at": checkpoint.last_success_at,
            "triage_status": triage_status,
            "triage_pending_count": triage_pending_count,
            "mode": checkpoint.mode,
            "reset_required": checkpoint.reset_required,
            "thread_count": len(thread_items),
            "changed_thread_count": (
                len(sync_outcome.fetch.changed_thread_ids)
                if sync_outcome is not None
                else 0
            ),
            "cached_decision_count": cached_thread_count,
            "missing_thread_count": len(tombstone_items),
            "pages": (
                sync_outcome.fetch.pages_fetched if sync_outcome is not None else 0
            ),
            "deferred_count": deferred_count,
            "cursor_advanced": provider_cursor_advanced,
            "reset_tracking_overflow": False,
        }
        usage = {
            "api_pages": (
                sync_outcome.fetch.pages_fetched if sync_outcome is not None else 0
            ),
            "api_thread_gets": (
                len(sync_outcome.fetch.changed_thread_ids)
                if sync_outcome is not None
                else 0
            ),
            **detector.as_dict()["usage"],
            **(
                budgeted_provider.usage_stats()
                if budgeted_provider is not None
                else _empty_provider_usage_stats()
            ),
        }
        dispositions = Counter(item.disposition for item in detector.detections)
        counts = {
            "gmail_threads": len(thread_items),
            "gmail_pending_threads": len(pending_threads),
            "gmail_cached_decisions": cached_thread_count,
            "gmail_surfaced": dispositions["surfaced"],
            "gmail_suppressed": dispositions["suppressed"],
            "gmail_deferred": dispositions["deferred"],
            "gmail_errors": dispositions["error"],
            "gmail_candidates": len(observation_bindings),
            "gmail_authority_restored": len(retained_provider_observations),
            "gmail_items": len({item.item_id for item in reconciliations}),
            "gmail_triage_pending": triage_pending_count,
        }
        return coverage, usage, counts


def _gmail_triage_backlog(
    store: GmailMirrorStore,
    *,
    account_key: str,
) -> int:
    """Count all current triage backlog without truncating or hiding leases."""

    return int(store.triage_counts(account_key)["backlog_count"])


def _gmail_checkpoint_differs_from_projection(
    current: Mapping[str, Any] | None,
    checkpoint: GmailMirrorCheckpoint,
) -> bool:
    if current is None:
        return True
    metadata = dict(current.get("metadata") or {})
    projected_pending = (
        _encode_pending_thread_ids(checkpoint.pending_thread_ids)
        if checkpoint.pending_thread_ids
        else None
    )
    return any(
        (
            _optional_cursor_text(current.get("cursor")) != checkpoint.history_id,
            _optional_cursor_text(metadata.get("continuation_page_token"))
            != checkpoint.continuation_page_token,
            _optional_cursor_text(metadata.get("baseline_history_id"))
            != checkpoint.baseline_history_id,
            _optional_cursor_text(metadata.get("pending_thread_ids"))
            != projected_pending,
            _optional_cursor_text(metadata.get("continuation_history_id"))
            != checkpoint.continuation_history_id,
            _continuation_mode(metadata)
            != (checkpoint.mode if not checkpoint.coverage_complete else None),
        )
    )


def _gmail_mailbox_status(checkpoint: GmailMirrorCheckpoint) -> str:
    if checkpoint.coverage_complete:
        return "complete"
    if checkpoint.reset_required:
        return "resyncing"
    return "partial"


def _requested_sources(
    policy: OperationsPolicy,
    sources: Sequence[str] | None,
) -> list[str]:
    enabled = {
        CALENDAR_CONNECTOR_ID: policy.sources.calendar.enabled,
        GMAIL_CONNECTOR_ID: policy.sources.gmail.enabled,
    }
    requested = list(sources) if sources is not None else [
        source for source, is_enabled in enabled.items() if is_enabled
    ]
    output: list[str] = []
    for source in requested:
        normalized = str(source).strip()
        if normalized not in enabled:
            raise ValueError(f"unsupported shadow source: {normalized}")
        if not enabled[normalized]:
            raise ValueError(f"shadow source is disabled by policy: {normalized}")
        if normalized not in output:
            output.append(normalized)
    return output


def _enabled_sources(policy: OperationsPolicy) -> tuple[str, ...]:
    return tuple(
        source
        for source, enabled in (
            (CALENDAR_CONNECTOR_ID, policy.sources.calendar.enabled),
            (GMAIL_CONNECTOR_ID, policy.sources.gmail.enabled),
        )
        if enabled
    )


def _calendar_observation(
    event: NormalizedCalendarEvent,
    *,
    account_key: str,
    source_order: int,
    source_ref: str,
    checkpoint: str,
    observed_at: str,
    default_timezone: str,
) -> OperationalObservation:
    source_timezone = event.source_timezone or default_timezone
    starts_at, ends_at, all_day = _calendar_times(
        event,
        timezone_name=source_timezone,
    )
    metadata = {
        "all_day": all_day,
        "attendee_count": event.attendee_count,
        "attendee_response": event.attendee_response,
        "calendar_id": "primary",
        "event_type": event.event_type,
        "ical_uid": getattr(event, "ical_uid", None),
        "location": event.location,
        "organizer_self": event.organizer_self,
        "original_start_time": event.original_start_time or event.original_start_date,
        "provider_sequence": event.sequence,
        "reconciliation_status": "confirmed",
        "recurring_event_id": event.recurring_event_id,
        "source_status": event.status,
        "transparency": event.transparency,
        "visibility": event.visibility,
    }
    return OperationalObservation(
        source_type="calendar",
        account_key=account_key,
        stream_key=CALENDAR_STREAM,
        source_key=_calendar_source_key(event),
        source_revision=event.source_revision or _calendar_revision(event, checkpoint),
        source_order=source_order,
        source_updated_at=event.updated_at,
        observed_at=observed_at,
        item_kind="event",
        title=event.title,
        details=(f"Location: {event.location}" if event.location else None),
        owner="operator",
        starts_at=starts_at,
        ends_at=ends_at,
        source_timezone=source_timezone,
        confidence=1.0,
        priority=0,
        # A self-declined meeting is operationally terminal even though Google
        # leaves its provider status as "confirmed".
        cancelled=_calendar_suppressed(event),
        evidence_refs=(
            {
                "account_key": account_key,
                "calendar_id": "primary",
                "event_id": event.event_id,
                "source_ref": source_ref,
                "source_revision": event.source_revision
                or _calendar_revision(event, checkpoint),
            },
        ),
        metadata={key: value for key, value in metadata.items() if value is not None},
    )


def _calendar_revalidation_observation(
    active_item: Mapping[str, Any],
    *,
    account_key: str,
    source_order: int,
    observed_at: str,
    checkpoint: str,
    default_timezone: str,
) -> OperationalObservation:
    source_key = str(active_item["source_key"])
    payload = json.dumps(
        [source_key, checkpoint, "calendar_reset_revalidation"],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    revision = "calendar-revalidation-" + hashlib.sha256(
        payload.encode("utf-8")
    ).hexdigest()
    references = []
    for value in active_item.get("evidence_refs") or ():
        if isinstance(value, Mapping):
            references.append(dict(value))
    if not references:
        references.append(
            {
                "account_key": account_key,
                "calendar_id": "primary",
                "source_ref": f"{account_key}:primary:{source_key}",
                "source_revision": active_item.get("current_source_revision"),
            }
        )
    metadata = {
        key: value
        for key, value in dict(active_item.get("metadata") or {}).items()
        if key != "schema_version"
    }
    metadata["reconciliation_status"] = "ambiguous"
    metadata["revalidation_reason"] = "calendar_reset_revalidation"
    details = str(active_item.get("details") or "").strip()
    warning = (
        "The Calendar sync token reset and this event was not present in the "
        "bounded rebuild window; verify it before relying on its status."
    )
    return OperationalObservation(
        source_type="calendar",
        account_key=account_key,
        stream_key=CALENDAR_STREAM,
        source_key=source_key,
        source_revision=revision,
        source_order=source_order,
        source_updated_at=None,
        observed_at=observed_at,
        item_kind="event",
        title=str(active_item["title"]),
        details=f"{details}\n\n{warning}".strip()[:4000],
        owner=str(active_item["owner"]),
        counterparty_entity_id=active_item.get("counterparty_entity_id"),
        project_ref=active_item.get("project_ref"),
        starts_at=active_item.get("starts_at"),
        due_at=active_item.get("due_at"),
        ends_at=active_item.get("ends_at"),
        source_timezone=active_item.get("source_timezone") or default_timezone,
        expires_at=active_item.get("expires_at"),
        confidence=0.1,
        priority=int(active_item.get("priority") or 0),
        cancelled=False,
        evidence_refs=tuple(references),
        metadata=metadata,
    )


def _gmail_observation(
    thread: NormalizedGmailThread,
    detection: GmailThreadDetection,
    candidate: Any,
    *,
    account_key: str,
    source_order: int,
    observed_at: str,
    source_ref: str,
    source_timezone: str,
    detector_version: str,
    policy_version: str,
) -> OperationalObservation:
    provider_revision = thread.source_revision or _gmail_revision(thread)
    revision = _gmail_derived_observation_revision(
        provider_revision=provider_revision,
        detector_version=detector_version,
        policy_version=policy_version,
    )
    evidence_refs = tuple(
        {
            "account_key": account_key,
            "thread_id": thread.thread_id,
            "message_id": message_id,
            "source_ref": source_ref,
            "source_revision": provider_revision,
        }
        for message_id in candidate.evidence_message_ids
    )
    return OperationalObservation(
        source_type="gmail",
        account_key=account_key,
        stream_key=GMAIL_STREAM,
        source_key=f"{thread.thread_id}:{candidate.detector_key}",
        source_revision=revision,
        source_order=source_order,
        source_updated_at=thread.updated_at,
        observed_at=observed_at,
        item_kind=candidate.kind,
        title=candidate.title,
        details=candidate.reason,
        owner=candidate.owner,
        starts_at=candidate.starts_at,
        due_at=candidate.due_at,
        ends_at=candidate.ends_at,
        source_timezone=source_timezone,
        expires_at=candidate.expires_at,
        confidence=candidate.confidence,
        priority=candidate.priority,
        # Model output can suggest reconciliation, but only deterministic
        # provider lifecycle evidence may close an operational item.
        cancelled=False,
        evidence_refs=evidence_refs,
        metadata={
            "detector_version": detector_version,
            "message_class": thread.message_class,
            "policy_version": policy_version,
            "reconciliation_status": candidate.reconciliation_status,
        },
    )


def _gmail_derived_observation_revision(
    *,
    provider_revision: str,
    detector_version: str,
    policy_version: str,
) -> str:
    """Version an immutable detector interpretation separately from Gmail evidence."""

    payload = json.dumps(
        [provider_revision, detector_version, policy_version],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "gmail-derived-v1-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _gmail_revalidation_observation(
    active_item: Mapping[str, Any],
    *,
    account_key: str,
    thread_id: str,
    source_order: int,
    observed_at: str,
    source_timezone: str,
    checkpoint: str,
    reason_code: str,
) -> OperationalObservation:
    detector_key = str(active_item.get("detector_key") or "").strip()
    if not detector_key:
        raise ValueError("active Gmail item is missing its detector key")
    revision_payload = json.dumps(
        [thread_id, detector_key, checkpoint, reason_code],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    revision = "gmail-revalidation-" + hashlib.sha256(
        revision_payload.encode("utf-8")
    ).hexdigest()
    source_ref = _gmail_source_ref(account_key, thread_id)
    prior_details = str(active_item.get("details") or "").strip()
    revalidation_detail = (
        "The source thread could not be revalidated; the existing item remains "
        "visible with unknown handled state."
    )
    details = f"{prior_details}\n\n{revalidation_detail}".strip()[:4000]
    references = [
        dict(value)
        for value in active_item.get("evidence_refs") or ()
        if isinstance(value, Mapping)
    ]
    if not references:
        references.append(
            {
                "account_key": account_key,
                "thread_id": thread_id,
                "source_ref": source_ref,
                "source_revision": active_item.get("current_source_revision"),
            }
        )
    return OperationalObservation(
        source_type="gmail",
        account_key=account_key,
        stream_key=GMAIL_STREAM,
        source_key=f"{thread_id}:{detector_key}",
        source_revision=revision,
        source_order=source_order,
        source_updated_at=None,
        observed_at=observed_at,
        item_kind=str(active_item["kind"]),
        title=str(active_item["title"]),
        details=details,
        owner=str(active_item["owner"]),
        counterparty_entity_id=active_item.get("counterparty_entity_id"),
        project_ref=active_item.get("project_ref"),
        starts_at=active_item.get("starts_at"),
        due_at=active_item.get("due_at"),
        ends_at=active_item.get("ends_at"),
        source_timezone=source_timezone,
        expires_at=active_item.get("expires_at"),
        confidence=0.1,
        priority=int(active_item.get("priority") or 0),
        cancelled=False,
        evidence_refs=tuple(references),
        metadata={
            "detector_version": GMAIL_DETECTOR_VERSION,
            "message_class": "unknown",
            "reconciliation_status": "ambiguous",
            "revalidation_reason": reason_code,
        },
    )


def _calendar_source_key(event: NormalizedCalendarEvent) -> str:
    return calendar_occurrence_key(event)


def _calendar_source_ref(account_key: str, event_id: str) -> str:
    return f"{account_key}:primary:{event_id}"


def _gmail_source_ref(account_key: str, thread_id: str) -> str:
    return f"{account_key}:{thread_id}"


def _calendar_revision(event: NormalizedCalendarEvent, checkpoint: str) -> str:
    payload = json.dumps(
        [checkpoint, event.event_id, event.status, event.updated_at],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "calendar-tombstone-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _gmail_revision(thread: NormalizedGmailThread) -> str:
    payload = json.dumps(thread.as_dict(), sort_keys=True, separators=(",", ":"))
    return "gmail-normalized-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _calendar_times(
    event: NormalizedCalendarEvent,
    *,
    timezone_name: str,
) -> tuple[str | None, str | None, bool]:
    if event.starts_at:
        return (
            _canonical_timestamp(event.starts_at),
            _canonical_timestamp(event.ends_at) if event.ends_at else None,
            False,
        )
    if not event.start_date:
        return None, None, False
    zone = ZoneInfo(timezone_name)
    start = datetime.fromisoformat(event.start_date).replace(tzinfo=zone)
    end = (
        datetime.fromisoformat(event.end_date).replace(tzinfo=zone)
        if event.end_date
        else start + timedelta(days=1)
    )
    return (
        start.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
        end.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
        True,
    )


def _calendar_suppressed(event: NormalizedCalendarEvent) -> bool:
    return calendar_event_is_inactive(event)


def _active_calendar_items(
    db_path: Path,
    *,
    account_key: str,
) -> list[dict[str, Any]]:
    with operational_connection(db_path, write=False) as conn:
        rows = conn.execute(
            """
            SELECT i.*, o.evidence_refs, o.observed_at AS current_observed_at,
                   o.source_order AS current_source_order,
                   o.source_revision AS current_source_revision
            FROM ops_items i
            JOIN ops_observations o ON o.id = i.current_observation_id
            WHERE i.source_type = 'calendar' AND i.account_key = ?
              AND i.state = 'active'
            ORDER BY i.updated_at DESC, i.id
            """,
            (account_key,),
        ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["metadata"] = json.loads(str(item["metadata"]))
        item["evidence_refs"] = json.loads(str(item["evidence_refs"]))
        output.append(item)
    return output


def _decided_gmail_revisions(
    db_path: Path,
    *,
    account_key: str,
    detector_version: str,
    policy_version: str,
) -> set[tuple[str, str]]:
    with operational_connection(db_path, write=False) as conn:
        rows = conn.execute(
            """
            SELECT d.source_key, d.source_revision
            FROM ops_shadow_decisions d
            JOIN ops_shadow_runs r ON r.id = d.run_id
            WHERE d.source_type = 'gmail' AND d.account_key = ?
              AND d.disposition IN ('surfaced', 'suppressed')
              AND d.source_revision IS NOT NULL
              AND r.detector_version = ?
              AND r.policy_version = ?
            """,
            (account_key, detector_version, policy_version),
        ).fetchall()
    return {(str(row["source_key"]), str(row["source_revision"])) for row in rows}


def _retained_gmail_provider_observations(
    db_path: Path,
    *,
    account_key: str,
    threads: Sequence[NormalizedGmailThread],
    active_items: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[OperationalObservation, ...]:
    """Replay immutable provider evidence hidden by synthetic revalidation.

    A decided Gmail revision normally skips the detector. If reset recovery has
    since made its item synthetic/ambiguous, seeing that same thread revision is
    affirmative presence evidence. Reusing the retained observation restores
    provider authority without spending another model call.
    """

    revisions = {
        thread.thread_id: thread.source_revision or _gmail_revision(thread)
        for thread in threads
    }
    rows: list[Any] = []
    with operational_connection(db_path, write=False) as conn:
        for thread_id, items in active_items.items():
            revision = revisions.get(thread_id)
            if not revision:
                continue
            for item in items:
                current_revision = str(item.get("current_source_revision") or "")
                if not current_revision.startswith("gmail-revalidation-"):
                    continue
                cited_revisions = {
                    str(reference.get("source_revision") or "")
                    for reference in item.get("evidence_refs") or ()
                    if isinstance(reference, Mapping)
                }
                if revision not in cited_revisions:
                    continue
                candidate_rows = conn.execute(
                    """
                    SELECT * FROM ops_observations
                    WHERE source_type = 'gmail' AND account_key = ?
                      AND stream_key = ? AND source_key = ?
                    ORDER BY source_order DESC, created_at DESC, id DESC
                    """,
                    (
                        account_key,
                        GMAIL_STREAM,
                        f"{thread_id}:{item['detector_key']}",
                    ),
                ).fetchall()
                row = next(
                    (
                        candidate_row
                        for candidate_row in candidate_rows
                        if not str(candidate_row["source_revision"]).startswith(
                            "gmail-revalidation-"
                        )
                        and (
                            str(candidate_row["source_revision"]) == revision
                            or revision
                            in {
                                str(reference.get("source_revision") or "")
                                for reference in json.loads(
                                    str(candidate_row["evidence_refs"])
                                )
                                if isinstance(reference, Mapping)
                            }
                        )
                    ),
                    None,
                )
                if row is None:
                    raise RuntimeError(
                        "Gmail revalidation cites a missing provider observation"
                    )
                rows.append(row)
    observations: list[OperationalObservation] = []
    for row in rows:
        payload = json.loads(str(row["payload"]))
        metadata = dict(payload.get("metadata") or {})
        metadata.pop("schema_version", None)
        observations.append(
            OperationalObservation(
                source_type="gmail",
                account_key=account_key,
                stream_key=GMAIL_STREAM,
                source_key=str(row["source_key"]),
                source_revision=str(row["source_revision"]),
                source_order=int(row["source_order"]),
                source_updated_at=row["source_updated_at"],
                observed_at=str(row["observed_at"]),
                item_kind=str(payload["item_kind"]),
                title=str(payload["title"]),
                details=payload.get("details"),
                owner=str(payload["owner"]),
                counterparty_entity_id=payload.get("counterparty_entity_id"),
                project_ref=payload.get("project_ref"),
                starts_at=payload.get("starts_at"),
                due_at=payload.get("due_at"),
                ends_at=payload.get("ends_at"),
                source_timezone=payload.get("source_timezone"),
                expires_at=payload.get("expires_at"),
                confidence=float(payload["confidence"]),
                priority=int(payload["priority"]),
                cancelled=bool(payload["cancelled"]),
                evidence_refs=tuple(json.loads(str(row["evidence_refs"]))),
                metadata=metadata,
            )
        )
    return tuple(observations)


def _active_gmail_items_by_thread(
    db_path: Path,
    *,
    account_key: str,
) -> dict[str, list[dict[str, Any]]]:
    with operational_connection(db_path, write=False) as conn:
        rows = conn.execute(
            """
            SELECT i.id, i.source_key, i.item_kind, i.state, i.title,
                   i.details, i.owner, i.counterparty_entity_id, i.project_ref,
                   i.starts_at, i.due_at, i.ends_at, i.expires_at, i.priority,
                   i.human_confirmed_at,
                   o.source_order AS current_source_order,
                   o.source_revision AS current_source_revision,
                   o.observed_at AS current_observed_at,
                   o.evidence_refs AS current_evidence_refs
            FROM ops_items i
            JOIN ops_observations o ON o.id = i.current_observation_id
            WHERE i.source_type = 'gmail' AND i.account_key = ?
              AND i.state = 'active'
            ORDER BY i.updated_at DESC
            """,
            (account_key,),
        ).fetchall()
    output: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        source_key = str(row["source_key"])
        thread_id, separator, detector_key = source_key.partition(":")
        if not separator:
            continue
        output.setdefault(thread_id, []).append(
            {
                "id": row["id"],
                "source_key": row["source_key"],
                "detector_key": detector_key,
                "kind": row["item_kind"],
                "state": row["state"],
                "title": row["title"],
                "details": row["details"],
                "owner": row["owner"],
                "counterparty_entity_id": row["counterparty_entity_id"],
                "project_ref": row["project_ref"],
                "starts_at": row["starts_at"],
                "due_at": row["due_at"],
                "ends_at": row["ends_at"],
                "expires_at": row["expires_at"],
                "priority": row["priority"],
                "human_confirmed_at": row["human_confirmed_at"],
                "current_source_order": row["current_source_order"],
                "current_source_revision": row["current_source_revision"],
                "current_observed_at": row["current_observed_at"],
                "evidence_refs": json.loads(str(row["current_evidence_refs"])),
            }
        )
    return output


def _human_confirmed_current_observation(item: Mapping[str, Any]) -> bool:
    confirmed_at = item.get("human_confirmed_at")
    observed_at = item.get("current_observed_at")
    if not confirmed_at or not observed_at:
        return False
    return datetime.fromisoformat(str(confirmed_at).replace("Z", "+00:00")) > (
        datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
    )


def _canonical_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("source timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("shadow trial clock must include a timezone")
    return value.astimezone(timezone.utc)


def _optional_cursor_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _continuation_mode(metadata: Mapping[str, Any]) -> str | None:
    value = _optional_cursor_text(metadata.get("continuation_mode"))
    if value is not None and value not in {"full", "incremental"}:
        raise ValueError("source cursor continuation_mode is invalid")
    return value


def _optional_cursor_generation(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("source cursor reset_started_generation is invalid")
    return value


def _reset_seen_item_ids(
    metadata: Mapping[str, Any],
) -> tuple[set[str], bool]:
    encoded = _optional_cursor_text(metadata.get("reset_seen_item_ids"))
    if encoded is None:
        values: Any = []
    else:
        try:
            values = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise ValueError("source cursor reset_seen_item_ids is invalid") from exc
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value or len(value) > 256
        for value in values
    ):
        raise ValueError("source cursor reset_seen_item_ids is invalid")
    if len(values) != len(set(values)):
        raise ValueError("source cursor reset_seen_item_ids contains duplicates")
    overflow = metadata.get("reset_seen_overflow", False)
    if not isinstance(overflow, bool):
        raise ValueError("source cursor reset_seen_overflow is invalid")
    return set(values), overflow


def _pending_thread_ids(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    encoded = _optional_cursor_text(metadata.get("pending_thread_ids"))
    if encoded is None:
        return ()
    if len(encoded.encode("utf-8")) > MAX_PENDING_THREAD_IDS_BYTES:
        raise ValueError("source cursor pending_thread_ids exceeds its bound")
    try:
        values = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError("source cursor pending_thread_ids is invalid") from exc
    if not isinstance(values, list) or len(values) > 10_000 or any(
        not isinstance(value, str) or not value or len(value) > 256
        for value in values
    ):
        raise ValueError("source cursor pending_thread_ids is invalid")
    if len(values) != len(set(values)):
        raise ValueError("source cursor pending_thread_ids contains duplicates")
    return tuple(values)


def _encode_pending_thread_ids(values: Sequence[str]) -> str:
    if len(values) > 10_000:
        raise ValueError("pending_thread_ids exceeds durable cursor capacity")
    encoded = json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_PENDING_THREAD_IDS_BYTES:
        raise ValueError("pending_thread_ids exceeds durable cursor capacity")
    return encoded


def _merge_reset_seen_item_ids(
    existing: set[str],
    additions: Sequence[str],
) -> tuple[set[str], bool]:
    output = set(existing)
    overflow = False
    for value in sorted(set(additions)):
        candidate = {*output, value}
        if len(_encode_reset_seen_item_ids(candidate)) > 8_000:
            overflow = True
            continue
        output = candidate
    return output, overflow


def _encode_reset_seen_item_ids(values: set[str]) -> str:
    return json.dumps(sorted(values), ensure_ascii=False, separators=(",", ":"))


def _required_cursor_text(metadata: dict[str, Any], key: str) -> str:
    value = _optional_cursor_text(metadata.get(key))
    if value is None:
        raise ValueError(f"partial source cursor is missing {key}")
    return value


class _DurablyBudgetedDetectorProvider:
    """Reserve local-day detector capacity immediately before each model call."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        operational_service: OperationalService,
        policy: OperationsPolicy,
        run_id: str,
        started: datetime,
    ) -> None:
        self._provider = provider
        self._service = operational_service
        self._policy = policy
        self._run_id = run_id
        self._started = started
        self._local_day = (
            started.astimezone(ZoneInfo(policy.operator.timezone)).date().isoformat()
        )
        self.name = provider.name
        self.model = provider.model
        self.gmail_input_overhead_token_ceiling = getattr(
            provider,
            "gmail_input_overhead_token_ceiling",
            None,
        )
        self.gmail_output_token_ceiling = getattr(
            provider,
            "gmail_output_token_ceiling",
            None,
        )
        self._latched_reason: str | None = None
        self._pre_reserved_calls = 0
        self._pre_reserved_input_tokens = 0
        self._pre_reserved_total_tokens = 0
        self._actual_input_tokens = 0
        self._actual_total_tokens = 0
        self._actual_usage_complete = True
        self._reported_provider_calls = 0
        self._unreported_provider_calls = 0

    def complete(self, prompt: str) -> str:
        if self._latched_reason is not None:
            raise DailyBudgetExceeded(self._latched_reason)
        token_ceiling = gmail_detector_token_ceiling(self._provider, prompt)
        reservations = self._service.reserve_daily_budgets(
            source_type=GMAIL_CONNECTOR_ID,
            reservations={
                "detector_calls": (
                    1,
                    self._policy.budgets.gmail.detector_calls_per_day,
                ),
                "detector_input_tokens": (
                    token_ceiling.input_tokens,
                    self._policy.budgets.gmail.detector_input_tokens_per_day,
                ),
                "detector_total_tokens": (
                    token_ceiling.total_tokens,
                    self._policy.budgets.gmail.detector_total_tokens_per_day,
                ),
            },
            local_day=self._local_day,
            policy_version=self._policy.version_ref,
            run_id=self._run_id,
            created_at=self._started.isoformat(),
        )
        self._pre_reserved_calls += 1
        self._pre_reserved_input_tokens += token_ceiling.input_tokens
        self._pre_reserved_total_tokens += token_ceiling.total_tokens

        result: str | None = None
        provider_error: BaseException | None = None
        with capture_provider_usage(self._provider) as capture:
            try:
                result = self._provider.complete(prompt)
            except BaseException as exc:
                provider_error = exc

        try:
            self._reconcile_usage(
                capture.records,
                reserved_input_tokens=token_ceiling.input_tokens,
                reserved_total_tokens=token_ceiling.total_tokens,
                reservation_totals={
                    metric: int(value["used"])
                    for metric, value in reservations.items()
                },
            )
        except DailyBudgetExceeded as exc:
            if provider_error is not None:
                raise exc from provider_error
            raise
        if provider_error is not None:
            raise provider_error
        if result is None:
            raise RuntimeError("detector provider returned no result")
        return result

    def usage_stats(self) -> dict[str, Any]:
        return {
            "pre_reserved_calls": self._pre_reserved_calls,
            "pre_reserved_input_tokens": self._pre_reserved_input_tokens,
            "pre_reserved_total_tokens": self._pre_reserved_total_tokens,
            "actual_input_tokens": self._actual_input_tokens,
            "actual_total_tokens": self._actual_total_tokens,
            "actual_usage_complete": self._actual_usage_complete,
            "reported_provider_calls": self._reported_provider_calls,
            "unreported_provider_calls": self._unreported_provider_calls,
        }

    def _reconcile_usage(
        self,
        records: Sequence[ProviderUsageRecord],
        *,
        reserved_input_tokens: int,
        reserved_total_tokens: int,
        reservation_totals: Mapping[str, int],
    ) -> None:
        reported = [record for record in records if record.usage is not None]
        unreported_calls = sum(record.usage is None for record in records)
        if not records:
            # The wrapper invoked the provider once, but it did not emit even an
            # attempt record. Treat that invocation as unreported and stop.
            unreported_calls = 1
        actual_calls = len(records) if records else 1
        actual_input_tokens = sum(
            int((record.usage or {}).get("input_tokens") or 0)
            for record in reported
        )
        actual_total_tokens = sum(
            int((record.usage or {}).get("total_tokens") or 0)
            for record in reported
        )
        self._reported_provider_calls += len(reported)
        self._unreported_provider_calls += unreported_calls
        self._actual_input_tokens += actual_input_tokens
        self._actual_total_tokens += actual_total_tokens
        if unreported_calls:
            self._actual_usage_complete = False

        deltas = {
            "detector_calls": max(0, actual_calls - 1),
            "detector_input_tokens": max(
                0,
                actual_input_tokens - reserved_input_tokens,
            ),
            "detector_total_tokens": max(
                0,
                actual_total_tokens - reserved_total_tokens,
            ),
        }
        positive_deltas = {
            metric: (amount, None)
            for metric, amount in deltas.items()
            if amount > 0
        }
        used_totals = dict(reservation_totals)
        if positive_deltas:
            adjustments = self._service.reserve_daily_budgets(
                source_type=GMAIL_CONNECTOR_ID,
                reservations=positive_deltas,
                local_day=self._local_day,
                policy_version=self._policy.version_ref,
                run_id=self._run_id,
                created_at=self._started.isoformat(),
            )
            used_totals.update(
                {
                    metric: int(value["used"])
                    for metric, value in adjustments.items()
                }
            )

        limits = {
            "detector_calls": self._policy.budgets.gmail.detector_calls_per_day,
            "detector_input_tokens": (
                self._policy.budgets.gmail.detector_input_tokens_per_day
            ),
            "detector_total_tokens": (
                self._policy.budgets.gmail.detector_total_tokens_per_day
            ),
        }
        overages = [
            f"{metric} {used_totals[metric]}/{limit}"
            for metric, limit in limits.items()
            if int(used_totals.get(metric, 0)) > int(limit)
        ]
        if overages:
            self._latch(
                "observed detector usage exceeded the daily budget: "
                + ", ".join(overages)
            )
        if unreported_calls:
            self._latch(
                "detector provider did not report complete token usage; "
                "further calls are blocked"
            )

    def _latch(self, reason: str) -> None:
        self._latched_reason = reason
        raise DailyBudgetExceeded(reason)


def _empty_provider_usage_stats() -> dict[str, Any]:
    return {
        "pre_reserved_calls": 0,
        "pre_reserved_input_tokens": 0,
        "pre_reserved_total_tokens": 0,
        "actual_input_tokens": 0,
        "actual_total_tokens": 0,
        "actual_usage_complete": True,
        "reported_provider_calls": 0,
        "unreported_provider_calls": 0,
    }
