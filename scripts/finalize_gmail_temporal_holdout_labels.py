#!/usr/bin/env python3
"""Finalize blind Gmail temporal holdout labels into authenticated private gold.

The finalizer is deliberately local and deterministic.  It verifies the
builder's complete frozen artifact inventory, authenticates its manifest,
proves that a completed label file changed only label fields, validates the
semantic-gold contract, and publishes a no-replace owner-only gold bundle.
It performs no model, network, or Brain persistence calls.
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
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any


VERSION = "gmail_temporal_holdout_label_finalizer_v4"
BUILDER_MANIFEST_VERSION = "gmail_temporal_holdout_manifest_v5"
LEGACY_BUILDER_MANIFEST_VERSION = "gmail_temporal_holdout_manifest_v4"
LABEL_MANIFEST_VERSION = "gmail_temporal_holdout_label_manifest_v2"
LABEL_QUEUE_VERSION = "gmail_temporal_holdout_source_label_queue_v2"
SAMPLE_VERSION = "gmail_temporal_holdout_sample_v2"
BINDING_VERSION = "gmail_temporal_holdout_binding_v1"
REQUEST_VERSION = "gmail_temporal_holdout_request_v1"
RESERVE_ORDER_VERSION = "gmail_temporal_holdout_reserve_order_v2"
GOLD_MANIFEST_VERSION = "gmail_temporal_holdout_gold_manifest_v4"
BUILDER_MANIFEST_DOMAIN = b"gmail_temporal_holdout_manifest_v5\0"
LEGACY_BUILDER_MANIFEST_DOMAIN = b"gmail_temporal_holdout_manifest_v4\0"
GOLD_MANIFEST_DOMAIN = b"gmail_temporal_holdout_gold_manifest_v4\0"
LABEL_AUTHORITY_VERSION = "gmail_temporal_holdout_label_authority_v2"
LABEL_AUTHORITY_DOMAIN = b"gmail_temporal_holdout_label_authority_v2\0"
LABEL_AUTHORITY_MODEL = "gpt-5.6-sol"
LABEL_AUTHORITY_REASONING_EFFORT = "medium"
EXTERNAL_PLAN_VERSION = "gmail_temporal_holdout_external_plan_v1"
EXTERNAL_CALL_REQUEST_VERSION = "gmail_temporal_holdout_external_call_request_v1"
EXTERNAL_CALL_START_VERSION = "gmail_temporal_holdout_external_call_start_v1"
EXTERNAL_CALL_RECEIPT_VERSION = "gmail_temporal_holdout_external_call_receipt_v1"
EXTERNAL_PLAN_DOMAIN = b"gmail_temporal_holdout_external_plan_v1\0"
EXTERNAL_CALL_START_DOMAIN = b"gmail_temporal_holdout_external_call_start_v1\0"
EXTERNAL_CALL_RECEIPT_DOMAIN = b"gmail_temporal_holdout_external_call_receipt_v1\0"
EXTERNAL_PROVIDER = "external-codex"
FREEZE_AUTHORITY_VERSION = "gmail_temporal_holdout_freeze_authority_v1"
FREEZE_ATTEMPT_VERSION = "gmail_temporal_holdout_freeze_attempt_v1"
FREEZE_OUTCOME_VERSION = "gmail_temporal_holdout_freeze_outcome_v1"
CANONICAL_FREEZE_AUTHORITY_STATUS = (
    "retained_owner_attempt_registered_before_discovery_selection_and_publication"
)
FREEZE_NO_REROLL_SCOPE = "within_single_retained_owner_authority_root"
LEGACY_V15_MANIFEST_SHA256 = (
    "5acc5cc02414646adc417513c65fbc6596e11d66f81e24521704b4ee156f8fb0"
)
LEGACY_V15_BUILDER_SHA256 = (
    "c2703850963409f33c69f5dd9ce0ca7d5479b5e6fde61fecac7e1281489700a7"
)
DEVELOPMENT_BASELINE_MANIFEST_VERSION = (
    "gmail_temporal_development_baseline_manifest_v2"
)
DEVELOPMENT_BASELINE_THREAD_SCOPE_VERSION = "gmail_temporal_development_thread_scope_v1"
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
MIN_HMAC_KEY_BYTES = 32
PRIMARY_MIN_LABELED_HARD_NEGATIVES = 40
CHALLENGE_MIN_EXPECTED_MATERIAL_RECORDS = 30
CHALLENGE_MIN_SEMANTIC_MEMBERS = 60
CHALLENGE_MIN_SUPPORTED_MEMBERS = 30
CHALLENGE_MIN_LABELED_HARD_NEGATIVES = 20
BASELINE_GRADE_PLACEHOLDER = "pending_adapter_recompute"
LABEL_TIME_BASIS = "target_assertion_as_of_target_message_internal_at"
LATER_CONTEXT_POLICY = (
    "identity_or_lifecycle_clarification_only_never_rewrite_target_assertion"
)
DIAGNOSTIC_EVIDENCE_CLASS = "diagnostic_only"
RETROSPECTIVE_EVIDENCE_CLASS = "retrospective_label_blind_review_only"
PROSPECTIVE_EVIDENCE_CLASS = "prospective_thread_unseen_review_only"
BASELINE_BACKED_EVIDENCE_CLASSES = frozenset(
    {RETROSPECTIVE_EVIDENCE_CLASS, PROSPECTIVE_EVIDENCE_CLASS}
)
BUILDER_SCRIPT_PATH = Path(__file__).with_name("build_gmail_temporal_holdout.py")

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SAMPLE_ID_PATTERN = re.compile(r"^gths_[0-9a-f]{64}$")
_THREAD_ID_PATTERN = re.compile(r"^gtht_[0-9a-f]{64}$")
_MESSAGE_ID_PATTERN = re.compile(r"^gthm_[0-9a-f]{64}$")
_LABEL_INVOCATION_ID_PATTERN = re.compile(r"^gthla_i_[0-9a-f]{64}$")
_LOGICAL_RUN_ID_PATTERN = re.compile(r"^gthxr_r_[0-9a-f]{64}$")
_EXTERNAL_UNIT_ID_PATTERN = re.compile(r"^gthxu_[0-9a-f]{64}$")
_FREEZE_ATTEMPT_ID_PATTERN = re.compile(r"^gthfa_[0-9a-f]{64}$")
_VERDICTS = {"supported", "uncertain"}
_FILTERS = {"should_admit", "should_suppress"}
_STRATUM_ADMISSION = {
    "important_fact": "fact",
    "suppressed_temporal_rescue": "temporal_rescue",
    "noise_not_admitted": "not_admitted",
}
_LABEL_FIELDS = {
    "label_status",
    "expected_material",
    "expected_filter",
    "hard_negative",
    "semantic_units",
    "critical_error",
    "notes",
}
_QUEUE_KEYS = {
    "version",
    "sample_id",
    "thread_id",
    "target",
    "thread_context",
    "context_is_label_only",
    *_LABEL_FIELDS,
}
_TARGET_KEYS = {
    "message_internal_at",
    "text",
    "source_char_count",
    "emitted_char_count",
    "sanitized_text_sha256",
    "body_truncation_status",
}
_CONTEXT_KEYS = {
    "prior_available",
    "prior_included",
    "prior_omitted",
    "later_available",
    "later_included",
    "later_omitted",
    "source_omitted_before_count",
    "source_truncated_message_count",
    "messages",
}
_CONTEXT_MESSAGE_KEYS = {
    "message_id",
    "relative_position",
    "message_internal_at",
    "text",
    "source_char_count",
    "emitted_char_count",
    "text_truncated_after",
}
_LABEL_MANIFEST_KEYS = {
    "version",
    "primary_count",
    "challenge_count",
    "primary_sha256",
    "challenge_sha256",
    "diagnostic_denominator",
    "pipeline_predictions_present",
    "admission_decisions_present",
    "selection_strata_present",
    "release_holdout_eligible",
    "label_time_basis",
    "later_context_policy",
}
_UNIT_KEYS = {"unit_id", "truth", "baseline_frontier_grade", "members"}
_MEMBER_KEYS = {
    "member_id",
    "expected_verdict",
    "baseline_frontier_grade",
    "alternatives",
}
_ALTERNATIVE_KEYS = {"quality", "expected_verdict", "locator"}
_LOCATOR_KEYS = {"expression", "subject", "lifecycle_mention", "derived"}
_EXPRESSION_KEYS = {"surface", "form", "field"}
_SUBJECT_KEYS = {"surface", "mention_type", "field"}
_LIFECYCLE_KEYS = {"surface", "lifecycle_role", "field"}
_DERIVED_KEYS = {
    "relation",
    "kind",
    "lifecycle",
    "normalized_value",
    "requires_defer",
}
_REQUEST_KEYS = {
    "version",
    "sample_id",
    "request_fingerprint",
    "batch_fingerprint",
    "frontier_fingerprint",
    "page_plan_fingerprint",
    "page_fingerprint",
    "candidate_count",
    "payload",
    "routable",
}
_RELEASE_REQUIRED_ARTIFACTS = frozenset(
    {
        "label-queue/primary.jsonl",
        "label-queue/challenge.jsonl",
        "label-queue/manifest.json",
        "evaluation-authority/primary-samples.jsonl",
        "evaluation-authority/challenge-samples.jsonl",
        "evaluation-authority/primary-bindings.jsonl",
        "evaluation-authority/challenge-bindings.jsonl",
        "evaluation-authority/primary-requests.jsonl",
        "evaluation-authority/challenge-requests.jsonl",
        "sealed-reserve/order.jsonl",
        "sealed-reserve/bindings.jsonl",
    }
)


class GmailTemporalLabelFinalizerError(ValueError):
    """Raised when blind labels cannot safely become release gold."""


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


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise GmailTemporalLabelFinalizerError("JSON object has duplicate keys")
        output[key] = value
    return output


def _parse_json(value: bytes, *, description: str) -> Any:
    try:
        return json.loads(
            value.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GmailTemporalLabelFinalizerError(
            f"{description} is not valid JSON"
        ) from exc


def _private_regular_file(path: Path, *, description: str) -> bytes:
    descriptor: int | None = None
    try:
        info = os.lstat(path)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != PRIVATE_FILE_MODE
            or info.st_nlink != 1
        ):
            raise GmailTemporalLabelFinalizerError(
                f"{description} must be an owner-only single-link regular file"
            )
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise GmailTemporalLabelFinalizerError(f"{description} changed while read")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            return handle.read()
    except OSError as exc:
        raise GmailTemporalLabelFinalizerError(f"{description} is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _private_directory(path: Path, *, description: str) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise GmailTemporalLabelFinalizerError(f"{description} is unavailable") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != PRIVATE_DIRECTORY_MODE
    ):
        raise GmailTemporalLabelFinalizerError(
            f"{description} must be an owner-only directory"
        )


def _private_hmac_key(path: Path) -> bytes:
    value = _private_regular_file(path, description="HMAC key")
    if len(value) < MIN_HMAC_KEY_BYTES:
        raise GmailTemporalLabelFinalizerError(
            "HMAC key must contain at least 32 bytes"
        )
    return value


def _safe_artifact_name(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise GmailTemporalLabelFinalizerError("artifact name is invalid")
    relative = Path(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise GmailTemporalLabelFinalizerError("artifact name is invalid")
    return relative.as_posix()


def _load_builder_manifest(path: Path, *, key: bytes) -> tuple[dict[str, Any], bytes]:
    raw = _private_regular_file(path, description="holdout manifest")
    value = _parse_json(raw, description="holdout manifest")
    if not isinstance(value, dict):
        raise GmailTemporalLabelFinalizerError("holdout manifest schema is invalid")
    if raw != _canonical_json(value) + b"\n":
        raise GmailTemporalLabelFinalizerError("holdout manifest is not canonical")
    authenticator = value.get("manifest_hmac_sha256")
    unsigned = dict(value)
    unsigned.pop("manifest_hmac_sha256", None)
    legacy_v15 = (
        _sha256_bytes(raw) == LEGACY_V15_MANIFEST_SHA256
        and value.get("version") == LEGACY_BUILDER_MANIFEST_VERSION
        and value.get("builder_version") == "gmail_temporal_holdout_builder_v4"
        and value.get("builder_sha256") == LEGACY_V15_BUILDER_SHA256
        and value.get("release_evidence_class") == RETROSPECTIVE_EVIDENCE_CLASS
    )
    expected = hmac.new(
        key,
        (LEGACY_BUILDER_MANIFEST_DOMAIN if legacy_v15 else BUILDER_MANIFEST_DOMAIN)
        + _canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()
    if (
        not isinstance(authenticator, str)
        or _SHA256_PATTERN.fullmatch(authenticator) is None
        or not hmac.compare_digest(authenticator, expected)
    ):
        raise GmailTemporalLabelFinalizerError("holdout manifest authentication failed")
    required = {
        "version": (
            LEGACY_BUILDER_MANIFEST_VERSION
            if legacy_v15
            else BUILDER_MANIFEST_VERSION
        ),
        "builder_version": (
            "gmail_temporal_holdout_builder_v4"
            if legacy_v15
            else "gmail_temporal_holdout_builder_v5"
        ),
        "builder_sha256": (
            LEGACY_V15_BUILDER_SHA256
            if legacy_v15
            else _sha256_bytes(BUILDER_SCRIPT_PATH.read_bytes())
        ),
        "label_status": "unlabeled",
        "diagnostic_denominator": "primary_only",
        "labeler_artifact": "label-queue/primary.jsonl",
        "labeler_must_not_inspect_internal_artifacts": True,
        "thread_policy": "at_most_one_message_per_thread",
        "label_time_basis": LABEL_TIME_BASIS,
        "later_context_policy": LATER_CONTEXT_POLICY,
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    if any(
        value.get(field) != expected_value for field, expected_value in required.items()
    ):
        raise GmailTemporalLabelFinalizerError("holdout manifest policy is invalid")
    if legacy_v15:
        # The authenticated v15 artifact remains readable as historical,
        # retrospective development evidence only.  Its pre-ledger signed
        # no-reroll booleans are not evidence of a canonical first attempt.
        value = {
            **value,
            "freeze_authority_version": None,
            "freeze_attempt_version": None,
            "freeze_outcome_version": None,
            "freeze_authority_manifest_sha256": None,
            "freeze_attempt_id": None,
            "freeze_attempt_sha256": None,
            "freeze_milestone": None,
            "freeze_authority_evidence_class": None,
            "freeze_authority_status": "legacy_v15_pre_ledger_unverified",
            "freeze_no_reroll_scope": "unverified",
            "freeze_authority_independently_reverified_downstream": False,
            "freeze_irrevocable_from_first_materialization": False,
            "labeled_cohort_reroll_forbidden": False,
            "all_labeled_attempts_must_be_retained": False,
            "legacy_signed_freeze_claims_downgraded": True,
        }
    if (
        value.get("labeler_must_not_inspect_internal_artifacts") is not True
        or not isinstance(value.get("external_calls"), int)
        or isinstance(value.get("external_calls"), bool)
        or value.get("external_calls") != 0
        or not isinstance(value.get("persistence_calls"), int)
        or isinstance(value.get("persistence_calls"), bool)
        or value.get("persistence_calls") != 0
        or value.get("private_content_printed") is not False
        or value.get("routable") is not False
    ):
        raise GmailTemporalLabelFinalizerError("holdout manifest policy is invalid")
    if not isinstance(value.get("release_holdout_eligible"), bool):
        raise GmailTemporalLabelFinalizerError(
            "holdout manifest release eligibility is invalid"
        )
    evidence_class = value.get("release_evidence_class")
    prospective_unseen = value.get("prospective_unseen_source_evidence")
    historical_exposed = value.get("historical_architecture_exposed")
    retrospective_eligible = value.get("retrospective_calibration_eligible")
    canary_required = value.get("content_changing_canary_required")
    if (
        evidence_class
        not in {
            DIAGNOSTIC_EVIDENCE_CLASS,
            RETROSPECTIVE_EVIDENCE_CLASS,
            PROSPECTIVE_EVIDENCE_CLASS,
        }
        or value.get("automatic_apply_eligible") is not False
        or not isinstance(prospective_unseen, bool)
        or not isinstance(historical_exposed, bool)
        or not isinstance(retrospective_eligible, bool)
        or not isinstance(canary_required, bool)
    ):
        raise GmailTemporalLabelFinalizerError(
            "holdout manifest evidence class is invalid"
        )
    evidence_contracts = {
        DIAGNOSTIC_EVIDENCE_CLASS: {
            "release_scope": "diagnostic_only",
            "release_holdout_eligible": False,
            "prospective_unseen_source_evidence": False,
            "historical_architecture_exposed": False,
            "retrospective_calibration_eligible": False,
            "semantic_development_overlap_status": "not_release_evidence",
            "content_changing_canary_required": False,
        },
        RETROSPECTIVE_EVIDENCE_CLASS: {
            "release_scope": "local_review_preview",
            "release_holdout_eligible": False,
            "prospective_unseen_source_evidence": False,
            "historical_architecture_exposed": True,
            "retrospective_calibration_eligible": True,
            "semantic_development_overlap_status": (
                "unknown_legacy_cohort_bindings_unrecoverable"
            ),
            "content_changing_canary_required": True,
        },
        PROSPECTIVE_EVIDENCE_CLASS: {
            "release_scope": "local_review_only",
            "release_holdout_eligible": True,
            "prospective_unseen_source_evidence": True,
            "historical_architecture_exposed": False,
            "retrospective_calibration_eligible": False,
            "semantic_development_overlap_status": ("excluded_by_frozen_thread_scope"),
            "content_changing_canary_required": True,
        },
    }
    if any(
        value.get(field) != expected_value
        for field, expected_value in evidence_contracts[str(evidence_class)].items()
    ):
        raise GmailTemporalLabelFinalizerError(
            "holdout manifest evidence class is inconsistent"
        )
    artifacts = value.get("artifact_sha256")
    primary_count = value.get("primary_sample_count")
    primary_threads = value.get("primary_thread_count")
    challenge_count = value.get("challenge_sample_count")
    if (
        not isinstance(artifacts, dict)
        or not artifacts
        or not isinstance(primary_count, int)
        or isinstance(primary_count, bool)
        or primary_count < 1
        or not isinstance(primary_threads, int)
        or isinstance(primary_threads, bool)
        or primary_threads != primary_count
        or not isinstance(challenge_count, int)
        or isinstance(challenge_count, bool)
        or challenge_count < 0
    ):
        raise GmailTemporalLabelFinalizerError("holdout manifest cohort is invalid")
    primary_overlap = value.get("development_baseline_primary_overlap_count", 0)
    challenge_overlap = value.get("development_baseline_challenge_overlap_count", 0)
    if any(
        not isinstance(count, int) or isinstance(count, bool) or count < 0
        for count in (primary_overlap, challenge_overlap)
    ):
        raise GmailTemporalLabelFinalizerError(
            "holdout manifest cohort evidence is invalid"
        )
    expected_primary_scope = {
        DIAGNOSTIC_EVIDENCE_CLASS: "diagnostic_natural_operability",
        RETROSPECTIVE_EVIDENCE_CLASS: "retrospective_natural_operability_preview",
        PROSPECTIVE_EVIDENCE_CLASS: "prospective_natural_operability_review_only",
    }[str(evidence_class)]
    primary_prospective_unseen = (
        evidence_class == PROSPECTIVE_EVIDENCE_CLASS and primary_overlap == 0
    )
    challenge_prospective_unseen = (
        evidence_class == PROSPECTIVE_EVIDENCE_CLASS
        and challenge_count > 0
        and challenge_overlap == 0
    )
    challenge_historical_exposed = challenge_overlap > 0
    freeze_status = value.get("freeze_authority_status")
    freeze_verified = freeze_status == CANONICAL_FREEZE_AUTHORITY_STATUS
    freeze_digest_fields = (
        "freeze_authority_manifest_sha256",
        "freeze_attempt_sha256",
    )
    if freeze_verified:
        if (
            value.get("freeze_authority_version") != FREEZE_AUTHORITY_VERSION
            or value.get("freeze_attempt_version") != FREEZE_ATTEMPT_VERSION
            or value.get("freeze_outcome_version") != FREEZE_OUTCOME_VERSION
            or any(
                not isinstance(value.get(field), str)
                or _SHA256_PATTERN.fullmatch(str(value[field])) is None
                for field in freeze_digest_fields
            )
            or not isinstance(value.get("freeze_attempt_id"), str)
            or _FREEZE_ATTEMPT_ID_PATTERN.fullmatch(str(value["freeze_attempt_id"]))
            is None
            or not isinstance(value.get("freeze_milestone"), str)
            or not value["freeze_milestone"]
            or value.get("freeze_authority_evidence_class") != evidence_class
            or value.get("freeze_no_reroll_scope") != FREEZE_NO_REROLL_SCOPE
            or value.get("freeze_authority_independently_reverified_downstream")
            is not False
        ):
            raise GmailTemporalLabelFinalizerError(
                "holdout freeze authority is invalid"
            )
    elif (
        evidence_class == PROSPECTIVE_EVIDENCE_CLASS
        or freeze_status
        not in {
            "unverified_no_canonical_attempt_ledger",
            "legacy_v15_pre_ledger_unverified",
        }
        or any(
            value.get(field) is not None
            for field in (
                "freeze_authority_version",
                "freeze_attempt_version",
                "freeze_outcome_version",
                "freeze_authority_manifest_sha256",
                "freeze_attempt_id",
                "freeze_attempt_sha256",
                "freeze_milestone",
                "freeze_authority_evidence_class",
            )
        )
        or value.get("freeze_no_reroll_scope") != "unverified"
        or value.get("freeze_authority_independently_reverified_downstream")
        is not False
    ):
        raise GmailTemporalLabelFinalizerError(
            "holdout freeze authority is invalid"
        )
    expected_challenge_scope = (
        "historical_balanced_capability_stress_review_only"
        if challenge_historical_exposed
        else "prospective_balanced_capability_stress_review_only"
        if challenge_prospective_unseen
        else "diagnostic_balanced_capability_stress"
    )
    cohort_evidence_contract = {
        "release_evidence_class_applies_to": "primary_natural_cohort",
        "prior_development_overlap_proven_zero_applies_to": "primary_and_reserve",
        "primary_evidence_scope": expected_primary_scope,
        "primary_prospective_unseen_source_evidence": primary_prospective_unseen,
        "primary_historical_architecture_exposed": primary_overlap > 0,
        "challenge_evidence_scope": expected_challenge_scope,
        "challenge_prospective_unseen_source_evidence": (
            challenge_prospective_unseen
        ),
        "challenge_historical_architecture_exposed": challenge_historical_exposed,
        "challenge_population_inference_eligible": False,
        "challenge_required_as_separate_promotion_gate": True,
        "cohort_metrics_must_not_be_pooled": True,
        "freeze_irrevocable_from_first_materialization": freeze_verified,
        "labeled_cohort_reroll_forbidden": freeze_verified,
        "all_labeled_attempts_must_be_retained": freeze_verified,
        "freeze_no_reroll_scope": (
            FREEZE_NO_REROLL_SCOPE if freeze_verified else "unverified"
        ),
        "freeze_authority_independently_reverified_downstream": False,
        "source_labels_must_be_sealed_before_verifier_outputs_opened": True,
        "primary_population_scope": (
            "new_thread_only_unseen"
            if evidence_class == PROSPECTIVE_EVIDENCE_CLASS
            else "historical_baseline_thread_preview"
            if evidence_class == RETROSPECTIVE_EVIDENCE_CLASS
            else "diagnostic_unrestricted"
        ),
        "representative_gmail_production_eligible": False,
        "prospective_existing_thread_update_gate_required": (
            evidence_class in BASELINE_BACKED_EVIDENCE_CLASSES
        ),
        "prospective_natural_recall_continuation_required": (
            evidence_class in BASELINE_BACKED_EVIDENCE_CLASSES
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
    }
    if any(
        value.get(field) != expected
        for field, expected in cohort_evidence_contract.items()
    ):
        raise GmailTemporalLabelFinalizerError(
            "release holdout authority cohort evidence is inconsistent"
        )
    value["legacy_signed_freeze_claims_downgraded"] = legacy_v15
    required_artifacts = {
        "label-queue/primary.jsonl",
        "label-queue/challenge.jsonl",
        "label-queue/manifest.json",
    }
    normalized: dict[str, str] = {}
    for raw_name, digest in artifacts.items():
        name = _safe_artifact_name(raw_name)
        if (
            name in normalized
            or not isinstance(digest, str)
            or _SHA256_PATTERN.fullmatch(digest) is None
        ):
            raise GmailTemporalLabelFinalizerError(
                "holdout artifact commitment is invalid"
            )
        normalized[name] = digest
    if not required_artifacts.issubset(normalized):
        raise GmailTemporalLabelFinalizerError("holdout label artifacts are incomplete")
    if evidence_class == RETROSPECTIVE_EVIDENCE_CLASS:
        corpus_fingerprint = value.get("development_baseline_corpus_fingerprint")
        artifact_set_sha256 = value.get("development_baseline_artifact_set_sha256")
        baseline_manifest_sha256 = value.get("development_baseline_manifest_sha256")
        reserve_count = value.get("reserve_sample_count")
        if (
            value.get("development_baseline_present") is not True
            or value.get("development_baseline_manifest_version")
            != DEVELOPMENT_BASELINE_MANIFEST_VERSION
            or value.get("development_baseline_thread_scope_version")
            != DEVELOPMENT_BASELINE_THREAD_SCOPE_VERSION
            or value.get("prior_development_overlap_proven_zero") is not False
            or value.get("development_baseline_primary_overlap_count") != primary_count
            or value.get("development_baseline_reserve_overlap_count") != reserve_count
            or value.get("development_baseline_challenge_overlap_count")
            != challenge_count
            or not isinstance(corpus_fingerprint, str)
            or re.fullmatch(r"gtdb_c_[0-9a-f]{64}", corpus_fingerprint) is None
            or not isinstance(artifact_set_sha256, str)
            or _SHA256_PATTERN.fullmatch(artifact_set_sha256) is None
            or not isinstance(baseline_manifest_sha256, str)
            or _SHA256_PATTERN.fullmatch(baseline_manifest_sha256) is None
            or primary_count != 150
            or challenge_count != 100
            or not isinstance(reserve_count, int)
            or isinstance(reserve_count, bool)
            or reserve_count != 75
            or set(normalized) != _RELEASE_REQUIRED_ARTIFACTS
        ):
            raise GmailTemporalLabelFinalizerError(
                "retrospective calibration authority is incomplete"
            )
    if value["release_holdout_eligible"]:
        corpus_fingerprint = value.get("development_baseline_corpus_fingerprint")
        artifact_set_sha256 = value.get("development_baseline_artifact_set_sha256")
        baseline_manifest_sha256 = value.get("development_baseline_manifest_sha256")
        reserve_count = value.get("reserve_sample_count")
        if (
            value.get("development_baseline_present") is not True
            or value.get("development_baseline_manifest_version")
            != DEVELOPMENT_BASELINE_MANIFEST_VERSION
            or value.get("development_baseline_thread_scope_version")
            != DEVELOPMENT_BASELINE_THREAD_SCOPE_VERSION
            or value.get("prior_development_overlap_proven_zero") is not True
            or not isinstance(
                value.get("development_baseline_primary_overlap_count"), int
            )
            or isinstance(value.get("development_baseline_primary_overlap_count"), bool)
            or value.get("development_baseline_primary_overlap_count") != 0
            or not isinstance(
                value.get("development_baseline_reserve_overlap_count"), int
            )
            or isinstance(value.get("development_baseline_reserve_overlap_count"), bool)
            or value.get("development_baseline_reserve_overlap_count") != 0
            or not isinstance(corpus_fingerprint, str)
            or re.fullmatch(r"gtdb_c_[0-9a-f]{64}", corpus_fingerprint) is None
            or not isinstance(artifact_set_sha256, str)
            or _SHA256_PATTERN.fullmatch(artifact_set_sha256) is None
            or not isinstance(baseline_manifest_sha256, str)
            or _SHA256_PATTERN.fullmatch(baseline_manifest_sha256) is None
            or primary_count != 150
            or challenge_count != 100
            or not isinstance(reserve_count, int)
            or isinstance(reserve_count, bool)
            or reserve_count != 75
            or set(normalized) != _RELEASE_REQUIRED_ARTIFACTS
        ):
            raise GmailTemporalLabelFinalizerError(
                "release holdout authority is incomplete"
            )
    value["artifact_sha256"] = normalized
    return value, raw


def _verify_artifact_inventory(
    root: Path,
    *,
    artifact_sha256: Mapping[str, str],
) -> dict[str, bytes]:
    _private_directory(root, description="holdout root")
    expected_files = {"manifest.json", *artifact_sha256}
    expected_directories = {"."}
    for name in artifact_sha256:
        parent = Path(name).parent
        while parent != Path("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    actual_files: set[str] = set()
    actual_directories = {"."}
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        relative_directory = directory_path.relative_to(root)
        if relative_directory != Path("."):
            _private_directory(directory_path, description="holdout artifact directory")
        for name in directory_names:
            child = directory_path / name
            try:
                info = os.lstat(child)
            except OSError as exc:
                raise GmailTemporalLabelFinalizerError(
                    "holdout inventory is unavailable"
                ) from exc
            if not stat.S_ISDIR(info.st_mode):
                raise GmailTemporalLabelFinalizerError(
                    "holdout inventory contains an unsafe entry"
                )
            relative = child.relative_to(root).as_posix()
            actual_directories.add(relative)
        for name in file_names:
            actual_files.add((directory_path / name).relative_to(root).as_posix())
    if actual_files != expected_files or actual_directories != expected_directories:
        raise GmailTemporalLabelFinalizerError("holdout inventory is not exact")
    output: dict[str, bytes] = {}
    for name, expected_digest in sorted(artifact_sha256.items()):
        payload = _private_regular_file(
            root / name,
            description="holdout artifact",
        )
        if not hmac.compare_digest(_sha256_bytes(payload), expected_digest):
            raise GmailTemporalLabelFinalizerError("holdout artifact commitment failed")
        output[name] = payload
    return output


def _load_label_manifest(
    raw: bytes,
    *,
    artifacts: Mapping[str, bytes],
    root_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    value = _parse_json(raw, description="label manifest")
    if (
        not isinstance(value, dict)
        or set(value) != _LABEL_MANIFEST_KEYS
        or value.get("version") != LABEL_MANIFEST_VERSION
        or value.get("diagnostic_denominator") != "primary_only"
        or value.get("pipeline_predictions_present") is not False
        or value.get("admission_decisions_present") is not False
        or value.get("selection_strata_present") is not False
        or not isinstance(value.get("release_holdout_eligible"), bool)
        or value.get("label_time_basis") != LABEL_TIME_BASIS
        or value.get("later_context_policy") != LATER_CONTEXT_POLICY
        or raw != _canonical_json(value) + b"\n"
    ):
        raise GmailTemporalLabelFinalizerError("label manifest schema is invalid")
    for count_field in ("primary_count", "challenge_count"):
        count = value.get(count_field)
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise GmailTemporalLabelFinalizerError("label manifest count is invalid")
    for digest_field in ("primary_sha256", "challenge_sha256"):
        digest = value.get(digest_field)
        if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
            raise GmailTemporalLabelFinalizerError(
                "label manifest commitment is invalid"
            )
    primary = artifacts["label-queue/primary.jsonl"]
    challenge = artifacts["label-queue/challenge.jsonl"]
    challenge_rows = (
        []
        if not challenge
        else _load_jsonl(challenge, description="challenge label queue")
    )
    if (
        value["primary_count"] != root_manifest["primary_sample_count"]
        or value["challenge_count"] != root_manifest["challenge_sample_count"]
        or value["challenge_count"] != len(challenge_rows)
        or value["primary_sha256"] != _sha256_bytes(primary)
        or value["challenge_sha256"] != _sha256_bytes(challenge)
        or _jsonl_bytes(challenge_rows) != challenge
        or value["release_holdout_eligible"]
        is not root_manifest.get("release_holdout_eligible")
    ):
        raise GmailTemporalLabelFinalizerError(
            "label manifest does not bind the frozen queues"
        )
    return value


def _load_jsonl(raw: bytes, *, description: str) -> list[dict[str, Any]]:
    if not raw or not raw.endswith(b"\n"):
        raise GmailTemporalLabelFinalizerError(f"{description} is not complete JSONL")
    lines = raw.splitlines()
    if not lines or any(not line.strip() for line in lines):
        raise GmailTemporalLabelFinalizerError(f"{description} is malformed")
    rows: list[dict[str, Any]] = []
    for line in lines:
        value = _parse_json(line, description=description)
        if not isinstance(value, dict):
            raise GmailTemporalLabelFinalizerError(f"{description} row is malformed")
        rows.append(value)
    return rows


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _aware_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.utcoffset() is not None


def _validate_context_message(value: Any) -> tuple[str, int]:
    if not isinstance(value, dict) or set(value) != _CONTEXT_MESSAGE_KEYS:
        raise GmailTemporalLabelFinalizerError("label queue context is malformed")
    message_id = value.get("message_id")
    position = value.get("relative_position")
    text = value.get("text")
    source_count = value.get("source_char_count")
    emitted_count = value.get("emitted_char_count")
    truncated = value.get("text_truncated_after")
    if (
        not isinstance(message_id, str)
        or _MESSAGE_ID_PATTERN.fullmatch(message_id) is None
        or not isinstance(position, int)
        or isinstance(position, bool)
        or position not in {-2, -1, 1, 2}
        or not _aware_timestamp(value.get("message_internal_at"))
        or not isinstance(text, str)
        or not _nonnegative_int(source_count)
        or not _nonnegative_int(emitted_count)
        or emitted_count != len(text)
        or source_count < emitted_count
        or not isinstance(truncated, bool)
        or truncated != (source_count > emitted_count)
    ):
        raise GmailTemporalLabelFinalizerError("label queue context is invalid")
    return message_id, position


def _validate_source_queue(rows: list[dict[str, Any]]) -> None:
    sample_ids: set[str] = set()
    thread_ids: set[str] = set()
    context_ids: set[str] = set()
    for row in rows:
        if (
            set(row) != _QUEUE_KEYS
            or row.get("version") != LABEL_QUEUE_VERSION
            or row.get("context_is_label_only") is not True
            or row.get("label_status") != "unlabeled"
            or row.get("expected_material") is not None
            or row.get("expected_filter") is not None
            or row.get("hard_negative") is not None
            or row.get("semantic_units") != []
            or row.get("critical_error") is not None
            or row.get("notes") is not None
        ):
            raise GmailTemporalLabelFinalizerError(
                "source label queue schema is invalid"
            )
        sample_id = row.get("sample_id")
        thread_id = row.get("thread_id")
        if (
            not isinstance(sample_id, str)
            or _SAMPLE_ID_PATTERN.fullmatch(sample_id) is None
            or sample_id in sample_ids
            or not isinstance(thread_id, str)
            or _THREAD_ID_PATTERN.fullmatch(thread_id) is None
            or thread_id in thread_ids
        ):
            raise GmailTemporalLabelFinalizerError(
                "source label queue identity is invalid"
            )
        sample_ids.add(sample_id)
        thread_ids.add(thread_id)
        target = row.get("target")
        if not isinstance(target, dict) or set(target) != _TARGET_KEYS:
            raise GmailTemporalLabelFinalizerError("label queue target is malformed")
        text = target.get("text")
        source_count = target.get("source_char_count")
        emitted_count = target.get("emitted_char_count")
        digest = target.get("sanitized_text_sha256")
        if (
            not _aware_timestamp(target.get("message_internal_at"))
            or not isinstance(text, str)
            or not text
            or not _nonnegative_int(source_count)
            or not _nonnegative_int(emitted_count)
            or source_count != emitted_count
            or emitted_count != len(text)
            or not isinstance(digest, str)
            or digest != _sha256_bytes(text.encode("utf-8"))
            or not isinstance(target.get("body_truncation_status"), str)
            or not target["body_truncation_status"]
        ):
            raise GmailTemporalLabelFinalizerError("label queue target is invalid")
        context = row.get("thread_context")
        if not isinstance(context, dict) or set(context) != _CONTEXT_KEYS:
            raise GmailTemporalLabelFinalizerError("label queue context is malformed")
        count_fields = (
            "prior_available",
            "prior_included",
            "prior_omitted",
            "later_available",
            "later_included",
            "later_omitted",
            "source_omitted_before_count",
            "source_truncated_message_count",
        )
        if any(not _nonnegative_int(context.get(field)) for field in count_fields):
            raise GmailTemporalLabelFinalizerError("label queue context is invalid")
        messages = context.get("messages")
        if not isinstance(messages, list):
            raise GmailTemporalLabelFinalizerError("label queue context is invalid")
        identities = [_validate_context_message(item) for item in messages]
        message_ids = [item[0] for item in identities]
        positions = [item[1] for item in identities]
        prior = sum(position < 0 for position in positions)
        later = sum(position > 0 for position in positions)
        if (
            positions != sorted(positions)
            or len(message_ids) != len(set(message_ids))
            or context_ids.intersection(message_ids)
            or context["prior_included"] != prior
            or context["later_included"] != later
            or context["prior_available"] - prior != context["prior_omitted"]
            or context["later_available"] - later != context["later_omitted"]
        ):
            raise GmailTemporalLabelFinalizerError(
                "label queue context is inconsistent"
            )
        context_ids.update(message_ids)


def _canonical_jsonl_rows_allow_empty(
    raw: bytes,
    *,
    description: str,
) -> list[dict[str, Any]]:
    if not raw:
        return []
    rows = _load_jsonl(raw, description=description)
    if _jsonl_bytes(rows) != raw:
        raise GmailTemporalLabelFinalizerError(f"{description} is not canonical JSONL")
    return rows


def _authority_sample_rows(
    raw: bytes,
    *,
    description: str,
    queue_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = _canonical_jsonl_rows_allow_empty(raw, description=description)
    if len(rows) != len(queue_rows):
        raise GmailTemporalLabelFinalizerError(
            "release evaluation authority is unscorable"
        )
    seen: set[str] = set()
    for row, queue in zip(rows, queue_rows, strict=True):
        sample_id = row.get("sample_id")
        stratum = row.get("stratum")
        preparation = row.get("preparation")
        source_sha256 = row.get("source_sha256")
        request_fingerprints = (
            preparation.get("request_fingerprints")
            if isinstance(preparation, dict)
            else None
        )
        page_count = (
            preparation.get("page_count") if isinstance(preparation, dict) else None
        )
        if (
            row.get("version") != SAMPLE_VERSION
            or sample_id != queue["sample_id"]
            or sample_id in seen
            or row.get("thread_id") != queue["thread_id"]
            or row.get("message_internal_at") != queue["target"]["message_internal_at"]
            or row.get("text") != queue["target"]["text"]
            or row.get("sanitized_text_sha256")
            != queue["target"]["sanitized_text_sha256"]
            or not isinstance(source_sha256, str)
            or _SHA256_PATTERN.fullmatch(source_sha256) is None
            or not isinstance(stratum, str)
            or stratum not in _STRATUM_ADMISSION
            or not isinstance(row.get("expressions"), list)
            or not isinstance(row.get("mentions"), list)
            or not isinstance(row.get("leads"), list)
            or not isinstance(preparation, dict)
            or preparation.get("admission_basis") != _STRATUM_ADMISSION[stratum]
            or not isinstance(request_fingerprints, list)
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"gtrq_[0-9a-f]{64}", item) is None
                for item in request_fingerprints
            )
            or len(request_fingerprints) != len(set(request_fingerprints))
            or not isinstance(page_count, int)
            or isinstance(page_count, bool)
            or page_count != len(request_fingerprints)
            or not _nonempty_string(row.get("analysis_fingerprint"))
            or not _nonempty_string(row.get("batch_plan_fingerprint"))
            or row.get("routable") is not False
        ):
            raise GmailTemporalLabelFinalizerError(
                "release evaluation authority is unscorable"
            )
        seen.add(str(sample_id))
    return rows


def _authority_binding_rows(
    raw: bytes,
    *,
    description: str,
    samples: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = _canonical_jsonl_rows_allow_empty(raw, description=description)
    if len(rows) != len(samples):
        raise GmailTemporalLabelFinalizerError(
            "release evaluation authority is unscorable"
        )
    seen: set[str] = set()
    for row, sample in zip(rows, samples, strict=True):
        sample_id = row.get("sample_id")
        source_sha256 = row.get("source_sha256")
        if (
            row.get("version") != BINDING_VERSION
            or sample_id != sample["sample_id"]
            or sample_id in seen
            or not isinstance(source_sha256, str)
            or _SHA256_PATTERN.fullmatch(source_sha256) is None
            or source_sha256 != sample["source_sha256"]
            or row.get("analysis_fingerprint") != sample["analysis_fingerprint"]
            or row.get("batch_plan_fingerprint") != sample["batch_plan_fingerprint"]
            or row.get("routable") is not False
        ):
            raise GmailTemporalLabelFinalizerError(
                "release evaluation authority is unscorable"
            )
        seen.add(str(sample_id))
    return rows


def _validate_request_rows(
    raw: bytes,
    *,
    description: str,
    samples: list[dict[str, Any]],
    manifest_count: int,
) -> set[str]:
    rows = _canonical_jsonl_rows_allow_empty(raw, description=description)
    if (
        not isinstance(manifest_count, int)
        or isinstance(manifest_count, bool)
        or manifest_count < 0
    ):
        raise GmailTemporalLabelFinalizerError(
            "release verifier request count is invalid"
        )
    expected = [
        (str(sample["sample_id"]), str(request_fingerprint))
        for sample in samples
        for request_fingerprint in sample["preparation"]["request_fingerprints"]
    ]
    if len(rows) != manifest_count or len(rows) != len(expected):
        raise GmailTemporalLabelFinalizerError(
            "release verifier request authority is incomplete"
        )
    seen: set[str] = set()
    actual: list[tuple[str, str]] = []
    for row in rows:
        sample_id = row.get("sample_id")
        request_fingerprint = row.get("request_fingerprint")
        candidate_count = row.get("candidate_count")
        payload = row.get("payload")
        if (
            set(row) != _REQUEST_KEYS
            or row.get("version") != REQUEST_VERSION
            or not isinstance(sample_id, str)
            or not isinstance(request_fingerprint, str)
            or re.fullmatch(r"gtrq_[0-9a-f]{64}", request_fingerprint) is None
            or request_fingerprint in seen
            or any(
                not _nonempty_string(row.get(field))
                for field in (
                    "batch_fingerprint",
                    "frontier_fingerprint",
                    "page_plan_fingerprint",
                    "page_fingerprint",
                )
            )
            or not isinstance(candidate_count, int)
            or isinstance(candidate_count, bool)
            or candidate_count < 1
            or not isinstance(payload, dict)
            or payload.get("request_fingerprint") != request_fingerprint
            or row.get("routable") is not False
        ):
            raise GmailTemporalLabelFinalizerError(
                "release verifier request authority is unscorable"
            )
        seen.add(request_fingerprint)
        actual.append((sample_id, request_fingerprint))
    if actual != expected:
        raise GmailTemporalLabelFinalizerError(
            "release verifier request authority does not match samples"
        )
    return seen


def _validate_release_authority(
    artifacts: Mapping[str, bytes],
    *,
    primary_queue: list[dict[str, Any]],
    challenge_queue: list[dict[str, Any]],
    reserve_count: int,
    primary_request_count: int,
    challenge_request_count: int,
) -> None:
    primary_samples = _authority_sample_rows(
        artifacts["evaluation-authority/primary-samples.jsonl"],
        description="primary evaluation samples",
        queue_rows=primary_queue,
    )
    challenge_samples = _authority_sample_rows(
        artifacts["evaluation-authority/challenge-samples.jsonl"],
        description="challenge evaluation samples",
        queue_rows=challenge_queue,
    )
    _authority_binding_rows(
        artifacts["evaluation-authority/primary-bindings.jsonl"],
        description="primary evaluation bindings",
        samples=primary_samples,
    )
    _authority_binding_rows(
        artifacts["evaluation-authority/challenge-bindings.jsonl"],
        description="challenge evaluation bindings",
        samples=challenge_samples,
    )
    primary_ids = {str(row["sample_id"]) for row in primary_samples}
    challenge_ids = {str(row["sample_id"]) for row in challenge_samples}
    primary_threads = {str(row["thread_id"]) for row in primary_queue}
    challenge_threads = {str(row["thread_id"]) for row in challenge_queue}
    if primary_ids.intersection(challenge_ids) or primary_threads.intersection(
        challenge_threads
    ):
        raise GmailTemporalLabelFinalizerError(
            "release evaluation authority is not cohort-disjoint"
        )
    primary_request_ids = _validate_request_rows(
        artifacts["evaluation-authority/primary-requests.jsonl"],
        description="primary verifier requests",
        samples=primary_samples,
        manifest_count=primary_request_count,
    )
    challenge_request_ids = _validate_request_rows(
        artifacts["evaluation-authority/challenge-requests.jsonl"],
        description="challenge verifier requests",
        samples=challenge_samples,
        manifest_count=challenge_request_count,
    )
    if primary_request_ids.intersection(challenge_request_ids):
        raise GmailTemporalLabelFinalizerError(
            "release verifier request authority is not cohort-unique"
        )
    reserve_order = _canonical_jsonl_rows_allow_empty(
        artifacts["sealed-reserve/order.jsonl"],
        description="sealed reserve order",
    )
    reserve_bindings = _canonical_jsonl_rows_allow_empty(
        artifacts["sealed-reserve/bindings.jsonl"],
        description="sealed reserve bindings",
    )
    if len(reserve_order) != reserve_count or len(reserve_bindings) != reserve_count:
        raise GmailTemporalLabelFinalizerError("sealed reserve authority is unscorable")
    reserve_ids: list[str] = []
    reserve_threads: set[str] = set()
    for position, row in enumerate(reserve_order, start=1):
        sample_id = row.get("sample_id")
        thread_id = row.get("thread_id")
        if (
            row.get("version") != RESERVE_ORDER_VERSION
            or not isinstance(row.get("position"), int)
            or isinstance(row.get("position"), bool)
            or row.get("position") != position
            or not isinstance(sample_id, str)
            or _SAMPLE_ID_PATTERN.fullmatch(sample_id) is None
            or sample_id in reserve_ids
            or not isinstance(thread_id, str)
            or _THREAD_ID_PATTERN.fullmatch(thread_id) is None
            or thread_id in reserve_threads
        ):
            raise GmailTemporalLabelFinalizerError(
                "sealed reserve authority is unscorable"
            )
        reserve_ids.append(sample_id)
        reserve_threads.add(thread_id)
    binding_ids: list[str] = []
    for row in reserve_bindings:
        sample_id = row.get("sample_id")
        if (
            row.get("version") != BINDING_VERSION
            or row.get("selection_status") != "sealed_reserve"
            or not isinstance(sample_id, str)
            or _SAMPLE_ID_PATTERN.fullmatch(sample_id) is None
            or sample_id in binding_ids
        ):
            raise GmailTemporalLabelFinalizerError(
                "sealed reserve authority is unscorable"
            )
        binding_ids.append(sample_id)
    queue_threads = {
        str(row["thread_id"]) for row in (*primary_queue, *challenge_queue)
    }
    if (
        binding_ids != reserve_ids
        or set(reserve_ids).intersection(primary_ids | challenge_ids)
        or reserve_threads.intersection(queue_threads)
    ):
        raise GmailTemporalLabelFinalizerError(
            "sealed reserve authority is not cohort-disjoint"
        )


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_locator(locator: Any, *, target_text: str) -> bytes:
    if not isinstance(locator, dict) or set(locator) != _LOCATOR_KEYS:
        raise GmailTemporalLabelFinalizerError("semantic locator is malformed")
    expression = locator.get("expression")
    subject = locator.get("subject")
    lifecycle = locator.get("lifecycle_mention")
    derived = locator.get("derived")
    if (
        not isinstance(expression, dict)
        or set(expression) != _EXPRESSION_KEYS
        or not isinstance(subject, dict)
        or set(subject) != _SUBJECT_KEYS
        or not isinstance(derived, dict)
        or set(derived) != _DERIVED_KEYS
        or (
            lifecycle is not None
            and (not isinstance(lifecycle, dict) or set(lifecycle) != _LIFECYCLE_KEYS)
        )
    ):
        raise GmailTemporalLabelFinalizerError("semantic locator schema is invalid")
    endpoints = (
        (expression, ("surface", "form", "field")),
        (subject, ("surface", "mention_type", "field")),
    )
    for endpoint, fields in endpoints:
        if any(not _nonempty_string(endpoint.get(field)) for field in fields):
            raise GmailTemporalLabelFinalizerError(
                "semantic locator endpoint is invalid"
            )
        if endpoint["surface"] not in target_text:
            raise GmailTemporalLabelFinalizerError(
                "semantic locator surface is not grounded in the target source"
            )
    if lifecycle is not None:
        if any(
            not _nonempty_string(lifecycle.get(field))
            for field in ("surface", "lifecycle_role", "field")
        ):
            raise GmailTemporalLabelFinalizerError(
                "semantic lifecycle locator is invalid"
            )
        if lifecycle["surface"] not in target_text:
            raise GmailTemporalLabelFinalizerError(
                "semantic lifecycle surface is not grounded in the target source"
            )
    if (
        any(
            not _nonempty_string(derived.get(field))
            for field in ("relation", "kind", "lifecycle")
        )
        or (
            derived.get("normalized_value") is not None
            and not _nonempty_string(derived.get("normalized_value"))
        )
        or not isinstance(derived.get("requires_defer"), bool)
    ):
        raise GmailTemporalLabelFinalizerError("semantic locator derivation is invalid")
    return _canonical_json(locator)


def _validate_semantic_units(
    raw_units: Any,
    *,
    target_text: str,
) -> dict[str, int]:
    if not isinstance(raw_units, list):
        raise GmailTemporalLabelFinalizerError("semantic units must be a list")
    unit_ids: set[str] = set()
    locator_owners: dict[bytes, tuple[str, str]] = {}
    counts = {
        "units": 0,
        "members": 0,
        "supported_members": 0,
        "uncertain_members": 0,
        "exact_alternatives": 0,
        "partial_alternatives": 0,
    }
    for unit in raw_units:
        if not isinstance(unit, dict) or set(unit) != _UNIT_KEYS:
            raise GmailTemporalLabelFinalizerError("semantic unit is malformed")
        unit_id = unit.get("unit_id")
        members = unit.get("members")
        if unit.get("baseline_frontier_grade") != BASELINE_GRADE_PLACEHOLDER:
            raise GmailTemporalLabelFinalizerError(
                "baseline frontier grade must use the evaluator-owned placeholder"
            )
        if (
            not _nonempty_string(unit_id)
            or unit_id in unit_ids
            or not _nonempty_string(unit.get("truth"))
            or not isinstance(members, list)
            or not members
        ):
            raise GmailTemporalLabelFinalizerError("semantic unit identity is invalid")
        unit_ids.add(unit_id)
        member_ids: set[str] = set()
        counts["units"] += 1
        for member in members:
            if not isinstance(member, dict) or set(member) != _MEMBER_KEYS:
                raise GmailTemporalLabelFinalizerError("semantic member is malformed")
            member_id = member.get("member_id")
            verdict = member.get("expected_verdict")
            alternatives = member.get("alternatives")
            if member.get("baseline_frontier_grade") != BASELINE_GRADE_PLACEHOLDER:
                raise GmailTemporalLabelFinalizerError(
                    "baseline frontier grade must use the evaluator-owned placeholder"
                )
            if (
                not _nonempty_string(member_id)
                or member_id in member_ids
                or verdict not in _VERDICTS
                or not isinstance(alternatives, list)
                or not alternatives
            ):
                raise GmailTemporalLabelFinalizerError(
                    "semantic member identity is invalid"
                )
            member_ids.add(member_id)
            counts["members"] += 1
            counts[f"{verdict}_members"] += 1
            has_exact_expected = False
            for alternative in alternatives:
                if (
                    not isinstance(alternative, dict)
                    or set(alternative) != _ALTERNATIVE_KEYS
                    or alternative.get("quality") not in {"exact", "partial"}
                    or alternative.get("expected_verdict") not in _VERDICTS
                ):
                    raise GmailTemporalLabelFinalizerError(
                        "semantic alternative is malformed"
                    )
                quality = alternative["quality"]
                alternative_verdict = alternative["expected_verdict"]
                if (
                    quality == "partial"
                    and alternative_verdict != "uncertain"
                    or quality == "exact"
                    and alternative_verdict != verdict
                ):
                    raise GmailTemporalLabelFinalizerError(
                        "semantic alternative calibration is inconsistent"
                    )
                has_exact_expected |= (
                    quality == "exact" and alternative_verdict == verdict
                )
                locator_key = _validate_locator(
                    alternative.get("locator"),
                    target_text=target_text,
                )
                owner = (unit_id, member_id)
                if locator_key in locator_owners:
                    raise GmailTemporalLabelFinalizerError(
                        "semantic locator is duplicated within the record"
                    )
                locator_owners[locator_key] = owner
                counts[f"{quality}_alternatives"] += 1
            if verdict == "supported" and not has_exact_expected:
                raise GmailTemporalLabelFinalizerError(
                    "supported semantic member has no exact supported alternative"
                )
    return counts


def _validate_completed_labels(
    source_rows: list[dict[str, Any]],
    completed_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if len(completed_rows) != len(source_rows):
        raise GmailTemporalLabelFinalizerError(
            "completed labels do not exactly cover the primary queue"
        )
    counts = {
        "records": len(source_rows),
        "threads": 0,
        "expected_material": 0,
        "expected_suppressed": 0,
        "labeled_hard_negatives": 0,
        "units": 0,
        "members": 0,
        "supported_members": 0,
        "uncertain_members": 0,
        "exact_alternatives": 0,
        "partial_alternatives": 0,
    }
    threads: set[str] = set()
    output: list[dict[str, Any]] = []
    for source, completed in zip(source_rows, completed_rows, strict=True):
        if set(completed) != _QUEUE_KEYS:
            raise GmailTemporalLabelFinalizerError(
                "completed label row schema is invalid"
            )
        if completed.get("sample_id") != source["sample_id"]:
            raise GmailTemporalLabelFinalizerError(
                "completed label order or coverage changed"
            )
        if any(
            _canonical_json(completed.get(field)) != _canonical_json(source[field])
            for field in _QUEUE_KEYS - _LABEL_FIELDS
        ):
            raise GmailTemporalLabelFinalizerError(
                "completed labels changed an immutable source field"
            )
        expected_material = completed.get("expected_material")
        expected_filter = completed.get("expected_filter")
        hard_negative = completed.get("hard_negative")
        critical_error = completed.get("critical_error")
        notes = completed.get("notes")
        if (
            completed.get("label_status") != "labeled"
            or not isinstance(expected_material, bool)
            or expected_filter not in _FILTERS
            or not isinstance(hard_negative, bool)
            or critical_error != "none"
            or (notes is not None and not _nonempty_string(notes))
        ):
            raise GmailTemporalLabelFinalizerError(
                "completed label fields are incomplete or invalid"
            )
        if expected_material != (expected_filter == "should_admit"):
            raise GmailTemporalLabelFinalizerError(
                "materiality and filter labels are inconsistent"
            )
        units = completed.get("semantic_units")
        if expected_material:
            if hard_negative or not isinstance(units, list) or not units:
                raise GmailTemporalLabelFinalizerError(
                    "useful record has no semantic gold"
                )
            counts["expected_material"] += 1
        else:
            if units != [] or (hard_negative and expected_filter != "should_suppress"):
                raise GmailTemporalLabelFinalizerError(
                    "negative record has semantic gold or invalid filtering"
                )
            counts["expected_suppressed"] += 1
            counts["labeled_hard_negatives"] += int(hard_negative)
        semantic_counts = _validate_semantic_units(
            units,
            target_text=source["target"]["text"],
        )
        for field, value in semantic_counts.items():
            counts[field] += value
        threads.add(source["thread_id"])
        output.append(dict(completed))
    counts["threads"] = len(threads)
    return output, counts


def _primary_label_data_gate(counts: Mapping[str, int]) -> bool:
    """Natural-mail labels support operability, not a forced recall denominator."""

    return (
        counts["labeled_hard_negatives"] >= PRIMARY_MIN_LABELED_HARD_NEGATIVES
    )


def _challenge_label_data_gate(counts: Mapping[str, int] | None) -> bool:
    """Balanced challenge labels support the separate stress-recall estimand."""

    return bool(
        counts is not None
        and counts["expected_material"] >= CHALLENGE_MIN_EXPECTED_MATERIAL_RECORDS
        and counts["members"] >= CHALLENGE_MIN_SEMANTIC_MEMBERS
        and counts["supported_members"] >= CHALLENGE_MIN_SUPPORTED_MEMBERS
        and counts["labeled_hard_negatives"]
        >= CHALLENGE_MIN_LABELED_HARD_NEGATIVES
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


def _publish_frozen(output_root: Path, artifacts: Mapping[str, bytes]) -> None:
    root = Path(output_root)
    parent = root.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise GmailTemporalLabelFinalizerError("gold output parent is unsafe")
    parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    if root.exists() or root.is_symlink():
        raise GmailTemporalLabelFinalizerError("gold output already exists")
    temporary = Path(tempfile.mkdtemp(prefix=f".{root.name}-", dir=parent))
    os.chmod(temporary, PRIVATE_DIRECTORY_MODE)
    try:
        for name, payload in sorted(artifacts.items()):
            relative = Path(_safe_artifact_name(name))
            artifact = temporary / relative
            artifact.parent.mkdir(
                parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE
            )
            os.chmod(artifact.parent, PRIVATE_DIRECTORY_MODE)
            _write_private_new(artifact, payload)
        os.replace(temporary, root)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _authenticated_gold_manifest_bytes(
    manifest: Mapping[str, Any],
    *,
    key: bytes,
) -> bytes:
    unsigned = _canonical_json(dict(manifest))
    authenticator = hmac.new(
        key,
        GOLD_MANIFEST_DOMAIN + unsigned,
        hashlib.sha256,
    ).hexdigest()
    return (
        _canonical_json({**dict(manifest), "manifest_hmac_sha256": authenticator})
        + b"\n"
    )


_EXTERNAL_PLAN_KEYS = {
    "version",
    "runner_sha256",
    "phase",
    "run_ordinal",
    "cohort",
    "provider",
    "model",
    "reasoning_effort",
    "batch_size",
    "inputs",
    "units",
    "ephemeral_execution",
    "restricted_execution",
    "local_model_used",
    "private_content_printed",
    "routable",
    "logical_run_id",
    "created_at",
    "plan_hmac_sha256",
}
_EXTERNAL_PLAN_UNIT_KEYS = {
    "unit_id",
    "cohort",
    "ordinal",
    "item_ids",
    "item_sha256",
    "request_sha256",
}
_EXTERNAL_LABEL_INPUT_KEYS = {
    "source_holdout_manifest_sha256",
    "source_primary_label_queue_sha256",
    "source_challenge_label_queue_sha256",
    "label_time_basis",
    "source_only_labeling",
    "pipeline_predictions_inspected",
    "internal_evaluation_artifacts_inspected",
    "verifier_outputs_available_during_labeling",
}
_EXTERNAL_CALL_START_KEYS = {
    "version",
    "logical_run_id",
    "phase",
    "cohort",
    "unit_id",
    "attempt_ordinal",
    "invocation_id",
    "provider",
    "model",
    "reasoning_effort",
    "started_at",
    "request_sha256",
    "external_call_started",
    "ephemeral_execution",
    "restricted_execution",
    "local_model_used",
    "private_content_printed",
    "routable",
    "start_hmac_sha256",
}
_EXTERNAL_CALL_RECEIPT_KEYS = {
    "version",
    "logical_run_id",
    "phase",
    "cohort",
    "unit_id",
    "attempt_ordinal",
    "invocation_id",
    "provider",
    "model",
    "reasoning_effort",
    "started_at",
    "completed_at",
    "request_sha256",
    "response_sha256",
    "status",
    "error_type",
    "external_call_started",
    "ephemeral_execution",
    "restricted_execution",
    "local_model_used",
    "private_content_printed",
    "routable",
    "receipt_hmac_sha256",
}


def _parse_aware_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _verify_external_signature(
    value: Mapping[str, Any],
    *,
    key: bytes,
    domain: bytes,
    signature_field: str,
) -> bool:
    supplied = value.get(signature_field)
    if not isinstance(supplied, str) or _SHA256_PATTERN.fullmatch(supplied) is None:
        return False
    unsigned = dict(value)
    unsigned.pop(signature_field, None)
    expected = hmac.new(
        key,
        domain + _canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(supplied, expected)


def _load_private_canonical_json(path: Path, *, description: str) -> tuple[dict[str, Any], bytes]:
    raw = _private_regular_file(path, description=description)
    value = _parse_json(raw, description=description)
    if not isinstance(value, dict) or raw != _canonical_json(value) + b"\n":
        raise GmailTemporalLabelFinalizerError(f"{description} is not canonical")
    return value, raw


def _hash_external_ordered_set(
    domain: str,
    rows: Sequence[Mapping[str, Any]],
) -> str:
    return _sha256_bytes(
        domain.encode("utf-8") + b"\0" + _canonical_json(list(rows))
    )


def _external_unit_id(
    cohort: str,
    ordinal: int,
    item_sha256: Sequence[str],
) -> str:
    material = {
        "phase": "labels",
        "cohort": cohort,
        "ordinal": ordinal,
        "item_sha256": list(item_sha256),
    }
    return "gthxu_" + _sha256_bytes(_canonical_json(material))


def _verify_retained_label_call_evidence(
    run_root: Path,
    *,
    key: bytes,
    source_holdout_manifest_sha256: str,
    source_primary_label_queue_raw: bytes,
    source_challenge_label_queue_raw: bytes,
    completed_labels_raw: bytes,
    completed_challenge_labels_raw: bytes,
) -> dict[str, Any]:
    """Replay the authenticated label ledger and derive its chronology.

    This proves facts about the retained run only.  It deliberately makes no
    claim that activity outside this authenticated authority did not occur.
    """

    _private_directory(run_root, description="label evidence root")
    plan, plan_raw = _load_private_canonical_json(
        run_root / "plan.json",
        description="label evidence plan",
    )
    inputs = plan.get("inputs")
    units = plan.get("units")
    plan_created_at = _parse_aware_datetime(plan.get("created_at"))
    if (
        set(plan) != _EXTERNAL_PLAN_KEYS
        or not _verify_external_signature(
            plan,
            key=key,
            domain=EXTERNAL_PLAN_DOMAIN,
            signature_field="plan_hmac_sha256",
        )
        or plan.get("version") != EXTERNAL_PLAN_VERSION
        or plan.get("phase") != "labels"
        or plan.get("run_ordinal") is not None
        or plan.get("cohort") != "primary_and_challenge"
        or plan.get("provider") != EXTERNAL_PROVIDER
        or plan.get("model") != LABEL_AUTHORITY_MODEL
        or plan.get("reasoning_effort") != LABEL_AUTHORITY_REASONING_EFFORT
        or not isinstance(plan.get("runner_sha256"), str)
        or _SHA256_PATTERN.fullmatch(str(plan["runner_sha256"])) is None
        or not isinstance(plan.get("batch_size"), int)
        or isinstance(plan.get("batch_size"), bool)
        or not 1 <= int(plan["batch_size"]) <= 8
        or not isinstance(inputs, dict)
        or set(inputs) != _EXTERNAL_LABEL_INPUT_KEYS
        or inputs.get("source_holdout_manifest_sha256")
        != source_holdout_manifest_sha256
        or inputs.get("source_primary_label_queue_sha256")
        != _sha256_bytes(source_primary_label_queue_raw)
        or inputs.get("source_challenge_label_queue_sha256")
        != _sha256_bytes(source_challenge_label_queue_raw)
        or inputs.get("label_time_basis") != LABEL_TIME_BASIS
        or inputs.get("source_only_labeling") is not True
        or inputs.get("pipeline_predictions_inspected") is not False
        or inputs.get("internal_evaluation_artifacts_inspected") is not False
        or inputs.get("verifier_outputs_available_during_labeling") is not False
        or not isinstance(units, list)
        or not units
        or _LOGICAL_RUN_ID_PATTERN.fullmatch(str(plan.get("logical_run_id"))) is None
        or plan_created_at is None
        or plan.get("ephemeral_execution") is not True
        or plan.get("restricted_execution") is not True
        or plan.get("local_model_used") is not False
        or plan.get("private_content_printed") is not False
        or plan.get("routable") is not False
    ):
        raise GmailTemporalLabelFinalizerError("label evidence plan is invalid")

    source_rows: dict[str, list[dict[str, Any]]] = {
        "primary": [],
        "challenge": [],
    }
    unit_requests: dict[str, tuple[dict[str, Any], bytes]] = {}
    seen_challenge = False
    for expected_ordinal, unit in enumerate(units, start=1):
        if not isinstance(unit, dict) or set(unit) != _EXTERNAL_PLAN_UNIT_KEYS:
            raise GmailTemporalLabelFinalizerError("label evidence plan unit is invalid")
        cohort = unit.get("cohort")
        item_ids = unit.get("item_ids")
        item_hashes = unit.get("item_sha256")
        unit_id = unit.get("unit_id")
        if cohort == "challenge":
            seen_challenge = True
        if (
            cohort not in {"primary", "challenge"}
            or (seen_challenge and cohort == "primary")
            or unit.get("ordinal") != expected_ordinal
            or not isinstance(item_ids, list)
            or not item_ids
            or len(item_ids) > int(plan["batch_size"])
            or not isinstance(item_hashes, list)
            or len(item_hashes) != len(item_ids)
            or any(not isinstance(value, str) or not value for value in item_ids)
            or any(
                not isinstance(value, str)
                or _SHA256_PATTERN.fullmatch(value) is None
                for value in item_hashes
            )
            or not isinstance(unit_id, str)
            or _EXTERNAL_UNIT_ID_PATTERN.fullmatch(unit_id) is None
            or unit_id != _external_unit_id(cohort, expected_ordinal, item_hashes)
            or not isinstance(unit.get("request_sha256"), str)
            or _SHA256_PATTERN.fullmatch(str(unit["request_sha256"])) is None
        ):
            raise GmailTemporalLabelFinalizerError("label evidence plan unit is invalid")
        request_path = run_root / "calls" / unit_id
        _private_directory(request_path, description="label evidence unit")
        attempt_entries = sorted(request_path.iterdir(), key=lambda entry: entry.name)
        if not attempt_entries:
            raise GmailTemporalLabelFinalizerError("label evidence attempt is missing")
        for attempt_entry in attempt_entries:
            _private_directory(
                attempt_entry,
                description="label evidence attempt",
            )
        first_request_raw = _private_regular_file(
            attempt_entries[0] / "request.json",
            description="label evidence request",
        )
        if _sha256_bytes(first_request_raw) != unit["request_sha256"]:
            raise GmailTemporalLabelFinalizerError("label evidence request is stale")
        request = _parse_json(first_request_raw, description="label evidence request")
        if (
            not isinstance(request, dict)
            or first_request_raw != _canonical_json(request) + b"\n"
            or set(request)
            != {
                "version",
                "phase",
                "contract",
                "label_time_basis",
                "later_context_policy",
                "records",
            }
            or request.get("version") != EXTERNAL_CALL_REQUEST_VERSION
            or request.get("phase") != "labels"
            or not isinstance(request.get("contract"), str)
            or not request["contract"]
            or request.get("label_time_basis") != LABEL_TIME_BASIS
            or request.get("later_context_policy") != LATER_CONTEXT_POLICY
            or not isinstance(request.get("records"), list)
            or len(request["records"]) != len(item_ids)
            or any(not isinstance(row, dict) for row in request["records"])
            or [row.get("sample_id") for row in request["records"]] != item_ids
            or [_sha256_bytes(_canonical_json(row)) for row in request["records"]]
            != item_hashes
        ):
            raise GmailTemporalLabelFinalizerError("label evidence request is invalid")
        source_rows[cohort].extend(dict(row) for row in request["records"])
        unit_requests[unit_id] = (request, first_request_raw)

    if (
        _jsonl_bytes(source_rows["primary"]) != source_primary_label_queue_raw
        or _jsonl_bytes(source_rows["challenge"])
        != source_challenge_label_queue_raw
    ):
        raise GmailTemporalLabelFinalizerError(
            "label evidence requests do not cover the frozen source queues"
        )

    calls_root = run_root / "calls"
    _private_directory(calls_root, description="label evidence calls")
    if {entry.name for entry in calls_root.iterdir()} != {
        str(unit["unit_id"]) for unit in units
    }:
        raise GmailTemporalLabelFinalizerError("label evidence call inventory is invalid")

    attempts: list[dict[str, Any]] = []
    successful_completed: dict[str, list[dict[str, Any]]] = {
        "primary": [],
        "challenge": [],
    }
    seen_invocations: set[str] = set()
    for unit in units:
        unit_id = str(unit["unit_id"])
        cohort = str(unit["cohort"])
        request, expected_request_raw = unit_requests[unit_id]
        success_count = 0
        attempt_entries = sorted(
            (calls_root / unit_id).iterdir(),
            key=lambda entry: entry.name,
        )
        for expected_attempt_ordinal, attempt_path in enumerate(
            attempt_entries,
            start=1,
        ):
            _private_directory(attempt_path, description="label evidence attempt")
            invocation_suffix = attempt_path.name.removeprefix(
                f"attempt-{expected_attempt_ordinal:02d}-"
            )
            if (
                invocation_suffix == attempt_path.name
                or _LABEL_INVOCATION_ID_PATTERN.fullmatch(invocation_suffix) is None
            ):
                raise GmailTemporalLabelFinalizerError(
                    "label evidence attempt identity is invalid"
                )
            names = {entry.name for entry in attempt_path.iterdir()}
            if "request.json" not in names or not names <= {
                "request.json",
                "started.json",
                "response.json",
                "receipt.json",
            }:
                raise GmailTemporalLabelFinalizerError(
                    "label evidence attempt inventory is invalid"
                )
            request_raw = _private_regular_file(
                attempt_path / "request.json",
                description="label evidence request",
            )
            if request_raw != expected_request_raw:
                raise GmailTemporalLabelFinalizerError("label evidence request is stale")
            if "started.json" not in names:
                if names != {"request.json"}:
                    raise GmailTemporalLabelFinalizerError(
                        "label evidence attempt chronology is invalid"
                    )
                continue
            if "receipt.json" not in names:
                raise GmailTemporalLabelFinalizerError(
                    "label evidence attempt is incomplete"
                )
            start, _start_raw = _load_private_canonical_json(
                attempt_path / "started.json",
                description="label evidence start",
            )
            started_at = _parse_aware_datetime(start.get("started_at"))
            if (
                set(start) != _EXTERNAL_CALL_START_KEYS
                or not _verify_external_signature(
                    start,
                    key=key,
                    domain=EXTERNAL_CALL_START_DOMAIN,
                    signature_field="start_hmac_sha256",
                )
                or start.get("version") != EXTERNAL_CALL_START_VERSION
                or start.get("logical_run_id") != plan["logical_run_id"]
                or start.get("phase") != "labels"
                or start.get("cohort") != cohort
                or start.get("unit_id") != unit_id
                or start.get("attempt_ordinal") != expected_attempt_ordinal
                or start.get("invocation_id") != invocation_suffix
                or start.get("provider") != EXTERNAL_PROVIDER
                or start.get("model") != LABEL_AUTHORITY_MODEL
                or start.get("reasoning_effort")
                != LABEL_AUTHORITY_REASONING_EFFORT
                or started_at is None
                or started_at < plan_created_at
                or start.get("request_sha256") != _sha256_bytes(request_raw)
                or start.get("external_call_started") is not True
                or start.get("ephemeral_execution") is not True
                or start.get("restricted_execution") is not True
                or start.get("local_model_used") is not False
                or start.get("private_content_printed") is not False
                or start.get("routable") is not False
                or invocation_suffix in seen_invocations
            ):
                raise GmailTemporalLabelFinalizerError(
                    "label evidence start is invalid"
                )
            seen_invocations.add(invocation_suffix)
            response_raw = (
                _private_regular_file(
                    attempt_path / "response.json",
                    description="label evidence response",
                )
                if "response.json" in names
                else None
            )
            receipt, receipt_raw = _load_private_canonical_json(
                attempt_path / "receipt.json",
                description="label evidence receipt",
            )
            completed_at = _parse_aware_datetime(receipt.get("completed_at"))
            common_receipt_fields = _EXTERNAL_CALL_START_KEYS - {
                "version",
                "start_hmac_sha256",
            }
            response_sha256 = (
                _sha256_bytes(response_raw) if response_raw is not None else None
            )
            status = receipt.get("status")
            if (
                set(receipt) != _EXTERNAL_CALL_RECEIPT_KEYS
                or not _verify_external_signature(
                    receipt,
                    key=key,
                    domain=EXTERNAL_CALL_RECEIPT_DOMAIN,
                    signature_field="receipt_hmac_sha256",
                )
                or receipt.get("version") != EXTERNAL_CALL_RECEIPT_VERSION
                or any(
                    receipt.get(field) != start.get(field)
                    for field in common_receipt_fields
                )
                or completed_at is None
                or completed_at < started_at
                or receipt.get("response_sha256") != response_sha256
                or status
                not in {"success", "failed", "invalid_response", "interrupted"}
                or (
                    status == "success"
                    and (response_raw is None or receipt.get("error_type") is not None)
                )
                or (
                    status != "success"
                    and not isinstance(receipt.get("error_type"), str)
                )
            ):
                raise GmailTemporalLabelFinalizerError(
                    "label evidence receipt is invalid"
                )
            if response_raw is not None:
                response = _parse_json(
                    response_raw,
                    description="label evidence response",
                )
                if (
                    not isinstance(response, dict)
                    or response_raw != _canonical_json(response) + b"\n"
                ):
                    raise GmailTemporalLabelFinalizerError(
                        "label evidence response is not canonical"
                    )
            else:
                response = None
            if status == "success":
                labels = response.get("labels") if isinstance(response, dict) else None
                records = request["records"]
                expected_label_keys = {"sample_id", *_LABEL_FIELDS}
                if (
                    set(response) != {"version", "labels"}
                    or response.get("version")
                    != "gmail_temporal_holdout_source_label_response_v1"
                    or not isinstance(labels, list)
                    or len(labels) != len(records)
                    or any(
                        not isinstance(label, dict)
                        or set(label) != expected_label_keys
                        or label.get("sample_id") != source.get("sample_id")
                        for source, label in zip(records, labels, strict=True)
                    )
                ):
                    raise GmailTemporalLabelFinalizerError(
                        "successful label evidence response is invalid"
                    )
                success_count += 1
                successful_completed[cohort].extend(
                    {
                        **source,
                        **{field: label[field] for field in _LABEL_FIELDS},
                    }
                    for source, label in zip(records, labels, strict=True)
                )
            attempts.append(
                {
                    "invocation_id": invocation_suffix,
                    "request_raw": request_raw,
                    "response_raw": response_raw,
                    "receipt_raw": receipt_raw,
                    "status": status,
                    "started_at": start["started_at"],
                    "completed_at": receipt["completed_at"],
                }
            )
        if success_count != 1:
            raise GmailTemporalLabelFinalizerError(
                "label evidence unit does not have exactly one success"
            )

    if (
        not attempts
        or _jsonl_bytes(successful_completed["primary"]) != completed_labels_raw
        or _jsonl_bytes(successful_completed["challenge"])
        != completed_challenge_labels_raw
    ):
        raise GmailTemporalLabelFinalizerError(
            "retained label responses do not reproduce the completed labels"
        )
    request_rows = [
        {
            "invocation_id": attempt["invocation_id"],
            "request_sha256": _sha256_bytes(attempt["request_raw"]),
        }
        for attempt in attempts
    ]
    response_rows = [
        {
            "invocation_id": attempt["invocation_id"],
            "response_sha256": (
                _sha256_bytes(attempt["response_raw"])
                if attempt["response_raw"] is not None
                else None
            ),
            "status": attempt["status"],
        }
        for attempt in attempts
    ]
    receipt_rows = [
        {
            "invocation_id": attempt["invocation_id"],
            "receipt_sha256": _sha256_bytes(attempt["receipt_raw"]),
        }
        for attempt in attempts
    ]
    started_values = [
        _parse_aware_datetime(attempt["started_at"]) for attempt in attempts
    ]
    completed_values = [
        _parse_aware_datetime(attempt["completed_at"]) for attempt in attempts
    ]
    if any(value is None for value in (*started_values, *completed_values)):
        raise GmailTemporalLabelFinalizerError("label evidence chronology is invalid")
    return {
        "logical_run_id": plan["logical_run_id"],
        "plan_sha256": _sha256_bytes(plan_raw),
        "plan_hmac_sha256": plan["plan_hmac_sha256"],
        "started_at": min(value for value in started_values if value is not None).isoformat(),
        "completed_at": max(
            value for value in completed_values if value is not None
        ).isoformat(),
        "invocation_count": len(attempts),
        "invocation_ids": [attempt["invocation_id"] for attempt in attempts],
        "request_set_sha256": _hash_external_ordered_set(
            "external-call-requests-v1",
            request_rows,
        ),
        "response_set_sha256": _hash_external_ordered_set(
            "external-call-responses-v1",
            response_rows,
        ),
        "receipt_set_sha256": _hash_external_ordered_set(
            "external-call-receipts-v1",
            receipt_rows,
        ),
    }


def _load_label_authority_manifest(
    path: Path,
    *,
    key: bytes,
    source_holdout_manifest_sha256: str,
    source_primary_label_queue_sha256: str,
    source_challenge_label_queue_sha256: str,
    completed_labels_sha256: str,
    completed_challenge_labels_sha256: str | None,
    source_primary_label_queue_raw: bytes,
    source_challenge_label_queue_raw: bytes,
    completed_labels_raw: bytes,
    completed_challenge_labels_raw: bytes,
) -> tuple[dict[str, Any], bytes]:
    raw = _private_regular_file(path, description="label authority manifest")
    value = _parse_json(raw, description="label authority manifest")
    if not isinstance(value, dict) or raw != _canonical_json(value) + b"\n":
        raise GmailTemporalLabelFinalizerError(
            "label authority manifest is not canonical"
        )
    authenticator = value.get("manifest_hmac_sha256")
    unsigned = dict(value)
    unsigned.pop("manifest_hmac_sha256", None)
    expected_authenticator = hmac.new(
        key,
        LABEL_AUTHORITY_DOMAIN + _canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()
    expected_keys = {
        "version",
        "logical_run_id",
        "label_plan_sha256",
        "label_plan_hmac_sha256",
        "model",
        "reasoning_effort",
        "execution_surface",
        "ephemeral_execution",
        "local_model_used",
        "source_only_labeling",
        "pipeline_predictions_inspected",
        "internal_evaluation_artifacts_inspected",
        "verifier_outputs_available_during_labeling",
        "labels_sealed_before_verifier_outputs_opened",
        "label_time_basis",
        "source_holdout_manifest_sha256",
        "source_primary_label_queue_sha256",
        "source_challenge_label_queue_sha256",
        "completed_labels_sha256",
        "completed_challenge_labels_sha256",
        "invocation_count",
        "invocation_ids",
        "request_set_sha256",
        "response_set_sha256",
        "receipt_set_sha256",
        "started_at",
        "completed_at",
    }
    invocation_ids = value.get("invocation_ids")
    invocation_count = value.get("invocation_count")
    if (
        set(unsigned) != expected_keys
        or not isinstance(authenticator, str)
        or _SHA256_PATTERN.fullmatch(authenticator) is None
        or not hmac.compare_digest(authenticator, expected_authenticator)
        or value.get("version") != LABEL_AUTHORITY_VERSION
        or _LOGICAL_RUN_ID_PATTERN.fullmatch(str(value.get("logical_run_id"))) is None
        or not isinstance(value.get("label_plan_sha256"), str)
        or _SHA256_PATTERN.fullmatch(str(value["label_plan_sha256"])) is None
        or not isinstance(value.get("label_plan_hmac_sha256"), str)
        or _SHA256_PATTERN.fullmatch(str(value["label_plan_hmac_sha256"])) is None
        or value.get("model") != LABEL_AUTHORITY_MODEL
        or value.get("reasoning_effort") != LABEL_AUTHORITY_REASONING_EFFORT
        or value.get("execution_surface") != "external_codex"
        or value.get("ephemeral_execution") is not True
        or value.get("local_model_used") is not False
        or value.get("source_only_labeling") is not True
        or value.get("pipeline_predictions_inspected") is not False
        or value.get("internal_evaluation_artifacts_inspected") is not False
        or value.get("verifier_outputs_available_during_labeling") is not False
        or value.get("labels_sealed_before_verifier_outputs_opened") is not True
        or value.get("label_time_basis") != LABEL_TIME_BASIS
        or value.get("source_holdout_manifest_sha256") != source_holdout_manifest_sha256
        or value.get("source_primary_label_queue_sha256")
        != source_primary_label_queue_sha256
        or value.get("source_challenge_label_queue_sha256")
        != source_challenge_label_queue_sha256
        or value.get("completed_labels_sha256") != completed_labels_sha256
        or value.get("completed_challenge_labels_sha256")
        != completed_challenge_labels_sha256
        or not isinstance(invocation_count, int)
        or isinstance(invocation_count, bool)
        or invocation_count < 1
        or not isinstance(invocation_ids, list)
        or len(invocation_ids) != invocation_count
        or len(set(invocation_ids)) != invocation_count
        or any(
            not isinstance(item, str)
            or _LABEL_INVOCATION_ID_PATTERN.fullmatch(item) is None
            for item in invocation_ids
        )
        or not isinstance(value.get("request_set_sha256"), str)
        or _SHA256_PATTERN.fullmatch(str(value["request_set_sha256"])) is None
        or not isinstance(value.get("response_set_sha256"), str)
        or _SHA256_PATTERN.fullmatch(str(value["response_set_sha256"])) is None
        or not isinstance(value.get("receipt_set_sha256"), str)
        or _SHA256_PATTERN.fullmatch(str(value["receipt_set_sha256"])) is None
        or _parse_aware_datetime(value.get("started_at")) is None
        or _parse_aware_datetime(value.get("completed_at")) is None
        or _parse_aware_datetime(value["completed_at"])
        < _parse_aware_datetime(value["started_at"])
    ):
        raise GmailTemporalLabelFinalizerError("label authority manifest is invalid")
    evidence = _verify_retained_label_call_evidence(
        path.parent,
        key=key,
        source_holdout_manifest_sha256=source_holdout_manifest_sha256,
        source_primary_label_queue_raw=source_primary_label_queue_raw,
        source_challenge_label_queue_raw=source_challenge_label_queue_raw,
        completed_labels_raw=completed_labels_raw,
        completed_challenge_labels_raw=completed_challenge_labels_raw,
    )
    expected_evidence = {
        "logical_run_id": value["logical_run_id"],
        "plan_sha256": value["label_plan_sha256"],
        "plan_hmac_sha256": value["label_plan_hmac_sha256"],
        "started_at": value["started_at"],
        "completed_at": value["completed_at"],
        "invocation_count": invocation_count,
        "invocation_ids": invocation_ids,
        "request_set_sha256": value["request_set_sha256"],
        "response_set_sha256": value["response_set_sha256"],
        "receipt_set_sha256": value["receipt_set_sha256"],
    }
    if evidence != expected_evidence:
        raise GmailTemporalLabelFinalizerError(
            "label authority does not match retained call evidence"
        )
    return value, raw


def finalize_gmail_temporal_holdout_labels(
    holdout_root: Path,
    completed_labels_path: Path,
    hmac_key_path: Path,
    output_root: Path,
    *,
    completed_challenge_labels_path: Path | None = None,
    label_authority_manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Validate complete blind labels and publish authenticated private gold."""

    key = _private_hmac_key(hmac_key_path)
    holdout_root = Path(holdout_root)
    root_manifest, root_manifest_raw = _load_builder_manifest(
        holdout_root / "manifest.json",
        key=key,
    )
    artifacts = _verify_artifact_inventory(
        holdout_root,
        artifact_sha256=root_manifest["artifact_sha256"],
    )
    label_manifest_raw = artifacts["label-queue/manifest.json"]
    label_manifest = _load_label_manifest(
        label_manifest_raw,
        artifacts=artifacts,
        root_manifest=root_manifest,
    )
    primary_raw = artifacts["label-queue/primary.jsonl"]
    source_rows = _load_jsonl(primary_raw, description="primary label queue")
    if (
        len(source_rows) != label_manifest["primary_count"]
        or _jsonl_bytes(source_rows) != primary_raw
    ):
        raise GmailTemporalLabelFinalizerError(
            "primary label queue is not exact canonical authority"
        )
    _validate_source_queue(source_rows)
    challenge_raw = artifacts["label-queue/challenge.jsonl"]
    challenge_rows = _canonical_jsonl_rows_allow_empty(
        challenge_raw,
        description="challenge label queue",
    )
    if len(challenge_rows) != label_manifest["challenge_count"]:
        raise GmailTemporalLabelFinalizerError(
            "challenge label queue does not match its manifest"
        )
    if root_manifest["release_holdout_eligible"]:
        _validate_source_queue(challenge_rows)
        _validate_release_authority(
            artifacts,
            primary_queue=source_rows,
            challenge_queue=challenge_rows,
            reserve_count=int(root_manifest["reserve_sample_count"]),
            primary_request_count=root_manifest.get("primary_request_count"),
            challenge_request_count=root_manifest.get("challenge_request_count"),
        )
    completed_raw = _private_regular_file(
        completed_labels_path,
        description="completed labels",
    )
    completed_rows = _load_jsonl(completed_raw, description="completed labels")
    gold_rows, counts = _validate_completed_labels(source_rows, completed_rows)
    gold_raw = _jsonl_bytes(gold_rows)
    gold_sha256 = _sha256_bytes(gold_raw)
    challenge_completed = completed_challenge_labels_path is not None
    challenge_diagnostic_ready = challenge_completed and bool(challenge_rows)
    challenge_completed_raw: bytes | None = None
    challenge_gold_raw: bytes | None = None
    challenge_counts: dict[str, int] | None = None
    if completed_challenge_labels_path is not None:
        _validate_source_queue(challenge_rows)
        challenge_completed_raw = _private_regular_file(
            completed_challenge_labels_path,
            description="completed challenge labels",
        )
        challenge_completed_rows = (
            []
            if not challenge_completed_raw
            else _load_jsonl(
                challenge_completed_raw,
                description="completed challenge labels",
            )
        )
        challenge_gold_rows, challenge_counts = _validate_completed_labels(
            challenge_rows,
            challenge_completed_rows,
        )
        challenge_gold_raw = _jsonl_bytes(challenge_gold_rows)
    primary_label_data_gate_passed = _primary_label_data_gate(counts)
    challenge_label_data_gate_passed = _challenge_label_data_gate(challenge_counts)
    label_gate_passed = (
        primary_label_data_gate_passed and challenge_label_data_gate_passed
    )
    label_authority: dict[str, Any] | None = None
    label_authority_raw: bytes | None = None
    if label_authority_manifest_path is not None:
        authority_path = Path(label_authority_manifest_path)
        evidence_root = authority_path.parent
        expected_primary_path = evidence_root / "completed-primary.jsonl"
        expected_challenge_path = evidence_root / "completed-challenge.jsonl"
        if (
            authority_path.name != "label-authority.json"
            or completed_challenge_labels_path is None
            or challenge_completed_raw is None
            or Path(os.path.abspath(completed_labels_path))
            != Path(os.path.abspath(expected_primary_path))
            or Path(os.path.abspath(completed_challenge_labels_path))
            != Path(os.path.abspath(expected_challenge_path))
        ):
            raise GmailTemporalLabelFinalizerError(
                "label authority artifacts must remain in their retained run root"
            )
        label_authority, label_authority_raw = _load_label_authority_manifest(
            authority_path,
            key=key,
            source_holdout_manifest_sha256=_sha256_bytes(root_manifest_raw),
            source_primary_label_queue_sha256=_sha256_bytes(primary_raw),
            source_challenge_label_queue_sha256=_sha256_bytes(challenge_raw),
            completed_labels_sha256=_sha256_bytes(completed_raw),
            completed_challenge_labels_sha256=(
                _sha256_bytes(challenge_completed_raw)
                if challenge_completed_raw is not None
                else None
            ),
            source_primary_label_queue_raw=primary_raw,
            source_challenge_label_queue_raw=challenge_raw,
            completed_labels_raw=completed_raw,
            completed_challenge_labels_raw=challenge_completed_raw,
        )
    sol_label_authority_attested = label_authority is not None
    effective_release_holdout_eligible = (
        root_manifest["release_holdout_eligible"]
        and sol_label_authority_attested
        and challenge_diagnostic_ready
        and label_gate_passed
    )
    output_artifacts = {"gold.jsonl": gold_raw}
    artifact_sha256 = {"gold.jsonl": gold_sha256}
    if challenge_gold_raw is not None:
        output_artifacts["challenge-diagnostic-gold.jsonl"] = challenge_gold_raw
        artifact_sha256["challenge-diagnostic-gold.jsonl"] = _sha256_bytes(
            challenge_gold_raw
        )
    manifest = {
        "version": GOLD_MANIFEST_VERSION,
        "finalizer_version": VERSION,
        "finalizer_sha256": _sha256_bytes(Path(__file__).read_bytes()),
        "source_holdout_manifest_sha256": _sha256_bytes(root_manifest_raw),
        "source_holdout_manifest_hmac_sha256": root_manifest["manifest_hmac_sha256"],
        "source_label_manifest_sha256": _sha256_bytes(label_manifest_raw),
        "source_primary_label_queue_sha256": _sha256_bytes(primary_raw),
        "source_challenge_label_queue_sha256": _sha256_bytes(challenge_raw),
        "completed_labels_sha256": _sha256_bytes(completed_raw),
        "completed_challenge_labels_sha256": (
            _sha256_bytes(challenge_completed_raw)
            if challenge_completed_raw is not None
            else None
        ),
        "label_authority_manifest_sha256": (
            _sha256_bytes(label_authority_raw)
            if label_authority_raw is not None
            else None
        ),
        "label_authority_version": (
            label_authority["version"] if label_authority is not None else None
        ),
        "label_authority_model": (
            label_authority["model"] if label_authority is not None else None
        ),
        "label_authority_reasoning_effort": (
            label_authority["reasoning_effort"] if label_authority is not None else None
        ),
        "label_authority_invocation_count": (
            label_authority["invocation_count"] if label_authority is not None else 0
        ),
        "label_chronology_verified": sol_label_authority_attested,
        "label_chronology_scope": (
            "authenticated_retained_run_only_no_off_ledger_absence_claim"
        ),
        "label_off_ledger_activity_absence_proven": False,
        "label_logical_run_id": (
            label_authority["logical_run_id"] if label_authority is not None else None
        ),
        "label_plan_sha256": (
            label_authority["label_plan_sha256"]
            if label_authority is not None
            else None
        ),
        "label_plan_hmac_sha256": (
            label_authority["label_plan_hmac_sha256"]
            if label_authority is not None
            else None
        ),
        "label_started_at": (
            label_authority["started_at"] if label_authority is not None else None
        ),
        "label_completed_at": (
            label_authority["completed_at"] if label_authority is not None else None
        ),
        "label_receipt_set_sha256": (
            label_authority["receipt_set_sha256"]
            if label_authority is not None
            else None
        ),
        "sol_label_authority_attested": sol_label_authority_attested,
        "source_only_label_authority_attested": sol_label_authority_attested,
        "source_labels_sealed_before_verifier_outputs_opened_attested": (
            sol_label_authority_attested
        ),
        "label_authority_provenance_cryptographically_verified": False,
        "artifact_sha256": artifact_sha256,
        "gold_record_count": counts["records"],
        "gold_thread_count": counts["threads"],
        "expected_material_count": counts["expected_material"],
        "expected_suppressed_count": counts["expected_suppressed"],
        "labeled_hard_negative_count": counts["labeled_hard_negatives"],
        "semantic_unit_count": counts["units"],
        "semantic_member_count": counts["members"],
        "supported_member_count": counts["supported_members"],
        "uncertain_member_count": counts["uncertain_members"],
        "exact_alternative_count": counts["exact_alternatives"],
        "partial_alternative_count": counts["partial_alternatives"],
        "challenge_completed": challenge_completed,
        "challenge_expected_record_count": label_manifest["challenge_count"],
        "challenge_gold_record_count": (
            challenge_counts["records"] if challenge_counts is not None else 0
        ),
        "challenge_gold_sha256": (
            _sha256_bytes(challenge_gold_raw)
            if challenge_gold_raw is not None
            else None
        ),
        "challenge_diagnostic_ready": challenge_diagnostic_ready,
        "challenge_diagnostic_only": root_manifest["challenge_evidence_scope"]
        == "diagnostic_balanced_capability_stress",
        "challenge_role": "conditional_capability_stress_gate",
        "challenge_required_as_separate_promotion_gate": True,
        "challenge_contributes_to_primary_release_gates": False,
        "estimands_must_not_be_pooled": True,
        "primary_estimand": "natural_mail_population_operability",
        "challenge_estimand": "conditional_temporal_capability_stress_recall",
        "primary_minimum_labeled_hard_negatives": (
            PRIMARY_MIN_LABELED_HARD_NEGATIVES
        ),
        "challenge_minimum_expected_material_records": (
            CHALLENGE_MIN_EXPECTED_MATERIAL_RECORDS
        ),
        "challenge_minimum_semantic_members": CHALLENGE_MIN_SEMANTIC_MEMBERS,
        "challenge_minimum_supported_members": CHALLENGE_MIN_SUPPORTED_MEMBERS,
        "challenge_minimum_labeled_hard_negatives": (
            CHALLENGE_MIN_LABELED_HARD_NEGATIVES
        ),
        "coverage": "exact_primary_queue_order_and_membership",
        "source_fields": "canonical_json_immutable",
        "label_time_basis": LABEL_TIME_BASIS,
        "later_context_policy": LATER_CONTEXT_POLICY,
        "baseline_frontier_grade_authority": (
            "adapter_recomputed_from_frozen_primary_evaluation_authority"
        ),
        "baseline_frontier_grade_input_placeholder": BASELINE_GRADE_PLACEHOLDER,
        "baseline_frontier_grade_human_controlled": False,
        "candidate_gold_adapter_required": True,
        "direct_candidate_gold_evaluator_ready": False,
        "primary_label_data_gate_passed": primary_label_data_gate_passed,
        "challenge_label_data_gate_passed": challenge_label_data_gate_passed,
        "label_gate_passed": label_gate_passed,
        "release_evidence_class": root_manifest["release_evidence_class"],
        "release_evidence_class_applies_to": root_manifest[
            "release_evidence_class_applies_to"
        ],
        "primary_evidence_scope": root_manifest["primary_evidence_scope"],
        "primary_prospective_unseen_source_evidence": root_manifest[
            "primary_prospective_unseen_source_evidence"
        ],
        "primary_historical_architecture_exposed": root_manifest[
            "primary_historical_architecture_exposed"
        ],
        "challenge_evidence_scope": root_manifest["challenge_evidence_scope"],
        "challenge_prospective_unseen_source_evidence": root_manifest[
            "challenge_prospective_unseen_source_evidence"
        ],
        "challenge_historical_architecture_exposed": root_manifest[
            "challenge_historical_architecture_exposed"
        ],
        "development_baseline_challenge_overlap_count": root_manifest.get(
            "development_baseline_challenge_overlap_count", 0
        ),
        "challenge_population_inference_eligible": False,
        "cohort_metrics_must_not_be_pooled": True,
        "freeze_authority_version": root_manifest["freeze_authority_version"],
        "freeze_attempt_version": root_manifest["freeze_attempt_version"],
        "freeze_outcome_version": root_manifest["freeze_outcome_version"],
        "freeze_authority_manifest_sha256": root_manifest[
            "freeze_authority_manifest_sha256"
        ],
        "freeze_attempt_id": root_manifest["freeze_attempt_id"],
        "freeze_attempt_sha256": root_manifest["freeze_attempt_sha256"],
        "freeze_milestone": root_manifest["freeze_milestone"],
        "freeze_authority_evidence_class": root_manifest[
            "freeze_authority_evidence_class"
        ],
        "freeze_authority_status": root_manifest["freeze_authority_status"],
        "freeze_no_reroll_scope": root_manifest["freeze_no_reroll_scope"],
        "freeze_authority_independently_reverified_downstream": root_manifest[
            "freeze_authority_independently_reverified_downstream"
        ],
        "legacy_signed_freeze_claims_downgraded": root_manifest.get(
            "legacy_signed_freeze_claims_downgraded", False
        ),
        "freeze_irrevocable_from_first_materialization": root_manifest[
            "freeze_irrevocable_from_first_materialization"
        ],
        "labeled_cohort_reroll_forbidden": root_manifest[
            "labeled_cohort_reroll_forbidden"
        ],
        "all_labeled_attempts_must_be_retained": root_manifest[
            "all_labeled_attempts_must_be_retained"
        ],
        "source_labels_must_be_sealed_before_verifier_outputs_opened": True,
        "primary_population_scope": root_manifest["primary_population_scope"],
        "representative_gmail_production_eligible": False,
        "prospective_existing_thread_update_gate_required": root_manifest[
            "prospective_existing_thread_update_gate_required"
        ],
        "prospective_natural_recall_continuation_required": root_manifest[
            "prospective_natural_recall_continuation_required"
        ],
        "prospective_natural_material_minimum": 20,
        "prospective_natural_effective_recall_minimum": 0.90,
        "prospective_natural_recall_continuation_passed": False,
        "underpowered_primary_action": root_manifest[
            "underpowered_primary_action"
        ],
        "underpowered_challenge_action": root_manifest[
            "underpowered_challenge_action"
        ],
        "release_scope": root_manifest["release_scope"],
        "prospective_unseen_source_evidence": root_manifest[
            "prospective_unseen_source_evidence"
        ],
        "historical_architecture_exposed": root_manifest[
            "historical_architecture_exposed"
        ],
        "retrospective_calibration_eligible": root_manifest[
            "retrospective_calibration_eligible"
        ],
        "semantic_development_overlap_status": root_manifest[
            "semantic_development_overlap_status"
        ],
        "automatic_apply_eligible": False,
        "content_changing_canary_required": root_manifest[
            "content_changing_canary_required"
        ],
        "source_release_holdout_eligible": root_manifest["release_holdout_eligible"],
        "release_holdout_eligible": effective_release_holdout_eligible,
        "release_structural_scorability_verified": (effective_release_holdout_eligible),
        "private_file_mode": "0600",
        "private_directory_mode": "0700",
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    manifest_raw = _authenticated_gold_manifest_bytes(manifest, key=key)
    output_artifacts["manifest.json"] = manifest_raw
    _publish_frozen(output_root, output_artifacts)
    return {
        "version": VERSION,
        "status": "finalized",
        "records": counts["records"],
        "threads": counts["threads"],
        "expected_material": counts["expected_material"],
        "expected_suppressed": counts["expected_suppressed"],
        "labeled_hard_negatives": counts["labeled_hard_negatives"],
        "semantic_units": counts["units"],
        "semantic_members": counts["members"],
        "supported_members": counts["supported_members"],
        "uncertain_members": counts["uncertain_members"],
        "primary_label_data_gate_passed": primary_label_data_gate_passed,
        "challenge_label_data_gate_passed": challenge_label_data_gate_passed,
        "label_gate_passed": label_gate_passed,
        "release_holdout_eligible": effective_release_holdout_eligible,
        "release_evidence_class": root_manifest["release_evidence_class"],
        "primary_evidence_scope": root_manifest["primary_evidence_scope"],
        "challenge_evidence_scope": root_manifest["challenge_evidence_scope"],
        "challenge_historical_architecture_exposed": root_manifest[
            "challenge_historical_architecture_exposed"
        ],
        "release_scope": root_manifest["release_scope"],
        "retrospective_calibration_eligible": root_manifest[
            "retrospective_calibration_eligible"
        ],
        "sol_label_authority_attested": sol_label_authority_attested,
        "challenge_diagnostic_ready": challenge_diagnostic_ready,
        "challenge_records": (
            challenge_counts["records"] if challenge_counts is not None else 0
        ),
        "manifest_sha256": _sha256_bytes(manifest_raw),
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--holdout-root", type=Path, required=True)
    parser.add_argument("--completed-labels", type=Path, required=True)
    parser.add_argument("--completed-challenge-labels", type=Path)
    parser.add_argument("--label-authority-manifest", type=Path)
    parser.add_argument("--hmac-key", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = finalize_gmail_temporal_holdout_labels(
            args.holdout_root,
            args.completed_labels,
            args.hmac_key,
            args.output_root,
            completed_challenge_labels_path=args.completed_challenge_labels,
            label_authority_manifest_path=args.label_authority_manifest,
        )
    except Exception:  # noqa: BLE001 - never emit private paths or label details.
        print(
            json.dumps(
                {
                    "version": VERSION,
                    "status": "failed",
                    "error": "gmail_temporal_holdout_label_finalization_failed",
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
