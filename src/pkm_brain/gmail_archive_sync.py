from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .connector_auth import connector_auth_status
from .gmail_archive import gmail_archive_identity_fingerprint
from .gmail_archive_source import (
    GmailArchiveReader,
    GmailArchiveSourceBatch,
    GmailHistoryExpired,
    GmailPageTokenExpired,
)
from .google_api import GoogleAPIClient, GoogleQuotaBudget, GoogleTokenManager
from .operational_budget import DailyBudgetExceeded, daily_budget_usage
from .operational_service import OperationalService
from .operations_policy import OperationsPolicy, load_operations_policy
from .paths import BrainPaths


GMAIL_ARCHIVE_PAGE_SIZE = 250
GMAIL_ARCHIVE_API_ATTEMPTS = 2
GMAIL_ARCHIVE_REQUESTS_PER_SECOND = 4.0
GMAIL_ARCHIVE_OPERATIONAL_HEADROOM = 250
GMAIL_ARCHIVE_MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024


class ArchiveStore(Protocol):
    def initialize(self) -> None: ...

    def provision_key(self) -> None: ...

    def get_state(self, account_key: str) -> Any | None: ...

    def apply_batch(
        self,
        account_key: str,
        *,
        messages: tuple[Any, ...] = (),
        deleted_message_ids: tuple[str, ...] = (),
        state: Any,
    ) -> Any: ...

    def status(self, account_key: str) -> Any: ...


@dataclass(frozen=True)
class GmailArchiveSyncOutcome:
    phase: str
    fetched: int
    inserted: int
    updated: int
    deleted: int
    skipped: int
    api_requests: int
    quota_units: int
    processed: int
    estimate: int | None
    coverage_complete: bool
    reset_required: bool
    stopped_reason: str | None
    archive_status: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        complete = (
            self.phase == "live"
            and self.coverage_complete
            and self.stopped_reason is None
        )
        paused_messages = {
            "low_disk_space": "Secure Gmail history copy needs more free disk space.",
            "daily_budget_headroom": (
                "Secure Gmail history copy paused at today's Gmail limit."
            ),
            "malformed_message": (
                "Secure Gmail history copy paused because one Gmail message "
                "could not be read safely."
            ),
            "page_token_restarted": (
                "Secure Gmail history copy restarted an expired Gmail page."
            ),
            "history_rescan": (
                "Secure Gmail history copy is safely rescanning after Gmail "
                "expired its update cursor."
            ),
        }
        if complete:
            message = "Secure Gmail history copy is current."
        elif self.stopped_reason:
            message = paused_messages.get(
                self.stopped_reason,
                "Secure Gmail history copy is paused and will retry.",
            )
        elif self.phase == "backfill":
            stored = int(
                self.archive_status.get(
                    "active_message_count",
                    self.archive_status.get("message_count", self.processed),
                )
            )
            noun = "message" if stored == 1 else "messages"
            message = f"Copying Gmail history: {stored:,} {noun} stored."
        else:
            message = "Secure Gmail history is copied; checking recent changes."
        return {
            "status": "complete" if complete else "partial",
            "message": message,
            "phase": self.phase,
            "messages_fetched": self.fetched,
            "inserted": self.inserted,
            "updated": self.updated,
            "deleted": self.deleted,
            "skipped": self.skipped,
            "api_requests": self.api_requests,
            "api_quota_units": self.quota_units,
            "processed": self.processed,
            "estimate": self.estimate,
            "coverage_complete": self.coverage_complete,
            "reset_required": self.reset_required,
            "stopped_reason": self.stopped_reason,
            "progress": self.archive_status,
        }


class GmailArchiveSynchronizer:
    """Copy one bounded Gmail page, without running an LLM or Knowledge ingest."""

    def __init__(
        self,
        paths: BrainPaths,
        operational_service: OperationalService,
        *,
        store: ArchiveStore | None = None,
        reader: GmailArchiveReader | None = None,
        now: Callable[[], datetime] | None = None,
        usage_reader: Callable[..., dict[str, dict[str, int]]] | None = None,
        free_bytes_reader: Callable[[Path], int] | None = None,
    ) -> None:
        self.paths = paths
        self.operational_service = operational_service
        self.store = store or _production_store(paths)
        self.reader = reader
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.usage_reader = usage_reader or daily_budget_usage
        self.free_bytes_reader = free_bytes_reader or (
            lambda path: int(shutil.disk_usage(path).free)
        )

    def sync(
        self,
        *,
        policy: OperationsPolicy | None = None,
        run_id: str | None = None,
    ) -> GmailArchiveSyncOutcome:
        active_policy = policy or load_operations_policy(self.paths)
        _require_archive_policy(active_policy)
        started = _utc(self.now()).replace(microsecond=0)
        account_key = active_policy.sources.gmail.account_key
        identity_fingerprint = gmail_archive_identity_fingerprint(
            active_policy.operator.gmail.email,
            active_policy.operator.gmail.provider_subject,
        )
        self.store.initialize()
        self.store.provision_key()
        state = self.store.get_state(account_key)
        if (
            state is not None
            and getattr(state, "identity_fingerprint", None) != identity_fingerprint
        ):
            raise ValueError("Gmail archive identity does not match the approved account")
        if not self._disk_ready():
            return self._outcome(state, stopped_reason="low_disk_space")
        if not self._budget_ready(active_policy, started):
            return self._outcome(state, stopped_reason="daily_budget_headroom")

        reader = self.reader or self._production_reader(
            active_policy, run_id=run_id, started=started
        )
        if state is None:
            history_id, requests, units = reader.capture_history_id()
            state = _initial_state(
                account_key,
                _utc(self.now()).replace(microsecond=0),
                active_policy.sources.gmail.archive.initial_days,
                history_id,
                identity_fingerprint,
            )
            self.store.apply_batch(account_key, state=state)
        else:
            requests = 0
            units = 0

        if state.phase == "backfill":
            try:
                batch = reader.backfill_page(state.query, page_token=state.page_token)
            except GmailPageTokenExpired:
                reset = replace(
                    state,
                    page_token=None,
                    pending_message_ids=(),
                    continuation_history_id=None,
                    coverage_complete=False,
                    reset_required=True,
                    updated_at=started.isoformat(),
                    error="Saved Gmail page token expired; restarting this scan.",
                )
                self.store.apply_batch(account_key, state=reset)
                return self._outcome(reset, stopped_reason="page_token_restarted")
            if batch.failures:
                return self._outcome(
                    state,
                    fetched=len(batch.messages),
                    skipped=len(batch.failures),
                    api_requests=requests + batch.api_requests,
                    quota_units=units + batch.quota_units,
                    stopped_reason="malformed_message",
                )
            next_state = _after_backfill(state, batch, started)
        else:
            try:
                batch = reader.history_page(
                    state.history_id or state.baseline_history_id or "",
                    page_token=state.page_token,
                    pending_ids=state.pending_message_ids,
                    continuation_history_id=state.continuation_history_id,
                )
            except GmailPageTokenExpired:
                reset = replace(
                    state,
                    page_token=None,
                    pending_message_ids=(),
                    continuation_history_id=None,
                    coverage_complete=False,
                    reset_required=True,
                    updated_at=started.isoformat(),
                    error="Saved Gmail page token expired; replaying recent changes.",
                )
                self.store.apply_batch(account_key, state=reset)
                return self._outcome(reset, stopped_reason="page_token_restarted")
            except GmailHistoryExpired:
                history_id, extra_requests, extra_units = reader.capture_history_id()
                reset = replace(
                    state,
                    phase="backfill",
                    query=_query_from_window_start(state.window_start),
                    page_token=None,
                    history_id=history_id,
                    baseline_history_id=history_id,
                    pending_message_ids=(),
                    continuation_history_id=None,
                    estimate=None,
                    processed=0,
                    coverage_complete=False,
                    reset_required=True,
                    last_success_at=None,
                    updated_at=started.isoformat(),
                    error=(
                        "Gmail history cursor expired; rescanning from the original "
                        "local-history boundary."
                    ),
                )
                self.store.apply_batch(account_key, state=reset)
                return self._outcome(
                    reset,
                    api_requests=requests + extra_requests,
                    quota_units=units + extra_units,
                    stopped_reason="history_rescan",
                )
            if batch.failures:
                return self._outcome(
                    state,
                    fetched=len(batch.messages),
                    skipped=len(batch.failures),
                    api_requests=requests + batch.api_requests,
                    quota_units=units + batch.quota_units,
                    stopped_reason="malformed_message",
                )
            next_state = _after_history(state, batch, started)

        archive_messages = _archive_messages(batch)
        applied = self.store.apply_batch(
            account_key,
            messages=archive_messages,
            deleted_message_ids=batch.missing_ids,
            state=next_state,
        )
        return self._outcome(
            applied.state,
            fetched=len(batch.messages),
            inserted=int(applied.inserted),
            updated=int(applied.updated),
            deleted=int(applied.deleted),
            skipped=len(batch.failures),
            api_requests=requests + batch.api_requests,
            quota_units=units + batch.quota_units,
        )

    def _production_reader(
        self,
        policy: OperationsPolicy,
        *,
        run_id: str | None,
        started: datetime,
    ) -> GmailArchiveReader:
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
                requests_per_second=GMAIL_ARCHIVE_REQUESTS_PER_SECOND,
                on_acquire=self._budget_reserver(policy, run_id, started),
            ),
            attempts=GMAIL_ARCHIVE_API_ATTEMPTS,
        )
        return GmailArchiveReader(client, page_size=GMAIL_ARCHIVE_PAGE_SIZE)

    def _budget_reserver(
        self,
        policy: OperationsPolicy,
        run_id: str | None,
        started: datetime,
    ) -> Callable[[int], None]:
        local_day = started.astimezone(
            ZoneInfo(policy.operator.timezone)
        ).date().isoformat()

        def reserve(quota_units: int) -> None:
            self.operational_service.reserve_daily_budgets(
                source_type="gmail",
                reservations={
                    "api_requests": (1, policy.budgets.gmail.api_requests_per_day),
                    "api_quota_units": (quota_units, None),
                },
                local_day=local_day,
                policy_version=policy.version_ref,
                run_id=run_id,
                created_at=started.isoformat(),
            )

        return reserve

    def _budget_ready(self, policy: OperationsPolicy, started: datetime) -> bool:
        local_day = started.astimezone(
            ZoneInfo(policy.operator.timezone)
        ).date().isoformat()
        used = int(
            self.usage_reader(self.paths.ops_sqlite_path, local_day=local_day)
            .get("gmail", {})
            .get("api_requests", 0)
        )
        needed = GMAIL_ARCHIVE_PAGE_SIZE + 2 + GMAIL_ARCHIVE_OPERATIONAL_HEADROOM
        return policy.budgets.gmail.api_requests_per_day - used >= needed

    def _disk_ready(self) -> bool:
        return (
            self.free_bytes_reader(self.paths.gmail_archive_sqlite_path.parent)
            >= GMAIL_ARCHIVE_MIN_FREE_BYTES
        )

    def _outcome(
        self,
        state: Any | None,
        *,
        fetched: int = 0,
        inserted: int = 0,
        updated: int = 0,
        deleted: int = 0,
        skipped: int = 0,
        api_requests: int = 0,
        quota_units: int = 0,
        stopped_reason: str | None = None,
    ) -> GmailArchiveSyncOutcome:
        account = getattr(state, "account_key", "")
        status = _safe_status(self.store.status(account)) if account else {}
        return GmailArchiveSyncOutcome(
            phase=str(getattr(state, "phase", "not_started")),
            fetched=fetched,
            inserted=inserted,
            updated=updated,
            deleted=deleted,
            skipped=skipped,
            api_requests=api_requests,
            quota_units=quota_units,
            processed=int(getattr(state, "processed", 0)),
            estimate=getattr(state, "estimate", None),
            coverage_complete=bool(getattr(state, "coverage_complete", False)),
            reset_required=bool(getattr(state, "reset_required", False)),
            stopped_reason=stopped_reason,
            archive_status=status,
        )


def run_scheduled_gmail_archive_sync(
    paths: BrainPaths,
    operational_service: OperationalService,
) -> dict[str, Any]:
    try:
        policy = load_operations_policy(paths)
        _require_archive_policy(policy)
    except (FileNotFoundError, ValueError):
        return {"status": "skipped", "reason": "Secure Gmail history is not enabled"}
    auth = connector_auth_status(paths, "gmail") or {}
    if auth.get("status") != "connected":
        return {"status": "skipped", "reason": "Gmail read-only access is unavailable"}
    if not paths.ops_sqlite_path.is_file():
        operational_service.initialize()
    try:
        return GmailArchiveSynchronizer(paths, operational_service).sync(
            policy=policy
        ).as_dict()
    except DailyBudgetExceeded:
        return {
            "status": "partial",
            "message": "Secure Gmail history copy paused at today's Gmail limit.",
            "stopped_reason": "daily_budget_headroom",
        }
    except Exception:
        return {
            "status": "failed",
            "message": "Secure Gmail history copy stopped safely; retry from the Brain app.",
        }


def _require_archive_policy(policy: OperationsPolicy) -> None:
    gmail = policy.sources.gmail
    if not (
        gmail.enabled
        and gmail.content_access_approved
        and gmail.archive.enabled
        and gmail.archive.agent_access_approved
    ):
        raise ValueError("Secure Gmail history is not approved")


def _initial_state(
    account_key: str,
    started: datetime,
    days: int,
    history_id: str,
    identity_fingerprint: str,
) -> Any:
    from .gmail_archive import ArchiveState

    end = _utc(started)
    start = end - timedelta(days=days)
    return ArchiveState(
        account_key=account_key,
        phase="backfill",
        query=(
            f"after:{int(start.timestamp()) - 1} "
            f"before:{int(end.timestamp()) + 1}"
        ),
        window_start=start.isoformat(),
        window_end=end.isoformat(),
        updated_at=end.isoformat(),
        history_id=history_id,
        baseline_history_id=history_id,
        identity_fingerprint=identity_fingerprint,
    )


def _after_backfill(state: Any, batch: GmailArchiveSourceBatch, now: datetime) -> Any:
    done = batch.next_page_token is None
    return replace(
        state,
        phase="live" if done else "backfill",
        page_token=batch.next_page_token,
        history_id=state.baseline_history_id,
        estimate=batch.result_size_estimate or state.estimate,
        processed=state.processed + _attempted(batch),
        coverage_complete=False,
        reset_required=False,
        updated_at=now.isoformat(),
        error=None,
    )


def _after_history(state: Any, batch: GmailArchiveSourceBatch, now: datetime) -> Any:
    complete = batch.next_history_id is not None
    return replace(
        state,
        phase="live",
        page_token=batch.next_page_token,
        history_id=batch.next_history_id or state.history_id,
        pending_message_ids=batch.pending_ids,
        continuation_history_id=(
            None if complete else batch.continuation_history_id
        ),
        processed=state.processed + _attempted(batch),
        coverage_complete=complete,
        reset_required=False,
        last_success_at=now.isoformat() if complete else state.last_success_at,
        updated_at=now.isoformat(),
        error=None,
    )


def _attempted(batch: GmailArchiveSourceBatch) -> int:
    return len(batch.messages) + len(batch.missing_ids) + len(batch.failures)


def _archive_messages(batch: GmailArchiveSourceBatch) -> tuple[Any, ...]:
    from .gmail_archive import ArchiveMessage

    return tuple(
        ArchiveMessage(
            message_id=item.message_id,
            thread_id=item.thread_id,
            raw_rfc822=item.raw,
            internal_date=item.internal_date,
            label_ids=item.label_ids,
        )
        for item in batch.messages
    )


def _query_from_window_start(value: str) -> str:
    start = _utc(datetime.fromisoformat(value))
    return f"after:{int(start.timestamp()) - 1}"


def _safe_status(value: Any) -> dict[str, Any]:
    data = asdict(value) if is_dataclass(value) else dict(value) if isinstance(value, dict) else {}
    state = data.pop("state", None)
    if isinstance(state, dict):
        for key in (
            "phase",
            "processed",
            "estimate",
            "coverage_complete",
            "last_success_at",
            "error",
        ):
            data[key] = state.get(key)
    allowed = {
        "message_count",
        "active_message_count",
        "thread_count",
        "deleted_count",
        "hidden_count",
        "phase",
        "processed",
        "estimate",
        "coverage_complete",
        "last_success_at",
        "key_state",
        "error",
    }
    return {key: data[key] for key in allowed if key in data}


def _production_store(paths: BrainPaths) -> ArchiveStore:
    from .gmail_archive import GmailArchiveStore

    return GmailArchiveStore.for_paths(paths)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
