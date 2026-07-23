from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pkm_brain.db import connection, init_db
from pkm_brain.gmail_archive import (
    ArchiveMessage,
    ArchiveState,
    GmailArchiveStore,
    StaticGmailArchiveKeyProvider,
)
from pkm_brain.gmail_knowledge import (
    GmailKnowledgeCapture,
    reconcile_gmail_document_revisions,
)
from pkm_brain.gmail_projection import (
    GMAIL_KNOWLEDGE_CLASSIFIER_VERSION,
    GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    GMAIL_MESSAGE_POLICY_VERSION,
    gmail_projection_session_id,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.util import slugify


MODULE_PATH = (
    Path(__file__).parents[1] / "scripts" / "audit_gmail_temporal_shadow_canary.py"
)
SPEC = importlib.util.spec_from_file_location(
    "audit_gmail_temporal_shadow_canary", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


ACCOUNT = "private-owner@example.test"
PRIVATE_BODY = "The private Atlas interview is scheduled for August 14, 2027."


def _key(path: Path) -> Path:
    path.write_bytes(b"shadow-canary-test-key-material-32")
    os.chmod(path, 0o600)
    return path


def _add_revision(
    paths: BrainPaths,
    *,
    document_id: str,
    thread_id: str,
    revision_character: str,
    messages: tuple[tuple[str, str, str], ...],
    projection_version: int = GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    classifier_version: int = GMAIL_KNOWLEDGE_CLASSIFIER_VERSION,
    deleted: bool = False,
) -> Path:
    rendered_messages: list[str] = []
    timestamps: list[dict[str, object]] = []
    policies: list[dict[str, object]] = []
    thread_heading = "# Email thread: Private shadow fixture"
    body_offset = len(thread_heading) + 2
    for index, (message_id, internal_at, body) in enumerate(messages, start=1):
        rendered = "\n".join(
            (
                f"## Message {index} — {internal_at} — {message_id}",
                "",
                "From: private-sender@example.test",
                f"To: {ACCOUNT}",
                "Direction: incoming (test)",
                "Subject: Private Atlas interview",
                "",
                body,
            )
        )
        start = body_offset
        end = start + len(rendered)
        timestamps.append(
            {
                "message_id": message_id,
                "internal_date": internal_at,
                "start_offset": start,
                "end_offset": end,
            }
        )
        policies.append(
            {
                "message_id": message_id,
                "delivery_kind": "human",
                "advertising_bases": [],
                "fact_admission_basis": "durable_human_candidate",
                "provider_important": False,
                "provider_starred": False,
                "human_signal_basis": "provider_sent",
                "operator_message_after": index < len(messages),
            }
        )
        rendered_messages.append(rendered)
        body_offset = end + 2

    revision = revision_character * 64
    message_ids = [message_id for message_id, _internal_at, _body in messages]
    markdown = "\n".join(
        (
            "---",
            "title: Private shadow fixture",
            "source_type: gmail_thread",
            f"gmail_account_key: {ACCOUNT}",
            f"gmail_thread_id: {thread_id}",
            f"gmail_source_revision: {revision}",
            f"gmail_projection_version: {projection_version}",
            f"gmail_classifier_version: {classifier_version}",
            f"gmail_message_ids: {json.dumps(message_ids)}",
            "gmail_message_timestamps_version: 1",
            f"gmail_message_timestamps: {json.dumps(timestamps)}",
            f"gmail_message_policy_version: {GMAIL_MESSAGE_POLICY_VERSION}",
            f"gmail_message_policies: {json.dumps(policies)}",
            f"retained_message_count: {len(messages)}",
            f"gmail_fact_admitted_message_ids: {json.dumps(message_ids)}",
            "fact_eligible: true",
            "fact_importance: durable_candidate",
            "actionability: informational",
            "delivery_kind: human",
            "classification: human",
            "importance_confidence: 0.99",
            "gmail_human_signal_basis: provider_sent",
            f"deleted: {'true' if deleted else 'false'}",
            "---",
            thread_heading,
            "",
            "\n\n".join(rendered_messages),
            "",
        )
    )
    session_id = gmail_projection_session_id(
        account_key=ACCOUNT,
        thread_id=thread_id,
        source_revision=revision,
        projection_version=projection_version,
    )
    source_path = paths.inbox / "documents" / "gmail" / f"{slugify(session_id)}.md"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(markdown, encoding="utf-8")
    content_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO documents(
              id, source_type, title, source_path, raw_path, content_hash,
              created_at, ingested_at, tags, status
            ) VALUES (?, 'gmail_thread', 'Private shadow fixture', ?, ?, ?, ?, ?,
                      '[]', ?)
            """,
            (
                document_id,
                str(source_path),
                str(paths.raw / f"{document_id}.md"),
                content_hash,
                "2027-07-01T16:00:00+00:00",
                "2027-07-01T16:01:00+00:00",
                "deleted" if deleted else "active",
            ),
        )
    return source_path


def _supersede(paths: BrainPaths, document_id: str) -> None:
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE documents SET status = 'superseded' WHERE id = ?",
            (document_id,),
        )


def _rewrite_classifier_version(
    paths: BrainPaths, document_id: str, source_path: Path, version: int
) -> None:
    text = source_path.read_text(encoding="utf-8")
    prefix = "gmail_classifier_version: "
    lines = [
        f"{prefix}{version}" if line.startswith(prefix) else line
        for line in text.splitlines()
    ]
    updated = "\n".join(lines) + "\n"
    source_path.write_text(updated, encoding="utf-8")
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE documents SET content_hash = ? WHERE id = ?",
            (hashlib.sha256(updated.encode("utf-8")).hexdigest(), document_id),
        )


def test_shadow_canary_separates_new_and_existing_thread_messages_without_writes(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "private-canary-home")
    init_db(paths.sqlite_path)
    key_path = _key(tmp_path / "canary.key")
    state_path = tmp_path / "canary-state.json"
    _add_revision(
        paths,
        document_id="baseline-document",
        thread_id="existing-private-thread",
        revision_character="a",
        messages=(
            ("baseline-private-message", "2027-07-01T09:00:00-07:00", PRIVATE_BODY),
        ),
    )

    initialized = audit.initialize_shadow_canary(
        paths.home,
        state_path=state_path,
        hmac_key_path=key_path,
        observed_at="2027-07-01T16:00:00+00:00",
        non_release_test_clock=True,
    )

    assert initialized["status"] == "ready"
    assert initialized["baseline"]["current_thread_records"] == 1
    assert initialized["baseline"]["visible_messages"] == 1
    assert initialized["external_calls"] == 0
    assert initialized["brain_mutations"] == 0
    assert stat_mode(state_path) == 0o600
    with pytest.raises(audit.ShadowCanaryAuditError, match="already exists"):
        audit.initialize_shadow_canary(
            paths.home,
            state_path=state_path,
            hmac_key_path=key_path,
            observed_at="2027-07-01T16:00:00+00:00",
            non_release_test_clock=True,
        )

    _supersede(paths, "baseline-document")
    _add_revision(
        paths,
        document_id="existing-update-document",
        thread_id="existing-private-thread",
        revision_character="b",
        messages=(
            ("baseline-private-message", "2027-07-01T09:00:00-07:00", PRIVATE_BODY),
            (
                "existing-thread-new-private-message",
                "2027-07-02T09:00:00-07:00",
                "The private Atlas interview was rescheduled from August 14, 2027 to August 16, 2027.",
            ),
        ),
    )
    _add_revision(
        paths,
        document_id="new-thread-document",
        thread_id="new-private-thread",
        revision_character="c",
        messages=(
            (
                "new-thread-private-message",
                "2027-07-02T10:00:00-07:00",
                "The private Nimbus appointment is scheduled for September 2, 2027.",
            ),
        ),
    )

    observed = audit.observe_shadow_canary(
        paths.home,
        state_path=state_path,
        hmac_key_path=key_path,
        observed_at="2027-07-08T16:00:00+00:00",
        non_release_test_clock=True,
    )

    assert observed["error_buckets"] == {}
    assert observed["status"] == "ready", observed
    assert observed["observation"] == {
        "new_thread_records": 1,
        "existing_threads_with_unseen_messages": 1,
        "revision_only_existing_thread_updates": 0,
        "threads_missing_since_prior_observation": 0,
        "newly_observed_messages": 2,
        "current_visible_messages": 3,
        "current_deleted_thread_records": 0,
    }
    assert observed["attempts"]["new_thread"]["attempted_messages"] == 1
    assert observed["attempts"]["existing_thread_unseen"]["attempted_messages"] == 1
    assert observed["attempts"]["new_thread"]["prepared_messages"] == 1
    assert observed["attempts"]["existing_thread_unseen"]["prepared_messages"] == 1
    cumulative = observed["cumulative"]
    assert cumulative["baseline_messages"] == 1
    assert cumulative["observed_messages"] == 2
    assert cumulative["structural_gates"]["seven_days_observed"] is False
    assert cumulative["structural_gates"]["seven_days_elapsed_diagnostic"] is True
    assert cumulative["structural_gates"]["release_clock_eligible"] is False
    assert cumulative["clock"] == {
        "mode": "test_non_release",
        "release_eligible": False,
        "duration_gate_evaluable": False,
        "test_clock_cannot_qualify_release": True,
    }
    assert (
        cumulative["structural_gates"]["existing_thread_unseen_stratum_observed"]
        is True
    )
    assert cumulative["semantic_gates"]["precision_recall_evaluable"] is False
    assert cumulative["release_claim"] is False
    assert observed["external_calls"] == 0
    assert observed["temporal_persistence_calls"] == 0

    serialized = json.dumps(observed, sort_keys=True) + state_path.read_text()
    private_values = (
        ACCOUNT,
        PRIVATE_BODY,
        "Atlas",
        "Nimbus",
        "existing-private-thread",
        "new-private-thread",
        "baseline-private-message",
        "existing-thread-new-private-message",
        "new-thread-private-message",
        "baseline-document",
        "existing-update-document",
        "new-thread-document",
        str(paths.home),
    )
    assert all(value not in serialized for value in private_values)

    with connection(paths.sqlite_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM gmail_temporal_review_executions"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM gmail_temporal_review_runs").fetchone()[
                0
            ]
            == 0
        )

    replay = audit.observe_shadow_canary(
        paths.home,
        state_path=state_path,
        hmac_key_path=key_path,
        observed_at="2027-07-08T16:10:00+00:00",
        non_release_test_clock=True,
    )
    assert replay["observation"]["newly_observed_messages"] == 0
    assert replay["attempts"]["new_thread"]["attempted_messages"] == 0
    assert replay["cumulative"]["observed_messages"] == 2


def test_shadow_canary_retries_failed_preparation_without_double_counting(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "private-retry-home")
    init_db(paths.sqlite_path)
    key_path = _key(tmp_path / "retry.key")
    state_path = tmp_path / "retry-state.json"
    _add_revision(
        paths,
        document_id="baseline-document",
        thread_id="baseline-thread",
        revision_character="d",
        messages=(("baseline-message", "2027-07-01T09:00:00-07:00", PRIVATE_BODY),),
    )
    audit.initialize_shadow_canary(
        paths.home,
        state_path=state_path,
        hmac_key_path=key_path,
        observed_at="2027-07-01T16:00:00+00:00",
        non_release_test_clock=True,
    )
    stale_path = _add_revision(
        paths,
        document_id="stale-new-document",
        thread_id="stale-new-thread",
        revision_character="e",
        classifier_version=GMAIL_KNOWLEDGE_CLASSIFIER_VERSION - 1,
        messages=(
            (
                "stale-new-message",
                "2027-07-02T09:00:00-07:00",
                "The private Cedar session is scheduled for September 3, 2027.",
            ),
        ),
    )

    failed = audit.observe_shadow_canary(
        paths.home,
        state_path=state_path,
        hmac_key_path=key_path,
        observed_at="2027-07-02T16:00:00+00:00",
        non_release_test_clock=True,
    )
    assert failed["status"] == "partial"
    assert failed["error_buckets"] == {"stale_policy_version": 1}
    assert failed["cumulative"]["observed_messages"] == 1
    assert failed["cumulative"]["by_stratum"]["new_thread"]["pending_messages"] == 1

    _rewrite_classifier_version(
        paths,
        "stale-new-document",
        stale_path,
        GMAIL_KNOWLEDGE_CLASSIFIER_VERSION,
    )
    recovered = audit.observe_shadow_canary(
        paths.home,
        state_path=state_path,
        hmac_key_path=key_path,
        observed_at="2027-07-02T16:10:00+00:00",
        non_release_test_clock=True,
    )
    assert recovered["error_buckets"] == {}
    assert recovered["status"] == "ready", recovered
    assert recovered["observation"]["newly_observed_messages"] == 0
    assert recovered["attempts"]["new_thread"]["retried_messages"] == 1
    assert recovered["attempts"]["new_thread"]["prepared_messages"] == 1
    assert recovered["cumulative"]["observed_messages"] == 1
    assert recovered["cumulative"]["by_stratum"]["new_thread"]["attempts"] == 2
    assert recovered["cumulative"]["by_stratum"]["new_thread"]["pending_messages"] == 0


def test_archive_append_projects_as_existing_thread_unseen_message(
    tmp_path: Path,
) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir(mode=0o700)
    store = GmailArchiveStore(
        archive_dir / "gmail.sqlite",
        StaticGmailArchiveKeyProvider(b"k" * 32),
    )
    store.initialize()
    store.provision_key()
    first_state = ArchiveState(
        account_key="gmail.primary",
        phase="live",
        query="after:1",
        window_start="2027-07-01T00:00:00+00:00",
        window_end="2027-07-02T00:00:00+00:00",
        updated_at="2027-07-01T16:00:00+00:00",
        history_id="100",
        coverage_complete=True,
    )
    store.apply_batch(
        "gmail.primary",
        messages=(
            _archive_message(
                "archive-baseline-message",
                "archive-existing-thread",
                "The private Atlas interview is scheduled for August 14, 2027.",
                "1782921600000",
            ),
        ),
        state=first_state,
    )

    paths = BrainPaths.from_value(tmp_path / "archive-canary-home")
    service = BrainService(paths)
    service.init_workspace()
    adapter = GmailKnowledgeCapture(
        paths,
        store,
        account_key="gmail.primary",
        operator_email="private-owner@example.test",
        batch_size=None,
    )
    first_capture = adapter.capture(adapter.discover())
    assert first_capture.errors == [] and first_capture.captured == 1
    service.ingest(source=paths.inbox / "documents" / "gmail")
    first_reconciliation = reconcile_gmail_document_revisions(paths)
    assert first_reconciliation.active_documents == 1

    key_path = _key(tmp_path / "archive-canary.key")
    state_path = tmp_path / "archive-canary-state.json"
    audit.initialize_shadow_canary(
        paths.home,
        state_path=state_path,
        hmac_key_path=key_path,
        observed_at="2027-07-01T16:00:00+00:00",
        non_release_test_clock=True,
    )

    second_state = ArchiveState(
        account_key="gmail.primary",
        phase="live",
        query="after:1",
        window_start="2027-07-01T00:00:00+00:00",
        window_end="2027-07-03T00:00:00+00:00",
        updated_at="2027-07-02T16:00:00+00:00",
        history_id="101",
        coverage_complete=True,
    )
    store.apply_batch(
        "gmail.primary",
        messages=(
            _archive_message(
                "archive-new-message",
                "archive-existing-thread",
                "The private Atlas interview was rescheduled from August 14, 2027 to August 16, 2027.",
                "1783008000000",
            ),
        ),
        state=second_state,
    )
    second_capture = adapter.capture(adapter.discover())
    assert second_capture.errors == [] and second_capture.captured == 1
    service.ingest(source=paths.inbox / "documents" / "gmail")
    second_reconciliation = reconcile_gmail_document_revisions(paths)
    assert second_reconciliation.active_documents == 1
    assert second_reconciliation.superseded_documents == 1

    result = audit.observe_shadow_canary(
        paths.home,
        state_path=state_path,
        hmac_key_path=key_path,
        observed_at="2027-07-02T16:10:00+00:00",
        non_release_test_clock=True,
    )

    assert result["status"] == "ready", result
    assert result["observation"]["new_thread_records"] == 0
    assert result["observation"]["existing_threads_with_unseen_messages"] == 1
    assert result["observation"]["newly_observed_messages"] == 1
    assert result["attempts"]["existing_thread_unseen"]["prepared_messages"] == 1


def test_shadow_canary_authenticates_the_entire_state_envelope(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "authenticated-state-home")
    init_db(paths.sqlite_path)
    key_path = _key(tmp_path / "authenticated-state.key")
    state_path = tmp_path / "authenticated-state.json"
    audit.initialize_shadow_canary(
        paths.home,
        state_path=state_path,
        hmac_key_path=key_path,
        observed_at="2027-07-01T16:00:00+00:00",
        non_release_test_clock=True,
    )

    envelope = json.loads(state_path.read_text(encoding="utf-8"))
    assert envelope["version"] == "gmail_temporal_shadow_canary_state_envelope_v1"
    assert len(envelope["state_hmac"]) == 64
    envelope["state"]["observation_count"] = 1
    envelope["state"]["generation"] = 1
    state_path.write_bytes(audit._canonical_bytes(envelope))
    os.chmod(state_path, 0o600)

    with pytest.raises(audit.ShadowCanaryAuditError, match="authentication"):
        audit.shadow_canary_status(
            state_path=state_path,
            hmac_key_path=key_path,
        )


def test_cli_as_of_cannot_create_a_release_clock_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = BrainPaths.from_value(tmp_path / "cli-clock-home")
    init_db(paths.sqlite_path)
    key_path = _key(tmp_path / "cli-clock.key")
    state_path = tmp_path / "cli-clock-state.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(MODULE_PATH),
            "init",
            "--home",
            str(paths.home),
            "--state",
            str(state_path),
            "--hmac-key",
            str(key_path),
            "--as-of",
            "2099-07-09T16:00:00+00:00",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        audit.main()

    assert exc_info.value.code == 2
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert report["state_written"] is False
    assert not state_path.exists()


def test_shadow_canary_state_write_rejects_stale_generation_and_authenticator(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "cas-home")
    init_db(paths.sqlite_path)
    key_path = _key(tmp_path / "cas.key")
    state_path = tmp_path / "cas-state.json"
    audit.initialize_shadow_canary(
        paths.home,
        state_path=state_path,
        hmac_key_path=key_path,
        observed_at="2027-07-01T16:00:00+00:00",
        non_release_test_clock=True,
    )
    key = key_path.read_bytes()

    with audit._state_lock(state_path, exclusive=True):
        original = audit._load_state(state_path, key=key)
        original_authenticator = audit._state_authenticator(original, key=key)
        next_state = copy.deepcopy(original)
        next_state["generation"] = 1
        next_state["observation_count"] = 1
        audit._write_private_state(
            state_path,
            next_state,
            key=key,
            expected_generation=0,
            expected_authenticator=original_authenticator,
        )

        with pytest.raises(audit.ShadowCanaryAuditError, match="concurrently"):
            audit._write_private_state(
                state_path,
                next_state,
                key=key,
                expected_generation=0,
                expected_authenticator=original_authenticator,
            )


def test_explicit_clock_is_non_release_only_and_cannot_pass_duration_gate(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "clock-home")
    init_db(paths.sqlite_path)
    key_path = _key(tmp_path / "clock.key")
    test_state_path = tmp_path / "test-clock-state.json"

    with pytest.raises(audit.ShadowCanaryAuditError, match="non-release"):
        audit.initialize_shadow_canary(
            paths.home,
            state_path=test_state_path,
            hmac_key_path=key_path,
            observed_at="2027-07-01T16:00:00+00:00",
        )

    initialized = audit.initialize_shadow_canary(
        paths.home,
        state_path=test_state_path,
        hmac_key_path=key_path,
        observed_at="2027-07-01T16:00:00+00:00",
        non_release_test_clock=True,
    )
    assert initialized["clock"]["release_eligible"] is False
    observed = audit.observe_shadow_canary(
        paths.home,
        state_path=test_state_path,
        hmac_key_path=key_path,
        observed_at="2027-07-09T16:00:00+00:00",
        non_release_test_clock=True,
    )
    assert observed["cumulative"]["elapsed_seconds"] == 8 * 86400
    assert (
        observed["cumulative"]["structural_gates"]["seven_days_elapsed_diagnostic"]
        is True
    )
    assert observed["cumulative"]["structural_gates"]["seven_days_observed"] is False
    with pytest.raises(audit.ShadowCanaryAuditError, match="clock mode"):
        audit.observe_shadow_canary(
            paths.home,
            state_path=test_state_path,
            hmac_key_path=key_path,
        )

    production_state_path = tmp_path / "production-clock-state.json"
    production = audit.initialize_shadow_canary(
        paths.home,
        state_path=production_state_path,
        hmac_key_path=key_path,
    )
    assert production["clock"] == {
        "mode": "production_wall_clock",
        "release_eligible": True,
        "duration_gate_evaluable": True,
        "test_clock_cannot_qualify_release": False,
    }


def test_concurrent_observations_are_serialized_without_duplicate_preparation(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "concurrent-home")
    init_db(paths.sqlite_path)
    key_path = _key(tmp_path / "concurrent.key")
    state_path = tmp_path / "concurrent-state.json"
    _add_revision(
        paths,
        document_id="concurrent-baseline-document",
        thread_id="concurrent-baseline-thread",
        revision_character="f",
        messages=(
            ("concurrent-baseline-message", "2027-07-01T09:00:00-07:00", PRIVATE_BODY),
        ),
    )
    audit.initialize_shadow_canary(
        paths.home,
        state_path=state_path,
        hmac_key_path=key_path,
        observed_at="2027-07-01T16:00:00+00:00",
        non_release_test_clock=True,
    )
    _add_revision(
        paths,
        document_id="concurrent-new-document",
        thread_id="concurrent-new-thread",
        revision_character="9",
        messages=(
            (
                "concurrent-new-message",
                "2027-07-02T09:00:00-07:00",
                "The private Cedar session is scheduled for September 3, 2027.",
            ),
        ),
    )
    barrier = threading.Barrier(2)

    def observe() -> dict[str, object]:
        barrier.wait()
        return audit.observe_shadow_canary(
            paths.home,
            state_path=state_path,
            hmac_key_path=key_path,
            observed_at="2027-07-02T16:00:00+00:00",
            non_release_test_clock=True,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _index: observe(), range(2)))

    assert sorted(
        int(item["attempts"]["new_thread"]["attempted_messages"])  # type: ignore[index]
        for item in results
    ) == [0, 1]
    final = audit.shadow_canary_status(
        state_path=state_path,
        hmac_key_path=key_path,
    )
    assert final["cumulative"]["state_generation"] == 2
    assert final["cumulative"]["observation_count"] == 2
    assert final["cumulative"]["by_stratum"]["new_thread"]["attempts"] == 1
    assert final["cumulative"]["by_stratum"]["new_thread"]["prepared_messages"] == 1


def test_shadow_canary_rejects_unprotected_key(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "empty-home")
    init_db(paths.sqlite_path)
    key_path = tmp_path / "unsafe.key"
    key_path.write_bytes(b"shadow-canary-test-key-material-32")
    os.chmod(key_path, 0o644)

    with pytest.raises(audit.ShadowCanaryAuditError, match="protected"):
        audit.initialize_shadow_canary(
            paths.home,
            state_path=tmp_path / "state.json",
            hmac_key_path=key_path,
            observed_at="2027-07-01T16:00:00+00:00",
            non_release_test_clock=True,
        )


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _archive_message(
    message_id: str, thread_id: str, body: str, internal_date: str
) -> ArchiveMessage:
    raw = (
        "From: Person <private-sender@example.test>\r\n"
        "To: Owner <private-owner@example.test>\r\n"
        "Subject: Private Atlas interview\r\n"
        "Date: Thu, 1 Jul 2027 09:00:00 -0700\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        f"{body}\r\n"
    ).encode()
    return ArchiveMessage(
        message_id=message_id,
        thread_id=thread_id,
        raw_rfc822=raw,
        internal_date=internal_date,
        label_ids=("SENT",),
    )
