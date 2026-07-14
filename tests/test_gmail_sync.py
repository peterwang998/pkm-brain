from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pkm_brain.gmail_sync as gmail_sync_module
import pkm_brain.gmail_mirror as gmail_mirror_module
import pytest

from pkm_brain.gmail_mirror import (
    GmailMirrorCheckpointUpdate,
    GmailMirrorQuarantineInput,
    GmailMirrorStore,
)
from pkm_brain.gmail_sync import (
    GMAIL_MIRROR_INITIAL_QUERY,
    GmailMirrorSynchronizer,
    _sync_thread_cap,
    run_scheduled_gmail_mirror_sync,
)
from pkm_brain.google_normalization import (
    NormalizedGmailMessage,
    NormalizedGmailThread,
)
from pkm_brain.google_api import GoogleAPIError
from pkm_brain.google_sources import (
    GMAIL_THREAD_PARSER_VERSION,
    GmailFetchResult,
    GmailThreadFailure,
)
from pkm_brain.operational_service import OperationalService
from pkm_brain.operational_budget import DailyBudgetExceeded
from pkm_brain.operations_policy import OperationsPolicy
from pkm_brain.paths import BrainPaths
from pkm_brain.shadow_setup import default_operations_policy_payload


NOW = datetime(2026, 7, 14, 18, 0, tzinfo=timezone.utc)


def test_sync_thread_cap_reserves_transport_overhead_near_daily_limit() -> None:
    assert _sync_thread_cap(1_200, used_requests=0) == 200
    assert _sync_thread_cap(1_200, used_requests=760) == 198
    with pytest.raises(DailyBudgetExceeded):
        _sync_thread_cap(1_200, used_requests=1_156)


class FakeReader:
    max_threads = 200

    def __init__(self, *results: GmailFetchResult | Exception) -> None:
        self.results = list(results)
        self.calls: list[dict[str, object]] = []

    def fetch(self, **kwargs: object) -> GmailFetchResult:
        self.calls.append(dict(kwargs))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_sync_commits_full_mirror_then_uses_history_incrementally(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    first_thread = _thread("thread-1", "revision-1")
    second_thread = _thread("thread-1", "revision-2")
    reader = FakeReader(
        _result(
            mode="full",
            history_id="history-1",
            thread=first_thread,
        ),
        _result(
            mode="incremental",
            history_id="history-2",
            thread=second_thread,
        ),
    )
    sync = GmailMirrorSynchronizer(
        paths,
        OperationalService(paths),
        reader=reader,
        now=lambda: NOW,
    )

    first = sync.sync(policy=_policy())
    second = sync.sync(policy=_policy())

    assert reader.calls[0]["query"] == GMAIL_MIRROR_INITIAL_QUERY
    assert reader.calls[0]["history_id"] is None
    assert reader.calls[1]["history_id"] == "history-1"
    assert second.checkpoint.history_id == "history-2"
    assert second.checkpoint.generation == first.checkpoint.generation + 1
    mirror = GmailMirrorStore(paths.gmail_mirror_sqlite_path)
    current = mirror.get_current_revision("gmail.primary", "thread-1")
    assert current is not None
    assert current.source_revision == "revision-2"
    assert mirror.triage_counts("gmail.primary")["backlog_count"] == 1


def test_partial_full_sync_resumes_from_durable_continuation_after_restart(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    partial = GmailFetchResult(
        mode="full",
        raw_threads=(),
        threads=(),
        changed_thread_ids=(),
        missing_thread_ids=(),
        next_history_id="history-baseline",
        reset_required=False,
        coverage_complete=False,
        pages_fetched=1,
        continuation_page_token="page-2",
        baseline_history_id="history-baseline",
    )
    complete = GmailFetchResult(
        mode="full",
        raw_threads=(),
        threads=(),
        changed_thread_ids=(),
        missing_thread_ids=(),
        next_history_id="history-baseline",
        reset_required=False,
        coverage_complete=True,
        pages_fetched=1,
    )
    first_reader = FakeReader(partial)
    GmailMirrorSynchronizer(
        paths,
        OperationalService(paths),
        reader=first_reader,
        now=lambda: NOW,
    ).sync(policy=_policy())

    second_reader = FakeReader(complete)
    outcome = GmailMirrorSynchronizer(
        paths,
        OperationalService(paths),
        reader=second_reader,
        now=lambda: NOW,
    ).sync(policy=_policy())

    assert second_reader.calls == [
        {
            "query": GMAIL_MIRROR_INITIAL_QUERY,
            "history_id": None,
            "continuation_page_token": "page-2",
            "baseline_history_id": "history-baseline",
            "pending_thread_ids": (),
            "continuation_history_id": None,
        }
    ]
    assert outcome.checkpoint.coverage_complete is True
    assert outcome.checkpoint.history_id == "history-baseline"


def test_explicit_missing_thread_becomes_a_durable_tombstone(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    original = _thread("thread-1", "revision-1")
    reader = FakeReader(
        _result(mode="full", history_id="history-1", thread=original),
        GmailFetchResult(
            mode="incremental",
            raw_threads=(),
            threads=(),
            changed_thread_ids=("thread-1",),
            missing_thread_ids=("thread-1",),
            next_history_id="history-2",
            reset_required=False,
            coverage_complete=True,
            pages_fetched=1,
        ),
    )
    sync = GmailMirrorSynchronizer(
        paths,
        OperationalService(paths),
        reader=reader,
        now=lambda: NOW,
    )
    sync.sync(policy=_policy())
    outcome = sync.sync(policy=_policy())

    current = GmailMirrorStore(paths.gmail_mirror_sqlite_path).get_current_revision(
        "gmail.primary",
        "thread-1",
    )
    assert current is not None and current.tombstoned is True
    assert outcome.mirror.tombstones == 1
    assert outcome.checkpoint.history_id == "history-2"


def test_sync_commits_valid_thread_and_durable_quarantine_in_same_checkpoint(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    good = _thread("thread-good", "revision-good")
    reader = FakeReader(
        GmailFetchResult(
            mode="full",
            raw_threads=({"id": "thread-good", "historyId": "revision-good"},),
            threads=(good,),
            changed_thread_ids=("thread-good", "thread-bad"),
            missing_thread_ids=(),
            next_history_id="history-complete",
            reset_required=False,
            coverage_complete=True,
            pages_fetched=1,
            quarantined_threads=(
                GmailThreadFailure(
                    thread_id="thread-bad",
                    source_revision="revision-bad",
                    stage="normalize",
                    error="ValueError: Gmail message is missing id or threadId",
                    payload_sha256="a" * 64,
                ),
            ),
        )
    )

    outcome = GmailMirrorSynchronizer(
        paths,
        OperationalService(paths),
        reader=reader,
        now=lambda: NOW,
    ).sync(policy=_policy())

    assert outcome.checkpoint.coverage_complete is True
    assert outcome.checkpoint.history_id == "history-complete"
    assert outcome.mirror.inserted_revisions == 1
    assert outcome.mirror.quarantined == 1
    assert outcome.as_dict()["status"] == "partial"
    store = GmailMirrorStore(paths.gmail_mirror_sqlite_path)
    assert store.get_current_revision("gmail.primary", "thread-good") is not None
    assert store.quarantine_counts("gmail.primary")["unresolved_count"] == 1
    assert store.triage_counts("gmail.primary")["backlog_count"] == 2


def test_quarantined_normalization_retries_after_checkpoint_and_recovers(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    failure = GmailThreadFailure(
        thread_id="thread-bad",
        source_revision="revision-bad",
        stage="normalize",
        error="ValueError: malformed message",
        payload_sha256="a" * 64,
    )
    GmailMirrorSynchronizer(
        paths,
        OperationalService(paths),
        reader=FakeReader(_failure_result("history-1", failure)),
        now=lambda: NOW,
    ).sync(policy=_policy())

    recovered = _thread("thread-bad", "revision-recovered")
    retry_reader = FakeReader(
        _empty_result("history-2"),
        _result(mode="incremental", history_id="history-2", thread=recovered),
    )
    outcome = GmailMirrorSynchronizer(
        paths,
        OperationalService(paths),
        reader=retry_reader,
        now=lambda: NOW + timedelta(minutes=10),
    ).sync(policy=_policy())

    assert len(retry_reader.calls) == 2
    assert retry_reader.calls[1]["history_id"] == "history-2"
    assert retry_reader.calls[1]["pending_thread_ids"] == ("thread-bad",)
    assert retry_reader.calls[1]["continuation_history_id"] == "history-2"
    assert outcome.retry_mirror is not None
    assert outcome.checkpoint.history_id == "history-2"
    assert outcome.checkpoint.generation == 3
    assert outcome.as_dict()["mailbox_status"] == "complete"
    assert outcome.as_dict()["status"] == "complete"
    store = GmailMirrorStore(paths.gmail_mirror_sqlite_path)
    current = store.get_current_revision("gmail.primary", "thread-bad")
    assert current is not None
    assert current.source_revision == "revision-recovered"
    assert store.quarantine_counts("gmail.primary")["unresolved_count"] == 0


def test_quarantined_storage_failure_retries_after_checkpoint_and_recovers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    bad = _thread("thread-large", "revision-large")
    original_limit = gmail_mirror_module.GMAIL_MIRROR_MAX_RAW_PAYLOAD_BYTES
    monkeypatch.setattr(
        gmail_mirror_module,
        "GMAIL_MIRROR_MAX_RAW_PAYLOAD_BYTES",
        500,
    )
    first_reader = FakeReader(
        GmailFetchResult(
            mode="full",
            raw_threads=(
                {
                    "id": "thread-large",
                    "historyId": "revision-large",
                    "oversized": "x" * 2_000,
                },
            ),
            threads=(bad,),
            changed_thread_ids=("thread-large",),
            missing_thread_ids=(),
            next_history_id="history-1",
            reset_required=False,
            coverage_complete=True,
            pages_fetched=1,
        )
    )
    first = GmailMirrorSynchronizer(
        paths,
        OperationalService(paths),
        reader=first_reader,
        now=lambda: NOW,
    ).sync(policy=_policy())
    assert first.checkpoint.history_id == "history-1"
    assert first.mirror.quarantined == 1
    monkeypatch.setattr(
        gmail_mirror_module,
        "GMAIL_MIRROR_MAX_RAW_PAYLOAD_BYTES",
        original_limit,
    )

    retry_reader = FakeReader(
        _empty_result("history-2"),
        _result(mode="incremental", history_id="history-2", thread=bad),
    )
    outcome = GmailMirrorSynchronizer(
        paths,
        OperationalService(paths),
        reader=retry_reader,
        now=lambda: NOW + timedelta(minutes=10),
    ).sync(policy=_policy())

    assert outcome.retry_mirror is not None
    assert outcome.checkpoint.generation == 3
    store = GmailMirrorStore(paths.gmail_mirror_sqlite_path)
    assert store.get_current_revision("gmail.primary", "thread-large") is not None
    assert store.quarantine_counts("gmail.primary")["unresolved_count"] == 0


@pytest.mark.parametrize(
    ("retry_error", "expected_reason"),
    [
        (
            DailyBudgetExceeded("gmail api_requests daily budget exhausted"),
            "daily_budget_exhausted",
        ),
        (
            GoogleAPIError(503, "provider temporarily unavailable", retryable=True),
            "provider_retry_unavailable",
        ),
        (
            RuntimeError("gmail API could not be reached after 2 attempts"),
            "provider_retry_unavailable",
        ),
    ],
)
def test_retry_only_failure_preserves_successful_mailbox_freshness(
    tmp_path: Path,
    retry_error: Exception,
    expected_reason: str,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    failure = GmailThreadFailure(
        thread_id="thread-bad",
        source_revision="revision-bad",
        stage="normalize",
        error="ValueError: malformed message",
        payload_sha256="a" * 64,
    )
    GmailMirrorSynchronizer(
        paths,
        OperationalService(paths),
        reader=FakeReader(_failure_result("history-1", failure)),
        now=lambda: NOW,
    ).sync(policy=_policy())
    reader = FakeReader(_empty_result("history-2"), retry_error)

    outcome = GmailMirrorSynchronizer(
        paths,
        OperationalService(paths),
        reader=reader,
        now=lambda: NOW + timedelta(minutes=10),
    ).sync(policy=_policy())

    assert len(reader.calls) == 2
    assert outcome.checkpoint.history_id == "history-2"
    assert outcome.checkpoint.generation == 2
    payload = outcome.as_dict()
    assert payload["mailbox_status"] == "complete"
    assert payload["status"] == "partial"
    assert payload["quarantine_retry_deferred_reason"] == expected_reason
    store = GmailMirrorStore(paths.gmail_mirror_sqlite_path)
    assert store.quarantine_counts("gmail.primary")["unresolved_count"] == 1


def test_quarantine_retry_is_bounded_and_confirmed_deletions_resolve(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    store = GmailMirrorStore(paths.gmail_mirror_sqlite_path)
    store.initialize()
    failures = tuple(
        GmailMirrorQuarantineInput(
            thread_id=f"thread-{index:02d}",
            source_revision=f"revision-{index:02d}",
            stage="normalize",
            error="ValueError: malformed message",
            payload_sha256=f"{index:064x}",
            parser_version=GMAIL_THREAD_PARSER_VERSION,
        )
        for index in range(12)
    )
    store.apply_sync_unit(
        GmailMirrorCheckpointUpdate(
            account_key="gmail.primary",
            history_id="history-1",
            mode="full",
            coverage_complete=True,
            reset_required=False,
            continuation_page_token=None,
            baseline_history_id=None,
            pending_thread_ids=(),
            continuation_history_id=None,
            expected_generation=None,
            last_success_at=NOW.isoformat(),
            updated_at=NOW.isoformat(),
        ),
        (),
        quarantined_threads=failures,
        parser_version=GMAIL_THREAD_PARSER_VERSION,
    )
    retried_ids = tuple(f"thread-{index:02d}" for index in range(10))
    reader = FakeReader(
        _empty_result("history-2"),
        GmailFetchResult(
            mode="incremental",
            raw_threads=(),
            threads=(),
            changed_thread_ids=retried_ids,
            missing_thread_ids=retried_ids,
            next_history_id="history-2",
            reset_required=False,
            coverage_complete=True,
            pages_fetched=0,
        ),
    )

    outcome = GmailMirrorSynchronizer(
        paths,
        OperationalService(paths),
        reader=reader,
        now=lambda: NOW + timedelta(minutes=10),
    ).sync(policy=_policy())

    assert reader.calls[1]["pending_thread_ids"] == retried_ids
    assert outcome.as_dict()["quarantine_retry_thread_count"] == 10
    assert store.quarantine_counts("gmail.primary")["unresolved_count"] == 2
    assert store.triage_counts("gmail.primary")["quarantined_count"] == 2


def test_scheduled_sync_initializes_local_ops_after_policy_and_auth_approval(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = SimpleNamespace(initialize_calls=0)

    def initialize() -> None:
        service.initialize_calls += 1
        paths.ops_sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        paths.ops_sqlite_path.touch()

    service.initialize = initialize
    monkeypatch.setattr(gmail_sync_module, "load_operations_policy", lambda _paths: _policy())
    monkeypatch.setattr(
        gmail_sync_module,
        "connector_auth_status",
        lambda _paths, _source: {"status": "connected"},
    )

    class FakeSynchronizer:
        def __init__(self, _paths, _service) -> None:
            assert _paths == paths
            assert _service is service

        def sync(self, *, policy):
            assert policy.sources.gmail.enabled is True
            return SimpleNamespace(as_dict=lambda: {"status": "complete"})

    monkeypatch.setattr(gmail_sync_module, "GmailMirrorSynchronizer", FakeSynchronizer)

    result = run_scheduled_gmail_mirror_sync(paths, service)

    assert result == {"status": "complete"}
    assert service.initialize_calls == 1


def _policy() -> OperationsPolicy:
    return OperationsPolicy.from_dict(
        default_operations_policy_payload(
            timezone_name="America/Los_Angeles",
            calendar_email="owner@example.com",
            gmail_email="owner@example.com",
        )
    )


def _result(
    *,
    mode: str,
    history_id: str,
    thread: NormalizedGmailThread,
) -> GmailFetchResult:
    return GmailFetchResult(
        mode=mode,
        raw_threads=({"id": thread.thread_id, "historyId": history_id},),
        threads=(thread,),
        changed_thread_ids=(thread.thread_id,),
        missing_thread_ids=(),
        next_history_id=history_id,
        reset_required=False,
        coverage_complete=True,
        pages_fetched=1,
        api_requests=2,
        quota_units=15,
    )


def _empty_result(history_id: str) -> GmailFetchResult:
    return GmailFetchResult(
        mode="incremental",
        raw_threads=(),
        threads=(),
        changed_thread_ids=(),
        missing_thread_ids=(),
        next_history_id=history_id,
        reset_required=False,
        coverage_complete=True,
        pages_fetched=1,
        api_requests=1,
        quota_units=2,
    )


def _failure_result(
    history_id: str,
    failure: GmailThreadFailure,
) -> GmailFetchResult:
    return GmailFetchResult(
        mode="full",
        raw_threads=(),
        threads=(),
        changed_thread_ids=(failure.thread_id,),
        missing_thread_ids=(),
        next_history_id=history_id,
        reset_required=False,
        coverage_complete=True,
        pages_fetched=1,
        quarantined_threads=(failure,),
    )


def _thread(thread_id: str, revision: str) -> NormalizedGmailThread:
    body = f"Please handle {revision} by Friday."
    message = NormalizedGmailMessage(
        message_id=f"message-{revision}",
        thread_id=thread_id,
        internal_date="1784058000000",
        timestamp="2026-07-14T18:00:00+00:00",
        from_addresses=("person@example.com",),
        to_addresses=("owner@example.com",),
        cc_addresses=(),
        subject="Operational request",
        date_header=None,
        internet_message_id=f"<{revision}@example.com>",
        in_reply_to=None,
        references=(),
        label_ids=("INBOX",),
        outgoing=False,
        operator_authored=False,
        body=body,
        body_kind="text/plain",
        attachment_count=0,
        quoted_chars_removed=0,
        truncated=False,
    )
    return NormalizedGmailThread(
        thread_id=thread_id,
        history_id=revision,
        source_revision=revision,
        subject="Operational request",
        created_at=message.timestamp,
        updated_at=message.timestamp,
        message_class="human",
        messages=(message,),
        body_chars=len(body),
        attachment_count=0,
        quoted_chars_removed=0,
        truncated=False,
    )
