from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "author_gmail_temporal_retrieval_queries.py"
EVALUATOR_PATH = ROOT / "scripts" / "evaluate_gmail_temporal_retrieval_holdout.py"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


authoring = _load(SCRIPT_PATH, "test_gmail_temporal_retrieval_query_authoring")
evaluator = _load(EVALUATOR_PATH, "test_query_authoring_retrieval_evaluator")


def _private_file(path: Path, raw: bytes) -> None:
    path.write_bytes(raw)
    path.chmod(0o600)


def _mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def _source_authority(
    root: Path,
    *,
    key: bytes,
    thread_count: int = 80,
    message_text_by_thread: dict[int, str] | None = None,
    messages_per_thread: int = 1,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    root.mkdir()
    root.chmod(0o700)
    source_rows: list[dict[str, Any]] = []
    binding_rows: list[dict[str, Any]] = []
    for index in range(thread_count):
        account = "account-owner"
        thread_id = f"thread-{index:03d}"
        document_id = f"document-{index:03d}"
        thread_scope_id = authoring._opaque_id(  # noqa: SLF001
            key,
            kind="thread",
            values=(account, thread_id),
        )
        for message_index in range(messages_per_thread):
            message_id = f"message-{index:03d}-{message_index:02d}"
            source_id = authoring._opaque_id(  # noqa: SLF001
                key,
                kind="source",
                values=(account, message_id),
            )
            available_at = (
                f"2026-07-{(index % 20) + 1:02d}T{12 + message_index:02d}:00:00+00:00"
            )
            base_text = (message_text_by_thread or {}).get(
                index, f"source text {index}"
            )
            text = (
                base_text
                if messages_per_thread == 1
                else f"{base_text} message {message_index}"
            )
            chunks: list[dict[str, Any]] = []
            source_rows.append(
                {
                    "available_at": available_at,
                    "source_id": source_id,
                    "thread_scope_id": thread_scope_id,
                    "version": authoring.SOURCE_VERSION,
                }
            )
            binding_rows.append(
                {
                    "available_at": available_at,
                    "chunk_inventory_hmac_sha256": (
                        authoring._chunk_inventory_authenticator(  # noqa: SLF001
                            key,
                            source_id=source_id,
                            document_id=document_id,
                            gmail_account_key=account,
                            gmail_message_id=message_id,
                            gmail_thread_id=thread_id,
                            chunks=chunks,
                        )
                    ),
                    "chunks": chunks,
                    "document_content_sha256": hashlib.sha256(
                        f"document {index}".encode()
                    ).hexdigest(),
                    "document_id": document_id,
                    "gmail_account_key": account,
                    "gmail_message_id": message_id,
                    "gmail_thread_id": thread_id,
                    "message_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "source_id": source_id,
                    "version": authoring.BINDING_VERSION,
                }
            )
    source_rows.sort(key=lambda row: (row["thread_scope_id"], row["source_id"]))
    binding_rows.sort(key=lambda row: row["source_id"])
    source_raw = authoring._jsonl_bytes(source_rows)  # noqa: SLF001
    binding_raw = authoring._jsonl_bytes(binding_rows)  # noqa: SLF001
    unsigned = {
        "version": authoring.SOURCE_AUTHORITY_MANIFEST_VERSION,
        "builder_version": authoring.SOURCE_AUTHORITY_BUILDER_VERSION,
        "source_version": authoring.SOURCE_VERSION,
        "binding_version": authoring.BINDING_VERSION,
        "artifact_sha256": {
            authoring.SOURCE_ARTIFACT: authoring._sha256_bytes(source_raw),  # noqa: SLF001
            authoring.BINDING_ARTIFACT: authoring._sha256_bytes(binding_raw),  # noqa: SLF001
        },
        "document_count": thread_count,
        "message_count": len(source_rows),
        "chunk_count": 0,
        "source_scope": "every_message_in_every_active_trusted_gmail_projection",
        "source_identity": "hmac_account_and_provider_message_id",
        "thread_identity": "hmac_account_and_provider_thread_id",
        "header_chunk_clock": "latest_retained_message_provider_internal_date",
        "chunk_binding": (
            "authenticated_exact_chunk_id_range_text_sha256_and_source_assignment"
        ),
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
    }
    manifest = {
        **unsigned,
        "manifest_hmac_sha256": hmac.new(
            key,
            authoring.SOURCE_AUTHORITY_MANIFEST_DOMAIN
            + authoring._canonical_json(unsigned),  # noqa: SLF001
            hashlib.sha256,
        ).hexdigest(),
    }
    _private_file(root / authoring.SOURCE_ARTIFACT, source_raw)
    _private_file(root / authoring.BINDING_ARTIFACT, binding_raw)
    _private_file(
        root / authoring.SOURCE_MANIFEST_ARTIFACT,
        authoring._canonical_json(manifest) + b"\n",  # noqa: SLF001
    )
    return source_rows, binding_rows


def _key(tmp_path: Path) -> tuple[Path, bytes]:
    raw = b"query-authoring-test-key-32-bytes!!"
    assert len(raw) >= 32
    path = tmp_path / "key"
    _private_file(path, raw)
    return path, raw


def _eligibility_rows(*, thread_count: int = 80) -> list[dict[str, Any]]:
    kinds = (
        ["deadline"] * 14
        + ["lifecycle"] * 14
        + ["occurrence"] * 14
        + ["relative"] * 14
        + ["schedule"] * 12
        + ["timeline"] * 12
    )
    if thread_count != len(kinds):
        raise ValueError("test eligibility requires exactly eighty threads")
    lifecycle_classes = ("cancellation", "current_status", "reschedule")
    lifecycle_index = 0
    rows: list[dict[str, Any]] = []
    for index, kind in enumerate(kinds):
        lifecycle_class = None
        if kind == "lifecycle":
            lifecycle_class = lifecycle_classes[
                lifecycle_index % len(lifecycle_classes)
            ]
            lifecycle_index += 1
        rows.append(
            {
                "gmail_account_key": "account-owner",
                "gmail_thread_id": f"thread-{index:03d}",
                "lifecycle_query_class": lifecycle_class,
                "owner_attestations": {
                    name: True
                    for name in sorted(authoring.ELIGIBILITY_ATTESTATION_KEYS)
                },
                "source_only_owner_nominated": True,
                "temporal_query_kind": kind,
                "version": authoring.ELIGIBILITY_VERSION,
            }
        )
    return rows


def _eligibility(path: Path) -> Path:
    _private_file(
        path,
        authoring._jsonl_bytes(_eligibility_rows()),  # noqa: SLF001
    )
    return path


def _prepare(
    tmp_path: Path,
    authority: Path,
    key_path: Path,
    output: Path,
    *,
    brain_home: Path | None = None,
) -> tuple[dict[str, Any], Path]:
    eligibility = _eligibility(tmp_path / f"{output.name}-eligible.jsonl")
    result = authoring.prepare_worksheet(
        authority,
        key_path,
        output,
        brain_home=brain_home,
        eligible_threads_path=eligibility,
    )
    return result, eligibility


def _completed_rows(template_path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in template_path.read_text().splitlines()]
    for row in rows:
        option = row["source_options"][-1]
        row["query_text"] = f"What is the temporal state for item {row['ordinal']}?"
        row["as_of"] = option["available_at"]
        row["relevant_source_ids"] = [option["source_id"]]
        row["owner_attestations"] = {
            name: True for name in sorted(authoring.ATTESTATION_KEYS)
        }
    return rows


def _write_completed(path: Path, rows: list[dict[str, Any]]) -> None:
    _private_file(path, authoring._jsonl_bytes(rows))  # noqa: SLF001


def test_prepare_is_deterministic_private_and_suggestion_free(tmp_path: Path) -> None:
    key_path, key = _key(tmp_path)
    authority = tmp_path / "authority"
    _source_authority(authority, key=key)
    first = tmp_path / "worksheet-first"
    second = tmp_path / "worksheet-second"

    first_result, _first_eligibility = _prepare(tmp_path, authority, key_path, first)
    _prepare(tmp_path, authority, key_path, second)

    assert first_result["query_count"] == authoring.PRIMARY_QUERY_COUNT
    assert first_result["source_text_mode"] == (
        "authenticated_provider_identity_locator_only"
    )
    assert first_result["external_calls"] == first_result["model_calls"] == 0
    assert first_result["retrieval_calls"] == first_result["fact_search_calls"] == 0
    assert _mode(first) == 0o700
    assert all(_mode(path) == 0o600 for path in first.iterdir())
    assert (first / authoring.WORKSHEET_ARTIFACT).read_bytes() == (
        second / authoring.WORKSHEET_ARTIFACT
    ).read_bytes()
    rows = [
        json.loads(line)
        for line in (first / authoring.WORKSHEET_ARTIFACT).read_text().splitlines()
    ]
    assert len(rows) == 40
    assert len({row["thread_scope_id"] for row in rows}) == 40
    assert all(row["query_text"] == "" for row in rows)
    assert all(row["relevant_source_ids"] == [] for row in rows)
    assert all(row["context_source_ids"] == [] for row in rows)
    assert all(
        set(row["source_options"][0])
        == {"available_at", "gmail_message_id", "source_id"}
        for row in rows
    )
    assert {
        kind: sum(row["temporal_query_kind"] == kind for row in rows)
        for kind in authoring.TEMPORAL_QUERY_KINDS
    } == authoring.TEMPORAL_KIND_SELECTION_QUOTAS
    assert {
        row["lifecycle_query_class"]
        for row in rows
        if row["temporal_query_kind"] == "lifecycle"
    } == authoring.LIFECYCLE_QUERY_CLASSES
    manifest = json.loads((first / authoring.PREPARE_MANIFEST_ARTIFACT).read_text())
    assert manifest["retriever_or_fact_suggestions_included"] is False
    assert manifest["worksheet_owner_authored_fields_blank"] is True
    assert manifest["worksheet_protected_temporal_taxonomy"] is True
    assert manifest["primary_as_of_policy"] == (
        "at_or_after_latest_visible_thread_source"
    )
    assert manifest["historical_prefix_queries_allowed"] is False


def test_prepare_accepts_hmac_ranked_source_only_owner_eligibility(
    tmp_path: Path,
) -> None:
    key_path, key = _key(tmp_path)
    authority = tmp_path / "authority"
    _source_authority(authority, key=key)
    eligibility = _eligibility(tmp_path / "eligible.jsonl")
    output = tmp_path / "worksheet"

    authoring.prepare_worksheet(
        authority,
        key_path,
        output,
        eligible_threads_path=eligibility,
    )

    worksheet = [
        json.loads(line)
        for line in (output / authoring.WORKSHEET_ARTIFACT).read_text().splitlines()
    ]
    selected_threads = {row["provider_locator"]["gmail_thread_id"] for row in worksheet}
    assert len(selected_threads) == 40
    assert selected_threads < {f"thread-{index:03d}" for index in range(80)}
    eligibility_by_thread = {row["gmail_thread_id"]: row for row in _eligibility_rows()}
    assert all(
        row["temporal_query_kind"]
        == eligibility_by_thread[row["provider_locator"]["gmail_thread_id"]][
            "temporal_query_kind"
        ]
        and row["lifecycle_query_class"]
        == eligibility_by_thread[row["provider_locator"]["gmail_thread_id"]][
            "lifecycle_query_class"
        ]
        for row in worksheet
    )
    manifest = json.loads((output / authoring.PREPARE_MANIFEST_ARTIFACT).read_text())
    assert manifest["selection_population"] == (
        "owner_nominated_source_only_temporal_thread_authority"
    )
    assert manifest["eligibility_candidate_count"] == 80
    assert manifest["selection_temporal_kind_quotas"] == (
        authoring.TEMPORAL_KIND_SELECTION_QUOTAS
    )
    assert manifest["eligibility_sha256"] == authoring._sha256_bytes(  # noqa: SLF001
        eligibility.read_bytes()
    )


def test_prepare_requires_source_only_temporal_eligibility(tmp_path: Path) -> None:
    key_path, key = _key(tmp_path)
    authority = tmp_path / "authority"
    _source_authority(authority, key=key)

    with pytest.raises(
        authoring.GmailTemporalRetrievalQueryAuthoringError,
        match="eligibility authority is required",
    ):
        authoring.prepare_worksheet(authority, key_path, tmp_path / "worksheet")


def test_prepare_rejects_insufficient_temporal_stratum_depth(
    tmp_path: Path,
) -> None:
    key_path, key = _key(tmp_path)
    authority = tmp_path / "authority"
    _source_authority(authority, key=key)
    rows = _eligibility_rows()
    timeline = next(row for row in rows if row["temporal_query_kind"] == "timeline")
    timeline["temporal_query_kind"] = "schedule"
    eligibility = tmp_path / "eligible.jsonl"
    _private_file(eligibility, authoring._jsonl_bytes(rows))  # noqa: SLF001

    with pytest.raises(
        authoring.GmailTemporalRetrievalQueryAuthoringError,
        match="insufficient stratified depth",
    ):
        authoring.prepare_worksheet(
            authority,
            key_path,
            tmp_path / "worksheet",
            eligible_threads_path=eligibility,
        )


def test_prepare_rejects_insufficient_lifecycle_class_depth(
    tmp_path: Path,
) -> None:
    key_path, key = _key(tmp_path)
    authority = tmp_path / "authority"
    _source_authority(authority, key=key)
    rows = _eligibility_rows()
    cancellations = [
        row for row in rows if row["lifecycle_query_class"] == "cancellation"
    ]
    for row in cancellations[1:]:
        row["lifecycle_query_class"] = "current_status"
    eligibility = tmp_path / "eligible.jsonl"
    _private_file(eligibility, authoring._jsonl_bytes(rows))  # noqa: SLF001

    with pytest.raises(
        authoring.GmailTemporalRetrievalQueryAuthoringError,
        match="insufficient stratified depth",
    ):
        authoring.prepare_worksheet(
            authority,
            key_path,
            tmp_path / "worksheet",
            eligible_threads_path=eligibility,
        )


def test_prepare_rejects_candidate_prediction_attestation_gap(
    tmp_path: Path,
) -> None:
    key_path, key = _key(tmp_path)
    authority = tmp_path / "authority"
    _source_authority(authority, key=key)
    rows = _eligibility_rows()
    rows[0]["owner_attestations"]["predictions_not_consulted"] = False
    eligibility = tmp_path / "eligible.jsonl"
    _private_file(eligibility, authoring._jsonl_bytes(rows))  # noqa: SLF001

    with pytest.raises(
        authoring.GmailTemporalRetrievalQueryAuthoringError,
        match="eligibility authority is invalid",
    ):
        authoring.prepare_worksheet(
            authority,
            key_path,
            tmp_path / "worksheet",
            eligible_threads_path=eligibility,
        )


def test_prepare_rejects_tampered_source_authority(tmp_path: Path) -> None:
    key_path, key = _key(tmp_path)
    authority = tmp_path / "authority"
    _source_authority(authority, key=key)
    eligibility = _eligibility(tmp_path / "eligible.jsonl")
    binding_path = authority / authoring.BINDING_ARTIFACT
    rows = [json.loads(line) for line in binding_path.read_text().splitlines()]
    rows[0]["gmail_message_id"] = "identity-swapped"
    _private_file(binding_path, authoring._jsonl_bytes(rows))  # noqa: SLF001

    with pytest.raises(
        authoring.GmailTemporalRetrievalQueryAuthoringError,
        match="authentication failed",
    ):
        authoring.prepare_worksheet(
            authority,
            key_path,
            tmp_path / "worksheet",
            eligible_threads_path=eligibility,
        )


def test_verified_source_text_mode_binds_exact_local_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path, key = _key(tmp_path)
    text = "PRIVATE SOURCE MESSAGE"
    authority = tmp_path / "authority"
    _sources, bindings = _source_authority(
        authority,
        key=key,
        thread_count=40,
        message_text_by_thread={0: text},
    )
    selected_binding = next(
        row for row in bindings if row["gmail_thread_id"] == "thread-000"
    )
    brain_home = tmp_path / "brain"
    (brain_home / "db").mkdir(parents=True)
    projection = tmp_path / "projection.md"
    projection.write_text(f"---\nx: y\n---\n\n{text}", encoding="utf-8")
    with sqlite3.connect(brain_home / "db" / "brain.sqlite") as conn:
        conn.execute(
            """
            CREATE TABLE documents (
              id TEXT PRIMARY KEY,
              source_type TEXT,
              source_path TEXT,
              raw_path TEXT,
              content_hash TEXT,
              status TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?)",
            (
                selected_binding["document_id"],
                "gmail_thread",
                str(projection),
                str(projection),
                selected_binding["document_content_sha256"],
                "active",
            ),
        )
    monkeypatch.setattr(
        authoring,
        "source_frontmatter_with_path",
        lambda _document: (
            {
                "gmail_account_key": selected_binding["gmail_account_key"],
                "gmail_thread_id": selected_binding["gmail_thread_id"],
            },
            projection,
        ),
    )
    monkeypatch.setattr(
        authoring,
        "trusted_gmail_message_timestamps",
        lambda _document, _frontmatter, _path: [
            {
                "internal_date": selected_binding["available_at"],
                "message_id": selected_binding["gmail_message_id"],
                "start_offset": 0,
                "end_offset": len(text),
            }
        ],
    )

    result = authoring._verified_source_texts(  # noqa: SLF001
        brain_home, bindings=[selected_binding]
    )

    assert result == {selected_binding["source_id"]: text}


def test_prepare_embeds_verified_source_text_but_finalize_strips_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key_path, key = _key(tmp_path)
    authority = tmp_path / "authority"
    _source_authority(authority, key=key)
    monkeypatch.setattr(
        authoring,
        "_verified_source_texts",
        lambda _brain_home, *, bindings: {
            str(row["source_id"]): f"PRIVATE TEXT {row['source_id']}"
            for row in bindings
        },
    )
    worksheet = tmp_path / "worksheet"
    _prepare(
        tmp_path,
        authority,
        key_path,
        worksheet,
        brain_home=tmp_path / "brain",
    )
    template_raw = (worksheet / authoring.WORKSHEET_ARTIFACT).read_bytes()
    assert b"PRIVATE TEXT" in template_raw
    completed = tmp_path / "completed.jsonl"
    _write_completed(
        completed,
        _completed_rows(worksheet / authoring.WORKSHEET_ARTIFACT),
    )
    output = tmp_path / "query-authority"

    result = authoring.finalize_query_authority(worksheet, completed, key_path, output)

    query_raw = (output / authoring.QUERY_ARTIFACT).read_bytes()
    assert result["query_count"] == 40
    assert b"PRIVATE TEXT" not in query_raw
    assert b"gmail_message_id" not in query_raw
    assert b"provider_locator" not in query_raw
    assert _mode(output) == 0o700
    assert all(_mode(path) == 0o600 for path in output.iterdir())
    manifest = json.loads((output / authoring.FINAL_MANIFEST_ARTIFACT).read_text())
    assert manifest["private_source_text_in_query_artifact"] is False
    assert manifest["primary_as_of_policy"] == (
        "at_or_after_latest_visible_thread_source"
    )
    assert manifest["historical_prefix_queries_allowed"] is False


def test_finalize_emits_evaluator_compatible_query_v3_authority(
    tmp_path: Path,
) -> None:
    key_path, key = _key(tmp_path)
    authority = tmp_path / "authority"
    source_rows, _bindings = _source_authority(authority, key=key)
    worksheet = tmp_path / "worksheet"
    _prepare(tmp_path, authority, key_path, worksheet)
    completed = tmp_path / "completed.jsonl"
    _write_completed(
        completed,
        _completed_rows(worksheet / authoring.WORKSHEET_ARTIFACT),
    )
    output = tmp_path / "query-authority"

    result = authoring.finalize_query_authority(worksheet, completed, key_path, output)

    source_raw = authoring._jsonl_bytes(source_rows)  # noqa: SLF001
    _loaded_sources, source_by_id = evaluator._load_source_rows(source_raw)  # noqa: SLF001
    query_raw = (output / authoring.QUERY_ARTIFACT).read_bytes()
    rows, by_id, thread_scopes = evaluator._load_query_rows(  # noqa: SLF001
        query_raw,
        cohort="primary",
        sources=source_by_id,
    )
    kind_counts, lifecycle_counts, coverage = evaluator._primary_kind_coverage(  # noqa: SLF001
        rows
    )
    assert len(rows) == len(by_id) == len(thread_scopes) == 40
    assert coverage is True
    assert kind_counts == result["temporal_query_kind_counts"]
    assert lifecycle_counts == result["lifecycle_query_class_counts"]
    assert all(
        set(row)
        == {
            "as_of",
            "context_source_ids",
            "lifecycle_query_class",
            "query_id",
            "query_text",
            "relevant_source_ids",
            "temporal_query_kind",
            "thread_scope_id",
            "version",
        }
        for row in rows
    )


def test_finalize_rejects_protected_authority_change(tmp_path: Path) -> None:
    key_path, key = _key(tmp_path)
    authority = tmp_path / "authority"
    _source_authority(authority, key=key)
    worksheet = tmp_path / "worksheet"
    _prepare(tmp_path, authority, key_path, worksheet)
    rows = _completed_rows(worksheet / authoring.WORKSHEET_ARTIFACT)
    rows[0]["provider_locator"]["gmail_thread_id"] = "swapped-thread"
    completed = tmp_path / "completed.jsonl"
    _write_completed(completed, rows)

    with pytest.raises(
        authoring.GmailTemporalRetrievalQueryAuthoringError,
        match="protected authority",
    ):
        authoring.finalize_query_authority(
            worksheet, completed, key_path, tmp_path / "query-authority"
        )


def test_finalize_rejects_incomplete_source_only_attestation(tmp_path: Path) -> None:
    key_path, key = _key(tmp_path)
    authority = tmp_path / "authority"
    _source_authority(authority, key=key)
    worksheet = tmp_path / "worksheet"
    _prepare(tmp_path, authority, key_path, worksheet)
    rows = _completed_rows(worksheet / authoring.WORKSHEET_ARTIFACT)
    rows[0]["owner_attestations"]["retriever_not_consulted"] = False
    completed = tmp_path / "completed.jsonl"
    _write_completed(completed, rows)

    with pytest.raises(
        authoring.GmailTemporalRetrievalQueryAuthoringError,
        match="attestations are incomplete",
    ):
        authoring.finalize_query_authority(
            worksheet, completed, key_path, tmp_path / "query-authority"
        )


def test_finalize_rejects_as_of_before_latest_visible_thread_source(
    tmp_path: Path,
) -> None:
    key_path, key = _key(tmp_path)
    authority = tmp_path / "authority"
    _source_authority(authority, key=key, messages_per_thread=2)
    worksheet = tmp_path / "worksheet"
    _prepare(tmp_path, authority, key_path, worksheet)
    rows = _completed_rows(worksheet / authoring.WORKSHEET_ARTIFACT)
    assert len(rows[0]["source_options"]) == 2
    rows[0]["as_of"] = rows[0]["source_options"][0]["available_at"]
    completed = tmp_path / "completed.jsonl"
    _write_completed(completed, rows)

    with pytest.raises(
        authoring.GmailTemporalRetrievalQueryAuthoringError,
        match="as-of precedes visible source authority",
    ):
        authoring.finalize_query_authority(
            worksheet, completed, key_path, tmp_path / "query-authority"
        )


@pytest.mark.parametrize(
    "protected_value",
    [
        lambda row: row["source_options"][0]["source_id"],
        lambda row: row["source_options"][0]["gmail_message_id"],
        lambda row: row["provider_locator"]["gmail_thread_id"],
        lambda row: row["provider_locator"]["document_id"],
        lambda row: row["thread_scope_id"],
    ],
)
def test_finalize_rejects_protected_identifier_in_query_text(
    tmp_path: Path,
    protected_value: Any,
) -> None:
    key_path, key = _key(tmp_path)
    authority = tmp_path / "authority"
    _source_authority(authority, key=key)
    worksheet = tmp_path / "worksheet"
    _prepare(tmp_path, authority, key_path, worksheet)
    rows = _completed_rows(worksheet / authoring.WORKSHEET_ARTIFACT)
    rows[0]["query_text"] = f"What happened to {protected_value(rows[0])}?"
    completed = tmp_path / "completed.jsonl"
    _write_completed(completed, rows)

    with pytest.raises(
        authoring.GmailTemporalRetrievalQueryAuthoringError,
        match="query leaks protected authority",
    ):
        authoring.finalize_query_authority(
            worksheet, completed, key_path, tmp_path / "query-authority"
        )


def test_finalize_rejects_temporal_taxonomy_relabel(tmp_path: Path) -> None:
    key_path, key = _key(tmp_path)
    authority = tmp_path / "authority"
    _source_authority(authority, key=key)
    worksheet = tmp_path / "worksheet"
    _prepare(tmp_path, authority, key_path, worksheet)
    rows = _completed_rows(worksheet / authoring.WORKSHEET_ARTIFACT)
    rows[0]["temporal_query_kind"] = "occurrence"
    completed = tmp_path / "completed.jsonl"
    _write_completed(completed, rows)

    with pytest.raises(
        authoring.GmailTemporalRetrievalQueryAuthoringError,
        match="protected authority",
    ):
        authoring.finalize_query_authority(
            worksheet, completed, key_path, tmp_path / "query-authority"
        )


def test_finalize_rejects_lifecycle_class_relabel(tmp_path: Path) -> None:
    key_path, key = _key(tmp_path)
    authority = tmp_path / "authority"
    _source_authority(authority, key=key)
    worksheet = tmp_path / "worksheet"
    _prepare(tmp_path, authority, key_path, worksheet)
    rows = _completed_rows(worksheet / authoring.WORKSHEET_ARTIFACT)
    lifecycle_row = next(
        row for row in rows if row["temporal_query_kind"] == "lifecycle"
    )
    lifecycle_row["lifecycle_query_class"] = (
        "reschedule"
        if lifecycle_row["lifecycle_query_class"] != "reschedule"
        else "cancellation"
    )
    completed = tmp_path / "completed.jsonl"
    _write_completed(completed, rows)

    with pytest.raises(
        authoring.GmailTemporalRetrievalQueryAuthoringError,
        match="protected authority",
    ):
        authoring.finalize_query_authority(
            worksheet, completed, key_path, tmp_path / "query-authority"
        )
