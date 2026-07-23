from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

from pkm_brain.db import connection, init_db
from pkm_brain.gmail_projection import (
    GMAIL_KNOWLEDGE_CLASSIFIER_VERSION,
    GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    GMAIL_MESSAGE_POLICY_VERSION,
    gmail_projection_session_id,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.util import slugify


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "audit_gmail_temporal_runner.py"
SPEC = importlib.util.spec_from_file_location(
    "audit_gmail_temporal_runner", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
audit_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit_module
SPEC.loader.exec_module(audit_module)


ACCOUNT = "private-owner@example.test"
INTERNAL_AT = "2027-07-01T09:00:00-07:00"
SECRET_TEXT = "The private Atlas interview is scheduled for August 14, 2027."


def _add_document(
    paths: BrainPaths,
    *,
    document_id: str,
    message_id: str,
    thread_id: str,
    revision_character: str,
) -> Path:
    revision = revision_character * 64
    rendered = "\n".join(
        (
            f"## Message 1 — {INTERNAL_AT} — {message_id}",
            "",
            "From: private-sender@example.test",
            f"To: {ACCOUNT}",
            "Direction: incoming (test)",
            "Subject: Private Atlas interview",
            "",
            SECRET_TEXT,
        )
    )
    thread_heading = "# Email thread: Private audit fixture"
    message_start = len(thread_heading) + 2
    message_end = message_start + len(rendered)
    timestamps = [
        {
            "message_id": message_id,
            "internal_date": INTERNAL_AT,
            "start_offset": message_start,
            "end_offset": message_end,
        }
    ]
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
    markdown = "\n".join(
        (
            "---",
            "title: Private audit fixture",
            "source_type: gmail_thread",
            f"gmail_account_key: {ACCOUNT}",
            f"gmail_thread_id: {thread_id}",
            f"gmail_source_revision: {revision}",
            f"gmail_projection_version: {GMAIL_KNOWLEDGE_PROJECTION_VERSION}",
            f"gmail_classifier_version: {GMAIL_KNOWLEDGE_CLASSIFIER_VERSION}",
            f"gmail_message_ids: {json.dumps([message_id])}",
            "gmail_message_timestamps_version: 1",
            f"gmail_message_timestamps: {json.dumps(timestamps)}",
            f"gmail_message_policy_version: {GMAIL_MESSAGE_POLICY_VERSION}",
            f"gmail_message_policies: {json.dumps(policies)}",
            "retained_message_count: 1",
            f"gmail_fact_admitted_message_ids: {json.dumps([message_id])}",
            "fact_eligible: true",
            "fact_importance: durable_candidate",
            "actionability: informational",
            "delivery_kind: human",
            "classification: human",
            "importance_confidence: 0.99",
            "gmail_human_signal_basis: provider_sent",
            "deleted: false",
            "---",
            thread_heading,
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
    content_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO documents(
              id, source_type, title, source_path, raw_path, content_hash,
              created_at, ingested_at, tags, status
            ) VALUES (?, 'gmail_thread', 'Private audit fixture', ?, ?, ?, ?, ?,
                      '[]', 'active')
            """,
            (
                document_id,
                str(source_path),
                str(paths.raw / "private-missing.md"),
                content_hash,
                "2027-07-01T16:00:00+00:00",
                "2027-07-01T16:01:00+00:00",
            ),
        )
    return source_path


def _rewrite_source(
    paths: BrainPaths,
    *,
    document_id: str,
    source_path: Path,
    old: str,
    new: str,
) -> None:
    updated = source_path.read_text(encoding="utf-8").replace(old, new, 1)
    source_path.write_text(updated, encoding="utf-8")
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE documents SET content_hash = ? WHERE id = ?",
            (hashlib.sha256(updated.encode("utf-8")).hexdigest(), document_id),
        )


def test_runner_audit_is_content_free_and_preparation_only(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "private-brain-home")
    init_db(paths.sqlite_path)
    document_id = "private-document-identity"
    message_id = "private-message-identity"
    source_path = _add_document(
        paths,
        document_id=document_id,
        message_id=message_id,
        thread_id="private-thread-identity",
        revision_character="a",
    )

    result = audit_module.audit_gmail_temporal_runner(paths.home)

    assert result["version"] == "gmail_temporal_runner_audit_v1"
    assert result["fatal"] is False
    assert result["counts"]["active_documents"] == 1
    assert result["counts"]["discovered_messages"] == 1
    assert result["counts"]["prepared_messages"] == 1
    assert result["counts"]["candidate_bearing_messages"] == 1
    assert result["counts"]["requests"] == result["counts"]["pages"]
    assert result["admission"] == {"fact": 1}
    assert result["dispositions"] == {"complete_review_projection": 1}
    assert result["error_buckets"] == {}
    assert result["strata"]["delivery"]["human"]["counts"]["messages"] == 1
    assert result["strata"]["advertising"]["none"]["counts"]["messages"] == 1
    assert result["strata"]["relevance"]["human_signal"]["counts"]["messages"] == 1
    assert (
        result["volume_percentiles"]["candidate_bearing_messages"]["candidates"]["max"]
        == result["counts"]["candidates"]
    )
    assert result["external_calls"] == 0
    assert result["persistence_calls"] == 0
    assert result["private_content_printed"] is False
    assert result["request_payloads_printed"] is False

    serialized = json.dumps(result, sort_keys=True)
    private_values = (
        SECRET_TEXT,
        "Atlas",
        document_id,
        message_id,
        "private-thread-identity",
        ACCOUNT,
        str(paths.home),
        str(source_path),
        hashlib.sha256(SECRET_TEXT.encode("utf-8")).hexdigest(),
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
            conn.execute(
                "SELECT COUNT(*) FROM gmail_temporal_review_components"
            ).fetchone()[0]
            == 0
        )


def test_runner_audit_statically_buckets_malformed_sources(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "malformed-private-brain")
    init_db(paths.sqlite_path)
    stale_document = "stale-private-document"
    stale_message = "stale-private-message"
    stale_path = _add_document(
        paths,
        document_id=stale_document,
        message_id=stale_message,
        thread_id="stale-private-thread",
        revision_character="b",
    )
    _rewrite_source(
        paths,
        document_id=stale_document,
        source_path=stale_path,
        old=f"gmail_projection_version: {GMAIL_KNOWLEDGE_PROJECTION_VERSION}",
        new=f"gmail_projection_version: {GMAIL_KNOWLEDGE_PROJECTION_VERSION - 1}",
    )

    invalid_policy_document = "policy-private-document"
    invalid_policy_message = "policy-private-message"
    invalid_policy_path = _add_document(
        paths,
        document_id=invalid_policy_document,
        message_id=invalid_policy_message,
        thread_id="policy-private-thread",
        revision_character="c",
    )
    _rewrite_source(
        paths,
        document_id=invalid_policy_document,
        source_path=invalid_policy_path,
        old='"delivery_kind": "human"',
        new='"delivery_kind": "not-a-valid-policy-kind"',
    )

    result = audit_module.audit_gmail_temporal_runner(paths.home)

    assert result["fatal"] is False
    assert result["counts"]["active_documents"] == 2
    assert result["counts"]["discovered_messages"] == 2
    assert result["counts"]["failed_messages"] == 2
    assert result["counts"]["error_count"] == 2
    assert result["error_buckets"] == {
        "message_policy_invalid": 1,
        "stale_policy_version": 1,
    }
    assert result["admission"] == {}
    assert result["dispositions"] == {}
    serialized = json.dumps(result, sort_keys=True)
    assert "Gmail source policy version is stale" not in serialized
    assert "trusted Gmail message policy is invalid" not in serialized
    assert SECRET_TEXT not in serialized
    assert stale_document not in serialized
    assert stale_message not in serialized
    assert invalid_policy_document not in serialized
    assert invalid_policy_message not in serialized
