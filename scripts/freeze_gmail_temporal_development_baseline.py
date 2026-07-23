#!/usr/bin/env python3
"""Freeze the current Gmail thread universe for future holdout exclusion.

This command is a local, read-only corpus audit.  It validates every active or
deleted projection-v7 Gmail document against the connector-authored timestamp
and message-policy indexes, then writes only keyed opaque thread scopes.  A
future release holdout built with the same HMAC key can prove that none of its
threads overlap this development baseline without retaining or disclosing
provider IDs.  Deleted tombstones are included so a previously seen thread
cannot re-enter a future holdout after provider-side deletion and reappearance.

The command performs no model, network, or Brain persistence calls.  Standard
output contains aggregate counts and digests only.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from pkm_brain.gmail_projection import (
    GMAIL_KNOWLEDGE_CLASSIFIER_VERSION,
    GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    GMAIL_MESSAGE_POLICY_VERSION,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.source_dates import (
    source_frontmatter_with_path,
    strict_int,
    trusted_gmail_message_policies,
    trusted_gmail_message_timestamps,
)


VERSION = "gmail_temporal_development_baseline_freezer_v2"
THREAD_SCOPE_VERSION = "gmail_temporal_development_thread_scope_v1"
MANIFEST_VERSION = "gmail_temporal_development_baseline_manifest_v2"
THREAD_SCOPE_NAMESPACE = "gmail_temporal_thread_scope_v1"
AUTHORITY_COMMITMENT_NAMESPACE = "gmail_temporal_development_authority_v1"
COUNT_COMMITMENT_NAMESPACE = "gmail_temporal_development_message_count_v1"
MANIFEST_HMAC_NAMESPACE = "gmail_temporal_development_manifest_v1"
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
MIN_HMAC_KEY_BYTES = 32
MAX_HMAC_KEY_BYTES = 4096
EXPECTED_PROJECTION_VERSION = 7
EXPECTED_CLASSIFIER_VERSION = 5
EXPECTED_MESSAGE_POLICY_VERSION = 1
THREAD_SCOPE_ARTIFACT = "development-thread-scopes.jsonl"
MANIFEST_ARTIFACT = "development-baseline-manifest.json"
OUTPUT_ARTIFACT_NAMES = (THREAD_SCOPE_ARTIFACT, MANIFEST_ARTIFACT)
STATIC_CLI_FAILURE = '{"error":"gmail_temporal_development_baseline_failed"}'


class GmailTemporalDevelopmentBaselineError(ValueError):
    """Raised when the development corpus cannot be frozen safely."""


@dataclass(frozen=True)
class _FreezeResult:
    scope_rows: tuple[dict[str, Any], ...]
    document_count: int
    active_document_count: int
    deleted_document_count: int
    active_target_count: int
    deleted_thread_count: int


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _jsonl_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical_json(dict(row)) + b"\n" for row in rows)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _policy_namespace() -> str:
    return (
        f"gmail_projection_v{EXPECTED_PROJECTION_VERSION}"
        f"_classifier_v{EXPECTED_CLASSIFIER_VERSION}"
        f"_message_policy_v{EXPECTED_MESSAGE_POLICY_VERSION}"
    )


def _hmac_hex(key: bytes, namespace: str, value: Any) -> str:
    payload = namespace.encode("ascii") + b"\0" + _canonical_json(value)
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _thread_scope_id(key: bytes, account_key: str, thread_id: str) -> str:
    # Deliberately exclude revision and policy versions.  The exclusion identity
    # means "this provider thread was used in development," not "this exact
    # rendering was used in development."
    return "gtdb_t_" + _hmac_hex(
        key,
        THREAD_SCOPE_NAMESPACE,
        {"account_key": account_key, "thread_id": thread_id},
    )


def _private_hmac_key(path: Path) -> bytes:
    descriptor: int | None = None
    info: os.stat_result | None = None
    value = b""
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        info = os.fstat(descriptor)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            value = handle.read(MAX_HMAC_KEY_BYTES + 1)
    except OSError as exc:
        raise GmailTemporalDevelopmentBaselineError("HMAC key is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        info is None
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != PRIVATE_FILE_MODE
        or info.st_nlink != 1
        or not MIN_HMAC_KEY_BYTES <= len(value) <= MAX_HMAC_KEY_BYTES
        or info.st_size != len(value)
    ):
        raise GmailTemporalDevelopmentBaselineError(
            "HMAC key must be an owner-only single-link regular file"
        )
    return value


def _read_gmail_documents(paths: BrainPaths) -> list[dict[str, Any]]:
    database = paths.sqlite_path
    if database.is_symlink() or not database.is_file():
        raise GmailTemporalDevelopmentBaselineError("Brain database is unavailable")
    try:
        conn = sqlite3.connect(
            f"{database.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=1.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            """
            SELECT * FROM documents
            WHERE source_type = 'gmail_thread'
              AND status IN ('active', 'deleted')
            ORDER BY id
            """
        ).fetchall()
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise GmailTemporalDevelopmentBaselineError(
            "Gmail document inventory is unavailable"
        ) from exc
    finally:
        if "conn" in locals():
            conn.close()
    return [dict(row) for row in rows]


def _hex_sha256(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) != 64:
        return None
    return (
        value if all(character in "0123456789abcdef" for character in value) else None
    )


def _freeze_scope_rows(
    documents: Sequence[Mapping[str, Any]],
    key: bytes,
    *,
    include_message_count_commitments: bool,
) -> _FreezeResult:
    if not documents:
        raise GmailTemporalDevelopmentBaselineError("Gmail document inventory is empty")
    rows: list[dict[str, Any]] = []
    seen_threads: set[tuple[str, str]] = set()
    seen_targets: set[tuple[str, str]] = set()
    active_target_count = 0
    active_document_count = 0
    deleted_document_count = 0
    deleted_thread_count = 0

    for raw_document in documents:
        document = dict(raw_document)
        frontmatter, source_path = source_frontmatter_with_path(document)
        timestamps = trusted_gmail_message_timestamps(
            document,
            frontmatter,
            source_path,
        )
        policies = trusted_gmail_message_policies(
            document,
            frontmatter,
            source_path,
        )
        account_key = frontmatter.get("gmail_account_key")
        thread_id = frontmatter.get("gmail_thread_id")
        source_revision = _hex_sha256(frontmatter.get("gmail_source_revision"))
        content_hash = _hex_sha256(document.get("content_hash"))
        message_ids = frontmatter.get("gmail_message_ids")
        retained_count = strict_int(frontmatter.get("retained_message_count"))
        omitted_count = strict_int(frontmatter.get("omitted_message_count"))
        truncated_count = strict_int(frontmatter.get("truncated_message_count"))
        deleted = frontmatter.get("deleted")
        document_id = document.get("id")
        document_status = document.get("status")
        if (
            GMAIL_KNOWLEDGE_PROJECTION_VERSION != EXPECTED_PROJECTION_VERSION
            or GMAIL_KNOWLEDGE_CLASSIFIER_VERSION != EXPECTED_CLASSIFIER_VERSION
            or GMAIL_MESSAGE_POLICY_VERSION != EXPECTED_MESSAGE_POLICY_VERSION
            or strict_int(frontmatter.get("gmail_projection_version"))
            != EXPECTED_PROJECTION_VERSION
            or strict_int(frontmatter.get("gmail_classifier_version"))
            != EXPECTED_CLASSIFIER_VERSION
            or strict_int(frontmatter.get("gmail_message_policy_version"))
            != EXPECTED_MESSAGE_POLICY_VERSION
            or not isinstance(document_id, str)
            or not document_id
            or not isinstance(account_key, str)
            or not account_key
            or not isinstance(thread_id, str)
            or not thread_id
            or source_revision is None
            or content_hash is None
            or source_path is None
            or source_path.is_symlink()
            or not source_path.is_file()
            or timestamps is None
            or policies is None
            or not isinstance(message_ids, list)
            or retained_count != len(message_ids)
            or omitted_count is None
            or omitted_count < 0
            or truncated_count is None
            or truncated_count < 0
            or truncated_count > retained_count
            or not isinstance(deleted, bool)
            or document_status not in {"active", "deleted"}
            or deleted is not (document_status == "deleted")
            or len(timestamps) != retained_count
            or len(policies) != retained_count
        ):
            raise GmailTemporalDevelopmentBaselineError(
                "projection-v7 Gmail authority is incomplete"
            )

        thread_key = (account_key, thread_id)
        if thread_key in seen_threads:
            raise GmailTemporalDevelopmentBaselineError(
                "Gmail thread authority is duplicated"
            )
        seen_threads.add(thread_key)
        for message_id in message_ids:
            target_key = (account_key, message_id)
            if target_key in seen_targets:
                raise GmailTemporalDevelopmentBaselineError(
                    "Gmail target authority is duplicated"
                )
            seen_targets.add(target_key)

        scope_id = _thread_scope_id(key, account_key, thread_id)
        authority_payload = {
            "document_id": document_id,
            "account_key": account_key,
            "thread_id": thread_id,
            "source_revision": source_revision,
            "content_hash": content_hash,
            "message_ids": message_ids,
            "message_timestamps": timestamps,
            "message_policies": policies,
            "fact_admitted_message_ids": frontmatter.get(
                "gmail_fact_admitted_message_ids"
            ),
            "retained_message_count": retained_count,
            "omitted_message_count": omitted_count,
            "truncated_message_count": truncated_count,
            "deleted": deleted,
            "fact_eligible": frontmatter.get("fact_eligible"),
            "policy_namespace": _policy_namespace(),
            "document_status": document_status,
        }
        row: dict[str, Any] = {
            "version": THREAD_SCOPE_VERSION,
            "thread_scope_id": scope_id,
            "source_authority_commitment": "gtdb_a_"
            + _hmac_hex(key, AUTHORITY_COMMITMENT_NAMESPACE, authority_payload),
        }
        if include_message_count_commitments:
            row["message_count_commitment"] = "gtdb_n_" + _hmac_hex(
                key,
                COUNT_COMMITMENT_NAMESPACE,
                {
                    "account_key": account_key,
                    "thread_id": thread_id,
                    "retained": retained_count,
                    "omitted": omitted_count,
                    "truncated": truncated_count,
                },
            )
        rows.append(row)
        active_target_count += retained_count
        active_document_count += int(document_status == "active")
        deleted_document_count += int(document_status == "deleted")
        deleted_thread_count += int(deleted)

    rows.sort(key=lambda row: row["thread_scope_id"])
    if len({row["thread_scope_id"] for row in rows}) != len(rows):
        raise GmailTemporalDevelopmentBaselineError(
            "opaque Gmail thread scope authority is duplicated"
        )
    return _FreezeResult(
        scope_rows=tuple(rows),
        document_count=len(documents),
        active_document_count=active_document_count,
        deleted_document_count=deleted_document_count,
        active_target_count=active_target_count,
        deleted_thread_count=deleted_thread_count,
    )


def _manifest_hmac(key: bytes, manifest_without_hmac: Mapping[str, Any]) -> str:
    return _hmac_hex(key, MANIFEST_HMAC_NAMESPACE, dict(manifest_without_hmac))


def _write_private_new(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != PRIVATE_FILE_MODE
            or info.st_nlink != 1
        ):
            raise OSError("unsafe private output artifact")
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise GmailTemporalDevelopmentBaselineError(
            "private output artifact write failed"
        ) from exc


def _publish_private_new(output_root: Path, artifacts: Mapping[str, bytes]) -> None:
    if set(artifacts) != set(OUTPUT_ARTIFACT_NAMES):
        raise GmailTemporalDevelopmentBaselineError(
            "private output artifact set is incomplete"
        )
    if output_root.exists() or output_root.is_symlink():
        raise GmailTemporalDevelopmentBaselineError("frozen output path already exists")
    parent = output_root.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise GmailTemporalDevelopmentBaselineError("output parent is unsafe")
    parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    claimed = False
    try:
        output_root.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        claimed = True
        output_root.chmod(PRIVATE_DIRECTORY_MODE)
        for name in OUTPUT_ARTIFACT_NAMES:
            _write_private_new(output_root / name, artifacts[name])
    except FileExistsError as exc:
        raise GmailTemporalDevelopmentBaselineError(
            "frozen output path already exists"
        ) from exc
    except Exception:
        if claimed and output_root.is_dir() and not output_root.is_symlink():
            for name in OUTPUT_ARTIFACT_NAMES:
                artifact = output_root / name
                if artifact.is_file() and not artifact.is_symlink():
                    artifact.unlink()
            try:
                output_root.rmdir()
            except OSError:
                pass
        raise


def freeze_gmail_temporal_development_baseline(
    brain_home: Path,
    hmac_key_path: Path,
    output_root: Path,
    *,
    include_message_count_commitments: bool = False,
) -> dict[str, Any]:
    """Freeze current opaque thread scopes and return aggregate-only evidence."""

    key = _private_hmac_key(hmac_key_path)
    paths = BrainPaths.from_value(brain_home)
    documents = _read_gmail_documents(paths)
    frozen = _freeze_scope_rows(
        documents,
        key,
        include_message_count_commitments=include_message_count_commitments,
    )
    scopes_bytes = _jsonl_bytes(frozen.scope_rows)
    scopes_sha256 = _sha256(scopes_bytes)
    artifact_set_sha256 = _sha256(
        _canonical_json({THREAD_SCOPE_ARTIFACT: scopes_sha256})
    )
    corpus_fingerprint = "gtdb_c_" + _sha256(
        _canonical_json(
            {
                "policy_namespace": _policy_namespace(),
                "document_count": frozen.document_count,
                "active_document_count": frozen.active_document_count,
                "deleted_document_count": frozen.deleted_document_count,
                "active_target_count": frozen.active_target_count,
                "scope_rows": [
                    {
                        "version": row["version"],
                        "thread_scope_id": row["thread_scope_id"],
                        "source_authority_commitment": row[
                            "source_authority_commitment"
                        ],
                    }
                    for row in frozen.scope_rows
                ],
            }
        )
    )
    manifest_without_hmac: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "builder_version": VERSION,
        "purpose": "exclude_current_and_deleted_development_threads_from_future_release_holdouts",
        "thread_scope_version": THREAD_SCOPE_VERSION,
        "thread_scope_namespace": THREAD_SCOPE_NAMESPACE,
        "thread_scope_algorithm": "HMAC-SHA256(account_key,thread_id)",
        "id_namespace": "gtdb_k_"
        + _hmac_hex(key, THREAD_SCOPE_NAMESPACE, "id-namespace"),
        "policy_namespace": _policy_namespace(),
        "projection_version": EXPECTED_PROJECTION_VERSION,
        "classifier_version": EXPECTED_CLASSIFIER_VERSION,
        "message_policy_version": EXPECTED_MESSAGE_POLICY_VERSION,
        "document_count": frozen.document_count,
        "active_document_count": frozen.active_document_count,
        "deleted_document_count": frozen.deleted_document_count,
        "thread_scope_count": len(frozen.scope_rows),
        "active_thread_scope_count": frozen.active_document_count,
        "active_target_message_count": frozen.active_target_count,
        "deleted_thread_scope_count": frozen.deleted_thread_count,
        "message_count_commitments_included": include_message_count_commitments,
        "corpus_fingerprint": corpus_fingerprint,
        "thread_scope_artifact": {
            "name": THREAD_SCOPE_ARTIFACT,
            "row_count": len(frozen.scope_rows),
            "sha256": scopes_sha256,
        },
        "artifact_set_sha256": artifact_set_sha256,
        "manifest_hmac_namespace": MANIFEST_HMAC_NAMESPACE,
        "private_file_mode": "0600",
        "private_directory_mode": "0700",
        "provider_identifiers_emitted": False,
        "message_content_emitted": False,
        "external_calls": 0,
        "persistence_calls": 0,
    }
    manifest = {
        **manifest_without_hmac,
        "manifest_hmac": _manifest_hmac(key, manifest_without_hmac),
    }
    manifest_bytes = _canonical_json(manifest) + b"\n"
    _publish_private_new(
        output_root,
        {
            THREAD_SCOPE_ARTIFACT: scopes_bytes,
            MANIFEST_ARTIFACT: manifest_bytes,
        },
    )
    return {
        "version": VERSION,
        "documents": frozen.document_count,
        "active_documents": frozen.active_document_count,
        "deleted_documents": frozen.deleted_document_count,
        "thread_scopes": len(frozen.scope_rows),
        "active_target_messages": frozen.active_target_count,
        "deleted_thread_scopes": frozen.deleted_thread_count,
        "message_count_commitments_included": include_message_count_commitments,
        "corpus_fingerprint": corpus_fingerprint,
        "artifact_set_sha256": artifact_set_sha256,
        "aggregate_only": True,
        "private_content_printed": False,
        "external_calls": 0,
        "persistence_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("brain_home", type=Path)
    parser.add_argument("hmac_key", type=Path)
    parser.add_argument("output_root", type=Path)
    parser.add_argument(
        "--include-message-count-commitments",
        action="store_true",
        help="bind per-thread retained/omitted/truncated counts without emitting them",
    )
    args = parser.parse_args()
    try:
        result = freeze_gmail_temporal_development_baseline(
            args.brain_home,
            args.hmac_key,
            args.output_root,
            include_message_count_commitments=(args.include_message_count_commitments),
        )
    except Exception:
        print(STATIC_CLI_FAILURE, file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
