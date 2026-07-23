from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from pkm_brain.db import connection, init_db
from pkm_brain.gmail_projection import (
    GMAIL_KNOWLEDGE_CLASSIFIER_VERSION,
    GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    GMAIL_MESSAGE_POLICY_VERSION,
    gmail_projection_session_id,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.util import slugify


ROOT = Path(__file__).parents[1]
SCRIPT_PATH = ROOT / "scripts" / "freeze_gmail_temporal_development_baseline.py"
SPEC = importlib.util.spec_from_file_location(
    "freeze_gmail_temporal_development_baseline",
    SCRIPT_PATH,
)
assert SPEC is not None and SPEC.loader is not None
baseline = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = baseline
SPEC.loader.exec_module(baseline)


ACCOUNT = "private-owner@example.test"
THREAD = "private-provider-thread"
MESSAGE = "private-provider-message"
PRIVATE_TEXT = "Private Atlas planning is scheduled for August 14."
INTERNAL_AT = "2027-07-01T16:00:00+00:00"


def _private_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    os.chmod(path, 0o600)


def _add_document(
    paths: BrainPaths,
    *,
    revision_character: str = "a",
    thread_id: str = THREAD,
    message_id: str = MESSAGE,
    deleted: bool = False,
) -> Path:
    revision = revision_character * 64
    if deleted:
        rendered = ""
        message_ids: list[str] = []
        timestamps: list[dict[str, Any]] = []
        policies: list[dict[str, Any]] = []
    else:
        rendered = "\n".join(
            (
                f"## Message 1 — {INTERNAL_AT} — {message_id}",
                "",
                "From: private-sender@example.test",
                f"To: {ACCOUNT}",
                "Direction: incoming (test)",
                "Subject: Private Atlas plan",
                "",
                PRIVATE_TEXT,
            )
        )
        message_ids = [message_id]
        policies = [
            {
                "message_id": message_id,
                "delivery_kind": "human",
                "advertising_bases": [],
                "fact_admission_basis": "durable_human_candidate",
                "provider_important": False,
                "provider_starred": False,
                "human_signal_basis": "provider_sent",
                "operator_message_after": False,
            }
        ]
    heading = "# Email thread: Private baseline fixture"
    start = len(heading) + 2
    end = start + len(rendered)
    if not deleted:
        timestamps = [
            {
                "message_id": message_id,
                "internal_date": INTERNAL_AT,
                "start_offset": start,
                "end_offset": end,
            }
        ]
    markdown = "\n".join(
        (
            "---",
            "title: Private baseline fixture",
            "source_type: gmail_thread",
            f"gmail_account_key: {ACCOUNT}",
            f"gmail_thread_id: {thread_id}",
            f"gmail_source_revision: {revision}",
            f"gmail_projection_version: {GMAIL_KNOWLEDGE_PROJECTION_VERSION}",
            f"gmail_classifier_version: {GMAIL_KNOWLEDGE_CLASSIFIER_VERSION}",
            f"gmail_message_ids: {json.dumps(message_ids)}",
            "gmail_message_timestamps_version: 1",
            f"gmail_message_timestamps: {json.dumps(timestamps)}",
            f"gmail_message_policy_version: {GMAIL_MESSAGE_POLICY_VERSION}",
            f"gmail_message_policies: {json.dumps(policies)}",
            f"retained_message_count: {len(message_ids)}",
            "omitted_message_count: 0",
            "truncated_message_count: 0",
            f"gmail_fact_admitted_message_ids: {json.dumps(message_ids)}",
            f"fact_eligible: {'false' if deleted else 'true'}",
            f"fact_importance: {'none' if deleted else 'durable_candidate'}",
            "actionability: informational",
            f"delivery_kind: {'unknown' if deleted else 'human'}",
            f"classification: {'unknown' if deleted else 'human'}",
            "importance_confidence: 0.99",
            "gmail_human_signal_basis: provider_sent",
            f"deleted: {'true' if deleted else 'false'}",
            "---",
            heading,
            "",
            rendered,
            "",
        )
    )
    session_id = gmail_projection_session_id(
        account_key=ACCOUNT,
        thread_id=thread_id,
        source_revision=revision,
        projection_version=GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    )
    source_path = paths.inbox / "documents" / "gmail" / f"{slugify(session_id)}.md"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(markdown, encoding="utf-8")
    content_hash = hashlib.sha256(markdown.encode()).hexdigest()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO documents(
              id, source_type, title, source_path, raw_path, content_hash,
              created_at, ingested_at, tags, status
            ) VALUES (?, 'gmail_thread', 'Private baseline fixture', ?, ?, ?,
                      ?, ?, '[]', ?)
            """,
            (
                f"document-{revision_character}",
                str(source_path),
                str(paths.raw / "private-missing.md"),
                content_hash,
                INTERNAL_AT,
                INTERNAL_AT,
                "deleted" if deleted else "active",
            ),
        )
    return source_path


def _fixture(tmp_path: Path) -> dict[str, Any]:
    paths = BrainPaths.from_value(tmp_path / "private-brain")
    init_db(paths.sqlite_path)
    source = _add_document(paths)
    key_path = tmp_path / "private-hmac.key"
    key = b"k" * 32
    _private_write(key_path, key)
    return {
        "paths": paths,
        "source": source,
        "key_path": key_path,
        "key": key,
        "output": tmp_path / "frozen-baseline",
    }


def test_freezer_emits_opaque_authenticated_private_baseline(tmp_path: Path) -> None:
    files = _fixture(tmp_path)

    result = baseline.freeze_gmail_temporal_development_baseline(
        files["paths"].home,
        files["key_path"],
        files["output"],
        include_message_count_commitments=True,
    )

    assert result["active_documents"] == 1
    assert result["thread_scopes"] == 1
    assert result["deleted_documents"] == 0
    assert result["deleted_thread_scopes"] == 0
    assert result["active_target_messages"] == 1
    assert result["aggregate_only"] is True
    assert result["external_calls"] == 0
    assert result["persistence_calls"] == 0
    assert stat.S_IMODE(files["output"].stat().st_mode) == 0o700
    for name in baseline.OUTPUT_ARTIFACT_NAMES:
        assert stat.S_IMODE((files["output"] / name).stat().st_mode) == 0o600

    scope = json.loads((files["output"] / baseline.THREAD_SCOPE_ARTIFACT).read_text())
    manifest = json.loads((files["output"] / baseline.MANIFEST_ARTIFACT).read_text())
    assert scope["thread_scope_id"] == baseline._thread_scope_id(
        files["key"], ACCOUNT, THREAD
    )
    assert scope["thread_scope_id"].startswith("gtdb_t_")
    assert scope["source_authority_commitment"].startswith("gtdb_a_")
    assert scope["message_count_commitment"].startswith("gtdb_n_")
    manifest_hmac = manifest.pop("manifest_hmac")
    assert manifest_hmac == baseline._manifest_hmac(files["key"], manifest)
    assert (
        manifest["policy_namespace"]
        == "gmail_projection_v7_classifier_v5_message_policy_v1"
    )
    assert (
        manifest["thread_scope_artifact"]["sha256"]
        == hashlib.sha256(
            (files["output"] / baseline.THREAD_SCOPE_ARTIFACT).read_bytes()
        ).hexdigest()
    )


def test_freezer_includes_deleted_tombstone_scopes(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    _add_document(
        files["paths"],
        revision_character="d",
        thread_id="private-deleted-thread",
        message_id="private-deleted-message",
        deleted=True,
    )

    result = baseline.freeze_gmail_temporal_development_baseline(
        files["paths"].home,
        files["key_path"],
        files["output"],
    )
    manifest = json.loads((files["output"] / baseline.MANIFEST_ARTIFACT).read_text())

    assert result["documents"] == 2
    assert result["active_documents"] == 1
    assert result["deleted_documents"] == 1
    assert result["thread_scopes"] == 2
    assert result["deleted_thread_scopes"] == 1
    assert result["active_target_messages"] == 1
    assert manifest["thread_scope_count"] == 2
    assert manifest["active_thread_scope_count"] == 1
    assert manifest["deleted_thread_scope_count"] == 1

    serialized = b"".join(
        (files["output"] / name).read_bytes() for name in baseline.OUTPUT_ARTIFACT_NAMES
    )
    for private_value in (
        ACCOUNT,
        THREAD,
        MESSAGE,
        PRIVATE_TEXT,
        str(files["paths"].home),
        str(files["source"]),
    ):
        assert private_value.encode() not in serialized


def test_thread_scope_is_revision_independent_and_counts_are_optional(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path)
    output = files["output"]
    baseline.freeze_gmail_temporal_development_baseline(
        files["paths"].home,
        files["key_path"],
        output,
    )
    scope = json.loads((output / baseline.THREAD_SCOPE_ARTIFACT).read_text())

    assert "message_count_commitment" not in scope
    assert baseline._thread_scope_id(
        files["key"], ACCOUNT, THREAD
    ) == baseline._thread_scope_id(files["key"], ACCOUNT, THREAD)
    assert baseline._thread_scope_id(
        files["key"], ACCOUNT, THREAD
    ) != baseline._thread_scope_id(files["key"], ACCOUNT, "a-different-thread")


def test_corpus_fingerprint_is_independent_of_optional_count_artifact(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path)
    without_counts = baseline.freeze_gmail_temporal_development_baseline(
        files["paths"].home,
        files["key_path"],
        tmp_path / "without-counts",
    )
    with_counts = baseline.freeze_gmail_temporal_development_baseline(
        files["paths"].home,
        files["key_path"],
        tmp_path / "with-counts",
        include_message_count_commitments=True,
    )

    assert without_counts["corpus_fingerprint"] == with_counts["corpus_fingerprint"]
    assert without_counts["artifact_set_sha256"] != with_counts["artifact_set_sha256"]


def test_freezer_fails_closed_on_stale_projection_authority(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    source = files["source"]
    text = source.read_text().replace(
        f"gmail_projection_version: {GMAIL_KNOWLEDGE_PROJECTION_VERSION}",
        f"gmail_projection_version: {GMAIL_KNOWLEDGE_PROJECTION_VERSION - 1}",
    )
    source.write_text(text, encoding="utf-8")
    with connection(files["paths"].sqlite_path) as conn:
        conn.execute(
            "UPDATE documents SET content_hash = ?",
            (hashlib.sha256(text.encode()).hexdigest(),),
        )

    with pytest.raises(
        baseline.GmailTemporalDevelopmentBaselineError,
        match="projection-v7 Gmail authority is incomplete",
    ):
        baseline.freeze_gmail_temporal_development_baseline(
            files["paths"].home,
            files["key_path"],
            files["output"],
        )
    assert not files["output"].exists()


def test_freezer_rejects_unsafe_key_and_existing_output(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    os.chmod(files["key_path"], 0o644)
    with pytest.raises(
        baseline.GmailTemporalDevelopmentBaselineError,
        match="owner-only single-link",
    ):
        baseline.freeze_gmail_temporal_development_baseline(
            files["paths"].home,
            files["key_path"],
            files["output"],
        )

    os.chmod(files["key_path"], 0o600)
    baseline.freeze_gmail_temporal_development_baseline(
        files["paths"].home,
        files["key_path"],
        files["output"],
    )
    before = {
        name: (files["output"] / name).read_bytes()
        for name in baseline.OUTPUT_ARTIFACT_NAMES
    }
    with pytest.raises(
        baseline.GmailTemporalDevelopmentBaselineError,
        match="already exists",
    ):
        baseline.freeze_gmail_temporal_development_baseline(
            files["paths"].home,
            files["key_path"],
            files["output"],
        )
    assert before == {
        name: (files["output"] / name).read_bytes()
        for name in baseline.OUTPUT_ARTIFACT_NAMES
    }


def test_freezer_rejects_symlink_hmac_key(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    linked_key = tmp_path / "linked-private-hmac.key"
    linked_key.symlink_to(files["key_path"])

    with pytest.raises(
        baseline.GmailTemporalDevelopmentBaselineError,
        match="HMAC key is unavailable",
    ):
        baseline.freeze_gmail_temporal_development_baseline(
            files["paths"].home,
            linked_key,
            files["output"],
        )
    assert not files["output"].exists()


def test_cli_failure_is_static_and_does_not_echo_private_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_missing_home = tmp_path / "private-secret-home"
    private_missing_key = tmp_path / "private-secret-key"
    private_output = tmp_path / "private-secret-output"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT_PATH),
            str(private_missing_home),
            str(private_missing_key),
            str(private_output),
        ],
    )

    with pytest.raises(SystemExit) as raised:
        baseline.main()

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.out == ""
    assert captured.err.strip() == baseline.STATIC_CLI_FAILURE
    assert "private-secret" not in captured.err
