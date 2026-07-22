#!/usr/bin/env python3
"""Build a private, identical-packet Gmail fact-parity cohort.

The builder joins two complete admission inventories against one canonical Gmail
projection snapshot.  It freezes the union of messages admitted by either arm,
replaces provider identifiers used as benchmark keys with keyed opaque IDs, and
writes identical message packets for both extractors to consume.

Private message text is written only to mode-0600 local artifacts.  The command
prints aggregate counts and artifact digests, never message text, provider IDs,
or local source paths.  It performs no network or model calls.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from pkm_brain.chunking import strip_frontmatter


VERSION = "gmail_fact_parity_cohort_builder_v1"
ADMISSION_VERSION = "gmail_fact_parity_admission_v1"
PACKET_VERSION = "gmail_fact_parity_packet_v1"
COHORT_VERSION = "gmail_fact_parity_cohort_v1"
JOIN_VERSION = "gmail_fact_parity_admission_join_v1"
MANIFEST_VERSION = "gmail_fact_parity_cohort_manifest_v1"
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
MIN_HMAC_KEY_BYTES = 32
OUTPUT_ARTIFACT_NAMES = (
    "packets.jsonl",
    "cohort.jsonl",
    "admissions.jsonl",
    "manifest.json",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_ADMISSION_KEYS = {
    "version",
    "gmail_account_key",
    "gmail_thread_id",
    "gmail_source_revision",
    "gmail_projection_version",
    "gmail_classifier_version",
    "source_sha256",
    "admitted_message_ids",
}
_TIMESTAMP_KEYS = {"message_id", "internal_date", "start_offset", "end_offset"}


class GmailFactParityCohortError(ValueError):
    """Raised when private cohort evidence is unsafe, stale, or ambiguous."""


@dataclass(frozen=True)
class SourceKey:
    account_key: str
    thread_id: str
    source_revision: str


@dataclass(frozen=True)
class SourceMessage:
    message_id: str
    internal_date: str
    text: str


@dataclass(frozen=True)
class CanonicalSource:
    key: SourceKey
    projection_version: int
    classifier_version: int
    source_sha256: str
    messages: tuple[SourceMessage, ...]


@dataclass(frozen=True)
class Admission:
    key: SourceKey
    projection_version: int
    classifier_version: int
    source_sha256: str
    admitted_message_ids: tuple[str, ...]


def _regular_private_file(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise GmailFactParityCohortError(f"{label} must be a regular non-symlink file")
    if stat.S_IMODE(path.stat().st_mode) != PRIVATE_FILE_MODE:
        raise GmailFactParityCohortError(f"{label} must have mode 0600")


def _positive_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GmailFactParityCohortError(f"{label} is invalid")
    return value


def _nonempty_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GmailFactParityCohortError(f"{label} is invalid")
    return value.strip()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    return b"".join(_canonical_json(row) + b"\n" for row in rows)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source_key(value: dict[str, Any], *, label: str) -> SourceKey:
    revision = _nonempty_text(value.get("gmail_source_revision"), label=label)
    if _SHA256_RE.fullmatch(revision) is None:
        raise GmailFactParityCohortError(f"{label} source revision is invalid")
    return SourceKey(
        account_key=_nonempty_text(value.get("gmail_account_key"), label=label),
        thread_id=_nonempty_text(value.get("gmail_thread_id"), label=label),
        source_revision=revision,
    )


def _load_admissions(path: Path, *, label: str) -> dict[SourceKey, Admission]:
    _regular_private_file(path, label=f"{label} admission inventory")
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GmailFactParityCohortError(
            f"{label} admission inventory is not valid JSONL"
        ) from exc
    if not rows:
        raise GmailFactParityCohortError(f"{label} admission inventory is empty")

    result: dict[SourceKey, Admission] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != _ADMISSION_KEYS:
            raise GmailFactParityCohortError(
                f"{label} admission inventory schema is invalid"
            )
        if row.get("version") != ADMISSION_VERSION:
            raise GmailFactParityCohortError(
                f"{label} admission inventory version is invalid"
            )
        key = _source_key(row, label=f"{label} admission row")
        if key in result:
            raise GmailFactParityCohortError(
                f"{label} admission inventory contains a duplicate source"
            )
        digest = row.get("source_sha256")
        raw_ids = row.get("admitted_message_ids")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise GmailFactParityCohortError(
                f"{label} admission source digest is invalid"
            )
        if (
            not isinstance(raw_ids, list)
            or any(
                not isinstance(item, str) or _MESSAGE_ID_RE.fullmatch(item) is None
                for item in raw_ids
            )
            or len(raw_ids) != len(set(raw_ids))
        ):
            raise GmailFactParityCohortError(
                f"{label} admitted message IDs are invalid"
            )
        result[key] = Admission(
            key,
            _positive_int(value=row.get("gmail_projection_version"), label=label),
            _positive_int(value=row.get("gmail_classifier_version"), label=label),
            digest,
            tuple(raw_ids),
        )
    return result


def _parse_frontmatter(text: str) -> dict[str, Any]:
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        raise GmailFactParityCohortError("canonical projection lacks frontmatter")
    end = stripped.find("\n---", 3)
    if end < 0:
        raise GmailFactParityCohortError(
            "canonical projection frontmatter is incomplete"
        )
    try:
        value = yaml.safe_load(stripped[3:end])
    except yaml.YAMLError as exc:
        raise GmailFactParityCohortError(
            "canonical projection frontmatter is invalid"
        ) from exc
    if not isinstance(value, dict):
        raise GmailFactParityCohortError("canonical projection frontmatter is invalid")
    return value


def _source_messages(
    frontmatter: dict[str, Any], body: str
) -> tuple[SourceMessage, ...]:
    raw_ids = frontmatter.get("gmail_message_ids")
    raw_timestamps = frontmatter.get("gmail_message_timestamps")
    timestamp_version = frontmatter.get("gmail_message_timestamps_version")
    retained_count = frontmatter.get("retained_message_count")
    if (
        isinstance(timestamp_version, bool)
        or timestamp_version != 1
        or isinstance(retained_count, bool)
        or not isinstance(retained_count, int)
        or not isinstance(raw_ids, list)
        or not isinstance(raw_timestamps, list)
        or len(raw_ids) != len(raw_timestamps)
        or retained_count != len(raw_ids)
        or any(
            not isinstance(item, str) or _MESSAGE_ID_RE.fullmatch(item) is None
            for item in raw_ids
        )
        or len(raw_ids) != len(set(raw_ids))
    ):
        raise GmailFactParityCohortError(
            "canonical projection message index is invalid"
        )

    messages: list[SourceMessage] = []
    previous_end = -1
    for expected_id, entry in zip(raw_ids, raw_timestamps):
        if not isinstance(entry, dict) or set(entry) != _TIMESTAMP_KEYS:
            raise GmailFactParityCohortError(
                "canonical projection timestamp index is invalid"
            )
        message_id = entry.get("message_id")
        internal_date = entry.get("internal_date")
        start = entry.get("start_offset")
        end = entry.get("end_offset")
        if (
            message_id != expected_id
            or not isinstance(internal_date, str)
            or isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 0
            or end <= start
            or end > len(body)
            or start <= previous_end
        ):
            raise GmailFactParityCohortError(
                "canonical projection timestamp index is invalid"
            )
        message_text = body[start:end]
        first_line = message_text.splitlines()[0] if message_text else ""
        if not first_line.startswith("## Message ") or not first_line.endswith(
            f" — {message_id}"
        ):
            raise GmailFactParityCohortError(
                "canonical projection message range is invalid"
            )
        messages.append(SourceMessage(message_id, internal_date, message_text))
        previous_end = end
    return tuple(messages)


def _load_canonical_sources(
    root: Path, wanted: set[SourceKey]
) -> dict[SourceKey, CanonicalSource]:
    if root.is_symlink() or not root.is_dir():
        raise GmailFactParityCohortError(
            "canonical projection root must be a non-symlink directory"
        )
    result: dict[SourceKey, CanonicalSource] = {}
    for path in sorted(root.rglob("*.md")):
        _regular_private_file(path, label="canonical projection")
        try:
            source_bytes = path.read_bytes()
            source_text = source_bytes.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise GmailFactParityCohortError(
                "canonical projection cannot be read as UTF-8"
            ) from exc
        frontmatter = _parse_frontmatter(source_text)
        if frontmatter.get("source_type") != "gmail_thread":
            continue
        key = _source_key(frontmatter, label="canonical projection")
        if key not in wanted:
            continue
        if key in result:
            raise GmailFactParityCohortError(
                "canonical projection contains a duplicate source identity"
            )
        result[key] = CanonicalSource(
            key=key,
            projection_version=_positive_int(
                frontmatter.get("gmail_projection_version"),
                label="canonical projection",
            ),
            classifier_version=_positive_int(
                frontmatter.get("gmail_classifier_version"),
                label="canonical projection",
            ),
            source_sha256=_sha256_bytes(source_bytes),
            messages=_source_messages(frontmatter, strip_frontmatter(source_text)),
        )
    if set(result) != wanted:
        raise GmailFactParityCohortError(
            "canonical projection does not exactly cover both admission inventories"
        )
    return result


def _load_hmac_key(path: Path) -> bytes:
    _regular_private_file(path, label="HMAC key")
    try:
        key = path.read_bytes()
    except OSError as exc:
        raise GmailFactParityCohortError("HMAC key cannot be read") from exc
    if len(key) < MIN_HMAC_KEY_BYTES:
        raise GmailFactParityCohortError("HMAC key must contain at least 32 bytes")
    return key


def _opaque_id(key: bytes, prefix: str, value: Any) -> str:
    payload = b"gmail_fact_parity_v1\0" + prefix.encode("ascii") + b"\0"
    payload += _canonical_json(value)
    digest = hmac.new(key, payload, hashlib.sha256).hexdigest()[:32]
    return f"gfp_{prefix}_{digest}"


def _write_private_new(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(PRIVATE_FILE_MODE)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise GmailFactParityCohortError(
            "private output artifact write failed"
        ) from exc


def _publish_frozen_artifacts(output_root: Path, artifacts: dict[str, bytes]) -> None:
    if set(artifacts) != set(OUTPUT_ARTIFACT_NAMES):
        raise GmailFactParityCohortError("output artifact set is incomplete")
    if output_root.exists() or output_root.is_symlink():
        raise GmailFactParityCohortError(
            "frozen output path already exists; choose a new output path"
        )
    parent = output_root.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise GmailFactParityCohortError("output parent is unsafe")
    parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    staging = parent / (f".{output_root.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}")
    try:
        staging.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        for name in OUTPUT_ARTIFACT_NAMES:
            _write_private_new(staging / name, artifacts[name])
        staging.replace(output_root)
    except Exception:
        if staging.is_dir() and not staging.is_symlink():
            for name in OUTPUT_ARTIFACT_NAMES:
                artifact = staging / name
                if artifact.is_file() and not artifact.is_symlink():
                    artifact.unlink()
            try:
                staging.rmdir()
            except OSError:
                pass
        raise


def _redact_message_heading(text: str, raw_id: str, opaque_id: str) -> str:
    lines = text.splitlines()
    if not lines or not lines[0].endswith(f" — {raw_id}"):
        raise GmailFactParityCohortError("canonical message heading is invalid")
    lines[0] = lines[0][: -len(raw_id)] + opaque_id
    return "\n".join(lines)


def _renderer_summary(admissions: dict[SourceKey, Admission]) -> list[dict[str, int]]:
    counts: dict[tuple[int, int], int] = {}
    for admission in admissions.values():
        key = (admission.projection_version, admission.classifier_version)
        counts[key] = counts.get(key, 0) + 1
    return [
        {
            "projection_version": projection_version,
            "classifier_version": classifier_version,
            "source_count": counts[(projection_version, classifier_version)],
        }
        for projection_version, classifier_version in sorted(counts)
    ]


def build_gmail_fact_parity_cohort(
    canonical_root: Path,
    original_inventory_path: Path,
    v2_inventory_path: Path,
    hmac_key_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Build opaque, union-admitted packets and return aggregate-only evidence."""

    original = _load_admissions(original_inventory_path, label="original")
    v2 = _load_admissions(v2_inventory_path, label="V2")
    if set(original) != set(v2):
        raise GmailFactParityCohortError(
            "admission inventories do not cover the same canonical source set"
        )
    wanted = set(original)
    sources = _load_canonical_sources(canonical_root, wanted)
    hmac_key = _load_hmac_key(hmac_key_path)

    packet_builds: list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    source_set_rows: list[dict[str, str]] = []
    original_members = 0
    v2_members = 0
    union_members = 0

    for source_key, source in sources.items():
        original_admission = original[source_key]
        v2_admission = v2[source_key]
        canonical_renderer = (source.projection_version, source.classifier_version)
        for admission in (original_admission, v2_admission):
            admission_renderer = (
                admission.projection_version,
                admission.classifier_version,
            )
            if (
                admission_renderer == canonical_renderer
                and admission.source_sha256 != source.source_sha256
            ):
                raise GmailFactParityCohortError(
                    "an admission inventory is stale for the canonical projection"
                )
        available = {message.message_id for message in source.messages}
        if (
            not set(original_admission.admitted_message_ids) <= available
            or not set(v2_admission.admitted_message_ids) <= available
        ):
            raise GmailFactParityCohortError(
                "an admission inventory references a non-canonical message"
            )

        raw_thread_key = [source_key.account_key, source_key.thread_id]
        raw_revision_key = [
            *raw_thread_key,
            source_key.source_revision,
        ]
        thread_id = _opaque_id(hmac_key, "t", raw_thread_key)
        revision_id = _opaque_id(hmac_key, "r", raw_revision_key)
        source_set_rows.append(
            {"revision_id": revision_id, "source_sha256": source.source_sha256}
        )

        original_ids = set(original_admission.admitted_message_ids)
        v2_ids = set(v2_admission.admitted_message_ids)
        union_ids = original_ids | v2_ids
        if not union_ids:
            continue
        ordered_messages = [
            message for message in source.messages if message.message_id in union_ids
        ]
        opaque_message_ids = {
            message.message_id: _opaque_id(
                hmac_key, "m", [*raw_revision_key, message.message_id]
            )
            for message in ordered_messages
        }
        packet_id = _opaque_id(
            hmac_key,
            "p",
            [
                raw_revision_key,
                source.projection_version,
                source.classifier_version,
                [message.message_id for message in ordered_messages],
            ],
        )
        packet_row = {
            "version": PACKET_VERSION,
            "packet_id": packet_id,
            "thread_id": thread_id,
            "revision_id": revision_id,
            "projection_version": source.projection_version,
            "classifier_version": source.classifier_version,
            "messages": [
                {
                    "message_id": opaque_message_ids[message.message_id],
                    "internal_date": message.internal_date,
                    "text": _redact_message_heading(
                        message.text,
                        message.message_id,
                        opaque_message_ids[message.message_id],
                    ),
                }
                for message in ordered_messages
            ],
        }
        packet_line = _canonical_json(packet_row) + b"\n"
        ordered_opaque_ids = [
            opaque_message_ids[message.message_id] for message in ordered_messages
        ]
        cohort_row = {
            "version": COHORT_VERSION,
            "packet_id": packet_id,
            "thread_id": thread_id,
            "revision_id": revision_id,
            "projection_version": source.projection_version,
            "classifier_version": source.classifier_version,
            "message_ids": ordered_opaque_ids,
            "source_sha256": source.source_sha256,
            "packet_sha256": _sha256_bytes(packet_line),
        }
        join_row = {
            "version": JOIN_VERSION,
            "packet_id": packet_id,
            "original_message_ids": [
                opaque_message_ids[message.message_id]
                for message in ordered_messages
                if message.message_id in original_ids
            ],
            "v2_message_ids": [
                opaque_message_ids[message.message_id]
                for message in ordered_messages
                if message.message_id in v2_ids
            ],
            "union_message_ids": ordered_opaque_ids,
            "original_renderer": {
                "projection_version": original_admission.projection_version,
                "classifier_version": original_admission.classifier_version,
                "source_sha256": original_admission.source_sha256,
            },
            "v2_renderer": {
                "projection_version": v2_admission.projection_version,
                "classifier_version": v2_admission.classifier_version,
                "source_sha256": v2_admission.source_sha256,
            },
        }
        original_members += len(original_ids)
        v2_members += len(v2_ids)
        union_members += len(union_ids)
        packet_builds.append((packet_id, packet_row, cohort_row, join_row))

    if not packet_builds:
        raise GmailFactParityCohortError(
            "the union of both admission inventories contains no messages"
        )

    packet_builds.sort(key=lambda item: item[0])
    packet_rows = [item[1] for item in packet_builds]
    cohort_rows = [item[2] for item in packet_builds]
    join_rows = [item[3] for item in packet_builds]
    packets_bytes = _jsonl_bytes(packet_rows)
    cohort_bytes = _jsonl_bytes(cohort_rows)
    joins_bytes = _jsonl_bytes(join_rows)
    source_set_bytes = _jsonl_bytes(
        sorted(source_set_rows, key=lambda item: item["revision_id"])
    )

    thread_count = len({row["thread_id"] for row in cohort_rows})
    projection_versions = sorted({row["projection_version"] for row in cohort_rows})
    classifier_versions = sorted({row["classifier_version"] for row in cohort_rows})
    manifest = {
        "version": MANIFEST_VERSION,
        "builder_version": VERSION,
        "cohort_sha256": _sha256_bytes(cohort_bytes),
        "packet_sha256": _sha256_bytes(packets_bytes),
        "admission_join_sha256": _sha256_bytes(joins_bytes),
        "canonical_source_set_sha256": _sha256_bytes(source_set_bytes),
        "original_inventory_sha256": _sha256_bytes(
            original_inventory_path.read_bytes()
        ),
        "v2_inventory_sha256": _sha256_bytes(v2_inventory_path.read_bytes()),
        "id_namespace": _opaque_id(hmac_key, "k", "cohort-namespace"),
        "source_revision_count": len(wanted),
        "packet_count": len(packet_rows),
        "thread_count": thread_count,
        "message_count": union_members,
        "original_admitted_message_count": original_members,
        "v2_admitted_message_count": v2_members,
        "union_admitted_message_count": union_members,
        "projection_versions": projection_versions,
        "classifier_versions": classifier_versions,
        "original_renderer_provenance": _renderer_summary(original),
        "v2_renderer_provenance": _renderer_summary(v2),
        "packet_policy": "union_admitted_messages_only",
        "portable_identity": "account_thread+source_revision+message_id",
        "renderer_versions_are_provenance_not_identity": True,
        "provider_ids_in_packet_metadata": False,
        "private_file_mode": "0600",
        "private_directory_mode": "0700",
    }
    manifest_bytes = _canonical_json(manifest) + b"\n"

    _publish_frozen_artifacts(
        output_root,
        {
            "packets.jsonl": packets_bytes,
            "cohort.jsonl": cohort_bytes,
            "admissions.jsonl": joins_bytes,
            "manifest.json": manifest_bytes,
        },
    )

    return {
        "version": VERSION,
        "cohort_sha256": manifest["cohort_sha256"],
        "packet_sha256": manifest["packet_sha256"],
        "canonical_source_set_sha256": manifest["canonical_source_set_sha256"],
        "source_revisions": len(wanted),
        "packets": len(packet_rows),
        "threads": thread_count,
        "messages": union_members,
        "original_admitted_messages": original_members,
        "v2_admitted_messages": v2_members,
        "private_content_printed": False,
        "external_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("canonical_root", type=Path)
    parser.add_argument("original_admissions", type=Path)
    parser.add_argument("v2_admissions", type=Path)
    parser.add_argument("hmac_key", type=Path)
    parser.add_argument("output_root", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            build_gmail_fact_parity_cohort(
                args.canonical_root,
                args.original_admissions,
                args.v2_admissions,
                args.hmac_key,
                args.output_root,
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
