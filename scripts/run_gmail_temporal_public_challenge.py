#!/usr/bin/env python3
"""Run a sealed public-synthetic Gmail temporal smoke through production V2.

This utility is deliberately separate from the private Gmail holdout authority.
``run`` accepts no gold path, uses the production restricted external-Codex
boundary, and seals every prediction before it mutates the isolated Brain home.
``score`` is a later, separate operation that may open the committed public gold.

The command is diagnostic-only.  It cannot establish private-distribution or
release evidence and every artifact it writes is owner-only.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import math
import os
import re
import secrets
import stat
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from pkm_brain import gmail_temporal_review as production_review
from pkm_brain.db import connection
from pkm_brain.gmail_temporal_runner import (
    GMAIL_TEMPORAL_COMPONENT_VERSION,
    GMAIL_TEMPORAL_EXTERNAL_PROVIDER,
    GMAIL_TEMPORAL_PIPELINE_SCOPE,
    GMAIL_TEMPORAL_RUNNER_VERSION,
    GMAIL_TEMPORAL_VERIFIER_MODEL,
    GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
    GmailTemporalReviewPreparation,
    GmailTemporalRunnerError,
    gmail_temporal_admission_policy_fingerprint,
    gmail_temporal_runner_policy_fingerprint,
    gmail_temporal_verifier_policy_fingerprint,
    prepare_gmail_temporal_review,
    run_gmail_temporal_review,
)
from pkm_brain.paths import BrainPaths


VERSION = "gmail_temporal_public_challenge_launcher_v3"
CHALLENGE_VERSION = "gmail_temporal_public_challenge_v3"
LEGACY_CHALLENGE_VERSION = "gmail_temporal_public_challenge_v2"
PLAN_VERSION = "gmail_temporal_public_challenge_plan_v3"
CALL_START_VERSION = "gmail_temporal_public_challenge_call_start_v3"
CALL_RECEIPT_VERSION = "gmail_temporal_public_challenge_call_receipt_v3"
PREDICTION_SEAL_VERSION = "gmail_temporal_public_challenge_prediction_seal_v3"
RESULT_VERSION = "gmail_temporal_public_challenge_result_v3"
SCORE_VERSION = "gmail_temporal_public_challenge_score_v13"
GOLD_VERSION = "public_blind_gmail_temporal_gold_v4"
LEGACY_STRUCTURED_GOLD_VERSION = "public_blind_gmail_temporal_gold_v3"
LEGACY_GOLD_VERSION = "public_blind_gmail_temporal_gold_v2"
PUBLIC_ROOT_AUTHORITY_VERSION = "gmail_temporal_public_root_authority_v2"
PUBLIC_ROOT_AUTHORITY_FILENAME = "public_temporal_challenge_authority.json"
FRONTIER_DIAGNOSTICS_VERSION = "gmail_temporal_public_frontier_diagnostics_v1"
FRONTIER_DIAGNOSTICS_FILENAME = "frontier-diagnostics.json"
GOLD_AUDIT_SUMMARY_VERSION = "gmail_temporal_public_gold_audit_summary_v1"
GOLD_AUDIT_PLAN_VERSION = "gmail_temporal_public_gold_audit_plan_v1"
GOLD_AUDIT_RECEIPT_VERSION = "gmail_temporal_public_gold_audit_receipt_v1"
GOLD_AUDIT_DETAIL_VERSION = "gmail_temporal_public_gold_audit_detail_v1"
GOLD_AUDIT_REQUEST_VERSION = "gmail_temporal_public_gold_audit_request_v1"
GOLD_AUDIT_RESPONSE_VERSION = "gmail_temporal_public_gold_audit_response_v1"
GOLD_AUDIT_SCOPE = "public_synthetic_prediction_blind_gold_audit"
GOLD_AUDIT_PROVIDER = "external-codex"
GOLD_AUDIT_MODEL = "gpt-5.6-sol"
GOLD_AUDIT_REASONING_EFFORT = "medium"
GOLD_AUDIT_FIXTURE_VERSION = "gmail_temporal_public_challenge_fixture_v3"
GOLD_AUDIT_FIXTURE_GENERATOR_VERSION = "gmail_temporal_public_scale_fixture_builder_v1"
GOLD_AUDIT_FIXTURE_GENERATOR_SHA256 = (
    "75e379814d68e95a4f951602083c816aa37f55b4fedfcf8820fa7c4eb7da5d11"
)
GOLD_AUDIT_APPROVED_FIXTURE_SHA256 = {
    1: "e67075ea3be61de904b78305b452adcc90df9a9a45058fb9340df798fa1566ac",
    2: "473bd0a0a691c72b112235d3e882bc7a80aacb0e9184cef18782735227eb1653",
}
GOLD_AUDIT_CONTRACT_SHA256 = (
    "d11f3f2893e0598f470d3a0a3539ee7c9d5a0babf0130c71fbf374500ff6990f"
)
GOLD_AUDIT_MAX_BATCH_CASES = 4
GOLD_AUDIT_MAX_REQUEST_BYTES = 48_000
GOLD_AUDIT_MAX_RESPONSE_BYTES = 32_768
PUBLIC_TEST_PROVIDER = "injected-test-double"
PUBLIC_NO_CALL_PROVIDER = "none"

PUBLIC_SCOPE = "public_synthetic_non_release"
RUN_COUNT = 3
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
DEFAULT_TIMEOUT_SECONDS = 900
MAX_TIMEOUT_SECONDS = 1800

PLAN_DOMAIN = b"gmail_temporal_public_challenge_plan_v3\0"
CALL_START_DOMAIN = b"gmail_temporal_public_challenge_call_start_v3\0"
CALL_RECEIPT_DOMAIN = b"gmail_temporal_public_challenge_call_receipt_v3\0"
PREDICTION_SEAL_DOMAIN = b"gmail_temporal_public_challenge_prediction_seal_v3\0"
RESULT_DOMAIN = b"gmail_temporal_public_challenge_result_v3\0"
SCORE_DOMAIN = b"gmail_temporal_public_challenge_score_v13\0"
PUBLIC_ROOT_AUTHORITY_DOMAIN = b"gmail_temporal_public_root_authority_v2\0"
FRONTIER_DIAGNOSTICS_DOMAIN = b"gmail_temporal_public_frontier_diagnostics_v1\0"
GOLD_AUDIT_SUMMARY_DOMAIN = b"gmail_temporal_public_gold_audit_summary_v1\0"
GOLD_AUDIT_PLAN_DOMAIN = b"gmail_temporal_public_gold_audit_plan_v1\0"
GOLD_AUDIT_RECEIPT_DOMAIN = b"gmail_temporal_public_gold_audit_receipt_v1\0"
GOLD_AUDIT_DETAIL_DOMAIN = b"gmail_temporal_public_gold_audit_detail_v1\0"
MIN_PAIRWISE_STABILITY = 0.95
MIN_FRONTIER_MEMBER_RECALL = 0.95
MIN_EFFECTIVE_MEMBER_RECALL = 0.90
MIN_CONFIRMED_MEMBER_RECALL = 0.90
MIN_COMPLETE_UNIT_RECALL = 0.90
MIN_EXACT_UNIT_RECALL = 0.90
MIN_CRITICAL_LIFECYCLE_EFFECTIVE_RECALL = 0.95
MIN_CRITICAL_TEMPORAL_EFFECTIVE_RECALL = 0.95
MIN_CRITICAL_TEMPORAL_CATEGORY_RECALL = 0.95
MIN_CANONICAL_TITLE_RECALL = 0.90
MIN_CANONICAL_SUBJECT_RECALL = 0.90
MIN_SUPPORTED_ARTIFACT_PRECISION = 0.95
MIN_REVIEW_ARTIFACT_PRECISION = 0.90
MIN_CRITICAL_VERDICT_STABILITY = 0.95
MIN_CANDIDATE_BEARING_NEGATIVE_REJECTION = 0.80
MAX_PREDICTION_LAUNCHER_ARTIFACT_BYTES = 4 * 1024 * 1024

_ROOT = Path(__file__).resolve().parents[1]
_HOLDOUT_RUNNER_PATH = _ROOT / "scripts" / "run_gmail_temporal_holdout_external.py"
_RUNNER_PATH = _ROOT / "src" / "pkm_brain" / "gmail_temporal_runner.py"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_CHALLENGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CALL_ID_RE = re.compile(r"^gtpvc_(?:test_)?i_[0-9a-f]{64}$")
_LOGICAL_RUN_ID_RE = re.compile(r"^gtpvc_(?:test_)?r_[0-9a-f]{64}$")
_RUNNER_POLICY_RE = re.compile(r"^gtrun_[0-9a-f]{64}$")
_TARGET_FINGERPRINT_RE = re.compile(r"^gtrt_[0-9a-f]{64}$")
_REQUEST_FINGERPRINT_RE = re.compile(r"^gtrq_[0-9a-f]{64}$")
_CANDIDATE_ID_RE = re.compile(r"^gtvc_[0-9a-f]{32}$")

_CHALLENGE_KEYS = {
    "version",
    "challenge_id",
    "scope",
    "created_at",
    "brain_home",
    "gold_sha256",
    "public_synthetic",
    "contains_private_gmail",
    "release_eligible",
    "cases",
}
_CASE_KEYS = {"case_id", "document_id", "gmail_message_id", "source_sha256"}
_PUBLIC_ROOT_AUTHORITY_KEYS = {
    "version",
    "challenge_id",
    "scope",
    "created_at",
    "brain_home",
    "challenge_manifest_sha256",
    "gold_sha256",
    "public_synthetic",
    "contains_private_gmail",
    "release_eligible",
    "cases",
    "authority_hmac_sha256",
}
_FRONTIER_DIAGNOSTICS_KEYS = {
    "version",
    "challenge_id",
    "challenge_manifest_sha256",
    "gold_sha256",
    "fixture_sha256",
    "aggregates",
    "cases",
    "public_synthetic",
    "contains_private_gmail",
    "release_eligible",
    "frontier_diagnostics_hmac_sha256",
}
_FRONTIER_DIAGNOSTICS_AGGREGATE_KEYS = {
    "cases",
    "positive_cases",
    "negative_cases",
    "gold_members",
    "frontier_covered_gold_members",
    "frontier_missing_gold_members",
    "positive_zero_work_cases",
    "candidate_bearing_positive_cases",
    "candidate_bearing_negative_cases",
}
_FRONTIER_DIAGNOSTICS_CASE_KEYS = {
    "case_id",
    "gold_members",
    "frontier_covered_gold_members",
    "frontier_missing_gold_members",
    "positive",
    "candidate_count",
    "candidate_bearing",
    "verifier_request_count",
    "zero_work",
    "positive_zero_work",
}
_GOLD_AUDIT_SUMMARY_KEYS = {
    "version",
    "status",
    "created_at",
    "scope",
    "fixture_sha256",
    "fixture_variant",
    "fixture_generator_version",
    "fixture_generator_sha256",
    "fixture_generator_exact_bytes_verified",
    "plan_sha256",
    "detail_sha256",
    "batch_count",
    "provider",
    "model",
    "reasoning_effort",
    "external_calls",
    "restricted_execution",
    "ephemeral_execution",
    "local_model_used",
    "test_invoker_used",
    "public_synthetic",
    "contains_private_gmail",
    "pipeline_predictions_present",
    "prediction_artifacts_read",
    "private_content_printed",
    "diagnostic_only",
    "release_eligible",
    "case_count",
    "valid_case_count",
    "correction_case_count",
    "member_count",
    "valid_member_count",
    "correction_member_count",
    "forbidden_binding_count",
    "valid_forbidden_binding_count",
    "correction_forbidden_binding_count",
    "valid_group_flag_count",
    "correction_group_flag_count",
    "summary_hmac_sha256",
}
_GOLD_AUDIT_PLAN_KEYS = {
    "version",
    "created_at",
    "scope",
    "fixture_version",
    "fixture_variant",
    "fixture_sha256",
    "fixture_generator_version",
    "fixture_generator_sha256",
    "fixture_generator_exact_bytes_verified",
    "case_count",
    "batch_count",
    "request_sha256",
    "provider",
    "model",
    "reasoning_effort",
    "public_synthetic",
    "contains_private_gmail",
    "pipeline_predictions_present",
    "prediction_artifacts_read",
    "diagnostic_only",
    "release_eligible",
    "plan_hmac_sha256",
}
_GOLD_AUDIT_DETAIL_KEYS = {
    "version",
    "status",
    "created_at",
    "scope",
    "fixture_sha256",
    "fixture_variant",
    "fixture_generator_version",
    "fixture_generator_sha256",
    "fixture_generator_exact_bytes_verified",
    "plan_sha256",
    "provider",
    "model",
    "reasoning_effort",
    "public_synthetic",
    "contains_private_gmail",
    "pipeline_predictions_present",
    "prediction_artifacts_read",
    "diagnostic_only",
    "release_eligible",
    "calls",
    "cases",
    "aggregates",
    "detail_hmac_sha256",
}
_GOLD_AUDIT_RECEIPT_KEYS = {
    "version",
    "unit_ordinal",
    "started_at",
    "completed_at",
    "provider",
    "model",
    "reasoning_effort",
    "request_sha256",
    "response_sha256",
    "case_count",
    "public_synthetic",
    "contains_private_gmail",
    "pipeline_predictions_present",
    "restricted_execution",
    "ephemeral_execution",
    "local_model_used",
    "test_invoker_used",
    "receipt_hmac_sha256",
}
_GOLD_AUDIT_DETAIL_CALL_KEYS = {
    "unit_ordinal",
    "request_sha256",
    "response_sha256",
    "receipt_sha256",
    "case_count",
}
_GOLD_AUDIT_REQUEST_KEYS = {
    "version",
    "phase",
    "contract",
    "challenge_id",
    "fixture_created_at",
    "message_internal_at",
    "account_email",
    "public_synthetic",
    "contains_private_gmail",
    "pipeline_predictions_present",
    "cases",
}
_GOLD_AUDIT_REQUEST_CASE_KEYS = {"case_id", "source", "proposed_gold"}
_GOLD_AUDIT_REQUEST_SOURCE_KEYS = {"sender", "subject", "body", "label_ids"}
_GOLD_AUDIT_REQUEST_GOLD_KEYS = {
    "members",
    "forbidden",
    "complete_group_required",
}
_GOLD_AUDIT_RESPONSE_KEYS = {"version", "cases"}
_GOLD_AUDIT_RESPONSE_CASE_KEYS = {
    "case_id",
    "disposition",
    "issue_codes",
    "rationale",
    "members",
    "forbidden_bindings",
    "group_flag",
}
_GOLD_AUDIT_DISPOSITION_KEYS = {"disposition", "issue_codes", "rationale"}
_GOLD_AUDIT_ISSUE_CODES = frozenset(
    {
        "none",
        "unsupported_member",
        "missing_member",
        "wrong_subject",
        "wrong_relation",
        "wrong_lifecycle",
        "wrong_value",
        "wrong_verdict",
        "wrong_canonical_requirement",
        "wrong_forbidden_binding",
        "wrong_group_requirement",
        "irrelevant_temporal_content",
        "other",
    }
)
_GOLD_AUDIT_FIXTURE_KEYS = {
    "version",
    "challenge_id",
    "created_at",
    "message_internal_at",
    "account_email",
    "public_synthetic",
    "contains_private_gmail",
    "release_eligible",
    "cases",
}
_GOLD_AUDIT_FIXTURE_CASE_KEYS = {
    "case_id",
    "sender",
    "subject",
    "body",
    "label_ids",
    "members",
    "forbidden",
    "complete_group_required",
}
_LIFECYCLE_ROLES = (
    "none",
    "unknown",
    "scheduled",
    "cancelled",
    "completed",
    "rescheduled_old",
    "rescheduled_replacement",
)
_CRITICAL_LIFECYCLE_ROLES = (
    "scheduled",
    "cancelled",
    "rescheduled_old",
    "rescheduled_replacement",
)
_TEMPORAL_RELATIONS = (
    "occurrence",
    "deadline",
    "unspecified",
)
_CRITICAL_TEMPORAL_CATEGORIES = (
    "scheduled",
    "cancelled",
    "rescheduled_old",
    "rescheduled_replacement",
    "deadline",
)
_NORMALIZED_TEMPORAL_VALUE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?$"
)


class PublicChallengeError(ValueError):
    """Raised without reflecting source or model content."""


ModelInvoker = Callable[
    [Mapping[str, Any], Mapping[str, Any], str, str, int], Mapping[str, Any]
]


@dataclass(frozen=True)
class _Case:
    case_id: str
    document_id: str
    gmail_message_id: str
    preparation: GmailTemporalReviewPreparation


@dataclass(frozen=True)
class _RequestRow:
    case_id: str
    request_fingerprint: str
    payload: Mapping[str, Any]
    batch_fingerprint: str
    frontier_fingerprint: str
    page_plan_fingerprint: str
    page_fingerprint: str
    candidate_ids: tuple[str, ...]


@dataclass(frozen=True)
class _CallUnit:
    unit_ordinal: int
    case_ids: tuple[str, ...]
    rows: tuple[_RequestRow, ...]
    request: Mapping[str, Any]
    request_sha256: str


@dataclass(frozen=True)
class _CompletedCall:
    run_ordinal: int
    unit_ordinal: int
    call_id: str
    logical_run_id: str
    started_at: str
    completed_at: str
    request_sha256: str
    response_sha256: str
    start_sha256: str
    receipt_sha256: str
    response: Mapping[str, Any]
    case_pages: Mapping[str, tuple[Mapping[str, Any], ...]]


@dataclass(frozen=True)
class _ExecutionClaims:
    provider: str
    external_call_started: bool
    restricted_execution: bool
    ephemeral_execution: bool
    local_model_used: bool
    test_invoker_used: bool


@dataclass(frozen=True)
class _PredictionSourceProvenance:
    launcher_sha256: str
    trust_basis: str
    exact_artifact_verified: bool
    scorer_sha256: str


@dataclass(frozen=True)
class _PublicCandidateAuthority:
    candidate: Any
    subject_alias_surfaces: frozenset[str]
    canonical_subject_surface: str | None
    parent_cluster_id: str


def _load_script(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PublicChallengeError("shared external runner is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(name, None)
        raise PublicChallengeError(
            "shared external runner could not be loaded"
        ) from exc
    return module


external = _load_script("_gmail_temporal_public_shared_external", _HOLDOUT_RUNNER_PATH)


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PublicChallengeError("artifact is not canonical JSON") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _aware_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 80:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _private_directory(path: Path, *, create: bool = False) -> None:
    if create and not path.exists() and not path.is_symlink():
        missing: list[Path] = []
        cursor = path
        while not cursor.exists() and not cursor.is_symlink():
            missing.append(cursor)
            cursor = cursor.parent
        if cursor.is_symlink() or not cursor.is_dir():
            raise PublicChallengeError("owner-only directory parent is unsafe")
        for item in reversed(missing):
            os.mkdir(item, PRIVATE_DIRECTORY_MODE)
            item.chmod(PRIVATE_DIRECTORY_MODE)
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise PublicChallengeError("owner-only directory is unavailable") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != PRIVATE_DIRECTORY_MODE
    ):
        raise PublicChallengeError("directory must be owner-only and non-symlinked")


def _fresh_private_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise PublicChallengeError("fresh output directory is required")
    parent = path.parent
    if not parent.exists():
        parent.mkdir(parents=True, mode=PRIVATE_DIRECTORY_MODE)
    if parent.is_symlink() or not parent.is_dir():
        raise PublicChallengeError("output parent is unsafe")
    os.mkdir(path, PRIVATE_DIRECTORY_MODE)
    _private_directory(path)


def _private_file(path: Path) -> bytes:
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != PRIVATE_FILE_MODE
            or info.st_nlink != 1
        ):
            raise PublicChallengeError("input must be an owner-only regular file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            return handle.read()
    except PublicChallengeError:
        raise
    except OSError as exc:
        raise PublicChallengeError("owner-only input is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_private_new(path: Path, payload: bytes) -> None:
    _private_directory(path.parent, create=True)
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
        raise PublicChallengeError("owner-only artifact write failed") from exc


def _strict_json(raw: bytes, *, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in pairs:
            if key in output:
                raise PublicChallengeError(f"{label} contains duplicate keys")
            output[key] = value
        return output

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PublicChallengeError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict) or raw != _canonical_json(value) + b"\n":
        raise PublicChallengeError(f"{label} is not canonical")
    return value


def _key(path: Path) -> bytes:
    raw = _private_file(path)
    if len(raw) < 32:
        raise PublicChallengeError("HMAC key is too short")
    return raw


def _execution_claims(
    *, test_invoker_used: bool, external_calls_required: bool
) -> _ExecutionClaims:
    if not external_calls_required:
        return _ExecutionClaims(
            provider=PUBLIC_NO_CALL_PROVIDER,
            external_call_started=False,
            restricted_execution=False,
            ephemeral_execution=False,
            local_model_used=False,
            test_invoker_used=False,
        )
    if test_invoker_used:
        # An injected callable has no external-execution attestation. Mark it
        # conservatively as local so test output cannot masquerade as the
        # restricted Codex evidence required by the smoke gate.
        return _ExecutionClaims(
            provider=PUBLIC_TEST_PROVIDER,
            external_call_started=False,
            restricted_execution=False,
            ephemeral_execution=False,
            local_model_used=True,
            test_invoker_used=True,
        )
    return _ExecutionClaims(
        provider=GMAIL_TEMPORAL_EXTERNAL_PROVIDER,
        external_call_started=True,
        restricted_execution=True,
        ephemeral_execution=True,
        local_model_used=False,
        test_invoker_used=False,
    )


def _signed(
    value: Mapping[str, Any],
    *,
    key: bytes,
    domain: bytes,
    signature_field: str,
) -> dict[str, Any]:
    if signature_field in value:
        raise PublicChallengeError("signature field is duplicated")
    signature = hmac.new(
        key, domain + _canonical_json(value), hashlib.sha256
    ).hexdigest()
    return {**dict(value), signature_field: signature}


def _verify_signed(
    value: Mapping[str, Any],
    *,
    key: bytes,
    domain: bytes,
    signature_field: str,
) -> bool:
    supplied = value.get(signature_field)
    if not isinstance(supplied, str) or _SHA256_RE.fullmatch(supplied) is None:
        return False
    unsigned = dict(value)
    unsigned.pop(signature_field, None)
    expected = hmac.new(
        key, domain + _canonical_json(unsigned), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(supplied, expected)


def _load_challenge(path: Path, *, key: bytes) -> tuple[dict[str, Any], bytes]:
    raw = _private_file(path)
    value = _strict_json(raw, label="challenge manifest")
    if set(value) != _CHALLENGE_KEYS:
        raise PublicChallengeError("challenge manifest schema is invalid")
    if (
        value.get("version") not in {CHALLENGE_VERSION, LEGACY_CHALLENGE_VERSION}
        or value.get("scope") != PUBLIC_SCOPE
        or value.get("public_synthetic") is not True
        or value.get("contains_private_gmail") is not False
        or value.get("release_eligible") is not False
        or _CHALLENGE_ID_RE.fullmatch(str(value.get("challenge_id") or "")) is None
        or _aware_timestamp(value.get("created_at")) is None
        or _SHA256_RE.fullmatch(str(value.get("gold_sha256") or "")) is None
    ):
        raise PublicChallengeError("public challenge authority is invalid")
    home = value.get("brain_home")
    if not isinstance(home, str) or not home or "\x00" in home:
        raise PublicChallengeError("challenge Brain home is invalid")
    cases = value.get("cases")
    if not isinstance(cases, list) or not cases:
        raise PublicChallengeError("challenge cases are empty")
    seen_cases: set[str] = set()
    seen_targets: set[tuple[str, str]] = set()
    for item in cases:
        if not isinstance(item, Mapping) or set(item) != _CASE_KEYS:
            raise PublicChallengeError("challenge case schema is invalid")
        case_id = item.get("case_id")
        document_id = item.get("document_id")
        message_id = item.get("gmail_message_id")
        source_sha256 = item.get("source_sha256")
        target = (str(document_id or ""), str(message_id or ""))
        if (
            not isinstance(case_id, str)
            or _CASE_ID_RE.fullmatch(case_id) is None
            or case_id in seen_cases
            or not all(target)
            or target in seen_targets
            or not isinstance(source_sha256, str)
            or _SHA256_RE.fullmatch(source_sha256) is None
        ):
            raise PublicChallengeError("challenge case identity is invalid")
        seen_cases.add(case_id)
        seen_targets.add(target)
    _validate_public_root_authority(value, raw, key=key)
    return value, raw


def _known_private_brain_homes() -> set[Path]:
    values = {
        (Path.home() / "brain").resolve(),
        (Path.home() / "brain-v2").resolve(),
    }
    try:
        values.add(BrainPaths.from_value(None).home.expanduser().resolve())
    except Exception:  # noqa: BLE001 - conservative fixed homes remain enforced.
        pass
    return values


def _validate_public_root_authority(
    challenge: Mapping[str, Any], challenge_raw: bytes, *, key: bytes
) -> None:
    raw_home = Path(str(challenge["brain_home"])).expanduser()
    if raw_home.is_symlink() or not raw_home.is_dir():
        raise PublicChallengeError("dedicated public Brain home is unavailable")
    home = raw_home.resolve()
    if home in _known_private_brain_homes():
        raise PublicChallengeError("default or production Brain home is forbidden")
    paths = BrainPaths.from_value(home)
    marker_path = paths.config_local / PUBLIC_ROOT_AUTHORITY_FILENAME
    try:
        marker_raw = _private_file(marker_path)
    except PublicChallengeError as exc:
        raise PublicChallengeError("public root authority is unavailable") from exc
    marker = _strict_json(marker_raw, label="public root authority")
    expected_cases = [
        {
            "case_id": str(item["case_id"]),
            "document_id": str(item["document_id"]),
            "gmail_message_id": str(item["gmail_message_id"]),
            "source_sha256": str(item["source_sha256"]),
        }
        for item in challenge["cases"]
    ]
    if (
        set(marker) != _PUBLIC_ROOT_AUTHORITY_KEYS
        or not _verify_signed(
            marker,
            key=key,
            domain=PUBLIC_ROOT_AUTHORITY_DOMAIN,
            signature_field="authority_hmac_sha256",
        )
        or marker.get("version") != PUBLIC_ROOT_AUTHORITY_VERSION
        or marker.get("challenge_id") != challenge["challenge_id"]
        or marker.get("scope") != PUBLIC_SCOPE
        or _aware_timestamp(marker.get("created_at")) is None
        or marker.get("brain_home") != str(home)
        or marker.get("challenge_manifest_sha256") != _sha256(challenge_raw)
        or marker.get("gold_sha256") != challenge["gold_sha256"]
        or marker.get("public_synthetic") is not True
        or marker.get("contains_private_gmail") is not False
        or marker.get("release_eligible") is not False
        or marker.get("cases") != expected_cases
    ):
        raise PublicChallengeError("public root authority is invalid")


def _prepare_cases(challenge: Mapping[str, Any]) -> tuple[_Case, ...]:
    paths = BrainPaths.from_value(str(challenge["brain_home"]))
    output: list[_Case] = []
    for item in challenge["cases"]:
        try:
            preparation = prepare_gmail_temporal_review(
                paths,
                document_id=str(item["document_id"]),
                gmail_message_id=str(item["gmail_message_id"]),
            )
        except GmailTemporalRunnerError as exc:
            raise PublicChallengeError(
                "production challenge preparation failed"
            ) from exc
        if preparation.source_sha256 != item["source_sha256"]:
            raise PublicChallengeError("public challenge source authority is stale")
        output.append(
            _Case(
                case_id=str(item["case_id"]),
                document_id=str(item["document_id"]),
                gmail_message_id=str(item["gmail_message_id"]),
                preparation=preparation,
            )
        )
    return tuple(output)


def _request_rows(cases: Sequence[_Case]) -> tuple[_RequestRow, ...]:
    rows: list[_RequestRow] = []
    for case in cases:
        for request in case.preparation.requests:
            try:
                payload = json.loads(request.payload)
                clusters = payload["page"]["clusters"]
                candidate_ids = tuple(
                    candidate_id
                    for cluster in clusters
                    for candidate_id in cluster["candidate_ids"]
                )
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise PublicChallengeError(
                    "production verifier request is malformed"
                ) from exc
            if not candidate_ids or len(candidate_ids) != len(set(candidate_ids)):
                raise PublicChallengeError(
                    "production verifier candidate set is invalid"
                )
            rows.append(
                _RequestRow(
                    case_id=case.case_id,
                    request_fingerprint=request.request_fingerprint,
                    payload=payload,
                    batch_fingerprint=request.batch_fingerprint,
                    frontier_fingerprint=request.frontier_fingerprint,
                    page_plan_fingerprint=request.page_plan_fingerprint,
                    page_fingerprint=request.page_fingerprint,
                    candidate_ids=candidate_ids,
                )
            )
    return tuple(rows)


def _archived_request_rows(
    cases: Sequence[_Case],
    plan: Mapping[str, Any],
) -> tuple[_RequestRow, ...]:
    """Rebuild a prior launcher's requests with its authenticated policy ID."""

    raw_cases = plan.get("cases")
    if not isinstance(raw_cases, list) or len(raw_cases) != len(cases):
        raise PublicChallengeError("archived request case coverage is invalid")
    current_by_case: dict[str, list[_RequestRow]] = defaultdict(list)
    for row in _request_rows(cases):
        current_by_case[row.case_id].append(row)
    output: list[_RequestRow] = []
    for case, raw_case in zip(cases, raw_cases, strict=True):
        if not isinstance(raw_case, Mapping) or raw_case.get("case_id") != case.case_id:
            raise PublicChallengeError("archived request case order is invalid")
        runner_policy = raw_case.get("runner_policy_fingerprint")
        request_fingerprints = raw_case.get("request_fingerprints")
        current_rows = current_by_case.get(case.case_id, [])
        if (
            not isinstance(runner_policy, str)
            or _RUNNER_POLICY_RE.fullmatch(runner_policy) is None
            or not isinstance(request_fingerprints, list)
            or len(request_fingerprints) != len(current_rows)
            or any(
                not isinstance(value, str)
                or _REQUEST_FINGERPRINT_RE.fullmatch(value) is None
                for value in request_fingerprints
            )
        ):
            raise PublicChallengeError("archived request identity is invalid")
        for current_row, request_fingerprint in zip(
            current_rows,
            request_fingerprints,
            strict=True,
        ):
            payload = dict(current_row.payload)
            payload["runner_policy_fingerprint"] = runner_policy
            payload.pop("request_fingerprint", None)
            expected_fingerprint = (
                "gtrq_" + hashlib.sha256(_canonical_json(payload)).hexdigest()
            )
            if expected_fingerprint != request_fingerprint:
                raise PublicChallengeError("archived verifier request is stale")
            payload["request_fingerprint"] = request_fingerprint
            output.append(
                _RequestRow(
                    case_id=current_row.case_id,
                    request_fingerprint=request_fingerprint,
                    payload=payload,
                    batch_fingerprint=current_row.batch_fingerprint,
                    frontier_fingerprint=current_row.frontier_fingerprint,
                    page_plan_fingerprint=current_row.page_plan_fingerprint,
                    page_fingerprint=current_row.page_fingerprint,
                    candidate_ids=current_row.candidate_ids,
                )
            )
    return tuple(output)


def bounded_public_call_units(rows: Sequence[_RequestRow]) -> tuple[_CallUnit, ...]:
    """Apply the private holdout's exact item and serialized-byte ceilings."""

    candidate_ids = [candidate_id for row in rows for candidate_id in row.candidate_ids]
    if any(
        not row.candidate_ids
        or any(
            not isinstance(candidate_id, str)
            or _CANDIDATE_ID_RE.fullmatch(candidate_id) is None
            for candidate_id in row.candidate_ids
        )
        for row in rows
    ) or len(candidate_ids) != len(set(candidate_ids)):
        raise PublicChallengeError("verifier candidate authority is invalid")
    shared_rows = [
        {
            "case_id": row.case_id,
            "request_fingerprint": row.request_fingerprint,
            "payload": dict(row.payload),
        }
        for row in rows
    ]
    case_groups: list[list[Mapping[str, Any]]] = []
    seen_cases: set[str] = set()
    for row in shared_rows:
        case_id = str(row["case_id"])
        if not case_groups or str(case_groups[-1][0]["case_id"]) != case_id:
            if case_id in seen_cases:
                raise PublicChallengeError("public case request order is noncontiguous")
            seen_cases.add(case_id)
            case_groups.append([])
        case_groups[-1].append(row)

    batches: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    for group in case_groups:
        group_request = external._verifier_request(group)  # noqa: SLF001
        if (
            len(group) > external.MAX_VERIFIER_BATCH_SIZE
            or len(_canonical_json(group_request) + b"\n")
            > external.MAX_VERIFIER_REQUEST_BYTES
        ):
            raise PublicChallengeError(
                "one public case cannot fit one safe external invocation"
            )
        proposed = [*current, *group]
        proposed_request = external._verifier_request(proposed)  # noqa: SLF001
        if current and (
            len(proposed) > external.MAX_VERIFIER_BATCH_SIZE
            or len(_canonical_json(proposed_request) + b"\n")
            > external.MAX_VERIFIER_REQUEST_BYTES
        ):
            batches.append(current)
            current = list(group)
        else:
            current = proposed
    if current:
        batches.append(current)
    by_fingerprint = {row.request_fingerprint: row for row in rows}
    if len(by_fingerprint) != len(rows):
        raise PublicChallengeError("verifier request identity is duplicated")
    output: list[_CallUnit] = []
    case_unit: dict[str, int] = {}
    for unit_ordinal, batch in enumerate(batches, start=1):
        resolved: list[_RequestRow] = []
        for item in batch:
            fingerprint = str(item.get("request_fingerprint") or "")
            row = by_fingerprint.get(fingerprint)
            if row is None:
                raise PublicChallengeError("bounded verifier request is unknown")
            resolved.append(row)
        case_ids = tuple(dict.fromkeys(row.case_id for row in resolved))
        for case_id in case_ids:
            previous = case_unit.setdefault(case_id, unit_ordinal)
            if previous != unit_ordinal:
                raise PublicChallengeError(
                    "one public case cannot span multiple external invocations"
                )
        request = external._verifier_request(batch)  # noqa: SLF001
        request_raw = _canonical_json(request) + b"\n"
        if (
            len(batch) > external.MAX_VERIFIER_BATCH_SIZE
            or len(request_raw) > external.MAX_VERIFIER_REQUEST_BYTES
        ):
            raise PublicChallengeError("bounded verifier unit violates shared ceiling")
        output.append(
            _CallUnit(
                unit_ordinal=unit_ordinal,
                case_ids=case_ids,
                rows=tuple(resolved),
                request=request,
                request_sha256=_sha256(request_raw),
            )
        )
    covered = [row.request_fingerprint for unit in output for row in unit.rows]
    if covered != [row.request_fingerprint for row in rows]:
        raise PublicChallengeError("bounded verifier coverage is incomplete")
    return tuple(output)


def _source_hashes() -> dict[str, str]:
    return {
        "launcher": _sha256(Path(__file__).read_bytes()),
        "production_runner": _sha256(_RUNNER_PATH.read_bytes()),
        "shared_external_runner": _sha256(_HOLDOUT_RUNNER_PATH.read_bytes()),
    }


def _prediction_launcher_artifact_sha256(path: Path) -> str:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise PublicChallengeError(
            "prior prediction launcher artifact is unavailable"
        ) from exc
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o022
        or info.st_size <= 0
        or info.st_size > MAX_PREDICTION_LAUNCHER_ARTIFACT_BYTES
    ):
        raise PublicChallengeError("prior prediction launcher artifact is unsafe")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PublicChallengeError(
            "prior prediction launcher artifact is unavailable"
        ) from exc
    if len(raw) != info.st_size:
        raise PublicChallengeError("prior prediction launcher artifact is stale")
    return _sha256(raw)


def _validate_prediction_source_provenance(
    source_hashes: Any,
    *,
    launcher_version: Any,
    plan_version: Any,
    prediction_launcher_artifact: Path | None,
) -> _PredictionSourceProvenance:
    """Authenticate prediction code independently from the current scorer."""

    if (
        not isinstance(source_hashes, Mapping)
        or set(source_hashes)
        != {"launcher", "production_runner", "shared_external_runner"}
        or any(
            not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
            for value in source_hashes.values()
        )
    ):
        raise PublicChallengeError("public prediction source provenance is invalid")
    current = _source_hashes()
    if (
        source_hashes["production_runner"] != current["production_runner"]
        or source_hashes["shared_external_runner"] != current["shared_external_runner"]
    ):
        raise PublicChallengeError("public prediction runner provenance is stale")

    launcher_sha256 = str(source_hashes["launcher"])
    if launcher_sha256 == current["launcher"]:
        if (
            prediction_launcher_artifact is not None
            and _prediction_launcher_artifact_sha256(prediction_launcher_artifact)
            != launcher_sha256
        ):
            raise PublicChallengeError(
                "current prediction launcher artifact does not match its plan"
            )
        return _PredictionSourceProvenance(
            launcher_sha256=launcher_sha256,
            trust_basis="current_scorer_source",
            exact_artifact_verified=prediction_launcher_artifact is not None,
            scorer_sha256=current["launcher"],
        )
    if (
        launcher_version != VERSION
        or plan_version != PLAN_VERSION
        or prediction_launcher_artifact is None
        or _prediction_launcher_artifact_sha256(prediction_launcher_artifact)
        != launcher_sha256
    ):
        raise PublicChallengeError("public prediction launcher provenance is stale")
    return _PredictionSourceProvenance(
        launcher_sha256=launcher_sha256,
        trust_basis="exact_prior_launcher_artifact",
        exact_artifact_verified=True,
        scorer_sha256=current["launcher"],
    )


def _plan_value(
    *,
    challenge: Mapping[str, Any],
    challenge_raw: bytes,
    cases: Sequence[_Case],
    units: Sequence[_CallUnit],
    claims: _ExecutionClaims,
) -> dict[str, Any]:
    run_prefix = "gtpvc_test_r_" if claims.test_invoker_used else "gtpvc_r_"
    call_prefix = "gtpvc_test_i_" if claims.test_invoker_used else "gtpvc_i_"
    logical_runs = [run_prefix + secrets.token_hex(32) for _ in range(RUN_COUNT)]
    calls = []
    for run_ordinal, logical_run_id in enumerate(logical_runs, start=1):
        for unit in units:
            calls.append(
                {
                    "run_ordinal": run_ordinal,
                    "logical_run_id": logical_run_id,
                    "unit_ordinal": unit.unit_ordinal,
                    "call_id": call_prefix + secrets.token_hex(32),
                    "case_ids": list(unit.case_ids),
                    "request_fingerprints": [
                        row.request_fingerprint for row in unit.rows
                    ],
                    "request_sha256": unit.request_sha256,
                    "request_bytes": len(_canonical_json(unit.request) + b"\n"),
                }
            )
    return {
        "version": PLAN_VERSION,
        "launcher_version": VERSION,
        "challenge_id": challenge["challenge_id"],
        "challenge_manifest_sha256": _sha256(challenge_raw),
        "scope": PUBLIC_SCOPE,
        "provider": claims.provider,
        "model": GMAIL_TEMPORAL_VERIFIER_MODEL,
        "reasoning_effort": GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
        "run_count": RUN_COUNT,
        "case_count": len(cases),
        "candidate_case_count": sum(bool(case.preparation.requests) for case in cases),
        "zero_work_case_count": sum(not case.preparation.requests for case in cases),
        "request_count_per_run": len(_request_rows(cases)),
        "call_count_per_run": len(units),
        "max_items_per_call": external.MAX_VERIFIER_BATCH_SIZE,
        "max_request_bytes": external.MAX_VERIFIER_REQUEST_BYTES,
        "cases": [
            {
                "case_id": case.case_id,
                "disposition": case.preparation.disposition,
                "admission_basis": case.preparation.admission_basis,
                "runner_policy_fingerprint": (
                    case.preparation.runner_policy_fingerprint
                ),
                "admission_policy_fingerprint": (
                    case.preparation.admission_policy_fingerprint
                ),
                "verifier_policy_fingerprint": (
                    case.preparation.verifier_policy_fingerprint
                ),
                "source_sha256": case.preparation.source_sha256,
                "analysis_fingerprint": case.preparation.analysis_fingerprint,
                "batch_plan_fingerprint": case.preparation.batch_plan_fingerprint,
                "target_fingerprint": case.preparation.target_fingerprint,
                "request_fingerprints": [
                    request.request_fingerprint for request in case.preparation.requests
                ],
            }
            for case in cases
        ],
        "calls": calls,
        "source_module_sha256": _source_hashes(),
        "gold_sha256_committed_but_not_opened": challenge["gold_sha256"],
        "gold_accessed": False,
        "public_synthetic": True,
        "contains_private_gmail": False,
        "release_eligible": False,
        "restricted_execution": claims.restricted_execution,
        "ephemeral_execution": claims.ephemeral_execution,
        "local_model_used": claims.local_model_used,
        "test_invoker_used": claims.test_invoker_used,
        "created_at": _now(),
    }


def _validate_response(
    unit: _CallUnit, response: Mapping[str, Any]
) -> dict[str, tuple[Mapping[str, Any], ...]]:
    if (
        set(response) != {"version", "pages"}
        or response.get("version") != external.VERIFIER_RESPONSE_VERSION
    ):
        raise PublicChallengeError("external verifier response schema is invalid")
    pages = response.get("pages")
    if not isinstance(pages, list) or len(pages) != len(unit.rows):
        raise PublicChallengeError("external verifier response coverage is invalid")
    rows_by_fingerprint = {row.request_fingerprint: row for row in unit.rows}
    if len(rows_by_fingerprint) != len(unit.rows):
        raise PublicChallengeError("external verifier request authority is invalid")
    verdicts_by_fingerprint: dict[str, dict[str, str]] = {}
    for page in pages:
        if not isinstance(page, Mapping) or set(page) != {
            "request_fingerprint",
            "verdicts",
        }:
            raise PublicChallengeError("external verifier page authority is invalid")
        request_fingerprint = page.get("request_fingerprint")
        if (
            not isinstance(request_fingerprint, str)
            or request_fingerprint not in rows_by_fingerprint
            or request_fingerprint in verdicts_by_fingerprint
        ):
            raise PublicChallengeError("external verifier page authority is invalid")
        row = rows_by_fingerprint[request_fingerprint]
        verdicts = page.get("verdicts")
        if not isinstance(verdicts, list) or not verdicts:
            raise PublicChallengeError("external verifier verdicts are invalid")
        parsed: dict[str, str] = {}
        for verdict in verdicts:
            if (
                not isinstance(verdict, Mapping)
                or set(verdict) != {"candidate_id", "verdict"}
                or not isinstance(verdict.get("candidate_id"), str)
                or verdict.get("verdict")
                not in {"supported", "uncertain", "unsupported"}
            ):
                raise PublicChallengeError("external verifier verdict is invalid")
            candidate_id = str(verdict["candidate_id"])
            if candidate_id in parsed:
                raise PublicChallengeError(
                    "external verifier candidate coverage is invalid"
                )
            parsed[candidate_id] = str(verdict["verdict"])
        if set(parsed) != set(row.candidate_ids):
            raise PublicChallengeError(
                "external verifier candidate coverage is invalid"
            )
        verdicts_by_fingerprint[request_fingerprint] = parsed
    if set(verdicts_by_fingerprint) != set(rows_by_fingerprint):
        raise PublicChallengeError("external verifier response coverage is invalid")

    by_case: dict[str, list[Mapping[str, Any]]] = {
        case_id: [] for case_id in unit.case_ids
    }
    for row in unit.rows:
        parsed = verdicts_by_fingerprint[row.request_fingerprint]
        by_case[row.case_id].append(
            {
                "request_fingerprint": row.request_fingerprint,
                "batch_fingerprint": row.batch_fingerprint,
                "frontier_fingerprint": row.frontier_fingerprint,
                "page_plan_fingerprint": row.page_plan_fingerprint,
                "page_fingerprint": row.page_fingerprint,
                "verdicts": [
                    {"candidate_id": candidate_id, "verdict": parsed[candidate_id]}
                    for candidate_id in row.candidate_ids
                ],
            }
        )
    return {key: tuple(value) for key, value in by_case.items()}


def _verifier_response_schema(unit: _CallUnit) -> dict[str, Any]:
    """Constrain redundant response identifiers to this exact sealed call."""

    request_fingerprints = [row.request_fingerprint for row in unit.rows]
    candidate_ids = [
        candidate_id for row in unit.rows for candidate_id in row.candidate_ids
    ]
    if (
        not request_fingerprints
        or len(request_fingerprints) != len(set(request_fingerprints))
        or not candidate_ids
        or any(
            not isinstance(candidate_id, str)
            or _CANDIDATE_ID_RE.fullmatch(candidate_id) is None
            for candidate_id in candidate_ids
        )
        or len(candidate_ids) != len(set(candidate_ids))
    ):
        raise PublicChallengeError("external verifier request authority is invalid")
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "version": {
                "type": "string",
                "const": external.VERIFIER_RESPONSE_VERSION,
            },
            "pages": {
                "type": "array",
                "minItems": len(unit.rows),
                "maxItems": len(unit.rows),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "request_fingerprint": {
                            "type": "string",
                            "enum": request_fingerprints,
                        },
                        "verdicts": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": max(
                                len(row.candidate_ids) for row in unit.rows
                            ),
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "candidate_id": {
                                        "type": "string",
                                        "enum": sorted(set(candidate_ids)),
                                    },
                                    "verdict": {
                                        "type": "string",
                                        "enum": [
                                            "supported",
                                            "uncertain",
                                            "unsupported",
                                        ],
                                    },
                                },
                                "required": ["candidate_id", "verdict"],
                            },
                        },
                    },
                    "required": ["request_fingerprint", "verdicts"],
                },
            },
        },
        "required": ["version", "pages"],
    }


def _call_path(output_root: Path, run_ordinal: int, unit_ordinal: int) -> Path:
    return output_root / "calls" / f"run-{run_ordinal}" / f"unit-{unit_ordinal:03d}"


def _execute_call(
    *,
    output_root: Path,
    key: bytes,
    plan: Mapping[str, Any],
    unit: _CallUnit,
    run_ordinal: int,
    invoke: ModelInvoker,
    timeout_seconds: int,
    claims: _ExecutionClaims,
) -> _CompletedCall:
    entries = [
        item
        for item in plan["calls"]
        if item["run_ordinal"] == run_ordinal
        and item["unit_ordinal"] == unit.unit_ordinal
    ]
    if len(entries) != 1:
        raise PublicChallengeError("call plan authority is ambiguous")
    entry = entries[0]
    call_id = str(entry["call_id"])
    logical_run_id = str(entry["logical_run_id"])
    if (
        _CALL_ID_RE.fullmatch(call_id) is None
        or _LOGICAL_RUN_ID_RE.fullmatch(logical_run_id) is None
        or entry["request_sha256"] != unit.request_sha256
    ):
        raise PublicChallengeError("call plan authority is invalid")
    call_root = _call_path(output_root, run_ordinal, unit.unit_ordinal)
    _private_directory(call_root, create=True)
    request_raw = _canonical_json(unit.request) + b"\n"
    _write_private_new(call_root / "request.json", request_raw)
    started_at = _now()
    start = _signed(
        {
            "version": CALL_START_VERSION,
            "challenge_id": plan["challenge_id"],
            "run_ordinal": run_ordinal,
            "logical_run_id": logical_run_id,
            "unit_ordinal": unit.unit_ordinal,
            "call_id": call_id,
            "provider": claims.provider,
            "model": GMAIL_TEMPORAL_VERIFIER_MODEL,
            "reasoning_effort": GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
            "request_sha256": unit.request_sha256,
            "started_at": started_at,
            "external_call_started": claims.external_call_started,
            "restricted_execution": claims.restricted_execution,
            "ephemeral_execution": claims.ephemeral_execution,
            "local_model_used": claims.local_model_used,
            "test_invoker_used": claims.test_invoker_used,
            "public_synthetic": True,
            "release_eligible": False,
        },
        key=key,
        domain=CALL_START_DOMAIN,
        signature_field="start_hmac_sha256",
    )
    start_raw = _canonical_json(start) + b"\n"
    _write_private_new(call_root / "started.json", start_raw)
    response: Mapping[str, Any] | None = None
    error_type: str | None = None
    case_pages: dict[str, tuple[Mapping[str, Any], ...]] | None = None
    try:
        response = invoke(
            unit.request,
            _verifier_response_schema(unit),
            GMAIL_TEMPORAL_VERIFIER_MODEL,
            GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
            timeout_seconds,
        )
        case_pages = _validate_response(unit, response)
    except Exception as exc:  # noqa: BLE001 - details are hashed, never reflected.
        error_type = type(exc).__name__
        diagnostic_sha256 = _sha256(
            f"{type(exc).__module__}.{type(exc).__name__}:{exc}".encode("utf-8")
        )
        completed_at = _now()
        receipt = _signed(
            {
                "version": CALL_RECEIPT_VERSION,
                "challenge_id": plan["challenge_id"],
                "run_ordinal": run_ordinal,
                "logical_run_id": logical_run_id,
                "unit_ordinal": unit.unit_ordinal,
                "call_id": call_id,
                "provider": claims.provider,
                "model": GMAIL_TEMPORAL_VERIFIER_MODEL,
                "reasoning_effort": GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
                "started_at": started_at,
                "completed_at": completed_at,
                "request_sha256": unit.request_sha256,
                "response_sha256": None,
                "status": "failed",
                "error_type": error_type,
                "diagnostic_sha256": diagnostic_sha256,
                "external_call_started": claims.external_call_started,
                "restricted_execution": claims.restricted_execution,
                "ephemeral_execution": claims.ephemeral_execution,
                "local_model_used": claims.local_model_used,
                "test_invoker_used": claims.test_invoker_used,
                "public_synthetic": True,
                "release_eligible": False,
            },
            key=key,
            domain=CALL_RECEIPT_DOMAIN,
            signature_field="receipt_hmac_sha256",
        )
        _write_private_new(call_root / "receipt.json", _canonical_json(receipt) + b"\n")
        raise PublicChallengeError("restricted external verifier call failed") from exc
    assert response is not None and case_pages is not None
    response_raw = _canonical_json(response) + b"\n"
    _write_private_new(call_root / "response.json", response_raw)
    completed_at = _now()
    receipt = _signed(
        {
            "version": CALL_RECEIPT_VERSION,
            "challenge_id": plan["challenge_id"],
            "run_ordinal": run_ordinal,
            "logical_run_id": logical_run_id,
            "unit_ordinal": unit.unit_ordinal,
            "call_id": call_id,
            "provider": claims.provider,
            "model": GMAIL_TEMPORAL_VERIFIER_MODEL,
            "reasoning_effort": GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
            "started_at": started_at,
            "completed_at": completed_at,
            "request_sha256": unit.request_sha256,
            "response_sha256": _sha256(response_raw),
            "status": "success",
            "error_type": None,
            "diagnostic_sha256": None,
            "external_call_started": claims.external_call_started,
            "restricted_execution": claims.restricted_execution,
            "ephemeral_execution": claims.ephemeral_execution,
            "local_model_used": claims.local_model_used,
            "test_invoker_used": claims.test_invoker_used,
            "public_synthetic": True,
            "release_eligible": False,
        },
        key=key,
        domain=CALL_RECEIPT_DOMAIN,
        signature_field="receipt_hmac_sha256",
    )
    receipt_raw = _canonical_json(receipt) + b"\n"
    _write_private_new(call_root / "receipt.json", receipt_raw)
    return _CompletedCall(
        run_ordinal=run_ordinal,
        unit_ordinal=unit.unit_ordinal,
        call_id=call_id,
        logical_run_id=logical_run_id,
        started_at=started_at,
        completed_at=completed_at,
        request_sha256=unit.request_sha256,
        response_sha256=_sha256(response_raw),
        start_sha256=_sha256(start_raw),
        receipt_sha256=_sha256(receipt_raw),
        response=response,
        case_pages=case_pages,
    )


def _component_value(
    case: _Case,
    call: _CompletedCall,
    *,
    archived_plan_case: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    preparation = case.preparation
    pages = call.case_pages.get(case.case_id)
    if pages is None or len(pages) != len(preparation.requests):
        raise PublicChallengeError("case prediction coverage is incomplete")
    return {
        "version": GMAIL_TEMPORAL_COMPONENT_VERSION,
        "run_ordinal": call.run_ordinal,
        "invocation_id": call.call_id,
        "provider": GMAIL_TEMPORAL_EXTERNAL_PROVIDER,
        "model": GMAIL_TEMPORAL_VERIFIER_MODEL,
        "reasoning_effort": GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
        "started_at": call.started_at,
        "completed_at": call.completed_at,
        "runner_policy_fingerprint": (
            archived_plan_case["runner_policy_fingerprint"]
            if archived_plan_case is not None
            else gmail_temporal_runner_policy_fingerprint()
        ),
        "admission_policy_fingerprint": (gmail_temporal_admission_policy_fingerprint()),
        "verifier_policy_fingerprint": gmail_temporal_verifier_policy_fingerprint(),
        "source_sha256": preparation.source_sha256,
        "analysis_fingerprint": preparation.analysis_fingerprint,
        "batch_plan_fingerprint": preparation.batch_plan_fingerprint,
        "target_fingerprint": (
            archived_plan_case["target_fingerprint"]
            if archived_plan_case is not None
            else preparation.target_fingerprint
        ),
        "pages": [dict(item) for item in pages],
        "complete": True,
        "routable": False,
    }


def _component_artifacts(
    output_root: Path,
    cases: Sequence[_Case],
    calls: Sequence[_CompletedCall],
) -> dict[str, tuple[Path, ...]]:
    by_run_case: dict[tuple[int, str], _CompletedCall] = {}
    for call in calls:
        for case_id in call.case_pages:
            key = (call.run_ordinal, case_id)
            if key in by_run_case:
                raise PublicChallengeError("case spans multiple calls in one run")
            by_run_case[key] = call
    output: dict[str, tuple[Path, ...]] = {}
    for case in cases:
        if not case.preparation.requests:
            output[case.case_id] = ()
            continue
        paths: list[Path] = []
        for run_ordinal in range(1, RUN_COUNT + 1):
            call = by_run_case.get((run_ordinal, case.case_id))
            if call is None:
                raise PublicChallengeError("case prediction run is missing")
            value = _component_value(case, call)
            path = output_root / "components" / case.case_id / f"run-{run_ordinal}.json"
            _write_private_new(path, _canonical_json(value) + b"\n")
            paths.append(path)
        output[case.case_id] = tuple(paths)
    return output


def _prediction_seal(
    *,
    key: bytes,
    challenge: Mapping[str, Any],
    challenge_raw: bytes,
    plan_raw: bytes,
    cases: Sequence[_Case],
    calls: Sequence[_CompletedCall],
    components: Mapping[str, tuple[Path, ...]],
    claims: _ExecutionClaims,
) -> dict[str, Any]:
    return _signed(
        {
            "version": PREDICTION_SEAL_VERSION,
            "launcher_version": VERSION,
            "challenge_id": challenge["challenge_id"],
            "challenge_manifest_sha256": _sha256(challenge_raw),
            "plan_sha256": _sha256(plan_raw),
            "gold_sha256_committed_but_not_opened": challenge["gold_sha256"],
            "gold_accessed": False,
            "provider": claims.provider,
            "model": GMAIL_TEMPORAL_VERIFIER_MODEL,
            "reasoning_effort": GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
            "run_count": RUN_COUNT,
            "invocation_count": len(calls),
            "external_call_count": (len(calls) if claims.external_call_started else 0),
            "call_ids": [call.call_id for call in calls],
            "call_evidence": [
                {
                    "run_ordinal": call.run_ordinal,
                    "unit_ordinal": call.unit_ordinal,
                    "logical_run_id": call.logical_run_id,
                    "call_id": call.call_id,
                    "request_sha256": call.request_sha256,
                    "start_sha256": call.start_sha256,
                    "response_sha256": call.response_sha256,
                    "receipt_sha256": call.receipt_sha256,
                }
                for call in calls
            ],
            "request_set_sha256": _sha256(
                _canonical_json(sorted(call.request_sha256 for call in calls))
            ),
            "response_set_sha256": _sha256(
                _canonical_json(sorted(call.response_sha256 for call in calls))
            ),
            "receipt_set_sha256": _sha256(
                _canonical_json(sorted(call.receipt_sha256 for call in calls))
            ),
            "cases": [
                {
                    "case_id": case.case_id,
                    "disposition": case.preparation.disposition,
                    "component_sha256": [
                        _sha256(_private_file(path))
                        for path in components[case.case_id]
                    ],
                }
                for case in cases
            ],
            "public_synthetic": True,
            "contains_private_gmail": False,
            "release_eligible": False,
            "restricted_execution": claims.restricted_execution,
            "ephemeral_execution": claims.ephemeral_execution,
            "local_model_used": claims.local_model_used,
            "test_invoker_used": claims.test_invoker_used,
            "sealed_at": _now(),
        },
        key=key,
        domain=PREDICTION_SEAL_DOMAIN,
        signature_field="seal_hmac_sha256",
    )


def run_public_challenge(
    challenge_path: Path,
    hmac_key_path: Path,
    output_root: Path,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    codex_binary: str | None = None,
    invoke: ModelInvoker | None = None,
    test_only_allow_injected_invoker: bool = False,
) -> dict[str, Any]:
    """Run and persist one fresh public challenge without opening semantic gold."""

    if not 30 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise PublicChallengeError("timeout is outside the safe bound")
    if (invoke is not None) != test_only_allow_injected_invoker:
        raise PublicChallengeError("injected invoker requires explicit test-only mode")
    key = _key(hmac_key_path)
    challenge, challenge_raw = _load_challenge(challenge_path, key=key)
    cases = _prepare_cases(challenge)
    rows = _request_rows(cases)
    units = bounded_public_call_units(rows)
    if rows and not units:
        raise PublicChallengeError("candidate-bearing challenge has no call units")
    claims = _execution_claims(
        test_invoker_used=invoke is not None,
        external_calls_required=bool(units),
    )
    _fresh_private_directory(output_root)
    plan = _signed(
        _plan_value(
            challenge=challenge,
            challenge_raw=challenge_raw,
            cases=cases,
            units=units,
            claims=claims,
        ),
        key=key,
        domain=PLAN_DOMAIN,
        signature_field="plan_hmac_sha256",
    )
    plan_raw = _canonical_json(plan) + b"\n"
    _write_private_new(output_root / "plan.json", plan_raw)
    active_invoke = (
        invoke or external.RestrictedCodexInvoker(codex_binary) if units else None
    )
    completed: list[_CompletedCall] = []
    for run_ordinal in range(1, RUN_COUNT + 1):
        for unit in units:
            if active_invoke is None:
                raise PublicChallengeError("public verifier invoker is unavailable")
            completed.append(
                _execute_call(
                    output_root=output_root,
                    key=key,
                    plan=plan,
                    unit=unit,
                    run_ordinal=run_ordinal,
                    invoke=active_invoke,
                    timeout_seconds=timeout_seconds,
                    claims=claims,
                )
            )

    components = _component_artifacts(output_root, cases, completed)
    # Validate every component against freshly reconstructed production authority
    # before the seal or any persistence mutation becomes possible.
    import pkm_brain.gmail_temporal_runner as production_runner

    paths = BrainPaths.from_value(str(challenge["brain_home"]))
    for case in cases:
        if not components[case.case_id]:
            continue
        authority = production_runner._build_authority(  # noqa: SLF001
            paths,
            document_id=case.document_id,
            gmail_message_id=case.gmail_message_id,
        )
        production_runner._load_components(  # noqa: SLF001
            components[case.case_id], authority=authority
        )
    seal = _prediction_seal(
        key=key,
        challenge=challenge,
        challenge_raw=challenge_raw,
        plan_raw=plan_raw,
        cases=cases,
        calls=completed,
        components=components,
        claims=claims,
    )
    seal_raw = _canonical_json(seal) + b"\n"
    _write_private_new(output_root / "prediction-seal.json", seal_raw)

    results: list[dict[str, Any]] = []
    for case in cases:
        try:
            result = run_gmail_temporal_review(
                paths,
                document_id=case.document_id,
                gmail_message_id=case.gmail_message_id,
                component_artifacts=components[case.case_id],
            )
        except GmailTemporalRunnerError as exc:
            raise PublicChallengeError(
                "production finalization failed after seal"
            ) from exc
        projection: Mapping[str, Any] | None = None
        if result.run_id is not None:
            with connection(paths.sqlite_path) as conn:
                row = conn.execute(
                    "SELECT projection_json FROM gmail_temporal_review_runs WHERE id = ?",
                    (result.run_id,),
                ).fetchone()
            if row is None:
                raise PublicChallengeError("persisted projection is unavailable")
            projection = json.loads(str(row["projection_json"]))
        results.append(
            {
                "case_id": case.case_id,
                "runner_result": asdict(result),
                "projection": projection,
            }
        )
    result_value = _signed(
        {
            "version": RESULT_VERSION,
            "launcher_version": VERSION,
            "challenge_id": challenge["challenge_id"],
            "challenge_manifest_sha256": _sha256(challenge_raw),
            "plan_sha256": _sha256(plan_raw),
            "prediction_seal_sha256": _sha256(seal_raw),
            "gold_sha256_committed_but_not_opened": challenge["gold_sha256"],
            "gold_accessed": False,
            "provider": claims.provider,
            "model": GMAIL_TEMPORAL_VERIFIER_MODEL,
            "reasoning_effort": GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
            "invocation_count": len(completed),
            "external_call_count": (
                len(completed) if claims.external_call_started else 0
            ),
            "results": results,
            "public_synthetic": True,
            "contains_private_gmail": False,
            "release_eligible": False,
            "restricted_execution": claims.restricted_execution,
            "ephemeral_execution": claims.ephemeral_execution,
            "local_model_used": claims.local_model_used,
            "test_invoker_used": claims.test_invoker_used,
            "complete": True,
            "completed_at": _now(),
        },
        key=key,
        domain=RESULT_DOMAIN,
        signature_field="result_hmac_sha256",
    )
    result_raw = _canonical_json(result_value) + b"\n"
    _write_private_new(output_root / "results.json", result_raw)
    return {
        "version": VERSION,
        "status": "complete",
        "challenge_id": challenge["challenge_id"],
        "cases": len(cases),
        "candidate_cases": sum(bool(case.preparation.requests) for case in cases),
        "zero_work_cases": sum(not case.preparation.requests for case in cases),
        "requests_per_run": len(rows),
        "calls_per_run": len(units),
        "invocations": len(completed),
        "external_calls": len(completed) if claims.external_call_started else 0,
        "artifact_count": sum(
            int(item["runner_result"]["artifact_count"]) for item in results
        ),
        "gold_accessed": False,
        "public_synthetic": True,
        "release_eligible": False,
        "restricted_execution": claims.restricted_execution,
        "test_invoker_used": claims.test_invoker_used,
        "local_model_used": claims.local_model_used,
        "private_content_printed": False,
    }


def _load_signed_artifact(
    path: Path,
    *,
    key: bytes,
    domain: bytes,
    signature_field: str,
    label: str,
) -> tuple[dict[str, Any], bytes]:
    raw = _private_file(path)
    value = _strict_json(raw, label=label)
    if not _verify_signed(
        value,
        key=key,
        domain=domain,
        signature_field=signature_field,
    ):
        raise PublicChallengeError(f"{label} authentication failed")
    return value, raw


def _plan_case_rows(cases: Sequence[_Case]) -> list[dict[str, Any]]:
    return [
        {
            "case_id": case.case_id,
            "disposition": case.preparation.disposition,
            "admission_basis": case.preparation.admission_basis,
            "runner_policy_fingerprint": case.preparation.runner_policy_fingerprint,
            "admission_policy_fingerprint": (
                case.preparation.admission_policy_fingerprint
            ),
            "verifier_policy_fingerprint": (
                case.preparation.verifier_policy_fingerprint
            ),
            "source_sha256": case.preparation.source_sha256,
            "analysis_fingerprint": case.preparation.analysis_fingerprint,
            "batch_plan_fingerprint": case.preparation.batch_plan_fingerprint,
            "target_fingerprint": case.preparation.target_fingerprint,
            "request_fingerprints": [
                request.request_fingerprint for request in case.preparation.requests
            ],
        }
        for case in cases
    ]


def _expected_plan_case_rows(
    cases: Sequence[_Case],
    plan: Mapping[str, Any],
    provenance: _PredictionSourceProvenance,
) -> list[dict[str, Any]]:
    expected = _plan_case_rows(cases)
    if provenance.trust_basis == "current_scorer_source":
        return expected
    raw_cases = plan.get("cases")
    if not isinstance(raw_cases, list) or len(raw_cases) != len(expected):
        raise PublicChallengeError("archived plan case coverage is invalid")
    for current, archived in zip(expected, raw_cases, strict=True):
        if not isinstance(archived, Mapping):
            raise PublicChallengeError("archived plan case is invalid")
        runner_policy = archived.get("runner_policy_fingerprint")
        target_fingerprint = archived.get("target_fingerprint")
        request_fingerprints = archived.get("request_fingerprints")
        if (
            not isinstance(runner_policy, str)
            or _RUNNER_POLICY_RE.fullmatch(runner_policy) is None
            or not isinstance(target_fingerprint, str)
            or _TARGET_FINGERPRINT_RE.fullmatch(target_fingerprint) is None
            or not isinstance(request_fingerprints, list)
            or any(
                not isinstance(value, str)
                or _REQUEST_FINGERPRINT_RE.fullmatch(value) is None
                for value in request_fingerprints
            )
        ):
            raise PublicChallengeError("archived plan case identity is invalid")
        current["runner_policy_fingerprint"] = runner_policy
        current["target_fingerprint"] = target_fingerprint
        current["request_fingerprints"] = list(request_fingerprints)
    return expected


def _claims_from_plan(
    plan: Mapping[str, Any], *, external_calls_required: bool
) -> _ExecutionClaims:
    test_value = plan.get("test_invoker_used")
    if not isinstance(test_value, bool):
        raise PublicChallengeError("public challenge execution mode is invalid")
    claims = _execution_claims(
        test_invoker_used=test_value,
        external_calls_required=external_calls_required,
    )
    if (
        plan.get("provider") != claims.provider
        or plan.get("restricted_execution") is not claims.restricted_execution
        or plan.get("ephemeral_execution") is not claims.ephemeral_execution
        or plan.get("local_model_used") is not claims.local_model_used
        or plan.get("test_invoker_used") is not claims.test_invoker_used
    ):
        raise PublicChallengeError("public challenge execution claims are invalid")
    return claims


def _validate_plan_authority(
    plan: Mapping[str, Any],
    *,
    challenge: Mapping[str, Any],
    challenge_raw: bytes,
    cases: Sequence[_Case],
    units: Sequence[_CallUnit],
    source_provenance: _PredictionSourceProvenance,
) -> tuple[
    _ExecutionClaims,
    dict[tuple[int, int], Mapping[str, Any]],
]:
    expected_keys = {
        "version",
        "launcher_version",
        "challenge_id",
        "challenge_manifest_sha256",
        "scope",
        "provider",
        "model",
        "reasoning_effort",
        "run_count",
        "case_count",
        "candidate_case_count",
        "zero_work_case_count",
        "request_count_per_run",
        "call_count_per_run",
        "max_items_per_call",
        "max_request_bytes",
        "cases",
        "calls",
        "source_module_sha256",
        "gold_sha256_committed_but_not_opened",
        "gold_accessed",
        "public_synthetic",
        "contains_private_gmail",
        "release_eligible",
        "restricted_execution",
        "ephemeral_execution",
        "local_model_used",
        "test_invoker_used",
        "created_at",
        "plan_hmac_sha256",
    }
    if set(plan) != expected_keys:
        raise PublicChallengeError("public challenge plan schema is invalid")
    claims = _claims_from_plan(plan, external_calls_required=bool(units))
    rows = tuple(row for unit in units for row in unit.rows)
    stable = {
        "version": PLAN_VERSION,
        "launcher_version": VERSION,
        "challenge_id": challenge["challenge_id"],
        "challenge_manifest_sha256": _sha256(challenge_raw),
        "scope": PUBLIC_SCOPE,
        "provider": claims.provider,
        "model": GMAIL_TEMPORAL_VERIFIER_MODEL,
        "reasoning_effort": GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
        "run_count": RUN_COUNT,
        "case_count": len(cases),
        "candidate_case_count": sum(bool(case.preparation.requests) for case in cases),
        "zero_work_case_count": sum(not case.preparation.requests for case in cases),
        "request_count_per_run": len(rows),
        "call_count_per_run": len(units),
        "max_items_per_call": external.MAX_VERIFIER_BATCH_SIZE,
        "max_request_bytes": external.MAX_VERIFIER_REQUEST_BYTES,
        "cases": _expected_plan_case_rows(cases, plan, source_provenance),
        "gold_sha256_committed_but_not_opened": challenge["gold_sha256"],
        "gold_accessed": False,
        "public_synthetic": True,
        "contains_private_gmail": False,
        "release_eligible": False,
        "restricted_execution": claims.restricted_execution,
        "ephemeral_execution": claims.ephemeral_execution,
        "local_model_used": claims.local_model_used,
        "test_invoker_used": claims.test_invoker_used,
    }
    if any(plan.get(key) != value for key, value in stable.items()):
        raise PublicChallengeError("public challenge plan authority is stale")
    if _aware_timestamp(plan.get("created_at")) is None:
        raise PublicChallengeError("public challenge plan chronology is invalid")
    raw_calls = plan.get("calls")
    if not isinstance(raw_calls, list) or len(raw_calls) != RUN_COUNT * len(units):
        raise PublicChallengeError("public challenge call plan coverage is invalid")
    expected_call_keys = {
        "run_ordinal",
        "logical_run_id",
        "unit_ordinal",
        "call_id",
        "case_ids",
        "request_fingerprints",
        "request_sha256",
        "request_bytes",
    }
    by_key: dict[tuple[int, int], Mapping[str, Any]] = {}
    logical_by_run: dict[int, str] = {}
    call_ids: set[str] = set()
    for raw_call in raw_calls:
        if not isinstance(raw_call, Mapping) or set(raw_call) != expected_call_keys:
            raise PublicChallengeError("public challenge call plan schema is invalid")
        run_ordinal = raw_call.get("run_ordinal")
        unit_ordinal = raw_call.get("unit_ordinal")
        if (
            not isinstance(run_ordinal, int)
            or isinstance(run_ordinal, bool)
            or run_ordinal not in range(1, RUN_COUNT + 1)
            or not isinstance(unit_ordinal, int)
            or isinstance(unit_ordinal, bool)
            or unit_ordinal not in range(1, len(units) + 1)
        ):
            raise PublicChallengeError("public challenge call plan ordinal is invalid")
        key = (run_ordinal, unit_ordinal)
        if key in by_key:
            raise PublicChallengeError("public challenge call plan is duplicated")
        unit = units[unit_ordinal - 1]
        call_id = raw_call.get("call_id")
        logical_id = raw_call.get("logical_run_id")
        expected_test_marker = "_test_" in str(call_id)
        if (
            not isinstance(call_id, str)
            or _CALL_ID_RE.fullmatch(call_id) is None
            or call_id in call_ids
            or expected_test_marker is not claims.test_invoker_used
            or not isinstance(logical_id, str)
            or _LOGICAL_RUN_ID_RE.fullmatch(logical_id) is None
            or ("_test_" in logical_id) is not claims.test_invoker_used
            or raw_call.get("case_ids") != list(unit.case_ids)
            or raw_call.get("request_fingerprints")
            != [row.request_fingerprint for row in unit.rows]
            or raw_call.get("request_sha256") != unit.request_sha256
            or raw_call.get("request_bytes")
            != len(_canonical_json(unit.request) + b"\n")
        ):
            raise PublicChallengeError(
                "public challenge call plan authority is invalid"
            )
        previous = logical_by_run.setdefault(run_ordinal, logical_id)
        if previous != logical_id:
            raise PublicChallengeError("logical run spans inconsistent identities")
        call_ids.add(call_id)
        by_key[key] = raw_call
    if (units and len(set(logical_by_run.values())) != RUN_COUNT) or set(by_key) != {
        (run, unit.unit_ordinal) for run in range(1, RUN_COUNT + 1) for unit in units
    }:
        raise PublicChallengeError("public challenge logical runs are incomplete")
    return claims, by_key


def _validate_call_evidence(
    *,
    output_root: Path,
    key: bytes,
    challenge: Mapping[str, Any],
    plan_calls: Mapping[tuple[int, int], Mapping[str, Any]],
    units: Sequence[_CallUnit],
    claims: _ExecutionClaims,
) -> tuple[_CompletedCall, ...]:
    calls_root = output_root / "calls"
    if not units:
        if calls_root.exists() or calls_root.is_symlink():
            raise PublicChallengeError("zero-work challenge fabricated call evidence")
        return ()
    _private_directory(calls_root)
    expected_run_names = {f"run-{value}" for value in range(1, RUN_COUNT + 1)}
    if {path.name for path in calls_root.iterdir()} != expected_run_names:
        raise PublicChallengeError("public challenge call directory is invalid")
    completed: list[_CompletedCall] = []
    start_keys = {
        "version",
        "challenge_id",
        "run_ordinal",
        "logical_run_id",
        "unit_ordinal",
        "call_id",
        "provider",
        "model",
        "reasoning_effort",
        "request_sha256",
        "started_at",
        "external_call_started",
        "restricted_execution",
        "ephemeral_execution",
        "local_model_used",
        "test_invoker_used",
        "public_synthetic",
        "release_eligible",
        "start_hmac_sha256",
    }
    receipt_keys = {
        "version",
        "challenge_id",
        "run_ordinal",
        "logical_run_id",
        "unit_ordinal",
        "call_id",
        "provider",
        "model",
        "reasoning_effort",
        "started_at",
        "completed_at",
        "request_sha256",
        "response_sha256",
        "status",
        "error_type",
        "diagnostic_sha256",
        "external_call_started",
        "restricted_execution",
        "ephemeral_execution",
        "local_model_used",
        "test_invoker_used",
        "public_synthetic",
        "release_eligible",
        "receipt_hmac_sha256",
    }
    for run_ordinal in range(1, RUN_COUNT + 1):
        run_root = calls_root / f"run-{run_ordinal}"
        _private_directory(run_root)
        expected_unit_names = {f"unit-{unit.unit_ordinal:03d}" for unit in units}
        if {path.name for path in run_root.iterdir()} != expected_unit_names:
            raise PublicChallengeError("public challenge call unit coverage is invalid")
        for unit in units:
            entry = plan_calls[(run_ordinal, unit.unit_ordinal)]
            call_root = _call_path(output_root, run_ordinal, unit.unit_ordinal)
            _private_directory(call_root)
            if {path.name for path in call_root.iterdir()} != {
                "request.json",
                "started.json",
                "response.json",
                "receipt.json",
            }:
                raise PublicChallengeError(
                    "public challenge call evidence is incomplete"
                )
            request_raw = _private_file(call_root / "request.json")
            request = _strict_json(request_raw, label="public verifier request")
            if request != unit.request or _sha256(request_raw) != unit.request_sha256:
                raise PublicChallengeError("public verifier request authority is stale")
            start, start_raw = _load_signed_artifact(
                call_root / "started.json",
                key=key,
                domain=CALL_START_DOMAIN,
                signature_field="start_hmac_sha256",
                label="public verifier call start",
            )
            expected_common = {
                "challenge_id": challenge["challenge_id"],
                "run_ordinal": run_ordinal,
                "logical_run_id": entry["logical_run_id"],
                "unit_ordinal": unit.unit_ordinal,
                "call_id": entry["call_id"],
                "provider": claims.provider,
                "model": GMAIL_TEMPORAL_VERIFIER_MODEL,
                "reasoning_effort": GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
                "request_sha256": unit.request_sha256,
                "external_call_started": claims.external_call_started,
                "restricted_execution": claims.restricted_execution,
                "ephemeral_execution": claims.ephemeral_execution,
                "local_model_used": claims.local_model_used,
                "test_invoker_used": claims.test_invoker_used,
                "public_synthetic": True,
                "release_eligible": False,
            }
            if (
                set(start) != start_keys
                or start.get("version") != CALL_START_VERSION
                or any(
                    start.get(name) != value for name, value in expected_common.items()
                )
                or _aware_timestamp(start.get("started_at")) is None
            ):
                raise PublicChallengeError("public verifier call start is invalid")
            response_raw = _private_file(call_root / "response.json")
            response = _strict_json(response_raw, label="public verifier response")
            case_pages = _validate_response(unit, response)
            receipt, receipt_raw = _load_signed_artifact(
                call_root / "receipt.json",
                key=key,
                domain=CALL_RECEIPT_DOMAIN,
                signature_field="receipt_hmac_sha256",
                label="public verifier call receipt",
            )
            started_at = _aware_timestamp(start["started_at"])
            completed_at = _aware_timestamp(receipt.get("completed_at"))
            if (
                set(receipt) != receipt_keys
                or receipt.get("version") != CALL_RECEIPT_VERSION
                or any(
                    receipt.get(name) != value
                    for name, value in expected_common.items()
                )
                or receipt.get("started_at") != start["started_at"]
                or completed_at is None
                or started_at is None
                or completed_at < started_at
                or receipt.get("response_sha256") != _sha256(response_raw)
                or receipt.get("status") != "success"
                or receipt.get("error_type") is not None
                or receipt.get("diagnostic_sha256") is not None
            ):
                raise PublicChallengeError("public verifier call receipt is invalid")
            completed.append(
                _CompletedCall(
                    run_ordinal=run_ordinal,
                    unit_ordinal=unit.unit_ordinal,
                    call_id=str(entry["call_id"]),
                    logical_run_id=str(entry["logical_run_id"]),
                    started_at=str(start["started_at"]),
                    completed_at=str(receipt["completed_at"]),
                    request_sha256=unit.request_sha256,
                    response_sha256=_sha256(response_raw),
                    start_sha256=_sha256(start_raw),
                    receipt_sha256=_sha256(receipt_raw),
                    response=response,
                    case_pages=case_pages,
                )
            )
    return tuple(completed)


def _validate_component_evidence(
    *,
    output_root: Path,
    challenge: Mapping[str, Any],
    cases: Sequence[_Case],
    calls: Sequence[_CompletedCall],
    plan: Mapping[str, Any],
    source_provenance: _PredictionSourceProvenance,
) -> tuple[dict[str, tuple[Path, ...]], dict[str, Any]]:
    import pkm_brain.gmail_temporal_runner as production_runner

    by_run_case: dict[tuple[int, str], _CompletedCall] = {}
    for call in calls:
        for case_id in call.case_pages:
            key = (call.run_ordinal, case_id)
            if key in by_run_case:
                raise PublicChallengeError("case spans multiple calls in one run")
            by_run_case[key] = call
    components_root = output_root / "components"
    candidate_cases = [case for case in cases if case.preparation.requests]
    paths = BrainPaths.from_value(str(challenge["brain_home"]))
    authorities = {
        case.case_id: production_runner._build_authority(  # noqa: SLF001
            paths,
            document_id=case.document_id,
            gmail_message_id=case.gmail_message_id,
        )
        for case in cases
    }
    if not candidate_cases:
        if components_root.exists() or components_root.is_symlink():
            raise PublicChallengeError("zero-work challenge fabricated components")
        return ({case.case_id: () for case in cases}, authorities)
    _private_directory(components_root)
    if {path.name for path in components_root.iterdir()} != {
        case.case_id for case in candidate_cases
    }:
        raise PublicChallengeError("public component case coverage is invalid")
    output: dict[str, tuple[Path, ...]] = {}
    raw_plan_cases = plan.get("cases")
    if not isinstance(raw_plan_cases, list) or len(raw_plan_cases) != len(cases):
        raise PublicChallengeError("public component plan coverage is invalid")
    for case, raw_plan_case in zip(cases, raw_plan_cases, strict=True):
        if not isinstance(raw_plan_case, Mapping):
            raise PublicChallengeError("public component plan case is invalid")
        authority = authorities[case.case_id]
        if not case.preparation.requests:
            output[case.case_id] = ()
            continue
        case_root = components_root / case.case_id
        _private_directory(case_root)
        expected_names = {f"run-{value}.json" for value in range(1, RUN_COUNT + 1)}
        if {path.name for path in case_root.iterdir()} != expected_names:
            raise PublicChallengeError("public component run coverage is invalid")
        case_paths: list[Path] = []
        for run_ordinal in range(1, RUN_COUNT + 1):
            call = by_run_case.get((run_ordinal, case.case_id))
            if call is None:
                raise PublicChallengeError("public component call authority is missing")
            path = case_root / f"run-{run_ordinal}.json"
            raw = _private_file(path)
            value = _strict_json(raw, label="public verifier component")
            if value != _component_value(
                case,
                call,
                archived_plan_case=(
                    raw_plan_case
                    if source_provenance.trust_basis != "current_scorer_source"
                    else None
                ),
            ):
                raise PublicChallengeError("public verifier component is stale")
            case_paths.append(path)
        if source_provenance.trust_basis == "current_scorer_source":
            production_runner._load_components(  # noqa: SLF001
                tuple(case_paths), authority=authority
            )
        output[case.case_id] = tuple(case_paths)
    return output, authorities


def _validate_seal_authority(
    seal: Mapping[str, Any],
    *,
    key: bytes,
    challenge: Mapping[str, Any],
    challenge_raw: bytes,
    plan_raw: bytes,
    cases: Sequence[_Case],
    calls: Sequence[_CompletedCall],
    components: Mapping[str, tuple[Path, ...]],
    claims: _ExecutionClaims,
) -> None:
    expected = _prediction_seal(
        key=key,
        challenge=challenge,
        challenge_raw=challenge_raw,
        plan_raw=plan_raw,
        cases=cases,
        calls=calls,
        components=components,
        claims=claims,
    )
    comparable = dict(seal)
    expected_comparable = dict(expected)
    for value in (comparable, expected_comparable):
        value.pop("seal_hmac_sha256", None)
        value.pop("sealed_at", None)
    if (
        comparable != expected_comparable
        or _aware_timestamp(seal.get("sealed_at")) is None
    ):
        raise PublicChallengeError("prediction seal authority is invalid")


def _validate_persisted_results(
    result: Mapping[str, Any],
    *,
    challenge: Mapping[str, Any],
    challenge_raw: bytes,
    plan: Mapping[str, Any],
    plan_raw: bytes,
    seal_raw: bytes,
    cases: Sequence[_Case],
    components: Mapping[str, tuple[Path, ...]],
    calls: Sequence[_CompletedCall],
    claims: _ExecutionClaims,
) -> dict[str, Mapping[str, Any]]:
    result_keys = {
        "version",
        "launcher_version",
        "challenge_id",
        "challenge_manifest_sha256",
        "plan_sha256",
        "prediction_seal_sha256",
        "gold_sha256_committed_but_not_opened",
        "gold_accessed",
        "provider",
        "model",
        "reasoning_effort",
        "invocation_count",
        "external_call_count",
        "results",
        "public_synthetic",
        "contains_private_gmail",
        "release_eligible",
        "restricted_execution",
        "ephemeral_execution",
        "local_model_used",
        "test_invoker_used",
        "complete",
        "completed_at",
        "result_hmac_sha256",
    }
    stable = {
        "version": RESULT_VERSION,
        "launcher_version": VERSION,
        "challenge_id": challenge["challenge_id"],
        "challenge_manifest_sha256": _sha256(challenge_raw),
        "plan_sha256": _sha256(plan_raw),
        "prediction_seal_sha256": _sha256(seal_raw),
        "gold_sha256_committed_but_not_opened": challenge["gold_sha256"],
        "gold_accessed": False,
        "provider": claims.provider,
        "model": GMAIL_TEMPORAL_VERIFIER_MODEL,
        "reasoning_effort": GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
        "invocation_count": len(calls),
        "external_call_count": len(calls) if claims.external_call_started else 0,
        "public_synthetic": True,
        "contains_private_gmail": False,
        "release_eligible": False,
        "restricted_execution": claims.restricted_execution,
        "ephemeral_execution": claims.ephemeral_execution,
        "local_model_used": claims.local_model_used,
        "test_invoker_used": claims.test_invoker_used,
        "complete": True,
    }
    if (
        set(result) != result_keys
        or any(result.get(name) != value for name, value in stable.items())
        or _aware_timestamp(result.get("completed_at")) is None
    ):
        raise PublicChallengeError("public challenge result authority is invalid")
    raw_rows = result.get("results")
    if not isinstance(raw_rows, list) or len(raw_rows) != len(cases):
        raise PublicChallengeError("prediction result case coverage is invalid")
    by_case: dict[str, Mapping[str, Any]] = {}
    paths = BrainPaths.from_value(str(challenge["brain_home"]))
    raw_plan_cases = plan.get("cases")
    if not isinstance(raw_plan_cases, list) or len(raw_plan_cases) != len(cases):
        raise PublicChallengeError("prediction result plan coverage is invalid")
    with connection(paths.sqlite_path) as conn:
        for case, raw_row, raw_plan_case in zip(
            cases,
            raw_rows,
            raw_plan_cases,
            strict=True,
        ):
            if (
                not isinstance(raw_row, Mapping)
                or set(raw_row) != {"case_id", "runner_result", "projection"}
                or raw_row.get("case_id") != case.case_id
                or not isinstance(raw_row.get("runner_result"), Mapping)
                or not isinstance(raw_plan_case, Mapping)
            ):
                raise PublicChallengeError("prediction result case schema is invalid")
            runner = raw_row["runner_result"]
            expected_runner_keys = {
                "version",
                "disposition",
                "message_scope_key",
                "admission_basis",
                "expression_count",
                "batch_count",
                "candidate_count",
                "page_count",
                "component_count",
                "artifact_count",
                "cluster_review_count",
                "group_count",
                "persisted",
                "head_cleared",
                "run_id",
                "head_generation",
                "execution_id",
                "replayed",
                "head_changed",
                "independent_invocations_verified",
                "private_content_printed",
                "routable",
            }
            projection = raw_row.get("projection")
            artifacts = (
                projection.get("artifacts", [])
                if isinstance(projection, Mapping)
                else []
            )
            reviews = (
                projection.get("cluster_reviews", [])
                if isinstance(projection, Mapping)
                else []
            )
            groups = (
                projection.get("groups", []) if isinstance(projection, Mapping) else []
            )
            preparation = case.preparation
            expected_runner = {
                "version": GMAIL_TEMPORAL_RUNNER_VERSION,
                "disposition": preparation.disposition,
                "message_scope_key": preparation.message_scope_key,
                "admission_basis": preparation.admission_basis,
                "expression_count": preparation.expression_count,
                "batch_count": preparation.batch_count,
                "candidate_count": preparation.candidate_count,
                "page_count": preparation.page_count,
                "component_count": RUN_COUNT if components[case.case_id] else 0,
                "artifact_count": len(artifacts),
                "cluster_review_count": len(reviews),
                "group_count": len(groups),
                "persisted": True,
                "independent_invocations_verified": False,
                "private_content_printed": False,
                "routable": False,
            }
            if (
                set(runner) != expected_runner_keys
                or any(
                    runner.get(name) != value for name, value in expected_runner.items()
                )
                or (
                    runner.get("head_generation") is not None
                    and (
                        not isinstance(runner.get("head_generation"), int)
                        or isinstance(runner.get("head_generation"), bool)
                    )
                )
                or not isinstance(runner.get("execution_id"), str)
            ):
                raise PublicChallengeError("production runner result is invalid")
            execution = conn.execute(
                "SELECT * FROM gmail_temporal_review_executions WHERE id = ?",
                (runner["execution_id"],),
            ).fetchone()
            if execution is None or (
                execution["message_scope_key"] != preparation.message_scope_key
                or execution["pipeline_scope"] != GMAIL_TEMPORAL_PIPELINE_SCOPE
                or execution["document_id"] != case.document_id
                or execution["source_sha256"] != preparation.source_sha256
                or execution["runner_policy_fingerprint"]
                != raw_plan_case.get("runner_policy_fingerprint")
                or execution["admission_policy_fingerprint"]
                != raw_plan_case.get("admission_policy_fingerprint")
                or execution["verifier_policy_fingerprint"]
                != raw_plan_case.get("verifier_policy_fingerprint")
                or execution["target_fingerprint"]
                != raw_plan_case.get("target_fingerprint")
                or execution["analysis_fingerprint"]
                != raw_plan_case.get("analysis_fingerprint")
                or execution["batch_plan_fingerprint"]
                != raw_plan_case.get("batch_plan_fingerprint")
                or execution["provider"] != GMAIL_TEMPORAL_EXTERNAL_PROVIDER
                or execution["model"] != GMAIL_TEMPORAL_VERIFIER_MODEL
                or execution["reasoning_effort"]
                != GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT
                or execution["review_run_id"] != runner.get("run_id")
                or execution["component_count"] != expected_runner["component_count"]
            ):
                raise PublicChallengeError("production execution receipt is stale")
            component_rows = conn.execute(
                """
                SELECT run_ordinal, invocation_id, started_at, completed_at,
                       artifact_sha256, payload_json
                FROM gmail_temporal_review_components
                WHERE execution_id = ? ORDER BY run_ordinal
                """,
                (runner["execution_id"],),
            ).fetchall()
            expected_component_rows = []
            for ordinal, path in enumerate(components[case.case_id], start=1):
                raw = _private_file(path)
                value = _strict_json(raw, label="persisted verifier component")
                expected_component_rows.append(
                    (
                        ordinal,
                        value["invocation_id"],
                        value["started_at"],
                        value["completed_at"],
                        _sha256(raw),
                        raw.decode("utf-8"),
                    )
                )
            if [tuple(row) for row in component_rows] != expected_component_rows:
                raise PublicChallengeError("persisted component evidence is stale")
            head = conn.execute(
                """
                SELECT run_id, generation FROM gmail_temporal_review_heads
                WHERE message_scope_key = ? AND pipeline_scope = ?
                """,
                (preparation.message_scope_key, GMAIL_TEMPORAL_PIPELINE_SCOPE),
            ).fetchone()
            if runner.get("head_generation") is None:
                if head is not None:
                    raise PublicChallengeError("production temporal head is stale")
            elif (
                head is None
                or head["run_id"] != runner.get("run_id")
                or head["generation"] != runner.get("head_generation")
            ):
                raise PublicChallengeError("production temporal head is stale")
            run_id = runner.get("run_id")
            if run_id is None:
                if projection is not None or components[case.case_id]:
                    raise PublicChallengeError(
                        "zero-work result fabricated projection evidence"
                    )
            else:
                if not isinstance(projection, Mapping):
                    raise PublicChallengeError("persisted projection is invalid")
                stored = conn.execute(
                    "SELECT projection_json FROM gmail_temporal_review_runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
                if (
                    stored is None
                    or json.loads(str(stored["projection_json"])) != projection
                ):
                    raise PublicChallengeError("persisted projection is stale")
            by_case[case.case_id] = raw_row
    return by_case


def _artifact_hypotheses(artifact: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    values = artifact.get("hypotheses")
    if not isinstance(values, list):
        return []
    return [item for item in values if isinstance(item, Mapping)]


def _normalized_subject(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).casefold()
    return normalized or None


_SUBJECT_IDENTITY_WRAPPERS = frozenset(
    {
        "cancellation",
        "cancelled",
        "completed",
        "confirm",
        "confirmation",
        "confirmed",
        "moved",
        "reminder",
        "rescheduled",
        "scheduled",
        "update",
    }
)
_SUBJECT_EVENT_HEADS = frozenset(
    {
        "appointment",
        "booking",
        "call",
        "ceremony",
        "class",
        "conference",
        "concert",
        "debrief",
        "demo",
        "delivery",
        "dinner",
        "event",
        "exam",
        "flight",
        "forum",
        "hearing",
        "interview",
        "launch",
        "meeting",
        "offsite",
        "orientation",
        "party",
        "pickup",
        "presentation",
        "reservation",
        "review",
        "screening",
        "session",
        "stay",
        "summit",
        "tour",
        "training",
        "trip",
        "visit",
        "webinar",
        "workshop",
    }
)
_SUBJECT_EVENT_DESCRIPTORS = _SUBJECT_EVENT_HEADS | {
    "design",
    "hiring",
    "planning",
    "project",
}


@dataclass(frozen=True)
class _SubjectIdentity:
    tokens: tuple[str, ...]
    prefix: tuple[str, ...]
    event_head: str | None


def _subject_identity(value: Any) -> _SubjectIdentity | None:
    normalized = _normalized_subject(value)
    if normalized is None:
        return None
    tokens = list(re.findall(r"[a-z0-9]+", normalized))
    while tokens and tokens[0] in _SUBJECT_IDENTITY_WRAPPERS:
        tokens.pop(0)
    while tokens and tokens[-1] in _SUBJECT_IDENTITY_WRAPPERS:
        tokens.pop()
    if not tokens:
        return None
    event_head = tokens[-1] if tokens[-1] in _SUBJECT_EVENT_HEADS else None
    prefix = tuple(tokens[:-1] if event_head is not None else tokens)
    return _SubjectIdentity(
        tokens=tuple(tokens),
        prefix=prefix,
        event_head=event_head,
    )


def _subject_prefix_expands_only_by_event_descriptors(
    shorter: tuple[str, ...],
    longer: tuple[str, ...],
) -> bool:
    return bool(
        shorter
        and len(shorter) <= len(longer)
        and longer[: len(shorter)] == shorter
        and all(token in _SUBJECT_EVENT_DESCRIPTORS for token in longer[len(shorter) :])
    )


def _subject_prefixes_are_compatible(
    first: tuple[str, ...],
    second: tuple[str, ...],
) -> bool:
    return _subject_prefix_expands_only_by_event_descriptors(
        first,
        second,
    ) or _subject_prefix_expands_only_by_event_descriptors(second, first)


def _subject_identity_tokens(value: Any) -> set[str]:
    identity = _subject_identity(value)
    return set(identity.tokens) if identity is not None else set()


def _subject_matches(expected: str, actual: str) -> bool:
    if _normalized_subject(expected) == _normalized_subject(actual):
        return True
    expected_identity = _subject_identity(expected)
    actual_identity = _subject_identity(actual)
    if expected_identity is None or actual_identity is None:
        return False
    if (
        expected_identity.event_head is not None
        and actual_identity.event_head is not None
        and expected_identity.event_head != actual_identity.event_head
    ):
        return False
    if expected_identity.event_head is None and actual_identity.event_head is None:
        return expected_identity.tokens == actual_identity.tokens
    if expected_identity.event_head == actual_identity.event_head:
        return _subject_prefixes_are_compatible(
            expected_identity.prefix,
            actual_identity.prefix,
        )
    headed, base = (
        (expected_identity, actual_identity)
        if expected_identity.event_head is not None
        else (actual_identity, expected_identity)
    )
    return _subject_prefix_expands_only_by_event_descriptors(
        base.tokens,
        headed.prefix,
    )


def _authority_subject_surfaces(authority: Any) -> dict[str, str]:
    text = authority.source.text
    output: dict[str, str] = {}
    for mention in authority.analysis.mentions:
        if mention.start < 0 or mention.end <= mention.start or mention.end > len(text):
            raise PublicChallengeError("production subject authority is invalid")
        output[mention.mention_id] = text[mention.start : mention.end]
    return output


_PROJECTION_VERSION = "gmail_temporal_review_projection_v3"
_LEGACY_PROJECTION_VERSION = "gmail_temporal_review_projection_v2"


def _authority_parent_cluster_subject_ids(
    authority: Any,
) -> dict[str, frozenset[str]]:
    """Expose the authenticated parent-cluster aliases used by legacy V2."""

    output: dict[str, frozenset[str]] = {}
    for batch in authority.batches:
        for page in batch.page_plan.pages:
            for cluster in page.clusters:
                subject_ids = frozenset(cluster.subject_mention_ids)
                if not subject_ids:
                    raise PublicChallengeError(
                        "production parent subject cluster is invalid"
                    )
                previous = output.setdefault(cluster.cluster_id, subject_ids)
                if previous != subject_ids:
                    raise PublicChallengeError(
                        "production parent subject cluster is unstable"
                    )
    return output


def _v3_artifact_subject_aliases(
    projection: Mapping[str, Any],
    *,
    subject_surfaces: Mapping[str, str],
) -> dict[str, frozenset[str]]:
    """Use only alias identity metadata exported by the authenticated V3 run."""

    artifacts = projection.get("artifacts")
    if not isinstance(artifacts, list):
        raise PublicChallengeError("public projection artifacts are invalid")
    output: dict[str, frozenset[str]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            raise PublicChallengeError("public projection artifact is invalid")
        artifact_id = artifact.get("artifact_id")
        hypotheses = _artifact_hypotheses(artifact)
        if not isinstance(artifact_id, str) or not artifact_id or not hypotheses:
            raise PublicChallengeError("public projection artifact is invalid")
        family_ids: tuple[str, ...] | None = None
        for hypothesis in hypotheses:
            selected_ids = hypothesis.get("subject_mention_ids")
            alias_ids = hypothesis.get("subject_alias_mention_ids")
            type_references = hypothesis.get("subject_alias_type_references")
            canonical_id = hypothesis.get("canonical_subject_mention_id")
            if (
                not isinstance(selected_ids, list)
                or not selected_ids
                or any(
                    not isinstance(value, str) or not value for value in selected_ids
                )
                or len(selected_ids) != len(set(selected_ids))
                or not isinstance(alias_ids, list)
                or not alias_ids
                or any(not isinstance(value, str) or not value for value in alias_ids)
                or alias_ids != sorted(alias_ids)
                or len(alias_ids) != len(set(alias_ids))
                or not set(selected_ids).issubset(alias_ids)
                or not isinstance(type_references, list)
                or len(type_references) != len(alias_ids)
                or canonical_id is not None
                and (not isinstance(canonical_id, str) or canonical_id not in alias_ids)
            ):
                raise PublicChallengeError(
                    "public projection subject alias metadata is invalid"
                )
            alias_types: dict[str, str] = {}
            for reference in type_references:
                if (
                    not isinstance(reference, list)
                    or len(reference) != 2
                    or not isinstance(reference[0], str)
                    or not reference[0]
                    or not isinstance(reference[1], str)
                    or not reference[1]
                    or reference[0] in alias_types
                ):
                    raise PublicChallengeError(
                        "public projection subject alias metadata is invalid"
                    )
                alias_types[reference[0]] = reference[1]
            if list(alias_types) != alias_ids or (
                canonical_id is not None
                and alias_types.get(canonical_id) != "event_title_candidate"
            ):
                raise PublicChallengeError(
                    "public projection subject alias metadata is invalid"
                )
            current_ids = tuple(alias_ids)
            if family_ids is None:
                family_ids = current_ids
            elif family_ids != current_ids:
                raise PublicChallengeError(
                    "public artifact spans incompatible subject alias families"
                )
        assert family_ids is not None
        if any(
            not isinstance(subject_surfaces.get(mention_id), str)
            or not subject_surfaces[mention_id]
            for mention_id in family_ids
        ):
            raise PublicChallengeError(
                "public projection subject alias authority is incomplete"
            )
        aliases = frozenset(subject_surfaces[mention_id] for mention_id in family_ids)
        output[artifact_id] = aliases
    return output


def _legacy_artifact_subject_aliases(
    projection: Mapping[str, Any],
    *,
    subject_surfaces: Mapping[str, str],
    parent_cluster_subject_ids: Mapping[str, frozenset[str]],
) -> dict[str, frozenset[str]]:
    """Reconstruct V2 aliases from exact authenticated parent clusters only."""

    artifacts = {
        str(item.get("artifact_id")): item
        for item in projection.get("artifacts", [])
        if isinstance(item, Mapping) and isinstance(item.get("artifact_id"), str)
    }
    analysis_fingerprint = projection.get("analysis_fingerprint")
    if not isinstance(analysis_fingerprint, str) or not analysis_fingerprint:
        return {}
    output: dict[str, frozenset[str]] = {}
    for group in projection.get("groups", []):
        if not isinstance(group, Mapping) or group.get("coverage") != "complete":
            continue
        family_id = group.get("subject_family_id")
        members = group.get("members")
        if (
            not isinstance(family_id, str)
            or re.fullmatch(r"gtrsf_[0-9a-f]{64}", family_id) is None
            or not isinstance(members, list)
            or not members
        ):
            continue
        artifact_ids: list[str] = []
        group_is_exact = True
        for member in members:
            if (
                not isinstance(member, Mapping)
                or member.get("state") != "present"
                or member.get("subject_family_ids") != [family_id]
                or member.get("cluster_review_ids") not in ([], ())
                or member.get("reasons") not in ([], ())
                or not isinstance(member.get("artifact_ids"), list)
                or not member["artifact_ids"]
                or any(
                    not isinstance(artifact_id, str) or artifact_id not in artifacts
                    for artifact_id in member["artifact_ids"]
                )
            ):
                group_is_exact = False
                break
            artifact_ids.extend(member["artifact_ids"])
        if not group_is_exact or len(artifact_ids) != len(set(artifact_ids)):
            continue
        family_mention_ids: set[str] = set()
        for artifact_id in artifact_ids:
            artifact = artifacts[artifact_id]
            parent_cluster_id = artifact.get("parent_cluster_id")
            cluster_ids = (
                parent_cluster_subject_ids.get(parent_cluster_id, frozenset())
                if isinstance(parent_cluster_id, str)
                else frozenset()
            )
            hypotheses = _artifact_hypotheses(artifact)
            selected_ids = {
                mention_id
                for hypothesis in hypotheses
                for mention_id in hypothesis.get("subject_mention_ids", [])
                if isinstance(mention_id, str)
            }
            if (
                not cluster_ids
                or not hypotheses
                or not selected_ids
                or not selected_ids.issubset(cluster_ids)
            ):
                group_is_exact = False
                break
            family_mention_ids.update(cluster_ids)
        expected_family_id = (
            "gtrsf_"
            + hashlib.sha256(
                _canonical_json(
                    {
                        "analysis_fingerprint": analysis_fingerprint,
                        "subject_mention_ids": sorted(family_mention_ids),
                    }
                )
            ).hexdigest()
        )
        surfaces_are_complete = all(
            isinstance(subject_surfaces.get(mention_id), str)
            and bool(subject_surfaces[mention_id])
            for mention_id in family_mention_ids
        )
        aliases = frozenset(
            subject_surfaces[mention_id]
            for mention_id in family_mention_ids
            if mention_id in subject_surfaces
        )
        if (
            not group_is_exact
            or family_id != expected_family_id
            or not surfaces_are_complete
        ):
            continue
        for artifact_id in artifact_ids:
            previous = output.setdefault(artifact_id, aliases)
            if previous != aliases:
                raise PublicChallengeError(
                    "public artifact spans incompatible subject alias families"
                )
    return output


def _artifact_subject_aliases(
    projection: Mapping[str, Any],
    *,
    subject_surfaces: Mapping[str, str],
    parent_cluster_subject_ids: Mapping[str, frozenset[str]],
) -> dict[str, frozenset[str]]:
    version = projection.get("version")
    if version == _PROJECTION_VERSION:
        return _v3_artifact_subject_aliases(
            projection,
            subject_surfaces=subject_surfaces,
        )
    if version == _LEGACY_PROJECTION_VERSION:
        return _legacy_artifact_subject_aliases(
            projection,
            subject_surfaces=subject_surfaces,
            parent_cluster_subject_ids=parent_cluster_subject_ids,
        )
    raise PublicChallengeError("prediction projection version is unsupported")


def _hypothesis_matches_member(
    hypothesis: Mapping[str, Any],
    member: Mapping[str, Any],
    *,
    subject_surfaces: Mapping[str, str],
    subject_alias_surfaces: Sequence[str] = (),
) -> bool:
    expected_subject = _normalized_subject(member.get("subject"))
    mention_ids = hypothesis.get("subject_mention_ids")
    if expected_subject is None or not isinstance(mention_ids, list) or not mention_ids:
        return False
    actual_subjects = {
        surface
        for mention_id in mention_ids
        if isinstance(mention_id, str)
        and isinstance((surface := subject_surfaces.get(mention_id)), str)
    }
    actual_subjects.update(subject_alias_surfaces)
    return (
        any(_subject_matches(expected_subject, actual) for actual in actual_subjects)
        and hypothesis.get("relation") == member.get("relation")
        and hypothesis.get("lifecycle") == member.get("lifecycle")
    )


def _exact_artifact_match(
    artifact: Mapping[str, Any],
    member: Mapping[str, Any],
    *,
    subject_surfaces: Mapping[str, str],
    subject_alias_surfaces: Sequence[str] = (),
) -> bool:
    evidence_status = artifact.get("evidence_status")
    # Semantic correctness and verifier calibration are distinct estimands.  A
    # status disagreement must not erase an otherwise exact temporal binding
    # from recall, structure, canonical identity, or artifact precision.  The
    # scoring loop separately credits only supported output against supported
    # gold in ``supported_artifact_precision`` and ``confirmed_member_recall``.
    if evidence_status not in {"supported", "uncertain"}:
        return False
    hypotheses = _artifact_hypotheses(artifact)
    if not hypotheses or not all(
        _hypothesis_matches_member(
            hypothesis,
            member,
            subject_surfaces=subject_surfaces,
            subject_alias_surfaces=subject_alias_surfaces,
        )
        for hypothesis in hypotheses
    ):
        return False
    actual_values = {item.get("normalized_value") for item in hypotheses}
    if "values" in member:
        expected_values = member.get("values")
        return (
            isinstance(expected_values, list)
            and set(expected_values) == actual_values
            and len(actual_values) == len(expected_values)
        )
    return actual_values == {member.get("value")}


def _best_exact_artifact_id(
    artifacts: Mapping[str, Mapping[str, Any]],
    member: Mapping[str, Any],
    *,
    subject_surfaces: Mapping[str, str],
    artifact_subject_aliases: Mapping[str, Sequence[str]],
    excluded_artifact_ids: set[str],
) -> str | None:
    """Choose deterministically, preferring confidence calibration second."""

    expected_verdict = str(member.get("expected_verdict", "supported"))
    candidates = [
        artifact_id
        for artifact_id, artifact in artifacts.items()
        if artifact_id not in excluded_artifact_ids
        and _exact_artifact_match(
            artifact,
            member,
            subject_surfaces=subject_surfaces,
            subject_alias_surfaces=artifact_subject_aliases.get(artifact_id, ()),
        )
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda artifact_id: (
            artifacts[artifact_id].get("evidence_status") != expected_verdict,
            artifact_id,
        ),
    )


def _artifacts_confirm_supported_member(
    artifacts: Sequence[Mapping[str, Any]],
    member: Mapping[str, Any],
) -> bool:
    return bool(
        member.get("expected_verdict", "supported") != "uncertain"
        and artifacts
        and all(item.get("evidence_status") == "supported" for item in artifacts)
    )


def _supported_artifact_calibration(
    artifact: Mapping[str, Any],
    expected_verdict: str,
) -> str | None:
    """Classify a supported output without changing its semantic match."""

    if artifact.get("evidence_status") != "supported":
        return None
    return "calibrated" if expected_verdict == "supported" else "overconfident"


def _critical_temporal_categories_for_member(
    member: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return the release categories represented by a critical gold binding."""

    categories: list[str] = []
    lifecycle = member.get("lifecycle")
    if lifecycle in _CRITICAL_LIFECYCLE_ROLES:
        categories.append(str(lifecycle))
    if member.get("relation") == "deadline":
        categories.append("deadline")
    return tuple(categories)


def _artifact_has_critical_temporal_hypothesis(
    artifact: Mapping[str, Any],
) -> bool:
    """Return whether an output asserts a deadline or critical event lifecycle."""

    return any(
        hypothesis.get("relation") == "deadline"
        or hypothesis.get("lifecycle") in _CRITICAL_LIFECYCLE_ROLES
        for hypothesis in _artifact_hypotheses(artifact)
    )


def _review_artifact_metrics(
    *,
    artifact_count: int,
    matched_artifact_count: int,
    cluster_review_count: int,
    gold_member_count: int,
) -> tuple[float, bool]:
    """Score semantic artifacts and expose whether every review output was scored."""

    precision = (
        matched_artifact_count / artifact_count
        if artifact_count
        else (1.0 if gold_member_count == 0 else 0.0)
    )
    return precision, cluster_review_count == 0


def _wilson_interval(numerator: int, denominator: int) -> dict[str, Any]:
    """Return the two-sided 95% Wilson interval for an observed proportion."""

    if (
        isinstance(numerator, bool)
        or isinstance(denominator, bool)
        or numerator < 0
        or denominator < 0
        or numerator > denominator
    ):
        raise PublicChallengeError("public challenge metric count is invalid")
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


def _semantic_unit_key(member: Mapping[str, Any], member_ordinal: int) -> str:
    """Return the case-local semantic unit identity for one gold member."""

    if member.get("lifecycle") in {
        "rescheduled_old",
        "rescheduled_replacement",
    }:
        subject = _normalized_subject(member.get("subject"))
        if subject is None:
            raise PublicChallengeError("semantic unit subject is invalid")
        return f"reschedule:{subject}"
    if "values" in member:
        return f"alternatives:{member_ordinal}"
    return f"member:{member_ordinal}"


def _semantic_unit_metrics(
    members: Sequence[Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    """Score complete and exact case-local units from already matched members.

    A reschedule pair is one unit. One explicit alternatives member is one unit,
    because its matcher already requires every alternative in one complete
    production group. Every other member is a singleton. Exactness is stricter
    than completeness only where the frozen gold requires a canonical subject.
    """

    if len(members) != len(outcomes):
        raise PublicChallengeError("semantic unit outcome coverage is invalid")
    units: defaultdict[str, list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = (
        defaultdict(list)
    )
    for ordinal, (member, outcome) in enumerate(zip(members, outcomes, strict=True)):
        if (
            not isinstance(member, Mapping)
            or not isinstance(outcome, Mapping)
            or set(outcome) != {"matched", "exact", "structural_group_id"}
            or not isinstance(outcome.get("matched"), bool)
            or not isinstance(outcome.get("exact"), bool)
            or outcome.get("structural_group_id") is not None
            and not isinstance(outcome.get("structural_group_id"), str)
        ):
            raise PublicChallengeError("semantic unit outcome is invalid")
        units[_semantic_unit_key(member, ordinal)].append((member, outcome))

    complete_units = 0
    exact_units = 0
    for rows in units.values():
        requires_group = any(
            "values" in member
            or member.get("lifecycle") in {"rescheduled_old", "rescheduled_replacement"}
            for member, _ in rows
        )
        group_ids = {
            str(outcome["structural_group_id"])
            for _, outcome in rows
            if outcome.get("structural_group_id") is not None
        }
        coherent_group = not requires_group or (
            len(group_ids) == 1
            and all(
                outcome.get("structural_group_id") is not None for _, outcome in rows
            )
        )
        complete = coherent_group and all(
            bool(outcome["matched"]) for _, outcome in rows
        )
        exact = complete and all(bool(outcome["exact"]) for _, outcome in rows)
        complete_units += int(complete)
        exact_units += int(exact)
    return {
        "semantic_units": len(units),
        "complete_units": complete_units,
        "exact_units": exact_units,
    }


def _reschedule_unit_metrics(
    members: Sequence[Mapping[str, Any]],
    outcomes: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    """Score old/replacement pairs as coherent lifecycle units."""

    if len(members) != len(outcomes):
        raise PublicChallengeError("reschedule unit outcome coverage is invalid")
    units: defaultdict[str, list[tuple[Mapping[str, Any], Mapping[str, Any]]]] = (
        defaultdict(list)
    )
    for member, outcome in zip(members, outcomes, strict=True):
        if member.get("lifecycle") not in {
            "rescheduled_old",
            "rescheduled_replacement",
        }:
            continue
        subject = _normalized_subject(member.get("subject"))
        if subject is None:
            raise PublicChallengeError("reschedule unit subject is invalid")
        units[subject].append((member, outcome))

    complete_units = 0
    for rows in units.values():
        roles = {str(member.get("lifecycle")) for member, _ in rows}
        group_ids = {
            str(outcome["structural_group_id"])
            for _, outcome in rows
            if outcome.get("structural_group_id") is not None
        }
        complete_units += int(
            roles == {"rescheduled_old", "rescheduled_replacement"}
            and len(group_ids) == 1
            and all(
                outcome.get("matched") is True
                and outcome.get("structural_group_id") is not None
                for _, outcome in rows
            )
        )
    return {
        "reschedule_units": len(units),
        "complete_reschedule_units": complete_units,
    }


def _pairwise_jaccard(
    values: tuple[set[Any], set[Any], set[Any]],
) -> tuple[list[dict[str, Any]], float]:
    rows: list[dict[str, Any]] = []
    for first, second in ((0, 1), (0, 2), (1, 2)):
        intersection = values[first] & values[second]
        union = values[first] | values[second]
        jaccard = len(intersection) / len(union) if union else 1.0
        rows.append(
            {
                "first_run": first + 1,
                "second_run": second + 1,
                "first_count": len(values[first]),
                "second_count": len(values[second]),
                "intersection_count": len(intersection),
                "union_count": len(union),
                "jaccard": jaccard,
            }
        )
    return rows, min(float(row["jaccard"]) for row in rows)


def _raw_candidate_verdicts_by_run(
    calls: Sequence[_CompletedCall],
) -> tuple[
    dict[tuple[str, str], str],
    dict[tuple[str, str], str],
    dict[tuple[str, str], str],
]:
    output: dict[int, dict[tuple[str, str], str]] = {
        ordinal: {} for ordinal in range(1, RUN_COUNT + 1)
    }
    for call in calls:
        if call.run_ordinal not in output:
            raise PublicChallengeError("public stability run identity is invalid")
        verdicts = output[call.run_ordinal]
        for case_id, pages in call.case_pages.items():
            for page in pages:
                for row in page.get("verdicts", []):
                    if not isinstance(row, Mapping):
                        raise PublicChallengeError(
                            "public stability verdict is invalid"
                        )
                    candidate_id = row.get("candidate_id")
                    verdict = row.get("verdict")
                    key = (case_id, str(candidate_id or ""))
                    if (
                        not isinstance(candidate_id, str)
                        or not candidate_id
                        or verdict not in {"supported", "uncertain", "unsupported"}
                        or key in verdicts
                    ):
                        raise PublicChallengeError(
                            "public stability verdict authority is invalid"
                        )
                    verdicts[key] = str(verdict)
    key_sets = [set(output[ordinal]) for ordinal in range(1, RUN_COUNT + 1)]
    if any(values != key_sets[0] for values in key_sets[1:]):
        raise PublicChallengeError("public stability candidate coverage differs")
    return tuple(output[ordinal] for ordinal in range(1, RUN_COUNT + 1))  # type: ignore[return-value]


def _public_candidate_authority(
    authorities: Mapping[str, Any],
) -> dict[tuple[str, str], _PublicCandidateAuthority]:
    output: dict[tuple[str, str], _PublicCandidateAuthority] = {}
    for case_id, authority in authorities.items():
        subject_surfaces = _authority_subject_surfaces(authority)
        candidates = tuple(
            candidate
            for batch in authority.batches
            for candidate in batch.frontier_candidates
        )
        try:
            family_ids = production_review._subject_alias_families(  # noqa: SLF001
                analysis=authority.analysis,
                batches=authority.batch_plan.batches,
                candidates=candidates,
            )
        except production_review.GmailTemporalReviewError as exc:
            raise PublicChallengeError(
                "public stability alias authority is invalid"
            ) from exc
        family_surfaces: defaultdict[str, set[str]] = defaultdict(set)
        for mention_id, family_id in family_ids.items():
            surface = subject_surfaces.get(mention_id)
            if isinstance(surface, str) and surface:
                family_surfaces[family_id].add(surface)
        family_members = production_review._subject_alias_family_members(  # noqa: SLF001
            family_ids
        )
        subject_types = {
            mention.mention_id: mention.mention_type
            for mention in authority.analysis.mentions
        }

        cluster_by_candidate: dict[str, str] = {}
        for batch in authority.batches:
            for page in batch.page_plan.pages:
                for cluster in page.clusters:
                    for candidate_id in cluster.candidate_ids:
                        previous = cluster_by_candidate.setdefault(
                            candidate_id,
                            cluster.cluster_id,
                        )
                        if previous != cluster.cluster_id:
                            raise PublicChallengeError(
                                "public stability parent cluster is ambiguous"
                            )
        for candidate in candidates:
            candidate_id = candidate.candidate_id
            cluster_id = cluster_by_candidate.get(candidate_id)
            surface = subject_surfaces.get(candidate.subject_mention_id)
            if not isinstance(cluster_id, str) or not cluster_id or not surface:
                raise PublicChallengeError(
                    "public stability candidate authority is incomplete"
                )
            aliases = {surface}
            family_id = family_ids.get(candidate.subject_mention_id)
            if family_id is not None:
                aliases.update(family_surfaces.get(family_id, ()))
            try:
                _, _, canonical_id = production_review._subject_identity_metadata(  # noqa: SLF001
                    subject_mention_ids=(candidate.subject_mention_id,),
                    subject_types_by_id=subject_types,
                    subject_families=family_ids,
                    subject_family_members=family_members,
                )
            except production_review.GmailTemporalReviewError as exc:
                raise PublicChallengeError(
                    "public stability canonical authority is invalid"
                ) from exc
            canonical_surface = (
                subject_surfaces.get(canonical_id)
                if isinstance(canonical_id, str)
                else None
            )
            key = (case_id, candidate_id)
            if key in output:
                raise PublicChallengeError(
                    "public stability candidate authority is duplicated"
                )
            output[key] = _PublicCandidateAuthority(
                candidate=candidate,
                subject_alias_surfaces=frozenset(aliases),
                canonical_subject_surface=canonical_surface,
                parent_cluster_id=cluster_id,
            )
    return output


def _candidate_matches_gold_member_value(
    authority: _PublicCandidateAuthority,
    member: Mapping[str, Any],
    expected_value: str,
) -> bool:
    candidate = authority.candidate
    expected_subject = _normalized_subject(member.get("subject"))
    return bool(
        expected_subject is not None
        and candidate.relation == member.get("relation")
        and candidate.lifecycle == member.get("lifecycle")
        and candidate.normalized_value == expected_value
        and any(
            _normalized_subject(surface) == expected_subject
            for surface in authority.subject_alias_surfaces
        )
        and (
            member.get("canonical_subject_required") is not True
            or _normalized_subject(authority.canonical_subject_surface)
            == expected_subject
        )
    )


def _recompute_frontier_gold_coverage(
    *,
    authorities: Mapping[str, Any],
    gold_rows: Mapping[str, Mapping[str, Any]],
    selected_ids: Sequence[str],
) -> dict[str, Any]:
    """Recompute member coverage from current production candidates and gold."""

    candidate_authority = _public_candidate_authority(authorities)
    by_case: defaultdict[str, list[_PublicCandidateAuthority]] = defaultdict(list)
    for (case_id, _candidate_id), authority in candidate_authority.items():
        by_case[case_id].append(authority)

    cases: list[dict[str, Any]] = []
    for case_id in selected_ids:
        members = gold_rows[case_id].get("members")
        if not isinstance(members, list):
            raise PublicChallengeError("public frontier gold is invalid")
        covered = 0
        for member in members:
            if not isinstance(member, Mapping):
                raise PublicChallengeError("public frontier gold member is invalid")
            raw_values = member.get("values")
            values = (
                [str(value) for value in raw_values]
                if isinstance(raw_values, list)
                else [str(member.get("value"))]
            )
            if values and all(
                any(
                    _candidate_matches_gold_member_value(
                        authority,
                        member,
                        expected_value,
                    )
                    for authority in by_case.get(case_id, ())
                )
                for expected_value in values
            ):
                covered += 1
        cases.append(
            {
                "case_id": case_id,
                "gold_members": len(members),
                "frontier_covered_gold_members": covered,
                "frontier_missing_gold_members": len(members) - covered,
            }
        )
    return {
        "gold_members": sum(row["gold_members"] for row in cases),
        "frontier_covered_gold_members": sum(
            row["frontier_covered_gold_members"] for row in cases
        ),
        "frontier_missing_gold_members": sum(
            row["frontier_missing_gold_members"] for row in cases
        ),
        "cases": cases,
    }


def _pairwise_verdict_agreement(
    verdicts_by_run: tuple[
        Mapping[Any, str],
        Mapping[Any, str],
        Mapping[Any, str],
    ],
    candidate_keys: Sequence[Any],
) -> tuple[list[dict[str, Any]], float]:
    """Compare exact supported/uncertain/unsupported calibration across runs."""

    rows: list[dict[str, Any]] = []
    for first, second in ((0, 1), (0, 2), (1, 2)):
        agreements = sum(
            verdicts_by_run[first][key] == verdicts_by_run[second][key]
            for key in candidate_keys
        )
        rows.append(
            {
                "first_run": first + 1,
                "second_run": second + 1,
                "candidate_count": len(candidate_keys),
                "agreement_count": agreements,
                "agreement": (
                    agreements / len(candidate_keys) if candidate_keys else 1.0
                ),
            }
        )
    return rows, min(float(row["agreement"]) for row in rows)


def _gold_member_verdict_status(
    *,
    verdicts: Mapping[tuple[str, str], str],
    case_candidates: Mapping[tuple[str, str], _PublicCandidateAuthority],
    member: Mapping[str, Any],
) -> str:
    """Collapse exact candidate verdicts to one confidence class per gold member."""

    raw_values = member.get("values")
    values = (
        [str(value) for value in raw_values]
        if isinstance(raw_values, list)
        else [str(member.get("value"))]
    )
    value_statuses: list[str] = []
    for expected_value in values:
        statuses = {
            verdicts[key]
            for key, authority in case_candidates.items()
            if _candidate_matches_gold_member_value(
                authority,
                member,
                expected_value,
            )
        }
        if "supported" in statuses:
            value_statuses.append("supported")
        elif "uncertain" in statuses:
            value_statuses.append("uncertain")
        else:
            value_statuses.append("unsupported")
    if value_statuses and all(value == "supported" for value in value_statuses):
        return "supported"
    if value_statuses and all(value != "unsupported" for value in value_statuses):
        return "uncertain"
    return "unsupported"


def _three_run_stability(
    *,
    calls: Sequence[_CompletedCall],
    authorities: Mapping[str, Any],
    gold_rows: Mapping[str, Mapping[str, Any]],
    selected_ids: Sequence[str],
) -> dict[str, Any]:
    """Derive alias-collapsed stability only from authenticated call evidence."""

    verdicts_by_run = _raw_candidate_verdicts_by_run(calls)
    candidate_authority = _public_candidate_authority(authorities)
    expected_candidate_keys = set(candidate_authority)
    if any(set(verdicts) != expected_candidate_keys for verdicts in verdicts_by_run):
        raise PublicChallengeError("public stability candidate authority is stale")

    accepted_parent_clusters = tuple(
        {
            (key[0], candidate_authority[key].parent_cluster_id)
            for key, verdict in verdicts.items()
            if verdict != "unsupported"
        }
        for verdicts in verdicts_by_run
    )
    parent_pairs, parent_minimum = _pairwise_jaccard(accepted_parent_clusters)  # type: ignore[arg-type]

    accepted_gold_members: list[set[tuple[str, int]]] = []
    critical_gold_member_verdicts: list[dict[tuple[str, int], str]] = []
    critical_gold_member_categories: dict[tuple[str, int], tuple[str, ...]] = {}
    for verdicts in verdicts_by_run:
        accepted: set[tuple[str, int]] = set()
        critical_verdicts: dict[tuple[str, int], str] = {}
        for case_id in selected_ids:
            members = gold_rows[case_id].get("members")
            if not isinstance(members, list):
                raise PublicChallengeError("public stability gold is invalid")
            case_candidates = {
                key: value
                for key, value in candidate_authority.items()
                if key[0] == case_id
            }
            for member_ordinal, member in enumerate(members):
                if not isinstance(member, Mapping):
                    raise PublicChallengeError("public stability gold is invalid")
                member_key = (case_id, member_ordinal)
                member_status = _gold_member_verdict_status(
                    verdicts=verdicts,
                    case_candidates=case_candidates,
                    member=member,
                )
                if member_status != "unsupported":
                    accepted.add(member_key)
                categories = _critical_temporal_categories_for_member(member)
                if categories:
                    previous = critical_gold_member_categories.setdefault(
                        member_key,
                        categories,
                    )
                    if previous != categories:
                        raise PublicChallengeError(
                            "public critical stability category is inconsistent"
                        )
                    critical_verdicts[member_key] = member_status
        accepted_gold_members.append(accepted)
        critical_gold_member_verdicts.append(critical_verdicts)
    member_pairs, member_minimum = _pairwise_jaccard(
        tuple(accepted_gold_members)  # type: ignore[arg-type]
    )

    candidate_keys = sorted(expected_candidate_keys)
    exact_candidate_pairs, exact_candidate_minimum = _pairwise_verdict_agreement(
        verdicts_by_run,
        candidate_keys,
    )
    critical_candidate_keys = [
        key
        for key in candidate_keys
        if candidate_authority[key].candidate.relation == "deadline"
        or candidate_authority[key].candidate.lifecycle in _CRITICAL_LIFECYCLE_ROLES
    ]
    critical_candidate_pairs, critical_candidate_minimum = _pairwise_verdict_agreement(
        verdicts_by_run, critical_candidate_keys
    )
    critical_gold_member_keys = sorted(critical_gold_member_categories)
    if any(
        set(verdicts) != set(critical_gold_member_keys)
        for verdicts in critical_gold_member_verdicts
    ):
        raise PublicChallengeError("public critical gold stability is incomplete")
    critical_gold_member_pairs, critical_gold_member_minimum = (
        _pairwise_verdict_agreement(
            tuple(critical_gold_member_verdicts),  # type: ignore[arg-type]
            critical_gold_member_keys,
        )
    )
    critical_gold_category_stability: dict[str, dict[str, Any]] = {}
    for category in _CRITICAL_TEMPORAL_CATEGORIES:
        category_keys = [
            key
            for key in critical_gold_member_keys
            if category in critical_gold_member_categories[key]
        ]
        category_pairs, category_minimum = _pairwise_verdict_agreement(
            tuple(critical_gold_member_verdicts),  # type: ignore[arg-type]
            category_keys,
        )
        critical_gold_category_stability[category] = {
            "member_count": len(category_keys),
            "pairwise": category_pairs,
            "minimum_pairwise_agreement": category_minimum,
            "gate_passed": (
                bool(category_keys)
                and category_minimum >= MIN_CRITICAL_VERDICT_STABILITY
            ),
        }
    nonvacuous_critical_category_gates = [
        row["gate_passed"]
        for row in critical_gold_category_stability.values()
        if row["member_count"] > 0
    ]

    return {
        "basis": (
            "authenticated_case_atomic_raw_verdicts_with_source_verified_alias_collapse"
        ),
        "run_count": RUN_COUNT,
        "minimum_required_pairwise_jaccard": MIN_PAIRWISE_STABILITY,
        "accepted_parent_clusters": {
            "pairwise": parent_pairs,
            "minimum_pairwise_jaccard": parent_minimum,
            "gate_passed": parent_minimum >= MIN_PAIRWISE_STABILITY,
        },
        "accepted_gold_members": {
            "pairwise": member_pairs,
            "minimum_pairwise_jaccard": member_minimum,
            "gate_passed": member_minimum >= MIN_PAIRWISE_STABILITY,
        },
        "exact_candidate_verdict_agreement_diagnostic_only": {
            "pairwise": exact_candidate_pairs,
            "minimum_pairwise_agreement": exact_candidate_minimum,
        },
        "critical_candidate_verdict_agreement": {
            "critical_basis": (
                "deadline_relation_or_scheduled_cancelled_reschedule_lifecycle"
            ),
            "candidate_count": len(critical_candidate_keys),
            "minimum_required_pairwise_agreement": MIN_CRITICAL_VERDICT_STABILITY,
            "pairwise": critical_candidate_pairs,
            "minimum_pairwise_agreement": critical_candidate_minimum,
            "gate_passed": (
                bool(critical_candidate_keys)
                and critical_candidate_minimum >= MIN_CRITICAL_VERDICT_STABILITY
            ),
        },
        "critical_gold_member_verdict_agreement": {
            "critical_basis": (
                "gold_deadline_relation_or_scheduled_cancelled_reschedule_lifecycle"
            ),
            "member_count": len(critical_gold_member_keys),
            "minimum_required_pairwise_agreement": MIN_CRITICAL_VERDICT_STABILITY,
            "pairwise": critical_gold_member_pairs,
            "minimum_pairwise_agreement": critical_gold_member_minimum,
            "categories": critical_gold_category_stability,
            "gate_passed": bool(nonvacuous_critical_category_gates)
            and all(nonvacuous_critical_category_gates),
        },
    }


def _artifacts_recover_canonical_subject(
    artifacts: Sequence[Mapping[str, Any]],
    member: Mapping[str, Any],
    *,
    subject_surfaces: Mapping[str, str],
) -> bool:
    """Require every matched hypothesis to name the exact trusted event title."""

    expected_subject = _normalized_subject(member.get("subject"))
    if expected_subject is None or not artifacts:
        return False
    for artifact in artifacts:
        hypotheses = _artifact_hypotheses(artifact)
        if not hypotheses:
            return False
        for hypothesis in hypotheses:
            canonical_id = hypothesis.get("canonical_subject_mention_id")
            if not isinstance(canonical_id, str) or not canonical_id:
                return False
            canonical_surface = subject_surfaces.get(canonical_id)
            if _normalized_subject(canonical_surface) != expected_subject:
                return False
    return True


def _alternatives_artifacts(
    projection: Mapping[str, Any],
    member: Mapping[str, Any],
    *,
    subject_surfaces: Mapping[str, str],
    artifact_subject_aliases: Mapping[str, Sequence[str]] | None = None,
    excluded_artifact_ids: set[str] | None = None,
) -> tuple[tuple[str, ...], str] | None:
    """Match one option-set gold member to production's per-expression artifacts.

    Review projection intentionally forbids one artifact from spanning temporal
    expressions.  An explicit ``X or Y`` option set is therefore represented as
    one complete alternatives group containing one uncertainty artifact per
    expression, not as one multi-value artifact.  Score that exact production
    shape without weakening subject, relation, or lifecycle matching. Confidence
    calibration is ranked second and scored independently.
    """

    expected_values = member.get("values")
    if (
        member.get("expected_verdict") != "uncertain"
        or not isinstance(expected_values, list)
        or not expected_values
    ):
        return None
    artifacts = {
        str(item.get("artifact_id")): item
        for item in projection.get("artifacts", [])
        if isinstance(item, Mapping) and isinstance(item.get("artifact_id"), str)
    }
    excluded = excluded_artifact_ids or set()
    matches: list[tuple[int, str, tuple[str, ...]]] = []
    for group in projection.get("groups", []):
        if (
            not isinstance(group, Mapping)
            or group.get("kind") != "alternatives"
            or group.get("coverage") != "complete"
            or not isinstance(group.get("subject_family_id"), str)
            or not group.get("subject_family_id")
        ):
            continue
        group_members = group.get("members")
        if not isinstance(group_members, list) or len(group_members) != len(
            expected_values
        ):
            continue
        matched: list[str] = []
        actual_values: set[str] = set()
        valid = True
        for group_member in group_members:
            if (
                not isinstance(group_member, Mapping)
                or group_member.get("role") != "alternative"
                or group_member.get("state") != "present"
                or group_member.get("cluster_review_ids") not in ([], ())
                or not isinstance(group_member.get("artifact_ids"), list)
                or len(group_member["artifact_ids"]) != 1
            ):
                valid = False
                break
            artifact_id = str(group_member["artifact_ids"][0])
            artifact = artifacts.get(artifact_id)
            hypotheses = _artifact_hypotheses(artifact) if artifact is not None else []
            if (
                artifact is None
                or artifact_id in excluded
                or artifact.get("evidence_status") not in {"supported", "uncertain"}
                or not hypotheses
                or not all(
                    _hypothesis_matches_member(
                        hypothesis,
                        member,
                        subject_surfaces=subject_surfaces,
                        subject_alias_surfaces=(
                            artifact_subject_aliases.get(artifact_id, ())
                            if artifact_subject_aliases is not None
                            else ()
                        ),
                    )
                    for hypothesis in hypotheses
                )
            ):
                valid = False
                break
            artifact_values = {
                hypothesis.get("normalized_value") for hypothesis in hypotheses
            }
            if (
                len(artifact_values) != 1
                or None in artifact_values
                or actual_values.intersection(artifact_values)
            ):
                valid = False
                break
            actual_values.update(str(value) for value in artifact_values)
            matched.append(artifact_id)
        if valid and actual_values == set(expected_values):
            group_id = group.get("group_id")
            if isinstance(group_id, str) and group_id:
                matched_ids = tuple(matched)
                calibration_mismatches = sum(
                    artifacts[artifact_id].get("evidence_status") != "uncertain"
                    for artifact_id in matched_ids
                )
                matches.append((calibration_mismatches, group_id, matched_ids))
    if not matches:
        return None
    _, group_id, matched_ids = min(matches)
    return matched_ids, group_id


def _reschedule_artifact(
    projection: Mapping[str, Any],
    member: Mapping[str, Any],
    *,
    subject_surfaces: Mapping[str, str],
    artifact_subject_aliases: Mapping[str, Sequence[str]] | None = None,
    excluded_artifact_ids: set[str] | None = None,
) -> tuple[str, str] | None:
    artifacts = {
        str(item.get("artifact_id")): item
        for item in projection.get("artifacts", [])
        if isinstance(item, Mapping)
    }
    role = member.get("lifecycle")
    expected_verdict = str(member.get("expected_verdict", "supported"))
    excluded = excluded_artifact_ids or set()
    matches: list[tuple[bool, str, str]] = []
    for group in projection.get("groups", []):
        if (
            not isinstance(group, Mapping)
            or group.get("kind") != "reschedule"
            or group.get("coverage") != "complete"
        ):
            continue
        for group_member in group.get("members", []):
            if (
                not isinstance(group_member, Mapping)
                or group_member.get("role") != role
            ):
                continue
            artifact_ids = group_member.get("artifact_ids")
            if not isinstance(artifact_ids, list) or len(artifact_ids) != 1:
                continue
            artifact_id = str(artifact_ids[0])
            artifact = artifacts.get(artifact_id)
            if artifact is None or artifact_id in excluded:
                continue
            if _exact_artifact_match(
                artifact,
                member,
                subject_surfaces=subject_surfaces,
                subject_alias_surfaces=(
                    artifact_subject_aliases.get(artifact_id, ())
                    if artifact_subject_aliases is not None
                    else ()
                ),
            ):
                group_id = group.get("group_id")
                if isinstance(group_id, str) and group_id:
                    matches.append(
                        (
                            artifact.get("evidence_status") != expected_verdict,
                            group_id,
                            artifact_id,
                        )
                    )
    if not matches:
        return None
    _, group_id, artifact_id = min(matches)
    return artifact_id, group_id


def _forbidden_hypothesis_matches(
    hypothesis: Mapping[str, Any],
    forbidden: Any,
    *,
    subject_surfaces: Mapping[str, str],
    subject_alias_surfaces: Sequence[str] = (),
) -> bool:
    """Apply a forbidden value without weakening an already frozen benchmark.

    A bare value is case-wide in legacy V2/V3 gold. Changing its meaning after
    predictions exist would silently improve an old score. Newly frozen V4 gold
    requires a scoped binding with subject, relation, lifecycle, and value.
    """

    if isinstance(forbidden, str):
        return hypothesis.get("normalized_value") == forbidden
    if not isinstance(forbidden, Mapping):
        return False
    value = forbidden.get("value")
    if hypothesis.get("normalized_value") != value:
        return False
    return _hypothesis_matches_member(
        hypothesis,
        forbidden,
        subject_surfaces=subject_surfaces,
        subject_alias_surfaces=subject_alias_surfaces,
    )


def _structural_component_key(
    member: Mapping[str, Any],
    member_ordinal: int,
) -> str | None:
    """Return the gold component whose completeness this member participates in."""

    if "values" in member:
        return f"alternatives:{member_ordinal}"
    if member.get("lifecycle") not in {
        "rescheduled_old",
        "rescheduled_replacement",
    }:
        return None
    identity_tokens = sorted(_subject_identity_tokens(member.get("subject")))
    subject_key = " ".join(identity_tokens) or str(
        _normalized_subject(member.get("subject"))
    )
    return f"reschedule:{subject_key}"


def _semantic_member_identity(member: Mapping[str, Any]) -> bytes:
    """Ignore calibration-only fields when detecting duplicate gold targets."""

    values = member.get("values")
    if not isinstance(values, list):
        values = [member.get("value")]
    return _canonical_json(
        {
            "subject": _normalized_subject(member.get("subject")),
            "relation": member.get("relation"),
            "lifecycle": member.get("lifecycle"),
            "values": sorted(values),
        }
    )


def _validate_gold(gold: Mapping[str, Any]) -> None:
    gold_version = gold.get("version")
    supported_gold_versions = {
        GOLD_VERSION,
        LEGACY_STRUCTURED_GOLD_VERSION,
        LEGACY_GOLD_VERSION,
    }
    current_gold = gold_version == GOLD_VERSION
    if (
        set(gold) != {"version", "created_before_predictions", "cases"}
        or gold_version not in supported_gold_versions
        or gold.get("created_before_predictions") is not True
        or not isinstance(gold.get("cases"), list)
        or not gold["cases"]
    ):
        raise PublicChallengeError("public semantic gold schema is invalid")
    seen_cases: set[str] = set()
    positive_cases = 0
    negative_cases = 0
    for row in gold["cases"]:
        if (
            not isinstance(row, Mapping)
            or not {"case_id", "members"} <= set(row)
            or not set(row)
            <= {"case_id", "members", "forbidden", "complete_group_required"}
            or not isinstance(row.get("case_id"), str)
            or _CASE_ID_RE.fullmatch(row["case_id"]) is None
            or row["case_id"] in seen_cases
            or not isinstance(row.get("members"), list)
            or (
                "complete_group_required" in row
                and not isinstance(row["complete_group_required"], bool)
            )
        ):
            raise PublicChallengeError("public semantic gold case schema is invalid")
        seen_cases.add(row["case_id"])
        members = row["members"]
        positive_cases += int(bool(members))
        negative_cases += int(not members)
        forbidden = row.get("forbidden", [])
        if not isinstance(forbidden, list):
            raise PublicChallengeError(
                "public semantic gold forbidden values are invalid"
            )
        seen_forbidden: set[bytes] = set()
        for value in forbidden:
            if isinstance(value, str):
                valid_forbidden = (
                    not current_gold
                    and gold_version
                    in {LEGACY_STRUCTURED_GOLD_VERSION, LEGACY_GOLD_VERSION}
                    and _NORMALIZED_TEMPORAL_VALUE_RE.fullmatch(value) is not None
                )
            else:
                relation = value.get("relation") if isinstance(value, Mapping) else None
                valid_forbidden = (
                    gold_version in {GOLD_VERSION, LEGACY_STRUCTURED_GOLD_VERSION}
                    and isinstance(value, Mapping)
                    and set(value) == {"subject", "relation", "lifecycle", "value"}
                    and _normalized_subject(value.get("subject")) is not None
                    and (
                        relation in {"occurrence", "deadline"}
                        or (current_gold and relation == "unspecified")
                    )
                    and value.get("lifecycle")
                    in {
                        "none",
                        "unknown",
                        "scheduled",
                        "cancelled",
                        "completed",
                        "rescheduled_old",
                        "rescheduled_replacement",
                    }
                    and isinstance(value.get("value"), str)
                    and _NORMALIZED_TEMPORAL_VALUE_RE.fullmatch(value["value"])
                    is not None
                )
            identity = _canonical_json(value)
            if not valid_forbidden or identity in seen_forbidden:
                raise PublicChallengeError(
                    "public semantic gold forbidden values are invalid"
                )
            seen_forbidden.add(identity)
        seen_members: set[bytes] = set()
        expected_values: set[str] = set()
        for member in members:
            if not isinstance(member, Mapping):
                raise PublicChallengeError("public semantic gold member is invalid")
            keys = set(member)
            has_value = "value" in member
            has_values = "values" in member
            expected_verdict = member.get("expected_verdict", "supported")
            relation = member.get("relation")
            if (
                not {"subject", "relation", "lifecycle"} <= keys
                or not keys
                <= {
                    "subject",
                    "relation",
                    "lifecycle",
                    "value",
                    "values",
                    "expected_verdict",
                    *({"canonical_subject_required"} if current_gold else set()),
                }
                or has_value == has_values
                or _normalized_subject(member.get("subject")) is None
                or (
                    relation not in {"occurrence", "deadline"}
                    and not (current_gold and relation == "unspecified")
                )
                or member.get("lifecycle")
                not in {
                    "none",
                    "unknown",
                    "scheduled",
                    "cancelled",
                    "completed",
                    "rescheduled_old",
                    "rescheduled_replacement",
                }
                or expected_verdict not in {"supported", "uncertain"}
                or (current_gold and "expected_verdict" not in member)
                or (
                    "canonical_subject_required" in member
                    and not isinstance(member["canonical_subject_required"], bool)
                )
            ):
                raise PublicChallengeError(
                    "public semantic gold member schema is invalid"
                )
            values = member.get("values") if has_values else [member.get("value")]
            if (
                not isinstance(values, list)
                or not values
                or len(values) != len(set(values))
                or any(
                    not isinstance(value, str)
                    or _NORMALIZED_TEMPORAL_VALUE_RE.fullmatch(value) is None
                    for value in values
                )
                or (has_values and member.get("expected_verdict") != "uncertain")
            ):
                raise PublicChallengeError(
                    "public semantic gold value schema is invalid"
                )
            expected_values.update(values)
            identity = _semantic_member_identity(member)
            if identity in seen_members:
                raise PublicChallengeError("public semantic gold member is duplicated")
            seen_members.add(identity)
        legacy_forbidden_values = {
            value for value in forbidden if isinstance(value, str)
        }
        structured_contradiction = any(
            isinstance(value, Mapping)
            and any(
                (
                    _subject_matches(
                        str(value.get("subject")),
                        str(member.get("subject")),
                    )
                    or _subject_matches(
                        str(member.get("subject")),
                        str(value.get("subject")),
                    )
                )
                and value.get("relation") == member.get("relation")
                and value.get("lifecycle") == member.get("lifecycle")
                and value.get("value")
                in (
                    member.get("values")
                    if isinstance(member.get("values"), list)
                    else [member.get("value")]
                )
                for member in members
                if isinstance(member, Mapping)
            )
            for value in forbidden
        )
        if expected_values & legacy_forbidden_values or structured_contradiction:
            raise PublicChallengeError("public semantic gold contradicts itself")
        if row.get("complete_group_required") is True and not any(
            isinstance(member, Mapping)
            and _structural_component_key(member, ordinal) is not None
            for ordinal, member in enumerate(members)
        ):
            raise PublicChallengeError(
                "public semantic gold requires a missing structural group"
            )
    if positive_cases == 0 or negative_cases == 0:
        raise PublicChallengeError("public semantic gold denominators are vacuous")


def _frontier_diagnostic_count(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PublicChallengeError("frontier diagnostics count is invalid")
    return value


def _load_frontier_diagnostics(
    path: Path,
    *,
    key: bytes,
    challenge: Mapping[str, Any],
    challenge_raw: bytes,
    gold_rows: Mapping[str, Mapping[str, Any]],
    gold_raw: bytes,
    cases: Sequence[_Case],
) -> tuple[dict[str, Any], bytes]:
    """Authenticate content-free freeze diagnostics and bind their denominators."""

    value, raw = _load_signed_artifact(
        path,
        key=key,
        domain=FRONTIER_DIAGNOSTICS_DOMAIN,
        signature_field="frontier_diagnostics_hmac_sha256",
        label="public frontier diagnostics",
    )
    aggregates = value.get("aggregates")
    raw_cases = value.get("cases")
    if (
        set(value) != _FRONTIER_DIAGNOSTICS_KEYS
        or value.get("version") != FRONTIER_DIAGNOSTICS_VERSION
        or value.get("challenge_id") != challenge["challenge_id"]
        or value.get("challenge_manifest_sha256") != _sha256(challenge_raw)
        or value.get("gold_sha256") != _sha256(gold_raw)
        or value.get("gold_sha256") != challenge["gold_sha256"]
        or _SHA256_RE.fullmatch(str(value.get("fixture_sha256") or "")) is None
        or value.get("public_synthetic") is not True
        or value.get("contains_private_gmail") is not False
        or value.get("release_eligible") is not False
        or not isinstance(aggregates, Mapping)
        or set(aggregates) != _FRONTIER_DIAGNOSTICS_AGGREGATE_KEYS
        or not isinstance(raw_cases, list)
        or len(raw_cases) != len(cases)
    ):
        raise PublicChallengeError("public frontier diagnostics authority is invalid")
    for aggregate_value in aggregates.values():
        _frontier_diagnostic_count(aggregate_value)

    normalized_cases: list[dict[str, Any]] = []
    for case, row in zip(cases, raw_cases, strict=True):
        if not isinstance(row, Mapping) or set(row) != _FRONTIER_DIAGNOSTICS_CASE_KEYS:
            raise PublicChallengeError("public frontier diagnostics case is invalid")
        gold_row = gold_rows.get(case.case_id)
        members = gold_row.get("members") if isinstance(gold_row, Mapping) else None
        if not isinstance(members, list):
            raise PublicChallengeError("public frontier diagnostics gold is invalid")
        gold_member_count = _frontier_diagnostic_count(row.get("gold_members"))
        covered = _frontier_diagnostic_count(row.get("frontier_covered_gold_members"))
        missing = _frontier_diagnostic_count(row.get("frontier_missing_gold_members"))
        candidate_count = _frontier_diagnostic_count(row.get("candidate_count"))
        request_count = _frontier_diagnostic_count(row.get("verifier_request_count"))
        positive = bool(members)
        candidate_bearing = case.preparation.candidate_count > 0
        zero_work = not case.preparation.requests
        if (
            row.get("case_id") != case.case_id
            or gold_member_count != len(members)
            or covered + missing != gold_member_count
            or row.get("positive") is not positive
            or candidate_count != case.preparation.candidate_count
            or row.get("candidate_bearing") is not candidate_bearing
            or request_count != len(case.preparation.requests)
            or row.get("zero_work") is not zero_work
            or row.get("positive_zero_work") is not (positive and zero_work)
        ):
            raise PublicChallengeError("public frontier diagnostics case is stale")
        normalized_cases.append(dict(row))

    expected_aggregates = {
        "cases": len(normalized_cases),
        "positive_cases": sum(bool(row["positive"]) for row in normalized_cases),
        "negative_cases": sum(not row["positive"] for row in normalized_cases),
        "gold_members": sum(row["gold_members"] for row in normalized_cases),
        "frontier_covered_gold_members": sum(
            row["frontier_covered_gold_members"] for row in normalized_cases
        ),
        "frontier_missing_gold_members": sum(
            row["frontier_missing_gold_members"] for row in normalized_cases
        ),
        "positive_zero_work_cases": sum(
            bool(row["positive_zero_work"]) for row in normalized_cases
        ),
        "candidate_bearing_positive_cases": sum(
            bool(row["positive"] and row["candidate_bearing"])
            for row in normalized_cases
        ),
        "candidate_bearing_negative_cases": sum(
            bool(not row["positive"] and row["candidate_bearing"])
            for row in normalized_cases
        ),
    }
    if dict(aggregates) != expected_aggregates:
        raise PublicChallengeError("public frontier diagnostics aggregate is stale")
    return (
        {
            "version": FRONTIER_DIAGNOSTICS_VERSION,
            "sha256": _sha256(raw),
            "fixture_sha256": value["fixture_sha256"],
            "aggregates": expected_aggregates,
            "cases": normalized_cases,
        },
        raw,
    )


def _gold_audit_disposition(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _GOLD_AUDIT_DISPOSITION_KEYS:
        raise PublicChallengeError(f"public gold audit {label} is invalid")
    disposition = value.get("disposition")
    issue_codes = value.get("issue_codes")
    rationale = value.get("rationale")
    if (
        disposition not in {"valid", "correction_needed"}
        or not isinstance(issue_codes, list)
        or not issue_codes
        or len(issue_codes) != len(set(issue_codes))
        or any(code not in _GOLD_AUDIT_ISSUE_CODES for code in issue_codes)
        or not isinstance(rationale, str)
        or not rationale.strip()
        or len(rationale) > 1_000
        or "\x00" in rationale
        or disposition == "valid"
        and issue_codes != ["none"]
        or disposition == "correction_needed"
        and "none" in issue_codes
    ):
        raise PublicChallengeError(f"public gold audit {label} is invalid")
    return {
        "disposition": disposition,
        "issue_codes": list(issue_codes),
        "rationale": rationale,
    }


def _gold_audit_ordinal_dispositions(
    values: Any,
    *,
    expected_count: int,
    ordinal_name: str,
    label: str,
) -> list[dict[str, Any]]:
    if not isinstance(values, list) or len(values) != expected_count:
        raise PublicChallengeError(f"public gold audit {label} coverage is invalid")
    output: list[dict[str, Any]] = []
    for expected_ordinal, item in enumerate(values, start=1):
        if (
            not isinstance(item, Mapping)
            or set(item) != {ordinal_name, "disposition", "issue_codes", "rationale"}
            or type(item.get(ordinal_name)) is not int
            or item[ordinal_name] != expected_ordinal
        ):
            raise PublicChallengeError(f"public gold audit {label} coverage is invalid")
        disposition = _gold_audit_disposition(
            {key: value for key, value in item.items() if key != ordinal_name},
            label=label,
        )
        output.append({ordinal_name: expected_ordinal, **disposition})
    return output


def _gold_audit_response(
    value: Mapping[str, Any],
    *,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    cases = value.get("cases")
    request_cases = request.get("cases")
    if (
        set(value) != _GOLD_AUDIT_RESPONSE_KEYS
        or value.get("version") != GOLD_AUDIT_RESPONSE_VERSION
        or not isinstance(cases, list)
        or not isinstance(request_cases, list)
        or len(cases) != len(request_cases)
    ):
        raise PublicChallengeError("public gold audit response is invalid")
    output: list[dict[str, Any]] = []
    for item, source in zip(cases, request_cases, strict=True):
        if (
            not isinstance(item, Mapping)
            or not isinstance(source, Mapping)
            or set(item) != _GOLD_AUDIT_RESPONSE_CASE_KEYS
            or item.get("case_id") != source.get("case_id")
        ):
            raise PublicChallengeError("public gold audit response coverage is invalid")
        proposed_gold = source.get("proposed_gold")
        if not isinstance(proposed_gold, Mapping):
            raise PublicChallengeError("public gold audit request is invalid")
        case = _gold_audit_disposition(
            {key: item[key] for key in _GOLD_AUDIT_DISPOSITION_KEYS},
            label="case response",
        )
        members = _gold_audit_ordinal_dispositions(
            item.get("members"),
            expected_count=len(proposed_gold.get("members", [])),
            ordinal_name="member_ordinal",
            label="member response",
        )
        forbidden = _gold_audit_ordinal_dispositions(
            item.get("forbidden_bindings"),
            expected_count=len(proposed_gold.get("forbidden", [])),
            ordinal_name="forbidden_ordinal",
            label="forbidden response",
        )
        group_flag = _gold_audit_disposition(
            item.get("group_flag"),
            label="group response",
        )
        if case["disposition"] == "valid" and any(
            row["disposition"] == "correction_needed"
            for row in [*members, *forbidden, group_flag]
        ):
            raise PublicChallengeError("public gold audit case disposition conflicts")
        output.append(
            {
                "case_id": str(item["case_id"]),
                **case,
                "members": members,
                "forbidden_bindings": forbidden,
                "group_flag": group_flag,
            }
        )
    return {"version": GOLD_AUDIT_RESPONSE_VERSION, "cases": output}


def _gold_audit_aggregates(cases: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    members = [member for case in cases for member in case["members"]]
    forbidden = [item for case in cases for item in case["forbidden_bindings"]]
    groups = [case["group_flag"] for case in cases]
    return {
        "case_count": len(cases),
        "valid_case_count": sum(case["disposition"] == "valid" for case in cases),
        "correction_case_count": sum(
            case["disposition"] == "correction_needed" for case in cases
        ),
        "member_count": len(members),
        "valid_member_count": sum(
            member["disposition"] == "valid" for member in members
        ),
        "correction_member_count": sum(
            member["disposition"] == "correction_needed" for member in members
        ),
        "forbidden_binding_count": len(forbidden),
        "valid_forbidden_binding_count": sum(
            item["disposition"] == "valid" for item in forbidden
        ),
        "correction_forbidden_binding_count": sum(
            item["disposition"] == "correction_needed" for item in forbidden
        ),
        "valid_group_flag_count": sum(
            item["disposition"] == "valid" for item in groups
        ),
        "correction_group_flag_count": sum(
            item["disposition"] == "correction_needed" for item in groups
        ),
    }


def _gold_audit_expected_request_case(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "case_id": str(row["case_id"]),
        "source": {
            "sender": str(row["sender"]),
            "subject": str(row["subject"]),
            "body": str(row["body"]),
            "label_ids": list(row["label_ids"]),
        },
        "proposed_gold": {
            "members": [dict(member) for member in row["members"]],
            "forbidden": [dict(binding) for binding in row["forbidden"]],
            "complete_group_required": bool(row["complete_group_required"]),
        },
    }


def _gold_audit_fixture_gold(fixture: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": GOLD_VERSION,
        "created_before_predictions": True,
        "cases": [
            {
                "case_id": str(row["case_id"]),
                "members": [dict(member) for member in row["members"]],
                "forbidden": [dict(binding) for binding in row["forbidden"]],
                "complete_group_required": bool(row["complete_group_required"]),
            }
            for row in fixture["cases"]
        ],
    }


def _gold_audit_directory_names(path: Path) -> set[str]:
    try:
        return {item.name for item in path.iterdir()}
    except OSError as exc:
        raise PublicChallengeError("public gold audit evidence is unavailable") from exc


def _load_gold_audit_evidence(
    root: Path,
    *,
    key: bytes,
    frontier_diagnostics: Mapping[str, Any],
    challenge: Mapping[str, Any],
    gold: Mapping[str, Any],
    prediction_plan_created_at: datetime,
) -> tuple[dict[str, Any], bytes]:
    """Validate every artifact in one zero-correction restricted Sol audit."""

    _private_directory(root)
    if _gold_audit_directory_names(root) != {
        "fixture.json",
        "audit-plan.json",
        "audit-detail.json",
        "audit-summary.json",
        "calls",
    }:
        raise PublicChallengeError("public gold audit root coverage is invalid")
    fixture_raw = _private_file(root / "fixture.json")
    fixture = _strict_json(fixture_raw, label="public gold audit fixture")
    plan, plan_raw = _load_signed_artifact(
        root / "audit-plan.json",
        key=key,
        domain=GOLD_AUDIT_PLAN_DOMAIN,
        signature_field="plan_hmac_sha256",
        label="public gold audit plan",
    )
    detail, detail_raw = _load_signed_artifact(
        root / "audit-detail.json",
        key=key,
        domain=GOLD_AUDIT_DETAIL_DOMAIN,
        signature_field="detail_hmac_sha256",
        label="public gold audit detail",
    )
    summary, summary_raw = _load_signed_artifact(
        root / "audit-summary.json",
        key=key,
        domain=GOLD_AUDIT_SUMMARY_DOMAIN,
        signature_field="summary_hmac_sha256",
        label="public gold audit summary",
    )
    plan_at = _aware_timestamp(plan.get("created_at"))
    detail_at = _aware_timestamp(detail.get("created_at"))
    summary_at = _aware_timestamp(summary.get("created_at"))
    fixture_variant = plan.get("fixture_variant")
    fixture_sha256 = _sha256(fixture_raw)
    if (
        set(fixture) != _GOLD_AUDIT_FIXTURE_KEYS
        or fixture.get("version") != GOLD_AUDIT_FIXTURE_VERSION
        or fixture.get("challenge_id") != challenge.get("challenge_id")
        or fixture.get("created_at") != challenge.get("created_at")
        or _aware_timestamp(fixture.get("created_at")) is None
        or _aware_timestamp(fixture.get("message_internal_at")) is None
        or _aware_timestamp(fixture.get("message_internal_at"))
        > _aware_timestamp(fixture.get("created_at"))
        or fixture.get("public_synthetic") is not True
        or fixture.get("contains_private_gmail") is not False
        or fixture.get("release_eligible") is not False
        or not isinstance(fixture.get("account_email"), str)
        or not str(fixture["account_email"]).casefold().endswith(".example.test")
        or not isinstance(fixture.get("cases"), list)
        or not fixture["cases"]
        or isinstance(fixture_variant, bool)
        or fixture_variant not in GOLD_AUDIT_APPROVED_FIXTURE_SHA256
        or fixture_sha256
        != GOLD_AUDIT_APPROVED_FIXTURE_SHA256.get(int(fixture_variant))
        or fixture_sha256 != frontier_diagnostics.get("fixture_sha256")
    ):
        raise PublicChallengeError("public gold audit fixture authority is invalid")
    fixture_cases = fixture["cases"]
    if any(
        not isinstance(row, Mapping) or set(row) != _GOLD_AUDIT_FIXTURE_CASE_KEYS
        for row in fixture_cases
    ):
        raise PublicChallengeError("public gold audit fixture cases are invalid")
    expected_case_ids = [str(row["case_id"]) for row in fixture_cases]
    if (
        len(expected_case_ids) != len(set(expected_case_ids))
        or expected_case_ids
        != [str(row["case_id"]) for row in challenge.get("cases", [])]
        or _gold_audit_fixture_gold(fixture) != gold
    ):
        raise PublicChallengeError("public gold audit fixture coverage is invalid")

    batch_count = _frontier_diagnostic_count(plan.get("batch_count"))
    request_hashes = plan.get("request_sha256")
    if (
        set(plan) != _GOLD_AUDIT_PLAN_KEYS
        or plan.get("version") != GOLD_AUDIT_PLAN_VERSION
        or plan.get("scope") != GOLD_AUDIT_SCOPE
        or plan_at is None
        or plan.get("fixture_version") != GOLD_AUDIT_FIXTURE_VERSION
        or plan.get("fixture_variant") != fixture_variant
        or plan.get("fixture_sha256") != fixture_sha256
        or plan.get("fixture_generator_version") != GOLD_AUDIT_FIXTURE_GENERATOR_VERSION
        or plan.get("fixture_generator_sha256") != GOLD_AUDIT_FIXTURE_GENERATOR_SHA256
        or plan.get("fixture_generator_exact_bytes_verified") is not True
        or plan.get("case_count") != len(fixture_cases)
        or batch_count == 0
        or not isinstance(request_hashes, list)
        or len(request_hashes) != batch_count
        or len(request_hashes) != len(set(request_hashes))
        or any(
            not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None
            for value in request_hashes
        )
        or plan.get("provider") != GOLD_AUDIT_PROVIDER
        or plan.get("model") != GOLD_AUDIT_MODEL
        or plan.get("reasoning_effort") != GOLD_AUDIT_REASONING_EFFORT
        or plan.get("public_synthetic") is not True
        or plan.get("contains_private_gmail") is not False
        or plan.get("pipeline_predictions_present") is not False
        or plan.get("prediction_artifacts_read") is not False
        or plan.get("diagnostic_only") is not True
        or plan.get("release_eligible") is not False
    ):
        raise PublicChallengeError("public gold audit plan authority is invalid")

    calls_root = root / "calls"
    _private_directory(calls_root)
    expected_call_names = {f"{ordinal:03d}" for ordinal in range(1, batch_count + 1)}
    if _gold_audit_directory_names(calls_root) != expected_call_names:
        raise PublicChallengeError("public gold audit call coverage is invalid")
    fixture_case_by_id = {str(row["case_id"]): row for row in fixture_cases}
    completed_case_ids: list[str] = []
    completed_cases: list[dict[str, Any]] = []
    detail_calls: list[dict[str, Any]] = []
    latest_completed_at = plan_at
    for unit_ordinal in range(1, batch_count + 1):
        call_root = calls_root / f"{unit_ordinal:03d}"
        _private_directory(call_root)
        if _gold_audit_directory_names(call_root) != {
            "request.json",
            "response.json",
            "receipt.json",
        }:
            raise PublicChallengeError("public gold audit call evidence is incomplete")
        request_raw = _private_file(call_root / "request.json")
        response_raw = _private_file(call_root / "response.json")
        request = _strict_json(request_raw, label="public gold audit request")
        response_value = _strict_json(response_raw, label="public gold audit response")
        receipt, receipt_raw = _load_signed_artifact(
            call_root / "receipt.json",
            key=key,
            domain=GOLD_AUDIT_RECEIPT_DOMAIN,
            signature_field="receipt_hmac_sha256",
            label="public gold audit receipt",
        )
        request_cases = request.get("cases")
        contract = request.get("contract")
        if (
            len(request_raw) > GOLD_AUDIT_MAX_REQUEST_BYTES
            or set(request) != _GOLD_AUDIT_REQUEST_KEYS
            or request.get("version") != GOLD_AUDIT_REQUEST_VERSION
            or request.get("phase") != "prediction_blind_public_gold_audit"
            or not isinstance(contract, str)
            or _sha256(contract.encode("utf-8")) != GOLD_AUDIT_CONTRACT_SHA256
            or request.get("challenge_id") != fixture["challenge_id"]
            or request.get("fixture_created_at") != fixture["created_at"]
            or request.get("message_internal_at") != fixture["message_internal_at"]
            or request.get("account_email") != fixture["account_email"]
            or request.get("public_synthetic") is not True
            or request.get("contains_private_gmail") is not False
            or request.get("pipeline_predictions_present") is not False
            or not isinstance(request_cases, list)
            or not 1 <= len(request_cases) <= GOLD_AUDIT_MAX_BATCH_CASES
            or _sha256(request_raw) != request_hashes[unit_ordinal - 1]
        ):
            raise PublicChallengeError("public gold audit request authority is invalid")
        for request_case in request_cases:
            if (
                not isinstance(request_case, Mapping)
                or set(request_case) != _GOLD_AUDIT_REQUEST_CASE_KEYS
                or not isinstance(request_case.get("source"), Mapping)
                or set(request_case["source"]) != _GOLD_AUDIT_REQUEST_SOURCE_KEYS
                or not isinstance(request_case.get("proposed_gold"), Mapping)
                or set(request_case["proposed_gold"]) != _GOLD_AUDIT_REQUEST_GOLD_KEYS
                or not isinstance(request_case.get("case_id"), str)
                or request_case.get("case_id") not in fixture_case_by_id
                or request_case
                != _gold_audit_expected_request_case(
                    fixture_case_by_id[str(request_case["case_id"])]
                )
            ):
                raise PublicChallengeError(
                    "public gold audit request case authority is invalid"
                )
            completed_case_ids.append(str(request_case["case_id"]))
        if len(response_raw) > GOLD_AUDIT_MAX_RESPONSE_BYTES:
            raise PublicChallengeError("public gold audit response is oversized")
        response = _gold_audit_response(response_value, request=request)
        started_at = _aware_timestamp(receipt.get("started_at"))
        completed_at = _aware_timestamp(receipt.get("completed_at"))
        if (
            set(receipt) != _GOLD_AUDIT_RECEIPT_KEYS
            or receipt.get("version") != GOLD_AUDIT_RECEIPT_VERSION
            or receipt.get("unit_ordinal") != unit_ordinal
            or started_at is None
            or completed_at is None
            or started_at < plan_at
            or completed_at < started_at
            or started_at < latest_completed_at
            or receipt.get("provider") != GOLD_AUDIT_PROVIDER
            or receipt.get("model") != GOLD_AUDIT_MODEL
            or receipt.get("reasoning_effort") != GOLD_AUDIT_REASONING_EFFORT
            or receipt.get("request_sha256") != _sha256(request_raw)
            or receipt.get("response_sha256") != _sha256(response_raw)
            or receipt.get("case_count") != len(request_cases)
            or receipt.get("public_synthetic") is not True
            or receipt.get("contains_private_gmail") is not False
            or receipt.get("pipeline_predictions_present") is not False
            or receipt.get("restricted_execution") is not True
            or receipt.get("ephemeral_execution") is not True
            or receipt.get("local_model_used") is not False
            or receipt.get("test_invoker_used") is not False
        ):
            raise PublicChallengeError("public gold audit receipt authority is invalid")
        latest_completed_at = completed_at
        completed_cases.extend(response["cases"])
        detail_calls.append(
            {
                "unit_ordinal": unit_ordinal,
                "request_sha256": _sha256(request_raw),
                "response_sha256": _sha256(response_raw),
                "receipt_sha256": _sha256(receipt_raw),
                "case_count": len(request_cases),
            }
        )
    if completed_case_ids != expected_case_ids:
        raise PublicChallengeError("public gold audit case coverage is incomplete")
    aggregates = _gold_audit_aggregates(completed_cases)
    common_invalid = bool(
        detail.get("scope") != GOLD_AUDIT_SCOPE
        or detail.get("fixture_sha256") != fixture_sha256
        or detail.get("fixture_variant") != fixture_variant
        or detail.get("fixture_generator_version")
        != GOLD_AUDIT_FIXTURE_GENERATOR_VERSION
        or detail.get("fixture_generator_sha256") != GOLD_AUDIT_FIXTURE_GENERATOR_SHA256
        or detail.get("fixture_generator_exact_bytes_verified") is not True
        or detail.get("provider") != GOLD_AUDIT_PROVIDER
        or detail.get("model") != GOLD_AUDIT_MODEL
        or detail.get("reasoning_effort") != GOLD_AUDIT_REASONING_EFFORT
        or detail.get("public_synthetic") is not True
        or detail.get("contains_private_gmail") is not False
        or detail.get("pipeline_predictions_present") is not False
        or detail.get("prediction_artifacts_read") is not False
        or detail.get("diagnostic_only") is not True
        or detail.get("release_eligible") is not False
    )
    if (
        set(detail) != _GOLD_AUDIT_DETAIL_KEYS
        or detail.get("version") != GOLD_AUDIT_DETAIL_VERSION
        or detail.get("status") != "complete"
        or detail_at is None
        or detail_at < latest_completed_at
        or detail.get("plan_sha256") != _sha256(plan_raw)
        or detail.get("calls") != detail_calls
        or detail.get("cases") != completed_cases
        or detail.get("aggregates") != aggregates
        or common_invalid
    ):
        raise PublicChallengeError("public gold audit detail authority is invalid")

    count_names = {
        *aggregates,
        "batch_count",
        "external_calls",
    }
    counts = {
        name: _frontier_diagnostic_count(summary.get(name)) for name in count_names
    }
    if (
        set(summary) != _GOLD_AUDIT_SUMMARY_KEYS
        or summary.get("version") != GOLD_AUDIT_SUMMARY_VERSION
        or summary.get("status") != "complete"
        or summary.get("scope") != GOLD_AUDIT_SCOPE
        or summary_at is None
        or detail_at is None
        or summary_at < detail_at
        or summary_at > prediction_plan_created_at
        or summary.get("fixture_sha256") != fixture_sha256
        or summary.get("fixture_variant") != fixture_variant
        or summary.get("fixture_generator_version")
        != GOLD_AUDIT_FIXTURE_GENERATOR_VERSION
        or summary.get("fixture_generator_sha256")
        != GOLD_AUDIT_FIXTURE_GENERATOR_SHA256
        or summary.get("fixture_generator_exact_bytes_verified") is not True
        or summary.get("plan_sha256") != _sha256(plan_raw)
        or summary.get("detail_sha256") != _sha256(detail_raw)
        or counts["batch_count"] != batch_count
        or counts["external_calls"] != batch_count
        or summary.get("provider") != GOLD_AUDIT_PROVIDER
        or summary.get("model") != GOLD_AUDIT_MODEL
        or summary.get("reasoning_effort") != GOLD_AUDIT_REASONING_EFFORT
        or summary.get("restricted_execution") is not True
        or summary.get("ephemeral_execution") is not True
        or summary.get("local_model_used") is not False
        or summary.get("test_invoker_used") is not False
        or summary.get("public_synthetic") is not True
        or summary.get("contains_private_gmail") is not False
        or summary.get("pipeline_predictions_present") is not False
        or summary.get("prediction_artifacts_read") is not False
        or summary.get("private_content_printed") is not False
        or summary.get("diagnostic_only") is not True
        or summary.get("release_eligible") is not False
        or any(counts[name] != expected for name, expected in aggregates.items())
    ):
        raise PublicChallengeError("public gold audit summary authority is invalid")
    if (
        counts["valid_case_count"] != counts["case_count"]
        or counts["correction_case_count"] != 0
        or counts["valid_member_count"] != counts["member_count"]
        or counts["correction_member_count"] != 0
        or counts["valid_forbidden_binding_count"] != counts["forbidden_binding_count"]
        or counts["correction_forbidden_binding_count"] != 0
        or counts["valid_group_flag_count"] != counts["case_count"]
        or counts["correction_group_flag_count"] != 0
    ):
        raise PublicChallengeError("public gold audit corrections are not zero")
    return (
        {
            "version": GOLD_AUDIT_SUMMARY_VERSION,
            "sha256": _sha256(summary_raw),
            "summary_sha256": _sha256(summary_raw),
            "detail_sha256": _sha256(detail_raw),
            "plan_sha256": _sha256(plan_raw),
            "fixture_sha256": fixture_sha256,
            "fixture_variant": fixture_variant,
            "fixture_generator_version": GOLD_AUDIT_FIXTURE_GENERATOR_VERSION,
            "fixture_generator_sha256": GOLD_AUDIT_FIXTURE_GENERATOR_SHA256,
            "fixture_generator_exact_bytes_verified": True,
            "audited_at": summary["created_at"],
            "provider": GOLD_AUDIT_PROVIDER,
            "model": GOLD_AUDIT_MODEL,
            "reasoning_effort": GOLD_AUDIT_REASONING_EFFORT,
            "external_calls": counts["external_calls"],
            "case_count": counts["case_count"],
            "member_count": counts["member_count"],
            "forbidden_binding_count": counts["forbidden_binding_count"],
            "complete_evidence_chain": True,
            "zero_corrections": True,
        },
        summary_raw,
    )


def score_public_challenge(
    challenge_path: Path,
    gold_path: Path,
    hmac_key_path: Path,
    output_root: Path,
    *,
    evaluation_mode: str,
    prediction_launcher_artifact: Path | None = None,
    frontier_diagnostics_path: Path | None = None,
    gold_audit_root: Path | None = None,
) -> dict[str, Any]:
    """Open gold only after a complete authenticated prediction result exists.

    ``evaluation_mode`` is an explicit operator assertion.  The signed evidence
    proves only that this invocation opened gold after this prediction seal; it
    cannot prove that the cohort's gold was never opened in an earlier run.
    """

    if evaluation_mode not in {"blind_first_use", "development_replay"}:
        raise PublicChallengeError("public challenge evaluation mode is invalid")

    key = _key(hmac_key_path)
    _private_directory(output_root)
    challenge, challenge_raw = _load_challenge(challenge_path, key=key)
    plan, plan_raw = _load_signed_artifact(
        output_root / "plan.json",
        key=key,
        domain=PLAN_DOMAIN,
        signature_field="plan_hmac_sha256",
        label="public challenge plan",
    )
    seal, seal_raw = _load_signed_artifact(
        output_root / "prediction-seal.json",
        key=key,
        domain=PREDICTION_SEAL_DOMAIN,
        signature_field="seal_hmac_sha256",
        label="prediction seal",
    )
    result, result_raw = _load_signed_artifact(
        output_root / "results.json",
        key=key,
        domain=RESULT_DOMAIN,
        signature_field="result_hmac_sha256",
        label="public challenge result",
    )
    cases = _prepare_cases(challenge)
    prediction_provenance = _validate_prediction_source_provenance(
        plan.get("source_module_sha256"),
        launcher_version=plan.get("launcher_version"),
        plan_version=plan.get("version"),
        prediction_launcher_artifact=prediction_launcher_artifact,
    )
    rows = (
        _request_rows(cases)
        if prediction_provenance.trust_basis == "current_scorer_source"
        else _archived_request_rows(cases, plan)
    )
    units = bounded_public_call_units(rows)
    claims, plan_calls = _validate_plan_authority(
        plan,
        challenge=challenge,
        challenge_raw=challenge_raw,
        cases=cases,
        units=units,
        source_provenance=prediction_provenance,
    )
    calls = _validate_call_evidence(
        output_root=output_root,
        key=key,
        challenge=challenge,
        plan_calls=plan_calls,
        units=units,
        claims=claims,
    )
    components, authorities = _validate_component_evidence(
        output_root=output_root,
        challenge=challenge,
        cases=cases,
        calls=calls,
        plan=plan,
        source_provenance=prediction_provenance,
    )
    _validate_seal_authority(
        seal,
        key=key,
        challenge=challenge,
        challenge_raw=challenge_raw,
        plan_raw=plan_raw,
        cases=cases,
        calls=calls,
        components=components,
        claims=claims,
    )
    result_rows = _validate_persisted_results(
        result,
        challenge=challenge,
        challenge_raw=challenge_raw,
        plan=plan,
        plan_raw=plan_raw,
        seal_raw=seal_raw,
        cases=cases,
        components=components,
        calls=calls,
        claims=claims,
    )
    plan_at = _aware_timestamp(plan.get("created_at"))
    seal_at = _aware_timestamp(seal.get("sealed_at"))
    result_at = _aware_timestamp(result.get("completed_at"))
    if (
        plan_at is None
        or seal_at is None
        or result_at is None
        or seal_at < plan_at
        or result_at < seal_at
        or any(
            (_aware_timestamp(call.started_at) or seal_at) < plan_at
            or (_aware_timestamp(call.completed_at) or plan_at) > seal_at
            for call in calls
        )
    ):
        raise PublicChallengeError("prediction chronology or authority is invalid")
    challenge_sha = _sha256(challenge_raw)
    gold_raw = _private_file(gold_path)
    if _sha256(gold_raw) != challenge["gold_sha256"]:
        raise PublicChallengeError("gold commitment does not match challenge freeze")
    gold = _strict_json(gold_raw, label="public semantic gold")
    _validate_gold(gold)
    selected_ids = [str(item["case_id"]) for item in challenge["cases"]]
    gold_rows = {
        str(item.get("case_id")): item
        for item in gold.get("cases", [])
        if isinstance(item, Mapping)
    }
    if any(case_id not in gold_rows for case_id in selected_ids):
        raise PublicChallengeError("semantic gold does not cover every selected case")
    if not any(gold_rows[case_id]["members"] for case_id in selected_ids) or not any(
        not gold_rows[case_id]["members"] for case_id in selected_ids
    ):
        raise PublicChallengeError("selected semantic gold denominators are vacuous")
    if set(result_rows) != set(selected_ids):
        raise PublicChallengeError("prediction result case coverage is invalid")
    # Production preparations are bound to the authenticated plan and checked
    # again against persisted execution receipts and results above.  Use their
    # frontier count for the candidate-bearing negative denominator rather than
    # inferring candidate availability from model output or verifier requests.
    candidate_bearing_case_ids = {
        case.case_id for case in cases if case.preparation.candidate_count > 0
    }
    frontier_diagnostics: dict[str, Any] | None = None
    if frontier_diagnostics_path is not None:
        frontier_diagnostics, _ = _load_frontier_diagnostics(
            frontier_diagnostics_path,
            key=key,
            challenge=challenge,
            challenge_raw=challenge_raw,
            gold_rows=gold_rows,
            gold_raw=gold_raw,
            cases=cases,
        )
    gold_audit: dict[str, Any] | None = None
    if gold_audit_root is not None:
        if frontier_diagnostics is None:
            raise PublicChallengeError(
                "public gold audit requires authenticated frontier diagnostics"
            )
        assert plan_at is not None
        gold_audit, _ = _load_gold_audit_evidence(
            gold_audit_root,
            key=key,
            frontier_diagnostics=frontier_diagnostics,
            challenge=challenge,
            gold=gold,
            prediction_plan_created_at=plan_at,
        )

    recomputed_frontier = _recompute_frontier_gold_coverage(
        authorities=authorities,
        gold_rows=gold_rows,
        selected_ids=selected_ids,
    )
    if frontier_diagnostics is not None:
        signed_cases = {
            str(row["case_id"]): row for row in frontier_diagnostics["cases"]
        }
        if set(signed_cases) != set(selected_ids) or any(
            signed_cases[row["case_id"]]["gold_members"] != row["gold_members"]
            or signed_cases[row["case_id"]]["frontier_covered_gold_members"]
            != row["frontier_covered_gold_members"]
            or signed_cases[row["case_id"]]["frontier_missing_gold_members"]
            != row["frontier_missing_gold_members"]
            for row in recomputed_frontier["cases"]
        ):
            raise PublicChallengeError(
                "public frontier diagnostics do not match production candidates"
            )
        aggregates = frontier_diagnostics["aggregates"]
        if any(
            aggregates[name] != recomputed_frontier[name]
            for name in (
                "gold_members",
                "frontier_covered_gold_members",
                "frontier_missing_gold_members",
            )
        ):
            raise PublicChallengeError(
                "public frontier aggregate does not match production candidates"
            )

    three_run_stability = _three_run_stability(
        calls=calls,
        authorities=authorities,
        gold_rows=gold_rows,
        selected_ids=selected_ids,
    )

    gold_members = 0
    supported_gold_members = 0
    matched_members = 0
    confirmed_members = 0
    canonical_subject_members = 0
    canonical_subject_members_recovered = 0
    canonical_title_keys: set[tuple[str, str]] = set()
    recovered_canonical_title_keys: set[tuple[str, str]] = set()
    total_artifacts = 0
    supported_artifacts = 0
    matched_artifact_ids: set[tuple[str, str]] = set()
    matched_artifact_expected_verdicts: dict[tuple[str, str], str] = {}
    cluster_reviews = 0
    positive_cases = 0
    positive_cases_fully_recovered = 0
    negative_cases = 0
    selected_negative_cases = 0
    candidate_bearing_negative_cases = 0
    selected_candidate_bearing_negative_cases = 0
    complete_group_components = 0
    complete_group_components_recovered = 0
    semantic_units = 0
    complete_units = 0
    exact_units = 0
    reschedule_units = 0
    complete_reschedule_units = 0
    forbidden_hypotheses = 0
    lifecycle_gold_members: Counter[str] = Counter()
    lifecycle_matched_members: Counter[str] = Counter()
    lifecycle_supported_gold_members: Counter[str] = Counter()
    lifecycle_confirmed_members: Counter[str] = Counter()
    relation_gold_members: Counter[str] = Counter()
    relation_matched_members: Counter[str] = Counter()
    relation_supported_gold_members: Counter[str] = Counter()
    relation_confirmed_members: Counter[str] = Counter()
    critical_category_gold_members: Counter[str] = Counter()
    critical_category_matched_members: Counter[str] = Counter()
    critical_temporal_gold_members = 0
    critical_temporal_matched_members = 0
    per_case: list[dict[str, Any]] = []
    for case_id in selected_ids:
        gold_row = gold_rows[case_id]
        members = gold_row.get("members")
        if not isinstance(members, list):
            raise PublicChallengeError("semantic gold member schema is invalid")
        prediction = result_rows[case_id]
        projection = prediction.get("projection")
        authority = authorities[case_id]
        subject_surfaces = _authority_subject_surfaces(authority)
        parent_cluster_subject_ids = _authority_parent_cluster_subject_ids(authority)
        artifact_subject_aliases: dict[str, frozenset[str]] = {}
        artifacts: list[Mapping[str, Any]] = []
        reviews: list[Mapping[str, Any]] = []
        if projection is not None:
            if not isinstance(projection, Mapping):
                raise PublicChallengeError("prediction projection is invalid")
            artifacts = [
                item
                for item in projection.get("artifacts", [])
                if isinstance(item, Mapping)
            ]
            reviews = [
                item
                for item in projection.get("cluster_reviews", [])
                if isinstance(item, Mapping)
            ]
            artifact_subject_aliases = _artifact_subject_aliases(
                projection,
                subject_surfaces=subject_surfaces,
                parent_cluster_subject_ids=parent_cluster_subject_ids,
            )
        total_artifacts += len(artifacts)
        supported_artifacts += sum(
            item.get("evidence_status") == "supported" for item in artifacts
        )
        cluster_reviews += len(reviews)
        case_selected = bool(artifacts or reviews)
        case_candidate_bearing = case_id in candidate_bearing_case_ids
        positive_cases += int(bool(members))
        if not members:
            negative_cases += 1
            selected_negative_cases += int(case_selected)
            candidate_bearing_negative_cases += int(case_candidate_bearing)
            selected_candidate_bearing_negative_cases += int(
                case_candidate_bearing and case_selected
            )
        forbidden_bindings = gold_row.get("forbidden", [])
        if not isinstance(forbidden_bindings, list):
            raise PublicChallengeError("semantic gold forbidden schema is invalid")
        case_forbidden = sum(
            any(
                _forbidden_hypothesis_matches(
                    hypothesis,
                    forbidden,
                    subject_surfaces=subject_surfaces,
                    subject_alias_surfaces=artifact_subject_aliases.get(
                        str(artifact.get("artifact_id")),
                        (),
                    ),
                )
                for forbidden in forbidden_bindings
            )
            for artifact in artifacts
            for hypothesis in _artifact_hypotheses(artifact)
        )
        forbidden_hypotheses += case_forbidden
        available = {
            str(item.get("artifact_id")): item
            for item in artifacts
            if isinstance(item.get("artifact_id"), str)
        }
        case_matches = 0
        case_confirmed = 0
        case_canonical_subject_members = 0
        case_canonical_subject_members_recovered = 0
        case_canonical_titles: set[str] = set()
        case_recovered_canonical_titles: set[str] = set()
        component_members: Counter[str] = Counter()
        complete_group_required = gold_row.get("complete_group_required") is True
        if complete_group_required:
            for member_ordinal, member in enumerate(members):
                if isinstance(member, Mapping):
                    component_key = _structural_component_key(member, member_ordinal)
                    if component_key is not None:
                        component_members[component_key] += 1
        complete_group_components += len(component_members)
        matched_component_members: Counter[str] = Counter()
        component_group_ids: defaultdict[str, set[str]] = defaultdict(set)
        member_outcomes: list[dict[str, Any]] = [
            {
                "matched": False,
                "exact": False,
                "structural_group_id": None,
            }
            for _ in members
        ]
        for member_ordinal, member in enumerate(members):
            if not isinstance(member, Mapping):
                raise PublicChallengeError("semantic gold member is invalid")
            component_key = (
                _structural_component_key(member, member_ordinal)
                if complete_group_required
                else None
            )
            gold_members += 1
            lifecycle = str(member.get("lifecycle"))
            if lifecycle not in _LIFECYCLE_ROLES:
                raise PublicChallengeError("semantic gold lifecycle is invalid")
            relation = str(member.get("relation"))
            if relation not in _TEMPORAL_RELATIONS:
                raise PublicChallengeError("semantic gold relation is invalid")
            critical_categories = _critical_temporal_categories_for_member(member)
            critical_temporal = bool(critical_categories)
            lifecycle_gold_members[lifecycle] += 1
            relation_gold_members[relation] += 1
            critical_temporal_gold_members += int(critical_temporal)
            critical_category_gold_members.update(critical_categories)
            expected_verdict = member.get("expected_verdict", "supported")
            supported_gold_members += int(expected_verdict != "uncertain")
            lifecycle_supported_gold_members[lifecycle] += int(
                expected_verdict != "uncertain"
            )
            relation_supported_gold_members[relation] += int(
                expected_verdict != "uncertain"
            )
            canonical_subject_required = (
                member.get("canonical_subject_required") is True
            )
            canonical_subject_members += int(canonical_subject_required)
            case_canonical_subject_members += int(canonical_subject_required)
            canonical_title = (
                _normalized_subject(member.get("subject"))
                if canonical_subject_required
                else None
            )
            if canonical_subject_required:
                if canonical_title is None:
                    raise PublicChallengeError(
                        "canonical title gold identity is invalid"
                    )
                canonical_title_keys.add((case_id, canonical_title))
                case_canonical_titles.add(canonical_title)
            matched_ids: tuple[str, ...] = ()
            structural_group_id: str | None = None
            used_case_artifact_ids = {
                artifact_id
                for used_case_id, artifact_id in matched_artifact_ids
                if used_case_id == case_id
            }
            if "values" in member and projection is not None:
                alternatives_match = _alternatives_artifacts(
                    projection,
                    member,
                    subject_surfaces=subject_surfaces,
                    artifact_subject_aliases=artifact_subject_aliases,
                    excluded_artifact_ids=used_case_artifact_ids,
                )
                if alternatives_match is not None:
                    matched_ids, structural_group_id = alternatives_match
            elif (
                member.get("lifecycle")
                in {
                    "rescheduled_old",
                    "rescheduled_replacement",
                }
                and projection is not None
            ):
                reschedule_match = _reschedule_artifact(
                    projection,
                    member,
                    subject_surfaces=subject_surfaces,
                    artifact_subject_aliases=artifact_subject_aliases,
                    excluded_artifact_ids=used_case_artifact_ids,
                )
                if reschedule_match is not None:
                    matched_id, structural_group_id = reschedule_match
                    matched_ids = (matched_id,)
            else:
                artifact_id = _best_exact_artifact_id(
                    available,
                    member,
                    subject_surfaces=subject_surfaces,
                    artifact_subject_aliases=artifact_subject_aliases,
                    excluded_artifact_ids=used_case_artifact_ids,
                )
                if artifact_id is not None:
                    matched_ids = (artifact_id,)
            if not matched_ids or any(
                (case_id, artifact_id) in matched_artifact_ids
                for artifact_id in matched_ids
            ):
                continue
            matched_artifact_ids.update(
                (case_id, artifact_id) for artifact_id in matched_ids
            )
            matched_artifact_expected_verdicts.update(
                {
                    (case_id, artifact_id): str(expected_verdict)
                    for artifact_id in matched_ids
                }
            )
            matched_members += 1
            case_matches += 1
            lifecycle_matched_members[lifecycle] += 1
            relation_matched_members[relation] += 1
            critical_temporal_matched_members += int(critical_temporal)
            critical_category_matched_members.update(critical_categories)
            if component_key is not None and structural_group_id is not None:
                matched_component_members[component_key] += 1
                component_group_ids[component_key].add(structural_group_id)
            confirmed = _artifacts_confirm_supported_member(
                tuple(available[artifact_id] for artifact_id in matched_ids),
                member,
            )
            if confirmed:
                confirmed_members += 1
                case_confirmed += 1
                lifecycle_confirmed_members[lifecycle] += 1
                relation_confirmed_members[relation] += 1
            canonical_recovered = bool(
                canonical_subject_required
                and _artifacts_recover_canonical_subject(
                    tuple(available[artifact_id] for artifact_id in matched_ids),
                    member,
                    subject_surfaces=subject_surfaces,
                )
            )
            if canonical_recovered:
                canonical_subject_members_recovered += 1
                case_canonical_subject_members_recovered += 1
                assert canonical_title is not None
                recovered_canonical_title_keys.add((case_id, canonical_title))
                case_recovered_canonical_titles.add(canonical_title)
            member_outcomes[member_ordinal] = {
                "matched": True,
                "exact": (not canonical_subject_required or canonical_recovered),
                "structural_group_id": structural_group_id,
            }
        case_complete_components = sum(
            matched_component_members[component_key] == expected_members
            and len(component_group_ids[component_key]) == 1
            for component_key, expected_members in component_members.items()
        )
        complete_group_components_recovered += case_complete_components
        case_unit_metrics = _semantic_unit_metrics(members, member_outcomes)
        semantic_units += case_unit_metrics["semantic_units"]
        complete_units += case_unit_metrics["complete_units"]
        exact_units += case_unit_metrics["exact_units"]
        case_reschedule_metrics = _reschedule_unit_metrics(members, member_outcomes)
        reschedule_units += case_reschedule_metrics["reschedule_units"]
        complete_reschedule_units += case_reschedule_metrics[
            "complete_reschedule_units"
        ]
        positive_case_fully_recovered = bool(members) and case_matches == len(members)
        positive_cases_fully_recovered += int(positive_case_fully_recovered)
        per_case.append(
            {
                "case_id": case_id,
                "gold_members": len(members),
                "matched_members": case_matches,
                "confirmed_members": case_confirmed,
                "canonical_subject_members": case_canonical_subject_members,
                "canonical_subject_members_recovered": (
                    case_canonical_subject_members_recovered
                ),
                "canonical_titles": len(case_canonical_titles),
                "canonical_titles_recovered": len(case_recovered_canonical_titles),
                "artifacts": len(artifacts),
                "cluster_reviews": len(reviews),
                "complete_group_required": complete_group_required,
                "complete_group_components": len(component_members),
                "complete_group_components_recovered": case_complete_components,
                "semantic_units": case_unit_metrics["semantic_units"],
                "complete_units": case_unit_metrics["complete_units"],
                "exact_units": case_unit_metrics["exact_units"],
                "reschedule_units": case_reschedule_metrics["reschedule_units"],
                "complete_reschedule_units": case_reschedule_metrics[
                    "complete_reschedule_units"
                ],
                "candidate_bearing": case_candidate_bearing,
                "positive_case_fully_recovered": (
                    positive_case_fully_recovered if members else None
                ),
                "negative_selected": bool(not members and case_selected),
                "candidate_bearing_negative_selected": bool(
                    not members and case_candidate_bearing and case_selected
                ),
                "forbidden_hypotheses": case_forbidden,
            }
        )
    matched_supported_artifacts = sum(
        result_rows[case_id]["projection"] is not None
        and any(
            isinstance(item, Mapping)
            and item.get("artifact_id") == artifact_id
            and _supported_artifact_calibration(item, expected_verdict) == "calibrated"
            for item in result_rows[case_id]["projection"].get("artifacts", [])
        )
        for (case_id, artifact_id), expected_verdict in (
            matched_artifact_expected_verdicts.items()
        )
    )
    overconfident_artifacts = sum(
        result_rows[case_id]["projection"] is not None
        and any(
            isinstance(item, Mapping)
            and item.get("artifact_id") == artifact_id
            and _supported_artifact_calibration(item, expected_verdict)
            == "overconfident"
            for item in result_rows[case_id]["projection"].get("artifacts", [])
        )
        for (case_id, artifact_id), expected_verdict in (
            matched_artifact_expected_verdicts.items()
        )
    )
    effective_recall = matched_members / gold_members if gold_members else 1.0
    confirmed_recall = (
        confirmed_members / supported_gold_members if supported_gold_members else 1.0
    )
    canonical_subject_recall = (
        canonical_subject_members_recovered / canonical_subject_members
        if canonical_subject_members
        else 1.0
    )
    canonical_titles = len(canonical_title_keys)
    canonical_titles_recovered = len(recovered_canonical_title_keys)
    canonical_title_recall = (
        canonical_titles_recovered / canonical_titles if canonical_titles else 1.0
    )
    supported_precision = (
        matched_supported_artifacts / supported_artifacts
        if supported_artifacts
        else (1.0 if supported_gold_members == 0 else 0.0)
    )
    supported_overclaim_count = supported_artifacts - matched_supported_artifacts
    if supported_overclaim_count < 0:
        raise PublicChallengeError("supported artifact calibration is invalid")
    critical_artifact_overclaim_ids: set[tuple[str, str]] = set()
    critical_supported_overclaim_ids: set[tuple[str, str]] = set()
    for case_id in selected_ids:
        projection = result_rows[case_id]["projection"]
        if projection is None:
            continue
        for artifact in projection.get("artifacts", []):
            if not isinstance(artifact, Mapping):
                continue
            artifact_id = artifact.get("artifact_id")
            if not isinstance(artifact_id, str) or not artifact_id:
                raise PublicChallengeError("critical artifact identity is invalid")
            artifact_key = (case_id, artifact_id)
            if (
                artifact_key not in matched_artifact_ids
                and _artifact_has_critical_temporal_hypothesis(artifact)
            ):
                critical_artifact_overclaim_ids.add(artifact_key)
            if (
                artifact.get("evidence_status") == "supported"
                and _artifact_has_critical_temporal_hypothesis(artifact)
                and matched_artifact_expected_verdicts.get(artifact_key) != "supported"
            ):
                critical_supported_overclaim_ids.add(artifact_key)
    critical_artifact_overclaim_count = len(critical_artifact_overclaim_ids)
    critical_supported_overclaim_count = len(critical_supported_overclaim_ids)
    if critical_supported_overclaim_count > supported_overclaim_count:
        raise PublicChallengeError("critical artifact calibration is invalid")
    # Cluster reviews are escalation records, not semantic artifacts.  Current
    # gold has no representation against which their correctness can be
    # judged, so report them as unscored workload instead of silently counting
    # every escalation as a false-positive artifact.
    review_artifact_precision, all_review_outputs_scored = _review_artifact_metrics(
        artifact_count=total_artifacts,
        matched_artifact_count=len(matched_artifact_ids),
        cluster_review_count=cluster_reviews,
        gold_member_count=gold_members,
    )
    complete_group_recall = (
        complete_group_components_recovered / complete_group_components
        if complete_group_components
        else 1.0
    )
    complete_unit_recall = complete_units / semantic_units if semantic_units else 1.0
    exact_unit_recall = exact_units / semantic_units if semantic_units else 1.0
    reschedule_unit_recall = (
        complete_reschedule_units / reschedule_units if reschedule_units else None
    )
    positive_case_completeness_recall = positive_cases_fully_recovered / positive_cases
    rejected_candidate_bearing_negative_cases = (
        candidate_bearing_negative_cases - selected_candidate_bearing_negative_cases
    )
    candidate_bearing_negative_rejection_rate = (
        rejected_candidate_bearing_negative_cases / candidate_bearing_negative_cases
        if candidate_bearing_negative_cases
        else None
    )
    frontier_member_recall: float | None = None
    authenticated_positive_zero_work_cases: int | None = None
    if frontier_diagnostics is not None:
        frontier_aggregates = frontier_diagnostics["aggregates"]
        frontier_gold_members = int(recomputed_frontier["gold_members"])
        frontier_covered_gold_members = int(
            recomputed_frontier["frontier_covered_gold_members"]
        )
        frontier_member_recall = (
            frontier_covered_gold_members / frontier_gold_members
            if frontier_gold_members
            else 1.0
        )
        authenticated_positive_zero_work_cases = int(
            frontier_aggregates["positive_zero_work_cases"]
        )
    lifecycle_metrics: dict[str, dict[str, Any]] = {}
    for lifecycle in _LIFECYCLE_ROLES:
        lifecycle_gold = lifecycle_gold_members[lifecycle]
        lifecycle_matched = lifecycle_matched_members[lifecycle]
        lifecycle_supported = lifecycle_supported_gold_members[lifecycle]
        lifecycle_confirmed = lifecycle_confirmed_members[lifecycle]
        lifecycle_metrics[lifecycle] = {
            "gold_members": lifecycle_gold,
            "matched_members": lifecycle_matched,
            "effective_member_recall": (
                lifecycle_matched / lifecycle_gold if lifecycle_gold else None
            ),
            "effective_member_recall_interval_95": _wilson_interval(
                lifecycle_matched,
                lifecycle_gold,
            ),
            "supported_gold_members": lifecycle_supported,
            "confirmed_members": lifecycle_confirmed,
            "confirmed_member_recall": (
                lifecycle_confirmed / lifecycle_supported
                if lifecycle_supported
                else None
            ),
            "confirmed_member_recall_interval_95": _wilson_interval(
                lifecycle_confirmed,
                lifecycle_supported,
            ),
        }
    relation_metrics: dict[str, dict[str, Any]] = {}
    for relation in _TEMPORAL_RELATIONS:
        relation_gold = relation_gold_members[relation]
        relation_matched = relation_matched_members[relation]
        relation_supported = relation_supported_gold_members[relation]
        relation_confirmed = relation_confirmed_members[relation]
        relation_metrics[relation] = {
            "gold_members": relation_gold,
            "matched_members": relation_matched,
            "effective_member_recall": (
                relation_matched / relation_gold if relation_gold else None
            ),
            "effective_member_recall_interval_95": _wilson_interval(
                relation_matched,
                relation_gold,
            ),
            "supported_gold_members": relation_supported,
            "confirmed_members": relation_confirmed,
            "confirmed_member_recall": (
                relation_confirmed / relation_supported if relation_supported else None
            ),
            "confirmed_member_recall_interval_95": _wilson_interval(
                relation_confirmed,
                relation_supported,
            ),
        }
    critical_temporal_category_metrics: dict[str, dict[str, Any]] = {}
    for category in _CRITICAL_TEMPORAL_CATEGORIES:
        category_gold = critical_category_gold_members[category]
        category_matched = critical_category_matched_members[category]
        critical_temporal_category_metrics[category] = {
            "gold_members": category_gold,
            "matched_members": category_matched,
            "effective_member_recall": (
                category_matched / category_gold if category_gold else None
            ),
            "effective_member_recall_interval_95": _wilson_interval(
                category_matched,
                category_gold,
            ),
        }
    critical_temporal_effective_member_recall = (
        critical_temporal_matched_members / critical_temporal_gold_members
        if critical_temporal_gold_members
        else None
    )
    critical_temporal_metrics = {
        "basis": ("deadline_relation_or_scheduled_cancelled_reschedule_lifecycle"),
        "gold_members": critical_temporal_gold_members,
        "matched_members": critical_temporal_matched_members,
        "effective_member_recall": critical_temporal_effective_member_recall,
        "effective_member_recall_interval_95": _wilson_interval(
            critical_temporal_matched_members,
            critical_temporal_gold_members,
        ),
        "categories": critical_temporal_category_metrics,
    }
    critical_lifecycle_gold_members = sum(
        lifecycle_gold_members[role] for role in _CRITICAL_LIFECYCLE_ROLES
    )
    critical_lifecycle_matched_members = sum(
        lifecycle_matched_members[role] for role in _CRITICAL_LIFECYCLE_ROLES
    )
    critical_lifecycle_effective_member_recall = (
        critical_lifecycle_matched_members / critical_lifecycle_gold_members
        if critical_lifecycle_gold_members
        else None
    )
    critical_lifecycle_metrics = {
        "roles": list(_CRITICAL_LIFECYCLE_ROLES),
        "gold_members": critical_lifecycle_gold_members,
        "matched_members": critical_lifecycle_matched_members,
        "effective_member_recall": critical_lifecycle_effective_member_recall,
        "effective_member_recall_interval_95": _wilson_interval(
            critical_lifecycle_matched_members,
            critical_lifecycle_gold_members,
        ),
    }
    lifecycle_reporting_complete = bool(
        set(lifecycle_metrics) == set(_LIFECYCLE_ROLES)
        and sum(row["gold_members"] for row in lifecycle_metrics.values())
        == gold_members
        and sum(row["matched_members"] for row in lifecycle_metrics.values())
        == matched_members
        and sum(row["supported_gold_members"] for row in lifecycle_metrics.values())
        == supported_gold_members
        and sum(row["confirmed_members"] for row in lifecycle_metrics.values())
        == confirmed_members
    )
    relation_reporting_complete = bool(
        set(relation_metrics) == set(_TEMPORAL_RELATIONS)
        and sum(row["gold_members"] for row in relation_metrics.values())
        == gold_members
        and sum(row["matched_members"] for row in relation_metrics.values())
        == matched_members
        and sum(row["supported_gold_members"] for row in relation_metrics.values())
        == supported_gold_members
        and sum(row["confirmed_members"] for row in relation_metrics.values())
        == confirmed_members
    )
    all_critical_temporal_categories_present = all(
        critical_temporal_category_metrics[category]["gold_members"] > 0
        for category in _CRITICAL_TEMPORAL_CATEGORIES
    )
    critical_temporal_category_minimum_recall = min(
        (
            float(row["effective_member_recall"])
            for row in critical_temporal_category_metrics.values()
            if row["effective_member_recall"] is not None
        ),
        default=None,
    )
    metric_intervals_95 = {
        "effective_member_recall": _wilson_interval(
            matched_members,
            gold_members,
        ),
        "confirmed_member_recall": _wilson_interval(
            confirmed_members,
            supported_gold_members,
        ),
        "supported_artifact_precision": _wilson_interval(
            matched_supported_artifacts,
            supported_artifacts,
        ),
        "review_artifact_precision": _wilson_interval(
            len(matched_artifact_ids),
            total_artifacts,
        ),
        "canonical_title_recall": _wilson_interval(
            canonical_titles_recovered,
            canonical_titles,
        ),
        "canonical_subject_recall": _wilson_interval(
            canonical_subject_members_recovered,
            canonical_subject_members,
        ),
        "complete_group_recall": _wilson_interval(
            complete_group_components_recovered,
            complete_group_components,
        ),
        "complete_unit_recall": _wilson_interval(
            complete_units,
            semantic_units,
        ),
        "exact_unit_recall": _wilson_interval(
            exact_units,
            semantic_units,
        ),
        "critical_lifecycle_effective_member_recall": _wilson_interval(
            critical_lifecycle_matched_members,
            critical_lifecycle_gold_members,
        ),
        "deadline_effective_member_recall": _wilson_interval(
            relation_matched_members["deadline"],
            relation_gold_members["deadline"],
        ),
        "critical_temporal_effective_member_recall": _wilson_interval(
            critical_temporal_matched_members,
            critical_temporal_gold_members,
        ),
        "reschedule_unit_recall": _wilson_interval(
            complete_reschedule_units,
            reschedule_units,
        ),
        "positive_case_completeness_recall": _wilson_interval(
            positive_cases_fully_recovered,
            positive_cases,
        ),
    }
    restricted_external_execution = bool(
        claims.restricted_execution
        and claims.external_call_started
        and not claims.test_invoker_used
    )
    gate = {
        "all_members_recovered": matched_members == gold_members,
        "all_supported_members_confirmed": (
            confirmed_members == supported_gold_members
        ),
        "all_canonical_subjects_recovered": (
            canonical_subject_members_recovered == canonical_subject_members
        ),
        "perfect_supported_precision": supported_precision == 1.0,
        "zero_supported_overclaims": supported_overclaim_count == 0,
        "perfect_review_artifact_precision": review_artifact_precision == 1.0,
        "all_review_outputs_scored": all_review_outputs_scored,
        "complete_structural_groups": (
            complete_group_components_recovered == complete_group_components
        ),
        "no_selected_hard_negatives": selected_negative_cases == 0,
        "no_selected_negative_cases": selected_negative_cases == 0,
        "no_forbidden_hypotheses": forbidden_hypotheses == 0,
        "restricted_external_execution": restricted_external_execution,
    }
    personal_target_gates = {
        "authenticated_zero_correction_sol_gold_audit": gold_audit is not None,
        "frontier_member_recall_at_least_0_95": bool(
            frontier_member_recall is not None
            and frontier_member_recall >= MIN_FRONTIER_MEMBER_RECALL
        ),
        "zero_authenticated_positive_zero_work_cases": (
            authenticated_positive_zero_work_cases == 0
        ),
        "effective_member_recall_at_least_0_90": (
            effective_recall >= MIN_EFFECTIVE_MEMBER_RECALL
        ),
        "confirmed_member_recall_at_least_0_90": (
            confirmed_recall >= MIN_CONFIRMED_MEMBER_RECALL
        ),
        "complete_unit_recall_at_least_0_90": (
            complete_unit_recall >= MIN_COMPLETE_UNIT_RECALL
        ),
        "exact_unit_recall_at_least_0_90": (exact_unit_recall >= MIN_EXACT_UNIT_RECALL),
        "critical_lifecycle_effective_member_recall_at_least_0_95": bool(
            critical_lifecycle_effective_member_recall is not None
            and critical_lifecycle_effective_member_recall
            >= MIN_CRITICAL_LIFECYCLE_EFFECTIVE_RECALL
        ),
        "critical_temporal_effective_member_recall_at_least_0_95": bool(
            critical_temporal_effective_member_recall is not None
            and critical_temporal_effective_member_recall
            >= MIN_CRITICAL_TEMPORAL_EFFECTIVE_RECALL
        ),
        "all_critical_temporal_categories_present": (
            all_critical_temporal_categories_present
        ),
        "each_critical_temporal_category_recall_at_least_0_95": bool(
            all_critical_temporal_categories_present
            and critical_temporal_category_minimum_recall is not None
            and critical_temporal_category_minimum_recall
            >= MIN_CRITICAL_TEMPORAL_CATEGORY_RECALL
        ),
        "deadline_relation_recall_at_least_0_95": bool(
            relation_metrics["deadline"]["effective_member_recall"] is not None
            and relation_metrics["deadline"]["effective_member_recall"]
            >= MIN_CRITICAL_TEMPORAL_CATEGORY_RECALL
        ),
        "reschedule_unit_recall_at_least_0_95": bool(
            reschedule_unit_recall is not None
            and reschedule_unit_recall >= MIN_CRITICAL_TEMPORAL_CATEGORY_RECALL
        ),
        "canonical_title_recall_at_least_0_90": (
            canonical_title_recall >= MIN_CANONICAL_TITLE_RECALL
        ),
        "canonical_subject_recall_at_least_0_90": (
            canonical_subject_recall >= MIN_CANONICAL_SUBJECT_RECALL
        ),
        "supported_artifact_precision_at_least_0_95": (
            supported_precision >= MIN_SUPPORTED_ARTIFACT_PRECISION
        ),
        "zero_supported_critical_overclaims": (critical_supported_overclaim_count == 0),
        "zero_critical_artifact_overclaims": (critical_artifact_overclaim_count == 0),
        "review_artifact_precision_at_least_0_90": (
            review_artifact_precision >= MIN_REVIEW_ARTIFACT_PRECISION
        ),
        "accepted_parent_cluster_stability_at_least_0_95": bool(
            three_run_stability["accepted_parent_clusters"]["gate_passed"]
        ),
        "accepted_gold_member_stability_at_least_0_95": bool(
            three_run_stability["accepted_gold_members"]["gate_passed"]
        ),
        "critical_candidate_verdict_stability_at_least_0_95": bool(
            three_run_stability["critical_candidate_verdict_agreement"]["gate_passed"]
        ),
        "critical_gold_member_verdict_stability_at_least_0_95": bool(
            three_run_stability["critical_gold_member_verdict_agreement"]["gate_passed"]
        ),
        "candidate_bearing_negative_rejection_at_least_0_80": bool(
            candidate_bearing_negative_rejection_rate is not None
            and candidate_bearing_negative_rejection_rate
            >= MIN_CANDIDATE_BEARING_NEGATIVE_REJECTION
        ),
        "no_forbidden_hypotheses": forbidden_hypotheses == 0,
        "complete_lifecycle_reporting": lifecycle_reporting_complete,
        "complete_relation_reporting": relation_reporting_complete,
        "restricted_external_execution": restricted_external_execution,
    }
    personal_target_gate_available = gold_audit is not None
    personal_target_gate_passed = bool(
        personal_target_gate_available and all(personal_target_gates.values())
    )
    production_release_evidence_gates = {
        "personal_quality_target_passed": personal_target_gate_passed,
        "blind_first_use": evaluation_mode == "blind_first_use",
        "cohort_release_eligible": challenge.get("release_eligible") is True,
    }
    production_release_evidence_gate_passed = all(
        production_release_evidence_gates.values()
    )
    score = _signed(
        {
            "version": SCORE_VERSION,
            "launcher_version": VERSION,
            "challenge_id": challenge["challenge_id"],
            "challenge_manifest_sha256": challenge_sha,
            "gold_sha256": _sha256(gold_raw),
            "prediction_seal_sha256": _sha256(seal_raw),
            "result_sha256": _sha256(result_raw),
            "prediction_launcher_sha256": (prediction_provenance.launcher_sha256),
            "prediction_launcher_trust_basis": (prediction_provenance.trust_basis),
            "prediction_launcher_exact_artifact_verified": (
                prediction_provenance.exact_artifact_verified
            ),
            "scorer_sha256": prediction_provenance.scorer_sha256,
            "frontier_diagnostics": frontier_diagnostics,
            "gold_audit": gold_audit,
            "gold_opened_after_this_prediction_seal": True,
            "operator_asserted_evaluation_mode": evaluation_mode,
            "first_use_blindness_claimed": evaluation_mode == "blind_first_use",
            "gold_members": gold_members,
            "supported_gold_members": supported_gold_members,
            "matched_members": matched_members,
            "confirmed_members": confirmed_members,
            "canonical_subject_members": canonical_subject_members,
            "canonical_subject_members_recovered": (
                canonical_subject_members_recovered
            ),
            "canonical_titles": canonical_titles,
            "canonical_titles_recovered": canonical_titles_recovered,
            "artifacts": total_artifacts,
            "supported_artifacts": supported_artifacts,
            "matched_supported_artifacts": matched_supported_artifacts,
            "overconfident_artifacts": overconfident_artifacts,
            "supported_overclaim_count": supported_overclaim_count,
            "critical_supported_overclaim_count": (critical_supported_overclaim_count),
            "critical_artifact_overclaim_count": (critical_artifact_overclaim_count),
            "matched_artifacts": len(matched_artifact_ids),
            "cluster_reviews": cluster_reviews,
            "unscored_cluster_reviews": cluster_reviews,
            "positive_cases": positive_cases,
            "positive_cases_fully_recovered": positive_cases_fully_recovered,
            "positive_case_completeness_recall": (positive_case_completeness_recall),
            "negative_cases": negative_cases,
            "selected_negative_cases": selected_negative_cases,
            "candidate_bearing_negative_cases": (candidate_bearing_negative_cases),
            "selected_candidate_bearing_negative_cases": (
                selected_candidate_bearing_negative_cases
            ),
            "rejected_candidate_bearing_negative_cases": (
                rejected_candidate_bearing_negative_cases
            ),
            "candidate_bearing_negative_rejection_rate": (
                candidate_bearing_negative_rejection_rate
            ),
            "frontier_member_recall": frontier_member_recall,
            "authenticated_positive_zero_work_cases": (
                authenticated_positive_zero_work_cases
            ),
            "candidate_bearing_negative_case_basis": (
                "authenticated_prediction_preparation_candidate_count"
            ),
            "forbidden_hypotheses": forbidden_hypotheses,
            "complete_group_components": complete_group_components,
            "complete_group_components_recovered": (
                complete_group_components_recovered
            ),
            "semantic_units": semantic_units,
            "complete_units": complete_units,
            "exact_units": exact_units,
            "reschedule_units": reschedule_units,
            "complete_reschedule_units": complete_reschedule_units,
            "reschedule_unit_recall": reschedule_unit_recall,
            "effective_member_recall": effective_recall,
            "confirmed_member_recall": confirmed_recall,
            "canonical_subject_recall": canonical_subject_recall,
            "canonical_title_recall": canonical_title_recall,
            "supported_artifact_precision": supported_precision,
            "review_artifact_precision": review_artifact_precision,
            "review_output_precision": review_artifact_precision,
            "complete_group_recall": complete_group_recall,
            "complete_unit_recall": complete_unit_recall,
            "exact_unit_recall": exact_unit_recall,
            "exact_unit_basis": (
                "exact_semantic_binding_and_required_canonical_subject_recovery"
            ),
            "lifecycle_metrics": lifecycle_metrics,
            "critical_lifecycle_metrics": critical_lifecycle_metrics,
            "lifecycle_reporting_complete": lifecycle_reporting_complete,
            "relation_metrics": relation_metrics,
            "relation_reporting_complete": relation_reporting_complete,
            "critical_temporal_metrics": critical_temporal_metrics,
            "critical_temporal_category_minimum_recall": (
                critical_temporal_category_minimum_recall
            ),
            "all_critical_temporal_categories_present": (
                all_critical_temporal_categories_present
            ),
            "three_run_stability": three_run_stability,
            "metric_intervals_95": metric_intervals_95,
            "confidence_interval_method": "wilson_score_95_two_sided",
            "cases": per_case,
            "gates": gate,
            "smoke_gate_passed": all(gate.values()),
            "personal_target_gates": personal_target_gates,
            "personal_target_gate_available": personal_target_gate_available,
            "personal_target_gate_passed": personal_target_gate_passed,
            "production_release_evidence_gates": (production_release_evidence_gates),
            "production_release_evidence_gate_passed": (
                production_release_evidence_gate_passed
            ),
            "public_synthetic": True,
            "gold_version": gold.get("version"),
            "release_eligible": False,
            "scored_at": _now(),
        },
        key=key,
        domain=SCORE_DOMAIN,
        signature_field="score_hmac_sha256",
    )
    _write_private_new(output_root / "score.json", _canonical_json(score) + b"\n")
    return {
        "version": SCORE_VERSION,
        "status": "complete",
        "gold_members": gold_members,
        "matched_members": matched_members,
        "supported_gold_members": supported_gold_members,
        "confirmed_members": confirmed_members,
        "canonical_subject_members": canonical_subject_members,
        "canonical_subject_members_recovered": canonical_subject_members_recovered,
        "canonical_titles": canonical_titles,
        "canonical_titles_recovered": canonical_titles_recovered,
        "artifacts": total_artifacts,
        "supported_artifacts": supported_artifacts,
        "matched_supported_artifacts": matched_supported_artifacts,
        "overconfident_artifacts": overconfident_artifacts,
        "supported_overclaim_count": supported_overclaim_count,
        "critical_supported_overclaim_count": critical_supported_overclaim_count,
        "critical_artifact_overclaim_count": critical_artifact_overclaim_count,
        "cluster_reviews": cluster_reviews,
        "unscored_cluster_reviews": cluster_reviews,
        "positive_cases": positive_cases,
        "positive_cases_fully_recovered": positive_cases_fully_recovered,
        "positive_case_completeness_recall": positive_case_completeness_recall,
        "negative_cases": negative_cases,
        "selected_negative_cases": selected_negative_cases,
        "candidate_bearing_negative_cases": candidate_bearing_negative_cases,
        "selected_candidate_bearing_negative_cases": (
            selected_candidate_bearing_negative_cases
        ),
        "rejected_candidate_bearing_negative_cases": (
            rejected_candidate_bearing_negative_cases
        ),
        "candidate_bearing_negative_rejection_rate": (
            candidate_bearing_negative_rejection_rate
        ),
        "frontier_member_recall": frontier_member_recall,
        "authenticated_positive_zero_work_cases": (
            authenticated_positive_zero_work_cases
        ),
        "candidate_bearing_negative_case_basis": (
            "authenticated_prediction_preparation_candidate_count"
        ),
        "forbidden_hypotheses": forbidden_hypotheses,
        "effective_member_recall": effective_recall,
        "confirmed_member_recall": confirmed_recall,
        "canonical_subject_recall": canonical_subject_recall,
        "canonical_title_recall": canonical_title_recall,
        "supported_artifact_precision": supported_precision,
        "review_artifact_precision": review_artifact_precision,
        "review_output_precision": review_artifact_precision,
        "complete_group_recall": complete_group_recall,
        "semantic_units": semantic_units,
        "complete_units": complete_units,
        "exact_units": exact_units,
        "reschedule_units": reschedule_units,
        "complete_reschedule_units": complete_reschedule_units,
        "reschedule_unit_recall": reschedule_unit_recall,
        "complete_unit_recall": complete_unit_recall,
        "exact_unit_recall": exact_unit_recall,
        "exact_unit_basis": (
            "exact_semantic_binding_and_required_canonical_subject_recovery"
        ),
        "lifecycle_metrics": lifecycle_metrics,
        "critical_lifecycle_metrics": critical_lifecycle_metrics,
        "lifecycle_reporting_complete": lifecycle_reporting_complete,
        "relation_metrics": relation_metrics,
        "relation_reporting_complete": relation_reporting_complete,
        "critical_temporal_metrics": critical_temporal_metrics,
        "critical_temporal_category_minimum_recall": (
            critical_temporal_category_minimum_recall
        ),
        "all_critical_temporal_categories_present": (
            all_critical_temporal_categories_present
        ),
        "three_run_stability": three_run_stability,
        "metric_intervals_95": metric_intervals_95,
        "confidence_interval_method": "wilson_score_95_two_sided",
        "complete_group_components": complete_group_components,
        "complete_group_components_recovered": complete_group_components_recovered,
        "cases": per_case,
        "gates": gate,
        "smoke_gate_passed": all(gate.values()),
        "personal_target_gates": personal_target_gates,
        "personal_target_gate_available": personal_target_gate_available,
        "personal_target_gate_passed": personal_target_gate_passed,
        "production_release_evidence_gates": production_release_evidence_gates,
        "production_release_evidence_gate_passed": (
            production_release_evidence_gate_passed
        ),
        "gold_opened_after_this_prediction_seal": True,
        "operator_asserted_evaluation_mode": evaluation_mode,
        "first_use_blindness_claimed": evaluation_mode == "blind_first_use",
        "public_synthetic": True,
        "gold_version": gold.get("version"),
        "prediction_launcher_sha256": prediction_provenance.launcher_sha256,
        "prediction_launcher_trust_basis": prediction_provenance.trust_basis,
        "prediction_launcher_exact_artifact_verified": (
            prediction_provenance.exact_artifact_verified
        ),
        "scorer_sha256": prediction_provenance.scorer_sha256,
        "frontier_diagnostics": frontier_diagnostics,
        "gold_audit": gold_audit,
        "release_eligible": False,
        "test_invoker_used": claims.test_invoker_used,
        "private_content_printed": False,
    }


def _safe_failure(phase: str) -> dict[str, Any]:
    return {
        "version": VERSION,
        "phase": phase,
        "status": "failed",
        "error": "public_temporal_challenge_failed",
        "public_synthetic": True,
        "release_eligible": False,
        "private_content_printed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="phase", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--challenge", type=Path, required=True)
    run.add_argument("--hmac-key", type=Path, required=True)
    run.add_argument("--output-root", type=Path, required=True)
    run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    run.add_argument("--codex-binary")
    score = subparsers.add_parser("score")
    score.add_argument("--challenge", type=Path, required=True)
    score.add_argument("--gold", type=Path, required=True)
    score.add_argument("--hmac-key", type=Path, required=True)
    score.add_argument("--output-root", type=Path, required=True)
    score.add_argument("--prediction-launcher-artifact", type=Path)
    score.add_argument("--frontier-diagnostics", type=Path)
    score.add_argument("--gold-audit-root", type=Path)
    score.add_argument(
        "--evaluation-mode",
        choices=("blind_first_use", "development_replay"),
        required=True,
    )
    args = parser.parse_args()
    try:
        if args.phase == "run":
            result = run_public_challenge(
                args.challenge,
                args.hmac_key,
                args.output_root,
                timeout_seconds=args.timeout,
                codex_binary=args.codex_binary,
            )
        else:
            result = score_public_challenge(
                args.challenge,
                args.gold,
                args.hmac_key,
                args.output_root,
                evaluation_mode=args.evaluation_mode,
                prediction_launcher_artifact=args.prediction_launcher_artifact,
                frontier_diagnostics_path=args.frontier_diagnostics,
                gold_audit_root=args.gold_audit_root,
            )
    except (PublicChallengeError, OSError, ValueError):
        print(json.dumps(_safe_failure(str(args.phase)), sort_keys=True))
        raise SystemExit(2) from None
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
