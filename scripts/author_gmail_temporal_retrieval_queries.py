#!/usr/bin/env python3
"""Prepare and finalize a local-only Gmail temporal retrieval query authority.

``prepare`` authenticates the complete Gmail retrieval source authority and a
mandatory source-only owner eligibility authority, then HMAC-ranks candidates
within protected temporal strata and publishes an owner-only worksheet.  The
worksheet contains source locators and, when ``--brain-home`` is supplied,
locally verified source message text.  It never contains facts, retrieval
results, predictions, or query/relevance suggestions.

``finalize`` accepts only owner-authored changes to the worksheet's semantic
fields, requires explicit source-only/no-retriever attestations, and emits the
exact forty-row query-v3 JSONL consumed by the retrieval holdout evaluator.

Both phases are local-only and perform no model, network, Gmail, retrieval,
fact-search, Brain-write, or persistence call.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pkm_brain.chunking import strip_frontmatter
from pkm_brain.paths import BrainPaths
from pkm_brain.source_dates import (
    source_frontmatter_with_path,
    trusted_gmail_message_timestamps,
)


VERSION = "gmail_temporal_retrieval_query_authoring_v1"
WORKSHEET_VERSION = "gmail_temporal_retrieval_query_worksheet_v1"
PREPARE_MANIFEST_VERSION = "gmail_temporal_retrieval_query_worksheet_manifest_v1"
FINAL_MANIFEST_VERSION = "gmail_temporal_retrieval_query_authority_manifest_v1"
ELIGIBILITY_VERSION = "gmail_temporal_retrieval_query_eligibility_v1"
QUERY_VERSION = "gmail_temporal_retrieval_query_v3"

SOURCE_VERSION = "gmail_temporal_retrieval_source_v1"
BINDING_VERSION = "gmail_temporal_retrieval_source_binding_v3"
SOURCE_AUTHORITY_BUILDER_VERSION = (
    "gmail_temporal_retrieval_source_authority_builder_v3"
)
SOURCE_AUTHORITY_MANIFEST_VERSION = (
    "gmail_temporal_retrieval_source_authority_manifest_v2"
)

SOURCE_AUTHORITY_MANIFEST_DOMAIN = (
    b"gmail_temporal_retrieval_source_authority_manifest_v2\0"
)
SOURCE_AUTHORITY_OPAQUE_ID_DOMAIN = b"gmail_temporal_retrieval_source_authority_v1\0"
CHUNK_INVENTORY_DOMAIN = b"gmail_temporal_retrieval_chunk_inventory_v1\0"
SELECTION_DOMAIN = b"gmail_temporal_retrieval_query_selection_v1\0"
QUERY_ID_DOMAIN = b"gmail_temporal_retrieval_query_id_v1\0"
PREPARE_MANIFEST_DOMAIN = b"gmail_temporal_retrieval_query_worksheet_manifest_v1\0"
FINAL_MANIFEST_DOMAIN = b"gmail_temporal_retrieval_query_authority_manifest_v1\0"

SOURCE_ARTIFACT = "source-authority.jsonl"
BINDING_ARTIFACT = "source-bindings.jsonl"
SOURCE_MANIFEST_ARTIFACT = "manifest.json"
WORKSHEET_ARTIFACT = "worksheet-template.jsonl"
INSTRUCTIONS_ARTIFACT = "instructions.txt"
PREPARE_MANIFEST_ARTIFACT = "manifest.json"
QUERY_ARTIFACT = "primary-queries.jsonl"
FINAL_MANIFEST_ARTIFACT = "manifest.json"

PRIMARY_QUERY_COUNT = 40
MIN_PRIMARY_QUERIES_PER_KIND = 5
MIN_PRIMARY_LIFECYCLE_QUERIES_PER_CLASS = 1
CANDIDATE_DEPTH_MULTIPLIER = 2
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
MIN_HMAC_KEY_BYTES = 32
MAX_HMAC_KEY_BYTES = 4096
MAX_IDENTIFIER_LENGTH = 256
MAX_QUERY_TEXT_LENGTH = 4_000

TEMPORAL_QUERY_KINDS = {
    "deadline",
    "lifecycle",
    "occurrence",
    "relative",
    "schedule",
    "timeline",
}
LIFECYCLE_QUERY_CLASSES = {
    "cancellation",
    "current_status",
    "reschedule",
}
TEMPORAL_KIND_SELECTION_QUOTAS = {
    "deadline": 7,
    "lifecycle": 7,
    "occurrence": 7,
    "relative": 7,
    "schedule": 6,
    "timeline": 6,
}
MIN_TEMPORAL_KIND_CANDIDATES = {
    kind: quota * CANDIDATE_DEPTH_MULTIPLIER
    for kind, quota in TEMPORAL_KIND_SELECTION_QUOTAS.items()
}
MIN_LIFECYCLE_CLASS_CANDIDATES = {
    lifecycle_class: (
        MIN_PRIMARY_LIFECYCLE_QUERIES_PER_CLASS * CANDIDATE_DEPTH_MULTIPLIER
    )
    for lifecycle_class in LIFECYCLE_QUERY_CLASSES
}
ATTESTATION_KEYS = {
    "complete_relevant_source_set",
    "facts_not_consulted",
    "predictions_not_consulted",
    "query_authored_from_source_only",
    "query_does_not_disclose_answer_or_target_labels",
    "retriever_not_consulted",
}
ELIGIBILITY_ATTESTATION_KEYS = {
    "facts_not_consulted",
    "predictions_not_consulted",
    "retriever_not_consulted",
    "temporal_annotation_authored_from_source_only",
}

INSTRUCTIONS_BYTES = b"""Gmail temporal retrieval query worksheet v1

This worksheet is private owner material. Keep it mode 0600.

Copy worksheet-template.jsonl to a separate mode-0600 completed worksheet.
Keep the prepared template and manifest unchanged; pass the edited copy to the
finalize command with --completed-worksheet.

The prepared rows were selected from a mandatory owner-nominated source-only
candidate authority. temporal_query_kind and lifecycle_query_class are the
owner's protected pre-selection annotations; do not relabel them after seeing
which candidates were selected. Candidate nomination and taxonomy attest that
facts, retriever results, and predictions were not consulted.

For every row, fill only these fields:
- query_text: a natural cold question, paraphrased rather than copied from a message;
- as_of: the UTC evidence cutoff for the question;
- relevant_source_ids: the complete sorted source set needed to answer the question;
- owner_attestations: set every value to true only after satisfying it.

This primary cohort is current/cold: as_of must be at or after the latest
source_options availability in the selected thread. Historical prefix queries
need a separate cohort with a protected cutoff chosen before source inspection.

Do not consult Brain facts, Brain retrieval, prior ranked results, or generated
query/relevance suggestions while authoring. Inspect only the source messages
identified in provider_locator/source_options. The optional message_text field
is locally verified source text, not a suggestion. Do not disclose the answer
or copy query, thread, document, Gmail message/thread, or source identifiers
into query_text. Keep context_source_ids [] for this global/cold primary cohort.
Do not alter IDs, locators, source options, row order, or versions.

The completed forty rows use forty distinct threads selected by protected,
stratified HMAC ranking: 7 each for deadline, lifecycle, occurrence, and
relative; 6 each for schedule and timeline. Lifecycle deterministically
includes cancellation, current_status, and reschedule.
"""


class GmailTemporalRetrievalQueryAuthoringError(ValueError):
    """Raised when query authoring authority cannot be established safely."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _jsonl_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical_json(dict(row)) + b"\n" for row in rows)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _identifier(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > MAX_IDENTIFIER_LENGTH
        or any(ord(character) < 0x20 for character in value)
    ):
        raise GmailTemporalRetrievalQueryAuthoringError(f"{label} is invalid")
    return value


def _timestamp(value: Any, *, label: str) -> tuple[datetime, str]:
    if not isinstance(value, str) or not value.strip():
        raise GmailTemporalRetrievalQueryAuthoringError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GmailTemporalRetrievalQueryAuthoringError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise GmailTemporalRetrievalQueryAuthoringError(
            f"{label} must include a timezone"
        )
    normalized = parsed.astimezone(timezone.utc)
    return normalized, normalized.isoformat()


def _private_directory(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GmailTemporalRetrievalQueryAuthoringError(
            f"{label} is unavailable or unsafe"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != PRIVATE_DIRECTORY_MODE
    ):
        raise GmailTemporalRetrievalQueryAuthoringError(
            f"{label} is unavailable or unsafe"
        )


def _private_file(path: Path, *, label: str) -> bytes:
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
            raise GmailTemporalRetrievalQueryAuthoringError(
                f"{label} is unavailable or unsafe"
            )
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, os.O_RDONLY | nofollow)
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != PRIVATE_FILE_MODE
        ):
            raise GmailTemporalRetrievalQueryAuthoringError(
                f"{label} is unavailable or unsafe"
            )
        chunks: list[bytes] = []
        while block := os.read(descriptor, 1024 * 1024):
            chunks.append(block)
        closed = os.fstat(descriptor)
        if (
            (closed.st_dev, closed.st_ino) != (opened.st_dev, opened.st_ino)
            or closed.st_size != opened.st_size
            or closed.st_mtime_ns != opened.st_mtime_ns
        ):
            raise GmailTemporalRetrievalQueryAuthoringError(
                f"{label} changed while read"
            )
        return b"".join(chunks)
    except GmailTemporalRetrievalQueryAuthoringError:
        raise
    except OSError as exc:
        raise GmailTemporalRetrievalQueryAuthoringError(
            f"{label} is unavailable or unsafe"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _hmac_key(path: Path) -> bytes:
    value = _private_file(path, label="HMAC key")
    if not MIN_HMAC_KEY_BYTES <= len(value) <= MAX_HMAC_KEY_BYTES:
        raise GmailTemporalRetrievalQueryAuthoringError("HMAC key length is invalid")
    return value


def _canonical_json_document(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GmailTemporalRetrievalQueryAuthoringError(
            f"{label} is malformed"
        ) from exc
    if not isinstance(value, dict) or raw != _canonical_json(value) + b"\n":
        raise GmailTemporalRetrievalQueryAuthoringError(f"{label} is malformed")
    return value


def _canonical_jsonl(raw: bytes, *, label: str) -> list[dict[str, Any]]:
    if not raw or not raw.endswith(b"\n") or any(not line for line in raw.splitlines()):
        raise GmailTemporalRetrievalQueryAuthoringError(f"{label} is malformed")
    output: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GmailTemporalRetrievalQueryAuthoringError(
                f"{label} is malformed"
            ) from exc
        if not isinstance(value, dict) or line != _canonical_json(value):
            raise GmailTemporalRetrievalQueryAuthoringError(f"{label} is malformed")
        output.append(value)
    return output


def _opaque_id(key: bytes, *, kind: str, values: Sequence[str]) -> str:
    material = _canonical_json([kind, *values])
    digest = hmac.new(
        key,
        SOURCE_AUTHORITY_OPAQUE_ID_DOMAIN + material,
        hashlib.sha256,
    ).hexdigest()
    prefix = "gtrs" if kind == "source" else "gtrt"
    return f"{prefix}_{digest[:32]}"


def _chunk_inventory_authenticator(
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


def _load_source_authority(
    root: Path,
    *,
    key: bytes,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, bytes],
]:
    _private_directory(root, label="source authority")
    expected_names = {
        SOURCE_ARTIFACT,
        BINDING_ARTIFACT,
        SOURCE_MANIFEST_ARTIFACT,
    }
    try:
        names = {path.name for path in root.iterdir()}
    except OSError as exc:
        raise GmailTemporalRetrievalQueryAuthoringError(
            "source authority is unavailable or unsafe"
        ) from exc
    if names != expected_names:
        raise GmailTemporalRetrievalQueryAuthoringError(
            "source authority inventory is invalid"
        )
    raw = {
        SOURCE_ARTIFACT: _private_file(
            root / SOURCE_ARTIFACT, label="source authority artifact"
        ),
        BINDING_ARTIFACT: _private_file(
            root / BINDING_ARTIFACT, label="source binding artifact"
        ),
        SOURCE_MANIFEST_ARTIFACT: _private_file(
            root / SOURCE_MANIFEST_ARTIFACT, label="source authority manifest"
        ),
    }
    manifest = _canonical_json_document(
        raw[SOURCE_MANIFEST_ARTIFACT], label="source authority manifest"
    )
    expected_manifest_keys = {
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
    authenticator = manifest.get("manifest_hmac_sha256")
    unsigned = dict(manifest)
    unsigned.pop("manifest_hmac_sha256", None)
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
    expected_hashes = {
        SOURCE_ARTIFACT: _sha256_bytes(raw[SOURCE_ARTIFACT]),
        BINDING_ARTIFACT: _sha256_bytes(raw[BINDING_ARTIFACT]),
    }
    expected_authenticator = hmac.new(
        key,
        SOURCE_AUTHORITY_MANIFEST_DOMAIN + _canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()
    if (
        set(manifest) != expected_manifest_keys
        or not isinstance(authenticator, str)
        or not hmac.compare_digest(authenticator, expected_authenticator)
        or manifest.get("artifact_sha256") != expected_hashes
        or any(manifest.get(name) != value for name, value in required_policy.items())
    ):
        raise GmailTemporalRetrievalQueryAuthoringError(
            "source authority authentication failed"
        )

    source_rows = _canonical_jsonl(raw[SOURCE_ARTIFACT], label="source authority")
    binding_rows = _canonical_jsonl(raw[BINDING_ARTIFACT], label="source bindings")
    source_by_id: dict[str, dict[str, Any]] = {}
    previous_source_key: tuple[str, str] | None = None
    for row in source_rows:
        if (
            set(row)
            != {
                "available_at",
                "source_id",
                "thread_scope_id",
                "version",
            }
            or row.get("version") != SOURCE_VERSION
        ):
            raise GmailTemporalRetrievalQueryAuthoringError(
                "source authority rows are invalid"
            )
        source_id = _identifier(row.get("source_id"), label="source identity")
        thread_scope_id = _identifier(
            row.get("thread_scope_id"), label="thread scope identity"
        )
        _timestamp(row.get("available_at"), label="source availability")
        sort_key = (thread_scope_id, source_id)
        if source_id in source_by_id or (
            previous_source_key is not None and sort_key <= previous_source_key
        ):
            raise GmailTemporalRetrievalQueryAuthoringError(
                "source authority rows are invalid"
            )
        previous_source_key = sort_key
        source_by_id[source_id] = row

    binding_keys = {
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
    previous_binding_id: str | None = None
    seen_sources: set[str] = set()
    seen_messages: set[tuple[str, str]] = set()
    seen_chunks: set[str] = set()
    document_hashes: dict[str, str] = {}
    for row in binding_rows:
        if set(row) != binding_keys or row.get("version") != BINDING_VERSION:
            raise GmailTemporalRetrievalQueryAuthoringError(
                "source binding rows are invalid"
            )
        source_id = _identifier(row.get("source_id"), label="binding source identity")
        account = _identifier(
            row.get("gmail_account_key"), label="binding account identity"
        )
        message_id = _identifier(
            row.get("gmail_message_id"), label="binding message identity"
        )
        thread_id = _identifier(
            row.get("gmail_thread_id"), label="binding thread identity"
        )
        document_id = _identifier(
            row.get("document_id"), label="binding document identity"
        )
        source = source_by_id.get(source_id)
        document_hash = row.get("document_content_sha256")
        message_hash = row.get("message_sha256")
        chunks = row.get("chunks")
        if (
            source is None
            or source_id in seen_sources
            or (previous_binding_id is not None and source_id <= previous_binding_id)
            or (account, message_id) in seen_messages
            or source_id != _opaque_id(key, kind="source", values=(account, message_id))
            or source.get("thread_scope_id")
            != _opaque_id(key, kind="thread", values=(account, thread_id))
            or source.get("available_at") != row.get("available_at")
            or not isinstance(document_hash, str)
            or len(document_hash) != 64
            or not isinstance(message_hash, str)
            or len(message_hash) != 64
            or any(
                character not in "0123456789abcdef"
                for digest in (document_hash, message_hash)
                for character in digest
            )
            or (
                document_id in document_hashes
                and document_hashes[document_id] != document_hash
            )
            or not isinstance(chunks, list)
        ):
            raise GmailTemporalRetrievalQueryAuthoringError(
                "source binding rows are invalid"
            )
        normalized_chunks: list[dict[str, Any]] = []
        for chunk in chunks:
            if not isinstance(chunk, dict) or set(chunk) != {
                "chunk_id",
                "end_offset",
                "start_offset",
                "text_sha256",
            }:
                raise GmailTemporalRetrievalQueryAuthoringError(
                    "source binding chunk authority is invalid"
                )
            chunk_id = _identifier(
                chunk.get("chunk_id"), label="binding chunk identity"
            )
            start = chunk.get("start_offset")
            end = chunk.get("end_offset")
            text_hash = chunk.get("text_sha256")
            if (
                chunk_id in seen_chunks
                or type(start) is not int
                or type(end) is not int
                or start < 0
                or end <= start
                or not isinstance(text_hash, str)
                or len(text_hash) != 64
                or any(character not in "0123456789abcdef" for character in text_hash)
            ):
                raise GmailTemporalRetrievalQueryAuthoringError(
                    "source binding chunk authority is invalid"
                )
            seen_chunks.add(chunk_id)
            normalized_chunks.append(dict(chunk))
        expected_chunks = sorted(
            normalized_chunks,
            key=lambda chunk: (
                chunk["start_offset"],
                chunk["end_offset"],
                chunk["chunk_id"],
            ),
        )
        chunk_authenticator = row.get("chunk_inventory_hmac_sha256")
        expected_chunk_authenticator = _chunk_inventory_authenticator(
            key,
            source_id=source_id,
            document_id=document_id,
            gmail_account_key=account,
            gmail_message_id=message_id,
            gmail_thread_id=thread_id,
            chunks=normalized_chunks,
        )
        if (
            normalized_chunks != expected_chunks
            or not isinstance(chunk_authenticator, str)
            or not hmac.compare_digest(
                chunk_authenticator, expected_chunk_authenticator
            )
        ):
            raise GmailTemporalRetrievalQueryAuthoringError(
                "source binding chunk authority is invalid"
            )
        previous_binding_id = source_id
        seen_sources.add(source_id)
        seen_messages.add((account, message_id))
        document_hashes[document_id] = document_hash

    if (
        not source_rows
        or seen_sources != set(source_by_id)
        or manifest.get("message_count") != len(source_rows)
        or manifest.get("document_count") != len(document_hashes)
        or manifest.get("chunk_count") != len(seen_chunks)
    ):
        raise GmailTemporalRetrievalQueryAuthoringError(
            "source authority coverage is invalid"
        )
    return source_rows, binding_rows, manifest, raw


def _load_eligibility(
    path: Path | None,
    *,
    key: bytes,
    available_threads: set[str],
) -> tuple[dict[str, tuple[str, str | None]], bytes, str]:
    if path is None:
        raise GmailTemporalRetrievalQueryAuthoringError(
            "source-only eligibility authority is required"
        )
    raw = _private_file(path, label="source-only eligibility authority")
    rows = _canonical_jsonl(raw, label="source-only eligibility authority")
    selected: dict[str, tuple[str, str | None]] = {}
    previous: tuple[str, str] | None = None
    kind_counts = {kind: 0 for kind in sorted(TEMPORAL_QUERY_KINDS)}
    lifecycle_counts = {
        lifecycle_class: 0 for lifecycle_class in sorted(LIFECYCLE_QUERY_CLASSES)
    }
    for row in rows:
        if (
            set(row)
            != {
                "gmail_account_key",
                "gmail_thread_id",
                "lifecycle_query_class",
                "owner_attestations",
                "source_only_owner_nominated",
                "temporal_query_kind",
                "version",
            }
            or row.get("version") != ELIGIBILITY_VERSION
        ):
            raise GmailTemporalRetrievalQueryAuthoringError(
                "source-only eligibility authority is invalid"
            )
        account = _identifier(
            row.get("gmail_account_key"), label="eligible account identity"
        )
        thread = _identifier(
            row.get("gmail_thread_id"), label="eligible thread identity"
        )
        sort_key = (account, thread)
        thread_scope_id = _opaque_id(key, kind="thread", values=(account, thread))
        temporal_kind = row.get("temporal_query_kind")
        lifecycle_class = row.get("lifecycle_query_class")
        attestations = row.get("owner_attestations")
        if (
            row.get("source_only_owner_nominated") is not True
            or thread_scope_id not in available_threads
            or thread_scope_id in selected
            or (previous is not None and sort_key <= previous)
            or temporal_kind not in TEMPORAL_QUERY_KINDS
            or (
                temporal_kind == "lifecycle"
                and lifecycle_class not in LIFECYCLE_QUERY_CLASSES
            )
            or (temporal_kind != "lifecycle" and lifecycle_class is not None)
            or not isinstance(attestations, dict)
            or set(attestations) != ELIGIBILITY_ATTESTATION_KEYS
            or any(
                attestations.get(name) is not True
                for name in ELIGIBILITY_ATTESTATION_KEYS
            )
        ):
            raise GmailTemporalRetrievalQueryAuthoringError(
                "source-only eligibility authority is invalid"
            )
        previous = sort_key
        selected[thread_scope_id] = (str(temporal_kind), lifecycle_class)
        kind_counts[str(temporal_kind)] += 1
        if lifecycle_class is not None:
            lifecycle_counts[str(lifecycle_class)] += 1
    if any(
        kind_counts[kind] < minimum
        for kind, minimum in MIN_TEMPORAL_KIND_CANDIDATES.items()
    ) or any(
        lifecycle_counts[lifecycle_class] < minimum
        for lifecycle_class, minimum in MIN_LIFECYCLE_CLASS_CANDIDATES.items()
    ):
        raise GmailTemporalRetrievalQueryAuthoringError(
            "source-only eligibility authority has insufficient stratified depth"
        )
    return (
        selected,
        raw,
        "owner_nominated_source_only_temporal_thread_authority",
    )


def _selection_rank(
    key: bytes,
    *,
    thread_scope_id: str,
    source_ids: Sequence[str],
) -> bytes:
    material = _canonical_json(
        {
            "source_ids": list(source_ids),
            "thread_scope_id": thread_scope_id,
        }
    )
    return hmac.new(key, SELECTION_DOMAIN + material, hashlib.sha256).digest()


def _query_id(key: bytes, *, thread_scope_id: str) -> str:
    digest = hmac.new(
        key,
        QUERY_ID_DOMAIN + thread_scope_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"gtrq_{digest[:32]}"


def _selected_threads(
    source_rows: Sequence[Mapping[str, Any]],
    *,
    key: bytes,
    eligible_threads: Mapping[str, tuple[str, str | None]],
) -> list[str]:
    sources_by_thread: dict[str, list[str]] = defaultdict(list)
    for row in source_rows:
        thread_scope_id = str(row["thread_scope_id"])
        if thread_scope_id in eligible_threads:
            sources_by_thread[thread_scope_id].append(str(row["source_id"]))
    if set(sources_by_thread) != set(eligible_threads):
        raise GmailTemporalRetrievalQueryAuthoringError(
            "source authority does not cover the eligible thread scopes"
        )

    def rank(thread_scope_id: str) -> tuple[bytes, str]:
        return (
            _selection_rank(
                key,
                thread_scope_id=thread_scope_id,
                source_ids=tuple(sorted(sources_by_thread[thread_scope_id])),
            ),
            thread_scope_id,
        )

    selected: list[str] = []
    for temporal_kind in sorted(TEMPORAL_KIND_SELECTION_QUOTAS):
        quota = TEMPORAL_KIND_SELECTION_QUOTAS[temporal_kind]
        candidates = [
            thread_scope_id
            for thread_scope_id, annotation in eligible_threads.items()
            if annotation[0] == temporal_kind
        ]
        if temporal_kind != "lifecycle":
            selected.extend(sorted(candidates, key=rank)[:quota])
            continue

        lifecycle_selected: list[str] = []
        for lifecycle_class in sorted(LIFECYCLE_QUERY_CLASSES):
            class_candidates = [
                thread_scope_id
                for thread_scope_id in candidates
                if eligible_threads[thread_scope_id][1] == lifecycle_class
            ]
            lifecycle_selected.extend(
                sorted(class_candidates, key=rank)[
                    :MIN_PRIMARY_LIFECYCLE_QUERIES_PER_CLASS
                ]
            )
        lifecycle_selected_set = set(lifecycle_selected)
        remaining = [
            thread_scope_id
            for thread_scope_id in candidates
            if thread_scope_id not in lifecycle_selected_set
        ]
        lifecycle_selected.extend(
            sorted(remaining, key=rank)[: quota - len(lifecycle_selected)]
        )
        selected.extend(sorted(lifecycle_selected, key=rank))
    if len(selected) != PRIMARY_QUERY_COUNT or len(set(selected)) != len(selected):
        raise GmailTemporalRetrievalQueryAuthoringError(
            "stratified source-only selection is invalid"
        )
    return selected


def _verified_source_texts(
    brain_home: Path,
    *,
    bindings: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """Return exact selected message text after re-verifying local projections."""

    paths = BrainPaths.from_value(brain_home)
    document_ids = sorted({str(row["document_id"]) for row in bindings})
    if not document_ids:
        raise GmailTemporalRetrievalQueryAuthoringError(
            "selected source text authority is empty"
        )
    placeholders = ",".join("?" for _item in document_ids)
    uri = f"file:{paths.sqlite_path}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            documents = {
                str(row["id"]): dict(row)
                for row in conn.execute(
                    f"""
                    SELECT id, source_type, source_path, raw_path,
                           content_hash, status
                    FROM documents
                    WHERE id IN ({placeholders})
                    """,  # noqa: S608 - placeholders are count-only, values are bound
                    document_ids,
                )
            }
    except sqlite3.Error as exc:
        raise GmailTemporalRetrievalQueryAuthoringError(
            "local source-text authority is unavailable"
        ) from exc
    if set(documents) != set(document_ids):
        raise GmailTemporalRetrievalQueryAuthoringError(
            "local source-text authority is incomplete"
        )

    bindings_by_document: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in bindings:
        bindings_by_document[str(row["document_id"])].append(row)
    output: dict[str, str] = {}
    for document_id in document_ids:
        document = documents[document_id]
        document_bindings = bindings_by_document[document_id]
        expected_document_hashes = {
            str(row["document_content_sha256"]) for row in document_bindings
        }
        if (
            document.get("source_type") != "gmail_thread"
            or document.get("status") != "active"
            or expected_document_hashes != {str(document.get("content_hash") or "")}
        ):
            raise GmailTemporalRetrievalQueryAuthoringError(
                "local source-text document authority is stale"
            )
        frontmatter, source_path = source_frontmatter_with_path(document)
        timestamps = (
            trusted_gmail_message_timestamps(document, frontmatter, source_path)
            if source_path is not None
            else None
        )
        if timestamps is None or source_path is None:
            raise GmailTemporalRetrievalQueryAuthoringError(
                "local source-text projection is not trusted"
            )
        timestamp_by_message = {str(row["message_id"]): row for row in timestamps}
        expected_message_ids = {
            str(row["gmail_message_id"]) for row in document_bindings
        }
        if (
            len(timestamp_by_message) != len(timestamps)
            or set(timestamp_by_message) != expected_message_ids
        ):
            raise GmailTemporalRetrievalQueryAuthoringError(
                "local source-text message authority is invalid"
            )
        try:
            body = strip_frontmatter(source_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise GmailTemporalRetrievalQueryAuthoringError(
                "local source-text projection is unavailable"
            ) from exc
        for binding in document_bindings:
            source_id = str(binding["source_id"])
            message_id = str(binding["gmail_message_id"])
            message = timestamp_by_message.get(message_id)
            if (
                message is None
                or frontmatter.get("gmail_account_key")
                != binding.get("gmail_account_key")
                or frontmatter.get("gmail_thread_id") != binding.get("gmail_thread_id")
            ):
                raise GmailTemporalRetrievalQueryAuthoringError(
                    "local source-text binding is stale"
                )
            _internal_date, normalized_internal_date = _timestamp(
                message.get("internal_date"), label="trusted Gmail internal date"
            )
            _binding_date, normalized_binding_date = _timestamp(
                binding.get("available_at"), label="binding availability"
            )
            if normalized_internal_date != normalized_binding_date:
                raise GmailTemporalRetrievalQueryAuthoringError(
                    "local source-text availability is stale"
                )
            start = message.get("start_offset")
            end = message.get("end_offset")
            if (
                type(start) is not int
                or type(end) is not int
                or start < 0
                or end <= start
                or end > len(body)
            ):
                raise GmailTemporalRetrievalQueryAuthoringError(
                    "local source-text range is invalid"
                )
            text = body[start:end]
            if _sha256_bytes(text.encode("utf-8")) != binding.get("message_sha256"):
                raise GmailTemporalRetrievalQueryAuthoringError(
                    "local source-text content is stale"
                )
            output[source_id] = text
    if set(output) != {str(row["source_id"]) for row in bindings}:
        raise GmailTemporalRetrievalQueryAuthoringError(
            "local source-text coverage is incomplete"
        )
    return output


def _manifest_bytes(
    manifest: Mapping[str, Any],
    *,
    key: bytes,
    domain: bytes,
) -> bytes:
    unsigned = dict(manifest)
    authenticator = hmac.new(
        key,
        domain + _canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()
    return _canonical_json({**unsigned, "manifest_hmac_sha256": authenticator}) + b"\n"


def _publish(root: Path, artifacts: Mapping[str, bytes]) -> None:
    parent = root.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise GmailTemporalRetrievalQueryAuthoringError("output parent is unsafe")
    parent.mkdir(parents=True, mode=PRIVATE_DIRECTORY_MODE, exist_ok=True)
    if root.exists() or root.is_symlink():
        raise GmailTemporalRetrievalQueryAuthoringError("output already exists")
    temporary = Path(tempfile.mkdtemp(prefix=f".{root.name}-", dir=parent))
    os.chmod(temporary, PRIVATE_DIRECTORY_MODE)
    try:
        for name, raw in sorted(artifacts.items()):
            descriptor = os.open(
                temporary / name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                PRIVATE_FILE_MODE,
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(temporary, root)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def prepare_worksheet(
    source_authority_root: Path,
    hmac_key_path: Path,
    output_root: Path,
    *,
    brain_home: Path | None = None,
    eligible_threads_path: Path | None = None,
) -> dict[str, Any]:
    """Publish one authenticated, private forty-thread authoring template."""

    key = _hmac_key(Path(hmac_key_path))
    source_rows, binding_rows, source_manifest, source_raw = _load_source_authority(
        Path(source_authority_root), key=key
    )
    sources_by_thread: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source_rows:
        sources_by_thread[str(row["thread_scope_id"])].append(dict(row))
    eligible_threads, eligibility_raw, selection_population = _load_eligibility(
        Path(eligible_threads_path) if eligible_threads_path is not None else None,
        key=key,
        available_threads=set(sources_by_thread),
    )
    selected_threads = _selected_threads(
        source_rows, key=key, eligible_threads=eligible_threads
    )
    eligibility_kind_counts = {
        kind: sum(annotation[0] == kind for annotation in eligible_threads.values())
        for kind in sorted(TEMPORAL_QUERY_KINDS)
    }
    eligibility_lifecycle_counts = {
        lifecycle_class: sum(
            annotation[1] == lifecycle_class for annotation in eligible_threads.values()
        )
        for lifecycle_class in sorted(LIFECYCLE_QUERY_CLASSES)
    }
    binding_by_source = {str(row["source_id"]): row for row in binding_rows}
    selected_bindings = [
        binding_by_source[str(source["source_id"])]
        for thread_scope_id in selected_threads
        for source in sources_by_thread[thread_scope_id]
    ]
    source_texts = (
        _verified_source_texts(Path(brain_home), bindings=selected_bindings)
        if brain_home is not None
        else {}
    )
    source_text_mode = (
        "embedded_verified_message_text"
        if brain_home is not None
        else "authenticated_provider_identity_locator_only"
    )
    worksheet: list[dict[str, Any]] = []
    for ordinal, thread_scope_id in enumerate(selected_threads, start=1):
        temporal_kind, lifecycle_class = eligible_threads[thread_scope_id]
        thread_sources = sorted(
            sources_by_thread[thread_scope_id],
            key=lambda row: (str(row["available_at"]), str(row["source_id"])),
        )
        thread_bindings = [
            binding_by_source[str(row["source_id"])] for row in thread_sources
        ]
        provider_pairs = {
            (
                str(row["gmail_account_key"]),
                str(row["gmail_thread_id"]),
                str(row["document_id"]),
            )
            for row in thread_bindings
        }
        if len(provider_pairs) != 1:
            raise GmailTemporalRetrievalQueryAuthoringError(
                "selected thread provider binding is invalid"
            )
        account, gmail_thread_id, document_id = provider_pairs.pop()
        source_options = []
        for source, binding in zip(thread_sources, thread_bindings, strict=True):
            option = {
                "available_at": source["available_at"],
                "gmail_message_id": binding["gmail_message_id"],
                "source_id": source["source_id"],
            }
            if brain_home is not None:
                option["message_text"] = source_texts[str(source["source_id"])]
            source_options.append(option)
        worksheet.append(
            {
                "version": WORKSHEET_VERSION,
                "ordinal": ordinal,
                "query_id": _query_id(key, thread_scope_id=thread_scope_id),
                "thread_scope_id": thread_scope_id,
                "provider_locator": {
                    "document_id": document_id,
                    "gmail_account_key": account,
                    "gmail_thread_id": gmail_thread_id,
                },
                "source_options": source_options,
                "query_text": "",
                "as_of": "",
                "temporal_query_kind": temporal_kind,
                "lifecycle_query_class": lifecycle_class,
                "relevant_source_ids": [],
                "context_source_ids": [],
                "owner_attestations": {
                    key_name: False for key_name in sorted(ATTESTATION_KEYS)
                },
            }
        )
    worksheet_raw = _jsonl_bytes(worksheet)
    artifacts = {
        WORKSHEET_ARTIFACT: worksheet_raw,
        INSTRUCTIONS_ARTIFACT: INSTRUCTIONS_BYTES,
    }
    manifest = {
        "version": PREPARE_MANIFEST_VERSION,
        "authoring_version": VERSION,
        "artifact_sha256": {
            name: _sha256_bytes(raw) for name, raw in sorted(artifacts.items())
        },
        "source_authority_manifest_sha256": _sha256_bytes(
            source_raw[SOURCE_MANIFEST_ARTIFACT]
        ),
        "source_authority_manifest_hmac_sha256": source_manifest[
            "manifest_hmac_sha256"
        ],
        "source_authority_artifact_sha256": source_manifest["artifact_sha256"],
        "eligibility_sha256": _sha256_bytes(eligibility_raw),
        "eligibility_candidate_count": len(eligible_threads),
        "eligibility_temporal_kind_counts": eligibility_kind_counts,
        "eligibility_lifecycle_class_counts": eligibility_lifecycle_counts,
        "selection_population": selection_population,
        "selection_policy": ("hmac_rank_within_protected_source_only_temporal_strata"),
        "selection_temporal_kind_quotas": TEMPORAL_KIND_SELECTION_QUOTAS,
        "selection_lifecycle_class_floor": (MIN_PRIMARY_LIFECYCLE_QUERIES_PER_CLASS),
        "minimum_temporal_kind_candidates": MIN_TEMPORAL_KIND_CANDIDATES,
        "minimum_lifecycle_class_candidates": MIN_LIFECYCLE_CLASS_CANDIDATES,
        "query_count": PRIMARY_QUERY_COUNT,
        "thread_group_count": PRIMARY_QUERY_COUNT,
        "query_identity": "hmac_selected_thread_scope",
        "primary_as_of_policy": "at_or_after_latest_visible_thread_source",
        "historical_prefix_queries_allowed": False,
        "source_text_mode": source_text_mode,
        "worksheet_owner_authored_fields_blank": True,
        "worksheet_protected_temporal_taxonomy": True,
        "retriever_or_fact_suggestions_included": False,
        "eligibility_source_only_attestations_required": True,
        "source_only_eligibility_required": True,
        "source_only_owner_authoring_required": True,
        "private_file_mode": "0600",
        "private_directory_mode": "0700",
        "external_calls": 0,
        "model_calls": 0,
        "network_calls": 0,
        "retrieval_calls": 0,
        "fact_search_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    manifest_raw = _manifest_bytes(manifest, key=key, domain=PREPARE_MANIFEST_DOMAIN)
    current_source = _load_source_authority(Path(source_authority_root), key=key)
    if (
        current_source[3] != source_raw
        or _private_file(
            Path(eligible_threads_path), label="source-only eligibility authority"
        )
        != eligibility_raw
    ):
        raise GmailTemporalRetrievalQueryAuthoringError(
            "query worksheet source authority changed while read"
        )
    _publish(
        Path(output_root),
        {**artifacts, PREPARE_MANIFEST_ARTIFACT: manifest_raw},
    )
    return {
        "version": VERSION,
        "status": "prepared",
        "query_count": PRIMARY_QUERY_COUNT,
        "thread_group_count": PRIMARY_QUERY_COUNT,
        "source_text_mode": source_text_mode,
        "external_calls": 0,
        "model_calls": 0,
        "network_calls": 0,
        "retrieval_calls": 0,
        "fact_search_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
    }


def _load_prepared_worksheet(
    root: Path,
    *,
    key: bytes,
) -> tuple[dict[str, Any], bytes, list[dict[str, Any]], bytes]:
    _private_directory(root, label="prepared worksheet")
    expected_names = {
        WORKSHEET_ARTIFACT,
        INSTRUCTIONS_ARTIFACT,
        PREPARE_MANIFEST_ARTIFACT,
    }
    try:
        names = {path.name for path in root.iterdir()}
    except OSError as exc:
        raise GmailTemporalRetrievalQueryAuthoringError(
            "prepared worksheet is unavailable or unsafe"
        ) from exc
    if names != expected_names:
        raise GmailTemporalRetrievalQueryAuthoringError(
            "prepared worksheet inventory is invalid"
        )
    worksheet_raw = _private_file(root / WORKSHEET_ARTIFACT, label="worksheet template")
    instructions_raw = _private_file(
        root / INSTRUCTIONS_ARTIFACT, label="worksheet instructions"
    )
    manifest_raw = _private_file(
        root / PREPARE_MANIFEST_ARTIFACT, label="worksheet manifest"
    )
    manifest = _canonical_json_document(manifest_raw, label="worksheet manifest")
    authenticator = manifest.get("manifest_hmac_sha256")
    unsigned = dict(manifest)
    unsigned.pop("manifest_hmac_sha256", None)
    expected_artifact_hashes = {
        WORKSHEET_ARTIFACT: _sha256_bytes(worksheet_raw),
        INSTRUCTIONS_ARTIFACT: _sha256_bytes(instructions_raw),
    }
    expected_policy = {
        "version": PREPARE_MANIFEST_VERSION,
        "authoring_version": VERSION,
        "artifact_sha256": expected_artifact_hashes,
        "selection_policy": ("hmac_rank_within_protected_source_only_temporal_strata"),
        "selection_temporal_kind_quotas": TEMPORAL_KIND_SELECTION_QUOTAS,
        "selection_lifecycle_class_floor": (MIN_PRIMARY_LIFECYCLE_QUERIES_PER_CLASS),
        "minimum_temporal_kind_candidates": MIN_TEMPORAL_KIND_CANDIDATES,
        "minimum_lifecycle_class_candidates": MIN_LIFECYCLE_CLASS_CANDIDATES,
        "query_count": PRIMARY_QUERY_COUNT,
        "thread_group_count": PRIMARY_QUERY_COUNT,
        "query_identity": "hmac_selected_thread_scope",
        "primary_as_of_policy": "at_or_after_latest_visible_thread_source",
        "historical_prefix_queries_allowed": False,
        "worksheet_owner_authored_fields_blank": True,
        "worksheet_protected_temporal_taxonomy": True,
        "retriever_or_fact_suggestions_included": False,
        "eligibility_source_only_attestations_required": True,
        "source_only_eligibility_required": True,
        "source_only_owner_authoring_required": True,
        "private_file_mode": "0600",
        "private_directory_mode": "0700",
        "external_calls": 0,
        "model_calls": 0,
        "network_calls": 0,
        "retrieval_calls": 0,
        "fact_search_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    expected_manifest_keys = {
        *expected_policy,
        "eligibility_candidate_count",
        "eligibility_lifecycle_class_counts",
        "eligibility_sha256",
        "eligibility_temporal_kind_counts",
        "manifest_hmac_sha256",
        "selection_population",
        "source_authority_artifact_sha256",
        "source_authority_manifest_hmac_sha256",
        "source_authority_manifest_sha256",
        "source_text_mode",
    }
    source_artifact_hashes = manifest.get("source_authority_artifact_sha256")
    selection_population = manifest.get("selection_population")
    eligibility_sha256 = manifest.get("eligibility_sha256")
    eligibility_candidate_count = manifest.get("eligibility_candidate_count")
    eligibility_kind_counts = manifest.get("eligibility_temporal_kind_counts")
    eligibility_lifecycle_counts = manifest.get("eligibility_lifecycle_class_counts")
    if (
        set(manifest) != expected_manifest_keys
        or not _is_sha256(authenticator)
        or not hmac.compare_digest(
            authenticator,
            hmac.new(
                key,
                PREPARE_MANIFEST_DOMAIN + _canonical_json(unsigned),
                hashlib.sha256,
            ).hexdigest(),
        )
        or any(manifest.get(name) != value for name, value in expected_policy.items())
        or instructions_raw != INSTRUCTIONS_BYTES
        or manifest.get("source_text_mode")
        not in {
            "embedded_verified_message_text",
            "authenticated_provider_identity_locator_only",
        }
        or selection_population
        != "owner_nominated_source_only_temporal_thread_authority"
        or not _is_sha256(eligibility_sha256)
        or type(eligibility_candidate_count) is not int
        or eligibility_candidate_count < sum(MIN_TEMPORAL_KIND_CANDIDATES.values())
        or not isinstance(eligibility_kind_counts, dict)
        or set(eligibility_kind_counts) != TEMPORAL_QUERY_KINDS
        or any(
            type(eligibility_kind_counts.get(kind)) is not int
            or eligibility_kind_counts[kind] < minimum
            for kind, minimum in MIN_TEMPORAL_KIND_CANDIDATES.items()
        )
        or sum(eligibility_kind_counts.values()) != eligibility_candidate_count
        or not isinstance(eligibility_lifecycle_counts, dict)
        or set(eligibility_lifecycle_counts) != LIFECYCLE_QUERY_CLASSES
        or any(
            type(eligibility_lifecycle_counts.get(lifecycle_class)) is not int
            or eligibility_lifecycle_counts[lifecycle_class] < minimum
            for lifecycle_class, minimum in MIN_LIFECYCLE_CLASS_CANDIDATES.items()
        )
        or sum(eligibility_lifecycle_counts.values())
        != eligibility_kind_counts.get("lifecycle")
        or not _is_sha256(manifest.get("source_authority_manifest_sha256"))
        or not _is_sha256(manifest.get("source_authority_manifest_hmac_sha256"))
        or not isinstance(source_artifact_hashes, dict)
        or set(source_artifact_hashes) != {SOURCE_ARTIFACT, BINDING_ARTIFACT}
        or any(not _is_sha256(digest) for digest in source_artifact_hashes.values())
    ):
        raise GmailTemporalRetrievalQueryAuthoringError(
            "prepared worksheet authentication failed"
        )
    rows = _canonical_jsonl(worksheet_raw, label="worksheet template")
    if len(rows) != PRIMARY_QUERY_COUNT:
        raise GmailTemporalRetrievalQueryAuthoringError(
            "worksheet template coverage is invalid"
        )
    return manifest, manifest_raw, rows, worksheet_raw


def _validated_query_rows(
    templates: Sequence[Mapping[str, Any]],
    completed: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, int]]:
    if len(templates) != PRIMARY_QUERY_COUNT or len(completed) != len(templates):
        raise GmailTemporalRetrievalQueryAuthoringError(
            "completed worksheet coverage is invalid"
        )
    expected_worksheet_keys = {
        "as_of",
        "context_source_ids",
        "lifecycle_query_class",
        "ordinal",
        "owner_attestations",
        "provider_locator",
        "query_id",
        "query_text",
        "relevant_source_ids",
        "source_options",
        "temporal_query_kind",
        "thread_scope_id",
        "version",
    }
    protected_keys = {
        "context_source_ids",
        "lifecycle_query_class",
        "ordinal",
        "provider_locator",
        "query_id",
        "source_options",
        "temporal_query_kind",
        "thread_scope_id",
        "version",
    }
    query_rows: list[dict[str, Any]] = []
    seen_queries: set[str] = set()
    seen_threads: set[str] = set()
    kind_counts = {kind: 0 for kind in sorted(TEMPORAL_QUERY_KINDS)}
    lifecycle_counts = {lifecycle: 0 for lifecycle in sorted(LIFECYCLE_QUERY_CLASSES)}
    for expected_ordinal, (template, row) in enumerate(
        zip(templates, completed, strict=True), start=1
    ):
        if (
            set(template) != expected_worksheet_keys
            or set(row) != expected_worksheet_keys
            or any(row.get(name) != template.get(name) for name in protected_keys)
            or row.get("version") != WORKSHEET_VERSION
            or row.get("ordinal") != expected_ordinal
            or template.get("query_text") != ""
            or template.get("as_of") != ""
            or template.get("relevant_source_ids") != []
            or template.get("context_source_ids") != []
            or template.get("owner_attestations")
            != {name: False for name in sorted(ATTESTATION_KEYS)}
        ):
            raise GmailTemporalRetrievalQueryAuthoringError(
                "completed worksheet changed protected authority"
            )
        query_id = _identifier(row.get("query_id"), label="query identity")
        thread_scope_id = _identifier(
            row.get("thread_scope_id"), label="thread scope identity"
        )
        query_text = row.get("query_text")
        if (
            not isinstance(query_text, str)
            or not query_text.strip()
            or query_text.strip() != query_text
            or len(query_text) > MAX_QUERY_TEXT_LENGTH
            or "\x00" in query_text
            or query_id in seen_queries
            or thread_scope_id in seen_threads
        ):
            raise GmailTemporalRetrievalQueryAuthoringError(
                "completed worksheet query is invalid"
            )
        attestations = row.get("owner_attestations")
        if (
            not isinstance(attestations, dict)
            or set(attestations) != ATTESTATION_KEYS
            or any(attestations.get(name) is not True for name in ATTESTATION_KEYS)
        ):
            raise GmailTemporalRetrievalQueryAuthoringError(
                "completed worksheet source-only attestations are incomplete"
            )
        temporal_kind = row.get("temporal_query_kind")
        lifecycle_class = row.get("lifecycle_query_class")
        if (
            temporal_kind not in TEMPORAL_QUERY_KINDS
            or (
                temporal_kind == "lifecycle"
                and lifecycle_class not in LIFECYCLE_QUERY_CLASSES
            )
            or (temporal_kind != "lifecycle" and lifecycle_class is not None)
        ):
            raise GmailTemporalRetrievalQueryAuthoringError(
                "completed worksheet temporal taxonomy is invalid"
            )
        source_options = row.get("source_options")
        if not isinstance(source_options, list) or not source_options:
            raise GmailTemporalRetrievalQueryAuthoringError(
                "completed worksheet source options are invalid"
            )
        option_sources: dict[str, datetime] = {}
        for option in source_options:
            if not isinstance(option, dict) or set(option) not in (
                {"available_at", "gmail_message_id", "source_id"},
                {
                    "available_at",
                    "gmail_message_id",
                    "message_text",
                    "source_id",
                },
            ):
                raise GmailTemporalRetrievalQueryAuthoringError(
                    "completed worksheet source options are invalid"
                )
            source_id = _identifier(
                option.get("source_id"), label="worksheet source identity"
            )
            available_at, _normalized_available_at = _timestamp(
                option.get("available_at"), label="worksheet source availability"
            )
            if source_id in option_sources:
                raise GmailTemporalRetrievalQueryAuthoringError(
                    "completed worksheet source options are invalid"
                )
            option_sources[source_id] = available_at
        provider_locator = row.get("provider_locator")
        if not isinstance(provider_locator, dict) or set(provider_locator) != {
            "document_id",
            "gmail_account_key",
            "gmail_thread_id",
        }:
            raise GmailTemporalRetrievalQueryAuthoringError(
                "completed worksheet provider locator is invalid"
            )
        protected_query_identifiers = {
            query_id,
            thread_scope_id,
            _identifier(
                provider_locator.get("document_id"),
                label="worksheet document identity",
            ),
            _identifier(
                provider_locator.get("gmail_thread_id"),
                label="worksheet Gmail thread identity",
            ),
            *option_sources,
            *(
                _identifier(
                    option.get("gmail_message_id"),
                    label="worksheet Gmail message identity",
                )
                for option in source_options
            ),
        }
        folded_query_text = query_text.casefold()
        if any(
            identifier.casefold() in folded_query_text
            for identifier in protected_query_identifiers
        ):
            raise GmailTemporalRetrievalQueryAuthoringError(
                "completed worksheet query leaks protected authority"
            )
        relevant = row.get("relevant_source_ids")
        if (
            not isinstance(relevant, list)
            or not relevant
            or any(not isinstance(item, str) for item in relevant)
            or relevant != sorted(set(relevant))
            or not set(relevant) <= set(option_sources)
        ):
            raise GmailTemporalRetrievalQueryAuthoringError(
                "completed worksheet relevance authority is invalid"
            )
        as_of, normalized_as_of = _timestamp(
            row.get("as_of"), label="completed query as-of"
        )
        if as_of < max(option_sources.values()):
            raise GmailTemporalRetrievalQueryAuthoringError(
                "completed worksheet as-of precedes visible source authority"
            )
        if any(option_sources[source_id] > as_of for source_id in relevant):
            raise GmailTemporalRetrievalQueryAuthoringError(
                "completed worksheet contains future relevance evidence"
            )
        seen_queries.add(query_id)
        seen_threads.add(thread_scope_id)
        kind_counts[str(temporal_kind)] += 1
        if lifecycle_class is not None:
            lifecycle_counts[str(lifecycle_class)] += 1
        query_rows.append(
            {
                "as_of": normalized_as_of,
                "context_source_ids": [],
                "lifecycle_query_class": lifecycle_class,
                "query_id": query_id,
                "query_text": query_text,
                "relevant_source_ids": list(relevant),
                "temporal_query_kind": temporal_kind,
                "thread_scope_id": thread_scope_id,
                "version": QUERY_VERSION,
            }
        )
    if kind_counts != TEMPORAL_KIND_SELECTION_QUOTAS or any(
        count < MIN_PRIMARY_LIFECYCLE_QUERIES_PER_CLASS
        for count in lifecycle_counts.values()
    ):
        raise GmailTemporalRetrievalQueryAuthoringError(
            "completed worksheet temporal-kind coverage is insufficient"
        )
    query_rows.sort(key=lambda row: str(row["query_id"]))
    return query_rows, kind_counts, lifecycle_counts


def finalize_query_authority(
    worksheet_root: Path,
    completed_worksheet_path: Path,
    hmac_key_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Validate owner work and publish exact query-v3 authority JSONL."""

    key = _hmac_key(Path(hmac_key_path))
    worksheet_manifest, worksheet_manifest_raw, templates, worksheet_raw = (
        _load_prepared_worksheet(Path(worksheet_root), key=key)
    )
    completed_raw = _private_file(
        Path(completed_worksheet_path), label="completed worksheet"
    )
    completed = _canonical_jsonl(completed_raw, label="completed worksheet")
    query_rows, kind_counts, lifecycle_counts = _validated_query_rows(
        templates, completed
    )
    query_raw = _jsonl_bytes(query_rows)
    manifest = {
        "version": FINAL_MANIFEST_VERSION,
        "authoring_version": VERSION,
        "artifact_sha256": {QUERY_ARTIFACT: _sha256_bytes(query_raw)},
        "worksheet_manifest_sha256": _sha256_bytes(worksheet_manifest_raw),
        "worksheet_manifest_hmac_sha256": worksheet_manifest["manifest_hmac_sha256"],
        "worksheet_template_sha256": _sha256_bytes(worksheet_raw),
        "completed_worksheet_sha256": _sha256_bytes(completed_raw),
        "source_authority_manifest_sha256": worksheet_manifest[
            "source_authority_manifest_sha256"
        ],
        "source_authority_manifest_hmac_sha256": worksheet_manifest[
            "source_authority_manifest_hmac_sha256"
        ],
        "query_version": QUERY_VERSION,
        "query_count": len(query_rows),
        "thread_group_count": len(query_rows),
        "temporal_query_kind_counts": kind_counts,
        "lifecycle_query_class_counts": lifecycle_counts,
        "minimum_queries_per_temporal_kind": MIN_PRIMARY_QUERIES_PER_KIND,
        "minimum_lifecycle_queries_per_class": (
            MIN_PRIMARY_LIFECYCLE_QUERIES_PER_CLASS
        ),
        "primary_retrieval_mode": "global_cold_text_only",
        "primary_as_of_policy": "at_or_after_latest_visible_thread_source",
        "historical_prefix_queries_allowed": False,
        "context_source_ids": "empty_for_every_primary_query",
        "source_only_owner_attestations_complete": True,
        "retriever_or_fact_suggestions_included": False,
        "private_source_text_in_query_artifact": False,
        "private_file_mode": "0600",
        "private_directory_mode": "0700",
        "external_calls": 0,
        "model_calls": 0,
        "network_calls": 0,
        "retrieval_calls": 0,
        "fact_search_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    manifest_raw = _manifest_bytes(manifest, key=key, domain=FINAL_MANIFEST_DOMAIN)
    current_prepared = _load_prepared_worksheet(Path(worksheet_root), key=key)
    if (
        current_prepared[1] != worksheet_manifest_raw
        or current_prepared[3] != worksheet_raw
        or _private_file(Path(completed_worksheet_path), label="completed worksheet")
        != completed_raw
    ):
        raise GmailTemporalRetrievalQueryAuthoringError(
            "query authoring evidence changed while read"
        )
    _publish(
        Path(output_root),
        {QUERY_ARTIFACT: query_raw, FINAL_MANIFEST_ARTIFACT: manifest_raw},
    )
    return {
        "version": VERSION,
        "status": "finalized",
        "query_count": len(query_rows),
        "thread_group_count": len(query_rows),
        "temporal_query_kind_counts": kind_counts,
        "lifecycle_query_class_counts": lifecycle_counts,
        "external_calls": 0,
        "model_calls": 0,
        "network_calls": 0,
        "retrieval_calls": 0,
        "fact_search_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
    }


def _failure() -> None:
    print(
        json.dumps(
            {
                "version": VERSION,
                "status": "failed",
                "error": "gmail_temporal_retrieval_query_authoring_failed",
                "external_calls": 0,
                "model_calls": 0,
                "network_calls": 0,
                "retrieval_calls": 0,
                "fact_search_calls": 0,
                "persistence_calls": 0,
                "private_content_printed": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    raise SystemExit(2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--source-authority-root", type=Path, required=True)
    prepare_parser.add_argument("--hmac-key", type=Path, required=True)
    prepare_parser.add_argument("--output-root", type=Path, required=True)
    prepare_parser.add_argument(
        "--brain-home",
        type=Path,
        help=(
            "optional matching local Brain index; embeds exact verified source "
            "message text in the private worksheet"
        ),
    )
    prepare_parser.add_argument(
        "--eligible-threads",
        type=Path,
        required=True,
        help=(
            "mode-0600 canonical JSONL of source-only owner nominations with "
            "version, gmail_account_key, gmail_thread_id, temporal_query_kind, "
            "lifecycle_query_class, source_only_owner_nominated=true, and true "
            "owner_attestations for no facts, predictions, or retriever use"
        ),
    )

    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--worksheet-root", type=Path, required=True)
    finalize_parser.add_argument(
        "--completed-worksheet",
        type=Path,
        required=True,
        help="mode-0600 edited copy of worksheet-template.jsonl",
    )
    finalize_parser.add_argument("--hmac-key", type=Path, required=True)
    finalize_parser.add_argument("--output-root", type=Path, required=True)

    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare_worksheet(
                args.source_authority_root,
                args.hmac_key,
                args.output_root,
                brain_home=args.brain_home,
                eligible_threads_path=args.eligible_threads,
            )
        else:
            result = finalize_query_authority(
                args.worksheet_root,
                args.completed_worksheet,
                args.hmac_key,
                args.output_root,
            )
    except Exception:  # noqa: BLE001 - private inputs must never reach stderr/stdout
        _failure()
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
