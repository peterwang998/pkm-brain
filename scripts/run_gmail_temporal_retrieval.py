#!/usr/bin/env python3
"""Run blind Gmail temporal queries through the production Brain retriever.

The scorer deliberately keeps Gmail identities and relevance gold away from the
retriever.  This adapter receives only the frozen blind query/source artifacts
plus a separate local message binding.  It verifies that every binding still
matches a trusted Gmail projection and the indexed chunk ranges, runs Brain in
read-only/no-telemetry mode, removes context and future evidence, and writes
ranked opaque source IDs.  It never pads a short result list.

This is retrospective execution evidence.  The existing holdout sealer remains
the authority that authenticates the bundle, results, and provenance together.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pkm_brain.service as brain_service_module
import pkm_brain.retrospective_retrieval as retrospective_retrieval_module
from pkm_brain.chunking import strip_frontmatter
from pkm_brain.paths import BrainPaths
from pkm_brain.service import (
    BrainService,
    RETROSPECTIVE_RETRIEVAL_VERSION,
    RetrospectiveEvidenceSource,
)
from pkm_brain.source_dates import (
    source_frontmatter_with_path,
    trusted_gmail_message_timestamps,
)

VERSION = "gmail_temporal_retrieval_runner_v4"
BINDING_VERSION = "gmail_temporal_retrieval_source_binding_v3"
RESULT_VERSION = "gmail_temporal_retrieval_result_v2"
CONFIG_VERSION = "gmail_temporal_retrieval_configuration_v4"
IMPLEMENTATION_PROVENANCE_VERSION = (
    "gmail_temporal_retrieval_implementation_provenance_v3"
)
INDEX_RECEIPT_VERSION = "gmail_temporal_retrieval_index_receipt_v2"
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
MAX_RESULTS = 10
MAX_CONTEXT_CHARS = 24_000
SOURCE_ARTIFACT = "blind-source-authority.jsonl"
PRIMARY_QUERY_ARTIFACT = "blind-primary-queries.jsonl"
CHALLENGE_QUERY_ARTIFACT = "blind-challenge-queries.jsonl"
MANIFEST_ARTIFACT = "manifest.json"
PRIMARY_RESULT_ARTIFACT = "primary-results.jsonl"
CHALLENGE_RESULT_ARTIFACT = "challenge-results.jsonl"
CONFIG_ARTIFACT = "retriever-configuration.json"
INDEX_RECEIPT_ARTIFACT = "retrieval-index-receipt.json"
QUERY_PROTOCOL_ARTIFACT = "query-protocol.txt"
IMPLEMENTATION_ARTIFACT = "retriever-implementation.json"
QUERY_PROTOCOL_BYTES = (
    "gmail_temporal_retrieval_query_protocol_v3\n"
    "input=blind_query_id_query_text_as_of_only\n"
    "challenge_context=local_bound_message_text_only\n"
    "retrieval_api=BrainService.retrieve_retrospective_evidence\n"
    "output=unique_opaque_sources_zero_to_ten_no_padding\n"
    "future_and_context_sources=excluded\n"
    "telemetry=disabled\n"
).encode("utf-8")


class GmailTemporalRetrievalRunnerError(ValueError):
    """Raised when a blind retrieval run cannot be reproduced safely."""


@dataclass(frozen=True)
class VerifiedSource:
    source_id: str
    available_at: datetime
    document_id: str
    gmail_message_id: str
    chunk_ids: tuple[str, ...]
    text: str


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical_json(dict(row)) + b"\n" for row in rows)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _parse_timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise GmailTemporalRetrievalRunnerError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GmailTemporalRetrievalRunnerError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise GmailTemporalRetrievalRunnerError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _private_file(path: Path, *, label: str) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GmailTemporalRetrievalRunnerError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != PRIVATE_FILE_MODE
    ):
        raise GmailTemporalRetrievalRunnerError(f"{label} is not owner-only")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise GmailTemporalRetrievalRunnerError(
            f"{label} cannot be opened without following links"
        )
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise GmailTemporalRetrievalRunnerError(f"{label} is unavailable") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != PRIVATE_FILE_MODE
        ):
            raise GmailTemporalRetrievalRunnerError(
                f"{label} changed while it was being opened"
            )
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            raw = handle.read()
            closed = os.fstat(handle.fileno())
            if (
                closed.st_dev != opened.st_dev
                or closed.st_ino != opened.st_ino
                or closed.st_size != opened.st_size
                or closed.st_mtime_ns != opened.st_mtime_ns
            ):
                raise GmailTemporalRetrievalRunnerError(
                    f"{label} changed while it was being read"
                )
            return raw
    except OSError as exc:
        raise GmailTemporalRetrievalRunnerError(f"{label} is unavailable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _private_directory(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise GmailTemporalRetrievalRunnerError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != PRIVATE_DIRECTORY_MODE
    ):
        raise GmailTemporalRetrievalRunnerError(f"{label} is not owner-only")


def _parse_json(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GmailTemporalRetrievalRunnerError(f"{label} is invalid") from exc
    if not isinstance(value, dict) or raw != _canonical_json(value) + b"\n":
        raise GmailTemporalRetrievalRunnerError(f"{label} is not canonical JSON")
    return value


def _parse_jsonl(raw: bytes, *, label: str) -> list[dict[str, Any]]:
    try:
        lines = raw.splitlines()
        rows = [json.loads(line) for line in lines]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GmailTemporalRetrievalRunnerError(f"{label} is invalid") from exc
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise GmailTemporalRetrievalRunnerError(f"{label} is empty or invalid")
    if raw != _jsonl_bytes(rows):
        raise GmailTemporalRetrievalRunnerError(f"{label} is not canonical JSONL")
    return rows


def _identifier(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 256
        or any(character in value for character in "\x00\r\n")
    ):
        raise GmailTemporalRetrievalRunnerError(f"{label} is invalid")
    return value


def _query_text(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 4_000
        or "\x00" in value
    ):
        raise GmailTemporalRetrievalRunnerError("query text is invalid")
    return value


def load_blind_bundle(
    bundle_root: Path,
) -> tuple[
    dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]
]:
    """Load only blind artifacts and verify their public hash commitments."""

    root = Path(bundle_root)
    _private_directory(root, label="retrieval bundle")
    manifest_raw = _private_file(root / MANIFEST_ARTIFACT, label="bundle manifest")
    manifest = _parse_json(manifest_raw, label="bundle manifest")
    artifact_hashes = manifest.get("artifact_sha256")
    if not isinstance(artifact_hashes, dict):
        raise GmailTemporalRetrievalRunnerError("bundle artifact commitment is invalid")
    source_binding_sha256 = manifest.get("source_binding_sha256")
    if (
        not isinstance(source_binding_sha256, str)
        or len(source_binding_sha256) != 64
        or any(
            character not in "0123456789abcdef" for character in source_binding_sha256
        )
    ):
        raise GmailTemporalRetrievalRunnerError(
            "bundle source binding commitment is invalid"
        )

    def load(name: str, *, required: bool) -> list[dict[str, Any]]:
        if name not in artifact_hashes:
            if required:
                raise GmailTemporalRetrievalRunnerError(
                    "bundle blind artifact commitment is incomplete"
                )
            return []
        raw = _private_file(root / name, label=f"bundle {name}")
        if artifact_hashes.get(name) != _sha256_bytes(raw):
            raise GmailTemporalRetrievalRunnerError(
                "bundle blind artifact commitment failed"
            )
        return _parse_jsonl(raw, label=f"bundle {name}")

    source_rows = load(SOURCE_ARTIFACT, required=True)
    primary_rows = load(PRIMARY_QUERY_ARTIFACT, required=True)
    challenge_rows = load(CHALLENGE_QUERY_ARTIFACT, required=False)
    if manifest.get("source_count") != len(source_rows):
        raise GmailTemporalRetrievalRunnerError("bundle source count is invalid")
    if manifest.get("primary_query_count") != len(primary_rows):
        raise GmailTemporalRetrievalRunnerError("bundle primary query count is invalid")
    if manifest.get("challenge_query_count") != len(challenge_rows):
        raise GmailTemporalRetrievalRunnerError(
            "bundle challenge query count is invalid"
        )
    return manifest, source_rows, primary_rows, challenge_rows


def load_source_bindings(
    path: Path,
    *,
    source_rows: Sequence[Mapping[str, Any]],
    expected_sha256: str,
) -> list[dict[str, Any]]:
    raw = _private_file(Path(path), label="source binding")
    if _sha256_bytes(raw) != expected_sha256:
        raise GmailTemporalRetrievalRunnerError(
            "source binding does not match the frozen bundle commitment"
        )
    rows = _parse_jsonl(raw, label="source binding")
    expected_keys = {
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
    source_authority: dict[str, datetime] = {}
    for row in source_rows:
        if set(row) != {"available_at", "source_id", "version"}:
            raise GmailTemporalRetrievalRunnerError("blind source authority is invalid")
        source_id = _identifier(row.get("source_id"), label="source identity")
        if source_id in source_authority:
            raise GmailTemporalRetrievalRunnerError(
                "blind source authority contains duplicates"
            )
        source_authority[source_id] = _parse_timestamp(
            row.get("available_at"), label="source availability"
        )

    seen: set[str] = set()
    seen_chunks: set[str] = set()
    for row in rows:
        if set(row) != expected_keys or row.get("version") != BINDING_VERSION:
            raise GmailTemporalRetrievalRunnerError("source binding schema is invalid")
        source_id = _identifier(row.get("source_id"), label="binding source identity")
        if source_id in seen or source_id not in source_authority:
            raise GmailTemporalRetrievalRunnerError(
                "source binding coverage is invalid"
            )
        seen.add(source_id)
        if (
            _parse_timestamp(row.get("available_at"), label="binding availability")
            != (source_authority[source_id])
        ):
            raise GmailTemporalRetrievalRunnerError(
                "source binding availability does not match blind authority"
            )
        for name in (
            "chunk_inventory_hmac_sha256",
            "document_content_sha256",
            "message_sha256",
        ):
            value = row.get(name)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise GmailTemporalRetrievalRunnerError(
                    "source binding digest is invalid"
                )
        for name in (
            "document_id",
            "gmail_account_key",
            "gmail_message_id",
            "gmail_thread_id",
        ):
            _identifier(row.get(name), label=f"binding {name}")
        chunks = row.get("chunks")
        if not isinstance(chunks, list):
            raise GmailTemporalRetrievalRunnerError(
                "source binding chunk inventory is invalid"
            )
        normalized_chunks: list[dict[str, Any]] = []
        for chunk in chunks:
            if not isinstance(chunk, dict) or set(chunk) != {
                "chunk_id",
                "end_offset",
                "start_offset",
                "text_sha256",
            }:
                raise GmailTemporalRetrievalRunnerError(
                    "source binding chunk inventory is invalid"
                )
            chunk_id = _identifier(
                chunk.get("chunk_id"), label="binding chunk identity"
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
                or len(text_sha256) != 64
                or any(character not in "0123456789abcdef" for character in text_sha256)
            ):
                raise GmailTemporalRetrievalRunnerError(
                    "source binding chunk inventory is invalid"
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
        ):
            raise GmailTemporalRetrievalRunnerError(
                "source binding chunk inventory is invalid"
            )
    if seen != set(source_authority):
        raise GmailTemporalRetrievalRunnerError(
            "source binding does not exactly cover blind authority"
        )
    return rows


def _active_gmail_document_rows(paths: BrainPaths) -> dict[str, dict[str, Any]]:
    uri = f"file:{paths.sqlite_path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, source_type, source_path, raw_path, content_hash, status
            FROM documents
            WHERE source_type = 'gmail_thread' AND status = 'active'
            """
        ).fetchall()
    return {str(row["id"]): dict(row) for row in rows}


def _document_chunks(paths: BrainPaths, document_id: str) -> list[dict[str, Any]]:
    uri = f"file:{paths.sqlite_path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT id AS chunk_id, start_offset, end_offset
                FROM chunks
                WHERE document_id = ?
                ORDER BY chunk_index, id
                """,
                (document_id,),
            )
        ]


def verify_source_bindings(
    paths: BrainPaths, rows: Sequence[Mapping[str, Any]]
) -> dict[str, VerifiedSource]:
    """Recompute every opaque source-to-message/chunk binding from the index."""

    documents = _active_gmail_document_rows(paths)
    rows_by_document: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        rows_by_document.setdefault(str(row["document_id"]), []).append(row)
    if not set(rows_by_document) <= set(documents):
        raise GmailTemporalRetrievalRunnerError(
            "source binding document authority is stale"
        )

    output: dict[str, VerifiedSource] = {}
    used_messages: set[tuple[str, str, str]] = set()
    for document_id, document in documents.items():
        frontmatter, source_path = source_frontmatter_with_path(document)
        timestamps = (
            trusted_gmail_message_timestamps(document, frontmatter, source_path)
            if source_path is not None
            else None
        )
        if timestamps is None or source_path is None:
            raise GmailTemporalRetrievalRunnerError(
                "active Gmail retrieval corpus contains an untrusted projection"
            )
        document_rows = rows_by_document.get(document_id, [])
        timestamp_by_message = {str(item["message_id"]): item for item in timestamps}
        if len(timestamp_by_message) != len(timestamps) or {
            str(row["gmail_message_id"]) for row in document_rows
        } != set(timestamp_by_message):
            raise GmailTemporalRetrievalRunnerError(
                "source binding does not cover every active Gmail message"
            )
        if not timestamps:
            if _document_chunks(paths, document_id):
                raise GmailTemporalRetrievalRunnerError(
                    "active Gmail document has chunks without message authority"
                )
            continue
        try:
            body = strip_frontmatter(source_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise GmailTemporalRetrievalRunnerError(
                "trusted Gmail projection is unavailable"
            ) from exc
        source_by_message: dict[str, str] = {}
        chunks_by_source: dict[str, list[dict[str, Any]]] = {}
        for row in document_rows:
            source_id = str(row["source_id"])
            message_id = str(row["gmail_message_id"])
            message = timestamp_by_message[message_id]
            if (
                document.get("content_hash") != row["document_content_sha256"]
                or frontmatter.get("gmail_account_key") != row["gmail_account_key"]
                or frontmatter.get("gmail_thread_id") != row["gmail_thread_id"]
            ):
                raise GmailTemporalRetrievalRunnerError(
                    "source binding Gmail projection is stale"
                )
            internal_date = _parse_timestamp(
                message.get("internal_date"), label="trusted Gmail internal date"
            )
            available_at = _parse_timestamp(
                row.get("available_at"), label="binding availability"
            )
            if internal_date != available_at:
                raise GmailTemporalRetrievalRunnerError(
                    "source availability is not the trusted Gmail internal date"
                )
            start = int(message["start_offset"])
            end = int(message["end_offset"])
            message_text = body[start:end]
            if _sha256_bytes(message_text.encode("utf-8")) != row["message_sha256"]:
                raise GmailTemporalRetrievalRunnerError(
                    "source binding message content is stale"
                )
            message_key = (str(row["gmail_account_key"]), document_id, message_id)
            if message_key in used_messages:
                raise GmailTemporalRetrievalRunnerError(
                    "multiple opaque sources bind the same Gmail message"
                )
            used_messages.add(message_key)
            source_by_message[message_id] = source_id
            chunks_by_source[source_id] = []
            output[source_id] = VerifiedSource(
                source_id=source_id,
                available_at=available_at,
                document_id=document_id,
                gmail_message_id=message_id,
                chunk_ids=(),
                text=message_text,
            )

        first_message_start = min(int(item["start_offset"]) for item in timestamps)
        latest_source_id = source_by_message[str(timestamps[-1]["message_id"])]
        for chunk in _document_chunks(paths, document_id):
            chunk_id = str(chunk.get("chunk_id") or "")
            chunk_start = chunk.get("start_offset")
            chunk_end = chunk.get("end_offset")
            if (
                not chunk_id
                or not isinstance(chunk_start, int)
                or isinstance(chunk_start, bool)
                or not isinstance(chunk_end, int)
                or isinstance(chunk_end, bool)
                or chunk_start < 0
                or chunk_end <= chunk_start
                or chunk_end > len(body)
            ):
                raise GmailTemporalRetrievalRunnerError(
                    "Gmail retrieval chunk range is invalid"
                )
            if chunk_end <= first_message_start:
                # The thread heading is rendered from the newest nonempty subject.
                # Conservatively clock all pre-message title/header chunks at the
                # latest trusted provider message so a later subject cannot leak
                # into an earlier replay.
                chunks_by_source[latest_source_id].append(
                    {
                        "chunk_id": chunk_id,
                        "end_offset": chunk_end,
                        "start_offset": chunk_start,
                        "text_sha256": _sha256_bytes(
                            body[chunk_start:chunk_end].encode("utf-8")
                        ),
                    }
                )
                continue
            matches = []
            for message_index, message in enumerate(timestamps):
                authority_end = (
                    int(timestamps[message_index + 1]["start_offset"])
                    if message_index + 1 < len(timestamps)
                    else int(message["end_offset"])
                )
                if (
                    int(message["start_offset"]) <= chunk_start
                    and chunk_end <= authority_end
                ):
                    matches.append(message)
            if len(matches) != 1:
                raise GmailTemporalRetrievalRunnerError(
                    "Gmail retrieval chunk crosses a message authority boundary"
                )
            message = matches[0]
            chunks_by_source[source_by_message[str(message["message_id"])]].append(
                {
                    "chunk_id": chunk_id,
                    "end_offset": chunk_end,
                    "start_offset": chunk_start,
                    "text_sha256": _sha256_bytes(
                        body[chunk_start:chunk_end].encode("utf-8")
                    ),
                }
            )

        row_by_source = {str(row["source_id"]): row for row in document_rows}
        for source_id, chunk_inventory in chunks_by_source.items():
            chunk_inventory.sort(
                key=lambda chunk: (
                    chunk["start_offset"],
                    chunk["end_offset"],
                    chunk["chunk_id"],
                )
            )
            if row_by_source[source_id].get("chunks") != chunk_inventory:
                raise GmailTemporalRetrievalRunnerError(
                    "source binding chunk inventory is stale"
                )
            source = output[source_id]
            output[source_id] = VerifiedSource(
                source_id=source.source_id,
                available_at=source.available_at,
                document_id=source.document_id,
                gmail_message_id=source.gmail_message_id,
                chunk_ids=tuple(chunk["chunk_id"] for chunk in chunk_inventory),
                text=source.text,
            )
    return output


class ProductionBrainRetriever:
    """Thin adapter over the public, read-only production retrieval API."""

    def __init__(
        self,
        paths: BrainPaths,
        *,
        mode: str,
        sources: Mapping[str, VerifiedSource],
    ) -> None:
        if mode not in {"source", "temporal"}:
            raise GmailTemporalRetrievalRunnerError("retrieval mode is invalid")
        self.mode = mode
        self.service = BrainService(paths, read_only=True)
        self.source_ids = frozenset(sources)
        self.evidence_sources = tuple(
            RetrospectiveEvidenceSource(
                evidence_id=source.source_id,
                available_at=source.available_at.isoformat(),
                chunk_ids=source.chunk_ids,
            )
            for source in sorted(sources.values(), key=lambda item: item.source_id)
        )

    def __call__(
        self,
        query_text: str,
        as_of: str,
        context_text: str,
        excluded_source_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        result = self.service.retrieve_retrospective_evidence(
            query_text,
            evidence_sources=self.evidence_sources,
            source_available_as_of=as_of,
            retrieval_arm=self.mode,
            excluded_evidence_ids=excluded_source_ids,
            context_text=context_text,
            limit=MAX_RESULTS,
        )
        expected_keys = {
            "version",
            "retrieval_arm",
            "source_available_as_of",
            "ranked_evidence_ids",
            "persisted",
        }
        ranked = result.get("ranked_evidence_ids")
        if (
            set(result) != expected_keys
            or result.get("version") != RETROSPECTIVE_RETRIEVAL_VERSION
            or result.get("retrieval_arm") != self.mode
            or result.get("persisted") is not False
            or not isinstance(ranked, list)
            or len(ranked) > MAX_RESULTS
            or any(
                not isinstance(item, str) or item not in self.source_ids
                for item in ranked
            )
            or len(ranked) != len(set(ranked))
        ):
            raise GmailTemporalRetrievalRunnerError(
                "production retriever returned invalid evidence"
            )
        return tuple(ranked)


def _validate_query_rows(
    rows: Sequence[Mapping[str, Any]], *, challenge: bool
) -> list[dict[str, Any]]:
    expected = {"as_of", "query_id", "query_text", "version"}
    if challenge:
        expected.add("context_source_ids")
    output: list[dict[str, Any]] = []
    previous: str | None = None
    for row in rows:
        if set(row) != expected:
            raise GmailTemporalRetrievalRunnerError("blind query schema is invalid")
        query_id = _identifier(row.get("query_id"), label="query identity")
        query_text = _query_text(row.get("query_text"))
        if previous is not None and query_id <= previous:
            raise GmailTemporalRetrievalRunnerError("blind query order is invalid")
        previous = query_id
        _parse_timestamp(row.get("as_of"), label="query as-of time")
        context = row.get("context_source_ids", [])
        if (
            not isinstance(context, list)
            or any(not isinstance(item, str) or not item for item in context)
            or len(context) != len(set(context))
            or (challenge and not context)
        ):
            raise GmailTemporalRetrievalRunnerError("query context is invalid")
        output.append({**dict(row), "query_text": query_text})
    return output


def run_blind_queries(
    rows: Sequence[Mapping[str, Any]],
    *,
    challenge: bool,
    sources: Mapping[str, VerifiedSource],
    retriever: Callable[[str, str, str, tuple[str, ...]], tuple[str, ...]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for query in _validate_query_rows(rows, challenge=challenge):
        context_ids = set(query.get("context_source_ids") or [])
        if not context_ids <= set(sources):
            raise GmailTemporalRetrievalRunnerError(
                "query context is outside source authority"
            )
        ordered_context_ids = tuple(
            sorted(
                context_ids,
                key=lambda item: (sources[item].available_at, item),
            )
        )
        context_text = "\n\n".join(
            sources[source_id].text for source_id in ordered_context_ids
        )[:MAX_CONTEXT_CHARS]
        ranked = retriever(
            str(query["query_text"]),
            str(query["as_of"]),
            context_text,
            ordered_context_ids,
        )
        cutoff = _parse_timestamp(query["as_of"], label="query as-of time")
        if (
            not isinstance(ranked, tuple)
            or len(ranked) > MAX_RESULTS
            or len(ranked) != len(set(ranked))
            or any(
                source_id not in sources
                or source_id in context_ids
                or sources[source_id].available_at > cutoff
                for source_id in ranked
            )
        ):
            raise GmailTemporalRetrievalRunnerError(
                "retriever returned invalid ranked evidence"
            )
        output.append(
            {
                "query_id": query["query_id"],
                "retrieved": [
                    {"rank": index, "source_id": source_id}
                    for index, source_id in enumerate(ranked, start=1)
                ],
                "version": RESULT_VERSION,
            }
        )
    return output


def _hash_file(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _implementation_artifact_bytes() -> tuple[bytes, str, str, str]:
    """Bind the runner and both production retrieval implementation modules."""

    runner_path = Path(__file__)
    raw_service_path = getattr(brain_service_module, "__file__", None)
    if not isinstance(raw_service_path, str) or not raw_service_path:
        raise GmailTemporalRetrievalRunnerError(
            "production retrieval service implementation is unavailable"
        )
    service_path = Path(raw_service_path)
    raw_retrospective_path = getattr(retrospective_retrieval_module, "__file__", None)
    if not isinstance(raw_retrospective_path, str) or not raw_retrospective_path:
        raise GmailTemporalRetrievalRunnerError(
            "retrospective retrieval implementation is unavailable"
        )
    retrospective_path = Path(raw_retrospective_path)
    try:
        runner_raw = runner_path.read_bytes()
        service_raw = service_path.read_bytes()
        retrospective_raw = retrospective_path.read_bytes()
        runner_source = runner_raw.decode("utf-8")
        service_source = service_raw.decode("utf-8")
        retrospective_source = retrospective_raw.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise GmailTemporalRetrievalRunnerError(
            "retrieval implementation provenance is unavailable"
        ) from exc
    runner_sha256 = _sha256_bytes(runner_raw)
    service_sha256 = _sha256_bytes(service_raw)
    retrospective_sha256 = _sha256_bytes(retrospective_raw)
    artifact = {
        "version": IMPLEMENTATION_PROVENANCE_VERSION,
        "production_api": "BrainService.retrieve_retrospective_evidence",
        "retrospective_retrieval_sha256": retrospective_sha256,
        "retrospective_retrieval_source": retrospective_source,
        "runner_sha256": runner_sha256,
        "runner_source": runner_source,
        "service_sha256": service_sha256,
        "service_source": service_source,
    }
    return (
        _canonical_json(artifact) + b"\n",
        runner_sha256,
        service_sha256,
        retrospective_sha256,
    )


def _tree_commitment(root: Path) -> str | None:
    if not root.is_dir() or root.is_symlink():
        return None
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            rows.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": _hash_file(path),
                }
            )
    return _sha256_bytes(_canonical_json(rows))


def index_commitment(paths: BrainPaths) -> tuple[str, dict[str, str | None]]:
    components = {
        "config_sha256": _hash_file(paths.config_file),
        "embedding_stamp_sha256": _hash_file(paths.embedding_provider_stamp_path),
        "lancedb_tree_sha256": _tree_commitment(paths.lancedb_path),
        "sqlite_sha256": _hash_file(paths.sqlite_path),
        "sqlite_wal_sha256": _hash_file(Path(str(paths.sqlite_path) + "-wal")),
    }
    return _sha256_bytes(_canonical_json(components)), components


def _copy_index_snapshot(source: BrainPaths, destination: BrainPaths) -> None:
    """Create a disposable, transactionally consistent retrieval snapshot."""

    if not source.sqlite_path.is_file() or source.sqlite_path.is_symlink():
        raise GmailTemporalRetrievalRunnerError("Brain SQLite index is unavailable")
    if not source.config_file.is_file() or source.config_file.is_symlink():
        raise GmailTemporalRetrievalRunnerError("Brain retrieval config is unavailable")
    if source.lancedb_path.is_symlink():
        raise GmailTemporalRetrievalRunnerError("Brain vector index is unsafe")
    destination.db_dir.mkdir(parents=True, mode=PRIVATE_DIRECTORY_MODE)
    source_uri = f"file:{source.sqlite_path}?mode=ro"
    try:
        with sqlite3.connect(source_uri, uri=True) as source_conn:
            with sqlite3.connect(destination.sqlite_path) as destination_conn:
                source_conn.backup(destination_conn)
    except sqlite3.Error as exc:
        raise GmailTemporalRetrievalRunnerError(
            "Brain SQLite snapshot could not be frozen"
        ) from exc
    os.chmod(destination.sqlite_path, PRIVATE_FILE_MODE)
    destination.config_local.mkdir(parents=True, mode=PRIVATE_DIRECTORY_MODE)
    shutil.copy2(source.config_file, destination.config_file)
    os.chmod(destination.config_file, PRIVATE_FILE_MODE)
    if source.lancedb_path.exists():
        for path in source.lancedb_path.rglob("*"):
            if path.is_symlink():
                raise GmailTemporalRetrievalRunnerError(
                    "Brain vector index contains a symlink"
                )
        shutil.copytree(source.lancedb_path, destination.lancedb_path)


def _publish(output_root: Path, artifacts: Mapping[str, bytes]) -> None:
    root = Path(output_root)
    parent = root.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise GmailTemporalRetrievalRunnerError("output parent is unsafe")
    parent.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    if root.exists() or root.is_symlink():
        raise GmailTemporalRetrievalRunnerError("output already exists")
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


def execute_retrieval_run(
    brain_home: Path,
    bundle_root: Path,
    binding_path: Path,
    output_root: Path,
    *,
    mode: str,
) -> dict[str, Any]:
    paths = BrainPaths.from_value(brain_home)
    manifest, source_rows, primary_queries, challenge_queries = load_blind_bundle(
        bundle_root
    )
    binding_sha256 = str(manifest["source_binding_sha256"])
    binding_rows = load_source_bindings(
        binding_path,
        source_rows=source_rows,
        expected_sha256=binding_sha256,
    )
    sources = verify_source_bindings(paths, binding_rows)
    live_before_commitment, live_components = index_commitment(paths)
    with tempfile.TemporaryDirectory(prefix="pkm-brain-retrieval-snapshot-") as temp:
        scratch_root = Path(temp)
        os.chmod(scratch_root, PRIVATE_DIRECTORY_MODE)
        scratch_paths = BrainPaths.from_value(scratch_root)
        _copy_index_snapshot(paths, scratch_paths)
        snapshot_commitment, snapshot_components = index_commitment(scratch_paths)
        live_after_snapshot, live_after_snapshot_components = index_commitment(paths)
        if (
            live_before_commitment != live_after_snapshot
            or live_components != live_after_snapshot_components
        ):
            raise GmailTemporalRetrievalRunnerError(
                "live retrieval index changed while the snapshot was created"
            )
        # This timestamp describes the committed scratch artifact that is
        # actually queried, so it must be taken only after copying and hashing.
        snapshot_as_of = datetime.now(timezone.utc).isoformat()
        retriever = ProductionBrainRetriever(
            scratch_paths,
            mode=mode,
            sources=sources,
        )
        primary_results = run_blind_queries(
            primary_queries,
            challenge=False,
            sources=sources,
            retriever=retriever,
        )
        challenge_results = run_blind_queries(
            challenge_queries,
            challenge=True,
            sources=sources,
            retriever=retriever,
        )
        snapshot_after_commitment, snapshot_after_components = index_commitment(
            scratch_paths
        )
        if (
            snapshot_commitment != snapshot_after_commitment
            or snapshot_components != snapshot_after_components
        ):
            raise GmailTemporalRetrievalRunnerError(
                "disposable retrieval snapshot changed during execution"
            )
    live_after_commitment, live_after_components = index_commitment(paths)
    if (
        live_before_commitment != live_after_commitment
        or live_components != live_after_components
    ):
        raise GmailTemporalRetrievalRunnerError(
            "retrieval index changed during read-only execution"
        )
    (
        implementation_raw,
        runner_sha256,
        service_sha256,
        retrospective_sha256,
    ) = _implementation_artifact_bytes()
    configuration = {
        "version": CONFIG_VERSION,
        "runner_version": VERSION,
        "mode": mode,
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
        "binding_sha256": binding_sha256,
        "production_retrieval_api": "BrainService.retrieve_retrospective_evidence",
        "production_retrieval_api_version": RETROSPECTIVE_RETRIEVAL_VERSION,
        "runner_sha256": runner_sha256,
        "service_sha256": service_sha256,
        "retrospective_retrieval_sha256": retrospective_sha256,
        "implementation_provenance_sha256": _sha256_bytes(implementation_raw),
        # Keep the generic index fields tied to the disposable artifact queried
        # by this run.  The live source commitment is separately recorded so a
        # copy from some other index cannot masquerade as the source snapshot.
        "index_components": snapshot_components,
        "snapshot_index_artifact_sha256": snapshot_commitment,
        "snapshot_index_components": snapshot_components,
        "live_source_index_artifact_sha256": live_before_commitment,
        "live_source_index_components": live_components,
    }
    receipt = {
        "version": INDEX_RECEIPT_VERSION,
        "blind_primary_queries_sha256": manifest["artifact_sha256"][
            PRIMARY_QUERY_ARTIFACT
        ],
        "index_artifact_sha256": snapshot_commitment,
        "index_components": snapshot_components,
        "snapshot_as_of": snapshot_as_of,
        "source_authority_sha256": manifest["artifact_sha256"][SOURCE_ARTIFACT],
        "source_count": len(source_rows),
    }
    artifacts = {
        PRIMARY_RESULT_ARTIFACT: _jsonl_bytes(primary_results),
        CONFIG_ARTIFACT: _canonical_json(configuration) + b"\n",
        INDEX_RECEIPT_ARTIFACT: _canonical_json(receipt) + b"\n",
        QUERY_PROTOCOL_ARTIFACT: QUERY_PROTOCOL_BYTES,
        IMPLEMENTATION_ARTIFACT: implementation_raw,
    }
    if challenge_queries:
        artifacts[CHALLENGE_RESULT_ARTIFACT] = _jsonl_bytes(challenge_results)
    _publish(Path(output_root), artifacts)
    return {
        "version": VERSION,
        "status": "completed",
        "mode": mode,
        "sources": len(sources),
        "primary_queries": len(primary_results),
        "challenge_queries": len(challenge_results),
        "primary_results": sum(len(row["retrieved"]) for row in primary_results),
        "challenge_results": sum(len(row["retrieved"]) for row in challenge_results),
        "index_unchanged": True,
        "live_source_index_unchanged": True,
        "snapshot_index_unchanged": True,
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
                "error": "gmail_temporal_retrieval_runner_failed",
                "private_content_printed": False,
            },
            sort_keys=True,
        )
    )
    raise SystemExit(2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brain-home", type=Path, required=True)
    parser.add_argument("--bundle-root", type=Path, required=True)
    parser.add_argument("--source-bindings", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("source", "temporal"), required=True)
    args = parser.parse_args()
    try:
        result = execute_retrieval_run(
            args.brain_home,
            args.bundle_root,
            args.source_bindings,
            args.output_root,
            mode=args.mode,
        )
    except Exception:  # noqa: BLE001 - private inputs must never reach stderr/stdout
        _failure()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
