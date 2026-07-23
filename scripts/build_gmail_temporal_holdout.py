#!/usr/bin/env python3
"""Freeze a private, thread-independent Gmail temporal release holdout.

The builder performs only local deterministic preparation.  It selects one
message per thread, writes HMAC-opaque source packets plus the exact sanitized
verifier requests produced by the authoritative runner, and emits only
aggregate counts on stdout.  It does not call a model or persist review output.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from pkm_brain.db import connection
from pkm_brain.gmail_sensitive_data import sanitize_gmail_model_payload
from pkm_brain.gmail_temporal_batching import plan_gmail_temporal_selector_batches
from pkm_brain.gmail_temporal_leads import (
    TemporalLeadAnalysis,
    analyze_gmail_temporal_leads,
)
from pkm_brain.gmail_temporal_runner import (
    GmailTemporalRunnerError,
    _admission_basis,
    _build_authority,
    _load_trusted_message,
    gmail_temporal_admission_policy_fingerprint,
    gmail_temporal_runner_policy_fingerprint,
)
from pkm_brain.gmail_temporal_verifier import (
    gmail_temporal_verifier_policy_fingerprint,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.source_dates import (
    source_frontmatter_with_path,
    strict_int,
    trusted_gmail_message_policies,
)


VERSION = "gmail_temporal_holdout_builder_v5"
SAMPLE_VERSION = "gmail_temporal_holdout_sample_v2"
BINDING_VERSION = "gmail_temporal_holdout_binding_v1"
REQUEST_VERSION = "gmail_temporal_holdout_request_v1"
LABEL_QUEUE_VERSION = "gmail_temporal_holdout_source_label_queue_v2"
LABEL_MANIFEST_VERSION = "gmail_temporal_holdout_label_manifest_v2"
RESERVE_ORDER_VERSION = "gmail_temporal_holdout_reserve_order_v2"
MANIFEST_VERSION = "gmail_temporal_holdout_manifest_v5"
SELECTION_POLICY_VERSION = "gmail_temporal_holdout_selection_v4"
CHALLENGE_SELECTION_POLICY_VERSION = "gmail_temporal_holdout_challenge_selection_v4"
MANIFEST_HMAC_DOMAIN = b"gmail_temporal_holdout_manifest_v5\0"
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
MIN_HMAC_KEY_BYTES = 32
MAX_PRIVATE_INPUT_BYTES = 32 * 1024 * 1024
CONTEXT_MESSAGES_PER_SIDE = 2
CONTEXT_MESSAGE_CHAR_CAP = 3_000
DEFAULT_SAMPLE_SIZE = 150
DEFAULT_CHALLENGE_SIZE = 100
DEFAULT_RESERVE_SIZE = 75
MAX_CHALLENGE_TOTAL_PAGES = 185
MAX_CHALLENGE_TOTAL_CANDIDATES = 275
MAX_CHALLENGE_MESSAGE_PAGES = 30
DIAGNOSTIC_EVIDENCE_CLASS = "diagnostic_only"
RETROSPECTIVE_EVIDENCE_CLASS = "retrospective_label_blind_review_only"
PROSPECTIVE_EVIDENCE_CLASS = "prospective_thread_unseen_review_only"
BASELINE_BACKED_EVIDENCE_CLASSES = frozenset(
    {RETROSPECTIVE_EVIDENCE_CLASS, PROSPECTIVE_EVIDENCE_CLASS}
)
RELEASE_EVIDENCE_CLASSES = frozenset({PROSPECTIVE_EVIDENCE_CLASS})

DEVELOPMENT_BASELINE_THREAD_ARTIFACT = "development-thread-scopes.jsonl"
DEVELOPMENT_BASELINE_MANIFEST_ARTIFACT = "development-baseline-manifest.json"
DEVELOPMENT_BASELINE_THREAD_VERSION = "gmail_temporal_development_thread_scope_v1"
DEVELOPMENT_BASELINE_MANIFEST_VERSION = (
    "gmail_temporal_development_baseline_manifest_v2"
)
DEVELOPMENT_BASELINE_THREAD_NAMESPACE = "gmail_temporal_thread_scope_v1"
DEVELOPMENT_BASELINE_MANIFEST_NAMESPACE = "gmail_temporal_development_manifest_v1"
FREEZE_AUTHORITY_VERSION = "gmail_temporal_holdout_freeze_authority_v1"
FREEZE_ATTEMPT_VERSION = "gmail_temporal_holdout_freeze_attempt_v1"
FREEZE_OUTCOME_VERSION = "gmail_temporal_holdout_freeze_outcome_v1"
FREEZE_AUTHORITY_MANIFEST_ARTIFACT = "authority.json"
FREEZE_AUTHORITY_DOMAIN = b"gmail_temporal_holdout_freeze_authority_v1\0"
FREEZE_ATTEMPT_DOMAIN = b"gmail_temporal_holdout_freeze_attempt_v1\0"
FREEZE_OUTCOME_DOMAIN = b"gmail_temporal_holdout_freeze_outcome_v1\0"
FREEZE_AUTHORITY_SCOPE = "retained_owner_gmail_temporal_holdout_milestones"
FREEZE_NO_REROLL_SCOPE = "within_single_retained_owner_authority_root"
_MILESTONE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ATTEMPT_ID_PATTERN = re.compile(r"^gthfa_[0-9a-f]{64}$")

# Multi-label quotas deliberately oversample the recall and likely-false-positive
# surfaces.  Fact, rescue, and not-admitted hard-negative admission classes are
# mutually exclusive, so their minima must still fit inside the 100 independent
# threads with slack for unanticipated combinations.  Explicit lifecycle-role
# minima prevent common "scheduled" examples from masking reschedule,
# cancellation, and completion gaps.  The live canary, rather than this
# challenge-weighted cohort, owns natural-prevalence review-volume measurement.
DEFAULT_QUOTAS: tuple[tuple[str, int], ...] = (
    ("preparation_failure", 1),
    ("bulk_candidate", 3),
    ("weak_advertising_candidate", 10),
    ("lifecycle_source", 10),
    ("lifecycle_candidate", 5),
    ("lifecycle_role_rescheduled", 2),
    ("lifecycle_role_cancelled", 5),
    ("lifecycle_role_completed", 5),
    ("temporal_form_without_candidate", 10),
    ("long_tail_candidate", 10),
    ("extreme_long_tail_candidate", 1),
    ("fact_candidate", 25),
    ("temporal_rescue_candidate", 25),
    ("hard_negative", 30),
    ("admitted_zero_candidate", 10),
    ("candidate_bearing", 55),
)

_ERROR_BUCKETS = {
    "temporal batch authority is incomplete": "batch_incomplete",
    "temporal candidate frontier is incomplete": "frontier_incomplete",
    "temporal candidate page plan is incomplete": "page_plan_incomplete",
    "temporal analysis authority is incomplete": "analysis_incomplete",
}


class GmailTemporalHoldoutError(ValueError):
    """Raised when a private holdout cannot be frozen safely and completely."""


def _validate_requested_cohort_sizes(
    evidence_class: str,
    *,
    sample_size: int,
    challenge_size: int,
    reserve_size: int,
) -> None:
    if evidence_class in BASELINE_BACKED_EVIDENCE_CLASSES and (
        sample_size,
        challenge_size,
        reserve_size,
    ) != (DEFAULT_SAMPLE_SIZE, DEFAULT_CHALLENGE_SIZE, DEFAULT_RESERVE_SIZE):
        raise GmailTemporalHoldoutError(
            "baseline-backed holdout sizes must be exactly 150/100/75"
        )


_SAFE_FAILURE_CODES = {
    "holdout scan did not cover every active Gmail target": "target_scan_incomplete",
    "Gmail source corpus changed during holdout scan": "corpus_changed",
    "fresh Gmail tail cannot fill primary and reserve cohorts": "fresh_tail_too_small",
    "recent Gmail holdout sample could not be filled": "fresh_tail_selection_failed",
    "holdout challenge quota is unavailable": "challenge_quota_unavailable",
    "exact holdout challenge constraints are infeasible": "challenge_infeasible",
    "exact holdout challenge selection was not proven optimal": "challenge_infeasible_or_inconclusive",
    "exact holdout challenge selection failed verification": "challenge_verification_failed",
    "exact holdout challenge canonical tie-break was not unique": (
        "challenge_canonical_tie_break_not_unique"
    ),
    "exact holdout challenge canonical tie-break was not proven": (
        "challenge_canonical_tie_break_inconclusive"
    ),
    "Gmail holdout splits are not thread-disjoint": "split_overlap",
    "selected Gmail authority changed during holdout freeze": "selected_authority_changed",
    "selected Gmail source changed during holdout freeze": "selected_source_changed",
    "selected Gmail context authority changed during freeze": "context_authority_changed",
    "holdout output already exists": "output_exists",
    "holdout output parent is unsafe": "output_parent_unsafe",
    "release holdout requires canonical freeze authority": (
        "canonical_freeze_authority_required"
    ),
    "freeze milestone and evidence class were already attempted": (
        "freeze_identity_already_attempted"
    ),
    "baseline-backed holdout sizes must be exactly 150/100/75": (
        "invalid_baseline_backed_cohort_size"
    ),
}


@dataclass(frozen=True)
class _Target:
    document_id: str
    message_id: str
    account_key: str
    thread_key: str
    source_revision: str
    document_content_hash: str
    message_position: int
    retained_message_count: int
    omitted_message_count: int
    truncated_message_count: int
    message_ids: tuple[str, ...]


@dataclass(frozen=True)
class _Discovery:
    targets: tuple[_Target, ...]
    active_document_count: int
    corpus_fingerprint: str


@dataclass(frozen=True)
class _Candidate:
    document_id: str
    message_id: str
    account_key: str
    thread_key: str
    source_revision: str
    source_sha256: str
    message_internal_at: str
    context_before_count: int
    context_after_count: int
    omitted_message_count: int
    thread_truncated_message_count: int
    target_body_truncation_status: str
    message_ids: tuple[str, ...]
    admission_basis: str
    disposition: str
    error_bucket: str | None
    expression_count: int
    mention_count: int
    candidate_count: int
    page_count: int
    policy: Mapping[str, Any]
    expression_forms: tuple[str, ...]
    lifecycle_roles: tuple[str, ...]
    strata: frozenset[str]
    rank: str
    fingerprint: str


@dataclass(frozen=True)
class _Materialized:
    candidate: _Candidate
    text: str
    analysis: TemporalLeadAnalysis
    requests: tuple[Any, ...]
    analysis_fingerprint: str
    batch_plan_fingerprint: str
    target_fingerprint: str | None
    context: tuple[_ContextSource, ...]


@dataclass(frozen=True)
class _ContextSource:
    relative_position: int
    message_id: str
    message_internal_at: str
    text: str


@dataclass(frozen=True)
class _Selection:
    primary: tuple[_Candidate, ...]
    challenge: tuple[_Candidate, ...]
    reserve: tuple[_Candidate, ...]
    fresh_after: str
    fresh_thread_count: int


@dataclass(frozen=True)
class _DevelopmentBaseline:
    thread_scope_ids: frozenset[str]
    corpus_fingerprint: str
    artifact_set_sha256: str
    manifest_sha256: str


@dataclass(frozen=True)
class _FreezeAttempt:
    authority_root: Path
    authority_manifest_sha256: str
    attempt_id: str
    attempt_sha256: str
    milestone: str
    evidence_class: str


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


def _private_hmac_key(path: Path) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        info = os.fstat(descriptor)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            value = handle.read()
    except OSError as exc:
        raise GmailTemporalHoldoutError("HMAC key is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != PRIVATE_FILE_MODE
        or info.st_nlink != 1
        or len(value) < MIN_HMAC_KEY_BYTES
    ):
        raise GmailTemporalHoldoutError(
            "HMAC key must be an owner-only single-link regular file of at least 32 bytes"
        )
    return value


def _private_file_bytes(path: Path) -> bytes:
    descriptor: int | None = None
    info: os.stat_result | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        info = os.fstat(descriptor)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            value = handle.read(MAX_PRIVATE_INPUT_BYTES + 1)
    except OSError as exc:
        raise GmailTemporalHoldoutError("private holdout input is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        info is None
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != PRIVATE_FILE_MODE
        or info.st_nlink != 1
        or info.st_size != len(value)
        or len(value) > MAX_PRIVATE_INPUT_BYTES
    ):
        raise GmailTemporalHoldoutError(
            "private holdout input is not an owner-only regular file"
        )
    return value


def _baseline_hmac_hex(key: bytes, namespace: str, value: Any) -> str:
    payload = namespace.encode("ascii") + b"\0" + _canonical_json(value)
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _baseline_thread_scope_id(
    key: bytes,
    account_key: str,
    thread_key: str,
) -> str:
    return "gtdb_t_" + _baseline_hmac_hex(
        key,
        DEVELOPMENT_BASELINE_THREAD_NAMESPACE,
        {"account_key": account_key, "thread_id": thread_key},
    )


def _load_development_baseline(
    root: Path,
    *,
    key: bytes,
) -> _DevelopmentBaseline:
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise GmailTemporalHoldoutError("development baseline is unavailable") from exc
    if (
        root.is_symlink()
        or not stat.S_ISDIR(root_info.st_mode)
        or root_info.st_uid != os.getuid()
        or stat.S_IMODE(root_info.st_mode) != PRIVATE_DIRECTORY_MODE
    ):
        raise GmailTemporalHoldoutError("development baseline directory is unsafe")
    scopes_bytes = _private_file_bytes(root / DEVELOPMENT_BASELINE_THREAD_ARTIFACT)
    manifest_bytes = _private_file_bytes(root / DEVELOPMENT_BASELINE_MANIFEST_ARTIFACT)
    try:
        manifest = json.loads(manifest_bytes)
        rows = [json.loads(line) for line in scopes_bytes.splitlines()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GmailTemporalHoldoutError("development baseline is malformed") from exc
    if not isinstance(manifest, dict) or not all(isinstance(row, dict) for row in rows):
        raise GmailTemporalHoldoutError("development baseline is malformed")
    manifest_without_hmac = dict(manifest)
    manifest_hmac = manifest_without_hmac.pop("manifest_hmac", None)
    expected_manifest_hmac = _baseline_hmac_hex(
        key,
        DEVELOPMENT_BASELINE_MANIFEST_NAMESPACE,
        manifest_without_hmac,
    )
    artifact = manifest.get("thread_scope_artifact")
    expected_id_namespace = "gtdb_k_" + _baseline_hmac_hex(
        key,
        DEVELOPMENT_BASELINE_THREAD_NAMESPACE,
        "id-namespace",
    )
    scopes_sha256 = _sha256_bytes(scopes_bytes)
    artifact_set_sha256 = _sha256_bytes(
        _canonical_json({DEVELOPMENT_BASELINE_THREAD_ARTIFACT: scopes_sha256})
    )
    allowed_row_keys = {
        "version",
        "thread_scope_id",
        "source_authority_commitment",
    }
    allowed_row_keys_with_count = {
        *allowed_row_keys,
        "message_count_commitment",
    }
    scope_ids: list[str] = []
    for row in rows:
        row_keys = set(row)
        if (
            row_keys != allowed_row_keys and row_keys != allowed_row_keys_with_count
        ) or (
            row.get("version") != DEVELOPMENT_BASELINE_THREAD_VERSION
            or not isinstance(row.get("thread_scope_id"), str)
            or not str(row["thread_scope_id"]).startswith("gtdb_t_")
        ):
            raise GmailTemporalHoldoutError("development baseline is malformed")
        scope_ids.append(str(row["thread_scope_id"]))
    active_scope_count = manifest.get("active_thread_scope_count")
    deleted_scope_count = manifest.get("deleted_thread_scope_count")
    if (
        manifest.get("version") != DEVELOPMENT_BASELINE_MANIFEST_VERSION
        or not isinstance(manifest_hmac, str)
        or not hmac.compare_digest(manifest_hmac, expected_manifest_hmac)
        or manifest.get("thread_scope_namespace")
        != DEVELOPMENT_BASELINE_THREAD_NAMESPACE
        or manifest.get("id_namespace") != expected_id_namespace
        or not isinstance(artifact, dict)
        or artifact.get("name") != DEVELOPMENT_BASELINE_THREAD_ARTIFACT
        or artifact.get("row_count") != len(rows)
        or artifact.get("sha256") != scopes_sha256
        or manifest.get("thread_scope_count") != len(rows)
        or not isinstance(active_scope_count, int)
        or isinstance(active_scope_count, bool)
        or not isinstance(deleted_scope_count, int)
        or isinstance(deleted_scope_count, bool)
        or active_scope_count + deleted_scope_count != len(rows)
        or manifest.get("artifact_set_sha256") != artifact_set_sha256
        or len(scope_ids) != len(set(scope_ids))
        or not isinstance(manifest.get("corpus_fingerprint"), str)
    ):
        raise GmailTemporalHoldoutError(
            "development baseline integrity verification failed"
        )
    return _DevelopmentBaseline(
        thread_scope_ids=frozenset(scope_ids),
        corpus_fingerprint=str(manifest["corpus_fingerprint"]),
        artifact_set_sha256=artifact_set_sha256,
        manifest_sha256=_sha256_bytes(manifest_bytes),
    )


def _opaque_id(key: bytes, prefix: str, *values: str) -> str:
    material = b"\0".join(
        item.encode("utf-8") for item in (SELECTION_POLICY_VERSION, prefix, *values)
    )
    return f"{prefix}_" + hmac.new(key, material, hashlib.sha256).hexdigest()


def _rank(key: bytes, candidate: Sequence[str]) -> str:
    return _opaque_id(key, "gthr", *candidate)


def _discover_targets(paths: BrainPaths) -> _Discovery:
    with connection(paths.sqlite_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM documents
            WHERE source_type = 'gmail_thread' AND status = 'active'
            ORDER BY id
            """
        ).fetchall()
    output: list[_Target] = []
    snapshot_rows: list[dict[str, Any]] = []
    seen_message_scopes: set[tuple[str, str, str]] = set()
    for row in rows:
        document = dict(row)
        frontmatter, source_path = source_frontmatter_with_path(document)
        policies = trusted_gmail_message_policies(
            document,
            frontmatter,
            source_path,
        )
        timestamps = frontmatter.get("gmail_message_timestamps")
        message_ids = frontmatter.get("gmail_message_ids")
        document_id = str(document.get("id") or "")
        account_key = str(frontmatter.get("gmail_account_key") or "")
        thread_key = str(frontmatter.get("gmail_thread_id") or "")
        source_revision = str(frontmatter.get("gmail_source_revision") or "")
        content_hash = str(document.get("content_hash") or "")
        retained_count = strict_int(frontmatter.get("retained_message_count"))
        omitted_count = strict_int(frontmatter.get("omitted_message_count"))
        truncated_count = strict_int(frontmatter.get("truncated_message_count"))
        if (
            not document_id
            or not account_key
            or not thread_key
            or len(source_revision) != 64
            or any(character not in "0123456789abcdef" for character in source_revision)
            or len(content_hash) != 64
            or any(character not in "0123456789abcdef" for character in content_hash)
            or policies is None
            or not isinstance(message_ids, list)
            or not isinstance(timestamps, list)
            or retained_count != len(message_ids)
            or omitted_count is None
            or omitted_count < 0
            or truncated_count is None
            or truncated_count < 0
            or truncated_count > retained_count
        ):
            raise GmailTemporalHoldoutError(
                "active Gmail target discovery is incomplete"
            )
        snapshot_rows.append(
            {
                "document_id": document_id,
                "content_hash": content_hash,
                "account_key": account_key,
                "thread_key": thread_key,
                "source_revision": source_revision,
                "message_ids": message_ids,
                "message_timestamps": timestamps,
                "omitted_message_count": omitted_count,
                "truncated_message_count": truncated_count,
            }
        )
        for position, message_id in enumerate(message_ids):
            scope = (account_key, thread_key, message_id)
            if scope in seen_message_scopes:
                raise GmailTemporalHoldoutError(
                    "active Gmail target discovery contains duplicate message authority"
                )
            seen_message_scopes.add(scope)
            output.append(
                _Target(
                    document_id=document_id,
                    message_id=message_id,
                    account_key=account_key,
                    thread_key=thread_key,
                    source_revision=source_revision,
                    document_content_hash=content_hash,
                    message_position=position,
                    retained_message_count=retained_count,
                    omitted_message_count=omitted_count,
                    truncated_message_count=truncated_count,
                    message_ids=tuple(message_ids),
                )
            )
    fingerprint = "gthp_" + hashlib.sha256(_canonical_json(snapshot_rows)).hexdigest()
    return _Discovery(
        targets=tuple(output),
        active_document_count=len(rows),
        corpus_fingerprint=fingerprint,
    )


def _analysis_for_source(
    source: Any, message_id: str
) -> tuple[str, TemporalLeadAnalysis]:
    inventory = analyze_gmail_temporal_leads(
        text=source.text,
        message_internal_at=source.locator.message_internal_at,
        fact_admitted=False,
        temporal_review_rescue=False,
        chunk_id=source.message_scope_key,
    )
    admission = _admission_basis(source, message_id, inventory)
    if admission == "fact":
        analysis = analyze_gmail_temporal_leads(
            text=source.text,
            message_internal_at=source.locator.message_internal_at,
            fact_admitted=True,
            temporal_review_rescue=False,
            chunk_id=source.message_scope_key,
        )
    elif admission == "temporal_rescue":
        analysis = analyze_gmail_temporal_leads(
            text=source.text,
            message_internal_at=source.locator.message_internal_at,
            fact_admitted=False,
            temporal_review_rescue=True,
            chunk_id=source.message_scope_key,
        )
    else:
        analysis = inventory
    return admission, analysis


def _strata(
    *,
    admission_basis: str,
    disposition: str,
    error_bucket: str | None,
    candidate_count: int,
    page_count: int,
    expression_count: int,
    policy: Mapping[str, Any],
    analysis: TemporalLeadAnalysis,
) -> frozenset[str]:
    output: set[str] = set()
    advertising_bases = set(policy.get("advertising_bases") or ())
    delivery = str(policy.get("delivery_kind") or "unknown")
    relevance = (
        policy.get("provider_important") is True
        or policy.get("provider_starred") is True
        or policy.get("human_signal_basis") != "none"
        or policy.get("operator_message_after") is True
    )
    if error_bucket is not None:
        output.add("preparation_failure")
    if candidate_count:
        output.add("candidate_bearing")
        output.add(f"{admission_basis}_candidate")
    if admission_basis in {"fact", "temporal_rescue"} and not candidate_count:
        output.add("admitted_zero_candidate")
    if "content_pattern" in advertising_bases and candidate_count:
        output.add("weak_advertising_candidate")
    if delivery == "bulk" and candidate_count:
        output.add("bulk_candidate")
    if any(item.mention_type == "lifecycle" for item in analysis.mentions):
        output.add("lifecycle_source")
    for role in {
        item.lifecycle_role
        for item in analysis.mentions
        if item.mention_type == "lifecycle" and item.lifecycle_role is not None
    }:
        output.add(f"lifecycle_role_{role}")
    if candidate_count and "lifecycle_source" in output:
        output.add("lifecycle_candidate")
    for form in {item.form for item in analysis.expressions}:
        output.add(f"expression_form_{form}")
    if any(item.resolution_status != "resolved" for item in analysis.expressions):
        output.add("ambiguous_or_unresolved_expression_source")
    if expression_count and not candidate_count:
        output.add("temporal_form_without_candidate")
    if candidate_count and page_count >= 12:
        output.add("long_tail_candidate")
    if candidate_count and page_count >= 22:
        output.add("extreme_long_tail_candidate")
    if candidate_count and expression_count >= 2:
        output.add("multi_expression_candidate")
    if candidate_count and any(
        "relative" in item.form or "weekday" in item.form
        for item in analysis.expressions
    ):
        output.add("relative_candidate")
    if admission_basis == "not_admitted" and (
        bool(advertising_bases) or (delivery == "bulk" and not relevance)
    ):
        output.add("hard_negative")
    output.add(f"delivery_{delivery}")
    output.add(f"disposition_{disposition}")
    return frozenset(output)


def _candidate_fingerprint(value: Mapping[str, Any]) -> str:
    return "gthc_" + hashlib.sha256(_canonical_json(value)).hexdigest()


def _summarize_target(
    paths: BrainPaths,
    target: _Target,
    *,
    key: bytes,
) -> _Candidate:
    error_bucket: str | None = None
    target_fingerprint: str | None = None
    try:
        authority = _build_authority(  # noqa: SLF001
            paths,
            document_id=target.document_id,
            gmail_message_id=target.message_id,
        )
        source = authority.source
        admission = authority.admission_basis
        analysis = authority.analysis
        disposition = authority.disposition
        candidate_count = sum(
            len(item.frontier_candidates) for item in authority.batches
        )
        page_count = sum(len(item.page_plan.pages) for item in authority.batches)
        target_fingerprint = authority.target_fingerprint
    except GmailTemporalRunnerError as exc:
        error_bucket = _ERROR_BUCKETS.get(str(exc))
        if error_bucket is None:
            raise GmailTemporalHoldoutError(
                "unrecognized Gmail temporal preparation failure"
            ) from exc
        source = _load_trusted_message(  # noqa: SLF001
            paths,
            document_id=target.document_id,
            gmail_message_id=target.message_id,
        )
        admission, analysis = _analysis_for_source(source, target.message_id)
        disposition = "preparation_failed"
        candidate_count = 0
        page_count = 0
    if (
        source.locator.gmail_account_key != target.account_key
        or source.locator.gmail_thread_id != target.thread_key
        or source.locator.gmail_source_revision != target.source_revision
        or source.locator.document_content_hash != target.document_content_hash
    ):
        raise GmailTemporalHoldoutError(
            "Gmail target authority changed during holdout scan"
        )
    policy = dict(source.message_policy)
    expression_count = len(analysis.expressions)
    context_before_count = target.omitted_message_count + target.message_position
    context_after_count = target.retained_message_count - target.message_position - 1
    target_body_truncation_status = (
        "unknown_thread_has_truncation"
        if target.truncated_message_count
        else "not_indicated"
    )
    value = {
        "document_id": target.document_id,
        "message_id": target.message_id,
        "account_key": source.locator.gmail_account_key,
        "thread_key": source.locator.gmail_thread_id,
        "source_revision": source.locator.gmail_source_revision,
        "source_sha256": source.locator.source_sha256,
        "message_internal_at": source.locator.message_internal_at,
        "context_before_count": context_before_count,
        "context_after_count": context_after_count,
        "omitted_message_count": target.omitted_message_count,
        "thread_truncated_message_count": target.truncated_message_count,
        "target_body_truncation_status": target_body_truncation_status,
        "admission_basis": admission,
        "disposition": disposition,
        "error_bucket": error_bucket,
        "expression_count": expression_count,
        "mention_count": len(analysis.mentions),
        "candidate_count": candidate_count,
        "page_count": page_count,
        "policy": policy,
        "expression_forms": sorted({item.form for item in analysis.expressions}),
        "lifecycle_roles": sorted(
            {
                str(item.lifecycle_role)
                for item in analysis.mentions
                if item.lifecycle_role is not None
            }
        ),
        "analysis_fingerprint": analysis.snapshot_fingerprint,
        "target_fingerprint": target_fingerprint,
    }
    strata = _strata(
        admission_basis=admission,
        disposition=disposition,
        error_bucket=error_bucket,
        candidate_count=candidate_count,
        page_count=page_count,
        expression_count=expression_count,
        policy=policy,
        analysis=analysis,
    )
    rank = _rank(
        key,
        (
            source.locator.gmail_account_key,
            source.locator.gmail_thread_id,
            source.locator.gmail_source_revision,
            target.message_id,
        ),
    )
    return _Candidate(
        document_id=target.document_id,
        message_id=target.message_id,
        account_key=source.locator.gmail_account_key,
        thread_key=source.locator.gmail_thread_id,
        source_revision=source.locator.gmail_source_revision,
        source_sha256=source.locator.source_sha256,
        message_internal_at=source.locator.message_internal_at,
        context_before_count=context_before_count,
        context_after_count=context_after_count,
        omitted_message_count=target.omitted_message_count,
        thread_truncated_message_count=target.truncated_message_count,
        target_body_truncation_status=target_body_truncation_status,
        message_ids=target.message_ids,
        admission_basis=admission,
        disposition=disposition,
        error_bucket=error_bucket,
        expression_count=expression_count,
        mention_count=len(analysis.mentions),
        candidate_count=candidate_count,
        page_count=page_count,
        policy=policy,
        expression_forms=tuple(value["expression_forms"]),
        lifecycle_roles=tuple(value["lifecycle_roles"]),
        strata=strata,
        rank=rank,
        fingerprint=_candidate_fingerprint(value),
    )


def _target_key(item: _Candidate) -> tuple[str, str, str, str]:
    return (
        item.account_key,
        item.thread_key,
        item.source_revision,
        item.message_id,
    )


def _thread_key(item: _Candidate) -> tuple[str, str]:
    return item.account_key, item.thread_key


def _blind_key(item: _Candidate) -> tuple[str, str, str, str, str]:
    return item.rank, *_target_key(item)


def _canonical_secondary_weight(item: _Candidate) -> int:
    """Return an exact, bounded, order-independent canonical MILP weight."""

    digest = hashlib.sha256(
        b"gmail_temporal_holdout_challenge_secondary_v1\0"
        + _canonical_json(list(_blind_key(item)))
    ).digest()
    # At most 100 selected 40-bit weights sum below 2**47, so scipy/HiGHS'
    # binary64 objective retains exact integer arithmetic for the tie-break.
    return (int.from_bytes(digest[:5], "big") & ((1 << 40) - 1)) + 1


def _timestamp(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GmailTemporalHoldoutError("Gmail target timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise GmailTemporalHoldoutError("Gmail target timestamp is invalid")
    return parsed.timestamp()


def _validated_quotas(
    quotas: Sequence[tuple[str, int]],
    *,
    challenge_size: int,
) -> tuple[tuple[str, int], ...]:
    output: list[tuple[str, int]] = []
    seen: set[str] = set()
    for stratum, minimum in quotas:
        if (
            not isinstance(stratum, str)
            or not stratum
            or stratum in seen
            or isinstance(minimum, bool)
            or not isinstance(minimum, int)
            or minimum < 0
            or minimum > challenge_size
        ):
            raise GmailTemporalHoldoutError("holdout challenge quota is invalid")
        seen.add(stratum)
        output.append((stratum, minimum))
    return tuple(sorted(output))


def _exact_challenge_selection(
    candidates: Sequence[_Candidate],
    *,
    challenge_size: int,
    quotas: Sequence[tuple[str, int]],
) -> tuple[_Candidate, ...]:
    validated_quotas = _validated_quotas(
        quotas,
        challenge_size=challenge_size,
    )
    if challenge_size == 0:
        if any(minimum for _stratum, minimum in validated_quotas):
            raise GmailTemporalHoldoutError("holdout challenge is infeasible")
        return ()
    ordered = tuple(
        sorted(
            (
                item
                for item in candidates
                if item.page_count <= MAX_CHALLENGE_MESSAGE_PAGES
            ),
            key=_blind_key,
        )
    )
    if len({_thread_key(item) for item in ordered}) < challenge_size:
        raise GmailTemporalHoldoutError(
            "not enough independent Gmail threads for challenge cohort"
        )
    try:
        import numpy as np
        from scipy.optimize import Bounds, LinearConstraint, milp
        from scipy.sparse import coo_array
    except ImportError as exc:
        raise GmailTemporalHoldoutError(
            "exact holdout challenge solver is unavailable"
        ) from exc

    row_indexes: list[int] = []
    column_indexes: list[int] = []
    coefficients: list[float] = []
    lower_bounds: list[float] = []
    upper_bounds: list[float] = []

    def add_constraint(
        columns: Sequence[int],
        *,
        lower: float,
        upper: float,
        weights: Sequence[float] | None = None,
    ) -> None:
        if weights is not None and len(weights) != len(columns):
            raise GmailTemporalHoldoutError(
                "exact holdout challenge constraint is malformed"
            )
        row = len(lower_bounds)
        row_indexes.extend([row] * len(columns))
        column_indexes.extend(columns)
        coefficients.extend([1.0] * len(columns) if weights is None else list(weights))
        lower_bounds.append(lower)
        upper_bounds.append(upper)

    add_constraint(
        tuple(range(len(ordered))),
        lower=float(challenge_size),
        upper=float(challenge_size),
    )
    for stratum, minimum in validated_quotas:
        columns = tuple(
            index for index, item in enumerate(ordered) if stratum in item.strata
        )
        if len(columns) < minimum:
            raise GmailTemporalHoldoutError("holdout challenge quota is unavailable")
        add_constraint(columns, lower=float(minimum), upper=np.inf)
    all_columns = tuple(range(len(ordered)))
    add_constraint(
        all_columns,
        lower=-np.inf,
        upper=float(MAX_CHALLENGE_TOTAL_PAGES),
        weights=tuple(float(item.page_count) for item in ordered),
    )
    add_constraint(
        all_columns,
        lower=-np.inf,
        upper=float(MAX_CHALLENGE_TOTAL_CANDIDATES),
        weights=tuple(float(item.candidate_count) for item in ordered),
    )
    thread_columns: dict[tuple[str, str], list[int]] = {}
    for index, item in enumerate(ordered):
        thread_columns.setdefault(_thread_key(item), []).append(index)
    for thread in sorted(thread_columns):
        add_constraint(
            thread_columns[thread],
            lower=-np.inf,
            upper=1.0,
        )
    def constraints() -> Any:
        matrix = coo_array(
            (coefficients, (row_indexes, column_indexes)),
            shape=(len(lower_bounds), len(ordered)),
        ).tocsc()
        return LinearConstraint(
            matrix,
            np.array(lower_bounds, dtype=float),
            np.array(upper_bounds, dtype=float),
        )

    def solve(objective: Any) -> Any:
        return milp(
            objective,
            integrality=np.ones(len(ordered), dtype=np.uint8),
            bounds=Bounds(0.0, 1.0),
            constraints=constraints(),
            options={"mip_rel_gap": 0.0, "presolve": True, "time_limit": 30.0},
        )

    # The challenge cohort is a conditional capability stress gate, never a
    # prevalence estimate.  Among quota- and budget-satisfying witnesses, minimize
    # verifier pages and then candidate volume so rare long-tail coverage does
    # not turn a personal benchmark into an unbounded external-call bill.
    primary_costs = tuple(
        10 * item.page_count + item.candidate_count for item in ordered
    )
    primary_objective = np.array(
        primary_costs,
        dtype=float,
    )
    result = solve(primary_objective)
    if result.status == 2:
        raise GmailTemporalHoldoutError(
            "exact holdout challenge constraints are infeasible"
        )
    if not result.success or result.status != 0 or result.x is None:
        raise GmailTemporalHoldoutError(
            "exact holdout challenge selection was not proven optimal"
        )
    primary_optimum = int(round(float(result.fun)))
    if abs(float(result.fun) - primary_optimum) > 1e-7:
        raise GmailTemporalHoldoutError(
            "exact holdout challenge selection failed verification"
        )

    # A second, explicitly constrained solve chooses a canonical witness among
    # all primary-cost optima.  Hash-derived integer weights do not depend on
    # solver variable order.  The final no-good solve proves that the secondary
    # optimum is unique; a collision fails closed instead of returning whichever
    # equal optimum HiGHS happened to enumerate first.
    add_constraint(
        all_columns,
        lower=float(primary_optimum),
        upper=float(primary_optimum),
        weights=tuple(float(value) for value in primary_costs),
    )
    secondary_weights = tuple(_canonical_secondary_weight(item) for item in ordered)
    secondary_objective = np.array(
        secondary_weights,
        dtype=float,
    )
    result = solve(secondary_objective)
    if not result.success or result.status != 0 or result.x is None:
        raise GmailTemporalHoldoutError(
            "exact holdout challenge canonical tie-break was not proven"
        )
    if any(abs(float(value) - round(float(value))) > 1e-7 for value in result.x):
        raise GmailTemporalHoldoutError(
            "exact holdout challenge selection was non-integral"
        )
    selected_indexes = tuple(
        index for index, value in enumerate(result.x) if round(float(value)) == 1
    )
    secondary_optimum = sum(secondary_weights[index] for index in selected_indexes)
    if abs(float(result.fun) - secondary_optimum) > 1e-7:
        raise GmailTemporalHoldoutError(
            "exact holdout challenge selection failed verification"
        )
    add_constraint(
        all_columns,
        lower=float(secondary_optimum),
        upper=float(secondary_optimum),
        weights=tuple(float(value) for value in secondary_weights),
    )
    add_constraint(
        selected_indexes,
        lower=-np.inf,
        upper=float(challenge_size - 1),
    )
    uniqueness = solve(
        [10 * item.page_count + item.candidate_count for item in ordered],
    )
    if uniqueness.status != 2:
        if uniqueness.success and uniqueness.status == 0 and uniqueness.x is not None:
            raise GmailTemporalHoldoutError(
                "exact holdout challenge canonical tie-break was not unique"
            )
        raise GmailTemporalHoldoutError(
            "exact holdout challenge canonical tie-break was not proven"
        )
    selected = tuple(ordered[index] for index in selected_indexes)
    if (
        len(selected) != challenge_size
        or len({_thread_key(item) for item in selected}) != challenge_size
        or sum(item.page_count for item in selected) > MAX_CHALLENGE_TOTAL_PAGES
        or sum(item.candidate_count for item in selected)
        > MAX_CHALLENGE_TOTAL_CANDIDATES
        or any(item.page_count > MAX_CHALLENGE_MESSAGE_PAGES for item in selected)
        or any(
            sum(stratum in item.strata for item in selected) < minimum
            for stratum, minimum in validated_quotas
        )
    ):
        raise GmailTemporalHoldoutError(
            "exact holdout challenge selection failed verification"
        )
    return tuple(sorted(selected, key=_blind_key))


def _select_cohort(
    candidates: Sequence[_Candidate],
    *,
    challenge_candidates: Sequence[_Candidate] | None = None,
    sample_size: int,
    challenge_size: int,
    reserve_size: int,
    fresh_after: str,
    quotas: Sequence[tuple[str, int]],
) -> _Selection:
    if (
        isinstance(sample_size, bool)
        or not isinstance(sample_size, int)
        or sample_size < 1
    ):
        raise GmailTemporalHoldoutError("sample size must be a positive integer")
    if (
        isinstance(challenge_size, bool)
        or not isinstance(challenge_size, int)
        or challenge_size < 0
    ):
        raise GmailTemporalHoldoutError("challenge sample size is invalid")
    if (
        isinstance(reserve_size, bool)
        or not isinstance(reserve_size, int)
        or reserve_size < 0
    ):
        raise GmailTemporalHoldoutError("reserve sample size is invalid")
    fresh_after_timestamp = _timestamp(fresh_after)
    target_keys = [_target_key(item) for item in candidates]
    if len(target_keys) != len(set(target_keys)):
        raise GmailTemporalHoldoutError(
            "Gmail holdout candidates contain duplicate targets"
        )
    unique_threads = {_thread_key(item) for item in candidates}
    if len(unique_threads) < sample_size + reserve_size:
        raise GmailTemporalHoldoutError(
            "not enough independent Gmail threads for primary and reserve"
        )
    challenge_pool = tuple(
        candidates if challenge_candidates is None else challenge_candidates
    )
    challenge_target_keys = [_target_key(item) for item in challenge_pool]
    if len(challenge_target_keys) != len(set(challenge_target_keys)):
        raise GmailTemporalHoldoutError(
            "Gmail challenge candidates contain duplicate targets"
        )
    fresh_candidates = sorted(
        (
            item
            for item in candidates
            if _timestamp(item.message_internal_at) >= fresh_after_timestamp
        ),
        key=_blind_key,
    )
    fresh_threads = {_thread_key(item) for item in fresh_candidates}
    if len(fresh_threads) < sample_size + reserve_size:
        raise GmailTemporalHoldoutError(
            "fresh Gmail tail cannot fill primary and reserve cohorts"
        )
    blind_recent: list[_Candidate] = []
    used_threads: set[tuple[str, str]] = set()
    for item in fresh_candidates:
        if len(blind_recent) >= sample_size + reserve_size:
            break
        thread = _thread_key(item)
        if thread in used_threads:
            continue
        blind_recent.append(item)
        used_threads.add(thread)
    if len(blind_recent) != sample_size + reserve_size:
        raise GmailTemporalHoldoutError(
            "recent Gmail holdout sample could not be filled"
        )
    primary = tuple(blind_recent[:sample_size])
    reserve = tuple(blind_recent[sample_size:])
    challenge = _exact_challenge_selection(
        tuple(
            item
            for item in challenge_pool
            if _thread_key(item) not in used_threads
            and _timestamp(item.message_internal_at) < fresh_after_timestamp
        ),
        challenge_size=challenge_size,
        quotas=quotas,
    )
    all_selected = (*primary, *challenge, *reserve)
    if len({_thread_key(item) for item in all_selected}) != len(all_selected):
        raise GmailTemporalHoldoutError("Gmail holdout splits are not thread-disjoint")
    return _Selection(
        primary=tuple(sorted(primary, key=_blind_key)),
        challenge=challenge,
        reserve=tuple(sorted(reserve, key=_blind_key)),
        fresh_after=fresh_after,
        fresh_thread_count=len(fresh_threads),
    )


def _context_for_candidate(
    paths: BrainPaths,
    candidate: _Candidate,
) -> tuple[_ContextSource, ...]:
    positions = [
        index
        for index, message_id in enumerate(candidate.message_ids)
        if message_id == candidate.message_id
    ]
    if len(positions) != 1:
        raise GmailTemporalHoldoutError(
            "selected Gmail target lacks exact thread position"
        )
    position = positions[0]
    neighbor_positions = (
        *range(max(0, position - CONTEXT_MESSAGES_PER_SIDE), position),
        *range(
            position + 1,
            min(len(candidate.message_ids), position + CONTEXT_MESSAGES_PER_SIDE + 1),
        ),
    )
    output: list[_ContextSource] = []
    for neighbor_position in neighbor_positions:
        message_id = candidate.message_ids[neighbor_position]
        source = _load_trusted_message(  # noqa: SLF001
            paths,
            document_id=candidate.document_id,
            gmail_message_id=message_id,
        )
        if (
            source.locator.gmail_account_key != candidate.account_key
            or source.locator.gmail_thread_id != candidate.thread_key
            or source.locator.gmail_source_revision != candidate.source_revision
        ):
            raise GmailTemporalHoldoutError(
                "selected Gmail context authority changed during freeze"
            )
        output.append(
            _ContextSource(
                relative_position=neighbor_position - position,
                message_id=message_id,
                message_internal_at=source.locator.message_internal_at,
                text=source.text,
            )
        )
    return tuple(output)


def _materialize(paths: BrainPaths, candidate: _Candidate) -> _Materialized:
    try:
        authority = _build_authority(  # noqa: SLF001
            paths,
            document_id=candidate.document_id,
            gmail_message_id=candidate.message_id,
        )
        source = authority.source
        analysis = authority.analysis
        batch_plan_fingerprint = authority.batch_plan.plan_fingerprint
        requests = authority.requests
        target_fingerprint: str | None = authority.target_fingerprint
    except GmailTemporalRunnerError as exc:
        bucket = _ERROR_BUCKETS.get(str(exc))
        if bucket is None:
            raise GmailTemporalHoldoutError(
                "unrecognized Gmail temporal preparation failure"
            ) from exc
        if bucket != candidate.error_bucket:
            raise GmailTemporalHoldoutError(
                "selected Gmail authority changed during holdout freeze"
            ) from exc
        source = _load_trusted_message(  # noqa: SLF001
            paths,
            document_id=candidate.document_id,
            gmail_message_id=candidate.message_id,
        )
        admission, analysis = _analysis_for_source(source, candidate.message_id)
        if admission != candidate.admission_basis:
            raise GmailTemporalHoldoutError(
                "selected Gmail admission changed during holdout freeze"
            )
        batch_plan = plan_gmail_temporal_selector_batches(
            text=source.text,
            analysis=analysis,
        )
        batch_plan_fingerprint = batch_plan.plan_fingerprint
        requests = ()
        target_fingerprint = None
    if (
        source.locator.source_sha256 != candidate.source_sha256
        or source.locator.gmail_source_revision != candidate.source_revision
        or analysis.snapshot_fingerprint == ""
    ):
        raise GmailTemporalHoldoutError(
            "selected Gmail source changed during holdout freeze"
        )
    return _Materialized(
        candidate=candidate,
        text=source.text,
        analysis=analysis,
        requests=tuple(requests),
        analysis_fingerprint=analysis.snapshot_fingerprint,
        batch_plan_fingerprint=batch_plan_fingerprint,
        target_fingerprint=target_fingerprint,
        context=_context_for_candidate(paths, candidate),
    )


def _without_chunk(value: Any) -> dict[str, Any]:
    output = asdict(value)
    output.pop("chunk_id", None)
    return output


def _policy_without_identity(policy: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(policy)
    output.pop("message_id", None)
    return output


def _sample_row(
    value: _Materialized,
    *,
    key: bytes,
    selection_partition: str,
) -> dict[str, Any]:
    candidate = value.candidate
    sample_id = _opaque_id(
        key,
        "gths",
        candidate.account_key,
        candidate.thread_key,
        candidate.source_revision,
        candidate.message_id,
    )
    thread_id = _opaque_id(
        key,
        "gtht",
        candidate.account_key,
        candidate.thread_key,
    )
    sanitized = sanitize_gmail_model_payload({"text": value.text})
    text = sanitized.get("text") if isinstance(sanitized, Mapping) else None
    if not isinstance(text, str) or len(text) != len(value.text):
        raise GmailTemporalHoldoutError(
            "Gmail holdout sanitization did not preserve source offsets"
        )
    return {
        "version": SAMPLE_VERSION,
        "sample_id": sample_id,
        "thread_id": thread_id,
        "selection_partition": selection_partition,
        "stratum": (
            "important_fact"
            if candidate.admission_basis == "fact"
            else "suppressed_temporal_rescue"
            if candidate.admission_basis == "temporal_rescue"
            else "noise_not_admitted"
        ),
        "message_internal_at": candidate.message_internal_at,
        "source_prior_message_count": candidate.context_before_count,
        "source_later_message_count": candidate.context_after_count,
        "source_omitted_before_count": candidate.omitted_message_count,
        "thread_truncated_message_count": candidate.thread_truncated_message_count,
        "target_body_truncation_status": candidate.target_body_truncation_status,
        "text": text,
        "sanitized_text_sha256": _sha256_bytes(text.encode("utf-8")),
        "source_sha256": candidate.source_sha256,
        "analysis_fingerprint": value.analysis_fingerprint,
        "batch_plan_fingerprint": value.batch_plan_fingerprint,
        "preparation": {
            "admission_basis": candidate.admission_basis,
            "disposition": candidate.disposition,
            "error_bucket": candidate.error_bucket,
            "expression_count": candidate.expression_count,
            "mention_count": candidate.mention_count,
            "candidate_count": candidate.candidate_count,
            "page_count": candidate.page_count,
            "request_fingerprints": [
                item.request_fingerprint for item in value.requests
            ],
        },
        "policy": _policy_without_identity(candidate.policy),
        "selection_strata": sorted(candidate.strata),
        "expressions": [_without_chunk(item) for item in value.analysis.expressions],
        "mentions": [_without_chunk(item) for item in value.analysis.mentions],
        "leads": [_without_chunk(item) for item in value.analysis.leads],
        "routable": False,
    }


def _binding_row(value: _Materialized, *, key: bytes) -> dict[str, Any]:
    candidate = value.candidate
    return {
        "version": BINDING_VERSION,
        "sample_id": _opaque_id(
            key,
            "gths",
            candidate.account_key,
            candidate.thread_key,
            candidate.source_revision,
            candidate.message_id,
        ),
        "document_id": candidate.document_id,
        "gmail_message_id": candidate.message_id,
        "gmail_account_key": candidate.account_key,
        "gmail_thread_id": candidate.thread_key,
        "gmail_source_revision": candidate.source_revision,
        "source_sha256": candidate.source_sha256,
        "candidate_fingerprint": candidate.fingerprint,
        "analysis_fingerprint": value.analysis_fingerprint,
        "batch_plan_fingerprint": value.batch_plan_fingerprint,
        "target_fingerprint": value.target_fingerprint,
        "routable": False,
    }


def _source_label_queue_row(
    sample: Mapping[str, Any],
    value: _Materialized,
    *,
    key: bytes,
) -> dict[str, Any]:
    """Return the only artifact a blind human labeler should inspect."""

    candidate = value.candidate
    context_rows: list[dict[str, Any]] = []
    for context in value.context:
        sanitized = sanitize_gmail_model_payload({"text": context.text})
        text = sanitized.get("text") if isinstance(sanitized, Mapping) else None
        if not isinstance(text, str):
            raise GmailTemporalHoldoutError("Gmail holdout context sanitization failed")
        emitted = text[:CONTEXT_MESSAGE_CHAR_CAP]
        context_rows.append(
            {
                "message_id": _opaque_id(
                    key,
                    "gthm",
                    candidate.account_key,
                    candidate.thread_key,
                    candidate.source_revision,
                    context.message_id,
                ),
                "relative_position": context.relative_position,
                "message_internal_at": context.message_internal_at,
                "text": emitted,
                "source_char_count": len(text),
                "emitted_char_count": len(emitted),
                "text_truncated_after": len(emitted) < len(text),
            }
        )
    prior_included = sum(item["relative_position"] < 0 for item in context_rows)
    later_included = sum(item["relative_position"] > 0 for item in context_rows)
    return {
        "version": LABEL_QUEUE_VERSION,
        "sample_id": sample["sample_id"],
        "thread_id": sample["thread_id"],
        "target": {
            "message_internal_at": sample["message_internal_at"],
            "text": sample["text"],
            "source_char_count": len(value.text),
            "emitted_char_count": len(sample["text"]),
            "sanitized_text_sha256": sample["sanitized_text_sha256"],
            "body_truncation_status": sample["target_body_truncation_status"],
        },
        "thread_context": {
            "prior_available": sample["source_prior_message_count"],
            "prior_included": prior_included,
            "prior_omitted": sample["source_prior_message_count"] - prior_included,
            "later_available": sample["source_later_message_count"],
            "later_included": later_included,
            "later_omitted": sample["source_later_message_count"] - later_included,
            "source_omitted_before_count": sample["source_omitted_before_count"],
            "source_truncated_message_count": sample["thread_truncated_message_count"],
            "messages": sorted(
                context_rows,
                key=lambda item: item["relative_position"],
            ),
        },
        "context_is_label_only": True,
        "label_status": "unlabeled",
        "expected_material": None,
        "expected_filter": None,
        "hard_negative": None,
        "semantic_units": [],
        "critical_error": None,
        "notes": None,
    }


def _reserve_order_row(
    candidate: _Candidate,
    *,
    key: bytes,
    position: int,
) -> dict[str, Any]:
    sample_id = _opaque_id(
        key,
        "gths",
        candidate.account_key,
        candidate.thread_key,
        candidate.source_revision,
        candidate.message_id,
    )
    return {
        "version": RESERVE_ORDER_VERSION,
        "position": position,
        "sample_id": sample_id,
        "thread_id": _opaque_id(
            key,
            "gtht",
            candidate.account_key,
            candidate.thread_key,
        ),
        "source_commitment": _opaque_id(
            key,
            "gthc",
            candidate.account_key,
            candidate.thread_key,
            candidate.source_revision,
            candidate.message_id,
            candidate.source_sha256,
        ),
    }


def _reserve_binding_row(candidate: _Candidate, *, key: bytes) -> dict[str, Any]:
    return {
        "version": BINDING_VERSION,
        "sample_id": _opaque_id(
            key,
            "gths",
            candidate.account_key,
            candidate.thread_key,
            candidate.source_revision,
            candidate.message_id,
        ),
        "document_id": candidate.document_id,
        "gmail_message_id": candidate.message_id,
        "gmail_account_key": candidate.account_key,
        "gmail_thread_id": candidate.thread_key,
        "gmail_source_revision": candidate.source_revision,
        "source_sha256": candidate.source_sha256,
        "candidate_fingerprint": candidate.fingerprint,
        "selection_status": "sealed_reserve",
        "routable": False,
    }


def _request_rows(value: _Materialized, *, key: bytes) -> list[dict[str, Any]]:
    candidate = value.candidate
    sample_id = _opaque_id(
        key,
        "gths",
        candidate.account_key,
        candidate.thread_key,
        candidate.source_revision,
        candidate.message_id,
    )
    return [
        {
            "version": REQUEST_VERSION,
            "sample_id": sample_id,
            "request_fingerprint": item.request_fingerprint,
            "batch_fingerprint": item.batch_fingerprint,
            "frontier_fingerprint": item.frontier_fingerprint,
            "page_plan_fingerprint": item.page_plan_fingerprint,
            "page_fingerprint": item.page_fingerprint,
            "candidate_count": item.candidate_count,
            "payload": json.loads(item.payload),
            "routable": False,
        }
        for item in value.requests
    ]


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


def _publish_frozen(output_root: Path, artifacts: Mapping[str, bytes]) -> None:
    root = Path(output_root)
    parent = root.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise GmailTemporalHoldoutError("holdout output parent is unsafe")
    parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    if root.exists() or root.is_symlink():
        raise GmailTemporalHoldoutError("holdout output already exists")
    temporary = Path(tempfile.mkdtemp(prefix=f".{root.name}-", dir=parent))
    os.chmod(temporary, PRIVATE_DIRECTORY_MODE)
    try:
        for name, payload in sorted(artifacts.items()):
            relative = Path(name)
            if (
                relative.is_absolute()
                or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise GmailTemporalHoldoutError("holdout artifact name is invalid")
            artifact = temporary / relative
            artifact.parent.mkdir(
                parents=True,
                exist_ok=True,
                mode=PRIVATE_DIRECTORY_MODE,
            )
            os.chmod(artifact.parent, PRIVATE_DIRECTORY_MODE)
            _write_private_new(artifact, payload)
        os.replace(temporary, root)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _authenticated_manifest_bytes(
    manifest: Mapping[str, Any],
    *,
    key: bytes,
) -> bytes:
    unsigned = _canonical_json(dict(manifest))
    authenticator = hmac.new(
        key,
        MANIFEST_HMAC_DOMAIN + unsigned,
        hashlib.sha256,
    ).hexdigest()
    return (
        _canonical_json({**dict(manifest), "manifest_hmac_sha256": authenticator})
        + b"\n"
    )


def _freeze_signed_bytes(
    value: Mapping[str, Any],
    *,
    key: bytes,
    domain: bytes,
) -> bytes:
    unsigned = dict(value)
    unsigned.pop("record_hmac_sha256", None)
    authenticator = hmac.new(
        key,
        domain + _canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()
    return _canonical_json(
        {**unsigned, "record_hmac_sha256": authenticator}
    ) + b"\n"


def _load_freeze_signed_record(
    path: Path,
    *,
    key: bytes,
    domain: bytes,
) -> tuple[dict[str, Any], bytes]:
    raw = _private_file_bytes(path)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GmailTemporalHoldoutError("freeze authority is malformed") from exc
    if not isinstance(value, dict) or raw != _canonical_json(value) + b"\n":
        raise GmailTemporalHoldoutError("freeze authority is malformed")
    authenticator = value.get("record_hmac_sha256")
    unsigned = dict(value)
    unsigned.pop("record_hmac_sha256", None)
    expected = hmac.new(
        key,
        domain + _canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()
    if not isinstance(authenticator, str) or not hmac.compare_digest(
        authenticator,
        expected,
    ):
        raise GmailTemporalHoldoutError("freeze authority authentication failed")
    return value, raw


def _private_freeze_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise GmailTemporalHoldoutError("freeze authority is unavailable") from exc
    if (
        path.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != PRIVATE_DIRECTORY_MODE
    ):
        raise GmailTemporalHoldoutError("freeze authority directory is unsafe")


def _load_or_create_freeze_authority(root: Path, *, key: bytes) -> tuple[dict[str, Any], bytes]:
    root = Path(root)
    parent = root.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise GmailTemporalHoldoutError("freeze authority parent is unsafe")
    parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    if not root.exists() and not root.is_symlink():
        temporary = Path(tempfile.mkdtemp(prefix=f".{root.name}-", dir=parent))
        os.chmod(temporary, PRIVATE_DIRECTORY_MODE)
        try:
            for name in ("attempts", "outcomes", "stages"):
                (temporary / name).mkdir(mode=PRIVATE_DIRECTORY_MODE)
            manifest = {
                "version": FREEZE_AUTHORITY_VERSION,
                "scope": FREEZE_AUTHORITY_SCOPE,
                "milestone_identity": "sha256_of_canonical_milestone_and_evidence_class",
                "append_only_attempts": True,
                "alternate_output_path_or_holdout_key_does_not_create_new_identity": True,
                "created_at": datetime.now(UTC).isoformat(),
                "private_file_mode": "0600",
                "private_directory_mode": "0700",
            }
            _write_private_new(
                temporary / FREEZE_AUTHORITY_MANIFEST_ARTIFACT,
                _freeze_signed_bytes(
                    manifest,
                    key=key,
                    domain=FREEZE_AUTHORITY_DOMAIN,
                ),
            )
            try:
                os.replace(temporary, root)
            except OSError:
                if not root.exists() or root.is_symlink():
                    raise
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
    _private_freeze_directory(root)
    if {item.name for item in root.iterdir()} != {
        FREEZE_AUTHORITY_MANIFEST_ARTIFACT,
        "attempts",
        "outcomes",
        "stages",
    }:
        raise GmailTemporalHoldoutError("freeze authority inventory is invalid")
    for name in ("attempts", "outcomes", "stages"):
        _private_freeze_directory(root / name)
    value, raw = _load_freeze_signed_record(
        root / FREEZE_AUTHORITY_MANIFEST_ARTIFACT,
        key=key,
        domain=FREEZE_AUTHORITY_DOMAIN,
    )
    if (
        value.get("version") != FREEZE_AUTHORITY_VERSION
        or value.get("scope") != FREEZE_AUTHORITY_SCOPE
        or value.get("append_only_attempts") is not True
        or value.get(
            "alternate_output_path_or_holdout_key_does_not_create_new_identity"
        )
        is not True
    ):
        raise GmailTemporalHoldoutError("freeze authority policy is invalid")
    return value, raw


def _freeze_attempt_id(milestone: str, evidence_class: str) -> str:
    return "gthfa_" + _sha256_bytes(
        b"gmail_temporal_holdout_freeze_identity_v1\0"
        + _canonical_json(
            {"milestone": milestone, "evidence_class": evidence_class}
        )
    )


def _register_freeze_attempt(
    authority_root: Path,
    *,
    authority_key: bytes,
    holdout_key: bytes,
    output_root: Path,
    milestone: str,
    evidence_class: str,
    configuration: Mapping[str, Any],
) -> _FreezeAttempt:
    if _MILESTONE_PATTERN.fullmatch(milestone) is None:
        raise GmailTemporalHoldoutError("freeze milestone is invalid")
    if hmac.compare_digest(authority_key, holdout_key):
        raise GmailTemporalHoldoutError(
            "freeze authority key must be independent from holdout key"
        )
    authority = Path(authority_root).resolve()
    output = Path(output_root).resolve()
    if authority == output or authority in output.parents or output in authority.parents:
        raise GmailTemporalHoldoutError(
            "freeze authority and holdout output must be disjoint"
        )
    _manifest, manifest_raw = _load_or_create_freeze_authority(
        authority,
        key=authority_key,
    )
    attempt_id = _freeze_attempt_id(milestone, evidence_class)
    attempt_path = authority / "attempts" / f"{attempt_id}.json"
    if attempt_path.exists() or attempt_path.is_symlink():
        _load_freeze_signed_record(
            attempt_path,
            key=authority_key,
            domain=FREEZE_ATTEMPT_DOMAIN,
        )
        raise GmailTemporalHoldoutError(
            "freeze milestone and evidence class were already attempted"
        )
    record = {
        "version": FREEZE_ATTEMPT_VERSION,
        "authority_manifest_sha256": _sha256_bytes(manifest_raw),
        "attempt_id": attempt_id,
        "milestone": milestone,
        "evidence_class": evidence_class,
        "registered_at": datetime.now(UTC).isoformat(),
        "registered_before_discovery_selection_and_publication": True,
        "builder_version": VERSION,
        "builder_sha256": _sha256_bytes(Path(__file__).read_bytes()),
        "holdout_output_root_sha256": _sha256_bytes(
            str(output).encode("utf-8")
        ),
        "holdout_hmac_key_sha256": _sha256_bytes(holdout_key),
        "configuration": dict(configuration),
        "configuration_sha256": _sha256_bytes(_canonical_json(configuration)),
        "routable": False,
    }
    attempt_raw = _freeze_signed_bytes(
        record,
        key=authority_key,
        domain=FREEZE_ATTEMPT_DOMAIN,
    )
    try:
        _write_private_new(attempt_path, attempt_raw)
    except FileExistsError as exc:
        raise GmailTemporalHoldoutError(
            "freeze milestone and evidence class were already attempted"
        ) from exc
    return _FreezeAttempt(
        authority_root=authority,
        authority_manifest_sha256=_sha256_bytes(manifest_raw),
        attempt_id=attempt_id,
        attempt_sha256=_sha256_bytes(attempt_raw),
        milestone=milestone,
        evidence_class=evidence_class,
    )


def _complete_freeze_attempt(
    attempt: _FreezeAttempt,
    *,
    authority_key: bytes,
    holdout_manifest_raw: bytes,
) -> None:
    outcome_path = attempt.authority_root / "outcomes" / f"{attempt.attempt_id}.json"
    record = {
        "version": FREEZE_OUTCOME_VERSION,
        "authority_manifest_sha256": attempt.authority_manifest_sha256,
        "attempt_id": attempt.attempt_id,
        "attempt_sha256": attempt.attempt_sha256,
        "milestone": attempt.milestone,
        "evidence_class": attempt.evidence_class,
        "status": "published",
        "completed_at": datetime.now(UTC).isoformat(),
        "holdout_manifest_sha256": _sha256_bytes(holdout_manifest_raw),
        "routable": False,
    }
    try:
        _write_private_new(
            outcome_path,
            _freeze_signed_bytes(
                record,
                key=authority_key,
                domain=FREEZE_OUTCOME_DOMAIN,
            ),
        )
    except FileExistsError as exc:
        raise GmailTemporalHoldoutError("freeze outcome already exists") from exc


def build_gmail_temporal_holdout(
    home: Path,
    hmac_key_path: Path,
    output_root: Path,
    *,
    fresh_after: str,
    development_baseline_root: Path | None = None,
    development_baseline_key_path: Path | None = None,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    challenge_size: int = DEFAULT_CHALLENGE_SIZE,
    reserve_size: int = DEFAULT_RESERVE_SIZE,
    quotas: Sequence[tuple[str, int]] = DEFAULT_QUOTAS,
    evidence_class: str | None = None,
    freeze_authority_root: Path | None = None,
    freeze_authority_key_path: Path | None = None,
    freeze_milestone: str | None = None,
) -> dict[str, Any]:
    """Build a frozen private cohort and return aggregate-only evidence."""

    paths = BrainPaths.from_value(home)
    key = _private_hmac_key(hmac_key_path)
    if (development_baseline_root is None) != (development_baseline_key_path is None):
        raise GmailTemporalHoldoutError(
            "development baseline root and key must be supplied together"
        )
    development_baseline: _DevelopmentBaseline | None = None
    development_baseline_key: bytes | None = None
    if development_baseline_root is not None:
        assert development_baseline_key_path is not None
        development_baseline_key = _private_hmac_key(development_baseline_key_path)
        development_baseline = _load_development_baseline(
            development_baseline_root,
            key=development_baseline_key,
        )
    resolved_evidence_class = evidence_class or (
        PROSPECTIVE_EVIDENCE_CLASS
        if development_baseline is not None
        else DIAGNOSTIC_EVIDENCE_CLASS
    )
    if resolved_evidence_class not in {
        DIAGNOSTIC_EVIDENCE_CLASS,
        *BASELINE_BACKED_EVIDENCE_CLASSES,
    } or (
        resolved_evidence_class in BASELINE_BACKED_EVIDENCE_CLASSES
        and development_baseline is None
    ):
        raise GmailTemporalHoldoutError("holdout evidence class is invalid")
    _validate_requested_cohort_sizes(
        resolved_evidence_class,
        sample_size=sample_size,
        challenge_size=challenge_size,
        reserve_size=reserve_size,
    )
    _timestamp(fresh_after)
    effective_quotas = () if challenge_size == 0 else tuple(quotas)
    validated_quotas = _validated_quotas(
        effective_quotas,
        challenge_size=challenge_size,
    )
    authority_arguments = (
        freeze_authority_root,
        freeze_authority_key_path,
        freeze_milestone,
    )
    authority_supplied = all(value is not None for value in authority_arguments)
    if any(value is not None for value in authority_arguments) and not authority_supplied:
        raise GmailTemporalHoldoutError(
            "freeze authority root, key, and milestone must be supplied together"
        )
    if resolved_evidence_class in RELEASE_EVIDENCE_CLASSES and not authority_supplied:
        raise GmailTemporalHoldoutError(
            "release holdout requires canonical freeze authority"
        )
    freeze_attempt: _FreezeAttempt | None = None
    freeze_authority_key: bytes | None = None
    if authority_supplied:
        assert freeze_authority_root is not None
        assert freeze_authority_key_path is not None
        assert freeze_milestone is not None
        freeze_authority_key = _private_hmac_key(freeze_authority_key_path)
        freeze_attempt = _register_freeze_attempt(
            freeze_authority_root,
            authority_key=freeze_authority_key,
            holdout_key=key,
            output_root=output_root,
            milestone=freeze_milestone,
            evidence_class=resolved_evidence_class,
            configuration={
                "fresh_after": fresh_after,
                "sample_size": sample_size,
                "challenge_size": challenge_size,
                "reserve_size": reserve_size,
                "quotas": [list(value) for value in validated_quotas],
                "development_baseline_manifest_sha256": (
                    development_baseline.manifest_sha256
                    if development_baseline is not None
                    else None
                ),
            },
        )
    discovery = _discover_targets(paths)
    targets = discovery.targets
    if not targets:
        raise GmailTemporalHoldoutError("no active Gmail targets are available")
    candidates: list[_Candidate] = []
    scan_errors: Counter[str] = Counter()
    for target in targets:
        try:
            candidates.append(_summarize_target(paths, target, key=key))
        except Exception:  # noqa: BLE001 - private details must not escape.
            scan_errors["unavailable_target"] += 1
    if scan_errors:
        raise GmailTemporalHoldoutError(
            "holdout scan did not cover every active Gmail target"
        )
    post_scan_discovery = _discover_targets(paths)
    if post_scan_discovery != discovery:
        raise GmailTemporalHoldoutError(
            "Gmail source corpus changed during holdout scan"
        )
    baseline_overlap_messages = 0
    baseline_overlap_threads: set[tuple[str, str]] = set()
    selection_candidates: list[_Candidate] = []
    for candidate in candidates:
        overlaps = False
        if development_baseline is not None:
            assert development_baseline_key is not None
            overlaps = (
                _baseline_thread_scope_id(
                    development_baseline_key,
                    candidate.account_key,
                    candidate.thread_key,
                )
                in development_baseline.thread_scope_ids
            )
        if overlaps:
            baseline_overlap_messages += 1
            baseline_overlap_threads.add(_thread_key(candidate))
        if resolved_evidence_class == RETROSPECTIVE_EVIDENCE_CLASS:
            if overlaps:
                selection_candidates.append(candidate)
        elif resolved_evidence_class == PROSPECTIVE_EVIDENCE_CLASS:
            if not overlaps:
                selection_candidates.append(candidate)
        else:
            selection_candidates.append(candidate)
    selection = _select_cohort(
        selection_candidates,
        challenge_candidates=(
            (
                tuple(
                    item
                    for item in candidates
                    if _thread_key(item) in baseline_overlap_threads
                )
                if resolved_evidence_class == RETROSPECTIVE_EVIDENCE_CLASS
                else candidates
            )
            if development_baseline is not None
            else selection_candidates
        ),
        sample_size=sample_size,
        challenge_size=challenge_size,
        reserve_size=reserve_size,
        fresh_after=fresh_after,
        quotas=validated_quotas,
    )
    primary_baseline_overlap_count = 0
    reserve_baseline_overlap_count = 0
    challenge_baseline_overlap_count = 0
    if development_baseline is not None:
        assert development_baseline_key is not None

        def selected_baseline_overlap(items: Sequence[_Candidate]) -> int:
            return sum(
                _baseline_thread_scope_id(
                    development_baseline_key,
                    item.account_key,
                    item.thread_key,
                )
                in development_baseline.thread_scope_ids
                for item in items
            )

        primary_baseline_overlap_count = selected_baseline_overlap(selection.primary)
        reserve_baseline_overlap_count = selected_baseline_overlap(selection.reserve)
        challenge_baseline_overlap_count = selected_baseline_overlap(
            selection.challenge
        )
    release_holdout_eligible = resolved_evidence_class in RELEASE_EVIDENCE_CLASSES
    primary_prospective_unseen = (
        resolved_evidence_class == PROSPECTIVE_EVIDENCE_CLASS
        and primary_baseline_overlap_count == 0
    )
    primary_historical_exposed = primary_baseline_overlap_count > 0
    challenge_prospective_unseen = (
        resolved_evidence_class == PROSPECTIVE_EVIDENCE_CLASS
        and challenge_size > 0
        and challenge_baseline_overlap_count == 0
    )
    challenge_historical_exposed = challenge_baseline_overlap_count > 0
    primary_evidence_scope = {
        DIAGNOSTIC_EVIDENCE_CLASS: "diagnostic_natural_operability",
        RETROSPECTIVE_EVIDENCE_CLASS: "retrospective_natural_operability_preview",
        PROSPECTIVE_EVIDENCE_CLASS: "prospective_natural_operability_review_only",
    }[resolved_evidence_class]
    challenge_evidence_scope = (
        "historical_balanced_capability_stress_review_only"
        if challenge_historical_exposed
        else "prospective_balanced_capability_stress_review_only"
        if challenge_prospective_unseen
        else "diagnostic_balanced_capability_stress"
    )
    primary_selection_partition = {
        DIAGNOSTIC_EVIDENCE_CLASS: "natural_historical_diagnostic",
        RETROSPECTIVE_EVIDENCE_CLASS: "natural_historical_release_review_only",
        PROSPECTIVE_EVIDENCE_CLASS: "natural_prospective_release_review_only",
    }[resolved_evidence_class]
    primary_materialized = [_materialize(paths, item) for item in selection.primary]
    challenge_materialized = [_materialize(paths, item) for item in selection.challenge]
    primary_sample_rows = [
        _sample_row(
            item,
            key=key,
            selection_partition=primary_selection_partition,
        )
        for item in primary_materialized
    ]
    challenge_sample_rows = [
        _sample_row(item, key=key, selection_partition="challenge_diagnostic")
        for item in challenge_materialized
    ]
    primary_binding_rows = [
        _binding_row(item, key=key) for item in primary_materialized
    ]
    challenge_binding_rows = [
        _binding_row(item, key=key) for item in challenge_materialized
    ]
    primary_request_rows = [
        row for item in primary_materialized for row in _request_rows(item, key=key)
    ]
    challenge_request_rows = [
        row for item in challenge_materialized for row in _request_rows(item, key=key)
    ]
    primary_label_rows = [
        _source_label_queue_row(sample, materialized, key=key)
        for sample, materialized in zip(
            primary_sample_rows,
            primary_materialized,
        )
    ]
    challenge_label_rows = [
        _source_label_queue_row(sample, materialized, key=key)
        for sample, materialized in zip(
            challenge_sample_rows,
            challenge_materialized,
        )
    ]
    reserve_order_rows = [
        _reserve_order_row(item, key=key, position=index)
        for index, item in enumerate(selection.reserve, start=1)
    ]
    reserve_binding_rows = [
        _reserve_binding_row(item, key=key) for item in selection.reserve
    ]
    primary_samples_bytes = _jsonl_bytes(primary_sample_rows)
    challenge_samples_bytes = _jsonl_bytes(challenge_sample_rows)
    primary_bindings_bytes = _jsonl_bytes(primary_binding_rows)
    challenge_bindings_bytes = _jsonl_bytes(challenge_binding_rows)
    primary_requests_bytes = _jsonl_bytes(primary_request_rows)
    challenge_requests_bytes = _jsonl_bytes(challenge_request_rows)
    primary_labels_bytes = _jsonl_bytes(primary_label_rows)
    challenge_labels_bytes = _jsonl_bytes(challenge_label_rows)
    reserve_order_bytes = _jsonl_bytes(reserve_order_rows)
    reserve_bindings_bytes = _jsonl_bytes(reserve_binding_rows)
    label_manifest = {
        "version": LABEL_MANIFEST_VERSION,
        "primary_count": len(primary_label_rows),
        "challenge_count": len(challenge_label_rows),
        "primary_sha256": _sha256_bytes(primary_labels_bytes),
        "challenge_sha256": _sha256_bytes(challenge_labels_bytes),
        "diagnostic_denominator": "primary_only",
        "pipeline_predictions_present": False,
        "admission_decisions_present": False,
        "selection_strata_present": False,
        "label_time_basis": ("target_assertion_as_of_target_message_internal_at"),
        "later_context_policy": (
            "identity_or_lifecycle_clarification_only_never_rewrite_target_assertion"
        ),
        "release_holdout_eligible": release_holdout_eligible,
    }
    label_manifest_bytes = _canonical_json(label_manifest) + b"\n"
    primary_strata = Counter(
        stratum for item in selection.primary for stratum in item.strata
    )
    challenge_strata = Counter(
        stratum for item in selection.challenge for stratum in item.strata
    )
    primary_admissions = Counter(item.admission_basis for item in selection.primary)
    challenge_admissions = Counter(item.admission_basis for item in selection.challenge)
    primary_dispositions = Counter(item.disposition for item in selection.primary)
    challenge_dispositions = Counter(item.disposition for item in selection.challenge)
    primary_errors = Counter(
        item.error_bucket for item in selection.primary if item.error_bucket is not None
    )
    challenge_errors = Counter(
        item.error_bucket
        for item in selection.challenge
        if item.error_bucket is not None
    )
    population_errors = Counter(
        item.error_bucket for item in candidates if item.error_bucket is not None
    )
    artifact_bytes = {
        "label-queue/primary.jsonl": primary_labels_bytes,
        "label-queue/challenge.jsonl": challenge_labels_bytes,
        "label-queue/manifest.json": label_manifest_bytes,
        "evaluation-authority/primary-samples.jsonl": primary_samples_bytes,
        "evaluation-authority/challenge-samples.jsonl": challenge_samples_bytes,
        "evaluation-authority/primary-bindings.jsonl": primary_bindings_bytes,
        "evaluation-authority/challenge-bindings.jsonl": challenge_bindings_bytes,
        "evaluation-authority/primary-requests.jsonl": primary_requests_bytes,
        "evaluation-authority/challenge-requests.jsonl": challenge_requests_bytes,
        "sealed-reserve/order.jsonl": reserve_order_bytes,
        "sealed-reserve/bindings.jsonl": reserve_bindings_bytes,
    }
    manifest = {
        "version": MANIFEST_VERSION,
        "builder_version": VERSION,
        "builder_sha256": _sha256_bytes(Path(__file__).read_bytes()),
        "selection_policy_version": SELECTION_POLICY_VERSION,
        "challenge_selection_policy_version": CHALLENGE_SELECTION_POLICY_VERSION,
        "runner_policy_fingerprint": gmail_temporal_runner_policy_fingerprint(),
        "admission_policy_fingerprint": gmail_temporal_admission_policy_fingerprint(),
        "verifier_policy_fingerprint": gmail_temporal_verifier_policy_fingerprint(),
        "source_target_count": len(targets),
        "source_document_count": discovery.active_document_count,
        "source_corpus_fingerprint": discovery.corpus_fingerprint,
        "scannable_target_count": len(candidates),
        "selection_eligible_target_count": len(selection_candidates),
        "scan_error_count": sum(scan_errors.values()),
        "development_baseline_present": development_baseline is not None,
        "development_baseline_manifest_version": (
            DEVELOPMENT_BASELINE_MANIFEST_VERSION
            if development_baseline is not None
            else None
        ),
        "development_baseline_thread_scope_version": (
            DEVELOPMENT_BASELINE_THREAD_VERSION
            if development_baseline is not None
            else None
        ),
        "development_baseline_overlap_message_count": baseline_overlap_messages,
        "development_baseline_overlap_thread_count": len(baseline_overlap_threads),
        "development_baseline_primary_overlap_count": (primary_baseline_overlap_count),
        "development_baseline_reserve_overlap_count": (reserve_baseline_overlap_count),
        "development_baseline_challenge_overlap_count": (
            challenge_baseline_overlap_count
        ),
        "development_baseline_corpus_fingerprint": (
            development_baseline.corpus_fingerprint
            if development_baseline is not None
            else None
        ),
        "development_baseline_artifact_set_sha256": (
            development_baseline.artifact_set_sha256
            if development_baseline is not None
            else None
        ),
        "development_baseline_manifest_sha256": (
            development_baseline.manifest_sha256
            if development_baseline is not None
            else None
        ),
        "primary_sample_count": len(primary_sample_rows),
        "primary_thread_count": len(
            {item["thread_id"] for item in primary_sample_rows}
        ),
        "challenge_sample_count": len(challenge_sample_rows),
        "challenge_thread_count": len(
            {item["thread_id"] for item in challenge_sample_rows}
        ),
        "reserve_sample_count": len(selection.reserve),
        "reserve_thread_count": len({_thread_key(item) for item in selection.reserve}),
        "fresh_after": selection.fresh_after,
        "fresh_tail_thread_count": selection.fresh_thread_count,
        "primary_request_count": len(primary_request_rows),
        "challenge_request_count": len(challenge_request_rows),
        "primary_candidate_count": sum(
            item.candidate_count for item in selection.primary
        ),
        "challenge_candidate_count": sum(
            item.candidate_count for item in selection.challenge
        ),
        "primary_max_candidate_count": max(
            (item.candidate_count for item in selection.primary),
            default=0,
        ),
        "challenge_max_candidate_count": max(
            (item.candidate_count for item in selection.challenge),
            default=0,
        ),
        "primary_page_count": sum(item.page_count for item in selection.primary),
        "challenge_page_count": sum(item.page_count for item in selection.challenge),
        "primary_max_page_count": max(
            (item.page_count for item in selection.primary),
            default=0,
        ),
        "challenge_max_page_count": max(
            (item.page_count for item in selection.challenge),
            default=0,
        ),
        "challenge_page_budget": MAX_CHALLENGE_TOTAL_PAGES,
        "challenge_candidate_budget": MAX_CHALLENGE_TOTAL_CANDIDATES,
        "challenge_per_message_page_budget": MAX_CHALLENGE_MESSAGE_PAGES,
        "challenge_quotas": {name: minimum for name, minimum in validated_quotas},
        "primary_selection_strata": dict(sorted(primary_strata.items())),
        "challenge_selection_strata": dict(sorted(challenge_strata.items())),
        "primary_admission": dict(sorted(primary_admissions.items())),
        "challenge_admission": dict(sorted(challenge_admissions.items())),
        "primary_dispositions": dict(sorted(primary_dispositions.items())),
        "challenge_dispositions": dict(sorted(challenge_dispositions.items())),
        "primary_error_buckets": dict(sorted(primary_errors.items())),
        "challenge_error_buckets": dict(sorted(challenge_errors.items())),
        "population_error_buckets": dict(sorted(population_errors.items())),
        "population_preparation_failure_count": sum(population_errors.values()),
        "artifact_sha256": {
            name: _sha256_bytes(payload)
            for name, payload in sorted(artifact_bytes.items())
        },
        "sample_identity": "hmac_opaque_account_thread_revision_message",
        "thread_policy": "at_most_one_message_per_thread",
        "label_status": "unlabeled",
        "diagnostic_denominator": "primary_only",
        "natural_selection": (
            "post_cutoff_hmac_rank_without_pipeline_output_or_label_access"
        ),
        "challenge_selection": (
            "exact_binary_multicover_primary_cost_then_verified_unique_canonical_secondary"
        ),
        "sealed_reserve_selection": "next_unused_fresh_tail_hmac_ranked_threads",
        "primary_reserve_prefix_stable": True,
        "reserve_activation_policy": (
            "regression_diagnostic_only_promote_in_authenticated_order_then_freeze_new_150_100_75_for_release"
        ),
        "prior_development_cohort_exclusion": (
            "frozen_all_current_thread_baseline"
            if resolved_evidence_class == PROSPECTIVE_EVIDENCE_CLASS
            else "legacy_semantic_cohort_bindings_unrecoverable"
            if resolved_evidence_class == RETROSPECTIVE_EVIDENCE_CLASS
            else "not_proven_old_bindings_unrecoverable"
        ),
        "prior_development_overlap_proven_zero": (
            resolved_evidence_class == PROSPECTIVE_EVIDENCE_CLASS
        ),
        "prior_development_overlap_proven_zero_applies_to": (
            "primary_and_reserve"
        ),
        "freshness_proof": (
            "baseline_absent_thread_scope_plus_chronological_cutoff"
            if resolved_evidence_class == PROSPECTIVE_EVIDENCE_CLASS
            else "frozen_historical_corpus_plus_chronological_cutoff"
            if resolved_evidence_class == RETROSPECTIVE_EVIDENCE_CLASS
            else "chronological_cutoff_only"
        ),
        "release_evidence_class": resolved_evidence_class,
        "release_evidence_class_applies_to": "primary_natural_cohort",
        "primary_evidence_scope": primary_evidence_scope,
        "primary_prospective_unseen_source_evidence": primary_prospective_unseen,
        "primary_historical_architecture_exposed": primary_historical_exposed,
        "challenge_evidence_scope": challenge_evidence_scope,
        "challenge_prospective_unseen_source_evidence": (
            challenge_prospective_unseen
        ),
        "challenge_historical_architecture_exposed": challenge_historical_exposed,
        "challenge_population_inference_eligible": False,
        "challenge_required_as_separate_promotion_gate": True,
        "cohort_metrics_must_not_be_pooled": True,
        "freeze_authority_version": (
            FREEZE_AUTHORITY_VERSION if freeze_attempt is not None else None
        ),
        "freeze_attempt_version": (
            FREEZE_ATTEMPT_VERSION if freeze_attempt is not None else None
        ),
        "freeze_outcome_version": (
            FREEZE_OUTCOME_VERSION if freeze_attempt is not None else None
        ),
        "freeze_authority_manifest_sha256": (
            freeze_attempt.authority_manifest_sha256
            if freeze_attempt is not None
            else None
        ),
        "freeze_attempt_id": (
            freeze_attempt.attempt_id if freeze_attempt is not None else None
        ),
        "freeze_attempt_sha256": (
            freeze_attempt.attempt_sha256 if freeze_attempt is not None else None
        ),
        "freeze_milestone": (
            freeze_attempt.milestone if freeze_attempt is not None else None
        ),
        "freeze_authority_evidence_class": (
            freeze_attempt.evidence_class if freeze_attempt is not None else None
        ),
        "freeze_authority_status": (
            "retained_owner_attempt_registered_before_discovery_selection_and_publication"
            if freeze_attempt is not None
            else "unverified_no_canonical_attempt_ledger"
        ),
        "freeze_no_reroll_scope": (
            FREEZE_NO_REROLL_SCOPE if freeze_attempt is not None else "unverified"
        ),
        "freeze_authority_independently_reverified_downstream": False,
        "freeze_irrevocable_from_first_materialization": freeze_attempt is not None,
        "labeled_cohort_reroll_forbidden": freeze_attempt is not None,
        "all_labeled_attempts_must_be_retained": freeze_attempt is not None,
        "source_labels_must_be_sealed_before_verifier_outputs_opened": True,
        "primary_population_scope": (
            "new_thread_only_unseen"
            if resolved_evidence_class == PROSPECTIVE_EVIDENCE_CLASS
            else "historical_baseline_thread_preview"
            if resolved_evidence_class == RETROSPECTIVE_EVIDENCE_CLASS
            else "diagnostic_unrestricted"
        ),
        "representative_gmail_production_eligible": False,
        "prospective_existing_thread_update_gate_required": (
            resolved_evidence_class in BASELINE_BACKED_EVIDENCE_CLASSES
        ),
        "prospective_natural_recall_continuation_required": (
            resolved_evidence_class in BASELINE_BACKED_EVIDENCE_CLASSES
        ),
        "prospective_natural_material_minimum": 20,
        "prospective_natural_effective_recall_minimum": 0.90,
        "prospective_natural_recall_continuation_passed": False,
        "underpowered_primary_action": (
            "publish_failure_then_activate_sealed_reserve_in_authenticated_order_for_regression_diagnostic_only_then_fresh_150_100_75_required_for_release"
        ),
        "underpowered_challenge_action": (
            "publish_underpowered_result_then_versioned_redesign_no_reroll"
        ),
        "prospective_unseen_source_evidence": (
            resolved_evidence_class == PROSPECTIVE_EVIDENCE_CLASS
        ),
        "historical_architecture_exposed": (
            resolved_evidence_class == RETROSPECTIVE_EVIDENCE_CLASS
        ),
        "retrospective_calibration_eligible": (
            resolved_evidence_class == RETROSPECTIVE_EVIDENCE_CLASS
        ),
        "semantic_development_overlap_status": (
            "excluded_by_frozen_thread_scope"
            if resolved_evidence_class == PROSPECTIVE_EVIDENCE_CLASS
            else "unknown_legacy_cohort_bindings_unrecoverable"
            if resolved_evidence_class == RETROSPECTIVE_EVIDENCE_CLASS
            else "not_release_evidence"
        ),
        "release_scope": (
            "local_review_only"
            if resolved_evidence_class == PROSPECTIVE_EVIDENCE_CLASS
            else "local_review_preview"
            if resolved_evidence_class == RETROSPECTIVE_EVIDENCE_CLASS
            else "diagnostic_only"
        ),
        "automatic_apply_eligible": False,
        "content_changing_canary_required": (
            resolved_evidence_class in BASELINE_BACKED_EVIDENCE_CLASSES
        ),
        "release_holdout_eligible": release_holdout_eligible,
        "labeler_artifact": "label-queue/primary.jsonl",
        "challenge_labeler_artifact": "label-queue/challenge.jsonl",
        "labeler_must_not_inspect_internal_artifacts": True,
        "label_time_basis": ("target_assertion_as_of_target_message_internal_at"),
        "later_context_policy": (
            "identity_or_lifecycle_clarification_only_never_rewrite_target_assertion"
        ),
        "reserve_source_text_materialized": False,
        "private_file_mode": "0600",
        "private_directory_mode": "0700",
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    manifest_bytes = _authenticated_manifest_bytes(manifest, key=key)
    _publish_frozen(
        output_root,
        {**artifact_bytes, "manifest.json": manifest_bytes},
    )
    if freeze_attempt is not None:
        assert freeze_authority_key is not None
        _complete_freeze_attempt(
            freeze_attempt,
            authority_key=freeze_authority_key,
            holdout_manifest_raw=manifest_bytes,
        )
    return {
        "version": VERSION,
        "primary_samples": len(primary_sample_rows),
        "primary_threads": manifest["primary_thread_count"],
        "challenge_samples": manifest["challenge_sample_count"],
        "sealed_reserve": manifest["reserve_sample_count"],
        "fresh_tail_threads": manifest["fresh_tail_thread_count"],
        "release_holdout_eligible": manifest["release_holdout_eligible"],
        "primary_candidate_bearing": primary_strata.get("candidate_bearing", 0),
        "primary_policy_proxy_hard_negatives": primary_strata.get("hard_negative", 0),
        "challenge_candidate_bearing": challenge_strata.get("candidate_bearing", 0),
        "challenge_policy_proxy_hard_negatives": challenge_strata.get(
            "hard_negative", 0
        ),
        "challenge_fact_candidates": challenge_strata.get("fact_candidate", 0),
        "challenge_temporal_rescue_candidates": challenge_strata.get(
            "temporal_rescue_candidate", 0
        ),
        "challenge_weak_advertising_candidates": challenge_strata.get(
            "weak_advertising_candidate", 0
        ),
        "challenge_lifecycle_source": challenge_strata.get("lifecycle_source", 0),
        "challenge_temporal_forms_without_candidates": challenge_strata.get(
            "temporal_form_without_candidate", 0
        ),
        "primary_requests": len(primary_request_rows),
        "challenge_requests": len(challenge_request_rows),
        "population_preparation_failures": sum(population_errors.values()),
        "manifest_sha256": _sha256_bytes(manifest_bytes),
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--hmac-key", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--fresh-after", required=True)
    parser.add_argument("--development-baseline-root", type=Path)
    parser.add_argument("--development-baseline-key", type=Path)
    parser.add_argument("--freeze-authority-root", type=Path)
    parser.add_argument("--freeze-authority-key", type=Path)
    parser.add_argument("--freeze-milestone")
    parser.add_argument(
        "--evidence-class",
        choices=(
            DIAGNOSTIC_EVIDENCE_CLASS,
            RETROSPECTIVE_EVIDENCE_CLASS,
            PROSPECTIVE_EVIDENCE_CLASS,
        ),
    )
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument(
        "--challenge-size",
        type=int,
        default=DEFAULT_CHALLENGE_SIZE,
    )
    parser.add_argument("--reserve-size", type=int, default=DEFAULT_RESERVE_SIZE)
    args = parser.parse_args()
    try:
        result = build_gmail_temporal_holdout(
            args.home,
            args.hmac_key,
            args.output_root,
            fresh_after=args.fresh_after,
            development_baseline_root=args.development_baseline_root,
            development_baseline_key_path=args.development_baseline_key,
            sample_size=args.sample_size,
            challenge_size=args.challenge_size,
            reserve_size=args.reserve_size,
            evidence_class=args.evidence_class,
            freeze_authority_root=args.freeze_authority_root,
            freeze_authority_key_path=args.freeze_authority_key,
            freeze_milestone=args.freeze_milestone,
        )
    except GmailTemporalHoldoutError as exc:
        print(
            json.dumps(
                {
                    "version": VERSION,
                    "status": "failed",
                    "error": _SAFE_FAILURE_CODES.get(
                        str(exc),
                        "gmail_temporal_holdout_validation_failed",
                    ),
                    "private_content_printed": False,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        raise SystemExit(2) from None
    except Exception:  # noqa: BLE001 - never print private paths or source details.
        print(
            json.dumps(
                {
                    "version": VERSION,
                    "status": "failed",
                    "error": "gmail_temporal_holdout_internal_failure",
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
