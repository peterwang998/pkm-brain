from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

from .google_api import GoogleAPIError
from .google_normalization import (
    NormalizedCalendarEvent,
    NormalizedGmailThread,
    normalize_calendar_event,
    normalize_gmail_thread,
    sanitize_calendar_event_payload,
    sanitize_gmail_thread_payload,
)
from .operational_state import MAX_PENDING_THREAD_IDS_BYTES


MAX_PENDING_THREAD_IDS = 10_000


class GoogleJSONClient(Protocol):
    def get_json(
        self,
        relative_path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        quota_units: int = 1,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CalendarFetchResult:
    mode: str
    raw_events: tuple[dict[str, Any], ...]
    events: tuple[NormalizedCalendarEvent, ...]
    next_sync_token: str | None
    reset_required: bool
    coverage_complete: bool
    pages_fetched: int
    continuation_page_token: str | None = None
    api_requests: int = 0
    quota_units: int = 0


@dataclass(frozen=True)
class GmailFetchResult:
    mode: str
    raw_threads: tuple[dict[str, Any], ...]
    threads: tuple[NormalizedGmailThread, ...]
    changed_thread_ids: tuple[str, ...]
    missing_thread_ids: tuple[str, ...]
    next_history_id: str | None
    reset_required: bool
    coverage_complete: bool
    pages_fetched: int
    continuation_page_token: str | None = None
    baseline_history_id: str | None = None
    pending_thread_ids: tuple[str, ...] = ()
    continuation_history_id: str | None = None
    api_requests: int = 0
    quota_units: int = 0


@dataclass
class _FetchUsage:
    api_requests: int = 0
    quota_units: int = 0

    def add(self, quota_units: int) -> None:
        self.api_requests += 1
        self.quota_units += quota_units


def calendar_occurrence_key(event: NormalizedCalendarEvent) -> str:
    """Return one stable key for an expanded occurrence and all its exceptions."""

    original = event.original_start_time or event.original_start_date
    if event.recurring_event_id and original:
        return f"{event.recurring_event_id}:{original}"
    return event.event_id


def calendar_event_is_inactive(event: NormalizedCalendarEvent) -> bool:
    """Cancelled events and meetings declined by the operator are not active work."""

    return event.cancelled or event.attendee_response == "declined"


class GoogleCalendarReader:
    """Bounded full/incremental reader for the owned primary calendar only."""

    def __init__(
        self,
        client: GoogleJSONClient,
        *,
        calendar_id: str = "primary",
        page_size: int = 250,
        max_pages: int = 20,
        max_events: int = 5_000,
    ) -> None:
        if calendar_id != "primary":
            raise ValueError(
                "the initial Calendar transport is restricted to calendarId=primary"
            )
        if not 1 <= page_size <= 2_500:
            raise ValueError("Calendar page_size must be between 1 and 2500")
        if max_pages <= 0 or max_events <= 0:
            raise ValueError("Calendar fetch bounds must be positive")
        self.client = client
        self.calendar_id = calendar_id
        self.page_size = page_size
        self.max_pages = max_pages
        self.max_events = max_events

    def fetch(
        self,
        *,
        time_min: str,
        time_max: str,
        sync_token: str | None = None,
        timezone_name: str | None = None,
        continuation_page_token: str | None = None,
    ) -> CalendarFetchResult:
        _bounded_window(time_min, time_max)
        page_token = _validated_page_token(continuation_page_token)
        usage = _FetchUsage()
        if sync_token:
            try:
                return self._fetch_pages(
                    mode="incremental",
                    sync_token=sync_token,
                    time_min=None,
                    time_max=None,
                    timezone_name=timezone_name,
                    continuation_page_token=page_token,
                    usage=usage,
                )
            except GoogleAPIError as exc:
                if exc.status != 410:
                    raise
                replacement = self._fetch_pages(
                    mode="full",
                    sync_token=None,
                    time_min=time_min,
                    time_max=time_max,
                    timezone_name=timezone_name,
                    continuation_page_token=None,
                    usage=usage,
                )
                return replace(replacement, reset_required=True)
        return self._fetch_pages(
            mode="full",
            sync_token=None,
            time_min=time_min,
            time_max=time_max,
            timezone_name=timezone_name,
            continuation_page_token=page_token,
            usage=usage,
        )

    def _fetch_pages(
        self,
        *,
        mode: str,
        sync_token: str | None,
        time_min: str | None,
        time_max: str | None,
        timezone_name: str | None,
        continuation_page_token: str | None,
        usage: _FetchUsage,
    ) -> CalendarFetchResult:
        raw_events: list[dict[str, Any]] = []
        page_token = continuation_page_token
        next_sync_token: str | None = None
        pages = 0
        coverage_complete = True
        while True:
            remaining = self.max_events - len(raw_events)
            if remaining <= 0 or pages >= self.max_pages:
                coverage_complete = False
                break
            params: dict[str, str | int] = {
                "maxResults": min(self.page_size, remaining),
                "showDeleted": "true",
                # Today needs concrete occurrences, not recurrence masters.  Google
                # keeps each expanded occurrence anchored to recurringEventId plus
                # originalStartTime, including moved and cancelled exceptions.
                "singleEvents": "true",
            }
            if sync_token:
                params["syncToken"] = sync_token
            else:
                assert time_min is not None and time_max is not None
                params["timeMin"] = time_min
                params["timeMax"] = time_max
            if timezone_name:
                params["timeZone"] = timezone_name
            if page_token:
                params["pageToken"] = page_token
            usage.add(1)
            payload = self.client.get_json(
                f"calendars/{urllib.parse.quote(self.calendar_id, safe='')}/events",
                params=params,
                quota_units=1,
            )
            pages += 1
            items = [
                item for item in payload.get("items") or [] if isinstance(item, Mapping)
            ]
            if len(items) > remaining:
                items = items[:remaining]
                coverage_complete = False
            raw_events.extend(sanitize_calendar_event_payload(item) for item in items)
            page_token = _optional_string(payload.get("nextPageToken"))
            if not page_token:
                next_sync_token = _optional_string(payload.get("nextSyncToken"))
                break
        if coverage_complete and not next_sync_token:
            raise RuntimeError("Calendar completed a sync without nextSyncToken")
        normalized = tuple(normalize_calendar_event(item) for item in raw_events)
        return CalendarFetchResult(
            mode=mode,
            raw_events=tuple(raw_events),
            events=normalized,
            next_sync_token=next_sync_token if coverage_complete else None,
            reset_required=False,
            coverage_complete=coverage_complete,
            pages_fetched=pages,
            continuation_page_token=page_token if not coverage_complete else None,
            api_requests=usage.api_requests,
            quota_units=usage.quota_units,
        )


class GmailThreadReader:
    """Bounded Gmail thread reader using history cursors after an initial window."""

    PROFILE_UNITS = 1
    HISTORY_LIST_UNITS = 2
    THREADS_LIST_UNITS = 10
    THREADS_GET_UNITS = 40

    def __init__(
        self,
        client: GoogleJSONClient,
        *,
        page_size: int = 100,
        history_page_size: int = 500,
        max_pages: int = 20,
        max_threads: int = 500,
        max_history_records: int = 5_000,
        message_body_cap: int = 30_000,
        thread_body_cap: int = 120_000,
        operator_emails: tuple[str, ...] = (),
    ) -> None:
        if not 1 <= page_size <= 500 or not 1 <= history_page_size <= 500:
            raise ValueError("Gmail page sizes must be between 1 and 500")
        if max_pages <= 0 or max_threads <= 0 or max_history_records <= 0:
            raise ValueError("Gmail fetch bounds must be positive")
        self.client = client
        self.page_size = page_size
        self.history_page_size = history_page_size
        self.max_pages = max_pages
        self.max_threads = max_threads
        self.max_history_records = max_history_records
        self.message_body_cap = message_body_cap
        self.thread_body_cap = thread_body_cap
        self.operator_emails = tuple(operator_emails)

    def fetch(
        self,
        *,
        query: str,
        history_id: str | None = None,
        continuation_page_token: str | None = None,
        baseline_history_id: str | None = None,
        pending_thread_ids: tuple[str, ...] = (),
        continuation_history_id: str | None = None,
    ) -> GmailFetchResult:
        normalized_query = query.strip()
        if not normalized_query or len(normalized_query) > 2_000:
            raise ValueError(
                "Gmail full-sync query must be non-empty and at most 2000 characters"
            )
        page_token = _validated_page_token(continuation_page_token)
        baseline = _validated_history_id(
            baseline_history_id, name="baseline_history_id"
        )
        starting_history_id = _validated_history_id(history_id, name="history_id")
        pending = _validated_pending_thread_ids(pending_thread_ids)
        history_checkpoint = _validated_history_id(
            continuation_history_id,
            name="continuation_history_id",
        )
        usage = _FetchUsage()
        if starting_history_id:
            if baseline is not None:
                raise ValueError(
                    "baseline_history_id is only valid for full-sync resume"
                )
            try:
                return self._fetch_incremental(
                    starting_history_id,
                    continuation_page_token=page_token,
                    pending_thread_ids=pending,
                    continuation_history_id=history_checkpoint,
                    usage=usage,
                )
            except GoogleAPIError as exc:
                if exc.status != 404:
                    raise
                replacement = self._fetch_full(
                    normalized_query,
                    continuation_page_token=None,
                    baseline_history_id=None,
                    usage=usage,
                )
                return replace(replacement, reset_required=True)
        if pending:
            raise ValueError("pending_thread_ids require an incremental history cursor")
        if history_checkpoint is not None:
            raise ValueError(
                "continuation_history_id requires an incremental history cursor"
            )
        if page_token is not None and baseline is None:
            raise ValueError(
                "baseline_history_id is required when resuming a full Gmail fetch"
            )
        if baseline is not None and page_token is None:
            raise ValueError(
                "baseline_history_id requires a full-fetch continuation_page_token"
            )
        return self._fetch_full(
            normalized_query,
            continuation_page_token=page_token,
            baseline_history_id=baseline,
            usage=usage,
        )

    def _fetch_full(
        self,
        query: str,
        *,
        continuation_page_token: str | None,
        baseline_history_id: str | None,
        usage: _FetchUsage,
    ) -> GmailFetchResult:
        if baseline_history_id is None:
            usage.add(self.PROFILE_UNITS)
            profile = self.client.get_json("profile", quota_units=self.PROFILE_UNITS)
            baseline_history_id = _optional_string(profile.get("historyId"))
            if not baseline_history_id:
                raise RuntimeError("Gmail profile did not provide historyId")
        thread_ids: list[str] = []
        seen: set[str] = set()
        page_token = continuation_page_token
        pages = 0
        coverage_complete = True
        while True:
            remaining = self.max_threads - len(thread_ids)
            if remaining <= 0 or pages >= self.max_pages:
                coverage_complete = False
                break
            params: dict[str, str | int] = {
                "maxResults": min(self.page_size, remaining),
                "q": query,
            }
            if page_token:
                params["pageToken"] = page_token
            usage.add(self.THREADS_LIST_UNITS)
            payload = self.client.get_json(
                "threads",
                params=params,
                quota_units=self.THREADS_LIST_UNITS,
            )
            pages += 1
            overflowed_page = False
            for item in payload.get("threads") or []:
                if not isinstance(item, Mapping):
                    continue
                thread_id = _optional_string(item.get("id"))
                if thread_id and thread_id not in seen:
                    if len(thread_ids) >= self.max_threads:
                        overflowed_page = True
                        coverage_complete = False
                        break
                    seen.add(thread_id)
                    thread_ids.append(thread_id)
            page_token = _optional_string(payload.get("nextPageToken"))
            if overflowed_page or not page_token:
                break
        if page_token:
            coverage_complete = False
        return self._fetch_thread_payloads(
            mode="full",
            thread_ids=thread_ids,
            next_history_id=baseline_history_id if coverage_complete else None,
            reset_required=False,
            coverage_complete=coverage_complete,
            pages=pages,
            continuation_page_token=page_token if not coverage_complete else None,
            baseline_history_id=baseline_history_id,
            pending_thread_ids=(),
            continuation_history_id=None,
            usage=usage,
        )

    def _fetch_incremental(
        self,
        history_id: str,
        *,
        continuation_page_token: str | None,
        pending_thread_ids: tuple[str, ...],
        continuation_history_id: str | None,
        usage: _FetchUsage,
    ) -> GmailFetchResult:
        if pending_thread_ids:
            selected = list(pending_thread_ids[: self.max_threads])
            remaining_pending = pending_thread_ids[self.max_threads :]
            coverage_complete = (
                not remaining_pending and continuation_page_token is None
            )
            if coverage_complete and continuation_history_id is None:
                raise ValueError(
                    "pending history completion requires continuation_history_id"
                )
            return self._fetch_thread_payloads(
                mode="incremental",
                thread_ids=selected,
                next_history_id=(
                    continuation_history_id if coverage_complete else None
                ),
                reset_required=False,
                coverage_complete=coverage_complete,
                pages=0,
                continuation_page_token=(
                    continuation_page_token if not coverage_complete else None
                ),
                baseline_history_id=None,
                pending_thread_ids=remaining_pending,
                continuation_history_id=(
                    continuation_history_id if not coverage_complete else None
                ),
                usage=usage,
            )
        changed_ids: list[str] = []
        seen: set[str] = set()
        page_token = continuation_page_token
        pages = 0
        records_seen = 0
        next_history_id: str | None = continuation_history_id
        coverage_complete = True
        while True:
            remaining = self.max_history_records - records_seen
            remaining_threads = self.max_threads - len(changed_ids)
            if remaining <= 0 or remaining_threads <= 0 or pages >= self.max_pages:
                coverage_complete = False
                break
            params: dict[str, str | int] = {
                "startHistoryId": history_id,
                "maxResults": min(
                    self.history_page_size,
                    remaining,
                    remaining_threads,
                ),
            }
            if page_token:
                params["pageToken"] = page_token
            usage.add(self.HISTORY_LIST_UNITS)
            payload = self.client.get_json(
                "history",
                params=params,
                quota_units=self.HISTORY_LIST_UNITS,
            )
            pages += 1
            records = [
                item
                for item in payload.get("history") or []
                if isinstance(item, Mapping)
            ]
            if len(records) > remaining:
                records = records[:remaining]
                coverage_complete = False
            records_seen += len(records)
            for record in records:
                for thread_id in _history_thread_ids(record):
                    if thread_id not in seen:
                        seen.add(thread_id)
                        changed_ids.append(thread_id)
            next_history_id = (
                _optional_string(payload.get("historyId")) or next_history_id
            )
            page_token = _optional_string(payload.get("nextPageToken"))
            if (
                not coverage_complete
                or not page_token
                or len(changed_ids) >= self.max_threads
            ):
                break
        if page_token:
            coverage_complete = False
        selected_ids = changed_ids[: self.max_threads]
        pending_ids = _validated_pending_thread_ids(
            tuple(changed_ids[self.max_threads :])
        )
        if pending_ids:
            coverage_complete = False
        if coverage_complete and not next_history_id:
            raise RuntimeError("Gmail completed history sync without historyId")
        return self._fetch_thread_payloads(
            mode="incremental",
            thread_ids=selected_ids,
            next_history_id=next_history_id if coverage_complete else None,
            reset_required=False,
            coverage_complete=coverage_complete,
            pages=pages,
            continuation_page_token=page_token if not coverage_complete else None,
            baseline_history_id=None,
            pending_thread_ids=pending_ids,
            continuation_history_id=(
                next_history_id if not coverage_complete else None
            ),
            usage=usage,
        )

    def _fetch_thread_payloads(
        self,
        *,
        mode: str,
        thread_ids: list[str],
        next_history_id: str | None,
        reset_required: bool,
        coverage_complete: bool,
        pages: int,
        continuation_page_token: str | None,
        baseline_history_id: str | None,
        pending_thread_ids: tuple[str, ...],
        continuation_history_id: str | None,
        usage: _FetchUsage,
    ) -> GmailFetchResult:
        raw_threads: list[dict[str, Any]] = []
        missing: list[str] = []
        for thread_id in thread_ids:
            safe_id = urllib.parse.quote(thread_id, safe="")
            try:
                usage.add(self.THREADS_GET_UNITS)
                payload = self.client.get_json(
                    f"threads/{safe_id}",
                    params={"format": "full"},
                    quota_units=self.THREADS_GET_UNITS,
                )
            except GoogleAPIError as exc:
                if exc.status == 404:
                    missing.append(thread_id)
                    continue
                raise
            raw_threads.append(sanitize_gmail_thread_payload(payload))
        normalized = tuple(
            normalize_gmail_thread(
                payload,
                message_body_cap=self.message_body_cap,
                thread_body_cap=self.thread_body_cap,
                operator_emails=self.operator_emails,
            )
            for payload in raw_threads
        )
        return GmailFetchResult(
            mode=mode,
            raw_threads=tuple(raw_threads),
            threads=normalized,
            changed_thread_ids=tuple(thread_ids),
            missing_thread_ids=tuple(missing),
            next_history_id=next_history_id if coverage_complete else None,
            reset_required=reset_required,
            coverage_complete=coverage_complete,
            pages_fetched=pages,
            continuation_page_token=continuation_page_token,
            baseline_history_id=baseline_history_id,
            pending_thread_ids=pending_thread_ids,
            continuation_history_id=continuation_history_id,
            api_requests=usage.api_requests,
            quota_units=usage.quota_units,
        )


def _history_thread_ids(record: Mapping[str, Any]) -> list[str]:
    output: list[str] = []
    for field in ("messagesAdded", "messagesDeleted", "labelsAdded", "labelsRemoved"):
        for change in record.get(field) or []:
            if not isinstance(change, Mapping):
                continue
            message = change.get("message")
            if isinstance(message, Mapping):
                thread_id = _optional_string(message.get("threadId"))
                if thread_id:
                    output.append(thread_id)
    if output:
        return output
    for message in record.get("messages") or []:
        if isinstance(message, Mapping):
            thread_id = _optional_string(message.get("threadId"))
            if thread_id:
                output.append(thread_id)
    return output


def _bounded_window(time_min: str, time_max: str) -> None:
    if not time_min.strip() or not time_max.strip():
        raise ValueError("Calendar full/reset fetch requires time_min and time_max")
    if len(time_min) > 100 or len(time_max) > 100:
        raise ValueError("Calendar window values are too long")


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _validated_page_token(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise ValueError("continuation_page_token must not be blank")
    if len(normalized) > 8_192:
        raise ValueError("continuation_page_token is too long")
    return normalized


def _validated_history_id(value: str | None, *, name: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be blank")
    if len(normalized) > 256:
        raise ValueError(f"{name} is too long")
    return normalized


def _validated_pending_thread_ids(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise ValueError("pending_thread_ids must be a tuple")
    if len(values) > MAX_PENDING_THREAD_IDS:
        raise ValueError("pending_thread_ids exceeds the durable continuation bound")
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("pending_thread_ids must contain strings")
        normalized = value.strip()
        if not normalized or len(normalized) > 256:
            raise ValueError("pending_thread_ids contains an invalid thread id")
        if normalized not in seen:
            output.append(normalized)
            seen.add(normalized)
    encoded = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_PENDING_THREAD_IDS_BYTES:
        raise ValueError("pending_thread_ids exceeds the durable continuation bound")
    return tuple(output)
