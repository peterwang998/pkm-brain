from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from pkm_brain.gmail_mirror import (
    GmailMirrorCheckpointUpdate,
    GmailMirrorStore,
    GmailMirrorThreadInput,
)
from pkm_brain.google_cache import GoogleEvidenceCache
from pkm_brain.google_normalization import (
    NormalizedGmailMessage,
    NormalizedGmailThread,
)
from pkm_brain.operations_http import (
    OperationsHTTPBadRequest,
    OperationsHTTPNotFound,
    operations_evidence_payload,
    shadow_setup_payload,
)
from pkm_brain.operations_policy import operations_policy_path
from pkm_brain.paths import BrainPaths
from pkm_brain.shadow_setup import default_operations_policy_payload


def _configured_paths(tmp_path: Path) -> BrainPaths:
    paths = BrainPaths.from_value(tmp_path / "brain")
    policy_path = operations_policy_path(paths)
    policy_path.parent.mkdir(parents=True, mode=0o700)
    policy_path.write_text(
        yaml.safe_dump(
            default_operations_policy_payload(
                timezone_name="America/Los_Angeles",
                calendar_email="owner@example.com",
                gmail_email="owner@example.com",
            ),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    policy_path.chmod(0o600)
    return paths


def test_operations_evidence_endpoint_reads_the_cited_revision_only(
    tmp_path: Path,
) -> None:
    paths = _configured_paths(tmp_path)
    cache = GoogleEvidenceCache.for_paths(paths)
    now = datetime.now(timezone.utc)
    cache.write_normalized(
        "gmail",
        "gmail.primary:thread-1",
        {"subject": "First"},
        source_revision="revision-1",
        cached_at=now,
    )
    cache.write_normalized(
        "gmail",
        "gmail.primary:thread-1",
        {"subject": "Second"},
        source_revision="revision-2",
        cached_at=now,
    )

    payload = operations_evidence_payload(
        paths,
        {
            "source_type": ["gmail"],
            "account_key": ["gmail.primary"],
            "source_ref": ["gmail.primary:thread-1"],
            "source_revision": ["revision-1"],
        },
    )
    assert payload["evidence"] == {"subject": "First"}
    assert payload["source_revision"] == "revision-1"

    with pytest.raises(OperationsHTTPNotFound):
        operations_evidence_payload(
            paths,
            {
                "source_type": ["gmail"],
                "account_key": ["gmail.someone-else"],
                "source_ref": ["gmail.someone-else:thread-1"],
                "source_revision": ["revision-1"],
            },
        )


def test_operations_evidence_endpoint_prefers_durable_gmail_mirror(
    tmp_path: Path,
) -> None:
    paths = _configured_paths(tmp_path)
    store = GmailMirrorStore(paths.gmail_mirror_sqlite_path)
    store.initialize()
    thread = _gmail_thread("thread-1", "Durable mirror subject")
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
            last_success_at="2026-07-14T17:00:00+00:00",
            updated_at="2026-07-14T17:00:00+00:00",
        ),
        (GmailMirrorThreadInput(thread=thread, raw_payload={"id": "thread-1"}),),
    )

    payload = operations_evidence_payload(
        paths,
        {
            "source_type": ["gmail"],
            "account_key": ["gmail.primary"],
            "source_ref": ["gmail.primary:thread-1"],
            "source_revision": ["revision-1"],
        },
    )

    assert payload["evidence"]["subject"] == "Durable mirror subject"
    assert payload["evidence_origin"] == "gmail_mirror"
    assert payload["source_revision"] == "revision-1"


def test_operations_evidence_endpoint_labels_current_local_gmail_fallback(
    tmp_path: Path,
) -> None:
    paths = _configured_paths(tmp_path)
    store = GmailMirrorStore(paths.gmail_mirror_sqlite_path)
    store.initialize()
    current = _gmail_thread("thread-1", "Current local subject")
    current = NormalizedGmailThread(
        thread_id=current.thread_id,
        history_id="history-2",
        source_revision="revision-2",
        subject=current.subject,
        created_at=current.created_at,
        updated_at=current.updated_at,
        message_class=current.message_class,
        messages=current.messages,
        body_chars=current.body_chars,
        attachment_count=current.attachment_count,
        quoted_chars_removed=current.quoted_chars_removed,
        truncated=current.truncated,
    )
    store.apply_sync_unit(
        GmailMirrorCheckpointUpdate(
            account_key="gmail.primary",
            history_id="history-2",
            mode="incremental",
            coverage_complete=True,
            reset_required=False,
            continuation_page_token=None,
            baseline_history_id=None,
            pending_thread_ids=(),
            continuation_history_id=None,
            expected_generation=None,
            last_success_at="2026-07-14T17:00:00+00:00",
            updated_at="2026-07-14T17:00:00+00:00",
        ),
        (GmailMirrorThreadInput(thread=current, raw_payload={"id": "thread-1"}),),
    )

    payload = operations_evidence_payload(
        paths,
        {
            "source_type": ["gmail"],
            "account_key": ["gmail.primary"],
            "source_ref": ["gmail.primary:thread-1"],
            "source_revision": ["revision-1"],
        },
    )

    assert payload["evidence"]["subject"] == "Current local subject"
    assert payload["evidence_origin"] == "gmail_mirror_current_fallback"
    assert payload["requested_source_revision"] == "revision-1"
    assert payload["source_revision"] == "revision-2"
    assert payload["revision_matches"] is False


def test_shadow_setup_reports_mailbox_and_triage_health_separately(
    tmp_path: Path,
) -> None:
    paths = _configured_paths(tmp_path)
    store = GmailMirrorStore(paths.gmail_mirror_sqlite_path)
    store.initialize()
    thread = _gmail_thread("thread-1", "Needs analysis")
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
            last_success_at="2026-07-14T17:00:00+00:00",
            updated_at="2026-07-14T17:00:00+00:00",
        ),
        (GmailMirrorThreadInput(thread=thread, raw_payload={"id": "thread-1"}),),
    )

    payload = shadow_setup_payload(paths, scheduler_state=_scheduler_state())

    assert payload["automatic_schedule_enabled"] is True
    assert payload["approved_defaults"]["gmail_sync_cadence_seconds"] == 600
    assert payload["gmail_mirror"]["mailbox_status"] == "synchronized"
    assert payload["gmail_mirror"]["triage_status"] == "backlogged"
    assert payload["gmail_mirror"]["triage_pending_count"] == 1
    assert payload["gmail_mirror"]["scheduled_sync"] == {
        "available": True,
        "job_id": "gmail_mirror_sync",
        "enabled": True,
        "paused": True,
        "paused_until": "2099-07-14T18:00:00+00:00",
        "last_run_at": "2026-07-14T17:00:00+00:00",
        "last_status": "failed",
        "last_error": "daily Gmail request budget exhausted",
        "next_due_at": "2026-07-14T17:10:00+00:00",
        "running": False,
    }


def test_shadow_setup_fails_closed_for_missing_and_insecure_enabled_mirror(
    tmp_path: Path,
) -> None:
    paths = _configured_paths(tmp_path)

    missing = shadow_setup_payload(paths, scheduler_state=_scheduler_state())

    assert missing["gmail_mirror"]["source_enabled"] is True
    assert missing["gmail_mirror"]["mailbox_status"] == "not_initialized"

    store = GmailMirrorStore(paths.gmail_mirror_sqlite_path)
    store.initialize()
    paths.gmail_mirror_sqlite_path.chmod(0o644)

    insecure = shadow_setup_payload(paths, scheduler_state=_scheduler_state())

    assert insecure["gmail_mirror"]["source_enabled"] is True
    assert insecure["gmail_mirror"]["mailbox_status"] == "unavailable"
    assert insecure["gmail_mirror"]["triage_status"] == "unknown"
    assert "owner-only" in insecure["gmail_mirror"]["message"]


def test_shadow_setup_marks_schedule_unavailable_without_runtime_state(
    tmp_path: Path,
) -> None:
    paths = _configured_paths(tmp_path)

    payload = shadow_setup_payload(paths)

    assert payload["automatic_schedule_enabled"] is False
    assert payload["gmail_mirror"]["scheduled_sync"]["available"] is False
    assert payload["gmail_mirror"]["scheduled_sync"]["enabled"] is None


@pytest.mark.parametrize(
    "query",
    (
        {"source_type": ["gmail"], "unknown": ["value"]},
        {"source_type": ["gmail", "calendar"]},
        {"source_type": ["gmail"], "account_key": ["x" * 513]},
        {
            "source_type": ["gmail"],
            "account_key": ["gmail.primary"],
            "source_ref": ["gmail.primary:thread-1"],
            "source_revision": ["x" * 1_025],
        },
    ),
)
def test_operations_evidence_endpoint_rejects_ambiguous_or_unbounded_query(
    tmp_path: Path,
    query: dict[str, list[str]],
) -> None:
    with pytest.raises(OperationsHTTPBadRequest):
        operations_evidence_payload(
            BrainPaths.from_value(tmp_path / "brain"),
            query,
        )


def _gmail_thread(thread_id: str, subject: str) -> NormalizedGmailThread:
    message = NormalizedGmailMessage(
        message_id="message-1",
        thread_id=thread_id,
        internal_date="1784058000000",
        timestamp="2026-07-14T17:00:00+00:00",
        from_addresses=("person@example.com",),
        to_addresses=("owner@example.com",),
        cc_addresses=(),
        subject=subject,
        date_header=None,
        internet_message_id="<message-1@example.com>",
        in_reply_to=None,
        references=(),
        label_ids=("INBOX",),
        outgoing=False,
        operator_authored=False,
        body="Please review this before Friday.",
        body_kind="text/plain",
        attachment_count=0,
        quoted_chars_removed=0,
        truncated=False,
    )
    return NormalizedGmailThread(
        thread_id=thread_id,
        history_id="history-1",
        source_revision="revision-1",
        subject=subject,
        created_at=message.timestamp,
        updated_at=message.timestamp,
        message_class="human",
        messages=(message,),
        body_chars=len(message.body),
        attachment_count=0,
        quoted_chars_removed=0,
        truncated=False,
    )


def _scheduler_state() -> dict[str, object]:
    return {
        "paused_until": "2099-07-14T18:00:00+00:00",
        "jobs": [
            {
                "id": "gmail_mirror_sync",
                "enabled": True,
                "last_run_at": "2026-07-14T17:00:00+00:00",
                "last_status": "failed",
                "last_error": "daily Gmail request budget exhausted",
                "next_due_at": "2026-07-14T17:10:00+00:00",
                "running": False,
            }
        ],
    }
