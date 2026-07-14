from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from .connector_auth import (
    CredentialStore,
    OAUTH_PROVIDERS,
    credential_store_for,
    load_auth_config,
    save_auth_config,
)
from .paths import BrainPaths
from .util import now_iso


GOOGLE_API_ROOTS = {
    "calendar": "https://www.googleapis.com/calendar/v3",
    "gmail": "https://gmail.googleapis.com/gmail/v1/users/me",
}
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
RETRYABLE_403_REASONS = {
    "backendError",
    "dailyLimitExceeded",
    "quotaExceeded",
    "rateLimitExceeded",
    "userRateLimitExceeded",
}


@dataclass(frozen=True)
class GoogleHTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


HTTPRequester = Callable[[urllib.request.Request, float], GoogleHTTPResponse]


class AccessTokenProvider(Protocol):
    def access_token(self, *, force_refresh: bool = False) -> str:
        ...


class GoogleAPIError(RuntimeError):
    def __init__(
        self,
        status: int,
        message: str,
        *,
        reason: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.retryable = retryable


class GoogleQuotaError(RuntimeError):
    pass


def perform_http_request(
    request: urllib.request.Request,
    timeout: float,
) -> GoogleHTTPResponse:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return GoogleHTTPResponse(
                status=int(response.status),
                headers=dict(response.headers.items()),
                body=response.read(),
            )
    except urllib.error.HTTPError as exc:
        return GoogleHTTPResponse(
            status=int(exc.code),
            headers=dict(exc.headers.items()) if exc.headers else {},
            body=exc.read(),
        )


class GoogleQuotaBudget:
    """Serialize requests behind explicit request-rate and quota-unit ceilings."""

    def __init__(
        self,
        *,
        units_per_minute: int = 6_000,
        requests_per_second: float = 2.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if units_per_minute <= 0:
            raise ValueError("units_per_minute must be positive")
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        self.units_per_minute = units_per_minute
        self.request_interval = 1.0 / requests_per_second
        self._monotonic = monotonic
        self._sleep = sleeper
        self._events: deque[tuple[float, int]] = deque()
        self._next_request_at = 0.0
        self._lock = threading.Lock()

    def acquire(self, units: int) -> None:
        if units <= 0:
            raise ValueError("quota units must be positive")
        if units > self.units_per_minute:
            raise GoogleQuotaError(
                f"one request requires {units} quota units; budget is "
                f"{self.units_per_minute}/minute"
            )
        with self._lock:
            while True:
                now = self._monotonic()
                self._discard_expired(now)
                used = sum(event_units for _, event_units in self._events)
                quota_wait = 0.0
                if used + units > self.units_per_minute and self._events:
                    quota_wait = max(0.0, 60.0 - (now - self._events[0][0]))
                rate_wait = max(0.0, self._next_request_at - now)
                wait_for = max(quota_wait, rate_wait)
                if wait_for <= 0:
                    timestamp = self._monotonic()
                    self._events.append((timestamp, units))
                    self._next_request_at = timestamp + self.request_interval
                    return
                self._sleep(wait_for)

    def _discard_expired(self, now: float) -> None:
        while self._events and now - self._events[0][0] >= 60.0:
            self._events.popleft()


class GoogleTokenManager:
    """Load and refresh one Brain-home-scoped Google connector grant."""

    def __init__(
        self,
        paths: BrainPaths,
        connector_id: str,
        *,
        store: CredentialStore | None = None,
        requester: HTTPRequester = perform_http_request,
        timeout_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        provider = OAUTH_PROVIDERS.get(connector_id)
        if connector_id not in GOOGLE_API_ROOTS or provider is None:
            raise ValueError(f"unsupported read-only Google connector: {connector_id}")
        if provider.phase != "read_only":
            raise ValueError(f"Google connector grant is not read-only: {connector_id}")
        self.paths = paths
        self.connector_id = connector_id
        self.provider = provider
        self.store = store or credential_store_for(paths)
        self.requester = requester
        self.timeout_seconds = timeout_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    def access_token(self, *, force_refresh: bool = False) -> str:
        with self._lock:
            config = load_auth_config(self.paths)
            state = config["connectors"].get(self.connector_id) or {}
            client_id = str(state.get("client_id") or "").strip()
            if not client_id:
                raise RuntimeError(
                    f"{self.connector_id} OAuth client is not configured"
                )
            granted = {str(value) for value in state.get("granted_scopes") or []}
            required = set(self.provider.scopes)
            if not required.issubset(granted):
                raise RuntimeError(
                    f"{self.connector_id} read-only grant requires reauthorization"
                )
            credentials = self.store.load(self.connector_id) or {}
            access_token = str(credentials.get("access_token") or "").strip()
            if (
                not force_refresh
                and access_token
                and not self._expires_soon(credentials.get("expires_at"))
            ):
                return access_token
            return self._refresh(
                config=config,
                state=state,
                client_id=client_id,
                credentials=credentials,
            )

    def _expires_soon(self, value: Any) -> bool:
        if not value:
            return True
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return True
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed <= self.clock() + timedelta(seconds=60)

    def _refresh(
        self,
        *,
        config: dict[str, Any],
        state: dict[str, Any],
        client_id: str,
        credentials: dict[str, Any],
    ) -> str:
        refresh_token = str(credentials.get("refresh_token") or "").strip()
        if not refresh_token:
            raise RuntimeError(
                f"{self.connector_id} read-only grant has no refresh token"
            )
        form = {
            "client_id": client_id,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        client_secret = str(credentials.get("client_secret") or "").strip()
        if client_secret:
            form["client_secret"] = client_secret
        request = urllib.request.Request(
            self.provider.token_endpoint,
            data=urllib.parse.urlencode(form).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "PKM-Brain/google-read-only",
            },
            method="POST",
        )
        response = self.requester(request, self.timeout_seconds)
        payload = _json_object(response.body)
        if response.status < 200 or response.status >= 300:
            _, message = _google_error_fields(payload, response.status)
            raise RuntimeError(
                f"{self.connector_id} token refresh failed: {message}"
            )
        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise RuntimeError(
                f"{self.connector_id} token refresh returned no access token"
            )
        updated = dict(credentials)
        updated["access_token"] = token
        if payload.get("token_type"):
            updated["token_type"] = str(payload["token_type"])
        if payload.get("scope"):
            updated["scope"] = str(payload["scope"])
            refreshed_scopes = set(str(payload["scope"]).replace(",", " ").split())
            if not set(self.provider.scopes).issubset(refreshed_scopes):
                raise RuntimeError(
                    f"{self.connector_id} token refresh dropped required read-only scopes"
                )
            state["granted_scopes"] = sorted(refreshed_scopes)
        try:
            expires_in = int(payload.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        expires_at = self.clock() + timedelta(seconds=max(1, expires_in))
        updated["expires_at"] = expires_at.replace(microsecond=0).isoformat()
        self.store.save(self.connector_id, updated)
        state.update(
            {
                "status": "connected",
                "last_error": None,
                "updated_at": now_iso(),
            }
        )
        save_auth_config(self.paths, config)
        return token


class GoogleAPIClient:
    """Allowlisted Google JSON GET client; it exposes no mutation method."""

    def __init__(
        self,
        connector_id: str,
        tokens: AccessTokenProvider,
        *,
        requester: HTTPRequester = perform_http_request,
        quota: GoogleQuotaBudget | None = None,
        timeout_seconds: float = 30.0,
        attempts: int = 5,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if connector_id not in GOOGLE_API_ROOTS:
            raise ValueError(f"unsupported Google API connector: {connector_id}")
        if attempts <= 0:
            raise ValueError("attempts must be positive")
        self.connector_id = connector_id
        self.api_root = GOOGLE_API_ROOTS[connector_id]
        self.tokens = tokens
        self.requester = requester
        self.quota = quota or GoogleQuotaBudget()
        self.timeout_seconds = timeout_seconds
        self.attempts = attempts
        self.sleeper = sleeper
        self.jitter = jitter

    def get_json(
        self,
        relative_path: str,
        *,
        params: Mapping[str, str | int | Sequence[str]] | None = None,
        quota_units: int = 1,
    ) -> dict[str, Any]:
        safe_path = _safe_relative_path(relative_path)
        query = urllib.parse.urlencode(params or {}, doseq=True)
        url = f"{self.api_root}/{safe_path}"
        if query:
            url = f"{url}?{query}"
        refreshed_after_unauthorized = False
        force_refresh_next = False
        last_transport_error: Exception | None = None
        for attempt in range(self.attempts):
            self.quota.acquire(quota_units)
            token = self.tokens.access_token(force_refresh=force_refresh_next)
            force_refresh_next = False
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                    "User-Agent": "PKM-Brain/google-read-only",
                },
                method="GET",
            )
            try:
                response = self.requester(request, self.timeout_seconds)
            except (TimeoutError, urllib.error.URLError, OSError) as exc:
                last_transport_error = exc
                if attempt + 1 >= self.attempts:
                    break
                self.sleeper(self._backoff_seconds(attempt, {}))
                continue
            payload = _json_object(response.body)
            if 200 <= response.status < 300:
                return payload
            reason, message = _google_error_fields(payload, response.status)
            if response.status == 401 and not refreshed_after_unauthorized:
                refreshed_after_unauthorized = True
                force_refresh_next = True
                continue
            retryable = _retryable(response.status, reason)
            if retryable and attempt + 1 < self.attempts:
                self.sleeper(self._backoff_seconds(attempt, response.headers))
                continue
            raise GoogleAPIError(
                response.status,
                message,
                reason=reason,
                retryable=retryable,
            )
        raise RuntimeError(
            f"{self.connector_id} API could not be reached after {self.attempts} attempts"
        ) from last_transport_error

    def _backoff_seconds(
        self,
        attempt: int,
        headers: Mapping[str, str],
    ) -> float:
        retry_after = _retry_after_seconds(headers)
        if retry_after is not None:
            return min(64.0, max(0.0, retry_after))
        return min(64.0, float(2**attempt) + min(1.0, max(0.0, self.jitter())))


def _safe_relative_path(value: str) -> str:
    normalized = value.strip().lstrip("/")
    if not normalized or "://" in normalized or "?" in normalized:
        raise ValueError("Google API path must be a non-empty relative path")
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ValueError("Google API path contains an unsafe segment")
    return normalized


def _json_object(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Google API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Google API returned a non-object JSON response")
    return payload


def _google_error_fields(
    payload: Mapping[str, Any],
    status: int,
) -> tuple[str | None, str]:
    error = payload.get("error")
    reason: str | None = None
    message = f"Google API request failed with HTTP {status}"
    if isinstance(error, str) and error:
        message = error[:500]
    elif isinstance(error, Mapping):
        if error.get("message"):
            message = str(error["message"])[:500]
        errors = error.get("errors")
        if isinstance(errors, list):
            for item in errors:
                if isinstance(item, Mapping) and item.get("reason"):
                    reason = str(item["reason"])
                    break
        if reason is None and error.get("status"):
            reason = str(error["status"])
    return reason, message


def _retryable(status: int, reason: str | None) -> bool:
    if status in RETRYABLE_STATUS_CODES:
        return True
    return status == 403 and reason in RETRYABLE_403_REASONS


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    raw = next(
        (value for key, value in headers.items() if key.casefold() == "retry-after"),
        None,
    )
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
