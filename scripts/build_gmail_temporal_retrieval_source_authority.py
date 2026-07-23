#!/usr/bin/env python3
"""Freeze a complete, opaque Gmail source authority for retrieval evaluation.

The retrieval benchmark must let irrelevant mail occupy ranked positions.  It
therefore cannot bind only the messages named by the gold set.  This builder
walks every active, trusted Gmail projection in one Brain index and emits:

* an opaque source authority containing every indexed Gmail message; and
* a private identity binding that the retrieval runner can re-verify against
  the exact index snapshot before executing any query.

Provider identifiers remain only in the owner-only binding.  Source and thread
identities are HMAC-derived.  The script performs no model, network, Gmail, or
Brain write.
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


VERSION = "gmail_temporal_retrieval_source_authority_builder_v3"
SOURCE_VERSION = "gmail_temporal_retrieval_source_v1"
BINDING_VERSION = "gmail_temporal_retrieval_source_binding_v3"
MANIFEST_VERSION = "gmail_temporal_retrieval_source_authority_manifest_v2"
MANIFEST_DOMAIN = b"gmail_temporal_retrieval_source_authority_manifest_v2\0"
CHUNK_INVENTORY_DOMAIN = b"gmail_temporal_retrieval_chunk_inventory_v1\0"
SOURCE_ARTIFACT = "source-authority.jsonl"
BINDING_ARTIFACT = "source-bindings.jsonl"
MANIFEST_ARTIFACT = "manifest.json"
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
MIN_HMAC_KEY_BYTES = 32
MAX_HMAC_KEY_BYTES = 4096


class GmailTemporalRetrievalSourceAuthorityError(ValueError):
    """Raised when the complete Gmail retrieval authority cannot be proven."""


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
            raise GmailTemporalRetrievalSourceAuthorityError(
                f"{label} is unavailable or unsafe"
            )
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise GmailTemporalRetrievalSourceAuthorityError(
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
            raise GmailTemporalRetrievalSourceAuthorityError(
                f"{label} changed while read"
            )
        return b"".join(chunks)
    except OSError as exc:
        raise GmailTemporalRetrievalSourceAuthorityError(
            f"{label} is unavailable or unsafe"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _hmac_key(path: Path) -> bytes:
    key = _private_file(path, label="HMAC key")
    if not MIN_HMAC_KEY_BYTES <= len(key) <= MAX_HMAC_KEY_BYTES:
        raise GmailTemporalRetrievalSourceAuthorityError("HMAC key length is invalid")
    return key


def _parse_timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise GmailTemporalRetrievalSourceAuthorityError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GmailTemporalRetrievalSourceAuthorityError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise GmailTemporalRetrievalSourceAuthorityError(
            f"{label} must include a timezone"
        )
    return parsed.astimezone(timezone.utc)


def _opaque_id(key: bytes, *, kind: str, values: Sequence[str]) -> str:
    material = _canonical_json([kind, *values])
    digest = hmac.new(
        key,
        b"gmail_temporal_retrieval_source_authority_v1\0" + material,
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


def _active_gmail_documents(paths: BrainPaths) -> list[dict[str, Any]]:
    uri = f"file:{paths.sqlite_path}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as conn:
            conn.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, source_type, source_path, raw_path, content_hash, status
                    FROM documents
                    WHERE source_type = 'gmail_thread' AND status = 'active'
                    ORDER BY id
                    """
                )
            ]
    except sqlite3.Error as exc:
        raise GmailTemporalRetrievalSourceAuthorityError(
            "Brain Gmail document authority is unavailable"
        ) from exc


def _document_chunks(paths: BrainPaths, document_id: str) -> list[dict[str, Any]]:
    uri = f"file:{paths.sqlite_path}?mode=ro"
    try:
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
    except sqlite3.Error as exc:
        raise GmailTemporalRetrievalSourceAuthorityError(
            "Brain Gmail chunk authority is unavailable"
        ) from exc


def _chunk_message_assignments(
    chunks: Sequence[Mapping[str, Any]],
    messages: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """Bind every indexed chunk to one message without crossing boundaries.

    Gmail markdown creates one title section before its message sections.  The
    title is selected from the latest non-empty subject, so that header chunk is
    conservatively assigned to the latest retained message and unavailable
    before that message's provider clock.
    """

    if not messages:
        raise GmailTemporalRetrievalSourceAuthorityError(
            "active Gmail projection has no trusted messages"
        )
    first_start = int(messages[0]["start_offset"])
    latest_message_id = str(messages[-1]["message_id"])
    output: dict[str, str] = {}
    for raw_chunk in chunks:
        chunk_id = str(raw_chunk.get("chunk_id") or "").strip()
        start = raw_chunk.get("start_offset")
        end = raw_chunk.get("end_offset")
        if (
            not chunk_id
            or isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 0
            or end <= start
            or chunk_id in output
        ):
            raise GmailTemporalRetrievalSourceAuthorityError(
                "Gmail chunk range authority is invalid"
            )
        matches = []
        for index, message in enumerate(messages):
            # Markdown section chunks may include the two separator newlines
            # after a message.  Those bytes predate the next message and remain
            # part of the preceding message's safe authority envelope.
            authority_end = (
                int(messages[index + 1]["start_offset"])
                if index + 1 < len(messages)
                else int(message["end_offset"])
            )
            if int(message["start_offset"]) <= start and end <= authority_end:
                matches.append(str(message["message_id"]))
        if not matches and end <= first_start:
            matches = [latest_message_id]
        if len(matches) != 1:
            raise GmailTemporalRetrievalSourceAuthorityError(
                "Gmail chunk does not resolve to exactly one trusted message"
            )
        output[chunk_id] = matches[0]
    return output


def _authenticated_chunk_inventories(
    chunks: Sequence[Mapping[str, Any]],
    messages: Sequence[Mapping[str, Any]],
    body: str,
) -> dict[str, list[dict[str, Any]]]:
    """Return the exact, content-bound chunk inventory for each message."""

    assignments = _chunk_message_assignments(chunks, messages)
    inventories = {str(message["message_id"]): [] for message in messages}
    for raw_chunk in chunks:
        chunk_id = str(raw_chunk["chunk_id"])
        start = int(raw_chunk["start_offset"])
        end = int(raw_chunk["end_offset"])
        if end > len(body):
            raise GmailTemporalRetrievalSourceAuthorityError(
                "Gmail chunk range exceeds trusted projection content"
            )
        inventories[assignments[chunk_id]].append(
            {
                "chunk_id": chunk_id,
                "end_offset": end,
                "start_offset": start,
                "text_sha256": _sha256_bytes(body[start:end].encode("utf-8")),
            }
        )
    for inventory in inventories.values():
        inventory.sort(
            key=lambda chunk: (
                chunk["start_offset"],
                chunk["end_offset"],
                chunk["chunk_id"],
            )
        )
    return inventories


def derive_source_authority(
    paths: BrainPaths,
    *,
    key: bytes,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Derive complete source and private binding rows from one Brain index."""

    source_rows: list[dict[str, Any]] = []
    binding_rows: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    seen_messages: set[tuple[str, str]] = set()
    seen_chunks: set[str] = set()
    document_count = 0
    for document in _active_gmail_documents(paths):
        document_id = str(document.get("id") or "").strip()
        content_hash = str(document.get("content_hash") or "").strip()
        if not document_id or len(content_hash) != 64:
            raise GmailTemporalRetrievalSourceAuthorityError(
                "active Gmail document authority is invalid"
            )
        frontmatter, source_path = source_frontmatter_with_path(document)
        timestamps = (
            trusted_gmail_message_timestamps(document, frontmatter, source_path)
            if source_path is not None
            else None
        )
        account = str(frontmatter.get("gmail_account_key") or "").strip()
        thread = str(frontmatter.get("gmail_thread_id") or "").strip()
        if timestamps is None or not timestamps or not account or not thread:
            raise GmailTemporalRetrievalSourceAuthorityError(
                "active Gmail projection is not trusted"
            )
        try:
            body = strip_frontmatter(source_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise GmailTemporalRetrievalSourceAuthorityError(
                "active Gmail projection is unavailable"
            ) from exc
        document_chunks = _document_chunks(paths, document_id)
        chunk_inventories = _authenticated_chunk_inventories(
            document_chunks, timestamps, body
        )
        document_chunk_ids = {
            str(chunk["chunk_id"])
            for inventory in chunk_inventories.values()
            for chunk in inventory
        }
        if seen_chunks & document_chunk_ids:
            raise GmailTemporalRetrievalSourceAuthorityError(
                "Gmail chunk identity is not unique"
            )
        seen_chunks.update(document_chunk_ids)
        thread_scope_id = _opaque_id(key, kind="thread", values=(account, thread))
        for message in timestamps:
            message_id = str(message["message_id"])
            message_key = (account, message_id)
            if message_key in seen_messages:
                raise GmailTemporalRetrievalSourceAuthorityError(
                    "active Gmail message authority contains duplicates"
                )
            seen_messages.add(message_key)
            source_id = _opaque_id(key, kind="source", values=(account, message_id))
            if source_id in seen_sources:
                raise GmailTemporalRetrievalSourceAuthorityError(
                    "opaque Gmail source identity collided"
                )
            seen_sources.add(source_id)
            available_at = _parse_timestamp(
                message.get("internal_date"), label="Gmail provider internal date"
            ).isoformat()
            start = int(message["start_offset"])
            end = int(message["end_offset"])
            message_text = body[start:end]
            chunks = chunk_inventories[message_id]
            source_rows.append(
                {
                    "available_at": available_at,
                    "source_id": source_id,
                    "thread_scope_id": thread_scope_id,
                    "version": SOURCE_VERSION,
                }
            )
            binding_rows.append(
                {
                    "available_at": available_at,
                    "document_content_sha256": content_hash,
                    "document_id": document_id,
                    "gmail_account_key": account,
                    "gmail_message_id": message_id,
                    "gmail_thread_id": thread,
                    "message_sha256": _sha256_bytes(message_text.encode("utf-8")),
                    "source_id": source_id,
                    "chunk_inventory_hmac_sha256": _chunk_inventory_authenticator(
                        key,
                        source_id=source_id,
                        document_id=document_id,
                        gmail_account_key=account,
                        gmail_message_id=message_id,
                        gmail_thread_id=thread,
                        chunks=chunks,
                    ),
                    "chunks": chunks,
                    "version": BINDING_VERSION,
                }
            )
        document_count += 1
    if not source_rows:
        raise GmailTemporalRetrievalSourceAuthorityError(
            "Brain index contains no active trusted Gmail messages"
        )
    source_rows.sort(key=lambda row: (row["thread_scope_id"], row["source_id"]))
    binding_rows.sort(key=lambda row: row["source_id"])
    summary = {
        "document_count": document_count,
        "message_count": len(source_rows),
        "chunk_count": len(seen_chunks),
    }
    return source_rows, binding_rows, summary


def _publish(root: Path, artifacts: Mapping[str, bytes]) -> None:
    parent = root.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise GmailTemporalRetrievalSourceAuthorityError("output parent is unsafe")
    parent.mkdir(parents=True, mode=PRIVATE_DIRECTORY_MODE, exist_ok=True)
    if root.exists() or root.is_symlink():
        raise GmailTemporalRetrievalSourceAuthorityError("output already exists")
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


def build_source_authority(
    brain_home: Path,
    hmac_key_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    key = _hmac_key(Path(hmac_key_path))
    source_rows, binding_rows, summary = derive_source_authority(
        BrainPaths.from_value(brain_home), key=key
    )
    source_raw = _jsonl_bytes(source_rows)
    binding_raw = _jsonl_bytes(binding_rows)
    unsigned_manifest = {
        "version": MANIFEST_VERSION,
        "builder_version": VERSION,
        "source_version": SOURCE_VERSION,
        "binding_version": BINDING_VERSION,
        "artifact_sha256": {
            SOURCE_ARTIFACT: _sha256_bytes(source_raw),
            BINDING_ARTIFACT: _sha256_bytes(binding_raw),
        },
        **summary,
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
        **unsigned_manifest,
        "manifest_hmac_sha256": hmac.new(
            key,
            MANIFEST_DOMAIN + _canonical_json(unsigned_manifest),
            hashlib.sha256,
        ).hexdigest(),
    }
    _publish(
        Path(output_root),
        {
            SOURCE_ARTIFACT: source_raw,
            BINDING_ARTIFACT: binding_raw,
            MANIFEST_ARTIFACT: _canonical_json(manifest) + b"\n",
        },
    )
    return {
        "version": VERSION,
        "status": "completed",
        **summary,
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("brain_home", type=Path)
    parser.add_argument("hmac_key", type=Path)
    parser.add_argument("output_root", type=Path)
    args = parser.parse_args()
    try:
        result = build_source_authority(
            args.brain_home, args.hmac_key, args.output_root
        )
    except GmailTemporalRetrievalSourceAuthorityError:
        result = {
            "version": VERSION,
            "status": "failed",
            "error": "gmail_temporal_retrieval_source_authority_failed",
            "external_calls": 0,
            "persistence_calls": 0,
            "private_content_printed": False,
        }
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
