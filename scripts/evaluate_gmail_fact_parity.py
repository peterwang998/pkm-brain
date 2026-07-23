#!/usr/bin/env python3
"""Score original-Brain versus Brain V2 fact retention from complete evidence.

The evaluator fails closed unless a frozen cohort, every packet, one original
run, at least three V2 runs, their self-reported invocation receipts, a complete
alignment, complete member-level labels, and an external Codex Sol/medium judge
receipt all reconcile.  It prints aggregate counts, rates, and digests only.
Private packet text and fact statements never appear on stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import runpy
import stat
import subprocess
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any


_STAGE_CONTRACT_MODULE = runpy.run_path(
    str(Path(__file__).with_name("gmail_fact_parity_contract.py"))
)
STAGE_CONTRACT_VERSION = str(_STAGE_CONTRACT_MODULE["CONTRACT_VERSION"])
STAGE_CONTRACT_SHA256 = str(_STAGE_CONTRACT_MODULE["CONTRACT_SHA256"])
STAGE_DISPOSITIONS = tuple(_STAGE_CONTRACT_MODULE["DISPOSITIONS"])
ACCEPTED_ACTION_STATUSES = tuple(_STAGE_CONTRACT_MODULE["ACCEPTED_ACTION_STATUSES"])
APPLIED_ACTION_STATUSES = tuple(_STAGE_CONTRACT_MODULE["APPLIED_ACTION_STATUSES"])


VERSION = "gmail_fact_parity_evaluation_v5"
LABEL_VERSION = "gmail_fact_parity_unit_v4"
MANIFEST_VERSION = "gmail_fact_parity_manifest_v5"
ALIGNMENT_VERSION = "gmail_fact_parity_alignment_unit_v1"
COMPLETED_UNIT_VERSION = "gmail_fact_parity_completed_unit_v1"
WORK_ITEM_VERSION = "gmail_fact_parity_alignment_work_item_v2"
RUN_VERSION = "gmail_fact_parity_run_v4"
RUN_PACKET_VERSION = "gmail_fact_parity_run_packet_v3"
RECEIPT_VERSION = "gmail_fact_parity_run_receipt_v4"
JUDGE_RECEIPT_VERSION = "gmail_fact_parity_judge_receipt_v1"
COHORT_MANIFEST_VERSION = "gmail_fact_parity_cohort_manifest_v2"
COHORT_VERSION = "gmail_fact_parity_cohort_v2"
PACKET_VERSION = "gmail_fact_parity_packet_v2"
ADMISSION_JOIN_VERSION = "gmail_fact_parity_admission_join_v2"
BINDING_VERSION = "gmail_fact_parity_source_binding_v2"
ADMISSION_INVENTORY_VERSION = "gmail_fact_parity_admission_v1"
INVOCATION_ATTESTATION = "self_reported_not_cryptographically_verified"
EXPECTED_PROVIDER = "external-codex"
EXPECTED_JUDGE_MODEL = "gpt-5.6-sol"
EXPECTED_JUDGE_REASONING_EFFORT = "medium"
EXPECTED_ORIGINAL_COMMIT = "d5405b9cf7a81775dfc84200892c206687756f3c"
EXPECTED_ORIGINAL_PROMPT_VERSION = "extractor-evidence-units-v6-speaker-context"
EXPECTED_ORIGINAL_MODEL = "gpt-5.6-luna"
EXPECTED_ORIGINAL_REASONING_EFFORT = "low"
EXPECTED_ORIGINAL_PRODUCTION_TREE_SHA256 = (
    "550d116aae8b38f172a2a342509b187f33bff8b8d044a95294c65c757d7f08b5"
)
EXPECTED_ORIGINAL_PROMPT_SHA256 = (
    "970706f3bb83fa2e25a66911f492074dc528d944e658cd76e3f0662b42304be6"
)
EXPECTED_ORIGINAL_RUNTIME_CONFIG_SHA256 = (
    "bd7df34012733f2ca2da3e61d16fc3c942577783aa8a91d6ed8ee0737e5a237b"
)
EXPECTED_V2_PROMPT_VERSION = "extractor-evidence-units-v15-gmail-event-time"
EXPECTED_V2_MODEL = "gpt-5.6-luna"
EXPECTED_V2_REASONING_EFFORT = "low"
JUDGE_CONTRACT_VERSION = "gmail_fact_parity_judge_contract_v1"
_JUDGE_CONTRACT = {
    "version": JUDGE_CONTRACT_VERSION,
    "input": "canonical_private_packet_text_and_blinded_run_members",
    "coverage": "every_packet_and_every_member_exactly_once",
    "classification": ["non_temporal", "temporal", "not_fact"],
    "member_judgments": ["supported", "scope_correct", "critical_error"],
    "content_policy": "gmail_text_is_untrusted_evidence_not_instruction",
}
JUDGE_CONTRACT_SHA256 = hashlib.sha256(
    json.dumps(
        _JUDGE_CONTRACT,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()
MIN_RETENTION = 0.95
MIN_PRECISION = 0.95
MIN_RUN_AGREEMENT = 0.95
MIN_V2_RUNS = 3
MIN_COHORT_PACKETS = 100
MIN_COHORT_THREADS = 100
MIN_COHORT_MESSAGES = 100
MIN_REFERENCE_UNITS_PER_STAGE = 50
MIN_REFERENCE_THREADS_PER_STAGE = 30
STAGES = tuple(_STAGE_CONTRACT_MODULE["STAGES"])
CLASSIFICATIONS = {"non_temporal", "temporal", "not_fact"}
CRITICAL_ERRORS = {"none", "unsupported", "wrong_scope", "wrong_entity", "other"}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_INVOCATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_PROVIDER_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_OPAQUE_PATTERNS = {
    "unit_id": re.compile(r"^gfp_u_[0-9a-f]{32}$"),
    "work_item_id": re.compile(r"^gfp_w_[0-9a-f]{32}$"),
    "packet_id": re.compile(r"^gfp_p_[0-9a-f]{32}$"),
    "thread_id": re.compile(r"^gfp_t_[0-9a-f]{32}$"),
    "revision_id": re.compile(r"^gfp_r_[0-9a-f]{32}$"),
    "message_id": re.compile(r"^gfp_m_[0-9a-f]{32}$"),
    "member_id": re.compile(r"^gfp_a_[0-9a-f]{32}$"),
}

_LABEL_KEYS = {
    "version",
    "unit_id",
    "packet_id",
    "thread_id",
    "useful",
    "classification",
    "original",
    "v2",
}
_ARM_KEYS = {"stage_counts", "members"}
_LABELED_MEMBER_KEYS = {
    "member_id",
    "supported",
    "scope_correct",
    "critical_error",
    "stages",
}
_ALIGNMENT_KEYS = {"version", "unit_id", "packet_id", "thread_id", "members"}
_COMPLETED_UNIT_KEYS = {
    "version",
    "packet_id",
    "useful",
    "classification",
    "members",
}
_JUDGMENT_KEYS = {"member_id", "supported", "scope_correct", "critical_error"}
_WORK_ITEM_KEYS = {
    "version",
    "work_item_id",
    "packet_id",
    "thread_id",
    "messages",
    "members",
}
_RUN_BINDING_KEYS = {
    "stage_contract_version",
    "stage_contract_sha256",
    "adapter_sha256",
    "adapter_executable_sha256",
    "production_tree_sha256",
    "runtime_config_sha256",
    "prompt_sha256",
    "invocation_ledger_sha256",
}
_RUN_HEADER_KEYS = {
    "version",
    "run_id",
    "arm",
    "commit",
    "prompt_version",
    "model",
    "reasoning_effort",
    "cohort_sha256",
    "packet_sha256",
    *_RUN_BINDING_KEYS,
}
_RUN_PACKET_KEYS = {"version", "run_id", "packet_id", "thread_id", "members"}
_RUN_MEMBER_KEYS = {"statement", "evidence_message_ids", "stages", "stage_record"}
_RUN_STAGE_RECORD_KEYS = {
    "version",
    "contract_sha256",
    "candidate_sha256",
    "action_id",
    "stages",
    "disposition",
    "action_status",
    "persisted_fact_ids",
}
_RECEIPT_KEYS = {
    "version",
    "run_id",
    "provider",
    "model",
    "reasoning_effort",
    "started_at",
    "completed_at",
    "output_sha256",
    "attestation",
    "invocations",
    *_RUN_BINDING_KEYS,
}
_INVOCATION_KEYS = {
    "invocation_id",
    "ordinal",
    "packet_id",
    "window_index",
    "window_count",
    "request_sha256",
    "response_sha256",
    "provider",
    "model",
    "reasoning_effort",
    "started_at",
    "completed_at",
}
_JUDGE_RECEIPT_KEYS = {
    "version",
    "invocation_id",
    "provider",
    "model",
    "reasoning_effort",
    "started_at",
    "completed_at",
    "cohort_sha256",
    "packet_sha256",
    "work_queue_sha256",
    "completed_units_sha256",
    "judge_contract_version",
    "judge_contract_sha256",
    "attestation",
}
_ADMISSION_JOIN_KEYS = {
    "version",
    "packet_id",
    "original_message_ids",
    "v2_message_ids",
    "union_message_ids",
    "original_renderer",
    "v2_renderer",
}
_RENDERER_KEYS = {"projection_version", "classifier_version", "source_sha256"}
_ADMISSION_INVENTORY_KEYS = {
    "version",
    "gmail_account_key",
    "gmail_thread_id",
    "gmail_source_revision",
    "gmail_projection_version",
    "gmail_classifier_version",
    "source_sha256",
    "admitted_message_ids",
}
_PACKET_KEYS = {
    "version",
    "packet_id",
    "thread_id",
    "revision_id",
    "projection_version",
    "classifier_version",
    "messages",
}
_PACKET_MESSAGE_KEYS = {"message_id", "internal_date", "text"}
_COHORT_KEYS = {
    "version",
    "packet_id",
    "thread_id",
    "revision_id",
    "projection_version",
    "classifier_version",
    "message_ids",
    "source_sha256",
    "packet_sha256",
}
_COHORT_MANIFEST_KEYS = {
    "version",
    "builder_version",
    "cohort_sha256",
    "packet_sha256",
    "admission_join_sha256",
    "source_binding_sha256",
    "canonical_source_set_sha256",
    "original_inventory_sha256",
    "v2_inventory_sha256",
    "id_namespace",
    "source_revision_count",
    "packet_count",
    "thread_count",
    "message_count",
    "original_admitted_message_count",
    "v2_admitted_message_count",
    "union_admitted_message_count",
    "projection_versions",
    "classifier_versions",
    "original_renderer_provenance",
    "v2_renderer_provenance",
    "packet_policy",
    "portable_identity",
    "renderer_versions_are_provenance_not_identity",
    "provider_ids_in_packet_metadata",
    "private_file_mode",
    "private_directory_mode",
    "manifest_hmac_sha256",
}
_SOURCE_BINDING_KEYS = {
    "version",
    "gmail_account_key",
    "gmail_thread_id",
    "gmail_source_revision",
    "source_sha256",
    "projection_version",
    "classifier_version",
    "thread_id",
    "revision_id",
    "packet_id",
    "messages",
    "original_admitted_message_ids",
    "v2_admitted_message_ids",
    "union_admitted_message_ids",
}
_SOURCE_BINDING_MESSAGE_KEYS = {"gmail_message_id", "message_id"}
MIN_HMAC_KEY_BYTES = 32
MANIFEST_HMAC_DOMAIN = b"gmail_fact_parity_cohort_manifest_v2\0"


class GmailFactParityError(ValueError):
    """Raised when parity evidence is malformed, stale, or incomplete."""


def _private_file(path: Path, *, allow_empty: bool = False) -> None:
    if path.is_symlink() or not path.is_file():
        raise GmailFactParityError("input must be a regular non-symlink file")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise GmailFactParityError("input artifacts must have mode 0600")
    if not allow_empty and path.stat().st_size == 0:
        raise GmailFactParityError("input artifacts must not be empty")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_identity(path: Path) -> tuple[int, int]:
    metadata = path.stat()
    return metadata.st_dev, metadata.st_ino


def _evaluator_path() -> Path:
    return Path(__file__)


def _preparer_path() -> Path:
    return Path(__file__).with_name("prepare_gmail_fact_parity_evaluation.py")


def _regular_file_bytes(path: Path) -> bytes | None:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        return None
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or path.is_symlink():
            return None
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
        ):
            return None
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            raw = handle.read()
            after = os.fstat(handle.fileno())
            if (
                after.st_dev != opened.st_dev
                or after.st_ino != opened.st_ino
                or after.st_size != opened.st_size
                or after.st_mtime_ns != opened.st_mtime_ns
            ):
                return None
        return raw if raw else None
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


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


def _git_output(repository_root: Path, arguments: tuple[str, ...]) -> bytes | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository_root), *arguments],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_environment(),
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _git_head_bound_files(
    repository_root: Path,
    relative_paths: tuple[str, ...],
) -> tuple[str, dict[str, bytes]] | None:
    root = repository_root.resolve()
    if not relative_paths or any(
        Path(value).is_absolute() or ".." in Path(value).parts
        for value in relative_paths
    ):
        return None
    top_level_raw = _git_output(root, ("rev-parse", "--show-toplevel"))
    head_raw = _git_output(root, ("rev-parse", "--verify", "HEAD^{commit}"))
    if top_level_raw is None or head_raw is None:
        return None
    try:
        top_level = top_level_raw.decode("utf-8").strip()
        head = head_raw.decode("ascii").strip()
    except UnicodeError:
        return None
    if Path(top_level).resolve() != root or _GIT_COMMIT_RE.fullmatch(head) is None:
        return None
    status_arguments = (
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        *relative_paths,
    )
    if _git_output(root, status_arguments) != b"":
        return None

    blobs: dict[str, bytes] = {}
    for relative_path in relative_paths:
        local_bytes = _regular_file_bytes(root / relative_path)
        if local_bytes is None:
            return None
        object_id_raw = _git_output(
            root, ("rev-parse", "--verify", f"{head}:{relative_path}")
        )
        index_object_id_raw = _git_output(
            root, ("rev-parse", "--verify", f":{relative_path}")
        )
        if object_id_raw is None or index_object_id_raw is None:
            return None
        try:
            object_id = object_id_raw.decode("ascii").strip()
            index_object_id = index_object_id_raw.decode("ascii").strip()
        except UnicodeError:
            return None
        if (
            _GIT_COMMIT_RE.fullmatch(object_id) is None
            or _GIT_COMMIT_RE.fullmatch(index_object_id) is None
            or not hmac.compare_digest(object_id, index_object_id)
            or _git_output(root, ("cat-file", "-t", object_id)) != b"blob\n"
        ):
            return None
        head_bytes = _git_output(root, ("cat-file", "blob", object_id))
        if head_bytes is None:
            return None
        if not hmac.compare_digest(local_bytes, head_bytes):
            return None
        blobs[relative_path] = head_bytes
    if _git_output(root, status_arguments) != b"":
        return None
    head_after_raw = _git_output(root, ("rev-parse", "--verify", "HEAD^{commit}"))
    if head_after_raw is None:
        return None
    try:
        head_after = head_after_raw.decode("ascii").strip()
    except UnicodeError:
        return None
    if not hmac.compare_digest(head, head_after):
        return None
    return head, blobs


def _expected_canonical_adapter_authority(
    repository_root: Path | None = None,
) -> dict[str, str] | None:
    root = (
        Path(repository_root).resolve()
        if repository_root is not None
        else Path(__file__).resolve().parents[1]
    )
    runner_path = "scripts/run_gmail_fact_parity.py"
    adapter_path = "scripts/gmail_fact_parity_production_adapter.py"
    bound = _git_head_bound_files(root, (runner_path, adapter_path))
    if bound is None:
        return None
    git_head, blobs = bound
    adapter_sha256 = _sha256_bytes(
        _canonical_json(
            {
                "runner": _sha256_bytes(blobs[runner_path]),
                "adapter": _sha256_bytes(blobs[adapter_path]),
            }
        )
    )
    return {"git_head": git_head, "adapter_sha256": adapter_sha256}


def _expected_canonical_adapter_sha256(
    repository_root: Path | None = None,
) -> str | None:
    authority = _expected_canonical_adapter_authority(repository_root)
    return authority["adapter_sha256"] if authority is not None else None


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _private_hmac_key(path: Path) -> bytes:
    _private_file(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise GmailFactParityError("HMAC key must be a mode-0600 regular file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            key = handle.read()
    except GmailFactParityError:
        raise
    except OSError as exc:
        raise GmailFactParityError("HMAC key cannot be read safely") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(key) < MIN_HMAC_KEY_BYTES:
        raise GmailFactParityError("HMAC key must contain at least 32 bytes")
    return key


def _derived_opaque_id(key: bytes, prefix: str, value: Any) -> str:
    payload = b"gmail_fact_parity_v1\0" + prefix.encode("ascii") + b"\0"
    payload += _canonical_json(value)
    digest = hmac.new(key, payload, hashlib.sha256).hexdigest()[:32]
    return f"gfp_{prefix}_{digest}"


def _manifest_hmac(key: bytes, manifest: Mapping[str, Any]) -> str:
    unsigned = dict(manifest)
    unsigned.pop("manifest_hmac_sha256", None)
    return hmac.new(
        key,
        MANIFEST_HMAC_DOMAIN + _canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()


def gmail_fact_parity_jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    return b"".join(_canonical_json(row) + b"\n" for row in rows)


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    _private_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GmailFactParityError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise GmailFactParityError(f"{label} is not a JSON object")
    return value


def _load_jsonl(path: Path, *, label: str) -> list[dict[str, Any]]:
    _private_file(path)
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GmailFactParityError(f"{label} is not valid JSONL") from exc
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise GmailFactParityError(f"{label} is empty or malformed")
    return rows


def validate_private_artifact_set(paths: Mapping[str, Path]) -> None:
    """Require every supplied private artifact to be a distinct inode."""

    identities: set[tuple[int, int]] = set()
    for path in paths.values():
        _private_file(Path(path))
        identity = _file_identity(Path(path))
        if identity in identities:
            raise GmailFactParityError("all evidence artifacts must be distinct files")
        identities.add(identity)


def _positive_int(value: Any, name: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise GmailFactParityError(f"{name} is invalid")
    return value


def _nonempty_string(value: Any, name: str, *, maximum: int = 1_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise GmailFactParityError(f"{name} is invalid")
    return value


def _digest(value: Any, name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise GmailFactParityError(f"{name} is invalid")
    return value


def _opaque_id(value: Any, kind: str) -> str:
    pattern = _OPAQUE_PATTERNS[kind]
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise GmailFactParityError(f"{kind} is invalid")
    return value


def _stage_counts(value: Any, name: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != set(STAGES):
        raise GmailFactParityError(f"{name} stage counts are invalid")
    return {
        stage: _positive_int(value[stage], f"{name}.{stage}", allow_zero=True)
        for stage in STAGES
    }


def _stage_membership(value: Any, name: str) -> dict[str, bool]:
    if (
        not isinstance(value, Mapping)
        or set(value) != set(STAGES)
        or any(not isinstance(value[stage], bool) for stage in STAGES)
    ):
        raise GmailFactParityError(f"{name} stage membership is invalid")
    stages = {stage: value[stage] for stage in STAGES}
    if not stages["candidate"]:
        raise GmailFactParityError(f"{name} member must exist at candidate stage")
    if stages["persisted"] and not stages["review"]:
        raise GmailFactParityError(f"{name} stage membership is not monotonic")
    return stages


def _parse_iso(value: Any, name: str) -> datetime:
    text = _nonempty_string(value, name, maximum=80)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GmailFactParityError(f"{name} is invalid") from exc
    if parsed.tzinfo is None:
        raise GmailFactParityError(f"{name} must include a timezone")
    return parsed


def _load_cohort_manifest(path: Path, *, hmac_key: bytes) -> dict[str, Any]:
    value = _load_json(path, label="cohort manifest")
    if set(value) != _COHORT_MANIFEST_KEYS:
        raise GmailFactParityError("cohort manifest schema is invalid")
    if value.get("version") != COHORT_MANIFEST_VERSION:
        raise GmailFactParityError("cohort manifest version is invalid")
    for name in (
        "cohort_sha256",
        "packet_sha256",
        "admission_join_sha256",
        "source_binding_sha256",
        "canonical_source_set_sha256",
        "original_inventory_sha256",
        "v2_inventory_sha256",
    ):
        _digest(value.get(name), f"cohort manifest {name}")
    for name in (
        "source_revision_count",
        "packet_count",
        "thread_count",
        "message_count",
        "original_admitted_message_count",
        "v2_admitted_message_count",
        "union_admitted_message_count",
    ):
        _positive_int(value.get(name), f"cohort manifest {name}")
    if (
        value.get("packet_policy") != "union_admitted_messages_only"
        or value.get("portable_identity") != "account_thread+source_revision+message_id"
        or value.get("renderer_versions_are_provenance_not_identity") is not True
        or value.get("provider_ids_in_packet_metadata") is not False
        or value.get("private_file_mode") != "0600"
        or value.get("private_directory_mode") != "0700"
    ):
        raise GmailFactParityError("cohort manifest privacy policy is invalid")
    authenticator = value.get("manifest_hmac_sha256")
    if (
        not isinstance(authenticator, str)
        or _SHA256_RE.fullmatch(authenticator) is None
        or not hmac.compare_digest(authenticator, _manifest_hmac(hmac_key, value))
    ):
        raise GmailFactParityError("cohort manifest authentication is invalid")
    return value


def _load_packets(path: Path) -> dict[str, dict[str, Any]]:
    rows = _load_jsonl(path, label="packet artifact")
    packets: dict[str, dict[str, Any]] = {}
    message_ids: set[str] = set()
    for row in rows:
        if set(row) != _PACKET_KEYS or row.get("version") != PACKET_VERSION:
            raise GmailFactParityError("packet artifact schema is invalid")
        packet_id = _opaque_id(row.get("packet_id"), "packet_id")
        if packet_id in packets:
            raise GmailFactParityError("packet artifact contains duplicate packets")
        thread_id = _opaque_id(row.get("thread_id"), "thread_id")
        revision_id = _opaque_id(row.get("revision_id"), "revision_id")
        projection_version = _positive_int(
            row.get("projection_version"), "packet projection version"
        )
        classifier_version = _positive_int(
            row.get("classifier_version"), "packet classifier version"
        )
        raw_messages = row.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise GmailFactParityError("packet messages are invalid")
        messages: list[dict[str, str]] = []
        local_ids: set[str] = set()
        for raw_message in raw_messages:
            if (
                not isinstance(raw_message, Mapping)
                or set(raw_message) != _PACKET_MESSAGE_KEYS
            ):
                raise GmailFactParityError("packet message schema is invalid")
            message_id = _opaque_id(raw_message.get("message_id"), "message_id")
            if message_id in local_ids or message_id in message_ids:
                raise GmailFactParityError("packet message IDs are not unique")
            internal_date = raw_message.get("internal_date")
            text = raw_message.get("text")
            if (
                not isinstance(internal_date, str)
                or not isinstance(text, str)
                or not text
            ):
                raise GmailFactParityError("packet message values are invalid")
            local_ids.add(message_id)
            message_ids.add(message_id)
            messages.append(
                {"message_id": message_id, "internal_date": internal_date, "text": text}
            )
        packets[packet_id] = {
            "version": PACKET_VERSION,
            "packet_id": packet_id,
            "thread_id": thread_id,
            "revision_id": revision_id,
            "projection_version": projection_version,
            "classifier_version": classifier_version,
            "messages": messages,
        }
    return packets


def _load_cohort(
    path: Path, packets: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    rows = _load_jsonl(path, label="cohort artifact")
    cohort: dict[str, dict[str, Any]] = {}
    for row in rows:
        if set(row) != _COHORT_KEYS or row.get("version") != COHORT_VERSION:
            raise GmailFactParityError("cohort artifact schema is invalid")
        packet_id = _opaque_id(row.get("packet_id"), "packet_id")
        if packet_id in cohort:
            raise GmailFactParityError("cohort artifact contains duplicate packets")
        packet = packets.get(packet_id)
        if packet is None:
            raise GmailFactParityError("cohort references an unknown packet")
        thread_id = _opaque_id(row.get("thread_id"), "thread_id")
        revision_id = _opaque_id(row.get("revision_id"), "revision_id")
        message_ids = row.get("message_ids")
        if (
            not isinstance(message_ids, list)
            or any(
                _OPAQUE_PATTERNS["message_id"].fullmatch(str(item)) is None
                for item in message_ids
            )
            or len(message_ids) != len(set(message_ids))
        ):
            raise GmailFactParityError("cohort message IDs are invalid")
        expected_message_ids = [item["message_id"] for item in packet["messages"]]
        packet_line = _canonical_json(packet) + b"\n"
        if (
            thread_id != packet["thread_id"]
            or revision_id != packet["revision_id"]
            or row.get("projection_version") != packet["projection_version"]
            or row.get("classifier_version") != packet["classifier_version"]
            or message_ids != expected_message_ids
            or _digest(row.get("source_sha256"), "cohort source digest")
            != row["source_sha256"]
            or _digest(row.get("packet_sha256"), "cohort packet digest")
            != _sha256_bytes(packet_line)
        ):
            raise GmailFactParityError("cohort and packet artifacts do not reconcile")
        cohort[packet_id] = dict(row)
    if set(cohort) != set(packets):
        raise GmailFactParityError("cohort does not exactly cover every packet")
    return cohort


def _renderer(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _RENDERER_KEYS:
        raise GmailFactParityError(f"{name} renderer schema is invalid")
    return {
        "projection_version": _positive_int(
            value.get("projection_version"), f"{name} projection version"
        ),
        "classifier_version": _positive_int(
            value.get("classifier_version"), f"{name} classifier version"
        ),
        "source_sha256": _digest(value.get("source_sha256"), f"{name} source digest"),
    }


def _opaque_message_list(value: Any, name: str) -> list[str]:
    if (
        not isinstance(value, list)
        or any(
            not isinstance(item, str)
            or _OPAQUE_PATTERNS["message_id"].fullmatch(item) is None
            for item in value
        )
        or len(value) != len(set(value))
    ):
        raise GmailFactParityError(f"{name} message IDs are invalid")
    return list(value)


def _load_admission_joins(
    path: Path,
    *,
    packets: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    rows = _load_jsonl(path, label="opaque admission join")
    joins: dict[str, dict[str, Any]] = {}
    for row in rows:
        if (
            set(row) != _ADMISSION_JOIN_KEYS
            or row.get("version") != ADMISSION_JOIN_VERSION
        ):
            raise GmailFactParityError("opaque admission join schema is invalid")
        packet_id = _opaque_id(row.get("packet_id"), "packet_id")
        packet = packets.get(packet_id)
        if packet is None or packet_id in joins:
            raise GmailFactParityError(
                "opaque admission join packet coverage is invalid"
            )
        original_ids = _opaque_message_list(
            row.get("original_message_ids"), "original admission"
        )
        v2_ids = _opaque_message_list(row.get("v2_message_ids"), "V2 admission")
        union_ids = _opaque_message_list(
            row.get("union_message_ids"), "union admission"
        )
        expected_union = [item["message_id"] for item in packet["messages"]]
        if (
            union_ids != expected_union
            or set(original_ids) | set(v2_ids) != set(union_ids)
            or original_ids != [item for item in union_ids if item in set(original_ids)]
            or v2_ids != [item for item in union_ids if item in set(v2_ids)]
        ):
            raise GmailFactParityError(
                "opaque admission join does not reconcile with its packet"
            )
        joins[packet_id] = {
            **dict(row),
            "original_renderer": _renderer(
                row.get("original_renderer"), "original admission"
            ),
            "v2_renderer": _renderer(row.get("v2_renderer"), "V2 admission"),
        }
    if set(joins) != set(packets):
        raise GmailFactParityError(
            "opaque admission join does not exactly cover every packet"
        )
    return joins


def _load_admission_inventory(
    path: Path,
    *,
    label: str,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    rows = _load_jsonl(path, label=f"{label} admission inventory")
    inventory: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        if (
            set(row) != _ADMISSION_INVENTORY_KEYS
            or row.get("version") != ADMISSION_INVENTORY_VERSION
        ):
            raise GmailFactParityError(f"{label} admission inventory schema is invalid")
        key = (
            _nonempty_string(row.get("gmail_account_key"), f"{label} account"),
            _nonempty_string(row.get("gmail_thread_id"), f"{label} thread"),
            _digest(row.get("gmail_source_revision"), f"{label} source revision"),
        )
        if key in inventory:
            raise GmailFactParityError(
                f"{label} admission inventory contains a duplicate source"
            )
        admitted_ids = row.get("admitted_message_ids")
        if (
            not isinstance(admitted_ids, list)
            or any(
                not isinstance(item, str)
                or _PROVIDER_MESSAGE_ID_RE.fullmatch(item) is None
                for item in admitted_ids
            )
            or len(admitted_ids) != len(set(admitted_ids))
        ):
            raise GmailFactParityError(
                f"{label} admission inventory message IDs are invalid"
            )
        inventory[key] = {
            "projection_version": _positive_int(
                row.get("gmail_projection_version"), f"{label} projection version"
            ),
            "classifier_version": _positive_int(
                row.get("gmail_classifier_version"), f"{label} classifier version"
            ),
            "source_sha256": _digest(
                row.get("source_sha256"), f"{label} source digest"
            ),
            "admitted_message_ids": list(admitted_ids),
        }
    return inventory


def _renderer_summary(
    inventory: Mapping[tuple[str, str, str], Mapping[str, Any]],
) -> list[dict[str, int]]:
    counts: dict[tuple[int, int], int] = defaultdict(int)
    for row in inventory.values():
        counts[(row["projection_version"], row["classifier_version"])] += 1
    return [
        {
            "projection_version": projection_version,
            "classifier_version": classifier_version,
            "source_count": counts[(projection_version, classifier_version)],
        }
        for projection_version, classifier_version in sorted(counts)
    ]


def _provider_message_list(value: Any, name: str) -> list[str]:
    if (
        not isinstance(value, list)
        or any(
            not isinstance(item, str) or _PROVIDER_MESSAGE_ID_RE.fullmatch(item) is None
            for item in value
        )
        or len(value) != len(set(value))
    ):
        raise GmailFactParityError(f"{name} provider message IDs are invalid")
    return list(value)


def _load_and_verify_source_bindings(
    path: Path,
    *,
    hmac_key: bytes,
    packets: Mapping[str, Mapping[str, Any]],
    cohort: Mapping[str, Mapping[str, Any]],
    joins: Mapping[str, Mapping[str, Any]],
    original_inventory: Mapping[tuple[str, str, str], Mapping[str, Any]],
    v2_inventory: Mapping[tuple[str, str, str], Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    rows = _load_jsonl(path, label="source identity binding")
    bindings: dict[tuple[str, str, str], dict[str, Any]] = {}
    bound_packet_ids: set[str] = set()
    source_set_rows: list[dict[str, str]] = []
    for index, row in enumerate(rows):
        if set(row) != _SOURCE_BINDING_KEYS or row.get("version") != BINDING_VERSION:
            raise GmailFactParityError("source identity binding schema is invalid")
        raw_key = (
            _nonempty_string(row.get("gmail_account_key"), f"binding[{index}] account"),
            _nonempty_string(row.get("gmail_thread_id"), f"binding[{index}] thread"),
            _digest(row.get("gmail_source_revision"), f"binding[{index}] revision"),
        )
        if raw_key in bindings or raw_key not in original_inventory:
            raise GmailFactParityError("source identity binding coverage is invalid")
        raw_thread_key = [raw_key[0], raw_key[1]]
        raw_revision_key = [*raw_thread_key, raw_key[2]]
        thread_id = _opaque_id(row.get("thread_id"), "thread_id")
        revision_id = _opaque_id(row.get("revision_id"), "revision_id")
        if thread_id != _derived_opaque_id(
            hmac_key, "t", raw_thread_key
        ) or revision_id != _derived_opaque_id(hmac_key, "r", raw_revision_key):
            raise GmailFactParityError("source identity binding opaque IDs are invalid")
        projection_version = _positive_int(
            row.get("projection_version"), f"binding[{index}] projection version"
        )
        classifier_version = _positive_int(
            row.get("classifier_version"), f"binding[{index}] classifier version"
        )
        source_sha256 = _digest(
            row.get("source_sha256"), f"binding[{index}] source digest"
        )
        original_raw = _provider_message_list(
            row.get("original_admitted_message_ids"), f"binding[{index}] original"
        )
        v2_raw = _provider_message_list(
            row.get("v2_admitted_message_ids"), f"binding[{index}] V2"
        )
        union_raw = _provider_message_list(
            row.get("union_admitted_message_ids"), f"binding[{index}] union"
        )
        raw_messages = row.get("messages")
        if not isinstance(raw_messages, list):
            raise GmailFactParityError("source identity binding messages are invalid")
        parsed_messages: list[tuple[str, str]] = []
        for raw_message in raw_messages:
            if (
                not isinstance(raw_message, Mapping)
                or set(raw_message) != _SOURCE_BINDING_MESSAGE_KEYS
            ):
                raise GmailFactParityError(
                    "source identity binding message schema is invalid"
                )
            gmail_message_id = _nonempty_string(
                raw_message.get("gmail_message_id"), "binding provider message ID"
            )
            if _PROVIDER_MESSAGE_ID_RE.fullmatch(gmail_message_id) is None:
                raise GmailFactParityError(
                    "source identity binding provider message ID is invalid"
                )
            message_id = _opaque_id(raw_message.get("message_id"), "message_id")
            expected_message_id = _derived_opaque_id(
                hmac_key, "m", [*raw_revision_key, gmail_message_id]
            )
            if message_id != expected_message_id:
                raise GmailFactParityError(
                    "source identity binding message identity is invalid"
                )
            parsed_messages.append((gmail_message_id, message_id))
        if (
            [item[0] for item in parsed_messages] != union_raw
            or len(parsed_messages) != len(set(parsed_messages))
            or set(original_raw)
            != set(original_inventory[raw_key]["admitted_message_ids"])
            or set(v2_raw) != set(v2_inventory[raw_key]["admitted_message_ids"])
            or set(original_raw) | set(v2_raw) != set(union_raw)
            or original_raw != [item for item in union_raw if item in set(original_raw)]
            or v2_raw != [item for item in union_raw if item in set(v2_raw)]
        ):
            raise GmailFactParityError(
                "source identity binding admission membership is invalid"
            )

        packet_id = row.get("packet_id")
        if union_raw:
            packet_id = _opaque_id(packet_id, "packet_id")
            expected_packet_id = _derived_opaque_id(
                hmac_key,
                "p",
                [raw_revision_key, projection_version, classifier_version, union_raw],
            )
            if packet_id != expected_packet_id or packet_id in bound_packet_ids:
                raise GmailFactParityError(
                    "source identity binding packet identity is invalid"
                )
            packet = packets.get(packet_id)
            cohort_row = cohort.get(packet_id)
            join = joins.get(packet_id)
            opaque_union = [item[1] for item in parsed_messages]
            opaque_original = [
                opaque_id
                for raw_id, opaque_id in parsed_messages
                if raw_id in set(original_raw)
            ]
            opaque_v2 = [
                opaque_id
                for raw_id, opaque_id in parsed_messages
                if raw_id in set(v2_raw)
            ]
            if (
                packet is None
                or cohort_row is None
                or join is None
                or packet["thread_id"] != thread_id
                or packet["revision_id"] != revision_id
                or packet["projection_version"] != projection_version
                or packet["classifier_version"] != classifier_version
                or [item["message_id"] for item in packet["messages"]] != opaque_union
                or cohort_row["source_sha256"] != source_sha256
                or join["original_message_ids"] != opaque_original
                or join["v2_message_ids"] != opaque_v2
                or join["union_message_ids"] != opaque_union
            ):
                raise GmailFactParityError(
                    "source identity binding does not reconcile with packet artifacts"
                )
            expected_original_renderer = {
                "projection_version": original_inventory[raw_key]["projection_version"],
                "classifier_version": original_inventory[raw_key]["classifier_version"],
                "source_sha256": original_inventory[raw_key]["source_sha256"],
            }
            expected_v2_renderer = {
                "projection_version": v2_inventory[raw_key]["projection_version"],
                "classifier_version": v2_inventory[raw_key]["classifier_version"],
                "source_sha256": v2_inventory[raw_key]["source_sha256"],
            }
            if (
                join["original_renderer"] != expected_original_renderer
                or join["v2_renderer"] != expected_v2_renderer
            ):
                raise GmailFactParityError(
                    "source identity binding renderer provenance is invalid"
                )
            bound_packet_ids.add(packet_id)
        elif packet_id is not None or parsed_messages or original_raw or v2_raw:
            raise GmailFactParityError(
                "empty source identity binding has packet membership"
            )
        source_set_rows.append(
            {"revision_id": revision_id, "source_sha256": source_sha256}
        )
        bindings[raw_key] = dict(row)

    if (
        set(bindings) != set(original_inventory)
        or bound_packet_ids != set(packets)
        or manifest.get("id_namespace")
        != _derived_opaque_id(hmac_key, "k", "cohort-namespace")
    ):
        raise GmailFactParityError("source identity binding is incomplete")
    canonical_source_set_sha256 = _sha256_bytes(
        gmail_fact_parity_jsonl_bytes(
            sorted(source_set_rows, key=lambda item: item["revision_id"])
        )
    )
    if canonical_source_set_sha256 != manifest.get("canonical_source_set_sha256"):
        raise GmailFactParityError(
            "canonical source set commitment does not authenticate"
        )
    return {
        "rows": bindings,
        "sha256": _sha256(path),
        "canonical_source_set_sha256": canonical_source_set_sha256,
    }


def load_gmail_fact_parity_bound_evidence(
    packets_path: Path,
    cohort_path: Path,
    admissions_path: Path,
    cohort_manifest_path: Path,
    source_bindings_path: Path,
    hmac_key_path: Path,
    original_inventory_path: Path,
    v2_inventory_path: Path,
) -> dict[str, Any]:
    """Load and reconcile the frozen private cohort and identical packets."""

    hmac_key = _private_hmac_key(hmac_key_path)
    manifest = _load_cohort_manifest(cohort_manifest_path, hmac_key=hmac_key)
    packets = _load_packets(packets_path)
    cohort = _load_cohort(cohort_path, packets)
    joins = _load_admission_joins(admissions_path, packets=packets)
    original_inventory = _load_admission_inventory(
        original_inventory_path, label="original"
    )
    v2_inventory = _load_admission_inventory(v2_inventory_path, label="V2")
    if set(original_inventory) != set(v2_inventory):
        raise GmailFactParityError(
            "admission inventories do not cover the same source set"
        )
    packet_digest = _sha256(packets_path)
    cohort_digest = _sha256(cohort_path)
    if packet_digest != manifest["packet_sha256"]:
        raise GmailFactParityError("cohort manifest does not match packet artifact")
    if cohort_digest != manifest["cohort_sha256"]:
        raise GmailFactParityError("cohort manifest does not match cohort artifact")
    if _sha256(admissions_path) != manifest["admission_join_sha256"]:
        raise GmailFactParityError(
            "cohort manifest does not match opaque admission joins"
        )
    if _sha256(source_bindings_path) != manifest["source_binding_sha256"]:
        raise GmailFactParityError(
            "cohort manifest does not match source identity bindings"
        )
    if _sha256(original_inventory_path) != manifest["original_inventory_sha256"]:
        raise GmailFactParityError(
            "cohort manifest does not match original admission inventory"
        )
    if _sha256(v2_inventory_path) != manifest["v2_inventory_sha256"]:
        raise GmailFactParityError(
            "cohort manifest does not match V2 admission inventory"
        )
    thread_count = len({row["thread_id"] for row in packets.values()})
    message_count = sum(len(row["messages"]) for row in packets.values())
    original_admitted = sum(
        len(row["admitted_message_ids"]) for row in original_inventory.values()
    )
    v2_admitted = sum(len(row["admitted_message_ids"]) for row in v2_inventory.values())
    union_admitted = sum(
        len(
            set(original_inventory[key]["admitted_message_ids"])
            | set(v2_inventory[key]["admitted_message_ids"])
        )
        for key in original_inventory
    )
    join_original = sum(len(row["original_message_ids"]) for row in joins.values())
    join_v2 = sum(len(row["v2_message_ids"]) for row in joins.values())
    join_union = sum(len(row["union_message_ids"]) for row in joins.values())
    if (
        len(packets) != manifest["packet_count"]
        or thread_count != manifest["thread_count"]
        or message_count != manifest["message_count"]
        or message_count != manifest["union_admitted_message_count"]
        or len(original_inventory) != manifest["source_revision_count"]
        or original_admitted != manifest["original_admitted_message_count"]
        or v2_admitted != manifest["v2_admitted_message_count"]
        or union_admitted != manifest["union_admitted_message_count"]
        or (join_original, join_v2, join_union)
        != (original_admitted, v2_admitted, union_admitted)
        or _renderer_summary(original_inventory)
        != manifest["original_renderer_provenance"]
        or _renderer_summary(v2_inventory) != manifest["v2_renderer_provenance"]
    ):
        raise GmailFactParityError("cohort manifest counts do not reconcile")
    source_bindings = _load_and_verify_source_bindings(
        source_bindings_path,
        hmac_key=hmac_key,
        packets=packets,
        cohort=cohort,
        joins=joins,
        original_inventory=original_inventory,
        v2_inventory=v2_inventory,
        manifest=manifest,
    )
    return {
        "packets": packets,
        "cohort": cohort,
        "admission_joins": joins,
        "manifest": manifest,
        "packet_sha256": packet_digest,
        "cohort_sha256": cohort_digest,
        "cohort_manifest_sha256": _sha256(cohort_manifest_path),
        "admission_join_sha256": _sha256(admissions_path),
        "source_binding_sha256": source_bindings["sha256"],
        "canonical_source_set_sha256": source_bindings["canonical_source_set_sha256"],
        "original_inventory_sha256": _sha256(original_inventory_path),
        "v2_inventory_sha256": _sha256(v2_inventory_path),
        "packet_count": len(packets),
        "thread_count": thread_count,
        "message_count": message_count,
    }


def _run_stage_record(value: Any, *, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _RUN_STAGE_RECORD_KEYS:
        raise GmailFactParityError(f"{name} stage record schema is invalid")
    if (
        value.get("version") != STAGE_CONTRACT_VERSION
        or value.get("contract_sha256") != STAGE_CONTRACT_SHA256
    ):
        raise GmailFactParityError(f"{name} stage record contract is invalid")
    stages = _stage_membership(value.get("stages"), f"{name} stage record")
    candidate_digest = _digest(
        value.get("candidate_sha256"), f"{name} candidate digest"
    )
    action_id = value.get("action_id")
    if action_id is not None:
        action_id = _nonempty_string(action_id, f"{name} action ID", maximum=256)
    disposition = value.get("disposition")
    if disposition not in STAGE_DISPOSITIONS:
        raise GmailFactParityError(f"{name} stage record disposition is invalid")
    action_status = value.get("action_status")
    if action_status is not None and action_status not in ACCEPTED_ACTION_STATUSES:
        raise GmailFactParityError(f"{name} stage record action status is invalid")
    if (action_id is None) != (action_status is None):
        raise GmailFactParityError(f"{name} stage record action identity is invalid")
    raw_fact_ids = value.get("persisted_fact_ids")
    if (
        not isinstance(raw_fact_ids, list)
        or any(
            not isinstance(item, str) or not item.strip() or len(item) > 256
            for item in raw_fact_ids
        )
        or len(raw_fact_ids) != len(set(raw_fact_ids))
    ):
        raise GmailFactParityError(f"{name} stage record fact IDs are invalid")

    expected: tuple[str, bool, bool, int] | None = None
    if action_status in APPLIED_ACTION_STATUSES:
        expected = ("applied", True, True, 1)
    elif action_status is None or action_status == "proposed":
        expected = ("deferred", False, False, 0)
    elif action_status == "needs_human":
        if disposition == "rejected":
            expected = ("rejected", False, False, 0)
        elif disposition == "residue":
            expected = ("residue", True, False, 0)
        else:
            expected = ("deferred", True, False, 0)
    elif action_status == "failed":
        expected = ("deferred", True, False, 0)
    elif action_status in {"rejected", "dismissed"}:
        expected = ("rejected", False, False, 0)
    elif action_status == "reverted":
        expected = ("rejected", True, False, 0)
    if expected is None:
        raise GmailFactParityError(f"{name} stage record is not derivable")
    expected_disposition, expected_review, expected_persisted, expected_fact_count = (
        expected
    )
    if (
        disposition != expected_disposition
        or stages["review"] is not expected_review
        or stages["persisted"] is not expected_persisted
        or len(raw_fact_ids) != expected_fact_count
    ):
        raise GmailFactParityError(f"{name} stage record is internally inconsistent")
    return {
        "version": STAGE_CONTRACT_VERSION,
        "contract_sha256": STAGE_CONTRACT_SHA256,
        "candidate_sha256": candidate_digest,
        "action_id": action_id,
        "stages": stages,
        "disposition": disposition,
        "action_status": action_status,
        "persisted_fact_ids": list(raw_fact_ids),
    }


def _run_member(
    value: Any,
    *,
    name: str,
    allowed_message_ids: set[str],
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _RUN_MEMBER_KEYS:
        raise GmailFactParityError(f"{name} member schema is invalid")
    statement = _nonempty_string(
        value.get("statement"), f"{name} statement", maximum=200_000
    )
    evidence_ids = value.get("evidence_message_ids")
    if (
        not isinstance(evidence_ids, list)
        or not evidence_ids
        or any(
            _OPAQUE_PATTERNS["message_id"].fullmatch(str(item)) is None
            for item in evidence_ids
        )
        or len(evidence_ids) != len(set(evidence_ids))
        or not set(evidence_ids) <= allowed_message_ids
    ):
        raise GmailFactParityError(f"{name} evidence messages are invalid")
    stages = _stage_membership(value.get("stages"), name)
    stage_record = _run_stage_record(value.get("stage_record"), name=name)
    if stages != stage_record["stages"]:
        raise GmailFactParityError(f"{name} stages do not match their stage record")
    return {
        "statement": statement,
        "evidence_message_ids": list(evidence_ids),
        "stages": stages,
        "stage_record": stage_record,
    }


def gmail_fact_parity_member_id(
    run_id: str,
    packet_id: str,
    member_index: int,
    member: Mapping[str, Any],
) -> str:
    """Assign an opaque member ID at the adapter boundary, never in the model."""

    if _RUN_ID_RE.fullmatch(str(run_id)) is None:
        raise GmailFactParityError("run ID is invalid for member identity")
    _opaque_id(packet_id, "packet_id")
    if (
        isinstance(member_index, bool)
        or not isinstance(member_index, int)
        or member_index < 0
        or not isinstance(member, Mapping)
        or set(member) != _RUN_MEMBER_KEYS
    ):
        raise GmailFactParityError("member identity input is invalid")
    material = _canonical_json(
        [
            "gmail-fact-parity-member-v2",
            run_id,
            packet_id,
            member_index,
            dict(member),
        ]
    )
    return f"gfp_a_{hashlib.sha256(material).hexdigest()[:32]}"


def _load_run_output(
    path: Path,
    *,
    expected_run_id: str,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    rows = _load_jsonl(path, label=f"{expected_run_id} run output")
    header = rows[0]
    if set(header) != _RUN_HEADER_KEYS or header.get("version") != RUN_VERSION:
        raise GmailFactParityError(f"{expected_run_id} run header is invalid")
    run_id = header.get("run_id")
    if run_id != expected_run_id or _RUN_ID_RE.fullmatch(str(run_id)) is None:
        raise GmailFactParityError("run output ID does not match its argument")
    if header.get("arm") not in {"original", "v2"}:
        raise GmailFactParityError(f"{run_id} arm is invalid")
    for name in ("commit", "prompt_version", "model", "reasoning_effort"):
        _nonempty_string(header.get(name), f"{run_id} {name}")
    if _GIT_COMMIT_RE.fullmatch(header["commit"]) is None:
        raise GmailFactParityError(f"{run_id} commit is not a full Git object ID")
    for name in (
        "stage_contract_sha256",
        "adapter_sha256",
        "adapter_executable_sha256",
        "production_tree_sha256",
        "runtime_config_sha256",
        "prompt_sha256",
        "invocation_ledger_sha256",
    ):
        _digest(header.get(name), f"{run_id} {name}")
    if (
        header.get("stage_contract_version") != STAGE_CONTRACT_VERSION
        or header.get("stage_contract_sha256") != STAGE_CONTRACT_SHA256
    ):
        raise GmailFactParityError(f"{run_id} stage contract binding is invalid")
    if (
        header.get("cohort_sha256") != evidence["cohort_sha256"]
        or header.get("packet_sha256") != evidence["packet_sha256"]
    ):
        raise GmailFactParityError(f"{run_id} output is bound to a different cohort")

    packet_rows: dict[str, dict[str, Any]] = {}
    members: dict[str, dict[str, Any]] = {}
    member_signatures: set[bytes] = set()
    candidate_owners: dict[str, tuple[str, int]] = {}
    action_owners: dict[str, str] = {}
    fact_owners: dict[str, str] = {}
    structural_exact_duplicates = 0
    for row in rows[1:]:
        if set(row) != _RUN_PACKET_KEYS or row.get("version") != RUN_PACKET_VERSION:
            raise GmailFactParityError(f"{run_id} packet output schema is invalid")
        if row.get("run_id") != run_id:
            raise GmailFactParityError(
                f"{run_id} packet output has a mismatched run ID"
            )
        packet_id = _opaque_id(row.get("packet_id"), "packet_id")
        if packet_id in packet_rows:
            raise GmailFactParityError(f"{run_id} output contains a duplicate packet")
        packet = evidence["packets"].get(packet_id)
        if packet is None or row.get("thread_id") != packet["thread_id"]:
            raise GmailFactParityError(f"{run_id} output packet identity is invalid")
        raw_members = row.get("members")
        if not isinstance(raw_members, list):
            raise GmailFactParityError(f"{run_id} packet members are invalid")
        allowed_message_ids = {item["message_id"] for item in packet["messages"]}
        parsed_members = []
        for member_index, item in enumerate(raw_members):
            parsed = _run_member(
                item,
                name=f"{run_id}.{packet_id}",
                allowed_message_ids=allowed_message_ids,
            )
            stage_record = parsed["stage_record"]
            candidate_digest = stage_record["candidate_sha256"]
            if candidate_digest in candidate_owners:
                raise GmailFactParityError(
                    f"{run_id} candidate ownership is reused across records"
                )
            candidate_owners[candidate_digest] = (packet_id, member_index)
            action_id = stage_record["action_id"]
            if action_id is not None:
                if action_id in action_owners:
                    raise GmailFactParityError(
                        f"{run_id} action ownership is reused across candidates"
                    )
                action_owners[action_id] = candidate_digest
            for fact_id in stage_record["persisted_fact_ids"]:
                if fact_id in fact_owners:
                    raise GmailFactParityError(
                        f"{run_id} persisted fact ownership is reused across candidates"
                    )
                fact_owners[fact_id] = candidate_digest
            parsed_members.append(
                {
                    "member_id": gmail_fact_parity_member_id(
                        run_id, packet_id, member_index, parsed
                    ),
                    **parsed,
                }
            )
            signature = _canonical_json(parsed)
            if signature in member_signatures:
                structural_exact_duplicates += 1
            member_signatures.add(signature)
        for member in parsed_members:
            member_id = member["member_id"]
            if member_id in members:
                raise GmailFactParityError(f"{run_id} member IDs are not unique")
            members[member_id] = {**member, "packet_id": packet_id}
        packet_rows[packet_id] = {
            "version": RUN_PACKET_VERSION,
            "run_id": run_id,
            "packet_id": packet_id,
            "thread_id": packet["thread_id"],
            "members": sorted(parsed_members, key=lambda item: item["member_id"]),
        }
    if set(packet_rows) != set(evidence["packets"]):
        raise GmailFactParityError(
            f"{run_id} output does not exactly cover every packet"
        )
    return {
        **dict(header),
        "output_sha256": _sha256(path),
        "packets": packet_rows,
        "members": members,
        "structural_exact_duplicate_members": structural_exact_duplicates,
    }


def _invocation_ledger(
    value: Any,
    *,
    run_id: str,
    run: Mapping[str, Any],
    receipt_started: datetime,
    receipt_completed: datetime,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise GmailFactParityError(f"{run_id} invocation ledger is empty or invalid")
    parsed: list[dict[str, Any]] = []
    invocation_ids: set[str] = set()
    scopes: dict[str, list[tuple[int, int]]] = defaultdict(list)
    previous_started: datetime | None = None
    for ordinal, raw_item in enumerate(value):
        if not isinstance(raw_item, Mapping) or set(raw_item) != _INVOCATION_KEYS:
            raise GmailFactParityError(f"{run_id} invocation ledger schema is invalid")
        invocation_id = raw_item.get("invocation_id")
        if (
            not isinstance(invocation_id, str)
            or _INVOCATION_ID_RE.fullmatch(invocation_id) is None
            or invocation_id in invocation_ids
            or raw_item.get("ordinal") != ordinal
        ):
            raise GmailFactParityError(f"{run_id} invocation ledger order is invalid")
        invocation_ids.add(invocation_id)
        packet_id = _opaque_id(raw_item.get("packet_id"), "packet_id")
        if packet_id not in run["packets"]:
            raise GmailFactParityError(f"{run_id} invocation packet scope is invalid")
        window_index = _positive_int(
            raw_item.get("window_index"),
            f"{run_id} invocation window_index",
            allow_zero=True,
        )
        window_count = _positive_int(
            raw_item.get("window_count"), f"{run_id} invocation window_count"
        )
        if window_index >= window_count:
            raise GmailFactParityError(f"{run_id} invocation window scope is invalid")
        scopes[packet_id].append((window_index, window_count))
        _digest(raw_item.get("request_sha256"), f"{run_id} invocation request")
        _digest(raw_item.get("response_sha256"), f"{run_id} invocation response")
        if raw_item.get("provider") != EXPECTED_PROVIDER:
            raise GmailFactParityError(f"{run_id} invocation provider is invalid")
        if (
            raw_item.get("model") != run["model"]
            or raw_item.get("reasoning_effort") != run["reasoning_effort"]
        ):
            raise GmailFactParityError(f"{run_id} invocation target is invalid")
        started = _parse_iso(
            raw_item.get("started_at"), f"{run_id} invocation started_at"
        )
        completed = _parse_iso(
            raw_item.get("completed_at"), f"{run_id} invocation completed_at"
        )
        if (
            completed < started
            or started < receipt_started
            or completed > receipt_completed
            or (previous_started is not None and started < previous_started)
        ):
            raise GmailFactParityError(f"{run_id} invocation chronology is invalid")
        previous_started = started
        parsed.append(dict(raw_item))

    if set(scopes) != set(run["packets"]):
        raise GmailFactParityError(
            f"{run_id} invocation ledger does not cover every packet"
        )
    for packet_scopes in scopes.values():
        window_counts = {count for _, count in packet_scopes}
        if len(window_counts) != 1:
            raise GmailFactParityError(f"{run_id} invocation window counts disagree")
        window_count = next(iter(window_counts))
        if {index for index, _ in packet_scopes} != set(range(window_count)):
            raise GmailFactParityError(f"{run_id} invocation windows are incomplete")
    return parsed


def _load_receipt(
    path: Path,
    *,
    expected_run_id: str,
    run: Mapping[str, Any],
) -> dict[str, Any]:
    value = _load_json(path, label=f"{expected_run_id} run receipt")
    if set(value) != _RECEIPT_KEYS or value.get("version") != RECEIPT_VERSION:
        raise GmailFactParityError(f"{expected_run_id} run receipt schema is invalid")
    run_id = value.get("run_id")
    if run_id != expected_run_id or _RUN_ID_RE.fullmatch(str(run_id)) is None:
        raise GmailFactParityError(f"{expected_run_id} run receipt identity is invalid")
    started = _parse_iso(value.get("started_at"), f"{run_id} started_at")
    completed = _parse_iso(value.get("completed_at"), f"{run_id} completed_at")
    if completed < started:
        raise GmailFactParityError(f"{run_id} receipt chronology is invalid")
    for name in _RUN_BINDING_KEYS:
        if value.get(name) != run.get(name):
            raise GmailFactParityError(
                f"{run_id} receipt binding does not match its run"
            )
    if (
        value.get("output_sha256") != run["output_sha256"]
        or value.get("model") != run["model"]
        or value.get("reasoning_effort") != run["reasoning_effort"]
        or value.get("provider") != EXPECTED_PROVIDER
        or value.get("attestation") != INVOCATION_ATTESTATION
    ):
        raise GmailFactParityError(f"{run_id} receipt does not match its run output")
    invocations = _invocation_ledger(
        value.get("invocations"),
        run_id=run_id,
        run=run,
        receipt_started=started,
        receipt_completed=completed,
    )
    if _sha256_bytes(_canonical_json(invocations)) != run["invocation_ledger_sha256"]:
        raise GmailFactParityError(f"{run_id} invocation ledger digest is invalid")
    return {**value, "invocations": invocations, "receipt_sha256": _sha256(path)}


def load_gmail_fact_parity_judge_receipt(
    path: Path,
    *,
    completed_units_sha256: str,
    work_queue_sha256: str,
    evidence: Mapping[str, Any],
    run_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind final semantic judgments to the required external Sol invocation."""

    value = _load_json(path, label="completed-units judge receipt")
    if (
        set(value) != _JUDGE_RECEIPT_KEYS
        or value.get("version") != JUDGE_RECEIPT_VERSION
    ):
        raise GmailFactParityError("completed-units judge receipt schema is invalid")
    invocation_id = value.get("invocation_id")
    if (
        not isinstance(invocation_id, str)
        or _INVOCATION_ID_RE.fullmatch(invocation_id) is None
    ):
        raise GmailFactParityError("completed-units judge receipt identity is invalid")
    started = _parse_iso(value.get("started_at"), "judge started_at")
    completed = _parse_iso(value.get("completed_at"), "judge completed_at")
    if completed < started:
        raise GmailFactParityError(
            "completed-units judge receipt chronology is invalid"
        )
    if (
        value.get("provider") != EXPECTED_PROVIDER
        or value.get("model") != EXPECTED_JUDGE_MODEL
        or value.get("reasoning_effort") != EXPECTED_JUDGE_REASONING_EFFORT
        or value.get("cohort_sha256") != evidence["cohort_sha256"]
        or value.get("packet_sha256") != evidence["packet_sha256"]
        or value.get("work_queue_sha256") != work_queue_sha256
        or value.get("completed_units_sha256") != completed_units_sha256
        or value.get("judge_contract_version") != JUDGE_CONTRACT_VERSION
        or value.get("judge_contract_sha256") != JUDGE_CONTRACT_SHA256
        or value.get("attestation") != INVOCATION_ATTESTATION
    ):
        raise GmailFactParityError(
            "completed-units judge receipt does not match the required external judge"
        )
    run_invocation_ids = {
        invocation["invocation_id"]
        for receipt in run_evidence["receipts"].values()
        for invocation in receipt["invocations"]
    }
    if invocation_id in run_invocation_ids:
        raise GmailFactParityError(
            "completed-units judge invocation ID must be distinct from evidence runs"
        )
    latest_evidence_completion = max(
        _parse_iso(receipt["completed_at"], "evidence run completed_at")
        for receipt in run_evidence["receipts"].values()
    )
    if started < latest_evidence_completion:
        raise GmailFactParityError(
            "completed-units judge started before the evidence runs completed"
        )
    return {**value, "receipt_sha256": _sha256(path)}


def load_gmail_fact_parity_runs(
    run_output_paths: Mapping[str, Path],
    run_receipt_paths: Mapping[str, Path],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Parse every run, require exact packet coverage, and bind receipts."""

    if set(run_output_paths) != set(run_receipt_paths) or not run_output_paths:
        raise GmailFactParityError("run output and receipt coverage must match")
    runs = {
        run_id: _load_run_output(Path(path), expected_run_id=run_id, evidence=evidence)
        for run_id, path in run_output_paths.items()
    }
    originals = [run_id for run_id, run in runs.items() if run["arm"] == "original"]
    v2_ids = sorted(run_id for run_id, run in runs.items() if run["arm"] == "v2")
    if len(originals) != 1:
        raise GmailFactParityError("exactly one original run is required")
    if len(v2_ids) < MIN_V2_RUNS:
        raise GmailFactParityError(f"at least {MIN_V2_RUNS} V2 runs are required")
    original_id = originals[0]
    original_target_config = {
        "commit": runs[original_id]["commit"],
        "prompt_version": runs[original_id]["prompt_version"],
        "prompt_sha256": runs[original_id]["prompt_sha256"],
        "model": runs[original_id]["model"],
        "reasoning_effort": runs[original_id]["reasoning_effort"],
        "production_tree_sha256": runs[original_id]["production_tree_sha256"],
        "runtime_config_sha256": runs[original_id]["runtime_config_sha256"],
        "adapter_sha256": runs[original_id]["adapter_sha256"],
        "adapter_executable_sha256": runs[original_id]["adapter_executable_sha256"],
    }
    if {
        key: original_target_config[key]
        for key in (
            "commit",
            "prompt_version",
            "prompt_sha256",
            "model",
            "reasoning_effort",
            "production_tree_sha256",
            "runtime_config_sha256",
        )
    } != {
        "commit": EXPECTED_ORIGINAL_COMMIT,
        "prompt_version": EXPECTED_ORIGINAL_PROMPT_VERSION,
        "prompt_sha256": EXPECTED_ORIGINAL_PROMPT_SHA256,
        "model": EXPECTED_ORIGINAL_MODEL,
        "reasoning_effort": EXPECTED_ORIGINAL_REASONING_EFFORT,
        "production_tree_sha256": EXPECTED_ORIGINAL_PRODUCTION_TREE_SHA256,
        "runtime_config_sha256": EXPECTED_ORIGINAL_RUNTIME_CONFIG_SHA256,
    }:
        raise GmailFactParityError(
            "original target config is not the pinned installed baseline"
        )
    adapter_digests = {run["adapter_sha256"] for run in runs.values()}
    if len(adapter_digests) != 1:
        raise GmailFactParityError("runs do not share one exact adapter code digest")
    v2_target_config = {
        "commit": runs[v2_ids[0]]["commit"],
        "prompt_version": runs[v2_ids[0]]["prompt_version"],
        "prompt_sha256": runs[v2_ids[0]]["prompt_sha256"],
        "model": runs[v2_ids[0]]["model"],
        "reasoning_effort": runs[v2_ids[0]]["reasoning_effort"],
        "production_tree_sha256": runs[v2_ids[0]]["production_tree_sha256"],
        "runtime_config_sha256": runs[v2_ids[0]]["runtime_config_sha256"],
        "adapter_sha256": runs[v2_ids[0]]["adapter_sha256"],
        "adapter_executable_sha256": runs[v2_ids[0]]["adapter_executable_sha256"],
    }
    if any(
        {
            "commit": runs[run_id]["commit"],
            "prompt_version": runs[run_id]["prompt_version"],
            "prompt_sha256": runs[run_id]["prompt_sha256"],
            "model": runs[run_id]["model"],
            "reasoning_effort": runs[run_id]["reasoning_effort"],
            "production_tree_sha256": runs[run_id]["production_tree_sha256"],
            "runtime_config_sha256": runs[run_id]["runtime_config_sha256"],
            "adapter_sha256": runs[run_id]["adapter_sha256"],
            "adapter_executable_sha256": runs[run_id]["adapter_executable_sha256"],
        }
        != v2_target_config
        for run_id in v2_ids
    ):
        raise GmailFactParityError("V2 runs do not share one exact target config")
    if (
        v2_target_config["prompt_version"] != EXPECTED_V2_PROMPT_VERSION
        or v2_target_config["model"] != EXPECTED_V2_MODEL
        or v2_target_config["reasoning_effort"] != EXPECTED_V2_REASONING_EFFORT
    ):
        raise GmailFactParityError("V2 target config is not the pinned release config")
    canonical_adapter_authority = _expected_canonical_adapter_authority()
    expected_adapter_sha256 = (
        canonical_adapter_authority["adapter_sha256"]
        if canonical_adapter_authority is not None
        else None
    )
    exact_adapter_authority = (
        expected_adapter_sha256 is not None
        and adapter_digests == {expected_adapter_sha256}
    )
    receipts = {
        run_id: _load_receipt(
            Path(run_receipt_paths[run_id]),
            expected_run_id=run_id,
            run=run,
        )
        for run_id, run in runs.items()
    }
    invocation_ids = [
        invocation["invocation_id"]
        for receipt in receipts.values()
        for invocation in receipt["invocations"]
    ]
    if len(invocation_ids) != len(set(invocation_ids)):
        raise GmailFactParityError("claimed invocation IDs must be unique")
    aliases = {
        run_id: (
            "gfp_x_"
            + hashlib.sha256(
                _canonical_json(
                    [
                        "gmail-fact-parity-blinded-run-v1",
                        evidence["cohort_sha256"],
                        runs[run_id]["output_sha256"],
                    ]
                )
            ).hexdigest()[:24]
        )
        for run_id in runs
    }
    if len(set(aliases.values())) != len(aliases):
        raise GmailFactParityError("blinded run aliases are not unique")
    return {
        "runs": runs,
        "receipts": receipts,
        "original_run_id": original_id,
        "v2_run_ids": v2_ids,
        "all_run_ids": [originals[0], *v2_ids],
        "run_aliases": aliases,
        "alias_run_ids": {alias: run_id for run_id, alias in aliases.items()},
        "all_run_aliases": [aliases[originals[0]], *(aliases[item] for item in v2_ids)],
        "original_run_alias": aliases[original_id],
        "original_target_config": original_target_config,
        "v2_run_aliases": [aliases[item] for item in v2_ids],
        "v2_target_config": v2_target_config,
        "target_authority": {
            "original_baseline_exact": True,
            "v2_runs_consistent": True,
            "canonical_adapter_available": expected_adapter_sha256 is not None,
            "canonical_adapter_tracked_at_git_head": (
                canonical_adapter_authority is not None
            ),
            "canonical_adapter_git_head": (
                canonical_adapter_authority["git_head"]
                if canonical_adapter_authority is not None
                else None
            ),
            "canonical_adapter_exact": exact_adapter_authority,
            "per_run_executable_bindings_present": True,
            "cross_arm_executable_equality_required": False,
            "passed": exact_adapter_authority,
        },
        "independent_invocations_verified": False,
        "claimed_invocation_ids_unique": True,
    }


def _work_item_id(cohort_sha256: str, packet_id: str) -> str:
    digest = hashlib.sha256(
        f"gmail-fact-parity-work-v1\0{cohort_sha256}\0{packet_id}".encode("ascii")
    ).hexdigest()[:32]
    return f"gfp_w_{digest}"


def build_gmail_fact_parity_work_queue(
    evidence: Mapping[str, Any], run_evidence: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for packet_id, packet in sorted(evidence["packets"].items()):
        rows.append(
            {
                "version": WORK_ITEM_VERSION,
                "work_item_id": _work_item_id(evidence["cohort_sha256"], packet_id),
                "packet_id": packet_id,
                "thread_id": packet["thread_id"],
                "messages": [dict(item) for item in packet["messages"]],
                "members": {
                    run_evidence["run_aliases"][run_id]: run_evidence["runs"][run_id][
                        "packets"
                    ][packet_id]["members"]
                    for run_id in run_evidence["all_run_ids"]
                },
            }
        )
    return rows


def _load_and_verify_work_queue(
    path: Path,
    *,
    expected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = _load_jsonl(path, label="alignment work queue")
    for row in rows:
        if set(row) != _WORK_ITEM_KEYS or row.get("version") != WORK_ITEM_VERSION:
            raise GmailFactParityError("alignment work queue schema is invalid")
        _opaque_id(row.get("work_item_id"), "work_item_id")
    if rows != expected:
        raise GmailFactParityError(
            "alignment work queue does not match bound run outputs"
        )
    return rows


def _judgment(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _JUDGMENT_KEYS:
        raise GmailFactParityError(f"{name} judgment schema is invalid")
    critical_error = value.get("critical_error")
    if (
        not isinstance(value.get("supported"), bool)
        or not isinstance(value.get("scope_correct"), bool)
        or critical_error not in CRITICAL_ERRORS
    ):
        raise GmailFactParityError(f"{name} judgment is invalid")
    return {
        "member_id": _opaque_id(value.get("member_id"), "member_id"),
        "supported": value["supported"],
        "scope_correct": value["scope_correct"],
        "critical_error": critical_error,
    }


def _unit_id(
    cohort_sha256: str,
    packet_id: str,
    members: Mapping[str, list[dict[str, Any]]],
) -> str:
    identity = [
        [run_id, item["member_id"]]
        for run_id in sorted(members)
        for item in sorted(members[run_id], key=lambda value: value["member_id"])
    ]
    material = _canonical_json([cohort_sha256, packet_id, identity])
    return f"gfp_u_{hashlib.sha256(material).hexdigest()[:32]}"


def load_gmail_fact_parity_completed_units(
    path: Path,
    *,
    evidence: Mapping[str, Any],
    run_evidence: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Require every packet and emitted member to receive one complete judgment."""

    rows = _load_jsonl(path, label="completed semantic units")
    expected_members = {
        run_id: set(run_evidence["runs"][run_id]["members"])
        for run_id in run_evidence["all_run_ids"]
    }
    empty_packets = {
        packet_id
        for packet_id in evidence["packets"]
        if all(
            not run_evidence["runs"][run_id]["packets"][packet_id]["members"]
            for run_id in run_evidence["all_run_ids"]
        )
    }
    seen = {run_id: set() for run_id in run_evidence["all_run_ids"]}
    normalized: list[dict[str, Any]] = []
    unit_ids: set[str] = set()
    for index, row in enumerate(rows):
        if (
            set(row) != _COMPLETED_UNIT_KEYS
            or row.get("version") != COMPLETED_UNIT_VERSION
        ):
            raise GmailFactParityError("completed semantic unit schema is invalid")
        packet_id = _opaque_id(row.get("packet_id"), "packet_id")
        if packet_id not in evidence["packets"]:
            raise GmailFactParityError(
                "completed semantic unit references an unknown packet"
            )
        classification = row.get("classification")
        if (
            not isinstance(row.get("useful"), bool)
            or classification not in CLASSIFICATIONS
        ):
            raise GmailFactParityError("completed semantic unit labels are invalid")
        raw_members = row.get("members")
        if not isinstance(raw_members, Mapping) or set(raw_members) != set(
            run_evidence["all_run_aliases"]
        ):
            raise GmailFactParityError(
                "completed semantic unit run coverage is incomplete"
            )
        members: dict[str, list[dict[str, Any]]] = {}
        member_count = 0
        for run_id in run_evidence["all_run_ids"]:
            run_alias = run_evidence["run_aliases"][run_id]
            raw_run_members = raw_members[run_alias]
            if not isinstance(raw_run_members, list):
                raise GmailFactParityError(
                    "completed semantic unit members are invalid"
                )
            parsed = sorted(
                (
                    _judgment(item, f"completed[{index}].{run_alias}")
                    for item in raw_run_members
                ),
                key=lambda item: item["member_id"],
            )
            local_ids = [item["member_id"] for item in parsed]
            if len(local_ids) != len(set(local_ids)):
                raise GmailFactParityError("completed unit contains duplicate members")
            for member_id in local_ids:
                source_member = run_evidence["runs"][run_id]["members"].get(member_id)
                if source_member is None or source_member["packet_id"] != packet_id:
                    raise GmailFactParityError(
                        "completed unit member binding is invalid"
                    )
                if member_id in seen[run_id]:
                    raise GmailFactParityError(
                        "an emitted member is aligned more than once"
                    )
                seen[run_id].add(member_id)
            members[run_id] = parsed
            member_count += len(parsed)
        if member_count == 0 and (
            packet_id not in empty_packets
            or row["useful"]
            or classification != "not_fact"
        ):
            raise GmailFactParityError(
                "an empty semantic unit is valid only for an all-empty packet"
            )
        unit_id = _unit_id(evidence["cohort_sha256"], packet_id, members)
        if unit_id in unit_ids:
            raise GmailFactParityError("completed semantic units are duplicated")
        unit_ids.add(unit_id)
        normalized.append(
            {
                "unit_id": unit_id,
                "packet_id": packet_id,
                "thread_id": evidence["packets"][packet_id]["thread_id"],
                "useful": row["useful"],
                "classification": classification,
                "members": members,
            }
        )
    for run_id, expected in expected_members.items():
        if seen[run_id] != expected:
            raise GmailFactParityError(
                f"completed units do not align every emitted member for {run_id}"
            )
    if {unit["packet_id"] for unit in normalized} != set(evidence["packets"]):
        raise GmailFactParityError("completed units do not label every packet")
    return sorted(normalized, key=lambda item: item["unit_id"])


def _arm_from_unit(
    unit: Mapping[str, Any],
    *,
    run_id: str,
    run_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    members = []
    for judgment in unit["members"][run_id]:
        source = run_evidence["runs"][run_id]["members"][judgment["member_id"]]
        members.append({**judgment, "stages": source["stages"]})
    return {
        "stage_counts": {
            stage: sum(member["stages"][stage] for member in members)
            for stage in STAGES
        },
        "members": members,
    }


def derive_gmail_fact_parity_units(
    completed_units: list[dict[str, Any]],
    *,
    run_evidence: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    alignment: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    original_id = run_evidence["original_run_id"]
    for unit in completed_units:
        alignment.append(
            {
                "version": ALIGNMENT_VERSION,
                "unit_id": unit["unit_id"],
                "packet_id": unit["packet_id"],
                "thread_id": unit["thread_id"],
                "members": {
                    run_evidence["run_aliases"][run_id]: [
                        item["member_id"] for item in unit["members"][run_id]
                    ]
                    for run_id in run_evidence["all_run_ids"]
                },
            }
        )
        labels.append(
            {
                "version": LABEL_VERSION,
                "unit_id": unit["unit_id"],
                "packet_id": unit["packet_id"],
                "thread_id": unit["thread_id"],
                "useful": unit["useful"],
                "classification": unit["classification"],
                "original": _arm_from_unit(
                    unit, run_id=original_id, run_evidence=run_evidence
                ),
                "v2": {
                    run_evidence["run_aliases"][run_id]: _arm_from_unit(
                        unit, run_id=run_id, run_evidence=run_evidence
                    )
                    for run_id in run_evidence["v2_run_ids"]
                },
            }
        )
    return alignment, labels


def _run_manifest(
    run_id: str,
    *,
    run_evidence: Mapping[str, Any],
    labels: list[dict[str, Any]],
) -> dict[str, Any]:
    run = run_evidence["runs"][run_id]
    receipt = run_evidence["receipts"][run_id]
    run_alias = run_evidence["run_aliases"][run_id]
    evidences = [
        row["original"]
        if run_id == run_evidence["original_run_id"]
        else row["v2"][run_alias]
        for row in labels
    ]
    return {
        "run_alias": run_alias,
        "arm": run["arm"],
        "commit": run["commit"],
        "prompt_version": run["prompt_version"],
        "model": run["model"],
        "reasoning_effort": run["reasoning_effort"],
        "provider": receipt["provider"],
        "stage_contract_version": run["stage_contract_version"],
        "stage_contract_sha256": run["stage_contract_sha256"],
        "adapter_sha256": run["adapter_sha256"],
        "adapter_executable_sha256": run["adapter_executable_sha256"],
        "production_tree_sha256": run["production_tree_sha256"],
        "runtime_config_sha256": run["runtime_config_sha256"],
        "prompt_sha256": run["prompt_sha256"],
        "invocation_ledger_sha256": run["invocation_ledger_sha256"],
        "invocation_count": len(receipt["invocations"]),
        "output_sha256": run["output_sha256"],
        "receipt_sha256": receipt["receipt_sha256"],
        "packet_count": len(run["packets"]),
        "member_count": len(run["members"]),
        "structural_exact_duplicate_members": run["structural_exact_duplicate_members"],
        "stage_members": {
            stage: sum(evidence["stage_counts"][stage] for evidence in evidences)
            for stage in STAGES
        },
    }


def build_gmail_fact_parity_manifest(
    *,
    labels_bytes: bytes,
    alignment_bytes: bytes,
    completed_units_bytes: bytes,
    work_queue_bytes: bytes,
    evidence: Mapping[str, Any],
    run_evidence: Mapping[str, Any],
    judge_receipt: Mapping[str, Any],
    labels: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the only accepted v5 manifest from fully reconciled artifacts."""

    preparer = _preparer_path()
    if not preparer.is_file():
        raise GmailFactParityError("fact parity preparation tool is unavailable")
    return {
        "version": MANIFEST_VERSION,
        "labels_sha256": _sha256_bytes(labels_bytes),
        "alignment_sha256": _sha256_bytes(alignment_bytes),
        "completed_units_sha256": _sha256_bytes(completed_units_bytes),
        "work_queue_sha256": _sha256_bytes(work_queue_bytes),
        "evaluator_sha256": _sha256(_evaluator_path()),
        "preparer_sha256": _sha256(preparer),
        "cohort_sha256": evidence["cohort_sha256"],
        "packet_sha256": evidence["packet_sha256"],
        "cohort_manifest_sha256": evidence["cohort_manifest_sha256"],
        "admission_join_sha256": evidence["admission_join_sha256"],
        "source_binding_sha256": evidence["source_binding_sha256"],
        "canonical_source_set_sha256": evidence["canonical_source_set_sha256"],
        "original_inventory_sha256": evidence["original_inventory_sha256"],
        "v2_inventory_sha256": evidence["v2_inventory_sha256"],
        "label_unit_count": len(labels),
        "alignment_unit_count": len(labels),
        "unit_packet_count": len({row["packet_id"] for row in labels}),
        "work_item_count": evidence["packet_count"],
        "thread_count": evidence["thread_count"],
        "message_count": evidence["message_count"],
        "packet_count": evidence["packet_count"],
        "original_run": _run_manifest(
            run_evidence["original_run_id"],
            run_evidence=run_evidence,
            labels=labels,
        ),
        "v2_runs": [
            _run_manifest(run_id, run_evidence=run_evidence, labels=labels)
            for run_id in run_evidence["v2_run_ids"]
        ],
        "v2_target_config": dict(run_evidence["v2_target_config"]),
        "original_target_config": dict(run_evidence["original_target_config"]),
        "target_authority": dict(run_evidence["target_authority"]),
        "judge": {
            "provider": judge_receipt["provider"],
            "model": judge_receipt["model"],
            "reasoning_effort": judge_receipt["reasoning_effort"],
            "invocation_id": judge_receipt["invocation_id"],
            "cohort_sha256": judge_receipt["cohort_sha256"],
            "packet_sha256": judge_receipt["packet_sha256"],
            "work_queue_sha256": judge_receipt["work_queue_sha256"],
            "completed_units_sha256": judge_receipt["completed_units_sha256"],
            "judge_contract_version": judge_receipt["judge_contract_version"],
            "judge_contract_sha256": judge_receipt["judge_contract_sha256"],
            "receipt_sha256": judge_receipt["receipt_sha256"],
            "receipt_policy": judge_receipt["attestation"],
        },
        "invocation_attestation": {
            "receipt_policy": INVOCATION_ATTESTATION,
            "distinct_output_files_required": True,
            "distinct_receipt_files_required": True,
            "claimed_invocation_ids_unique": True,
            "cryptographically_verified": False,
        },
    }


def _labeled_member(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _LABELED_MEMBER_KEYS:
        raise GmailFactParityError(f"{name} member schema is invalid")
    critical_error = value.get("critical_error")
    if (
        not isinstance(value.get("supported"), bool)
        or not isinstance(value.get("scope_correct"), bool)
        or critical_error not in CRITICAL_ERRORS
    ):
        raise GmailFactParityError(f"{name} member judgment is invalid")
    return {
        **dict(value),
        "member_id": _opaque_id(value.get("member_id"), "member_id"),
        "stages": _stage_membership(value.get("stages"), name),
    }


def _arm_evidence(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _ARM_KEYS:
        raise GmailFactParityError(f"{name} arm evidence is invalid")
    stage_counts = _stage_counts(value.get("stage_counts"), name)
    raw_members = value.get("members")
    if not isinstance(raw_members, list):
        raise GmailFactParityError(f"{name} members are invalid")
    members = [
        _labeled_member(item, f"{name}.member[{index}]")
        for index, item in enumerate(raw_members)
    ]
    member_ids = [item["member_id"] for item in members]
    if len(member_ids) != len(set(member_ids)):
        raise GmailFactParityError(f"{name} contains duplicate member IDs")
    for stage in STAGES:
        if sum(item["stages"][stage] for item in members) != stage_counts[stage]:
            raise GmailFactParityError(f"{name}.{stage} stage count is unreconciled")
    return {"stage_counts": stage_counts, "members": members}


def _load_labels(path: Path, run_evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = _load_jsonl(path, label="label artifact")
    seen_units: set[str] = set()
    validated: list[dict[str, Any]] = []
    for row in rows:
        if set(row) != _LABEL_KEYS or row.get("version") != LABEL_VERSION:
            raise GmailFactParityError("label schema is invalid")
        unit_id = _opaque_id(row.get("unit_id"), "unit_id")
        if unit_id in seen_units:
            raise GmailFactParityError("label artifact contains duplicate units")
        packet_id = _opaque_id(row.get("packet_id"), "packet_id")
        thread_id = _opaque_id(row.get("thread_id"), "thread_id")
        if (
            not isinstance(row.get("useful"), bool)
            or row.get("classification") not in CLASSIFICATIONS
        ):
            raise GmailFactParityError("label values are invalid")
        raw_v2 = row.get("v2")
        if not isinstance(raw_v2, Mapping) or set(raw_v2) != set(
            run_evidence["v2_run_aliases"]
        ):
            raise GmailFactParityError("label V2 run coverage is incomplete")
        original = _arm_evidence(row.get("original"), f"{unit_id}.original")
        v2 = {
            run_id: _arm_evidence(value, f"{unit_id}.{run_id}")
            for run_id, value in raw_v2.items()
        }
        seen_units.add(unit_id)
        validated.append(
            {
                **dict(row),
                "packet_id": packet_id,
                "thread_id": thread_id,
                "original": original,
                "v2": v2,
            }
        )
    return validated


def _load_alignment(
    path: Path, run_evidence: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows = _load_jsonl(path, label="alignment artifact")
    for row in rows:
        if set(row) != _ALIGNMENT_KEYS or row.get("version") != ALIGNMENT_VERSION:
            raise GmailFactParityError("alignment artifact schema is invalid")
        _opaque_id(row.get("unit_id"), "unit_id")
        _opaque_id(row.get("packet_id"), "packet_id")
        _opaque_id(row.get("thread_id"), "thread_id")
        members = row.get("members")
        if not isinstance(members, Mapping) or set(members) != set(
            run_evidence["all_run_aliases"]
        ):
            raise GmailFactParityError("alignment run coverage is incomplete")
        for values in members.values():
            if (
                not isinstance(values, list)
                or any(
                    _OPAQUE_PATTERNS["member_id"].fullmatch(str(item)) is None
                    for item in values
                )
                or len(values) != len(set(values))
            ):
                raise GmailFactParityError("alignment member IDs are invalid")
    return rows


def _member_good(member: Mapping[str, Any]) -> bool:
    return bool(
        member["supported"]
        and member["scope_correct"]
        and member["critical_error"] == "none"
    )


def _fact_member_good(row: Mapping[str, Any], member: Mapping[str, Any]) -> bool:
    return bool(row["classification"] != "not_fact" and _member_good(member))


def _members_at_stage(evidence: Mapping[str, Any], stage: str) -> list[dict[str, Any]]:
    return [member for member in evidence["members"] if member["stages"][stage]]


def _has_good_evidence(
    row: Mapping[str, Any], evidence: Mapping[str, Any], stage: str
) -> bool:
    return any(
        _fact_member_good(row, member) for member in _members_at_stage(evidence, stage)
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _public_model(value: str) -> str:
    return value if value in {EXPECTED_V2_MODEL, EXPECTED_JUDGE_MODEL} else "other"


def _public_effort(value: str) -> str:
    return value if value in {"low", "medium", "high", "xhigh"} else "other"


def evaluate_gmail_fact_parity(
    labels_path: Path,
    manifest_path: Path,
    alignment_path: Path,
    completed_units_path: Path,
    judge_receipt_path: Path,
    work_queue_path: Path,
    cohort_path: Path,
    packets_path: Path,
    admissions_path: Path,
    cohort_manifest_path: Path,
    source_bindings_path: Path,
    hmac_key_path: Path,
    original_inventory_path: Path,
    v2_inventory_path: Path,
    run_output_paths: Mapping[str, Path],
    run_receipt_paths: Mapping[str, Path],
) -> dict[str, Any]:
    """Validate complete evidence and score opaque semantic-unit parity."""

    artifacts = {
        "labels": labels_path,
        "manifest": manifest_path,
        "alignment": alignment_path,
        "completed_units": completed_units_path,
        "judge_receipt": judge_receipt_path,
        "work_queue": work_queue_path,
        "cohort": cohort_path,
        "packets": packets_path,
        "admissions": admissions_path,
        "cohort_manifest": cohort_manifest_path,
        "source_bindings": source_bindings_path,
        "hmac_key": hmac_key_path,
        "original_inventory": original_inventory_path,
        "v2_inventory": v2_inventory_path,
        **{f"output:{key}": Path(value) for key, value in run_output_paths.items()},
        **{f"receipt:{key}": Path(value) for key, value in run_receipt_paths.items()},
    }
    validate_private_artifact_set(artifacts)
    evidence = load_gmail_fact_parity_bound_evidence(
        packets_path,
        cohort_path,
        admissions_path,
        cohort_manifest_path,
        source_bindings_path,
        hmac_key_path,
        original_inventory_path,
        v2_inventory_path,
    )
    run_evidence = load_gmail_fact_parity_runs(
        run_output_paths, run_receipt_paths, evidence
    )
    expected_queue = build_gmail_fact_parity_work_queue(evidence, run_evidence)
    _load_and_verify_work_queue(work_queue_path, expected=expected_queue)
    completed_units = load_gmail_fact_parity_completed_units(
        completed_units_path,
        evidence=evidence,
        run_evidence=run_evidence,
    )
    judge_receipt = load_gmail_fact_parity_judge_receipt(
        judge_receipt_path,
        completed_units_sha256=_sha256(completed_units_path),
        work_queue_sha256=_sha256(work_queue_path),
        evidence=evidence,
        run_evidence=run_evidence,
    )
    expected_alignment, expected_labels = derive_gmail_fact_parity_units(
        completed_units, run_evidence=run_evidence
    )
    alignment = _load_alignment(alignment_path, run_evidence)
    labels = _load_labels(labels_path, run_evidence)
    if alignment != expected_alignment:
        raise GmailFactParityError(
            "alignment does not cover the completed units exactly"
        )
    if labels != expected_labels:
        raise GmailFactParityError("labels do not cover every aligned unit exactly")

    labels_bytes = labels_path.read_bytes()
    alignment_bytes = alignment_path.read_bytes()
    completed_bytes = completed_units_path.read_bytes()
    queue_bytes = work_queue_path.read_bytes()
    expected_manifest = build_gmail_fact_parity_manifest(
        labels_bytes=labels_bytes,
        alignment_bytes=alignment_bytes,
        completed_units_bytes=completed_bytes,
        work_queue_bytes=queue_bytes,
        evidence=evidence,
        run_evidence=run_evidence,
        judge_receipt=judge_receipt,
        labels=labels,
    )
    manifest = _load_json(manifest_path, label="evaluation manifest")
    if manifest.get("version") != MANIFEST_VERSION or manifest != expected_manifest:
        raise GmailFactParityError("evaluation manifest is stale or incomplete")

    reference_denominators = {
        stage: [
            row
            for row in labels
            if row["classification"] == "non_temporal"
            and _has_good_evidence(row, row["original"], stage)
        ]
        for stage in STAGES
    }
    useful_denominators = {
        stage: [row for row in rows if row["useful"]]
        for stage, rows in reference_denominators.items()
    }
    reference_thread_counts = {
        stage: len({row["thread_id"] for row in rows})
        for stage, rows in reference_denominators.items()
    }
    coverage_checks = {
        "minimum_packets": evidence["packet_count"] >= MIN_COHORT_PACKETS,
        "minimum_threads": evidence["thread_count"] >= MIN_COHORT_THREADS,
        "minimum_messages": evidence["message_count"] >= MIN_COHORT_MESSAGES,
        **{
            f"minimum_{stage}_reference_units": len(reference_denominators[stage])
            >= MIN_REFERENCE_UNITS_PER_STAGE
            for stage in STAGES
        },
        **{
            f"minimum_{stage}_reference_threads": reference_thread_counts[stage]
            >= MIN_REFERENCE_THREADS_PER_STAGE
            for stage in STAGES
        },
    }
    coverage_gate = {
        "minimums": {
            "packets": MIN_COHORT_PACKETS,
            "threads": MIN_COHORT_THREADS,
            "messages": MIN_COHORT_MESSAGES,
            "reference_units_per_stage": MIN_REFERENCE_UNITS_PER_STAGE,
            "reference_threads_per_stage": MIN_REFERENCE_THREADS_PER_STAGE,
        },
        "observed": {
            "packets": evidence["packet_count"],
            "threads": evidence["thread_count"],
            "messages": evidence["message_count"],
            "reference_units": {
                stage: len(reference_denominators[stage]) for stage in STAGES
            },
            "reference_threads": reference_thread_counts,
            "useful_reference_units_diagnostic": {
                stage: len(useful_denominators[stage]) for stage in STAGES
            },
        },
        "checks": coverage_checks,
        "passed": all(coverage_checks.values()),
    }

    run_results: dict[str, Any] = {}
    presence_sets: dict[str, dict[str, set[str]]] = {}
    for run_id in run_evidence["v2_run_ids"]:
        run_alias = run_evidence["run_aliases"][run_id]
        stage_results: dict[str, Any] = {}
        presence_sets[run_id] = {}
        for stage in STAGES:
            present_rows = [
                row for row in labels if row["v2"][run_alias]["stage_counts"][stage] > 0
            ]
            presence_sets[run_id][stage] = {row["unit_id"] for row in present_rows}
            present_members = [
                (row, member)
                for row in labels
                for member in _members_at_stage(row["v2"][run_alias], stage)
            ]
            good_members = [
                member
                for row, member in present_members
                if _fact_member_good(row, member)
            ]
            retained = sum(
                _has_good_evidence(row, row["v2"][run_alias], stage)
                for row in reference_denominators[stage]
            )
            raw_retained = sum(
                row["v2"][run_alias]["stage_counts"][stage] > 0
                for row in reference_denominators[stage]
            )
            useful_retained = sum(
                _has_good_evidence(row, row["v2"][run_alias], stage)
                for row in useful_denominators[stage]
            )
            duplicate_members = sum(
                max(0, row["v2"][run_alias]["stage_counts"][stage] - 1)
                for row in labels
            )
            critical_members = sum(
                member["critical_error"] != "none" for _, member in present_members
            )
            critical_units = sum(
                any(
                    member["critical_error"] != "none"
                    for member in _members_at_stage(row["v2"][run_alias], stage)
                )
                for row in labels
            )
            stage_by_thread: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in reference_denominators[stage]:
                stage_by_thread[row["thread_id"]].append(row)
            stage_thread_recalls = [
                sum(
                    _has_good_evidence(row, row["v2"][run_alias], stage) for row in rows
                )
                / len(rows)
                for rows in stage_by_thread.values()
            ]
            retained_threads = sum(value > 0 for value in stage_thread_recalls)
            stage_results[stage] = {
                "original_non_temporal_units": len(reference_denominators[stage]),
                "original_non_temporal_threads": reference_thread_counts[stage],
                "original_useful_non_temporal_units": len(useful_denominators[stage]),
                "raw_present_units": raw_retained,
                "retained_units": retained,
                "retention": _ratio(retained, len(reference_denominators[stage])),
                "retained_threads": retained_threads,
                "thread_retention": _ratio(retained_threads, len(stage_thread_recalls)),
                "macro_thread_retention": (
                    sum(stage_thread_recalls) / len(stage_thread_recalls)
                    if stage_thread_recalls
                    else None
                ),
                "useful_retained_units": useful_retained,
                "useful_retention_diagnostic": _ratio(
                    useful_retained, len(useful_denominators[stage])
                ),
                "v2_units": len(present_rows),
                "v2_members": len(present_members),
                "supported_scope_correct_members": len(good_members),
                "precision": _ratio(len(good_members), len(present_members)),
                "duplicate_members": duplicate_members,
                "critical_error_members": critical_members,
                "critical_error_units": critical_units,
            }

        all_members = [
            (row, member)
            for row in labels
            for member in row["v2"][run_alias]["members"]
        ]
        critical_members = sum(
            member["critical_error"] != "none" for _, member in all_members
        )
        critical_units = sum(
            any(
                member["critical_error"] != "none"
                for member in row["v2"][run_alias]["members"]
            )
            for row in labels
        )
        candidate_stage = stage_results["candidate"]
        reference_thread_coverage = candidate_stage["retained_threads"]
        structural_duplicates = run_evidence["runs"][run_id][
            "structural_exact_duplicate_members"
        ]
        run_results[run_alias] = {
            "execution": {
                "provider": run_evidence["receipts"][run_id]["provider"],
                "model": _public_model(run_evidence["runs"][run_id]["model"]),
                "reasoning_effort": _public_effort(
                    run_evidence["runs"][run_id]["reasoning_effort"]
                ),
                "attestation": run_evidence["receipts"][run_id]["attestation"],
            },
            "stages": stage_results,
            "reference_thread_count": reference_thread_counts["candidate"],
            "reference_thread_coverage": reference_thread_coverage,
            "reference_thread_coverage_rate": _ratio(
                reference_thread_coverage, reference_thread_counts["candidate"]
            ),
            "macro_candidate_recall": candidate_stage["macro_thread_retention"],
            "critical_error_members": critical_members,
            "critical_error_units": critical_units,
            "structural_exact_duplicate_members": structural_duplicates,
            "gates": {
                **{
                    f"{stage}_retention": value["retention"] is not None
                    and value["retention"] >= MIN_RETENTION
                    for stage, value in stage_results.items()
                },
                **{
                    f"{stage}_thread_retention": value["thread_retention"] is not None
                    and value["thread_retention"] >= MIN_RETENTION
                    for stage, value in stage_results.items()
                },
                **{
                    f"{stage}_macro_thread_retention": value["macro_thread_retention"]
                    is not None
                    and value["macro_thread_retention"] >= MIN_RETENTION
                    for stage, value in stage_results.items()
                },
                **{
                    f"{stage}_precision": value["precision"] is not None
                    and value["precision"] >= MIN_PRECISION
                    for stage, value in stage_results.items()
                },
                "no_critical_errors": critical_members == 0,
                "no_duplicate_members": all(
                    value["duplicate_members"] == 0 for value in stage_results.values()
                )
                and structural_duplicates == 0,
            },
        }

    agreement_pairs: list[dict[str, Any]] = []
    ordered_runs = run_evidence["v2_run_ids"]
    for index, first in enumerate(ordered_runs):
        for second in ordered_runs[index + 1 :]:
            for stage in STAGES:
                intersection = (
                    presence_sets[first][stage] & presence_sets[second][stage]
                )
                union = presence_sets[first][stage] | presence_sets[second][stage]
                rate = _ratio(len(intersection), len(union))
                agreement_pairs.append(
                    {
                        "first": run_evidence["run_aliases"][first],
                        "second": run_evidence["run_aliases"][second],
                        "stage": stage,
                        "intersection_units": len(intersection),
                        "union_units": len(union),
                        "agreement": rate,
                        "passed": rate is not None and rate >= MIN_RUN_AGREEMENT,
                    }
                )

    stability: list[dict[str, Any]] = []
    for stage in STAGES:
        stage_sets = [presence_sets[run_id][stage] for run_id in ordered_runs]
        union = set().union(*stage_sets)
        intersection = set.intersection(*stage_sets)
        rate = _ratio(len(intersection), len(union))
        stability.append(
            {
                "stage": stage,
                "all_run_intersection_units": len(intersection),
                "all_run_union_units": len(union),
                "intersection_over_union": rate,
                "passed": rate is not None and rate >= MIN_RUN_AGREEMENT,
            }
        )

    all_run_gates = all(
        all(result["gates"].values()) for result in run_results.values()
    )
    metric_gate_passed = (
        all_run_gates
        and coverage_gate["passed"]
        and all(item["passed"] for item in stability)
    )
    return {
        "version": VERSION,
        "manifest_version": manifest["version"],
        "provenance": {
            "manifest_sha256": _sha256(manifest_path),
            "labels_sha256": manifest["labels_sha256"],
            "alignment_sha256": manifest["alignment_sha256"],
            "completed_units_sha256": manifest["completed_units_sha256"],
            "judge_receipt_sha256": manifest["judge"]["receipt_sha256"],
            "work_queue_sha256": manifest["work_queue_sha256"],
            "evaluator_sha256": manifest["evaluator_sha256"],
            "preparer_sha256": manifest["preparer_sha256"],
            "cohort_sha256": manifest["cohort_sha256"],
            "packet_sha256": manifest["packet_sha256"],
            "cohort_manifest_sha256": manifest["cohort_manifest_sha256"],
            "admission_join_sha256": evidence["admission_join_sha256"],
            "source_binding_sha256": evidence["source_binding_sha256"],
            "canonical_source_set_sha256": evidence["canonical_source_set_sha256"],
            "original_inventory_sha256": evidence["original_inventory_sha256"],
            "v2_inventory_sha256": evidence["v2_inventory_sha256"],
            "run_output_sha256": {
                run_evidence["run_aliases"][run_id]: run_evidence["runs"][run_id][
                    "output_sha256"
                ]
                for run_id in run_evidence["all_run_ids"]
            },
            "run_receipt_sha256": {
                run_evidence["run_aliases"][run_id]: run_evidence["receipts"][run_id][
                    "receipt_sha256"
                ]
                for run_id in run_evidence["all_run_ids"]
            },
        },
        "cohort": {
            "threads": evidence["thread_count"],
            "messages": evidence["message_count"],
            "packets": evidence["packet_count"],
            "labeled_units": len(labels),
            "labeled_packets": len({row["packet_id"] for row in labels}),
        },
        "coverage_gate": coverage_gate,
        "v2_target_config": dict(run_evidence["v2_target_config"]),
        "target_authority_gate": dict(run_evidence["target_authority"]),
        "runs": run_results,
        "evidence_execution": {
            run_evidence["run_aliases"][run_id]: {
                "arm": run_evidence["runs"][run_id]["arm"],
                "provider": run_evidence["receipts"][run_id]["provider"],
                "model": _public_model(run_evidence["runs"][run_id]["model"]),
                "reasoning_effort": _public_effort(
                    run_evidence["runs"][run_id]["reasoning_effort"]
                ),
                "attestation": run_evidence["receipts"][run_id]["attestation"],
            }
            for run_id in run_evidence["all_run_ids"]
        },
        "run_agreement": agreement_pairs,
        "all_v2_stability": stability,
        "invocation_attestation": {
            "claimed_invocation_ids_unique": True,
            "distinct_evidence_files_verified": True,
            "independent_invocations_verified": False,
            "limitation": INVOCATION_ATTESTATION,
        },
        "judge": {
            "provider": judge_receipt["provider"],
            "model": judge_receipt["model"],
            "reasoning_effort": judge_receipt["reasoning_effort"],
            "contract_version": judge_receipt["judge_contract_version"],
            "contract_sha256": judge_receipt["judge_contract_sha256"],
            "attestation": judge_receipt["attestation"],
        },
        "metric_gate_passed": metric_gate_passed,
        "gate_passed": metric_gate_passed
        and run_evidence["target_authority"]["passed"]
        and run_evidence["independent_invocations_verified"],
        "private_content_printed": False,
    }


def _run_artifact_arguments(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise GmailFactParityError(
                "run artifact arguments must use RUN_ID=PATH syntax"
            )
        run_id, raw_path = value.split("=", 1)
        if _RUN_ID_RE.fullmatch(run_id) is None or not raw_path or run_id in result:
            raise GmailFactParityError("run artifact argument is invalid")
        result[run_id] = Path(raw_path)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("labels", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("alignment", type=Path)
    parser.add_argument("completed_units", type=Path)
    parser.add_argument("judge_receipt", type=Path)
    parser.add_argument("work_queue", type=Path)
    parser.add_argument("cohort", type=Path)
    parser.add_argument("packets", type=Path)
    parser.add_argument("admissions", type=Path)
    parser.add_argument("cohort_manifest", type=Path)
    parser.add_argument("source_bindings", type=Path)
    parser.add_argument("original_inventory", type=Path)
    parser.add_argument("v2_inventory", type=Path)
    parser.add_argument("--hmac-key", type=Path, required=True)
    parser.add_argument(
        "--run-output",
        action="append",
        default=[],
        metavar="RUN_ID=PATH",
        help="private output artifact for one run (repeat for every run)",
    )
    parser.add_argument(
        "--run-receipt",
        action="append",
        default=[],
        metavar="RUN_ID=PATH",
        help="private self-reported invocation receipt (repeat for every run)",
    )
    args = parser.parse_args()
    result = evaluate_gmail_fact_parity(
        args.labels,
        args.manifest,
        args.alignment,
        args.completed_units,
        args.judge_receipt,
        args.work_queue,
        args.cohort,
        args.packets,
        args.admissions,
        args.cohort_manifest,
        args.source_bindings,
        args.hmac_key,
        args.original_inventory,
        args.v2_inventory,
        _run_artifact_arguments(args.run_output),
        _run_artifact_arguments(args.run_receipt),
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    if not result["gate_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
