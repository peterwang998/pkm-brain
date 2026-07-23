#!/usr/bin/env python3
"""Freeze, seal, and score the private Gmail temporal retrieval holdout.

The three phases deliberately use separate authenticated artifacts:

* ``freeze`` authenticates the builder manifest that jointly commits one
  complete private source authority and its exact source-to-Gmail binding file,
  then commits that upstream manifest alongside exactly forty primary,
  thread-disjoint global/cold temporal queries (plus an optional contextual
  follow-up challenge cohort);
* ``seal-run`` binds a retriever invocation to that exact authority and rejects
  missing query rows, mixed, duplicated, over-depth, or future-leaking ranked
  results; and
* ``score`` publishes only aggregate hit/recall metrics.  Challenge queries are
  always diagnostic and can never enter the primary metric denominator.

The primary retriever input contains only query identity, text, and its as-of
clock.  Query taxonomy, thread scope, context source identities, and relevance
gold remain sealed.  Context source identities may be exposed only in the
separate challenge artifact, whose metrics are diagnostic and never pooled with
the primary denominator.

The result is a retrospective prerequisite preview because the local index
receipt is not an authenticated upstream ingestion/index authority.  It never
claims release or promotion readiness.

The script is local-only.  It performs no model, network, or Brain writes.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VERSION = "gmail_temporal_retrieval_holdout_evaluator_v7"
BUNDLE_MANIFEST_VERSION = "gmail_temporal_retrieval_bundle_manifest_v7"
RUN_MANIFEST_VERSION = "gmail_temporal_retrieval_run_manifest_v7"
SCORE_MANIFEST_VERSION = "gmail_temporal_retrieval_score_manifest_v7"
SOURCE_VERSION = "gmail_temporal_retrieval_source_v1"
SOURCE_CONTROL_VERSION = "gmail_temporal_retrieval_source_control_v1"
BINDING_VERSION = "gmail_temporal_retrieval_source_binding_v3"
SOURCE_AUTHORITY_BUILDER_VERSION = (
    "gmail_temporal_retrieval_source_authority_builder_v3"
)
SOURCE_AUTHORITY_MANIFEST_VERSION = (
    "gmail_temporal_retrieval_source_authority_manifest_v2"
)
QUERY_VERSION = "gmail_temporal_retrieval_query_v3"
GOLD_VERSION = "gmail_temporal_retrieval_gold_v3"
RESULT_VERSION = "gmail_temporal_retrieval_result_v2"
SCORE_VERSION = "gmail_temporal_retrieval_score_v3"
RETRIEVER_RUNNER_VERSION = "gmail_temporal_retrieval_runner_v4"
RETRIEVER_CONFIG_VERSION = "gmail_temporal_retrieval_configuration_v4"
RETRIEVER_IMPLEMENTATION_VERSION = (
    "gmail_temporal_retrieval_implementation_provenance_v3"
)
RETROSPECTIVE_RETRIEVAL_VERSION = "retrospective_evidence_retrieval_v1"
INDEX_RECEIPT_VERSION = "gmail_temporal_retrieval_index_receipt_v2"

BUNDLE_MANIFEST_DOMAIN = b"gmail_temporal_retrieval_bundle_manifest_v7\0"
RUN_MANIFEST_DOMAIN = b"gmail_temporal_retrieval_run_manifest_v7\0"
SCORE_MANIFEST_DOMAIN = b"gmail_temporal_retrieval_score_manifest_v7\0"
SOURCE_AUTHORITY_MANIFEST_DOMAIN = (
    b"gmail_temporal_retrieval_source_authority_manifest_v2\0"
)
SOURCE_AUTHORITY_OPAQUE_ID_DOMAIN = b"gmail_temporal_retrieval_source_authority_v1\0"
CHUNK_INVENTORY_DOMAIN = b"gmail_temporal_retrieval_chunk_inventory_v1\0"
SOURCE_AUTHORITY_SOURCE_ARTIFACT = "source-authority.jsonl"
SOURCE_AUTHORITY_BINDING_ARTIFACT = "source-bindings.jsonl"

SOURCE_ARTIFACT = "blind-source-authority.jsonl"
SOURCE_CONTROL_ARTIFACT = "sealed-source-control.jsonl"
PRIMARY_QUERY_ARTIFACT = "blind-primary-queries.jsonl"
CHALLENGE_QUERY_ARTIFACT = "blind-challenge-queries.jsonl"
PRIMARY_GOLD_ARTIFACT = "sealed-primary-gold.jsonl"
CHALLENGE_GOLD_ARTIFACT = "sealed-challenge-gold.jsonl"
PRIMARY_RESULT_ARTIFACT = "primary-results.jsonl"
CHALLENGE_RESULT_ARTIFACT = "challenge-results.jsonl"
SCORE_ARTIFACT = "score.json"
MANIFEST_ARTIFACT = "manifest.json"

PRIMARY_QUERY_COUNT = 40
MIN_PRIMARY_QUERIES_PER_KIND = 5
MIN_PRIMARY_LIFECYCLE_QUERIES_PER_CLASS = 1
MIN_RESULT_DEPTH = 0
MAX_RESULT_DEPTH = 10
TOP_5_THRESHOLD = 0.90
TOP_10_THRESHOLD = 0.95
MACRO_RELEVANT_RECALL_AT_10_THRESHOLD = 0.90
COMPLETE_QUERY_RECALL_AT_10_THRESHOLD = 0.85
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
MIN_HMAC_KEY_BYTES = 32
MAX_HMAC_KEY_BYTES = 4096
MAX_IDENTIFIER_LENGTH = 256
MAX_QUERY_TEXT_LENGTH = 4_000

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)"
)
_TEMPORAL_QUERY_KINDS = {
    "deadline",
    "lifecycle",
    "occurrence",
    "relative",
    "schedule",
    "timeline",
}
_LIFECYCLE_QUERY_CLASSES = {
    "cancellation",
    "current_status",
    "reschedule",
}
_PRIMARY_BLIND_QUERY_KEYS = {
    "as_of",
    "query_id",
    "query_text",
    "version",
}
_CONTEXTUAL_BLIND_QUERY_KEYS = {
    *_PRIMARY_BLIND_QUERY_KEYS,
    "context_source_ids",
}
_SEALED_QUERY_CONTROL_KEYS = {
    "context_source_ids",
    "lifecycle_query_class",
    "query_id",
    "relevant_source_ids",
    "temporal_query_kind",
    "thread_scope_id",
    "version",
}
_PROVENANCE_ROLES = {
    "configuration",
    "implementation",
    "index_receipt",
    "query_protocol",
}
_INDEX_COMPONENT_KEYS = {
    "config_sha256",
    "embedding_stamp_sha256",
    "lancedb_tree_sha256",
    "sqlite_sha256",
    "sqlite_wal_sha256",
}
_QUERY_PROTOCOL_BYTES = (
    "gmail_temporal_retrieval_query_protocol_v3\n"
    "input=blind_query_id_query_text_as_of_only\n"
    "challenge_context=local_bound_message_text_only\n"
    "retrieval_api=BrainService.retrieve_retrospective_evidence\n"
    "output=unique_opaque_sources_zero_to_ten_no_padding\n"
    "future_and_context_sources=excluded\n"
    "telemetry=disabled\n"
).encode("utf-8")
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_GIT_IMPLEMENTATION_PATHS = {
    "retrospective_retrieval": "src/pkm_brain/retrospective_retrieval.py",
    "runner": "scripts/run_gmail_temporal_retrieval.py",
    "service": "src/pkm_brain/service.py",
}
_GIT_OBJECT_ID_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_GIT_COMMAND_TIMEOUT_SECONDS = 10


class GmailTemporalRetrievalHoldoutError(ValueError):
    """Raised when private retrieval benchmark evidence is invalid."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _jsonl_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical_json(dict(row)) + b"\n" for row in rows)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _evaluator_sha256() -> str:
    return _sha256_bytes(Path(__file__).read_bytes())


def _private_directory(path: Path, *, description: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GmailTemporalRetrievalHoldoutError(
            f"{description} is unavailable or unsafe"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != PRIVATE_DIRECTORY_MODE
    ):
        raise GmailTemporalRetrievalHoldoutError(
            f"{description} is unavailable or unsafe"
        )


def _private_file(path: Path, *, description: str) -> bytes:
    descriptor: int | None = None
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != PRIVATE_FILE_MODE
        ):
            raise GmailTemporalRetrievalHoldoutError(
                f"{description} is unavailable or unsafe"
            )
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise GmailTemporalRetrievalHoldoutError(
                f"{description} is unavailable or unsafe"
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            value = handle.read()
    except GmailTemporalRetrievalHoldoutError:
        raise
    except OSError as exc:
        raise GmailTemporalRetrievalHoldoutError(
            f"{description} is unavailable or unsafe"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return value


def _hmac_key(path: Path) -> bytes:
    value = _private_file(path, description="HMAC key")
    if not MIN_HMAC_KEY_BYTES <= len(value) <= MAX_HMAC_KEY_BYTES:
        raise GmailTemporalRetrievalHoldoutError("HMAC key is unavailable or unsafe")
    return value


def _parse_json(raw: bytes, *, description: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GmailTemporalRetrievalHoldoutError(f"{description} is malformed") from exc


def _canonical_jsonl(raw: bytes, *, description: str) -> list[dict[str, Any]]:
    if not raw or not raw.endswith(b"\n") or any(not line for line in raw.splitlines()):
        raise GmailTemporalRetrievalHoldoutError(f"{description} is malformed")
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        value = _parse_json(line, description=description)
        if not isinstance(value, dict) or line != _canonical_json(value):
            raise GmailTemporalRetrievalHoldoutError(f"{description} is malformed")
        rows.append(value)
    return rows


def _identifier(value: Any, *, description: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_IDENTIFIER_LENGTH
        or value.strip() != value
        or any(ord(character) < 0x20 for character in value)
    ):
        raise GmailTemporalRetrievalHoldoutError(f"{description} is invalid")
    return value


def _timestamp(value: Any, *, description: str) -> datetime:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise GmailTemporalRetrievalHoldoutError(f"{description} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GmailTemporalRetrievalHoldoutError(f"{description} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise GmailTemporalRetrievalHoldoutError(f"{description} is invalid")
    return parsed


def _load_source_rows(
    raw: bytes,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    rows = _canonical_jsonl(raw, description="source authority")
    by_id: dict[str, dict[str, Any]] = {}
    previous_key: tuple[str, str] | None = None
    required = {"available_at", "source_id", "thread_scope_id", "version"}
    for row in rows:
        if set(row) != required or row.get("version") != SOURCE_VERSION:
            raise GmailTemporalRetrievalHoldoutError("source authority is invalid")
        source_id = _identifier(row.get("source_id"), description="source identity")
        thread_scope_id = _identifier(
            row.get("thread_scope_id"), description="thread scope identity"
        )
        _timestamp(row.get("available_at"), description="source availability")
        key = (thread_scope_id, source_id)
        if source_id in by_id or (previous_key is not None and key <= previous_key):
            raise GmailTemporalRetrievalHoldoutError("source authority is invalid")
        previous_key = key
        by_id[source_id] = row
    if len(rows) < MAX_RESULT_DEPTH:
        raise GmailTemporalRetrievalHoldoutError("source authority is incomplete")
    return rows, by_id


def _expected_bound_source_id(key: bytes, account: str, message_id: str) -> str:
    material = _canonical_json(["source", account, message_id])
    digest = hmac.new(
        key,
        SOURCE_AUTHORITY_OPAQUE_ID_DOMAIN + material,
        hashlib.sha256,
    ).hexdigest()
    return f"gtrs_{digest[:32]}"


def _expected_bound_thread_scope_id(key: bytes, account: str, thread_id: str) -> str:
    material = _canonical_json(["thread", account, thread_id])
    digest = hmac.new(
        key,
        SOURCE_AUTHORITY_OPAQUE_ID_DOMAIN + material,
        hashlib.sha256,
    ).hexdigest()
    return f"gtrt_{digest[:32]}"


def _expected_chunk_inventory_authenticator(
    key: bytes,
    *,
    source_id: str,
    document_id: str,
    gmail_account_key: str,
    gmail_message_id: str,
    gmail_thread_id: str,
    chunks: Sequence[Mapping[str, Any]],
) -> str:
    material = {
        "chunks": [dict(chunk) for chunk in chunks],
        "document_id": document_id,
        "gmail_account_key": gmail_account_key,
        "gmail_message_id": gmail_message_id,
        "gmail_thread_id": gmail_thread_id,
        "source_id": source_id,
    }
    return hmac.new(
        key,
        CHUNK_INVENTORY_DOMAIN + _canonical_json(material),
        hashlib.sha256,
    ).hexdigest()


def _load_source_binding_rows(
    raw: bytes,
    *,
    key: bytes,
    sources: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], set[str], int]:
    rows = _canonical_jsonl(raw, description="source bindings")
    required = {
        "available_at",
        "chunk_inventory_hmac_sha256",
        "chunks",
        "document_content_sha256",
        "document_id",
        "gmail_account_key",
        "gmail_message_id",
        "gmail_thread_id",
        "message_sha256",
        "source_id",
        "version",
    }
    previous_source_id: str | None = None
    seen_sources: set[str] = set()
    seen_messages: set[tuple[str, str]] = set()
    document_hashes: dict[str, str] = {}
    seen_chunks: set[str] = set()
    for row in rows:
        if set(row) != required or row.get("version") != BINDING_VERSION:
            raise GmailTemporalRetrievalHoldoutError("source bindings are invalid")
        source_id = _identifier(
            row.get("source_id"), description="binding source identity"
        )
        account = _identifier(
            row.get("gmail_account_key"), description="binding account identity"
        )
        message_id = _identifier(
            row.get("gmail_message_id"), description="binding message identity"
        )
        thread_id = _identifier(
            row.get("gmail_thread_id"), description="binding thread identity"
        )
        document_id = _identifier(
            row.get("document_id"), description="binding document identity"
        )
        chunks = row.get("chunks")
        chunk_inventory_authenticator = row.get("chunk_inventory_hmac_sha256")
        if (
            not isinstance(chunks, list)
            or not isinstance(chunk_inventory_authenticator, str)
            or _SHA256_RE.fullmatch(chunk_inventory_authenticator) is None
        ):
            raise GmailTemporalRetrievalHoldoutError(
                "source binding chunk authority is invalid"
            )
        normalized_chunks: list[dict[str, Any]] = []
        for chunk in chunks:
            if not isinstance(chunk, dict) or set(chunk) != {
                "chunk_id",
                "end_offset",
                "start_offset",
                "text_sha256",
            }:
                raise GmailTemporalRetrievalHoldoutError(
                    "source binding chunk authority is invalid"
                )
            chunk_id = _identifier(
                chunk.get("chunk_id"), description="binding chunk identity"
            )
            start = chunk.get("start_offset")
            end = chunk.get("end_offset")
            text_sha256 = chunk.get("text_sha256")
            if (
                chunk_id in seen_chunks
                or type(start) is not int
                or type(end) is not int
                or start < 0
                or end <= start
                or not isinstance(text_sha256, str)
                or _SHA256_RE.fullmatch(text_sha256) is None
            ):
                raise GmailTemporalRetrievalHoldoutError(
                    "source binding chunk authority is invalid"
                )
            seen_chunks.add(chunk_id)
            normalized_chunks.append(dict(chunk))
        if normalized_chunks != sorted(
            normalized_chunks,
            key=lambda chunk: (
                chunk["start_offset"],
                chunk["end_offset"],
                chunk["chunk_id"],
            ),
        ) or not hmac.compare_digest(
            chunk_inventory_authenticator,
            _expected_chunk_inventory_authenticator(
                key,
                source_id=source_id,
                document_id=document_id,
                gmail_account_key=account,
                gmail_message_id=message_id,
                gmail_thread_id=thread_id,
                chunks=normalized_chunks,
            ),
        ):
            raise GmailTemporalRetrievalHoldoutError(
                "source binding chunk authority is invalid"
            )
        source = sources.get(source_id)
        document_hash = row.get("document_content_sha256")
        message_hash = row.get("message_sha256")
        if (
            source is None
            or source_id in seen_sources
            or (previous_source_id is not None and source_id <= previous_source_id)
            or row.get("available_at") != source.get("available_at")
            or not hmac.compare_digest(
                source_id, _expected_bound_source_id(key, account, message_id)
            )
            or not hmac.compare_digest(
                str(source.get("thread_scope_id") or ""),
                _expected_bound_thread_scope_id(key, account, thread_id),
            )
            or (account, message_id) in seen_messages
            or not isinstance(document_hash, str)
            or _SHA256_RE.fullmatch(document_hash) is None
            or not isinstance(message_hash, str)
            or _SHA256_RE.fullmatch(message_hash) is None
            or (
                document_id in document_hashes
                and document_hashes[document_id] != document_hash
            )
        ):
            raise GmailTemporalRetrievalHoldoutError(
                "source binding authority is invalid"
            )
        _timestamp(row.get("available_at"), description="binding availability")
        previous_source_id = source_id
        seen_sources.add(source_id)
        seen_messages.add((account, message_id))
        document_hashes[document_id] = document_hash
    if seen_sources != set(sources):
        raise GmailTemporalRetrievalHoldoutError(
            "source binding authority coverage is invalid"
        )
    return rows, set(document_hashes), len(seen_chunks)


def _load_source_authority_manifest(
    raw: bytes,
    *,
    key: bytes,
    source_raw: bytes,
    source_bindings_raw: bytes,
    sources: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    manifest = _parse_json(raw, description="source authority manifest")
    expected_keys = {
        "artifact_sha256",
        "binding_version",
        "builder_version",
        "chunk_binding",
        "chunk_count",
        "document_count",
        "external_calls",
        "header_chunk_clock",
        "manifest_hmac_sha256",
        "message_count",
        "persistence_calls",
        "private_content_printed",
        "source_identity",
        "source_scope",
        "source_version",
        "thread_identity",
        "version",
    }
    if (
        not isinstance(manifest, dict)
        or raw != _canonical_json(manifest) + b"\n"
        or set(manifest) != expected_keys
    ):
        raise GmailTemporalRetrievalHoldoutError("source authority manifest is invalid")
    authenticator = manifest.get("manifest_hmac_sha256")
    unsigned = dict(manifest)
    unsigned.pop("manifest_hmac_sha256", None)
    expected_authenticator = hmac.new(
        key,
        SOURCE_AUTHORITY_MANIFEST_DOMAIN + _canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()
    if not isinstance(authenticator, str) or not hmac.compare_digest(
        authenticator, expected_authenticator
    ):
        raise GmailTemporalRetrievalHoldoutError(
            "source authority manifest authentication failed"
        )
    artifact_hashes = manifest.get("artifact_sha256")
    expected_artifact_hashes = {
        SOURCE_AUTHORITY_SOURCE_ARTIFACT: _sha256_bytes(source_raw),
        SOURCE_AUTHORITY_BINDING_ARTIFACT: _sha256_bytes(source_bindings_raw),
    }
    required_policy = {
        "version": SOURCE_AUTHORITY_MANIFEST_VERSION,
        "builder_version": SOURCE_AUTHORITY_BUILDER_VERSION,
        "source_version": SOURCE_VERSION,
        "binding_version": BINDING_VERSION,
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
    if artifact_hashes != expected_artifact_hashes or any(
        manifest.get(name) != value for name, value in required_policy.items()
    ):
        raise GmailTemporalRetrievalHoldoutError(
            "source authority manifest policy is invalid"
        )
    binding_rows, document_ids, authenticated_chunk_count = _load_source_binding_rows(
        source_bindings_raw,
        key=key,
        sources=sources,
    )
    message_count = manifest.get("message_count")
    document_count = manifest.get("document_count")
    chunk_count = manifest.get("chunk_count")
    if (
        type(message_count) is not int
        or message_count != len(sources)
        or message_count != len(binding_rows)
        or type(document_count) is not int
        or document_count != len(document_ids)
        or type(chunk_count) is not int
        or chunk_count != authenticated_chunk_count
    ):
        raise GmailTemporalRetrievalHoldoutError(
            "source authority manifest counts are invalid"
        )
    return manifest


def _split_source_rows(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blind = sorted(
        (
            {
                "available_at": row["available_at"],
                "source_id": row["source_id"],
                "version": SOURCE_VERSION,
            }
            for row in rows
        ),
        key=lambda row: str(row["source_id"]),
    )
    control = sorted(
        (
            {
                "source_id": row["source_id"],
                "thread_scope_id": row["thread_scope_id"],
                "version": SOURCE_CONTROL_VERSION,
            }
            for row in rows
        ),
        key=lambda row: str(row["source_id"]),
    )
    return blind, control


def _join_blind_source_control(
    blind_raw: bytes,
    control_raw: bytes,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    blind_rows = _canonical_jsonl(blind_raw, description="blind source authority")
    control_rows = _canonical_jsonl(control_raw, description="sealed source control")
    if len(blind_rows) != len(control_rows):
        raise GmailTemporalRetrievalHoldoutError(
            "blind source and sealed control coverage is invalid"
        )
    joined: list[dict[str, Any]] = []
    previous_id: str | None = None
    for blind, control in zip(blind_rows, control_rows, strict=True):
        source_id = _identifier(blind.get("source_id"), description="source identity")
        if (
            set(blind) != {"available_at", "source_id", "version"}
            or blind.get("version") != SOURCE_VERSION
            or set(control) != {"source_id", "thread_scope_id", "version"}
            or control.get("version") != SOURCE_CONTROL_VERSION
            or control.get("source_id") != source_id
            or (previous_id is not None and source_id <= previous_id)
        ):
            raise GmailTemporalRetrievalHoldoutError(
                "blind source and sealed control binding is invalid"
            )
        _timestamp(blind.get("available_at"), description="source availability")
        thread_scope_id = _identifier(
            control.get("thread_scope_id"), description="thread scope identity"
        )
        previous_id = source_id
        joined.append(
            {
                "available_at": blind["available_at"],
                "source_id": source_id,
                "thread_scope_id": thread_scope_id,
                "version": SOURCE_VERSION,
            }
        )
    joined.sort(key=lambda row: (row["thread_scope_id"], row["source_id"]))
    return _load_source_rows(_jsonl_bytes(joined))


def _string_list(
    value: Any,
    *,
    description: str,
    allow_empty: bool = False,
) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise GmailTemporalRetrievalHoldoutError(f"{description} is invalid")
    items = [_identifier(item, description=description) for item in value]
    if items != sorted(set(items)):
        raise GmailTemporalRetrievalHoldoutError(f"{description} is invalid")
    return items


def _load_query_rows(
    raw: bytes,
    *,
    cohort: str,
    sources: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], set[str]]:
    rows = _canonical_jsonl(raw, description=f"{cohort} query authority")
    required = {
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
    by_id: dict[str, dict[str, Any]] = {}
    thread_scopes: set[str] = set()
    previous_id: str | None = None
    for row in rows:
        if set(row) != required or row.get("version") != QUERY_VERSION:
            raise GmailTemporalRetrievalHoldoutError(
                f"{cohort} query authority is invalid"
            )
        query_id = _identifier(row.get("query_id"), description="query identity")
        thread_scope_id = _identifier(
            row.get("thread_scope_id"), description="thread scope identity"
        )
        query_text = row.get("query_text")
        if (
            not isinstance(query_text, str)
            or not query_text.strip()
            or len(query_text) > MAX_QUERY_TEXT_LENGTH
            or "\x00" in query_text
        ):
            raise GmailTemporalRetrievalHoldoutError(
                f"{cohort} query authority is invalid"
            )
        temporal_kind = row.get("temporal_query_kind")
        lifecycle_class = row.get("lifecycle_query_class")
        if (
            temporal_kind not in _TEMPORAL_QUERY_KINDS
            or (
                temporal_kind == "lifecycle"
                and lifecycle_class not in _LIFECYCLE_QUERY_CLASSES
            )
            or (temporal_kind != "lifecycle" and lifecycle_class is not None)
        ):
            raise GmailTemporalRetrievalHoldoutError(
                f"{cohort} query authority is invalid"
            )
        as_of = _timestamp(row.get("as_of"), description="query as-of time")
        relevant = _string_list(
            row.get("relevant_source_ids"), description="relevant source authority"
        )
        context = _string_list(
            row.get("context_source_ids"),
            description="query context authority",
            allow_empty=cohort == "primary",
        )
        if set(relevant) & set(context):
            raise GmailTemporalRetrievalHoldoutError(
                f"{cohort} query context overlaps sealed relevance gold"
            )
        for source_id in set(relevant) | set(context):
            source = sources.get(source_id)
            if source is None or source.get("thread_scope_id") != thread_scope_id:
                raise GmailTemporalRetrievalHoldoutError(
                    f"{cohort} query source coverage is invalid"
                )
            available_at = _timestamp(
                source.get("available_at"), description="source availability"
            )
            if available_at > as_of:
                raise GmailTemporalRetrievalHoldoutError(
                    f"{cohort} query contains future evidence"
                )
        if (
            query_id in by_id
            or thread_scope_id in thread_scopes
            or (previous_id is not None and query_id <= previous_id)
        ):
            raise GmailTemporalRetrievalHoldoutError(
                f"{cohort} query authority is invalid"
            )
        previous_id = query_id
        by_id[query_id] = row
        thread_scopes.add(thread_scope_id)
    return rows, by_id, thread_scopes


def _query_kind_counts(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, int], dict[str, int]]:
    kind_counts = {
        kind: sum(row.get("temporal_query_kind") == kind for row in rows)
        for kind in sorted(_TEMPORAL_QUERY_KINDS)
    }
    lifecycle_counts = {
        lifecycle_class: sum(
            row.get("temporal_query_kind") == "lifecycle"
            and row.get("lifecycle_query_class") == lifecycle_class
            for row in rows
        )
        for lifecycle_class in sorted(_LIFECYCLE_QUERY_CLASSES)
    }
    return kind_counts, lifecycle_counts


def _primary_kind_coverage(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, int], dict[str, int], bool]:
    kind_counts, lifecycle_counts = _query_kind_counts(rows)
    passed = all(
        count >= MIN_PRIMARY_QUERIES_PER_KIND for count in kind_counts.values()
    ) and all(
        count >= MIN_PRIMARY_LIFECYCLE_QUERIES_PER_CLASS
        for count in lifecycle_counts.values()
    )
    return kind_counts, lifecycle_counts, passed


def _split_query_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    cohort: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if cohort not in {"primary", "challenge"}:
        raise GmailTemporalRetrievalHoldoutError("query cohort is invalid")
    blind: list[dict[str, Any]] = []
    gold: list[dict[str, Any]] = []
    for row in rows:
        blind_row = {
            name: row[name]
            for name in (
                "as_of",
                "query_id",
                "query_text",
                "version",
            )
        }
        if cohort == "challenge":
            blind_row["context_source_ids"] = row["context_source_ids"]
        blind.append(blind_row)
        gold.append(
            {
                "context_source_ids": row["context_source_ids"],
                "lifecycle_query_class": row["lifecycle_query_class"],
                "query_id": row["query_id"],
                "relevant_source_ids": row["relevant_source_ids"],
                "temporal_query_kind": row["temporal_query_kind"],
                "thread_scope_id": row["thread_scope_id"],
                "version": GOLD_VERSION,
            }
        )
    return blind, gold


def _join_blind_gold_rows(
    blind_raw: bytes,
    gold_raw: bytes,
    *,
    cohort: str,
) -> list[dict[str, Any]]:
    if cohort not in {"primary", "challenge"}:
        raise GmailTemporalRetrievalHoldoutError("query cohort is invalid")
    blind_rows = _canonical_jsonl(blind_raw, description=f"{cohort} blind queries")
    gold_rows = _canonical_jsonl(gold_raw, description=f"{cohort} sealed gold")
    blind_keys = (
        _PRIMARY_BLIND_QUERY_KEYS
        if cohort == "primary"
        else _CONTEXTUAL_BLIND_QUERY_KEYS
    )
    if len(blind_rows) != len(gold_rows):
        raise GmailTemporalRetrievalHoldoutError(
            f"{cohort} blind query and sealed gold coverage is invalid"
        )
    joined: list[dict[str, Any]] = []
    for blind, gold in zip(blind_rows, gold_rows, strict=True):
        if (
            set(blind) != blind_keys
            or blind.get("version") != QUERY_VERSION
            or set(gold) != _SEALED_QUERY_CONTROL_KEYS
            or gold.get("version") != GOLD_VERSION
            or blind.get("query_id") != gold.get("query_id")
            or (
                cohort == "challenge"
                and blind.get("context_source_ids") != gold.get("context_source_ids")
            )
        ):
            raise GmailTemporalRetrievalHoldoutError(
                f"{cohort} blind query and sealed gold binding is invalid"
            )
        joined.append(
            {
                **blind,
                "context_source_ids": gold["context_source_ids"],
                "lifecycle_query_class": gold["lifecycle_query_class"],
                "relevant_source_ids": gold["relevant_source_ids"],
                "temporal_query_kind": gold["temporal_query_kind"],
                "thread_scope_id": gold["thread_scope_id"],
            }
        )
    return joined


def _manifest_bytes(manifest: Mapping[str, Any], *, key: bytes, domain: bytes) -> bytes:
    unsigned = _canonical_json(dict(manifest))
    authenticator = hmac.new(key, domain + unsigned, hashlib.sha256).hexdigest()
    return (
        _canonical_json({**dict(manifest), "manifest_hmac_sha256": authenticator})
        + b"\n"
    )


def _write_private_new(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        PRIVATE_FILE_MODE,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    os.chmod(path, PRIVATE_FILE_MODE)


def _publish(output_root: Path, artifacts: Mapping[str, bytes]) -> None:
    root = Path(output_root)
    parent = root.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise GmailTemporalRetrievalHoldoutError("output parent is unsafe")
    parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    if root.exists() or root.is_symlink():
        raise GmailTemporalRetrievalHoldoutError("output already exists")
    temporary = Path(tempfile.mkdtemp(prefix=f".{root.name}-", dir=parent))
    os.chmod(temporary, PRIVATE_DIRECTORY_MODE)
    try:
        for name, payload in sorted(artifacts.items()):
            if Path(name).name != name or name in {"", ".", ".."}:
                raise GmailTemporalRetrievalHoldoutError("output inventory is invalid")
            _write_private_new(temporary / name, payload)
        os.replace(temporary, root)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _load_authenticated_root(
    root: Path,
    *,
    key: bytes,
    domain: bytes,
    manifest_version: str,
    expected_artifacts: set[str],
    description: str,
) -> tuple[dict[str, Any], bytes, dict[str, bytes]]:
    _private_directory(root, description=description)
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise GmailTemporalRetrievalHoldoutError(
            f"{description} is unavailable or unsafe"
        ) from exc
    expected_names = {MANIFEST_ARTIFACT, *expected_artifacts}
    if {entry.name for entry in entries} != expected_names:
        raise GmailTemporalRetrievalHoldoutError(
            f"{description} inventory is not exact"
        )
    raw_manifest = _private_file(
        root / MANIFEST_ARTIFACT, description=f"{description} manifest"
    )
    value = _parse_json(raw_manifest, description=f"{description} manifest")
    if (
        not isinstance(value, dict)
        or raw_manifest != _canonical_json(value) + b"\n"
        or value.get("version") != manifest_version
    ):
        raise GmailTemporalRetrievalHoldoutError(f"{description} manifest is invalid")
    authenticator = value.get("manifest_hmac_sha256")
    unsigned = dict(value)
    unsigned.pop("manifest_hmac_sha256", None)
    expected_authenticator = hmac.new(
        key, domain + _canonical_json(unsigned), hashlib.sha256
    ).hexdigest()
    if not isinstance(authenticator, str) or not hmac.compare_digest(
        authenticator, expected_authenticator
    ):
        raise GmailTemporalRetrievalHoldoutError(
            f"{description} manifest authentication failed"
        )
    artifact_hashes = value.get("artifact_sha256")
    if (
        not isinstance(artifact_hashes, dict)
        or set(artifact_hashes) != expected_artifacts
    ):
        raise GmailTemporalRetrievalHoldoutError(
            f"{description} artifact commitment is invalid"
        )
    artifacts: dict[str, bytes] = {}
    for name in sorted(expected_artifacts):
        raw = _private_file(root / name, description=f"{description} artifact")
        digest = artifact_hashes.get(name)
        if (
            not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
            or not hmac.compare_digest(digest, _sha256_bytes(raw))
        ):
            raise GmailTemporalRetrievalHoldoutError(
                f"{description} artifact commitment failed"
            )
        artifacts[name] = raw
    return value, raw_manifest, artifacts


def freeze_retrieval_holdout(
    source_authority_path: Path,
    primary_queries_path: Path,
    hmac_key_path: Path,
    output_root: Path,
    *,
    source_bindings_path: Path,
    source_authority_manifest_path: Path,
    challenge_queries_path: Path | None = None,
) -> dict[str, Any]:
    """Freeze the source/query authority into an authenticated private bundle."""

    key = _hmac_key(Path(hmac_key_path))
    source_raw = _private_file(
        Path(source_authority_path), description="source authority"
    )
    source_bindings_raw = _private_file(
        Path(source_bindings_path), description="source bindings"
    )
    if not source_bindings_raw:
        raise GmailTemporalRetrievalHoldoutError("source bindings are invalid")
    source_authority_manifest_raw = _private_file(
        Path(source_authority_manifest_path),
        description="source authority manifest",
    )
    primary_raw = _private_file(
        Path(primary_queries_path), description="primary query authority"
    )
    challenge_raw = (
        _private_file(
            Path(challenge_queries_path), description="challenge query authority"
        )
        if challenge_queries_path is not None
        else None
    )
    source_rows, sources = _load_source_rows(source_raw)
    _load_source_authority_manifest(
        source_authority_manifest_raw,
        key=key,
        source_raw=source_raw,
        source_bindings_raw=source_bindings_raw,
        sources=sources,
    )
    primary_rows, primary_by_id, primary_threads = _load_query_rows(
        primary_raw, cohort="primary", sources=sources
    )
    if len(primary_rows) != PRIMARY_QUERY_COUNT:
        raise GmailTemporalRetrievalHoldoutError(
            "primary query authority must contain exactly forty queries"
        )
    primary_kind_counts, primary_lifecycle_counts, kind_coverage_passed = (
        _primary_kind_coverage(primary_rows)
    )
    if not kind_coverage_passed:
        raise GmailTemporalRetrievalHoldoutError(
            "primary query authority temporal-kind coverage is insufficient"
        )
    challenge_rows: list[dict[str, Any]] = []
    challenge_threads: set[str] = set()
    if challenge_raw is not None:
        challenge_rows, challenge_by_id, challenge_threads = _load_query_rows(
            challenge_raw, cohort="challenge", sources=sources
        )
        if not challenge_rows:
            raise GmailTemporalRetrievalHoldoutError(
                "challenge query authority is invalid"
            )
        if primary_threads & challenge_threads or set(primary_by_id) & set(
            challenge_by_id
        ):
            raise GmailTemporalRetrievalHoldoutError(
                "primary and challenge query authorities overlap"
            )

    challenge_kind_counts, challenge_lifecycle_counts = _query_kind_counts(
        challenge_rows
    )
    source_blind, source_control = _split_source_rows(source_rows)
    primary_blind, primary_gold = _split_query_rows(primary_rows, cohort="primary")
    challenge_blind, challenge_gold = _split_query_rows(
        challenge_rows,
        cohort="challenge",
    )
    artifacts = {
        SOURCE_ARTIFACT: _jsonl_bytes(source_blind),
        SOURCE_CONTROL_ARTIFACT: _jsonl_bytes(source_control),
        PRIMARY_QUERY_ARTIFACT: _jsonl_bytes(primary_blind),
        PRIMARY_GOLD_ARTIFACT: _jsonl_bytes(primary_gold),
    }
    if challenge_rows:
        artifacts[CHALLENGE_QUERY_ARTIFACT] = _jsonl_bytes(challenge_blind)
        artifacts[CHALLENGE_GOLD_ARTIFACT] = _jsonl_bytes(challenge_gold)
    manifest = {
        "version": BUNDLE_MANIFEST_VERSION,
        "evaluator_version": VERSION,
        "evaluator_sha256": _evaluator_sha256(),
        "artifact_sha256": {
            name: _sha256_bytes(raw) for name, raw in sorted(artifacts.items())
        },
        "source_binding_sha256": _sha256_bytes(source_bindings_raw),
        "source_authority_manifest_sha256": _sha256_bytes(
            source_authority_manifest_raw
        ),
        "source_count": len(source_rows),
        "primary_query_count": len(primary_rows),
        "primary_thread_group_count": len(primary_threads),
        "challenge_query_count": len(challenge_rows),
        "challenge_thread_group_count": len(challenge_threads),
        "query_grouping": "one_query_per_thread_scope",
        "blind_query_thread_scope_exposed": False,
        "blind_source_thread_scope_exposed": False,
        "blind_query_temporal_taxonomy_exposed": False,
        "primary_retrieval_mode": "global_cold_text_only",
        "primary_blind_query_contract": "query_id_query_text_as_of_only",
        "primary_blind_context_source_ids_exposed": False,
        "challenge_retrieval_mode": "contextual_follow_up_diagnostic",
        "challenge_blind_context_source_ids_exposed": bool(challenge_rows),
        "cohort_metrics_must_not_be_pooled": True,
        "context_relevance_overlap_allowed": False,
        "ranked_context_sources_allowed": False,
        "query_gold_coverage": (
            "complete_relevance_and_present_context_source_authority_binding"
        ),
        "primary_context_cardinality": "zero_or_more",
        "challenge_context_cardinality": "one_or_more_when_present",
        "gold_isolation": (
            "sealed_taxonomy_context_relevance_and_thread_control_scorer_only"
        ),
        "primary_temporal_query_kind_counts": primary_kind_counts,
        "primary_lifecycle_query_class_counts": primary_lifecycle_counts,
        "challenge_temporal_query_kind_counts": challenge_kind_counts,
        "challenge_lifecycle_query_class_counts": challenge_lifecycle_counts,
        "minimum_primary_queries_per_temporal_kind": MIN_PRIMARY_QUERIES_PER_KIND,
        "minimum_primary_lifecycle_queries_per_class": (
            MIN_PRIMARY_LIFECYCLE_QUERIES_PER_CLASS
        ),
        "primary_temporal_kind_coverage_passed": True,
        "future_leakage_policy": "all_query_evidence_available_at_or_before_as_of",
        "minimum_result_depth": MIN_RESULT_DEPTH,
        "maximum_result_depth": MAX_RESULT_DEPTH,
        "missing_result_ranks_scored_as_misses": True,
        "primary_release_denominator": PRIMARY_QUERY_COUNT,
        "challenge_diagnostic_only": True,
        "diagnostic_denominator": "primary_global_cold_only",
        "release_authority": "retrospective_preview_only_no_upstream_index_authority",
        "release_holdout_eligible": False,
        "private_file_mode": "0600",
        "private_directory_mode": "0700",
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    manifest_raw = _manifest_bytes(manifest, key=key, domain=BUNDLE_MANIFEST_DOMAIN)
    if (
        _private_file(Path(source_authority_path), description="source authority")
        != source_raw
        or _private_file(Path(source_bindings_path), description="source bindings")
        != source_bindings_raw
        or _private_file(
            Path(source_authority_manifest_path),
            description="source authority manifest",
        )
        != source_authority_manifest_raw
        or _private_file(
            Path(primary_queries_path), description="primary query authority"
        )
        != primary_raw
        or (
            challenge_queries_path is not None
            and _private_file(
                Path(challenge_queries_path),
                description="challenge query authority",
            )
            != challenge_raw
        )
    ):
        raise GmailTemporalRetrievalHoldoutError(
            "retrieval holdout evidence changed while read"
        )
    _publish(Path(output_root), {**artifacts, MANIFEST_ARTIFACT: manifest_raw})
    return {
        "version": VERSION,
        "status": "frozen",
        "sources": len(source_rows),
        "source_binding_committed": True,
        "source_authority_manifest_committed": True,
        "primary_queries": len(primary_rows),
        "primary_thread_groups": len(primary_threads),
        "challenge_queries": len(challenge_rows),
        "release_holdout_eligible": False,
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
    }


def _bundle_artifacts(manifest: Mapping[str, Any]) -> set[str]:
    challenge_count = manifest.get("challenge_query_count")
    if not isinstance(challenge_count, int) or isinstance(challenge_count, bool):
        raise GmailTemporalRetrievalHoldoutError("retrieval bundle policy is invalid")
    names = {
        SOURCE_ARTIFACT,
        SOURCE_CONTROL_ARTIFACT,
        PRIMARY_QUERY_ARTIFACT,
        PRIMARY_GOLD_ARTIFACT,
    }
    if challenge_count > 0:
        names.update({CHALLENGE_QUERY_ARTIFACT, CHALLENGE_GOLD_ARTIFACT})
    return names


def _load_bundle(
    root: Path, *, key: bytes
) -> tuple[
    dict[str, Any],
    bytes,
    dict[str, bytes],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    # Discover only whether the optional challenge artifact is present, then
    # authenticate the exact inventory and verify the manifest agrees.
    _private_directory(root, description="retrieval bundle")
    try:
        names = {entry.name for entry in root.iterdir()}
    except OSError as exc:
        raise GmailTemporalRetrievalHoldoutError(
            "retrieval bundle is unavailable or unsafe"
        ) from exc
    artifacts = {
        SOURCE_ARTIFACT,
        SOURCE_CONTROL_ARTIFACT,
        PRIMARY_QUERY_ARTIFACT,
        PRIMARY_GOLD_ARTIFACT,
    }
    if CHALLENGE_QUERY_ARTIFACT in names:
        artifacts.update({CHALLENGE_QUERY_ARTIFACT, CHALLENGE_GOLD_ARTIFACT})
    manifest, manifest_raw, raw_artifacts = _load_authenticated_root(
        root,
        key=key,
        domain=BUNDLE_MANIFEST_DOMAIN,
        manifest_version=BUNDLE_MANIFEST_VERSION,
        expected_artifacts=artifacts,
        description="retrieval bundle",
    )
    required_policy = {
        "evaluator_version": VERSION,
        "evaluator_sha256": _evaluator_sha256(),
        "primary_query_count": PRIMARY_QUERY_COUNT,
        "primary_thread_group_count": PRIMARY_QUERY_COUNT,
        "query_grouping": "one_query_per_thread_scope",
        "blind_query_thread_scope_exposed": False,
        "blind_source_thread_scope_exposed": False,
        "blind_query_temporal_taxonomy_exposed": False,
        "primary_retrieval_mode": "global_cold_text_only",
        "primary_blind_query_contract": "query_id_query_text_as_of_only",
        "primary_blind_context_source_ids_exposed": False,
        "challenge_retrieval_mode": "contextual_follow_up_diagnostic",
        "challenge_blind_context_source_ids_exposed": (
            bool(manifest.get("challenge_query_count"))
        ),
        "cohort_metrics_must_not_be_pooled": True,
        "context_relevance_overlap_allowed": False,
        "ranked_context_sources_allowed": False,
        "query_gold_coverage": (
            "complete_relevance_and_present_context_source_authority_binding"
        ),
        "primary_context_cardinality": "zero_or_more",
        "challenge_context_cardinality": "one_or_more_when_present",
        "gold_isolation": (
            "sealed_taxonomy_context_relevance_and_thread_control_scorer_only"
        ),
        "minimum_primary_queries_per_temporal_kind": MIN_PRIMARY_QUERIES_PER_KIND,
        "minimum_primary_lifecycle_queries_per_class": (
            MIN_PRIMARY_LIFECYCLE_QUERIES_PER_CLASS
        ),
        "primary_temporal_kind_coverage_passed": True,
        "future_leakage_policy": "all_query_evidence_available_at_or_before_as_of",
        "minimum_result_depth": MIN_RESULT_DEPTH,
        "maximum_result_depth": MAX_RESULT_DEPTH,
        "missing_result_ranks_scored_as_misses": True,
        "primary_release_denominator": PRIMARY_QUERY_COUNT,
        "challenge_diagnostic_only": True,
        "diagnostic_denominator": "primary_global_cold_only",
        "release_authority": "retrospective_preview_only_no_upstream_index_authority",
        "release_holdout_eligible": False,
        "private_file_mode": "0600",
        "private_directory_mode": "0700",
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    if any(manifest.get(name) != value for name, value in required_policy.items()):
        raise GmailTemporalRetrievalHoldoutError("retrieval bundle policy is invalid")
    expected_manifest_keys = {
        "artifact_sha256",
        "blind_query_temporal_taxonomy_exposed",
        "blind_query_thread_scope_exposed",
        "blind_source_thread_scope_exposed",
        "challenge_blind_context_source_ids_exposed",
        "challenge_lifecycle_query_class_counts",
        "challenge_diagnostic_only",
        "challenge_query_count",
        "challenge_retrieval_mode",
        "challenge_context_cardinality",
        "challenge_temporal_query_kind_counts",
        "challenge_thread_group_count",
        "cohort_metrics_must_not_be_pooled",
        "context_relevance_overlap_allowed",
        "diagnostic_denominator",
        "evaluator_sha256",
        "evaluator_version",
        "external_calls",
        "future_leakage_policy",
        "gold_isolation",
        "manifest_hmac_sha256",
        "minimum_primary_lifecycle_queries_per_class",
        "minimum_primary_queries_per_temporal_kind",
        "minimum_result_depth",
        "maximum_result_depth",
        "missing_result_ranks_scored_as_misses",
        "persistence_calls",
        "primary_blind_context_source_ids_exposed",
        "primary_blind_query_contract",
        "primary_context_cardinality",
        "primary_query_count",
        "primary_lifecycle_query_class_counts",
        "primary_release_denominator",
        "primary_retrieval_mode",
        "primary_temporal_kind_coverage_passed",
        "primary_temporal_query_kind_counts",
        "primary_thread_group_count",
        "private_content_printed",
        "private_directory_mode",
        "private_file_mode",
        "query_gold_coverage",
        "query_grouping",
        "ranked_context_sources_allowed",
        "release_authority",
        "release_holdout_eligible",
        "routable",
        "source_authority_manifest_sha256",
        "source_count",
        "source_binding_sha256",
        "version",
    }
    if (
        set(manifest) != expected_manifest_keys
        or any(
            type(manifest.get(name)) is not int or manifest[name] < 0
            for name in (
                "source_count",
                "primary_query_count",
                "primary_thread_group_count",
                "challenge_query_count",
                "challenge_thread_group_count",
                "minimum_result_depth",
                "maximum_result_depth",
                "primary_release_denominator",
                "external_calls",
                "persistence_calls",
            )
        )
        or not isinstance(manifest.get("release_holdout_eligible"), bool)
        or not isinstance(manifest.get("source_binding_sha256"), str)
        or _SHA256_RE.fullmatch(manifest["source_binding_sha256"]) is None
        or not isinstance(manifest.get("source_authority_manifest_sha256"), str)
        or _SHA256_RE.fullmatch(manifest["source_authority_manifest_sha256"]) is None
        or _bundle_artifacts(manifest) != artifacts
    ):
        raise GmailTemporalRetrievalHoldoutError("retrieval bundle policy is invalid")
    _source_rows, sources = _join_blind_source_control(
        raw_artifacts[SOURCE_ARTIFACT], raw_artifacts[SOURCE_CONTROL_ARTIFACT]
    )
    primary_joined = _join_blind_gold_rows(
        raw_artifacts[PRIMARY_QUERY_ARTIFACT],
        raw_artifacts[PRIMARY_GOLD_ARTIFACT],
        cohort="primary",
    )
    primary_rows, primary, primary_threads = _load_query_rows(
        _jsonl_bytes(primary_joined), cohort="primary", sources=sources
    )
    challenge: dict[str, dict[str, Any]] = {}
    challenge_threads: set[str] = set()
    if CHALLENGE_QUERY_ARTIFACT in artifacts:
        challenge_joined = _join_blind_gold_rows(
            raw_artifacts[CHALLENGE_QUERY_ARTIFACT],
            raw_artifacts[CHALLENGE_GOLD_ARTIFACT],
            cohort="challenge",
        )
        challenge_rows, challenge, challenge_threads = _load_query_rows(
            _jsonl_bytes(challenge_joined),
            cohort="challenge",
            sources=sources,
        )
        if not challenge_rows:
            raise GmailTemporalRetrievalHoldoutError(
                "retrieval bundle challenge authority is invalid"
            )
    if (
        len(primary_rows) != PRIMARY_QUERY_COUNT
        or len(primary_threads) != PRIMARY_QUERY_COUNT
        or primary_threads & challenge_threads
        or set(primary) & set(challenge)
        or manifest.get("source_count") != len(sources)
        or manifest.get("challenge_query_count") != len(challenge)
        or manifest.get("challenge_thread_group_count") != len(challenge_threads)
    ):
        raise GmailTemporalRetrievalHoldoutError("retrieval bundle coverage is invalid")
    primary_kind_counts, primary_lifecycle_counts, primary_kind_coverage = (
        _primary_kind_coverage(primary_rows)
    )
    challenge_kind_counts, challenge_lifecycle_counts = _query_kind_counts(
        list(challenge.values())
    )
    if (
        not primary_kind_coverage
        or manifest.get("primary_temporal_query_kind_counts") != primary_kind_counts
        or manifest.get("primary_lifecycle_query_class_counts")
        != primary_lifecycle_counts
        or manifest.get("challenge_temporal_query_kind_counts") != challenge_kind_counts
        or manifest.get("challenge_lifecycle_query_class_counts")
        != challenge_lifecycle_counts
    ):
        raise GmailTemporalRetrievalHoldoutError(
            "retrieval bundle temporal-kind coverage is invalid"
        )
    return manifest, manifest_raw, raw_artifacts, sources, primary, challenge


def _load_result_rows(
    raw: bytes,
    *,
    cohort: str,
    queries: Mapping[str, Mapping[str, Any]],
    sources: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    rows = _canonical_jsonl(raw, description=f"{cohort} retrieval results")
    required = {"query_id", "retrieved", "version"}
    ranked: dict[str, list[str]] = {}
    previous_id: str | None = None
    for row in rows:
        if set(row) != required or row.get("version") != RESULT_VERSION:
            raise GmailTemporalRetrievalHoldoutError(
                f"{cohort} retrieval results are invalid"
            )
        query_id = _identifier(row.get("query_id"), description="query identity")
        query = queries.get(query_id)
        retrieved = row.get("retrieved")
        if (
            query is None
            or query_id in ranked
            or (previous_id is not None and query_id <= previous_id)
            or not isinstance(retrieved, list)
            or not MIN_RESULT_DEPTH <= len(retrieved) <= MAX_RESULT_DEPTH
        ):
            raise GmailTemporalRetrievalHoldoutError(
                f"{cohort} retrieval result coverage is invalid"
            )
        previous_id = query_id
        source_ids: list[str] = []
        context_source_ids = set(query.get("context_source_ids", []))
        as_of = _timestamp(query.get("as_of"), description="query as-of time")
        for expected_rank, item in enumerate(retrieved, start=1):
            if not isinstance(item, dict) or set(item) != {"rank", "source_id"}:
                raise GmailTemporalRetrievalHoldoutError(
                    f"{cohort} retrieval results are invalid"
                )
            if type(item.get("rank")) is not int or item.get("rank") != expected_rank:
                raise GmailTemporalRetrievalHoldoutError(
                    f"{cohort} retrieval ranking is invalid"
                )
            source_id = _identifier(
                item.get("source_id"), description="retrieved source identity"
            )
            source = sources.get(source_id)
            if (
                source is None
                or source_id in source_ids
                or source_id in context_source_ids
            ):
                raise GmailTemporalRetrievalHoldoutError(
                    f"{cohort} retrieval source coverage or context isolation is invalid"
                )
            if (
                _timestamp(
                    source.get("available_at"), description="source availability"
                )
                > as_of
            ):
                raise GmailTemporalRetrievalHoldoutError(
                    f"{cohort} retrieval results contain future evidence"
                )
            source_ids.append(source_id)
        ranked[query_id] = source_ids
    if set(ranked) != set(queries):
        raise GmailTemporalRetrievalHoldoutError(
            f"{cohort} retrieval result coverage is invalid"
        )
    return rows, ranked


def _result_depth_summary(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    depths = [len(row["retrieved"]) for row in rows]
    return {
        "minimum_observed_result_depth": min(depths, default=0),
        "maximum_observed_result_depth": max(depths, default=0),
        "retrieved_result_count": sum(depths),
    }


def _validated_index_components(value: Any, *, description: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _INDEX_COMPONENT_KEYS:
        raise GmailTemporalRetrievalHoldoutError(f"{description} is invalid")
    for name, digest in value.items():
        if digest is not None and (
            not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None
        ):
            raise GmailTemporalRetrievalHoldoutError(f"{description} is invalid")
        if name in {"config_sha256", "sqlite_sha256"} and (
            not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None
        ):
            raise GmailTemporalRetrievalHoldoutError(f"{description} is invalid")
    return dict(value)


def _git_environment() -> dict[str, str]:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
    )
    return environment


def _git_output(repository_root: Path, arguments: Sequence[str]) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository_root), *arguments],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_environment(),
            timeout=_GIT_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GmailTemporalRetrievalHoldoutError(
            "authoritative Git implementation is unavailable"
        ) from exc
    if completed.returncode != 0:
        raise GmailTemporalRetrievalHoldoutError(
            "authoritative Git implementation is unavailable"
        )
    return completed.stdout


def _stable_worktree_source(path: Path) -> bytes:
    descriptor: int | None = None
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise GmailTemporalRetrievalHoldoutError(
            "authoritative production implementation is unavailable"
        )
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
            raise GmailTemporalRetrievalHoldoutError(
                "authoritative production implementation is unavailable"
            )
        descriptor = os.open(
            path,
            os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
        ):
            raise GmailTemporalRetrievalHoldoutError(
                "authoritative production implementation is unavailable"
            )
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw = handle.read()
            closed = os.fstat(handle.fileno())
            if (
                closed.st_dev != opened.st_dev
                or closed.st_ino != opened.st_ino
                or closed.st_size != opened.st_size
                or closed.st_mtime_ns != opened.st_mtime_ns
            ):
                raise GmailTemporalRetrievalHoldoutError(
                    "authoritative production implementation is unavailable"
                )
        if not raw:
            raise GmailTemporalRetrievalHoldoutError(
                "authoritative production implementation is unavailable"
            )
        return raw
    except GmailTemporalRetrievalHoldoutError:
        raise
    except OSError as exc:
        raise GmailTemporalRetrievalHoldoutError(
            "authoritative production implementation is unavailable"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _verified_git_implementation_sources(
    repository_root: Path,
    relative_paths: Mapping[str, str],
) -> dict[str, tuple[str, str]]:
    """Return sources only when fixed worktree files equal immutable HEAD blobs."""

    root = Path(repository_root).resolve()
    try:
        top_level = Path(
            _git_output(root, ["rev-parse", "--show-toplevel"]).decode("utf-8").strip()
        ).resolve()
        head = (
            _git_output(root, ["rev-parse", "--verify", "HEAD^{commit}"])
            .decode("ascii")
            .strip()
        )
    except (UnicodeError, OSError) as exc:
        raise GmailTemporalRetrievalHoldoutError(
            "authoritative Git implementation is unavailable"
        ) from exc
    if top_level != root or _GIT_OBJECT_ID_RE.fullmatch(head) is None:
        raise GmailTemporalRetrievalHoldoutError(
            "authoritative Git implementation is unavailable"
        )
    pathspecs = [relative_paths[role] for role in sorted(relative_paths)]
    if _git_output(
        root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *pathspecs],
    ):
        raise GmailTemporalRetrievalHoldoutError(
            "authoritative production implementation working tree is dirty"
        )

    sources: dict[str, tuple[str, str]] = {}
    for role, relative_path in sorted(relative_paths.items()):
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or Path(relative_path).is_absolute()
            or ".." in Path(relative_path).parts
        ):
            raise GmailTemporalRetrievalHoldoutError(
                "authoritative Git implementation path is invalid"
            )
        try:
            object_id = (
                _git_output(
                    root,
                    ["rev-parse", "--verify", f"{head}:{relative_path}"],
                )
                .decode("ascii")
                .strip()
            )
            index_object_id = (
                _git_output(root, ["rev-parse", "--verify", f":{relative_path}"])
                .decode("ascii")
                .strip()
            )
            object_type = _git_output(root, ["cat-file", "-t", object_id]).strip()
        except UnicodeError as exc:
            raise GmailTemporalRetrievalHoldoutError(
                "authoritative Git implementation is unavailable"
            ) from exc
        if (
            _GIT_OBJECT_ID_RE.fullmatch(object_id) is None
            or _GIT_OBJECT_ID_RE.fullmatch(index_object_id) is None
            or not hmac.compare_digest(object_id, index_object_id)
            or object_type != b"blob"
        ):
            raise GmailTemporalRetrievalHoldoutError(
                "authoritative production implementation index is dirty"
            )
        blob = _git_output(root, ["cat-file", "blob", object_id])
        worktree = _stable_worktree_source(root / relative_path)
        if worktree != blob:
            raise GmailTemporalRetrievalHoldoutError(
                "production implementation does not match authoritative Git HEAD"
            )
        try:
            source = blob.decode("utf-8")
        except UnicodeError as exc:
            raise GmailTemporalRetrievalHoldoutError(
                "authoritative Git implementation is unavailable"
            ) from exc
        if not source:
            raise GmailTemporalRetrievalHoldoutError(
                "authoritative Git implementation is unavailable"
            )
        sources[role] = (source, _sha256_bytes(blob))

    if _git_output(
        root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *pathspecs],
    ):
        raise GmailTemporalRetrievalHoldoutError(
            "authoritative production implementation working tree is dirty"
        )
    try:
        head_after = (
            _git_output(root, ["rev-parse", "--verify", "HEAD^{commit}"])
            .decode("ascii")
            .strip()
        )
    except UnicodeError as exc:
        raise GmailTemporalRetrievalHoldoutError(
            "authoritative Git implementation is unavailable"
        ) from exc
    if not hmac.compare_digest(head, head_after):
        raise GmailTemporalRetrievalHoldoutError(
            "authoritative Git HEAD changed during provenance verification"
        )
    return sources


def _authoritative_implementation_sources() -> dict[str, tuple[str, str]]:
    return _verified_git_implementation_sources(
        _REPOSITORY_ROOT,
        _GIT_IMPLEMENTATION_PATHS,
    )


def _load_implementation_provenance(raw: bytes) -> dict[str, Any]:
    value = _parse_json(raw, description="retriever implementation provenance")
    expected_keys = {
        "production_api",
        "retrospective_retrieval_sha256",
        "retrospective_retrieval_source",
        "runner_sha256",
        "runner_source",
        "service_sha256",
        "service_source",
        "version",
    }
    if (
        not isinstance(value, dict)
        or raw != _canonical_json(value) + b"\n"
        or set(value) != expected_keys
        or value.get("version") != RETRIEVER_IMPLEMENTATION_VERSION
        or value.get("production_api") != "BrainService.retrieve_retrospective_evidence"
    ):
        raise GmailTemporalRetrievalHoldoutError(
            "retriever implementation provenance is invalid"
        )
    authoritative_sources = _authoritative_implementation_sources()
    for prefix in ("runner", "service", "retrospective_retrieval"):
        source = value.get(f"{prefix}_source")
        digest = value.get(f"{prefix}_sha256")
        if (
            not isinstance(source, str)
            or not source
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
            or not hmac.compare_digest(digest, _sha256_bytes(source.encode("utf-8")))
        ):
            raise GmailTemporalRetrievalHoldoutError(
                "retriever implementation provenance is invalid"
            )
        authoritative_source, authoritative_digest = authoritative_sources[prefix]
        if source != authoritative_source or not hmac.compare_digest(
            digest, authoritative_digest
        ):
            raise GmailTemporalRetrievalHoldoutError(
                "retriever implementation provenance does not match "
                "authoritative production implementation"
            )
    return value


def _load_retriever_configuration(
    raw: bytes,
    *,
    bundle_manifest: Mapping[str, Any],
    implementation: Mapping[str, Any],
    implementation_raw: bytes,
) -> dict[str, Any]:
    value = _parse_json(raw, description="retriever configuration")
    expected_keys = {
        "binding_sha256",
        "brain_read_only",
        "context_source_filter",
        "fact_source_fusion",
        "future_source_filter",
        "implementation_provenance_sha256",
        "index_components",
        "live_source_index_artifact_sha256",
        "live_source_index_components",
        "mode",
        "production_retrieval_api",
        "production_retrieval_api_version",
        "result_depth_policy",
        "retrieval_execution",
        "retrospective_retrieval_sha256",
        "runner_sha256",
        "runner_version",
        "scratch_writes_discarded",
        "service_sha256",
        "snapshot_index_artifact_sha256",
        "snapshot_index_components",
        "source_recency_and_lineage",
        "telemetry_recorded",
        "temporal_fact_clock",
        "version",
    }
    static_policy = {
        "version": RETRIEVER_CONFIG_VERSION,
        "runner_version": RETRIEVER_RUNNER_VERSION,
        "brain_read_only": True,
        "retrieval_execution": "disposable_transactional_index_snapshot",
        "scratch_writes_discarded": True,
        "telemetry_recorded": False,
        "result_depth_policy": "zero_to_ten_no_padding",
        "fact_source_fusion": "ranked_facts_then_deterministic_source_chunks",
        "temporal_fact_clock": (
            "source_availability_replay_via_complete_fact_citation_cutoff"
        ),
        "source_recency_and_lineage": "disabled_for_as_of_replay_determinism",
        "future_source_filter": "available_at_lte_query_as_of",
        "context_source_filter": "excluded_from_ranked_results",
        "production_retrieval_api": "BrainService.retrieve_retrospective_evidence",
        "production_retrieval_api_version": RETROSPECTIVE_RETRIEVAL_VERSION,
    }
    expected_binding_sha256 = bundle_manifest.get("source_binding_sha256")
    if (
        not isinstance(value, dict)
        or raw != _canonical_json(value) + b"\n"
        or set(value) != expected_keys
        or value.get("mode") not in {"source", "temporal"}
        or any(value.get(name) != expected for name, expected in static_policy.items())
        or not isinstance(expected_binding_sha256, str)
        or _SHA256_RE.fullmatch(expected_binding_sha256) is None
        or not isinstance(value.get("binding_sha256"), str)
        or not hmac.compare_digest(value["binding_sha256"], expected_binding_sha256)
        or value.get("runner_sha256") != implementation.get("runner_sha256")
        or value.get("service_sha256") != implementation.get("service_sha256")
        or value.get("retrospective_retrieval_sha256")
        != implementation.get("retrospective_retrieval_sha256")
        or value.get("implementation_provenance_sha256")
        != _sha256_bytes(implementation_raw)
    ):
        raise GmailTemporalRetrievalHoldoutError(
            "retriever configuration contract is invalid"
        )
    snapshot_components = _validated_index_components(
        value.get("snapshot_index_components"),
        description="retriever snapshot index components",
    )
    index_components = _validated_index_components(
        value.get("index_components"),
        description="retriever index components",
    )
    live_components = _validated_index_components(
        value.get("live_source_index_components"),
        description="retriever live index components",
    )
    snapshot_commitment = _sha256_bytes(_canonical_json(snapshot_components))
    live_commitment = _sha256_bytes(_canonical_json(live_components))
    if (
        index_components != snapshot_components
        or value.get("snapshot_index_artifact_sha256") != snapshot_commitment
        or value.get("live_source_index_artifact_sha256") != live_commitment
    ):
        raise GmailTemporalRetrievalHoldoutError(
            "retriever index commitment is invalid"
        )
    return value


def _load_retriever_provenance(
    paths: Mapping[str, Path],
    *,
    bundle_manifest: Mapping[str, Any],
) -> tuple[dict[str, bytes], dict[str, str], str]:
    if set(paths) != _PROVENANCE_ROLES:
        raise GmailTemporalRetrievalHoldoutError("retriever provenance is incomplete")
    raw_by_role = {
        role: _private_file(Path(paths[role]), description="retriever provenance")
        for role in sorted(_PROVENANCE_ROLES)
    }
    if any(not raw for raw in raw_by_role.values()):
        raise GmailTemporalRetrievalHoldoutError("retriever provenance is invalid")
    if raw_by_role["query_protocol"] != _QUERY_PROTOCOL_BYTES:
        raise GmailTemporalRetrievalHoldoutError("retriever query protocol is invalid")
    implementation_raw = raw_by_role["implementation"]
    implementation = _load_implementation_provenance(implementation_raw)
    configuration = _load_retriever_configuration(
        raw_by_role["configuration"],
        bundle_manifest=bundle_manifest,
        implementation=implementation,
        implementation_raw=implementation_raw,
    )
    receipt_raw = raw_by_role["index_receipt"]
    receipt = _parse_json(receipt_raw, description="retrieval index receipt")
    expected_receipt_keys = {
        "blind_primary_queries_sha256",
        "index_artifact_sha256",
        "index_components",
        "snapshot_as_of",
        "source_authority_sha256",
        "source_count",
        "version",
    }
    if (
        not isinstance(receipt, dict)
        or receipt_raw != _canonical_json(receipt) + b"\n"
        or set(receipt) != expected_receipt_keys
        or receipt.get("version") != INDEX_RECEIPT_VERSION
        or receipt.get("source_authority_sha256")
        != bundle_manifest["artifact_sha256"][SOURCE_ARTIFACT]
        or receipt.get("blind_primary_queries_sha256")
        != bundle_manifest["artifact_sha256"][PRIMARY_QUERY_ARTIFACT]
        or type(receipt.get("source_count")) is not int
        or receipt.get("source_count") != bundle_manifest["source_count"]
        or receipt.get("index_artifact_sha256")
        != configuration.get("snapshot_index_artifact_sha256")
    ):
        raise GmailTemporalRetrievalHoldoutError("retrieval index receipt is invalid")
    receipt_components = _validated_index_components(
        receipt.get("index_components"),
        description="retrieval receipt index components",
    )
    if receipt_components != configuration.get("snapshot_index_components") or receipt[
        "index_artifact_sha256"
    ] != _sha256_bytes(_canonical_json(receipt_components)):
        raise GmailTemporalRetrievalHoldoutError("retrieval index receipt is invalid")
    _timestamp(receipt.get("snapshot_as_of"), description="index snapshot time")
    hashes = {role: _sha256_bytes(raw) for role, raw in sorted(raw_by_role.items())}
    fingerprint = _sha256_bytes(
        _canonical_json(
            {
                "version": "gmail_temporal_retriever_provenance_fingerprint_v1",
                "artifact_sha256": hashes,
            }
        )
    )
    return raw_by_role, hashes, fingerprint


def seal_retrieval_run(
    bundle_root: Path,
    primary_results_path: Path,
    hmac_key_path: Path,
    retriever_provenance_paths: Mapping[str, Path],
    output_root: Path,
    *,
    challenge_results_path: Path | None = None,
) -> dict[str, Any]:
    """Validate and seal one exact retrieval run against a frozen bundle."""

    key = _hmac_key(Path(hmac_key_path))
    (
        bundle_manifest,
        bundle_manifest_raw,
        bundle_artifacts,
        sources,
        primary_queries,
        challenge_queries,
    ) = _load_bundle(Path(bundle_root), key=key)
    provenance_raw, provenance_hashes, retriever_provenance_sha256 = (
        _load_retriever_provenance(
            retriever_provenance_paths,
            bundle_manifest=bundle_manifest,
        )
    )
    primary_raw = _private_file(
        Path(primary_results_path), description="primary retrieval results"
    )
    primary_rows, _primary_ranked = _load_result_rows(
        primary_raw,
        cohort="primary",
        queries=primary_queries,
        sources=sources,
    )
    challenge_raw: bytes | None = None
    challenge_rows: list[dict[str, Any]] = []
    if bool(challenge_queries) != (challenge_results_path is not None):
        raise GmailTemporalRetrievalHoldoutError(
            "challenge retrieval coverage is invalid"
        )
    if challenge_results_path is not None:
        challenge_raw = _private_file(
            Path(challenge_results_path), description="challenge retrieval results"
        )
        challenge_rows, _challenge_ranked = _load_result_rows(
            challenge_raw,
            cohort="challenge",
            queries=challenge_queries,
            sources=sources,
        )

    artifacts = {PRIMARY_RESULT_ARTIFACT: _jsonl_bytes(primary_rows)}
    if challenge_rows:
        artifacts[CHALLENGE_RESULT_ARTIFACT] = _jsonl_bytes(challenge_rows)
    result_depth_summary = _result_depth_summary([*primary_rows, *challenge_rows])
    manifest = {
        "version": RUN_MANIFEST_VERSION,
        "evaluator_version": VERSION,
        "evaluator_sha256": _evaluator_sha256(),
        "source_bundle_manifest_sha256": _sha256_bytes(bundle_manifest_raw),
        "source_bundle_manifest_hmac_sha256": bundle_manifest["manifest_hmac_sha256"],
        "source_artifact_sha256": dict(
            sorted(bundle_manifest["artifact_sha256"].items())
        ),
        "source_authority_manifest_sha256": bundle_manifest[
            "source_authority_manifest_sha256"
        ],
        "source_binding_sha256": bundle_manifest["source_binding_sha256"],
        "retriever_provenance_sha256": retriever_provenance_sha256,
        "retriever_provenance_artifact_sha256": provenance_hashes,
        "artifact_sha256": {
            name: _sha256_bytes(raw) for name, raw in sorted(artifacts.items())
        },
        "primary_query_count": len(primary_rows),
        "challenge_query_count": len(challenge_rows),
        "minimum_result_depth": MIN_RESULT_DEPTH,
        "maximum_result_depth": MAX_RESULT_DEPTH,
        "missing_result_ranks_scored_as_misses": True,
        **result_depth_summary,
        "query_coverage": "exact_frozen_query_authority",
        "source_coverage": "exact_frozen_source_authority",
        "future_leakage_policy": "retrieved_source_available_at_or_before_query_as_of",
        "primary_retrieval_mode": "global_cold_text_only",
        "primary_blind_query_contract": "query_id_query_text_as_of_only",
        "primary_blind_context_source_ids_exposed": False,
        "blind_query_temporal_taxonomy_exposed": False,
        "challenge_retrieval_mode": "contextual_follow_up_diagnostic",
        "challenge_blind_context_source_ids_exposed": bool(challenge_rows),
        "cohort_metrics_must_not_be_pooled": True,
        "challenge_diagnostic_only": True,
        "diagnostic_denominator": "primary_global_cold_only",
        "source_release_holdout_eligible": bundle_manifest["release_holdout_eligible"],
        "release_holdout_eligible": bundle_manifest["release_holdout_eligible"],
        "private_file_mode": "0600",
        "private_directory_mode": "0700",
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    manifest_raw = _manifest_bytes(manifest, key=key, domain=RUN_MANIFEST_DOMAIN)
    current_bundle = _load_bundle(Path(bundle_root), key=key)
    if (
        current_bundle[1] != bundle_manifest_raw
        or current_bundle[2] != bundle_artifacts
        or _private_file(
            Path(primary_results_path), description="primary retrieval results"
        )
        != primary_raw
        or (
            challenge_results_path is not None
            and _private_file(
                Path(challenge_results_path),
                description="challenge retrieval results",
            )
            != challenge_raw
        )
        or any(
            _private_file(
                Path(retriever_provenance_paths[role]),
                description="retriever provenance",
            )
            != raw
            for role, raw in provenance_raw.items()
        )
    ):
        raise GmailTemporalRetrievalHoldoutError(
            "retrieval run evidence changed while read"
        )
    _publish(Path(output_root), {**artifacts, MANIFEST_ARTIFACT: manifest_raw})
    return {
        "version": VERSION,
        "status": "sealed",
        "primary_queries": len(primary_rows),
        "challenge_queries": len(challenge_rows),
        "minimum_result_depth": MIN_RESULT_DEPTH,
        "maximum_result_depth": MAX_RESULT_DEPTH,
        **result_depth_summary,
        "release_holdout_eligible": bundle_manifest["release_holdout_eligible"],
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
    }


def _run_artifacts(manifest: Mapping[str, Any]) -> set[str]:
    challenge_count = manifest.get("challenge_query_count")
    if not isinstance(challenge_count, int) or isinstance(challenge_count, bool):
        raise GmailTemporalRetrievalHoldoutError("retrieval run policy is invalid")
    names = {PRIMARY_RESULT_ARTIFACT}
    if challenge_count > 0:
        names.add(CHALLENGE_RESULT_ARTIFACT)
    return names


def _load_run(
    root: Path,
    *,
    key: bytes,
    bundle_manifest: Mapping[str, Any],
    bundle_manifest_raw: bytes,
    bundle_artifacts: Mapping[str, bytes],
    sources: Mapping[str, Mapping[str, Any]],
    primary_queries: Mapping[str, Mapping[str, Any]],
    challenge_queries: Mapping[str, Mapping[str, Any]],
) -> tuple[
    dict[str, Any],
    bytes,
    dict[str, bytes],
    dict[str, list[str]],
    dict[str, list[str]],
]:
    _private_directory(root, description="retrieval run")
    try:
        names = {entry.name for entry in root.iterdir()}
    except OSError as exc:
        raise GmailTemporalRetrievalHoldoutError(
            "retrieval run is unavailable or unsafe"
        ) from exc
    artifact_names = {PRIMARY_RESULT_ARTIFACT}
    if CHALLENGE_RESULT_ARTIFACT in names:
        artifact_names.add(CHALLENGE_RESULT_ARTIFACT)
    manifest, manifest_raw, artifacts = _load_authenticated_root(
        root,
        key=key,
        domain=RUN_MANIFEST_DOMAIN,
        manifest_version=RUN_MANIFEST_VERSION,
        expected_artifacts=artifact_names,
        description="retrieval run",
    )
    required_policy = {
        "evaluator_version": VERSION,
        "evaluator_sha256": _evaluator_sha256(),
        "source_bundle_manifest_sha256": _sha256_bytes(bundle_manifest_raw),
        "source_bundle_manifest_hmac_sha256": bundle_manifest["manifest_hmac_sha256"],
        "source_artifact_sha256": dict(
            sorted(bundle_manifest["artifact_sha256"].items())
        ),
        "source_authority_manifest_sha256": bundle_manifest[
            "source_authority_manifest_sha256"
        ],
        "source_binding_sha256": bundle_manifest["source_binding_sha256"],
        "primary_query_count": PRIMARY_QUERY_COUNT,
        "challenge_query_count": len(challenge_queries),
        "minimum_result_depth": MIN_RESULT_DEPTH,
        "maximum_result_depth": MAX_RESULT_DEPTH,
        "missing_result_ranks_scored_as_misses": True,
        "query_coverage": "exact_frozen_query_authority",
        "source_coverage": "exact_frozen_source_authority",
        "future_leakage_policy": "retrieved_source_available_at_or_before_query_as_of",
        "primary_retrieval_mode": "global_cold_text_only",
        "primary_blind_query_contract": "query_id_query_text_as_of_only",
        "primary_blind_context_source_ids_exposed": False,
        "blind_query_temporal_taxonomy_exposed": False,
        "challenge_retrieval_mode": "contextual_follow_up_diagnostic",
        "challenge_blind_context_source_ids_exposed": bool(challenge_queries),
        "cohort_metrics_must_not_be_pooled": True,
        "challenge_diagnostic_only": True,
        "diagnostic_denominator": "primary_global_cold_only",
        "source_release_holdout_eligible": bundle_manifest["release_holdout_eligible"],
        "release_holdout_eligible": bundle_manifest["release_holdout_eligible"],
        "private_file_mode": "0600",
        "private_directory_mode": "0700",
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    expected_manifest_keys = {
        "artifact_sha256",
        "blind_query_temporal_taxonomy_exposed",
        "challenge_blind_context_source_ids_exposed",
        "challenge_diagnostic_only",
        "challenge_query_count",
        "challenge_retrieval_mode",
        "cohort_metrics_must_not_be_pooled",
        "diagnostic_denominator",
        "evaluator_sha256",
        "evaluator_version",
        "external_calls",
        "future_leakage_policy",
        "manifest_hmac_sha256",
        "maximum_observed_result_depth",
        "maximum_result_depth",
        "minimum_observed_result_depth",
        "minimum_result_depth",
        "missing_result_ranks_scored_as_misses",
        "persistence_calls",
        "primary_blind_context_source_ids_exposed",
        "primary_blind_query_contract",
        "primary_query_count",
        "primary_retrieval_mode",
        "private_content_printed",
        "private_directory_mode",
        "private_file_mode",
        "query_coverage",
        "release_holdout_eligible",
        "retrieved_result_count",
        "retriever_provenance_sha256",
        "retriever_provenance_artifact_sha256",
        "routable",
        "source_artifact_sha256",
        "source_authority_manifest_sha256",
        "source_binding_sha256",
        "source_bundle_manifest_hmac_sha256",
        "source_bundle_manifest_sha256",
        "source_coverage",
        "source_release_holdout_eligible",
        "version",
    }
    if (
        set(manifest) != expected_manifest_keys
        or any(
            type(manifest.get(name)) is not int or manifest[name] < 0
            for name in (
                "primary_query_count",
                "challenge_query_count",
                "minimum_result_depth",
                "maximum_result_depth",
                "minimum_observed_result_depth",
                "maximum_observed_result_depth",
                "retrieved_result_count",
                "external_calls",
                "persistence_calls",
            )
        )
        or any(manifest.get(name) != value for name, value in required_policy.items())
        or _run_artifacts(manifest) != artifact_names
        or not isinstance(manifest.get("retriever_provenance_sha256"), str)
        or _SHA256_RE.fullmatch(manifest["retriever_provenance_sha256"]) is None
        or not isinstance(manifest.get("retriever_provenance_artifact_sha256"), dict)
        or set(manifest["retriever_provenance_artifact_sha256"]) != _PROVENANCE_ROLES
        or any(
            not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None
            for digest in manifest["retriever_provenance_artifact_sha256"].values()
        )
    ):
        raise GmailTemporalRetrievalHoldoutError("retrieval run policy is invalid")
    primary_rows, primary_ranked = _load_result_rows(
        artifacts[PRIMARY_RESULT_ARTIFACT],
        cohort="primary",
        queries=primary_queries,
        sources=sources,
    )
    challenge_ranked: dict[str, list[str]] = {}
    challenge_rows: list[dict[str, Any]] = []
    if challenge_queries:
        challenge_rows, challenge_ranked = _load_result_rows(
            artifacts[CHALLENGE_RESULT_ARTIFACT],
            cohort="challenge",
            queries=challenge_queries,
            sources=sources,
        )
    result_depth_summary = _result_depth_summary([*primary_rows, *challenge_rows])
    if any(manifest.get(name) != value for name, value in result_depth_summary.items()):
        raise GmailTemporalRetrievalHoldoutError(
            "retrieval run result-depth policy is invalid"
        )
    if set(bundle_artifacts) != _bundle_artifacts(bundle_manifest):
        raise GmailTemporalRetrievalHoldoutError(
            "retrieval run source binding is invalid"
        )
    return manifest, manifest_raw, artifacts, primary_ranked, challenge_ranked


def _wilson_interval(numerator: int, denominator: int) -> dict[str, Any]:
    if (
        isinstance(numerator, bool)
        or isinstance(denominator, bool)
        or numerator < 0
        or denominator < 0
        or numerator > denominator
    ):
        raise GmailTemporalRetrievalHoldoutError("retrieval metric count is invalid")
    if denominator == 0:
        return {
            "numerator": numerator,
            "denominator": denominator,
            "estimate": None,
            "wilson_95_lower": None,
            "wilson_95_upper": None,
            "interval_defined": False,
        }
    z = 1.959963984540054
    estimate = numerator / denominator
    z_squared = z * z
    scale = 1.0 + z_squared / denominator
    center = (estimate + z_squared / (2.0 * denominator)) / scale
    radius = (
        z
        * math.sqrt(
            estimate * (1.0 - estimate) / denominator
            + z_squared / (4.0 * denominator * denominator)
        )
        / scale
    )
    return {
        "numerator": numerator,
        "denominator": denominator,
        "estimate": estimate,
        "wilson_95_lower": max(0.0, center - radius),
        "wilson_95_upper": min(1.0, center + radius),
        "interval_defined": True,
    }


def _retrieval_metrics(
    queries: Mapping[str, Mapping[str, Any]],
    ranked: Mapping[str, Sequence[str]],
) -> dict[str, Any]:
    hits_at_5 = 0
    hits_at_10 = 0
    complete_at_10 = 0
    relevant_total = 0
    relevant_at_5 = 0
    relevant_at_10 = 0
    macro_relevant_recall_at_5 = 0.0
    macro_relevant_recall_at_10 = 0.0
    reciprocal_rank_total = 0.0
    result_depths: list[int] = []
    for query_id, query in queries.items():
        relevant = set(query["relevant_source_ids"])
        retrieved = list(ranked[query_id])
        top_5 = set(retrieved[:5])
        top_10 = set(retrieved[:10])
        matched_at_5 = len(relevant & top_5)
        matched_at_10 = len(relevant & top_10)
        hits_at_5 += bool(relevant & top_5)
        hits_at_10 += bool(relevant & top_10)
        complete_at_10 += relevant <= top_10
        relevant_total += len(relevant)
        relevant_at_5 += matched_at_5
        relevant_at_10 += matched_at_10
        macro_relevant_recall_at_5 += matched_at_5 / len(relevant)
        macro_relevant_recall_at_10 += matched_at_10 / len(relevant)
        reciprocal_rank_total += next(
            (
                1.0 / rank
                for rank, source_id in enumerate(retrieved[:10], start=1)
                if source_id in relevant
            ),
            0.0,
        )
        result_depths.append(len(retrieved))
    count = len(queries)
    return {
        "queries": count,
        "thread_groups": count,
        "retrieved_sources": sum(result_depths),
        "queries_with_no_results": sum(depth == 0 for depth in result_depths),
        "minimum_result_depth": min(result_depths, default=None),
        "maximum_result_depth": max(result_depths, default=None),
        "mean_result_depth": sum(result_depths) / count if count else None,
        "hits_at_5": hits_at_5,
        "hits_at_10": hits_at_10,
        "query_hit_rate_at_5": hits_at_5 / count if count else None,
        "query_hit_rate_at_10": hits_at_10 / count if count else None,
        "query_hit_rate_at_5_interval_95": _wilson_interval(hits_at_5, count),
        "query_hit_rate_at_10_interval_95": _wilson_interval(hits_at_10, count),
        "relevant_sources": relevant_total,
        "relevant_source_recall_at_5": (
            relevant_at_5 / relevant_total if relevant_total else None
        ),
        "relevant_source_recall_at_10": (
            relevant_at_10 / relevant_total if relevant_total else None
        ),
        "macro_relevant_source_recall_at_5": (
            macro_relevant_recall_at_5 / count if count else None
        ),
        "macro_relevant_source_recall_at_10": (
            macro_relevant_recall_at_10 / count if count else None
        ),
        "mean_reciprocal_rank_at_10": (
            reciprocal_rank_total / count if count else None
        ),
        "complete_queries_at_10": complete_at_10,
        "complete_query_recall_at_10": complete_at_10 / count if count else None,
        "complete_query_recall_at_10_interval_95": _wilson_interval(
            complete_at_10, count
        ),
    }


def _metric_breakdown(
    queries: Mapping[str, Mapping[str, Any]],
    ranked: Mapping[str, Sequence[str]],
    *,
    field: str,
    values: Sequence[str],
) -> dict[str, dict[str, Any]]:
    return {
        value: _retrieval_metrics(
            {
                query_id: query
                for query_id, query in queries.items()
                if query.get(field) == value
            },
            ranked,
        )
        for value in values
    }


def _cohort_metrics(
    queries: Mapping[str, Mapping[str, Any]],
    ranked: Mapping[str, Sequence[str]],
    *,
    diagnostic_only: bool,
    retrieval_mode: str,
) -> dict[str, Any]:
    return {
        **_retrieval_metrics(queries, ranked),
        "temporal_query_kind_metrics": _metric_breakdown(
            queries,
            ranked,
            field="temporal_query_kind",
            values=sorted(_TEMPORAL_QUERY_KINDS),
        ),
        "lifecycle_query_class_metrics": _metric_breakdown(
            queries,
            ranked,
            field="lifecycle_query_class",
            values=sorted(_LIFECYCLE_QUERY_CLASSES),
        ),
        "retrieval_mode": retrieval_mode,
        "diagnostic_only": diagnostic_only,
        "included_in_primary_metric_denominator": not diagnostic_only,
        "metrics_pooled_with_other_cohort": False,
    }


def _assert_aggregate_only(
    value: Mapping[str, Any],
    *,
    private_values: set[str],
) -> None:
    raw = _canonical_json(dict(value)).decode("utf-8")
    if any(private_value and private_value in raw for private_value in private_values):
        raise GmailTemporalRetrievalHoldoutError(
            "retrieval score contains private benchmark content"
        )
    forbidden_fragments = ("_id", "path", "sha256", "query_text", "source_id")

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if any(
                    fragment in str(key).lower() for fragment in forbidden_fragments
                ):
                    raise GmailTemporalRetrievalHoldoutError(
                        "retrieval score contains private benchmark identity"
                    )
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)


def score_retrieval_holdout(
    bundle_root: Path,
    run_root: Path,
    hmac_key_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Authenticate one sealed run and publish aggregate-only retrieval gates."""

    key = _hmac_key(Path(hmac_key_path))
    (
        bundle_manifest,
        bundle_manifest_raw,
        bundle_artifacts,
        sources,
        primary_queries,
        challenge_queries,
    ) = _load_bundle(Path(bundle_root), key=key)
    (
        run_manifest,
        run_manifest_raw,
        run_artifacts,
        primary_ranked,
        challenge_ranked,
    ) = _load_run(
        Path(run_root),
        key=key,
        bundle_manifest=bundle_manifest,
        bundle_manifest_raw=bundle_manifest_raw,
        bundle_artifacts=bundle_artifacts,
        sources=sources,
        primary_queries=primary_queries,
        challenge_queries=challenge_queries,
    )
    primary_metrics = _cohort_metrics(
        primary_queries,
        primary_ranked,
        diagnostic_only=False,
        retrieval_mode="global_cold_text_only",
    )
    challenge_metrics = (
        _cohort_metrics(
            challenge_queries,
            challenge_ranked,
            diagnostic_only=True,
            retrieval_mode="contextual_follow_up_diagnostic",
        )
        if challenge_queries
        else None
    )
    top_5_passed = primary_metrics["query_hit_rate_at_5"] >= TOP_5_THRESHOLD
    top_10_passed = primary_metrics["query_hit_rate_at_10"] >= TOP_10_THRESHOLD
    macro_recall_at_10_passed = (
        primary_metrics["macro_relevant_source_recall_at_10"]
        >= MACRO_RELEVANT_RECALL_AT_10_THRESHOLD
    )
    complete_query_recall_at_10_passed = (
        primary_metrics["complete_query_recall_at_10"]
        >= COMPLETE_QUERY_RECALL_AT_10_THRESHOLD
    )
    retrieval_gate_passed = all(
        (
            top_5_passed,
            top_10_passed,
            macro_recall_at_10_passed,
            complete_query_recall_at_10_passed,
        )
    )
    diagnostic_prerequisite_checks = {
        "source_release_holdout_eligible": bundle_manifest["release_holdout_eligible"],
        "exact_forty_primary_thread_groups": len(primary_queries)
        == PRIMARY_QUERY_COUNT,
        "complete_query_rows_and_bounded_ranked_results": True,
        "future_leakage_absent": True,
        "thread_scope_taxonomy_context_and_gold_hidden_from_primary_input": True,
        "primary_global_cold_input_is_text_only": True,
        "contextual_follow_up_is_separate_diagnostic_and_non_pooled": True,
        "context_echo_absent": True,
        "temporal_kind_coverage": bundle_manifest[
            "primary_temporal_kind_coverage_passed"
        ],
        "top_5_query_hit_rate": top_5_passed,
        "top_10_query_hit_rate": top_10_passed,
        "macro_relevant_source_recall_at_10": macro_recall_at_10_passed,
        "complete_query_recall_at_10": complete_query_recall_at_10_passed,
    }
    # This evaluator binds a local index receipt, not an authenticated upstream
    # ingestion/index-authority chain.  Its metric can be a prerequisite, never
    # a release or promotion decision on its own.
    release_score_gate_passed = False
    score = {
        "version": SCORE_VERSION,
        "status": "scored",
        "primary": primary_metrics,
        "challenge": challenge_metrics,
        "primary_temporal_query_kind_counts": bundle_manifest[
            "primary_temporal_query_kind_counts"
        ],
        "primary_lifecycle_query_class_counts": bundle_manifest[
            "primary_lifecycle_query_class_counts"
        ],
        "minimum_primary_queries_per_temporal_kind": (MIN_PRIMARY_QUERIES_PER_KIND),
        "minimum_primary_lifecycle_queries_per_class": (
            MIN_PRIMARY_LIFECYCLE_QUERIES_PER_CLASS
        ),
        "primary_temporal_kind_coverage_passed": True,
        "required_query_hit_rate_at_5": TOP_5_THRESHOLD,
        "required_query_hit_rate_at_10": TOP_10_THRESHOLD,
        "required_macro_relevant_source_recall_at_10": (
            MACRO_RELEVANT_RECALL_AT_10_THRESHOLD
        ),
        "required_complete_query_recall_at_10": (COMPLETE_QUERY_RECALL_AT_10_THRESHOLD),
        "minimum_result_depth": MIN_RESULT_DEPTH,
        "maximum_result_depth": MAX_RESULT_DEPTH,
        "missing_result_ranks_scored_as_misses": True,
        "top_5_gate_passed": top_5_passed,
        "top_10_gate_passed": top_10_passed,
        "macro_relevant_source_recall_at_10_gate_passed": (macro_recall_at_10_passed),
        "complete_query_recall_at_10_gate_passed": (complete_query_recall_at_10_passed),
        "retrieval_gate_passed": retrieval_gate_passed,
        "retrieval_metric_prerequisite_passed": retrieval_gate_passed,
        "diagnostic_prerequisite_checks": diagnostic_prerequisite_checks,
        "source_release_holdout_eligible": bundle_manifest["release_holdout_eligible"],
        "release_score_gate_passed": release_score_gate_passed,
        "retrospective_preview_only": True,
        "promotion_pending": True,
        "release_or_promotion_claimed": False,
        "pending_final_authenticated_rollup": True,
        "confidence_interval_method": "wilson_score_95_two_sided",
        "primary_retrieval_mode": "global_cold_text_only",
        "primary_blind_query_contract": "query_id_query_text_as_of_only",
        "primary_context_hidden_from_blind_input": True,
        "blind_query_temporal_taxonomy_exposed": False,
        "challenge_retrieval_mode": "contextual_follow_up_diagnostic",
        "challenge_context_input_present": bool(challenge_queries),
        "challenge_diagnostic_only": True,
        "cohort_metrics_must_not_be_pooled": True,
        "diagnostic_denominator": "primary_global_cold_only",
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    private_values = {
        str(row.get(field, ""))
        for row in [
            *sources.values(),
            *primary_queries.values(),
            *challenge_queries.values(),
        ]
        for field in ("source_id", "thread_scope_id", "query_id", "query_text")
    }
    _assert_aggregate_only(score, private_values=private_values)
    score_raw = _canonical_json(score) + b"\n"
    manifest = {
        "version": SCORE_MANIFEST_VERSION,
        "evaluator_version": VERSION,
        "evaluator_sha256": _evaluator_sha256(),
        "source_bundle_manifest_sha256": _sha256_bytes(bundle_manifest_raw),
        "source_bundle_manifest_hmac_sha256": bundle_manifest["manifest_hmac_sha256"],
        "source_run_manifest_sha256": _sha256_bytes(run_manifest_raw),
        "source_run_manifest_hmac_sha256": run_manifest["manifest_hmac_sha256"],
        "retriever_provenance_sha256": run_manifest["retriever_provenance_sha256"],
        "artifact_sha256": {SCORE_ARTIFACT: _sha256_bytes(score_raw)},
        "primary_query_count": len(primary_queries),
        "challenge_query_count": len(challenge_queries),
        "primary_temporal_query_kind_counts": bundle_manifest[
            "primary_temporal_query_kind_counts"
        ],
        "primary_lifecycle_query_class_counts": bundle_manifest[
            "primary_lifecycle_query_class_counts"
        ],
        "minimum_primary_queries_per_temporal_kind": (MIN_PRIMARY_QUERIES_PER_KIND),
        "minimum_primary_lifecycle_queries_per_class": (
            MIN_PRIMARY_LIFECYCLE_QUERIES_PER_CLASS
        ),
        "primary_temporal_kind_coverage_passed": True,
        "top_5_threshold": TOP_5_THRESHOLD,
        "top_10_threshold": TOP_10_THRESHOLD,
        "macro_relevant_source_recall_at_10_threshold": (
            MACRO_RELEVANT_RECALL_AT_10_THRESHOLD
        ),
        "complete_query_recall_at_10_threshold": (
            COMPLETE_QUERY_RECALL_AT_10_THRESHOLD
        ),
        "minimum_result_depth": MIN_RESULT_DEPTH,
        "maximum_result_depth": MAX_RESULT_DEPTH,
        "missing_result_ranks_scored_as_misses": True,
        "retrieval_gate_passed": retrieval_gate_passed,
        "retrieval_metric_prerequisite_passed": retrieval_gate_passed,
        "source_release_holdout_eligible": bundle_manifest["release_holdout_eligible"],
        "release_score_gate_passed": release_score_gate_passed,
        "retrospective_preview_only": True,
        "promotion_pending": True,
        "release_or_promotion_claimed": False,
        "pending_final_authenticated_rollup": True,
        "confidence_interval_method": "wilson_score_95_two_sided",
        "primary_retrieval_mode": "global_cold_text_only",
        "primary_blind_query_contract": "query_id_query_text_as_of_only",
        "primary_context_hidden_from_blind_input": True,
        "blind_query_temporal_taxonomy_exposed": False,
        "challenge_retrieval_mode": "contextual_follow_up_diagnostic",
        "challenge_context_input_present": bool(challenge_queries),
        "challenge_diagnostic_only": True,
        "cohort_metrics_must_not_be_pooled": True,
        "diagnostic_denominator": "primary_global_cold_only",
        "private_file_mode": "0600",
        "private_directory_mode": "0700",
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    manifest_raw = _manifest_bytes(manifest, key=key, domain=SCORE_MANIFEST_DOMAIN)

    current_bundle = _load_bundle(Path(bundle_root), key=key)
    current_run = _load_run(
        Path(run_root),
        key=key,
        bundle_manifest=current_bundle[0],
        bundle_manifest_raw=current_bundle[1],
        bundle_artifacts=current_bundle[2],
        sources=current_bundle[3],
        primary_queries=current_bundle[4],
        challenge_queries=current_bundle[5],
    )
    if (
        current_bundle[1] != bundle_manifest_raw
        or current_bundle[2] != bundle_artifacts
        or current_run[1] != run_manifest_raw
        or current_run[2] != run_artifacts
    ):
        raise GmailTemporalRetrievalHoldoutError(
            "retrieval scoring evidence changed while read"
        )
    _publish(
        Path(output_root),
        {SCORE_ARTIFACT: score_raw, MANIFEST_ARTIFACT: manifest_raw},
    )
    return {
        "version": VERSION,
        "status": "scored",
        "primary_queries": len(primary_queries),
        "challenge_queries": len(challenge_queries),
        "query_hit_rate_at_5": primary_metrics["query_hit_rate_at_5"],
        "query_hit_rate_at_10": primary_metrics["query_hit_rate_at_10"],
        "query_hit_rate_at_5_interval_95": primary_metrics[
            "query_hit_rate_at_5_interval_95"
        ],
        "query_hit_rate_at_10_interval_95": primary_metrics[
            "query_hit_rate_at_10_interval_95"
        ],
        "mean_reciprocal_rank_at_10": primary_metrics["mean_reciprocal_rank_at_10"],
        "macro_relevant_source_recall_at_10": primary_metrics[
            "macro_relevant_source_recall_at_10"
        ],
        "complete_query_recall_at_10": primary_metrics["complete_query_recall_at_10"],
        "retrieval_gate_passed": retrieval_gate_passed,
        "retrieval_metric_prerequisite_passed": retrieval_gate_passed,
        "release_score_gate_passed": release_score_gate_passed,
        "retrospective_preview_only": True,
        "promotion_pending": True,
        "release_or_promotion_claimed": False,
        "pending_final_authenticated_rollup": True,
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
    }


def _failure() -> None:
    print(
        json.dumps(
            {
                "version": VERSION,
                "status": "failed",
                "error": "gmail_temporal_retrieval_holdout_failed",
                "private_content_printed": False,
            },
            sort_keys=True,
        )
    )
    raise SystemExit(2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    freeze_parser = subparsers.add_parser("freeze")
    freeze_parser.add_argument("--source-authority", type=Path, required=True)
    freeze_parser.add_argument("--source-bindings", type=Path, required=True)
    freeze_parser.add_argument("--source-authority-manifest", type=Path, required=True)
    freeze_parser.add_argument("--primary-queries", type=Path, required=True)
    freeze_parser.add_argument("--challenge-queries", type=Path)
    freeze_parser.add_argument("--hmac-key", type=Path, required=True)
    freeze_parser.add_argument("--output-root", type=Path, required=True)

    seal_parser = subparsers.add_parser("seal-run")
    seal_parser.add_argument("--bundle-root", type=Path, required=True)
    seal_parser.add_argument("--primary-results", type=Path, required=True)
    seal_parser.add_argument("--challenge-results", type=Path)
    seal_parser.add_argument("--hmac-key", type=Path, required=True)
    seal_parser.add_argument("--retriever-implementation", type=Path, required=True)
    seal_parser.add_argument("--retriever-configuration", type=Path, required=True)
    seal_parser.add_argument("--retrieval-index-receipt", type=Path, required=True)
    seal_parser.add_argument("--query-protocol", type=Path, required=True)
    seal_parser.add_argument("--output-root", type=Path, required=True)

    score_parser = subparsers.add_parser("score")
    score_parser.add_argument("--bundle-root", type=Path, required=True)
    score_parser.add_argument("--run-root", type=Path, required=True)
    score_parser.add_argument("--hmac-key", type=Path, required=True)
    score_parser.add_argument("--output-root", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.command == "freeze":
            result = freeze_retrieval_holdout(
                args.source_authority,
                args.primary_queries,
                args.hmac_key,
                args.output_root,
                source_bindings_path=args.source_bindings,
                source_authority_manifest_path=args.source_authority_manifest,
                challenge_queries_path=args.challenge_queries,
            )
        elif args.command == "seal-run":
            result = seal_retrieval_run(
                args.bundle_root,
                args.primary_results,
                args.hmac_key,
                {
                    "implementation": args.retriever_implementation,
                    "configuration": args.retriever_configuration,
                    "index_receipt": args.retrieval_index_receipt,
                    "query_protocol": args.query_protocol,
                },
                args.output_root,
                challenge_results_path=args.challenge_results,
            )
        else:
            result = score_retrieval_holdout(
                args.bundle_root,
                args.run_root,
                args.hmac_key,
                args.output_root,
            )
    except (GmailTemporalRetrievalHoldoutError, OSError, ValueError):
        _failure()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
