from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pkm_brain.gmail_archive import ArchiveState, gmail_archive_identity_fingerprint
from pkm_brain.gmail_archive_source import (
    GmailArchiveRawMessage,
    GmailArchiveSourceBatch,
    GmailArchiveSourceFailure,
    GmailHistoryExpired,
)
from pkm_brain.gmail_archive_sync import (
    GMAIL_ARCHIVE_MIN_FREE_BYTES,
    GmailArchiveSynchronizer,
    _query_from_window_start,
    _require_archive_policy,
    run_scheduled_gmail_archive_sync,
)
from pkm_brain.google_api import GoogleAPIError
from pkm_brain.operational_service import OperationalService
from pkm_brain.operations_policy import OperationsPolicy
from pkm_brain.paths import BrainPaths
from pkm_brain.shadow_setup import default_operations_policy_payload


NOW = datetime(2026, 7, 14, 18, 0, tzinfo=timezone.utc)
IDENTITY_FINGERPRINT = gmail_archive_identity_fingerprint(
    "owner@example.com", "gmail-subject"
)


def _policy() -> OperationsPolicy:
    return OperationsPolicy.from_dict(
        default_operations_policy_payload(
            timezone_name="America/Los_Angeles",
            calendar_email="owner@example.com",
            gmail_email="owner@example.com",
            calendar_provider_subject="calendar-subject",
            gmail_provider_subject="gmail-subject",
        )
    )


def _message(message_id: str = "m1") -> GmailArchiveRawMessage:
    return GmailArchiveRawMessage(
        message_id=message_id,
        thread_id="thread-1",
        history_id="101",
        internal_date="1710000000000",
        label_ids=("INBOX",),
        raw=b"Subject: test\r\n\r\nbody",
    )


class FakeStore:
    def __init__(self, state: Any | None = None) -> None:
        self.state = state
        self.events: list[str] = []
        self.messages: dict[str, Any] = {}

    def initialize(self) -> None:
        self.events.append("initialize")

    def provision_key(self) -> None:
        self.events.append("key")

    def get_state(self, account_key: str) -> Any | None:
        del account_key
        return self.state

    def apply_batch(
        self,
        account_key: str,
        *,
        messages: tuple[Any, ...] = (),
        deleted_message_ids: tuple[str, ...] = (),
        state: Any,
    ) -> Any:
        del account_key
        self.events.append("apply")
        inserted = 0
        updated = 0
        for message in messages:
            if message.message_id in self.messages:
                updated += 1
            else:
                inserted += 1
            self.messages[message.message_id] = message
        self.state = state
        return SimpleNamespace(
            inserted=inserted,
            updated=updated,
            deleted=len(deleted_message_ids),
            state=state,
        )

    def status(self, account_key: str) -> dict[str, Any]:
        del account_key
        return {
            "message_count": len(self.messages),
            "phase": getattr(self.state, "phase", "not_started"),
            "processed": getattr(self.state, "processed", 0),
            "secret": "not returned",
        }


class FakeReader:
    def __init__(self, *responses: Any, baseline: str = "100") -> None:
        self.responses = list(responses)
        self.baseline = baseline
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def capture_history_id(self) -> tuple[str, int, int]:
        self.calls.append(("profile", {}))
        return self.baseline, 1, 1

    def backfill_page(self, query: str, *, page_token: str | None = None) -> Any:
        self.calls.append(("backfill", {"query": query, "page_token": page_token}))
        return self._next()

    def history_page(self, history_id: str, **kwargs: Any) -> Any:
        self.calls.append(("history", {"history_id": history_id, **kwargs}))
        return self._next()

    def _next(self) -> Any:
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _sync(
    tmp_path: Path,
    store: FakeStore,
    reader: FakeReader,
    *,
    free_bytes: int = GMAIL_ARCHIVE_MIN_FREE_BYTES,
    used: int = 0,
) -> GmailArchiveSynchronizer:
    paths = BrainPaths.from_value(tmp_path / "brain")
    return GmailArchiveSynchronizer(
        paths,
        OperationalService(paths),
        store=store,
        reader=reader,
        now=lambda: NOW,
        usage_reader=lambda *args, **kwargs: {"gmail": {"api_requests": used}},
        free_bytes_reader=lambda path: free_bytes,
    )


def test_initial_pass_captures_h0_then_resumes_one_fixed_backfill(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    reader = FakeReader(
        GmailArchiveSourceBatch(
            messages=(_message(),),
            next_page_token="page-2",
            result_size_estimate=10,
            api_requests=2,
            quota_units=25,
        )
    )

    outcome = _sync(tmp_path, store, reader).sync(policy=_policy())

    assert [name for name, _ in reader.calls] == ["profile", "backfill"]
    assert store.events[:2] == ["initialize", "key"]
    assert store.state.phase == "backfill"
    assert store.state.baseline_history_id == "100"
    assert store.state.page_token == "page-2"
    assert store.state.processed == 1
    assert store.state.estimate == 10
    assert store.state.query.startswith("after:") and "before:" in store.state.query
    assert store.state.identity_fingerprint == IDENTITY_FINGERPRINT
    assert outcome.fetched == 1
    assert outcome.api_requests == 3
    assert outcome.as_dict()["message"] == "Copying Gmail history: 1 message stored."


def test_final_backfill_transitions_to_live_then_history_becomes_current(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    reader = FakeReader(
        GmailArchiveSourceBatch(messages=(), next_page_token=None, api_requests=1),
        GmailArchiveSourceBatch(
            messages=(_message("m2"),),
            next_history_id="110",
            continuation_history_id="110",
            api_requests=2,
            quota_units=22,
        ),
    )
    synchronizer = _sync(tmp_path, store, reader)

    first = synchronizer.sync(policy=_policy())
    second = synchronizer.sync(policy=_policy())

    assert first.phase == "live" and first.coverage_complete is False
    assert reader.calls[-1][0] == "history"
    assert reader.calls[-1][1]["history_id"] == "100"
    assert second.phase == "live" and second.coverage_complete is True
    assert store.state.history_id == "110"
    assert store.state.last_success_at == NOW.isoformat()


def test_expired_history_restarts_from_original_lower_bound(tmp_path: Path) -> None:
    state = ArchiveState(
        account_key="gmail.primary",
        phase="live",
        query="after:100",
        window_start="2026-04-15T18:00:00+00:00",
        window_end=NOW.isoformat(),
        updated_at=NOW.isoformat(),
        history_id="100",
        baseline_history_id="100",
        coverage_complete=True,
        identity_fingerprint=IDENTITY_FINGERPRINT,
    )
    store = FakeStore(state)
    reader = FakeReader(GmailHistoryExpired("expired"), baseline="200")

    outcome = _sync(tmp_path, store, reader).sync(policy=_policy())

    assert outcome.stopped_reason == "history_rescan"
    assert store.state.phase == "backfill"
    assert store.state.history_id == "200"
    assert store.state.baseline_history_id == "200"
    assert store.state.query == _query_from_window_start(state.window_start)
    assert store.state.processed == 0
    assert store.state.reset_required is True


@pytest.mark.parametrize("identity_fingerprint", [None, "0" * 64])
def test_existing_archive_identity_must_match_policy_before_provider_access(
    tmp_path: Path,
    identity_fingerprint: str | None,
) -> None:
    state = ArchiveState(
        account_key="gmail.primary",
        phase="backfill",
        query="after:100 before:200",
        window_start="2026-04-15T18:00:00+00:00",
        window_end=NOW.isoformat(),
        updated_at=NOW.isoformat(),
        identity_fingerprint=identity_fingerprint,
    )
    store = FakeStore(state)
    reader = FakeReader(GmailArchiveSourceBatch(messages=()))

    with pytest.raises(ValueError, match="identity"):
        _sync(tmp_path, store, reader).sync(policy=_policy())

    assert reader.calls == []
    assert store.state is state


def test_malformed_message_pauses_without_advancing_or_claiming_complete(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    reader = FakeReader(
        GmailArchiveSourceBatch(
            messages=(_message("good-peer"),),
            failures=(GmailArchiveSourceFailure("bad", "malformed_raw_message"),),
            api_requests=3,
            quota_units=45,
        )
    )

    outcome = _sync(tmp_path, store, reader).sync(policy=_policy())
    payload = outcome.as_dict()

    assert outcome.stopped_reason == "malformed_message"
    assert outcome.skipped == 1
    assert store.messages == {}
    assert store.state.processed == 0
    assert payload["status"] == "partial"
    assert "could not be read safely" in payload["message"]


@pytest.mark.parametrize(
    ("free_bytes", "used", "reason"),
    [
        (GMAIL_ARCHIVE_MIN_FREE_BYTES - 1, 0, "low_disk_space"),
        (GMAIL_ARCHIVE_MIN_FREE_BYTES, 9_500, "daily_budget_headroom"),
    ],
)
def test_resource_gates_stop_before_any_provider_fetch(
    tmp_path: Path, free_bytes: int, used: int, reason: str
) -> None:
    store = FakeStore()
    reader = FakeReader()

    outcome = _sync(tmp_path, store, reader, free_bytes=free_bytes, used=used).sync(
        policy=_policy()
    )

    assert outcome.stopped_reason == reason
    assert reader.calls == []
    assert store.events == ["initialize", "key"]


def test_archive_policy_gate_is_explicit() -> None:
    policy = _policy()
    _require_archive_policy(policy)
    disabled = OperationsPolicy.from_dict(
        {
            **default_operations_policy_payload(
                timezone_name="America/Los_Angeles",
                calendar_email="owner@example.com",
                gmail_email="owner@example.com",
            ),
            "sources": {
                **default_operations_policy_payload(
                    timezone_name="America/Los_Angeles",
                    calendar_email="owner@example.com",
                    gmail_email="owner@example.com",
                )["sources"],
                "gmail": {
                    **default_operations_policy_payload(
                        timezone_name="America/Los_Angeles",
                        calendar_email="owner@example.com",
                        gmail_email="owner@example.com",
                    )["sources"]["gmail"],
                    "archive": {
                        "enabled": False,
                        "initial_days": 90,
                        "agent_access_approved": False,
                    },
                },
            },
        }
    )
    with pytest.raises(ValueError):
        _require_archive_policy(disabled)


def test_scheduled_failure_reports_only_a_safe_error_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SensitiveProviderFailure(Exception):
        pass

    class FailingSynchronizer:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        def sync(self, *, policy: OperationsPolicy) -> Any:
            del policy
            raise SensitiveProviderFailure(
                "private Gmail subject and message body must not escape"
            )

    paths = BrainPaths.from_value(tmp_path / "brain")
    operational_service = OperationalService(paths)
    monkeypatch.setattr(operational_service, "initialize", lambda: None)
    monkeypatch.setattr(
        "pkm_brain.gmail_archive_sync.load_operations_policy", lambda paths: _policy()
    )
    monkeypatch.setattr(
        "pkm_brain.gmail_archive_sync.connector_auth_status",
        lambda paths, connector: {"status": "connected"},
    )
    monkeypatch.setattr(
        "pkm_brain.gmail_archive_sync.GmailArchiveSynchronizer",
        FailingSynchronizer,
    )

    payload = run_scheduled_gmail_archive_sync(paths, operational_service)

    assert payload == {
        "status": "failed",
        "message": "Secure Gmail history copy stopped safely; retry from the Brain app.",
        "error_code": "gmail_archive_SensitiveProviderFailure",
    }
    assert "subject" not in repr(payload)
    assert "message body" not in repr(payload)


def test_scheduled_provider_rate_limit_is_distinct_from_brain_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RateLimitedSynchronizer:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

        def sync(self, *, policy: OperationsPolicy) -> Any:
            del policy
            raise GoogleAPIError(
                429,
                "provider detail must stay private",
                reason="userRateLimitExceeded",
                retryable=True,
            )

    paths = BrainPaths.from_value(tmp_path / "brain")
    service = OperationalService(paths)
    monkeypatch.setattr(service, "initialize", lambda: None)
    monkeypatch.setattr(
        "pkm_brain.gmail_archive_sync.load_operations_policy", lambda paths: _policy()
    )
    monkeypatch.setattr(
        "pkm_brain.gmail_archive_sync.connector_auth_status",
        lambda paths, connector: {"status": "connected"},
    )
    monkeypatch.setattr(
        "pkm_brain.gmail_archive_sync.GmailArchiveSynchronizer",
        RateLimitedSynchronizer,
    )

    assert run_scheduled_gmail_archive_sync(paths, service) == {
        "status": "partial",
        "message": "Google asked Brain to slow down; the copy will retry.",
        "stopped_reason": "provider_rate_limited",
    }
