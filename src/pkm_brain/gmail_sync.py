from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .connector_auth import connector_auth_status
from .gmail_mirror import (
    GmailMirrorCheckpoint,
    GmailMirrorCheckpointUpdate,
    GmailMirrorQuarantineInput,
    GmailMirrorStore,
    GmailMirrorSyncResult,
    GmailMirrorThreadInput,
)
from .google_api import (
    GoogleAPIClient,
    GoogleAPIError,
    GoogleQuotaBudget,
    GoogleTokenManager,
)
from .google_sources import (
    GMAIL_THREAD_PARSER_VERSION,
    GmailFetchResult,
    GmailThreadReader,
)
from .operational_budget import DailyBudgetExceeded, daily_budget_usage
from .operational_service import OperationalService
from .operations_policy import OperationsPolicy, load_operations_policy
from .paths import BrainPaths


GMAIL_MIRROR_STREAM = "mailbox"
GMAIL_MIRROR_INITIAL_QUERY = "newer_than:7d -in:spam -in:trash"
GMAIL_MIRROR_MAX_THREADS_PER_SYNC = 200
GMAIL_MIRROR_MAX_PAGES_PER_SYNC = 20
GMAIL_MIRROR_API_ATTEMPTS = 2
# One failed saved-token/history read, one replacement profile read, and the
# bounded 20-page list traversal. Thread payload GETs are budgeted separately.
GMAIL_MIRROR_WORST_CASE_LOGICAL_OVERHEAD = GMAIL_MIRROR_MAX_PAGES_PER_SYNC + 2
GMAIL_MIRROR_MAX_QUARANTINE_RETRIES_PER_SYNC = 10


@dataclass(frozen=True)
class GmailMirrorSyncOutcome:
    """One provider read durably accepted by the local mirror."""

    fetch: GmailFetchResult
    mirror: GmailMirrorSyncResult
    previous_checkpoint: GmailMirrorCheckpoint | None
    retry_fetch: GmailFetchResult | None = None
    retry_mirror: GmailMirrorSyncResult | None = None
    quarantine_backlog_count: int = 0
    retry_deferred_reason: str | None = None

    @property
    def checkpoint(self) -> GmailMirrorCheckpoint:
        if self.retry_mirror is not None:
            return self.retry_mirror.checkpoint
        return self.mirror.checkpoint

    @property
    def cursor_advanced(self) -> bool:
        before = self.previous_checkpoint
        after = self.checkpoint
        if before is None:
            return bool(
                after.history_id
                or after.continuation_page_token
                or after.pending_thread_ids
            )
        return (
            before.history_id,
            before.continuation_page_token,
            before.pending_thread_ids,
            before.continuation_history_id,
        ) != (
            after.history_id,
            after.continuation_page_token,
            after.pending_thread_ids,
            after.continuation_history_id,
        )

    def as_dict(self) -> dict[str, Any]:
        checkpoint = self.checkpoint
        retry_mirror = self.retry_mirror
        degraded = self.quarantine_backlog_count > 0
        return {
            "status": (
                "complete"
                if checkpoint.coverage_complete and not degraded
                else "partial"
            ),
            "mailbox_status": (
                "complete"
                if checkpoint.coverage_complete
                else "resyncing"
                if checkpoint.reset_required
                else "partial"
            ),
            "mode": checkpoint.mode,
            "coverage_complete": checkpoint.coverage_complete,
            "reset_required": checkpoint.reset_required,
            "cursor_advanced": self.cursor_advanced,
            "history_id": checkpoint.history_id,
            "checkpoint_generation": checkpoint.generation,
            "last_success_at": checkpoint.last_success_at,
            "changed_thread_count": len(self.fetch.changed_thread_ids),
            "missing_thread_count": len(self.fetch.missing_thread_ids),
            "inserted_revisions": self.mirror.inserted_revisions
            + (retry_mirror.inserted_revisions if retry_mirror else 0),
            "current_updates": self.mirror.current_updates
            + (retry_mirror.current_updates if retry_mirror else 0),
            "tombstones": self.mirror.tombstones
            + (retry_mirror.tombstones if retry_mirror else 0),
            "queued": self.mirror.queued + (retry_mirror.queued if retry_mirror else 0),
            "superseded": self.mirror.superseded
            + (retry_mirror.superseded if retry_mirror else 0),
            "quarantined_thread_count": self.quarantine_backlog_count,
            "quarantine_retry_thread_count": (
                len(self.retry_fetch.changed_thread_ids) if self.retry_fetch else 0
            ),
            "quarantine_retry_status": (
                "deferred"
                if self.retry_deferred_reason
                else "attempted"
                if self.retry_fetch is not None
                else "current"
                if not self.quarantine_backlog_count
                else "backoff"
            ),
            "quarantine_retry_deferred_reason": self.retry_deferred_reason,
            "api_requests": self.fetch.api_requests
            + (self.retry_fetch.api_requests if self.retry_fetch else 0),
            "api_quota_units": self.fetch.quota_units
            + (self.retry_fetch.quota_units if self.retry_fetch else 0),
            "pages": self.fetch.pages_fetched
            + (self.retry_fetch.pages_fetched if self.retry_fetch else 0),
        }


_SYNC_LOCKS_GUARD = threading.Lock()
_SYNC_LOCKS: dict[tuple[str, str], threading.Lock] = {}


class GmailMirrorSynchronizer:
    """Fetch Gmail changes and commit them without invoking operational models."""

    def __init__(
        self,
        paths: BrainPaths,
        operational_service: OperationalService,
        *,
        store: GmailMirrorStore | None = None,
        reader: GmailThreadReader | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.paths = paths
        self.operational_service = operational_service
        self.store = store or GmailMirrorStore(paths.gmail_mirror_sqlite_path)
        self._reader = reader
        self._now = now or (lambda: datetime.now(timezone.utc))

    def sync(
        self,
        *,
        policy: OperationsPolicy | None = None,
        run_id: str | None = None,
    ) -> GmailMirrorSyncOutcome:
        active_policy = policy or load_operations_policy(self.paths)
        if not active_policy.sources.gmail.enabled:
            raise ValueError("Gmail operational source is disabled")
        if not active_policy.sources.gmail.content_access_approved:
            raise ValueError("Gmail content access is not approved")
        started = _utc(self._now()).replace(microsecond=0)
        account_key = active_policy.sources.gmail.account_key
        lock = _sync_lock(self.paths.gmail_mirror_sqlite_path, account_key)
        with lock:
            self.store.initialize()
            previous = self.store.get_checkpoint(
                account_key,
                GMAIL_MIRROR_STREAM,
            )
            resume_full = bool(
                previous and not previous.coverage_complete and previous.mode == "full"
            )
            reader = self._reader_for(
                active_policy,
                run_id=run_id,
                started=started,
            )
            result = reader.fetch(
                query=GMAIL_MIRROR_INITIAL_QUERY,
                history_id=(
                    None
                    if resume_full
                    else previous.history_id
                    if previous is not None
                    else None
                ),
                continuation_page_token=(
                    previous.continuation_page_token if previous is not None else None
                ),
                baseline_history_id=(
                    previous.baseline_history_id if resume_full else None
                ),
                pending_thread_ids=(
                    ()
                    if resume_full or previous is None
                    else previous.pending_thread_ids
                ),
                continuation_history_id=(
                    None
                    if resume_full or previous is None
                    else previous.continuation_history_id
                ),
            )
            inputs = _mirror_inputs(result)
            quarantined = _mirror_quarantines(result)
            update = GmailMirrorCheckpointUpdate.from_fetch_result(
                account_key,
                result,
                previous=previous,
                updated_at=started.isoformat(),
                stream_key=GMAIL_MIRROR_STREAM,
            )
            mirror = self.store.apply_sync_unit(
                update,
                inputs,
                missing_thread_ids=result.missing_thread_ids,
                quarantined_threads=quarantined,
                parser_version=GMAIL_THREAD_PARSER_VERSION,
            )
            outcome = GmailMirrorSyncOutcome(
                fetch=result,
                mirror=mirror,
                previous_checkpoint=previous,
                quarantine_backlog_count=self.store.quarantine_counts(account_key)[
                    "unresolved_count"
                ],
            )
            if (
                not mirror.checkpoint.coverage_complete
                or not mirror.checkpoint.history_id
            ):
                return outcome
            retry_limit = min(
                GMAIL_MIRROR_MAX_QUARANTINE_RETRIES_PER_SYNC,
                max(1, int(getattr(reader, "max_threads", 1))),
            )
            due = self.store.list_due_quarantine_retries(
                account_key,
                parser_version=GMAIL_THREAD_PARSER_VERSION,
                as_of=started.isoformat(),
                limit=retry_limit,
            )
            if not due:
                return outcome
            retry_ids = tuple(item.thread_id for item in due)
            try:
                retry_result = reader.fetch(
                    query=GMAIL_MIRROR_INITIAL_QUERY,
                    history_id=mirror.checkpoint.history_id,
                    continuation_page_token=None,
                    baseline_history_id=None,
                    pending_thread_ids=retry_ids,
                    continuation_history_id=mirror.checkpoint.history_id,
                )
            except DailyBudgetExceeded:
                return replace(
                    outcome,
                    quarantine_backlog_count=self.store.quarantine_counts(account_key)[
                        "unresolved_count"
                    ],
                    retry_deferred_reason="daily_budget_exhausted",
                )
            except GoogleAPIError:
                return replace(
                    outcome,
                    quarantine_backlog_count=self.store.quarantine_counts(account_key)[
                        "unresolved_count"
                    ],
                    retry_deferred_reason="provider_retry_unavailable",
                )
            except RuntimeError as exc:
                if not _retry_transport_failure(exc):
                    raise
                return replace(
                    outcome,
                    quarantine_backlog_count=self.store.quarantine_counts(account_key)[
                        "unresolved_count"
                    ],
                    retry_deferred_reason="provider_retry_unavailable",
                )
            if not retry_result.coverage_complete:
                raise RuntimeError("bounded Gmail quarantine retry did not complete")
            retry_update = GmailMirrorCheckpointUpdate.from_fetch_result(
                account_key,
                retry_result,
                previous=mirror.checkpoint,
                updated_at=started.isoformat(),
                stream_key=GMAIL_MIRROR_STREAM,
            )
            retry_mirror = self.store.apply_sync_unit(
                retry_update,
                _mirror_inputs(retry_result),
                missing_thread_ids=retry_result.missing_thread_ids,
                quarantined_threads=_mirror_quarantines(retry_result),
                quarantine_retry=True,
                parser_version=GMAIL_THREAD_PARSER_VERSION,
            )
            return replace(
                outcome,
                retry_fetch=retry_result,
                retry_mirror=retry_mirror,
                quarantine_backlog_count=self.store.quarantine_counts(account_key)[
                    "unresolved_count"
                ],
            )

    def _reader_for(
        self,
        policy: OperationsPolicy,
        *,
        run_id: str | None,
        started: datetime,
    ) -> GmailThreadReader:
        if self._reader is not None:
            return self._reader
        tokens = GoogleTokenManager(
            self.paths,
            "gmail",
            expected_email=policy.operator.gmail.email,
            expected_subject=policy.operator.gmail.provider_subject,
            require_exact_scopes=True,
        )
        client = GoogleAPIClient(
            "gmail",
            tokens,
            quota=GoogleQuotaBudget(
                requests_per_second=2.0,
                on_acquire=self._api_budget_reserver(
                    policy=policy,
                    run_id=run_id,
                    started=started,
                ),
                shared_key=f"gmail:{policy.operator.gmail.provider_subject}",
            ),
            attempts=GMAIL_MIRROR_API_ATTEMPTS,
        )
        local_day = (
            started.astimezone(ZoneInfo(policy.operator.timezone)).date().isoformat()
        )
        used_requests = int(
            daily_budget_usage(
                self.paths.ops_sqlite_path,
                local_day=local_day,
            )
            .get("gmail", {})
            .get("api_requests", 0)
        )
        return GmailThreadReader(
            client,
            max_threads=_sync_thread_cap(
                policy.budgets.gmail.api_requests_per_day,
                used_requests=used_requests,
            ),
            max_pages=GMAIL_MIRROR_MAX_PAGES_PER_SYNC,
            operator_emails=(policy.operator.gmail.email,),
        )

    def _api_budget_reserver(
        self,
        *,
        policy: OperationsPolicy,
        run_id: str | None,
        started: datetime,
    ) -> Callable[[int], None]:
        local_day = (
            started.astimezone(ZoneInfo(policy.operator.timezone)).date().isoformat()
        )

        def reserve(quota_units: int) -> None:
            self.operational_service.reserve_daily_budgets(
                source_type="gmail",
                reservations={
                    "api_requests": (
                        1,
                        policy.budgets.gmail.api_requests_per_day,
                    ),
                    "api_quota_units": (quota_units, None),
                },
                local_day=local_day,
                policy_version=policy.version_ref,
                run_id=run_id,
                created_at=started.isoformat(),
            )

        return reserve


def run_scheduled_gmail_mirror_sync(
    paths: BrainPaths,
    operational_service: OperationalService,
) -> dict[str, Any]:
    """Fetch-only daemon job; explicit policy and grant are required every run."""

    try:
        policy = load_operations_policy(paths)
    except FileNotFoundError:
        return {
            "status": "skipped",
            "reason": "Gmail operations policy is not configured",
        }
    if not policy.sources.gmail.enabled:
        return {
            "status": "skipped",
            "reason": "Gmail operational source is disabled",
        }
    if not policy.sources.gmail.content_access_approved:
        return {
            "status": "skipped",
            "reason": "Gmail content access is not approved",
        }
    auth = connector_auth_status(paths, "gmail") or {}
    if auth.get("status") != "connected":
        return {
            "status": "skipped",
            "reason": f"Gmail read-only grant is {auth.get('status') or 'unavailable'}",
        }
    if not paths.ops_sqlite_path.is_file():
        # The daemon owns the writer lease. Once policy and auth are explicitly
        # approved, the scheduled source lane may create its local control store
        # without requiring a first manual Shadow click.
        operational_service.initialize()
    try:
        return (
            GmailMirrorSynchronizer(paths, operational_service)
            .sync(
                policy=policy,
            )
            .as_dict()
        )
    except DailyBudgetExceeded:
        return {
            "status": "partial",
            "message": "Gmail updates paused at Brain's daily Gmail safety budget.",
            "stopped_reason": "daily_budget_exhausted",
        }
    except GoogleAPIError as exc:
        if (
            exc.reason
            in {
                "dailyLimitExceeded",
                "quotaExceeded",
                "rateLimitExceeded",
                "userRateLimitExceeded",
                "RESOURCE_EXHAUSTED",
            }
            or exc.status == 429
        ):
            return {
                "status": "partial",
                "message": "Google asked Brain to slow down; updates will retry.",
                "stopped_reason": "provider_rate_limited",
            }
        return {
            "status": "failed",
            "message": "Gmail update check stopped safely and will retry.",
            "error_code": "gmail_mirror_GoogleAPIError",
        }


def _sync_thread_cap(
    api_requests_per_day: int,
    *,
    used_requests: int = 0,
) -> int:
    # Bound the whole unit by actual worst-case attempts. This includes an
    # invalid-token/history recovery read, a replacement profile, every bounded
    # page-list call, and each thread GET. The API client uses the same fixed
    # attempt count, so transient retries cannot consume capacity reserved for
    # committing the tail of the unit.
    remaining = int(api_requests_per_day) - int(used_requests)
    logical_capacity = remaining // GMAIL_MIRROR_API_ATTEMPTS
    if logical_capacity <= GMAIL_MIRROR_WORST_CASE_LOGICAL_OVERHEAD:
        raise DailyBudgetExceeded(
            "gmail api_requests daily budget exhausted "
            f"({used_requests}/{api_requests_per_day} already reserved)"
        )
    return min(
        GMAIL_MIRROR_MAX_THREADS_PER_SYNC,
        logical_capacity - GMAIL_MIRROR_WORST_CASE_LOGICAL_OVERHEAD,
    )


def _mirror_inputs(result: GmailFetchResult) -> tuple[GmailMirrorThreadInput, ...]:
    raw_by_id = {
        str(raw.get("id") or ""): raw
        for raw in result.raw_threads
        if str(raw.get("id") or "")
    }
    return tuple(
        GmailMirrorThreadInput(
            thread=thread,
            raw_payload=raw_by_id.get(thread.thread_id) or {},
        )
        for thread in result.threads
    )


def _mirror_quarantines(
    result: GmailFetchResult,
) -> tuple[GmailMirrorQuarantineInput, ...]:
    return tuple(
        GmailMirrorQuarantineInput(
            thread_id=failure.thread_id,
            source_revision=failure.source_revision,
            stage=failure.stage,
            error=failure.error,
            payload_sha256=failure.payload_sha256,
            parser_version=GMAIL_THREAD_PARSER_VERSION,
        )
        for failure in result.quarantined_threads
    )


def _retry_transport_failure(error: RuntimeError) -> bool:
    detail = str(error).casefold()
    return (
        "api could not be reached after" in detail
        or "google api returned invalid json" in detail
        or "google api returned a non-object json response" in detail
    )


def _sync_lock(db_path: Path, account_key: str) -> threading.Lock:
    key = (str(db_path), account_key)
    with _SYNC_LOCKS_GUARD:
        return _SYNC_LOCKS.setdefault(key, threading.Lock())


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Gmail mirror timestamps must include a timezone")
    return value.astimezone(timezone.utc)
