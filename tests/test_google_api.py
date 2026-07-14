from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pkm_brain.connector_auth import (
    MemoryCredentialStore,
    OAUTH_PROVIDERS,
    configure_connector_auth,
    load_auth_config,
    save_auth_config,
)
from pkm_brain.google_api import (
    GoogleAPIClient,
    GoogleAPIError,
    GoogleHTTPResponse,
    GoogleQuotaBudget,
    GoogleTokenManager,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


def response(status: int, payload: dict[str, object], **headers: str) -> GoogleHTTPResponse:
    return GoogleHTTPResponse(
        status=status,
        headers=headers,
        body=json.dumps(payload).encode("utf-8"),
    )


class Tokens:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    def access_token(self, *, force_refresh: bool = False) -> str:
        self.calls.append(force_refresh)
        return "refreshed" if force_refresh else "initial"


def configured_google_grant(
    tmp_path: Path,
    connector_id: str,
) -> tuple[BrainPaths, MemoryCredentialStore]:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    store = MemoryCredentialStore()
    configure_connector_auth(
        paths,
        connector_id,
        client_id="desktop-client",
        client_secret="desktop-secret",
        store=store,
    )
    config = load_auth_config(paths)
    config["connectors"][connector_id]["granted_scopes"] = list(
        OAUTH_PROVIDERS[connector_id].scopes
    )
    save_auth_config(paths, config)
    return paths, store


def test_token_manager_refreshes_and_persists_home_scoped_grant(tmp_path: Path) -> None:
    paths, store = configured_google_grant(tmp_path, "calendar")
    store.save(
        "calendar",
        {
            "client_secret": "desktop-secret",
            "refresh_token": "refresh-secret",
            "access_token": "expired-secret",
            "expires_at": "2026-07-13T08:00:00+00:00",
        },
    )
    requests = []

    def requester(request, _timeout):
        requests.append(request)
        return response(
            200,
            {
                "access_token": "new-secret",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )

    manager = GoogleTokenManager(
        paths,
        "calendar",
        store=store,
        requester=requester,
        clock=lambda: datetime(2026, 7, 13, 12, tzinfo=timezone.utc),
    )

    assert manager.access_token() == "new-secret"
    saved = store.load("calendar")
    assert saved is not None
    assert saved["refresh_token"] == "refresh-secret"
    assert saved["access_token"] == "new-secret"
    assert saved["expires_at"] == "2026-07-13T13:00:00+00:00"
    assert requests[0].get_method() == "POST"
    form = urllib.parse.parse_qs(requests[0].data.decode("utf-8"))
    assert form == {
        "client_id": ["desktop-client"],
        "client_secret": ["desktop-secret"],
        "grant_type": ["refresh_token"],
        "refresh_token": ["refresh-secret"],
    }


def test_token_manager_refuses_identity_only_or_stale_scope_state(tmp_path: Path) -> None:
    paths, store = configured_google_grant(tmp_path, "gmail")
    store.save("gmail", {"access_token": "secret", "refresh_token": "refresh"})
    config = load_auth_config(paths)
    config["connectors"]["gmail"]["granted_scopes"] = ["openid", "email", "profile"]
    save_auth_config(paths, config)

    manager = GoogleTokenManager(paths, "gmail", store=store)

    with pytest.raises(RuntimeError, match="requires reauthorization"):
        manager.access_token()


def test_get_client_retries_quota_error_and_uses_get_only() -> None:
    tokens = Tokens()
    requests = []
    replies = [
        response(
            429,
            {"error": {"message": "slow down", "status": "RESOURCE_EXHAUSTED"}},
            **{"Retry-After": "3"},
        ),
        response(200, {"ok": True}),
    ]
    sleeps: list[float] = []

    def requester(request, _timeout):
        requests.append(request)
        return replies.pop(0)

    client = GoogleAPIClient(
        "gmail",
        tokens,
        requester=requester,
        quota=GoogleQuotaBudget(requests_per_second=1_000),
        sleeper=sleeps.append,
        jitter=lambda: 0,
    )

    assert client.get_json("threads/abc", params={"format": "full"}, quota_units=40) == {
        "ok": True
    }
    assert sleeps == [3.0]
    assert [request.get_method() for request in requests] == ["GET", "GET"]
    assert all(request.headers["Authorization"] == "Bearer initial" for request in requests)
    assert requests[-1].full_url.endswith("/threads/abc?format=full")


def test_get_client_refreshes_once_after_401() -> None:
    tokens = Tokens()
    replies = [response(401, {"error": {"message": "expired"}}), response(200, {"ok": 1})]

    client = GoogleAPIClient(
        "calendar",
        tokens,
        requester=lambda _request, _timeout: replies.pop(0),
        quota=GoogleQuotaBudget(requests_per_second=1_000),
    )

    assert client.get_json("calendars/primary/events") == {"ok": 1}
    assert tokens.calls == [False, True]


def test_get_client_does_not_retry_permission_denial() -> None:
    calls = 0

    def requester(_request, _timeout):
        nonlocal calls
        calls += 1
        return response(
            403,
            {
                "error": {
                    "message": "forbidden",
                    "errors": [{"reason": "insufficientPermissions"}],
                }
            },
        )

    client = GoogleAPIClient(
        "calendar",
        Tokens(),
        requester=requester,
        quota=GoogleQuotaBudget(requests_per_second=1_000),
    )

    with pytest.raises(GoogleAPIError) as raised:
        client.get_json("calendars/primary/events")
    assert raised.value.status == 403
    assert raised.value.retryable is False
    assert calls == 1


def test_get_client_rejects_arbitrary_urls_and_parent_segments() -> None:
    client = GoogleAPIClient("gmail", Tokens())

    for value in ("https://example.com/steal", "threads/../profile", "threads?id=x"):
        with pytest.raises(ValueError):
            client.get_json(value)


def test_quota_budget_waits_for_rate_and_minute_capacity() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    budget = GoogleQuotaBudget(
        units_per_minute=60,
        requests_per_second=2,
        monotonic=lambda: now[0],
        sleeper=sleep,
    )

    budget.acquire(40)
    budget.acquire(20)
    budget.acquire(1)

    assert sleeps == [0.5, 59.5]
    assert now[0] == 60.0


def test_unexpired_token_is_reused_without_network(tmp_path: Path) -> None:
    paths, store = configured_google_grant(tmp_path, "gmail")
    store.save(
        "gmail",
        {
            "access_token": "still-valid",
            "refresh_token": "refresh-secret",
            "expires_at": (
                datetime(2026, 7, 13, 14, tzinfo=timezone.utc) + timedelta(hours=1)
            ).isoformat(),
        },
    )
    manager = GoogleTokenManager(
        paths,
        "gmail",
        store=store,
        requester=lambda _request, _timeout: pytest.fail("unexpected refresh"),
        clock=lambda: datetime(2026, 7, 13, 14, tzinfo=timezone.utc),
    )

    assert manager.access_token() == "still-valid"
