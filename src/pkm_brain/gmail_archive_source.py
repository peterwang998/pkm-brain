from __future__ import annotations

import base64
import binascii
import hashlib
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .google_api import GoogleAPIError


GMAIL_RAW_MESSAGE_PARSER_VERSION = "gmail-raw-v1"
MAX_RAW_MESSAGE_BYTES = 128 * 1024 * 1024
MAX_PENDING_IDS = 10_000


class GoogleJSONClient(Protocol):
    def get_json(
        self,
        relative_path: str,
        *,
        params: Mapping[str, str | int] | None = None,
        quota_units: int = 1,
    ) -> dict[str, Any]: ...


class GmailHistoryExpired(RuntimeError):
    """Gmail can no longer serve the saved history cursor."""


class GmailPageTokenExpired(RuntimeError):
    """A saved list token can be discarded and replayed."""


@dataclass(frozen=True)
class GmailArchiveRawMessage:
    message_id: str
    thread_id: str
    history_id: str | None
    internal_date: str | None
    label_ids: tuple[str, ...]
    raw: bytes

    @property
    def raw_sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()


@dataclass(frozen=True)
class GmailArchiveSourceFailure:
    message_id: str
    code: str


@dataclass(frozen=True)
class GmailArchiveSourceBatch:
    messages: tuple[GmailArchiveRawMessage, ...]
    missing_ids: tuple[str, ...] = ()
    failures: tuple[GmailArchiveSourceFailure, ...] = ()
    next_page_token: str | None = None
    next_history_id: str | None = None
    pending_ids: tuple[str, ...] = ()
    continuation_history_id: str | None = None
    result_size_estimate: int | None = None
    api_requests: int = 0
    quota_units: int = 0


class GmailArchiveReader:
    """Small Gmail RAW reader; durable state belongs to the archive store."""

    PROFILE_UNITS = 1
    LIST_UNITS = 5
    HISTORY_UNITS = 2
    GET_UNITS = 20

    def __init__(
        self,
        client: GoogleJSONClient,
        *,
        page_size: int = 250,
        max_raw_bytes: int = MAX_RAW_MESSAGE_BYTES,
    ) -> None:
        if not 1 <= page_size <= 500:
            raise ValueError("page_size must be between 1 and 500")
        if max_raw_bytes <= 0:
            raise ValueError("max_raw_bytes must be positive")
        self.client = client
        self.page_size = page_size
        self.max_raw_bytes = max_raw_bytes

    def capture_history_id(self) -> tuple[str, int, int]:
        payload = self.client.get_json("profile", quota_units=self.PROFILE_UNITS)
        history_id = _history_id(payload.get("historyId"))
        if history_id is None:
            raise ValueError("Gmail profile did not contain a historyId")
        return history_id, 1, self.PROFILE_UNITS

    def backfill_page(
        self,
        query: str,
        *,
        page_token: str | None = None,
    ) -> GmailArchiveSourceBatch:
        normalized_query = _required_text(query, "query", 2_000)
        token = _optional_text(page_token, "page_token", 8_192)
        params: dict[str, str | int] = {
            "q": normalized_query,
            "maxResults": self.page_size,
            "includeSpamTrash": "true",
        }
        if token:
            params["pageToken"] = token
        try:
            payload = self.client.get_json(
                "messages", params=params, quota_units=self.LIST_UNITS
            )
        except GoogleAPIError as exc:
            if token and exc.status == 400:
                raise GmailPageTokenExpired("saved Gmail list token expired") from exc
            raise
        ids = _message_ids(payload.get("messages"))
        fetched = self._fetch_messages(ids)
        return GmailArchiveSourceBatch(
            messages=fetched.messages,
            missing_ids=fetched.missing_ids,
            failures=fetched.failures,
            next_page_token=_optional_text(
                payload.get("nextPageToken"), "nextPageToken", 8_192
            ),
            result_size_estimate=_optional_nonnegative_int(
                payload.get("resultSizeEstimate")
            ),
            api_requests=1 + fetched.api_requests,
            quota_units=self.LIST_UNITS + fetched.quota_units,
        )

    def history_page(
        self,
        history_id: str,
        *,
        page_token: str | None = None,
        pending_ids: Sequence[str] = (),
        continuation_history_id: str | None = None,
    ) -> GmailArchiveSourceBatch:
        start = _history_id(history_id)
        if start is None:
            raise ValueError("history_id is required")
        token = _optional_text(page_token, "page_token", 8_192)
        continuation = _history_id(continuation_history_id)
        pending = _pending_ids(pending_ids)

        if pending:
            selected = pending[: self.page_size]
            remaining = pending[self.page_size :]
            fetched = self._fetch_messages(selected)
            complete = not remaining and token is None
            return GmailArchiveSourceBatch(
                messages=fetched.messages,
                missing_ids=fetched.missing_ids,
                failures=fetched.failures,
                next_page_token=token,
                next_history_id=continuation if complete else None,
                pending_ids=remaining,
                continuation_history_id=continuation,
                api_requests=fetched.api_requests,
                quota_units=fetched.quota_units,
            )

        params: dict[str, str | int] = {
            "startHistoryId": start,
            "maxResults": 500,
        }
        if token:
            params["pageToken"] = token
        try:
            payload = self.client.get_json(
                "history", params=params, quota_units=self.HISTORY_UNITS
            )
        except GoogleAPIError as exc:
            if exc.status == 404:
                raise GmailHistoryExpired("saved Gmail history cursor expired") from exc
            if token and exc.status == 400:
                raise GmailPageTokenExpired("saved Gmail history token expired") from exc
            raise

        changed = _history_message_ids(payload.get("history"))
        selected = changed[: self.page_size]
        remaining = changed[self.page_size :]
        next_token = _optional_text(
            payload.get("nextPageToken"), "nextPageToken", 8_192
        )
        final_cursor = _history_id(payload.get("historyId")) or continuation or start
        fetched = self._fetch_messages(selected)
        complete = not remaining and next_token is None
        return GmailArchiveSourceBatch(
            messages=fetched.messages,
            missing_ids=fetched.missing_ids,
            failures=fetched.failures,
            next_page_token=next_token,
            next_history_id=final_cursor if complete else None,
            pending_ids=remaining,
            continuation_history_id=final_cursor,
            api_requests=1 + fetched.api_requests,
            quota_units=self.HISTORY_UNITS + fetched.quota_units,
        )

    def _fetch_messages(self, ids: Sequence[str]) -> GmailArchiveSourceBatch:
        messages: list[GmailArchiveRawMessage] = []
        missing: list[str] = []
        failures: list[GmailArchiveSourceFailure] = []
        requests = 0
        for message_id in ids:
            requests += 1
            try:
                payload = self.client.get_json(
                    f"messages/{urllib.parse.quote(message_id, safe='')}",
                    params={"format": "raw"},
                    quota_units=self.GET_UNITS,
                )
            except GoogleAPIError as exc:
                if exc.status == 404:
                    missing.append(message_id)
                    continue
                raise
            try:
                messages.append(self._decode_message(payload, expected_id=message_id))
            except (TypeError, ValueError, binascii.Error):
                failures.append(
                    GmailArchiveSourceFailure(message_id, "malformed_raw_message")
                )
        return GmailArchiveSourceBatch(
            messages=tuple(messages),
            missing_ids=tuple(missing),
            failures=tuple(failures),
            api_requests=requests,
            quota_units=requests * self.GET_UNITS,
        )

    def _decode_message(
        self, payload: Mapping[str, Any], *, expected_id: str
    ) -> GmailArchiveRawMessage:
        message_id = _required_text(payload.get("id"), "message id", 2_000)
        if message_id != expected_id:
            raise ValueError("Gmail returned the wrong message")
        thread_id = _required_text(payload.get("threadId"), "thread id", 2_000)
        raw_value = _required_text(payload.get("raw"), "raw", 256 * 1024 * 1024)
        padding = "=" * (-len(raw_value) % 4)
        raw = base64.b64decode(raw_value + padding, altchars=b"-_", validate=True)
        if not raw or len(raw) > self.max_raw_bytes:
            raise ValueError("Gmail raw message size is invalid")
        labels_value = payload.get("labelIds") or []
        if not isinstance(labels_value, list):
            raise ValueError("Gmail labelIds is malformed")
        labels = tuple(
            _required_text(item, "label id", 256) for item in labels_value[:1_000]
        )
        return GmailArchiveRawMessage(
            message_id=message_id,
            thread_id=thread_id,
            history_id=_history_id(payload.get("historyId")),
            internal_date=_optional_text(
                payload.get("internalDate"), "internalDate", 64
            ),
            label_ids=labels,
            raw=raw,
        )


def _message_ids(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("Gmail messages list is malformed")
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("Gmail message reference is malformed")
        message_id = _required_text(item.get("id"), "message id", 2_000)
        if message_id not in seen:
            output.append(message_id)
            seen.add(message_id)
    return tuple(output)


def _history_message_ids(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("Gmail history is malformed")
    output: list[str] = []
    seen: set[str] = set()
    for record in value:
        if not isinstance(record, Mapping):
            raise ValueError("Gmail history record is malformed")
        candidates: list[Mapping[str, Any]] = []
        for key in ("messagesAdded", "messagesDeleted", "labelsAdded", "labelsRemoved"):
            changes = _history_array(record, key)
            for wrapper in changes:
                if not isinstance(wrapper, Mapping):
                    raise ValueError(f"Gmail {key} item is malformed")
                message = wrapper.get("message")
                if not isinstance(message, Mapping):
                    raise ValueError(f"Gmail {key} message is malformed")
                candidates.append(message)
        messages = _history_array(record, "messages")
        for message in messages:
            if not isinstance(message, Mapping):
                raise ValueError("Gmail history message is malformed")
            candidates.append(message)
        for item in candidates:
            message_id = _required_text(item.get("id"), "message id", 2_000)
            if message_id not in seen:
                output.append(message_id)
                seen.add(message_id)
    return tuple(output)


def _history_array(record: Mapping[str, Any], name: str) -> list[Any]:
    if name not in record:
        return []
    value = record[name]
    if not isinstance(value, list):
        raise ValueError(f"Gmail {name} list is malformed")
    return value


def _pending_ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or len(values) > MAX_PENDING_IDS:
        raise ValueError("pending Gmail message IDs are invalid")
    return tuple(_required_text(value, "pending message id", 2_000) for value in values)


def _history_id(value: Any) -> str | None:
    result = _optional_text(value, "history id", 128)
    if result is not None and (not result.isdigit() or int(result) <= 0):
        raise ValueError("Gmail history id is invalid")
    return result


def _required_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} is invalid")
    return value.strip()


def _optional_text(value: Any, name: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _required_text(value, name, maximum)


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Gmail resultSizeEstimate is invalid")
    return value
