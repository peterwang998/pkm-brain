#!/usr/bin/env python3
"""Freeze identical, private Gmail admissions for the fact-parity challenge.

The installed original Brain predates Gmail knowledge projection, so it has no
native Gmail admission inventory to replay.  This exporter instead selects a
separate fact-rich capability cohort from the canonical projection, excludes
every thread in the authenticated temporal holdout, and writes the same message
admissions for the original and V2 extractors.  It reads projection frontmatter
only; it performs no model, network, database, or persistence calls.

The resulting cohort is deliberately not a mailbox-population estimate.  Its
semantic release denominator (at least 50 supported original non-temporal units
across at least 30 threads) remains unverified until the blinded parity judge
has completed the downstream work queue.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


VERSION = "gmail_fact_parity_admission_export_v1"
MANIFEST_VERSION = "gmail_fact_parity_admission_export_manifest_v1"
ADMISSION_VERSION = "gmail_fact_parity_admission_v1"
TEMPORAL_MANIFEST_VERSION = "gmail_temporal_holdout_manifest_v4"
TEMPORAL_BINDING_VERSION = "gmail_temporal_holdout_binding_v1"
TEMPORAL_MANIFEST_DOMAIN = b"gmail_temporal_holdout_manifest_v4\0"
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
MIN_HMAC_KEY_BYTES = 32
DEFAULT_THREAD_COUNT = 150
MIN_PARITY_PACKETS = 100
MIN_REFERENCE_UNITS_PER_STAGE = 50
MIN_REFERENCE_THREADS_PER_STAGE = 30
OUTPUT_NAMES = (
    "original-admissions.jsonl",
    "v2-admissions.jsonl",
    "manifest.json",
)
TEMPORAL_BINDING_NAMES = (
    "evaluation-authority/primary-bindings.jsonl",
    "evaluation-authority/challenge-bindings.jsonl",
    "sealed-reserve/bindings.jsonl",
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EVALUATOR_PATH = _REPO_ROOT / "scripts" / "evaluate_gmail_fact_parity.py"
_SPEC = importlib.util.spec_from_file_location(
    "fact_parity_admission_export_evaluator", _EVALUATOR_PATH
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - import guard
    raise RuntimeError("fact parity evaluator could not be loaded")
evaluator = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(evaluator)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_BINDING_REQUIRED_KEYS = {
    "version",
    "gmail_account_key",
    "gmail_thread_id",
    "gmail_source_revision",
    "gmail_message_id",
}


class GmailFactParityAdmissionExportError(ValueError):
    """Raised when the private parity admission freeze is unsafe or stale."""


@dataclass(frozen=True)
class ProjectionAdmission:
    account_key: str
    thread_id: str
    source_revision: str
    projection_version: int
    classifier_version: int
    source_sha256: str
    admitted_message_ids: tuple[str, ...]


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


def _private_file(path: Path, *, label: str) -> bytes:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise GmailFactParityAdmissionExportError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != PRIVATE_FILE_MODE
    ):
        raise GmailFactParityAdmissionExportError(
            f"{label} must be an owner-only regular file"
        )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise GmailFactParityAdmissionExportError(f"{label} is unavailable") from exc


def _private_directory(path: Path, *, label: str) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise GmailFactParityAdmissionExportError(f"{label} is unavailable") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != PRIVATE_DIRECTORY_MODE
    ):
        raise GmailFactParityAdmissionExportError(
            f"{label} must be an owner-only directory"
        )


def _parse_json(raw: bytes, *, label: str) -> Any:
    try:
        return json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GmailFactParityAdmissionExportError(f"{label} is invalid JSON") from exc


def _positive_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GmailFactParityAdmissionExportError(f"{label} is invalid")
    return value


def _required_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GmailFactParityAdmissionExportError(f"{label} is invalid")
    return value.strip()


def _load_hmac_key(path: Path) -> bytes:
    value = _private_file(path, label="HMAC key")
    if len(value) < MIN_HMAC_KEY_BYTES:
        raise GmailFactParityAdmissionExportError(
            "HMAC key must contain at least 32 bytes"
        )
    return value


def _load_temporal_manifest(root: Path, *, key: bytes) -> tuple[dict[str, Any], bytes]:
    _private_directory(root, label="temporal holdout root")
    raw = _private_file(root / "manifest.json", label="temporal holdout manifest")
    value = _parse_json(raw, label="temporal holdout manifest")
    if not isinstance(value, dict) or raw != _canonical_json(value) + b"\n":
        raise GmailFactParityAdmissionExportError(
            "temporal holdout manifest is not canonical"
        )
    authenticator = value.get("manifest_hmac_sha256")
    unsigned = dict(value)
    unsigned.pop("manifest_hmac_sha256", None)
    expected = hmac.new(
        key,
        TEMPORAL_MANIFEST_DOMAIN + _canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()
    if (
        value.get("version") != TEMPORAL_MANIFEST_VERSION
        or not isinstance(authenticator, str)
        or _SHA256_RE.fullmatch(authenticator) is None
        or not hmac.compare_digest(authenticator, expected)
    ):
        raise GmailFactParityAdmissionExportError(
            "temporal holdout manifest authentication failed"
        )
    if (
        value.get("label_status") != "unlabeled"
        or value.get("routable") is not False
        or value.get("external_calls") != 0
        or value.get("persistence_calls") != 0
        or value.get("private_content_printed") is not False
    ):
        raise GmailFactParityAdmissionExportError(
            "temporal holdout policy is incompatible"
        )
    return value, raw


def _load_excluded_threads(
    root: Path,
    *,
    manifest: Mapping[str, Any],
) -> set[tuple[str, str]]:
    artifact_digests = manifest.get("artifact_sha256")
    if not isinstance(artifact_digests, Mapping):
        raise GmailFactParityAdmissionExportError(
            "temporal holdout artifact inventory is invalid"
        )
    result: set[tuple[str, str]] = set()
    seen_messages: set[tuple[str, str, str, str]] = set()
    for relative_name in TEMPORAL_BINDING_NAMES:
        raw = _private_file(root / relative_name, label="temporal holdout binding")
        if artifact_digests.get(relative_name) != _sha256_bytes(raw):
            raise GmailFactParityAdmissionExportError(
                "temporal holdout binding digest is stale"
            )
        try:
            rows = [
                json.loads(line)
                for line in raw.decode("utf-8").splitlines()
                if line.strip()
            ]
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise GmailFactParityAdmissionExportError(
                "temporal holdout binding is invalid JSONL"
            ) from exc
        for row in rows:
            if (
                not isinstance(row, dict)
                or not _BINDING_REQUIRED_KEYS <= set(row)
                or row.get("version") != TEMPORAL_BINDING_VERSION
            ):
                raise GmailFactParityAdmissionExportError(
                    "temporal holdout binding schema is invalid"
                )
            account = _required_text(
                row.get("gmail_account_key"), label="binding account"
            )
            thread = _required_text(
                row.get("gmail_thread_id"), label="binding thread"
            )
            revision = _required_text(
                row.get("gmail_source_revision"), label="binding revision"
            )
            message = _required_text(
                row.get("gmail_message_id"), label="binding message"
            )
            message_key = (account, thread, revision, message)
            if message_key in seen_messages:
                raise GmailFactParityAdmissionExportError(
                    "temporal holdout bindings overlap"
                )
            seen_messages.add(message_key)
            result.add((account, thread))
    return result


def _frontmatter(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise GmailFactParityAdmissionExportError(
            "canonical projection is not UTF-8"
        ) from exc
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        raise GmailFactParityAdmissionExportError(
            "canonical projection lacks frontmatter"
        )
    end = stripped.find("\n---", 3)
    if end < 0:
        raise GmailFactParityAdmissionExportError(
            "canonical projection frontmatter is incomplete"
        )
    try:
        value = yaml.safe_load(stripped[3:end])
    except yaml.YAMLError as exc:
        raise GmailFactParityAdmissionExportError(
            "canonical projection frontmatter is invalid"
        ) from exc
    if not isinstance(value, dict):
        raise GmailFactParityAdmissionExportError(
            "canonical projection frontmatter is invalid"
        )
    return value


def _projection_admission(path: Path) -> ProjectionAdmission | None:
    raw = _private_file(path, label="canonical projection")
    value = _frontmatter(raw)
    if value.get("source_type") != "gmail_thread" or value.get("deleted") is True:
        return None
    account = _required_text(value.get("gmail_account_key"), label="projection account")
    thread = _required_text(value.get("gmail_thread_id"), label="projection thread")
    revision = _required_text(
        value.get("gmail_source_revision"), label="projection revision"
    )
    if _SHA256_RE.fullmatch(revision) is None:
        raise GmailFactParityAdmissionExportError(
            "canonical projection revision is invalid"
        )
    raw_message_ids = value.get("gmail_message_ids")
    raw_admitted_ids = value.get("gmail_fact_admitted_message_ids")
    if (
        not isinstance(raw_message_ids, list)
        or not isinstance(raw_admitted_ids, list)
        or any(
            not isinstance(item, str) or _MESSAGE_ID_RE.fullmatch(item) is None
            for item in [*raw_message_ids, *raw_admitted_ids]
        )
        or len(raw_message_ids) != len(set(raw_message_ids))
        or len(raw_admitted_ids) != len(set(raw_admitted_ids))
        or not set(raw_admitted_ids) <= set(raw_message_ids)
    ):
        raise GmailFactParityAdmissionExportError(
            "canonical projection message admission is invalid"
        )
    if not raw_admitted_ids:
        return None
    return ProjectionAdmission(
        account_key=account,
        thread_id=thread,
        source_revision=revision,
        projection_version=_positive_int(
            value.get("gmail_projection_version"), label="projection version"
        ),
        classifier_version=_positive_int(
            value.get("gmail_classifier_version"), label="classifier version"
        ),
        source_sha256=_sha256_bytes(raw),
        admitted_message_ids=tuple(raw_admitted_ids),
    )


def _rank(key: bytes, item: ProjectionAdmission) -> str:
    material = _canonical_json(
        [item.account_key, item.thread_id, item.source_revision]
    )
    return hmac.new(
        key,
        b"gmail_fact_parity_admission_export_v1\0" + material,
        hashlib.sha256,
    ).hexdigest()


def _load_eligible_projections(
    canonical_root: Path,
    *,
    excluded_threads: set[tuple[str, str]],
) -> list[ProjectionAdmission]:
    _private_directory(canonical_root, label="canonical projection root")
    eligible: list[ProjectionAdmission] = []
    seen_threads: set[tuple[str, str]] = set()
    for path in sorted(canonical_root.glob("*.md")):
        item = _projection_admission(path)
        if item is None:
            continue
        thread_key = (item.account_key, item.thread_id)
        if thread_key in seen_threads:
            raise GmailFactParityAdmissionExportError(
                "canonical projection contains duplicate current threads"
            )
        seen_threads.add(thread_key)
        if thread_key not in excluded_threads:
            eligible.append(item)
    return eligible


def _admission_row(item: ProjectionAdmission) -> dict[str, Any]:
    return {
        "version": ADMISSION_VERSION,
        "gmail_account_key": item.account_key,
        "gmail_thread_id": item.thread_id,
        "gmail_source_revision": item.source_revision,
        "gmail_projection_version": item.projection_version,
        "gmail_classifier_version": item.classifier_version,
        "source_sha256": item.source_sha256,
        "admitted_message_ids": list(item.admitted_message_ids),
    }


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
        raise GmailFactParityAdmissionExportError(
            "private output artifact write failed"
        ) from exc


def _publish(output_root: Path, artifacts: Mapping[str, bytes]) -> None:
    if set(artifacts) != set(OUTPUT_NAMES):
        raise GmailFactParityAdmissionExportError("output artifact set is incomplete")
    if output_root.exists() or output_root.is_symlink():
        raise GmailFactParityAdmissionExportError(
            "frozen output path already exists; choose a new output path"
        )
    parent = output_root.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise GmailFactParityAdmissionExportError("output parent is unsafe")
    parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    staging = parent / f".{output_root.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    try:
        staging.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        for name in OUTPUT_NAMES:
            _write_private_new(staging / name, artifacts[name])
        staging.replace(output_root)
    except Exception:
        if staging.is_dir() and not staging.is_symlink():
            for name in OUTPUT_NAMES:
                path = staging / name
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            try:
                staging.rmdir()
            except OSError:
                pass
        raise


def export_gmail_fact_parity_admissions(
    canonical_root: Path,
    temporal_holdout_root: Path,
    hmac_key_path: Path,
    output_root: Path,
    *,
    thread_count: int = DEFAULT_THREAD_COUNT,
) -> dict[str, Any]:
    """Freeze identical fact-rich admissions and return aggregate-only evidence."""

    if isinstance(thread_count, bool) or thread_count < MIN_PARITY_PACKETS:
        raise GmailFactParityAdmissionExportError(
            f"thread count must be at least {MIN_PARITY_PACKETS}"
        )
    key = _load_hmac_key(hmac_key_path)
    temporal_manifest, temporal_manifest_raw = _load_temporal_manifest(
        temporal_holdout_root, key=key
    )
    excluded_threads = _load_excluded_threads(
        temporal_holdout_root, manifest=temporal_manifest
    )
    eligible = _load_eligible_projections(
        canonical_root, excluded_threads=excluded_threads
    )
    if len(eligible) < thread_count:
        raise GmailFactParityAdmissionExportError(
            "not enough non-holdout fact-rich threads for the parity cohort"
        )
    selected = sorted(eligible, key=lambda item: (_rank(key, item), item.source_sha256))[
        :thread_count
    ]
    rows = [_admission_row(item) for item in selected]
    rows.sort(
        key=lambda row: (
            row["gmail_account_key"],
            row["gmail_thread_id"],
            row["gmail_source_revision"],
        )
    )
    inventory_bytes = _jsonl_bytes(rows)
    selected_message_count = sum(len(item.admitted_message_ids) for item in selected)
    manifest = {
        "version": MANIFEST_VERSION,
        "exporter_version": VERSION,
        "exporter_sha256": _sha256_bytes(Path(__file__).read_bytes()),
        "evaluator_sha256": _sha256_bytes(_EVALUATOR_PATH.read_bytes()),
        "temporal_holdout_manifest_sha256": _sha256_bytes(temporal_manifest_raw),
        "temporal_holdout_builder_sha256": temporal_manifest.get("builder_sha256"),
        "temporal_holdout_threads_excluded": len(excluded_threads),
        "selection_policy": "hmac_ranked_fact_admitted_threads_excluding_temporal_holdout_v1",
        "selection_scope": "fact_rich_capability_challenge_not_population_estimate",
        "selection_message_basis": "gmail_fact_admitted_message_ids",
        "selected_thread_count": len(selected),
        "selected_message_count": selected_message_count,
        "original_inventory_sha256": _sha256_bytes(inventory_bytes),
        "v2_inventory_sha256": _sha256_bytes(inventory_bytes),
        "inventories_identical": True,
        "packet_input_policy": "identical_preselected_messages_to_both_extractors",
        "original_native_gmail_admission_available": False,
        "original_baseline": {
            "commit": evaluator.EXPECTED_ORIGINAL_COMMIT,
            "prompt_version": evaluator.EXPECTED_ORIGINAL_PROMPT_VERSION,
            "model": evaluator.EXPECTED_ORIGINAL_MODEL,
            "reasoning_effort": evaluator.EXPECTED_ORIGINAL_REASONING_EFFORT,
        },
        "v2_target": {
            "prompt_version": evaluator.EXPECTED_V2_PROMPT_VERSION,
            "model": evaluator.EXPECTED_V2_MODEL,
            "reasoning_effort": evaluator.EXPECTED_V2_REASONING_EFFORT,
        },
        "structural_minimum_packets": MIN_PARITY_PACKETS,
        "structural_minimum_passed": len(selected) >= MIN_PARITY_PACKETS,
        "semantic_minimum_reference_units_per_stage": MIN_REFERENCE_UNITS_PER_STAGE,
        "semantic_minimum_reference_threads_per_stage": MIN_REFERENCE_THREADS_PER_STAGE,
        "semantic_denominator_verified": False,
        "release_evidence_ready": False,
        "may_be_pooled_with_temporal_cohorts": False,
        "private_file_mode": "0600",
        "private_directory_mode": "0700",
        "private_content_printed": False,
        "external_calls": 0,
        "database_calls": 0,
        "persistence_calls": 0,
    }
    _publish(
        output_root,
        {
            "original-admissions.jsonl": inventory_bytes,
            "v2-admissions.jsonl": inventory_bytes,
            "manifest.json": _canonical_json(manifest) + b"\n",
        },
    )
    return {
        "version": VERSION,
        "threads": len(selected),
        "messages": selected_message_count,
        "eligible_threads": len(eligible),
        "temporal_holdout_threads_excluded": len(excluded_threads),
        "inventories_identical": True,
        "semantic_denominator_verified": False,
        "release_evidence_ready": False,
        "original_inventory_sha256": manifest["original_inventory_sha256"],
        "v2_inventory_sha256": manifest["v2_inventory_sha256"],
        "private_content_printed": False,
        "external_calls": 0,
        "database_calls": 0,
        "persistence_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("canonical_root", type=Path)
    parser.add_argument("temporal_holdout_root", type=Path)
    parser.add_argument("hmac_key", type=Path)
    parser.add_argument("output_root", type=Path)
    parser.add_argument("--thread-count", type=int, default=DEFAULT_THREAD_COUNT)
    args = parser.parse_args()
    try:
        result = export_gmail_fact_parity_admissions(
            args.canonical_root,
            args.temporal_holdout_root,
            args.hmac_key,
            args.output_root,
            thread_count=args.thread_count,
        )
    except GmailFactParityAdmissionExportError:
        print(
            json.dumps(
                {
                    "version": VERSION,
                    "status": "failed",
                    "private_content_printed": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        raise SystemExit(2) from None
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
