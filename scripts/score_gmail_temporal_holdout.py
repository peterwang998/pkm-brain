#!/usr/bin/env python3
"""Score an authenticated Gmail temporal holdout with three verifier runs.

The scorer is local-only.  It authenticates the candidate-gold adapter bundle,
binds three owner-only checkpoints to its exact sample and current deterministic
frontier, applies the production three-run consensus and calibration policy,
and publishes authenticated aggregate-only metrics.  It neither uses the
synthetic benchmark run-manifest provenance nor performs model, network, or
Brain persistence calls.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import math
import re
import sys
from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from pkm_brain.gmail_temporal_frontier import (
    gmail_temporal_candidate_ensemble_policy_fingerprint,
    plan_gmail_temporal_candidate_pages,
    validate_gmail_temporal_candidate_ensemble_verdict_set,
)


VERSION = "gmail_temporal_holdout_scorer_v3"
MANIFEST_VERSION = "gmail_temporal_holdout_score_manifest_v3"
SCORE_VERSION = "gmail_temporal_holdout_score_v3"
MANIFEST_DOMAIN = b"gmail_temporal_holdout_score_manifest_v3\0"
ATTESTATION_VERSION = "gmail_temporal_holdout_invocation_attestation_v2"
ATTESTATION_DOMAIN = b"gmail_temporal_holdout_invocation_attestation_v2\0"
SCORE_ARTIFACT = "score.json"
OWNER_AUDIT_POPULATION_ARTIFACT = "owner-audit-population.jsonl"
OWNER_AUDIT_ERRORS_ARTIFACT = "owner-audit-errors.jsonl"
OWNER_AUDIT_POPULATION_VERSION = "gmail_temporal_owner_audit_population_v1"
OWNER_AUDIT_ERROR_VERSION = "gmail_temporal_owner_audit_error_v1"
CHALLENGE_LIFECYCLE_SOURCE_GOLD_METRICS_VERSION = (
    "gmail_temporal_challenge_lifecycle_source_gold_metrics_v1"
)
CHALLENGE_LIFECYCLE_REQUIRED_CATEGORIES = (
    "reschedule",
    "cancellation",
    "completion",
    "timezone",
)
CHALLENGE_LIFECYCLE_MIN_EFFECTIVE_RECALL = 0.80
CHALLENGE_LIFECYCLE_MIN_CONFIRMED_RECALL = 0.80
CHALLENGE_LIFECYCLE_LOW_N_MEMBER_THRESHOLD = 5
PRIMARY_OPERABILITY_GATE_NAMES = frozenset(
    {
        "supported_to_uncertain_rate",
        "strict_supported_precision",
        "supported_artifact_precision",
        "recall_arm_precision",
        "effective_artifact_precision",
        "uncertainty_hypothesis_purity",
        "accepted_negative_review_rate",
        "no_supported_hard_negative_artifacts",
        "no_supported_negative_artifacts",
        "no_redundant_artifacts",
        "no_duplicate_aliases",
        "no_supported_overclaims",
        "no_critical_calibration_errors",
        "no_default_negative_supported",
        "no_default_negative_accepted",
    }
)
_ROOT = Path(__file__).resolve().parents[1]
_ADAPTER_PATH = _ROOT / "scripts" / "prepare_gmail_temporal_holdout_candidate_gold.py"
_ENSEMBLE_PATH = _ROOT / "scripts" / "evaluate_gmail_temporal_candidate_ensemble.py"
_EXTERNAL_RUNNER_PATH = _ROOT / "scripts" / "run_gmail_temporal_holdout_external.py"


class GmailTemporalHoldoutScoreError(ValueError):
    """Raised when authenticated holdout scoring evidence is invalid."""


def _load_script(name: str, path: Path) -> ModuleType:
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise GmailTemporalHoldoutScoreError(
            "required local scorer could not be loaded"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(name, None)
        raise GmailTemporalHoldoutScoreError(
            "required local scorer could not be loaded"
        ) from exc
    return module


adapter = _load_script(
    "_gmail_temporal_holdout_candidate_gold_adapter_for_scorer",
    _ADAPTER_PATH,
)
ensemble = _load_script(
    "_gmail_temporal_candidate_ensemble_for_holdout_scorer",
    _ENSEMBLE_PATH,
)
external_runner = _load_script(
    "_gmail_temporal_holdout_external_runner_for_scorer",
    _EXTERNAL_RUNNER_PATH,
)
candidate_gold = adapter.candidate_evaluator
finalizer = adapter.finalizer


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _private_file(path: Path, *, description: str) -> bytes:
    try:
        return finalizer._private_regular_file(path, description=description)
    except finalizer.GmailTemporalLabelFinalizerError as exc:
        raise GmailTemporalHoldoutScoreError(
            f"{description} is unavailable or unsafe"
        ) from exc


def _parse_manifest(raw: bytes, *, description: str) -> dict[str, Any]:
    try:
        value = finalizer._parse_json(raw, description=description)
    except finalizer.GmailTemporalLabelFinalizerError as exc:
        raise GmailTemporalHoldoutScoreError(f"{description} is invalid") from exc
    if not isinstance(value, dict) or raw != _canonical_json(value) + b"\n":
        raise GmailTemporalHoldoutScoreError(f"{description} is invalid")
    return value


def _load_adapter_root(
    root: Path,
    *,
    key: bytes,
) -> tuple[dict[str, Any], bytes, Path, bytes, list[dict[str, Any]]]:
    try:
        finalizer._private_directory(root, description="candidate-gold root")
        entries = list(root.iterdir())
    except (OSError, finalizer.GmailTemporalLabelFinalizerError) as exc:
        raise GmailTemporalHoldoutScoreError(
            "candidate-gold root is unavailable or unsafe"
        ) from exc
    expected_names = {"manifest.json", adapter.OUTPUT_SAMPLE_ARTIFACT}
    if {entry.name for entry in entries} != expected_names or any(
        entry.is_symlink() or not entry.is_file() for entry in entries
    ):
        raise GmailTemporalHoldoutScoreError("candidate-gold inventory is not exact")
    manifest_raw = _private_file(
        root / "manifest.json",
        description="candidate-gold manifest",
    )
    sample_path = root / adapter.OUTPUT_SAMPLE_ARTIFACT
    sample_raw = _private_file(sample_path, description="candidate-gold samples")
    manifest = _parse_manifest(manifest_raw, description="candidate-gold manifest")
    authenticator = manifest.get("manifest_hmac_sha256")
    unsigned = dict(manifest)
    unsigned.pop("manifest_hmac_sha256", None)
    expected_authenticator = hmac.new(
        key,
        adapter.MANIFEST_DOMAIN + _canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()
    if not isinstance(authenticator, str) or not hmac.compare_digest(
        authenticator,
        expected_authenticator,
    ):
        raise GmailTemporalHoldoutScoreError(
            "candidate-gold manifest authentication failed"
        )
    required = {
        "version": adapter.MANIFEST_VERSION,
        "adapter_version": adapter.VERSION,
        "coverage": "exact_cohort_sample_binding_request_and_gold",
        "baseline_frontier_grade_human_controlled": False,
        "candidate_gold_sample_compatible": True,
        "estimands_must_not_be_pooled": True,
        "primary_estimand": "natural_mail_population_operability",
        "challenge_estimand": "conditional_temporal_capability_stress_recall",
        "challenge_role": "conditional_capability_stress_gate",
        "challenge_required_as_separate_promotion_gate": True,
        "challenge_contributes_to_primary_release_gates": False,
        "cohort_metrics_must_not_be_pooled": True,
        "challenge_population_inference_eligible": False,
        "release_evidence_class_applies_to": "primary_natural_cohort",
        "freeze_no_reroll_scope": finalizer.FREEZE_NO_REROLL_SCOPE,
        "freeze_authority_independently_reverified_downstream": False,
        "labeled_cohort_reroll_forbidden": True,
        "all_labeled_attempts_must_be_retained": True,
        "underpowered_primary_action": (
            "publish_failure_then_activate_sealed_reserve_in_authenticated_order_for_regression_diagnostic_only_then_fresh_150_100_75_required_for_release"
        ),
        "underpowered_challenge_action": (
            "publish_underpowered_result_then_versioned_redesign_no_reroll"
        ),
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    if any(manifest.get(name) != value for name, value in required.items()):
        raise GmailTemporalHoldoutScoreError(
            "candidate-gold manifest policy is invalid"
        )
    cohort = manifest.get("cohort")
    source_evidence_class = manifest.get("source_release_evidence_class")
    evidence_contracts = {
        finalizer.DIAGNOSTIC_EVIDENCE_CLASS: {
            "source_release_scope": "diagnostic_only",
            "source_primary_release_holdout_eligible": False,
            "source_prospective_unseen_source_evidence": False,
            "source_historical_architecture_exposed": False,
            "retrospective_calibration_eligible": False,
            "source_semantic_development_overlap_status": "not_release_evidence",
            "content_changing_canary_required": False,
        },
        finalizer.RETROSPECTIVE_EVIDENCE_CLASS: {
            "source_release_scope": "local_review_preview",
            "source_primary_release_holdout_eligible": False,
            "source_prospective_unseen_source_evidence": False,
            "source_historical_architecture_exposed": True,
            "retrospective_calibration_eligible": True,
            "source_semantic_development_overlap_status": (
                "unknown_legacy_cohort_bindings_unrecoverable"
            ),
            "content_changing_canary_required": True,
        },
        finalizer.PROSPECTIVE_EVIDENCE_CLASS: {
            "source_release_scope": "local_review_only",
            "source_primary_release_holdout_eligible": True,
            "source_prospective_unseen_source_evidence": True,
            "source_historical_architecture_exposed": False,
            "retrospective_calibration_eligible": False,
            "source_semantic_development_overlap_status": (
                "excluded_by_frozen_thread_scope"
            ),
            "content_changing_canary_required": True,
        },
    }
    source_contract = evidence_contracts.get(source_evidence_class)
    sol_attested = manifest.get("sol_label_authority_attested")
    source_only_attested = manifest.get("source_only_label_authority_attested")
    label_authority_present = sol_attested is True and source_only_attested is True
    label_authority_sha256 = manifest.get("label_authority_manifest_sha256")
    label_authority_invocation_count = manifest.get("label_authority_invocation_count")
    expected_release_eligible = bool(
        source_contract
        and source_contract["source_primary_release_holdout_eligible"]
        and label_authority_present
        and cohort == "primary"
    )
    raw_overlap_count = manifest.get("development_baseline_cohort_overlap_count")
    if not isinstance(raw_overlap_count, int) or isinstance(raw_overlap_count, bool):
        raw_overlap_count = -1
    expected_cohort_prospective = bool(
        source_evidence_class == finalizer.PROSPECTIVE_EVIDENCE_CLASS
        and raw_overlap_count == 0
    )
    expected_cohort_historical = bool(
        source_evidence_class == finalizer.RETROSPECTIVE_EVIDENCE_CLASS
        if cohort == "primary"
        else raw_overlap_count > 0
    )
    expected_cohort_scope = (
        {
            finalizer.DIAGNOSTIC_EVIDENCE_CLASS: "diagnostic_natural_operability",
            finalizer.RETROSPECTIVE_EVIDENCE_CLASS: (
                "retrospective_natural_operability_preview"
            ),
            finalizer.PROSPECTIVE_EVIDENCE_CLASS: (
                "prospective_natural_operability_review_only"
            ),
        }.get(source_evidence_class)
        if cohort == "primary"
        else "historical_balanced_capability_stress_review_only"
        if expected_cohort_historical
        else "prospective_balanced_capability_stress_review_only"
        if expected_cohort_prospective
        else "diagnostic_balanced_capability_stress"
    )
    expected_overlap_status = (
        "challenge_stress_contains_development_baseline_overlap"
        if cohort == "challenge" and raw_overlap_count > 0
        else source_contract.get("source_semantic_development_overlap_status")
        if source_contract is not None
        else None
    )
    expected_diagnostic_only = expected_cohort_scope in {
        "diagnostic_natural_operability",
        "diagnostic_balanced_capability_stress",
    }
    if (
        cohort not in {"primary", "challenge"}
        or source_contract is None
        or any(
            manifest.get(field) != expected
            for field, expected in source_contract.items()
        )
        or manifest.get("release_evidence_class") != source_evidence_class
        or manifest.get("release_evidence_class_applies_to") != "primary_natural_cohort"
        or manifest.get("cohort_metrics_must_not_be_pooled") is not True
        or manifest.get("challenge_population_inference_eligible") is not False
        or raw_overlap_count < 0
        or manifest.get("cohort_evidence_scope") != expected_cohort_scope
        or manifest.get("release_scope") != expected_cohort_scope
        or manifest.get("prospective_unseen_source_evidence")
        is not expected_cohort_prospective
        or manifest.get("historical_architecture_exposed")
        is not expected_cohort_historical
        or manifest.get("semantic_development_overlap_status")
        != expected_overlap_status
        or manifest.get("automatic_apply_eligible") is not False
        or not isinstance(sol_attested, bool)
        or not isinstance(source_only_attested, bool)
        or sol_attested is not source_only_attested
        or manifest.get("label_authority_provenance_cryptographically_verified")
        is not False
        or not isinstance(label_authority_invocation_count, int)
        or isinstance(label_authority_invocation_count, bool)
        or label_authority_invocation_count < 0
        or (
            label_authority_present
            and (
                manifest.get("label_authority_version")
                != finalizer.LABEL_AUTHORITY_VERSION
                or manifest.get("label_authority_model")
                != finalizer.LABEL_AUTHORITY_MODEL
                or manifest.get("label_authority_reasoning_effort")
                != finalizer.LABEL_AUTHORITY_REASONING_EFFORT
                or label_authority_invocation_count < 1
                or not isinstance(label_authority_sha256, str)
                or finalizer._SHA256_PATTERN.fullmatch(label_authority_sha256) is None
            )
        )
        or (
            not label_authority_present
            and (
                manifest.get("label_authority_version") is not None
                or manifest.get("label_authority_model") is not None
                or manifest.get("label_authority_reasoning_effort") is not None
                or label_authority_invocation_count != 0
                or label_authority_sha256 is not None
            )
        )
        or manifest.get("baseline_frontier_grade_authority")
        != f"frozen_{cohort}_evaluation_authority_only"
        or manifest.get("diagnostic_denominator") != f"{cohort}_only"
        or not isinstance(manifest.get("diagnostic_only"), bool)
        or not isinstance(manifest.get("release_holdout_eligible"), bool)
        or manifest["release_holdout_eligible"] is not expected_release_eligible
        or manifest.get("release_structural_scorability_verified")
        is not expected_release_eligible
        or manifest["diagnostic_only"] is not expected_diagnostic_only
        or manifest.get("source_release_holdout_eligible")
        is not manifest["release_holdout_eligible"]
    ):
        raise GmailTemporalHoldoutScoreError(
            "candidate-gold release eligibility is invalid"
        )
    if manifest.get("artifact_sha256") != {
        adapter.OUTPUT_SAMPLE_ARTIFACT: _sha256_bytes(sample_raw)
    }:
        raise GmailTemporalHoldoutScoreError("candidate-gold sample commitment failed")
    current_evaluator_sha256 = _sha256_bytes(
        candidate_gold._EVALUATOR_PATH.read_bytes()
    )
    if manifest.get("candidate_evaluator_sha256") != current_evaluator_sha256:
        raise GmailTemporalHoldoutScoreError(
            "candidate-gold evaluator provenance is stale"
        )
    try:
        samples = candidate_gold._load_jsonl(sample_path)
    except candidate_gold.CandidateGoldError as exc:
        raise GmailTemporalHoldoutScoreError(
            "candidate-gold samples are invalid"
        ) from exc
    if manifest.get("record_count") != len(samples):
        raise GmailTemporalHoldoutScoreError(
            "candidate-gold sample coverage is invalid"
        )
    return manifest, manifest_raw, sample_path, sample_raw, samples


def _load_checkpoint_rows(path: Path) -> tuple[bytes, list[dict[str, Any]]]:
    raw = _private_file(path, description="verifier checkpoint")
    if (
        not raw
        or not raw.endswith(b"\n")
        or any(not line.strip() for line in raw.splitlines())
    ):
        raise GmailTemporalHoldoutScoreError("verifier checkpoint is malformed")
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            value = finalizer._parse_json(line, description="verifier checkpoint")
        except finalizer.GmailTemporalLabelFinalizerError as exc:
            raise GmailTemporalHoldoutScoreError(
                "verifier checkpoint is malformed"
            ) from exc
        if not isinstance(value, dict):
            raise GmailTemporalHoldoutScoreError("verifier checkpoint is malformed")
        rows.append(value)
    return raw, rows


def _aware_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _frozen_request_fingerprints(
    runtime_batches: list[Any],
    *,
    expected_count: int,
) -> tuple[str, ...]:
    values: list[str] = []
    try:
        for runtime in runtime_batches:
            frontier = adapter.build_gmail_temporal_candidate_frontier(
                analysis=runtime.analysis,
                batch=runtime.batch,
            )
            for page in runtime.pages:
                request = adapter._request_for_page(
                    batch=runtime.batch,
                    frontier=frontier,
                    page_plan=SimpleNamespace(
                        plan_fingerprint=runtime.candidate_page_plan_fingerprint
                    ),
                    page=page,
                )
                values.append(request.request_fingerprint)
    except (ValueError, TypeError, AttributeError) as exc:
        raise GmailTemporalHoldoutScoreError(
            "independent invocation attestation is invalid"
        ) from exc
    if len(values) != expected_count or len(set(values)) != len(values):
        raise GmailTemporalHoldoutScoreError(
            "independent invocation attestation is invalid"
        )
    return tuple(values)


def _retained_run_evidence(
    run_root: Path,
    *,
    key: bytes,
    hmac_key_path: Path,
    attestation: Mapping[str, Any],
    adapter_manifest: Mapping[str, Any],
    adapter_manifest_sha256: str,
    request_fingerprints: tuple[str, ...],
    checkpoint_protocol: str,
    checkpoint_source_hashes: Mapping[str, str],
    current_source_hashes: Mapping[str, str],
    cohort: str,
    run_ordinal: int,
) -> dict[str, Any]:
    """Bind one v2 attestation to its signed plan and every retained call."""

    try:
        root = Path(run_root)
        external_runner._private_directory(root)
        plan_path = root / "plan.json"
        plan_raw = external_runner._private_file(plan_path)
        plan = external_runner._parse_canonical_json(plan_raw)
        calls_root = root / "calls"
        external_runner._private_directory(calls_root)
        current_runner_sha256 = _sha256_bytes(_EXTERNAL_RUNNER_PATH.read_bytes())
        expected_plan_keys = {
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
        inputs = plan.get("inputs")
        units = plan.get("units")
        batch_size = plan.get("batch_size")
        plan_created_at = _aware_timestamp(plan.get("created_at"))
        attestation_started_at = _aware_timestamp(attestation.get("started_at"))
        label_completed_at = _aware_timestamp(
            adapter_manifest.get("label_completed_at")
        )
        expected_input_keys = {
            "source_holdout_manifest_sha256",
            "adapter_manifest_sha256",
            "frozen_request_artifact_sha256",
            "frozen_request_count",
            "partition_version",
            "request_partition_sha256",
            "protocol_fingerprint",
            "source_module_sha256",
            "labels_finalized_before_verification",
            "label_chronology_verified",
            "label_logical_run_id",
            "label_plan_sha256",
            "label_plan_hmac_sha256",
            "label_started_at",
            "label_completed_at",
            "label_receipt_set_sha256",
        }
        if (
            set(plan) != expected_plan_keys
            or not external_runner._verify_signature(
                plan,
                key=key,
                domain=external_runner.PLAN_DOMAIN,
                signature_field="plan_hmac_sha256",
            )
            or plan.get("version") != external_runner.PLAN_VERSION
            or plan.get("runner_sha256") != current_runner_sha256
            or plan.get("phase") != "verify"
            or plan.get("run_ordinal") != run_ordinal
            or plan.get("cohort") != cohort
            or plan.get("provider") != external_runner.PROVIDER
            or plan.get("model") != candidate_gold.EXPECTED_MODEL
            or plan.get("reasoning_effort")
            != candidate_gold.EXPECTED_REASONING_EFFORT
            or not isinstance(batch_size, int)
            or isinstance(batch_size, bool)
            or not 1 <= batch_size <= external_runner.MAX_VERIFIER_BATCH_SIZE
            or not isinstance(inputs, Mapping)
            or set(inputs) != expected_input_keys
            or not isinstance(units, list)
            or not units
            or plan.get("logical_run_id") != attestation.get("logical_run_id")
            or plan_created_at is None
            or attestation_started_at is None
            or plan_created_at > attestation_started_at
            or label_completed_at is None
            or plan_created_at <= label_completed_at
            or attestation_started_at <= label_completed_at
            or plan.get("ephemeral_execution") is not True
            or plan.get("restricted_execution") is not True
            or plan.get("local_model_used") is not False
            or plan.get("private_content_printed") is not False
            or plan.get("routable") is not False
        ):
            raise external_runner.GmailTemporalExternalRunnerError(
                "retained verifier plan is invalid"
            )
        expected_inputs = {
            "source_holdout_manifest_sha256": adapter_manifest.get(
                "source_holdout_manifest_sha256"
            ),
            "adapter_manifest_sha256": adapter_manifest_sha256,
            "frozen_request_artifact_sha256": adapter_manifest.get(
                "source_cohort_requests_sha256"
            ),
            "frozen_request_count": len(request_fingerprints),
            "partition_version": external_runner.VERIFIER_PARTITION_VERSION,
            "request_partition_sha256": attestation.get(
                "request_partition_sha256"
            ),
            "protocol_fingerprint": checkpoint_protocol,
            "source_module_sha256": dict(current_source_hashes),
            "labels_finalized_before_verification": True,
            **{
                field: adapter_manifest.get(field)
                for field in (
                    "label_chronology_verified",
                    "label_logical_run_id",
                    "label_plan_sha256",
                    "label_plan_hmac_sha256",
                    "label_started_at",
                    "label_completed_at",
                    "label_receipt_set_sha256",
                )
            },
        }
        if dict(inputs) != expected_inputs:
            raise external_runner.GmailTemporalExternalRunnerError(
                "retained verifier inputs are invalid"
            )

        expected_unit_keys = {
            "unit_id",
            "cohort",
            "ordinal",
            "item_ids",
            "item_sha256",
            "request_sha256",
        }
        partition_rows: list[dict[str, Any]] = []
        unit_ids: list[str] = []
        for index, unit in enumerate(units, start=1):
            expected_items = list(
                request_fingerprints[
                    (index - 1) * batch_size : index * batch_size
                ]
            )
            item_ids = unit.get("item_ids") if isinstance(unit, Mapping) else None
            item_sha256 = (
                unit.get("item_sha256") if isinstance(unit, Mapping) else None
            )
            unit_id = unit.get("unit_id") if isinstance(unit, Mapping) else None
            if (
                not isinstance(unit, Mapping)
                or set(unit) != expected_unit_keys
                or not isinstance(unit_id, str)
                or external_runner._UNIT_ID_RE.fullmatch(unit_id) is None
                or unit_id in unit_ids
                or unit.get("cohort") != cohort
                or unit.get("ordinal") != index
                or item_ids != expected_items
                or not expected_items
                or not isinstance(item_sha256, list)
                or len(item_sha256) != len(expected_items)
                or any(
                    not isinstance(value, str)
                    or external_runner._SHA256_RE.fullmatch(value) is None
                    for value in item_sha256
                )
                or not isinstance(unit.get("request_sha256"), str)
                or external_runner._SHA256_RE.fullmatch(unit["request_sha256"])
                is None
            ):
                raise external_runner.GmailTemporalExternalRunnerError(
                    "retained verifier partition is invalid"
                )
            unit_ids.append(unit_id)
            partition_rows.append(
                {"unit_id": unit_id, "request_fingerprints": list(item_ids)}
            )
        if (
            len(units)
            != math.ceil(len(request_fingerprints) / batch_size)
            or external_runner.verifier_partition_sha256(partition_rows)
            != attestation.get("request_partition_sha256")
        ):
            raise external_runner.GmailTemporalExternalRunnerError(
                "retained verifier partition is invalid"
            )
        call_entries = list(calls_root.iterdir())
        if {entry.name for entry in call_entries} != set(unit_ids):
            raise external_runner.GmailTemporalExternalRunnerError(
                "retained verifier call inventory is invalid"
            )
        for entry in call_entries:
            external_runner._private_directory(entry)
        call_hashes = external_runner.recompute_call_set_hashes(
            root,
            hmac_key_path,
        )
        if any(
            call_hashes.get(field) != attestation.get(field)
            for field in (
                "invocation_ids",
                "request_set_sha256",
                "response_set_sha256",
                "receipt_set_sha256",
            )
        ):
            raise external_runner.GmailTemporalExternalRunnerError(
                "retained verifier call sets are invalid"
            )
        if (
            attestation.get("protocol_fingerprint") != checkpoint_protocol
            or attestation.get("source_module_sha256")
            != dict(checkpoint_source_hashes)
            or dict(checkpoint_source_hashes) != dict(current_source_hashes)
        ):
            raise external_runner.GmailTemporalExternalRunnerError(
                "retained verifier provenance is invalid"
            )
    except (external_runner.GmailTemporalExternalRunnerError, OSError) as exc:
        raise GmailTemporalHoldoutScoreError(
            "independent invocation attestation is invalid"
        ) from exc
    return {
        "run_root": root,
        "plan_raw": plan_raw,
        "unit_ids": tuple(unit_ids),
        "call_hashes": call_hashes,
    }


def _load_invocation_attestations(
    paths: tuple[Path, Path, Path],
    *,
    key: bytes,
    hmac_key_path: Path,
    adapter_manifest: Mapping[str, Any],
    adapter_manifest_raw: bytes,
    runtime_batches: list[Any],
    checkpoint_raw: tuple[bytes, bytes, bytes],
    checkpoint_protocols: tuple[str, str, str],
    checkpoint_source_hashes: tuple[
        Mapping[str, str], Mapping[str, str], Mapping[str, str]
    ],
    cohort: str,
) -> tuple[tuple[bytes, bytes, bytes], tuple[dict[str, Any], ...]]:
    if len({Path(path).resolve() for path in paths}) != 3:
        raise GmailTemporalHoldoutScoreError(
            "three distinct invocation attestations are required"
        )
    run_roots = tuple(Path(path).parent.resolve() for path in paths)
    if len(set(run_roots)) != 3:
        raise GmailTemporalHoldoutScoreError(
            "three distinct invocation attestations are required"
        )
    adapter_sha256 = _sha256_bytes(adapter_manifest_raw)
    request_count = adapter_manifest.get("request_count")
    page_count = adapter_manifest.get("page_count")
    if (
        not isinstance(request_count, int)
        or isinstance(request_count, bool)
        or request_count < 1
        or page_count != request_count
    ):
        raise GmailTemporalHoldoutScoreError(
            "independent invocation attestation is invalid"
        )
    request_fingerprints = _frozen_request_fingerprints(
        runtime_batches,
        expected_count=request_count,
    )
    frozen_request_sha256 = adapter_manifest.get("source_cohort_requests_sha256")
    try:
        current_source_hashes = external_runner._source_module_hashes()
        current_protocol = external_runner._protocol_fingerprint(
            adapter_manifest_sha256=adapter_sha256,
            frozen_request_sha256=str(frozen_request_sha256),
            source_module_sha256=current_source_hashes,
        )
    except (external_runner.GmailTemporalExternalRunnerError, OSError) as exc:
        raise GmailTemporalHoldoutScoreError(
            "independent invocation attestation is invalid"
        ) from exc
    raws: list[bytes] = []
    evidence: list[dict[str, Any]] = []
    logical_run_ids: set[str] = set()
    invocation_ids: set[str] = set()
    for ordinal, (path, checkpoint, protocol, source_hashes) in enumerate(
        zip(
            paths,
            checkpoint_raw,
            checkpoint_protocols,
            checkpoint_source_hashes,
            strict=True,
        ),
        start=1,
    ):
        try:
            value, raw = external_runner.load_verifier_attestation_v2(
                Path(path),
                key=key,
                adapter_manifest_sha256=adapter_sha256,
                checkpoint_sha256=_sha256_bytes(checkpoint),
                cohort=cohort,
                run_ordinal=ordinal,
                frozen_request_artifact_sha256=str(frozen_request_sha256),
                checkpoint_row_count=request_count,
                retained_run_root=Path(path).parent,
            )
        except (external_runner.GmailTemporalExternalRunnerError, OSError) as exc:
            raise GmailTemporalHoldoutScoreError(
                "independent invocation attestation is invalid"
            ) from exc
        run_id, run_invocation_ids = external_runner.validate_verifier_attestation_v2(
            value,
            key=key,
            adapter_manifest_sha256=adapter_sha256,
            checkpoint_sha256=_sha256_bytes(checkpoint),
            cohort=cohort,
            run_ordinal=ordinal,
            frozen_request_artifact_sha256=str(frozen_request_sha256),
            checkpoint_row_count=request_count,
        )
        if (
            value.get("version") != ATTESTATION_VERSION
            or value.get("frozen_request_count") != request_count
            or value.get("protocol_fingerprint") != protocol
            or protocol != current_protocol
            or value.get("source_module_sha256") != dict(source_hashes)
            or dict(source_hashes) != current_source_hashes
            or run_id in logical_run_ids
            or invocation_ids.intersection(run_invocation_ids)
        ):
            raise GmailTemporalHoldoutScoreError(
                "independent invocation attestation is invalid"
            )
        retained = _retained_run_evidence(
            Path(path).parent,
            key=key,
            hmac_key_path=hmac_key_path,
            attestation=value,
            adapter_manifest=adapter_manifest,
            adapter_manifest_sha256=adapter_sha256,
            request_fingerprints=request_fingerprints,
            checkpoint_protocol=protocol,
            checkpoint_source_hashes=source_hashes,
            current_source_hashes=current_source_hashes,
            cohort=cohort,
            run_ordinal=ordinal,
        )
        logical_run_ids.add(run_id)
        invocation_ids.update(run_invocation_ids)
        raws.append(raw)
        evidence.append(
            {
                **retained,
                "attestation": value,
            }
        )
    return (raws[0], raws[1], raws[2]), tuple(evidence)


def _retained_run_evidence_unchanged(
    evidence: Mapping[str, Any],
    *,
    hmac_key_path: Path,
) -> bool:
    try:
        run_root = Path(evidence["run_root"])
        if external_runner._private_file(run_root / "plan.json") != evidence["plan_raw"]:
            return False
        calls_root = run_root / "calls"
        external_runner._private_directory(calls_root)
        entries = list(calls_root.iterdir())
        if {entry.name for entry in entries} != set(evidence["unit_ids"]):
            return False
        for entry in entries:
            external_runner._private_directory(entry)
        recomputed = external_runner.recompute_call_set_hashes(
            run_root,
            hmac_key_path,
        )
    except (external_runner.GmailTemporalExternalRunnerError, OSError, KeyError):
        return False
    return all(
        recomputed.get(field) == evidence["call_hashes"].get(field)
        for field in (
            "invocation_ids",
            "request_set_sha256",
            "response_set_sha256",
            "receipt_set_sha256",
        )
    )


def _checkpoint_manifest_view(
    rows: list[dict[str, Any]],
) -> tuple[Any, str, dict[str, str]]:
    first = rows[0]
    version = first.get("version")
    protocol = first.get("protocol_fingerprint")
    raw_source_hashes = first.get("source_module_sha256")
    if (
        version != candidate_gold.EXPECTED_CHECKPOINT_VERSION
        or not isinstance(protocol, str)
        or candidate_gold._PROTOCOL_PATTERN.fullmatch(protocol) is None
        or not isinstance(raw_source_hashes, Mapping)
        or set(raw_source_hashes) != candidate_gold._PROVENANCE_MODULE_KEYS
        or any(
            not isinstance(digest, str)
            or candidate_gold._SHA256_PATTERN.fullmatch(digest) is None
            for digest in raw_source_hashes.values()
        )
    ):
        raise GmailTemporalHoldoutScoreError(
            "verifier checkpoint provenance is invalid"
        )
    source_hashes = {
        str(name): str(digest) for name, digest in raw_source_hashes.items()
    }
    for row in rows:
        if (
            row.get("version") != version
            or row.get("protocol_fingerprint") != protocol
            or row.get("source_module_sha256") != source_hashes
        ):
            raise GmailTemporalHoldoutScoreError(
                "verifier checkpoint provenance is incoherent"
            )
    try:
        current_hashes = candidate_gold._current_repo_module_hashes()
    except candidate_gold.CandidateGoldError as exc:
        raise GmailTemporalHoldoutScoreError(
            "current candidate authority is unavailable"
        ) from exc
    if any(source_hashes[name] != digest for name, digest in current_hashes.items()):
        raise GmailTemporalHoldoutScoreError(
            "verifier checkpoint source provenance is stale"
        )
    manifest_view = candidate_gold.RunManifest(
        checkpoint_version=str(version),
        protocol_fingerprint=protocol,
        model=candidate_gold.EXPECTED_MODEL,
        reasoning_effort=candidate_gold.EXPECTED_REASONING_EFFORT,
        source_module_sha256=dict(sorted(source_hashes.items())),
        evaluator_sha256="0" * 64,
        semantic_gold_sha256="0" * 64,
        benchmark_builder_sha256="0" * 64,
        sample_sha256="0" * 64,
        sample_record_count=1,
        checkpoint_sha256="0" * 64,
        checkpoint_row_count=len(rows),
    )
    return manifest_view, protocol, source_hashes


def _load_component(
    path: Path,
    *,
    runtime_batches: list[Any],
    pages: Mapping[str, tuple[Any, Any]],
) -> tuple[Any, bytes, str, dict[str, str]]:
    raw, rows = _load_checkpoint_rows(path)
    manifest_view, protocol, source_hashes = _checkpoint_manifest_view(rows)
    try:
        raw_verdicts, _effective = candidate_gold._checkpoint_verdicts(
            rows,
            runtime_batches,
            pages,
            manifest_view,
        )
    except candidate_gold.CandidateGoldError as exc:
        raise GmailTemporalHoldoutScoreError(
            "verifier checkpoint does not cover the current frontier"
        ) from exc
    component = ensemble._ComponentRun(
        manifest=manifest_view,
        rows_by_page={str(row["page_fingerprint"]): row for row in rows},
        raw_verdicts=raw_verdicts,
        checkpoint_sha256=_sha256_bytes(raw),
        manifest_sha256="0" * 64,
        authority_fingerprint=ensemble._checkpoint_authority_fingerprint(rows),
    )
    return component, raw, protocol, source_hashes


def _production_consensus(
    runtime_batches: list[Any],
    candidates: Mapping[str, Any],
    cluster_to_candidates: Mapping[str, set[str]],
    components: tuple[Any, Any, Any],
) -> tuple[dict[str, str], dict[str, str], int, str, str, str]:
    raw_consensus: dict[str, str] = {}
    effective_consensus: dict[str, str] = {}
    reviewed_clusters: set[str] = set()
    versions: set[str] = set()
    policy_versions: set[str] = set()
    policy_fingerprints: set[str] = set()
    for runtime_batch in runtime_batches:
        if not runtime_batch.pages:
            continue
        page_plan = plan_gmail_temporal_candidate_pages(
            analysis=runtime_batch.analysis,
            batch=runtime_batch.batch,
            max_clusters_per_page=4,
            max_candidates_per_page=12,
            max_payload_bytes=12_000,
        )
        result = validate_gmail_temporal_candidate_ensemble_verdict_set(
            analysis=runtime_batch.analysis,
            batch=runtime_batch.batch,
            plan=page_plan,
            runs=tuple(
                ensemble._typed_rows_for_batch(component, runtime_batch)
                for component in components
            ),
        )
        versions.add(result.version)
        policy_versions.add(result.policy_version)
        policy_fingerprints.add(result.policy_fingerprint)
        for review in result.cluster_reviews:
            if (
                review.cluster_id not in cluster_to_candidates
                or review.cluster_id in reviewed_clusters
            ):
                raise GmailTemporalHoldoutScoreError(
                    "production consensus review authority is invalid"
                )
            reviewed_clusters.add(review.cluster_id)
        for row in result.consensus_rows:
            for verdict in row.verdicts:
                if verdict.candidate_id in raw_consensus:
                    raise GmailTemporalHoldoutScoreError(
                        "production consensus repeats a candidate"
                    )
                raw_consensus[verdict.candidate_id] = verdict.verdict
        supported = set(result.verdict_set.supported_candidate_ids)
        uncertain = {
            candidate_id
            for uncertainty in result.verdict_set.uncertain_clusters
            for candidate_id in uncertainty.plausible_candidate_ids
        }
        for runtime_candidate in runtime_batch.candidates:
            candidate_id = runtime_candidate.candidate.candidate_id
            effective_consensus[candidate_id] = (
                "supported"
                if candidate_id in supported
                else "uncertain"
                if candidate_id in uncertain
                else "unsupported"
            )
    expected = set(candidates)
    if set(raw_consensus) != expected or set(effective_consensus) != expected:
        raise GmailTemporalHoldoutScoreError(
            "production consensus does not cover the candidate authority"
        )
    expected_policy_fingerprint = gmail_temporal_candidate_ensemble_policy_fingerprint()
    if (
        len(versions) != 1
        or len(policy_versions) != 1
        or policy_fingerprints != {expected_policy_fingerprint}
    ):
        raise GmailTemporalHoldoutScoreError(
            "production consensus policy provenance is incoherent"
        )
    return (
        raw_consensus,
        effective_consensus,
        len(reviewed_clusters),
        next(iter(versions)),
        next(iter(policy_versions)),
        expected_policy_fingerprint,
    )


def _minimum_pairwise_agreement(components: tuple[Any, Any, Any]) -> float:
    candidate_ids = tuple(sorted(components[0].raw_verdicts))
    values: list[float] = []
    for first, second in ((0, 1), (0, 2), (1, 2)):
        matches = sum(
            components[first].raw_verdicts[candidate_id]
            == components[second].raw_verdicts[candidate_id]
            for candidate_id in candidate_ids
        )
        values.append(matches / len(candidate_ids) if candidate_ids else 1.0)
    return min(values)


def _wilson_interval(numerator: int, denominator: int) -> dict[str, Any]:
    if (
        isinstance(numerator, bool)
        or isinstance(denominator, bool)
        or numerator < 0
        or denominator < 0
        or numerator > denominator
    ):
        raise GmailTemporalHoldoutScoreError("metric count is invalid")
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


def _source_lifecycle_strata(sample: Mapping[str, Any]) -> dict[str, bool]:
    """Classify frozen source evidence without consulting model predictions."""

    mentions = sample.get("mentions")
    expressions = sample.get("expressions")
    if not isinstance(mentions, list) or not isinstance(expressions, list):
        raise GmailTemporalHoldoutScoreError(
            "candidate-gold lifecycle source authority is invalid"
        )
    roles = {
        str(mention.get("lifecycle_role"))
        for mention in mentions
        if isinstance(mention, Mapping)
        and mention.get("mention_type") == "lifecycle"
        and isinstance(mention.get("lifecycle_role"), str)
    }
    timezone_sensitive = False
    for expression in expressions:
        if not isinstance(expression, Mapping):
            raise GmailTemporalHoldoutScoreError(
                "candidate-gold lifecycle source authority is invalid"
            )
        local_time = expression.get("local_time")
        if local_time is not None and not isinstance(local_time, Mapping):
            raise GmailTemporalHoldoutScoreError(
                "candidate-gold lifecycle source authority is invalid"
            )
        timezone_basis = (
            local_time.get("timezone_basis")
            if isinstance(local_time, Mapping)
            else expression.get("timezone_basis")
        )
        blockers = expression.get("blockers")
        if isinstance(timezone_basis, str) and timezone_basis not in {
            "",
            "none",
            "not_applicable",
        }:
            timezone_sensitive = True
        if isinstance(blockers, list) and any(
            isinstance(blocker, str) and "timezone" in blocker for blocker in blockers
        ):
            timezone_sensitive = True
    return {
        "reschedule": any(role.startswith("rescheduled") for role in roles),
        "cancellation": "cancelled" in roles,
        "completion": "completed" in roles,
        "timezone": timezone_sensitive,
    }


def _source_gold_member_categories(
    *,
    samples: list[dict[str, Any]],
    units: tuple[Any, ...],
) -> dict[tuple[str, str, str], frozenset[str]]:
    """Bind lifecycle categories to the specific source-gold semantic member."""

    compiled_member_keys = {
        member.key for unit in units for member in getattr(unit, "members", ())
    }
    output: dict[tuple[str, str, str], frozenset[str]] = {}
    for sample in samples:
        sample_id = sample.get("sample_id")
        gold = sample.get("gold")
        semantic_units = (
            gold.get("semantic_units") if isinstance(gold, Mapping) else None
        )
        if not isinstance(sample_id, str) or not isinstance(semantic_units, list):
            raise GmailTemporalHoldoutScoreError(
                "challenge lifecycle source-gold coverage is invalid"
            )
        for unit in semantic_units:
            unit_id = unit.get("unit_id") if isinstance(unit, Mapping) else None
            members = unit.get("members") if isinstance(unit, Mapping) else None
            if not isinstance(unit_id, str) or not isinstance(members, list):
                raise GmailTemporalHoldoutScoreError(
                    "challenge lifecycle source-gold coverage is invalid"
                )
            for member in members:
                member_id = (
                    member.get("member_id") if isinstance(member, Mapping) else None
                )
                alternatives = (
                    member.get("alternatives") if isinstance(member, Mapping) else None
                )
                member_key = (sample_id, unit_id, str(member_id))
                if (
                    not isinstance(member_id, str)
                    or not isinstance(alternatives, list)
                    or member_key in output
                    or member_key not in compiled_member_keys
                ):
                    raise GmailTemporalHoldoutScoreError(
                        "challenge lifecycle source-gold coverage is invalid"
                    )
                categories: set[str] = set()
                for alternative in alternatives:
                    locator = (
                        alternative.get("locator")
                        if isinstance(alternative, Mapping)
                        else None
                    )
                    if not isinstance(locator, Mapping):
                        raise GmailTemporalHoldoutScoreError(
                            "challenge lifecycle source-gold coverage is invalid"
                        )
                    lifecycle_mention = locator.get("lifecycle_mention")
                    derived = locator.get("derived")
                    if lifecycle_mention is not None and not isinstance(
                        lifecycle_mention,
                        Mapping,
                    ):
                        raise GmailTemporalHoldoutScoreError(
                            "challenge lifecycle source-gold coverage is invalid"
                        )
                    if not isinstance(derived, Mapping):
                        raise GmailTemporalHoldoutScoreError(
                            "challenge lifecycle source-gold coverage is invalid"
                        )
                    roles = {
                        str(
                            lifecycle_mention.get("lifecycle_role", "")
                            if isinstance(lifecycle_mention, Mapping)
                            else ""
                        ).lower(),
                        str(derived.get("lifecycle", "")).lower(),
                    }
                    if any("resched" in role for role in roles):
                        categories.add("reschedule")
                    if any("cancel" in role for role in roles):
                        categories.add("cancellation")
                    if any("complet" in role for role in roles):
                        categories.add("completion")
                    normalized = derived.get("normalized_value")
                    if isinstance(normalized, str) and re.search(
                        r"(?:Z|[+-]\d\d:\d\d|\b(?:UTC|GMT|P[SD]T)\b)",
                        normalized,
                    ):
                        categories.add("timezone")
                output[member_key] = frozenset(categories)
    if set(output) != compiled_member_keys:
        raise GmailTemporalHoldoutScoreError(
            "challenge lifecycle source-gold coverage is invalid"
        )
    return output


def _challenge_lifecycle_source_gold_metrics(
    *,
    samples: list[dict[str, Any]],
    units: tuple[Any, ...],
    matched_effective_member_keys: set[tuple[str, str, str]],
    matched_confirmed_member_keys: set[tuple[str, str, str]],
) -> tuple[dict[str, Any], bool]:
    """Score non-pooled challenge lifecycle recall against frozen source gold."""

    categories_by_member = _source_gold_member_categories(
        samples=samples,
        units=units,
    )
    member_keys_by_category: dict[str, set[tuple[str, str, str]]] = {
        category: set() for category in CHALLENGE_LIFECYCLE_REQUIRED_CATEGORIES
    }
    confirmable_keys_by_category: dict[str, set[tuple[str, str, str]]] = {
        category: set() for category in CHALLENGE_LIFECYCLE_REQUIRED_CATEGORIES
    }
    all_member_keys: set[tuple[str, str, str]] = set()
    for unit in units:
        unit_key = getattr(unit, "key", None)
        members = getattr(unit, "members", None)
        if (
            not isinstance(unit_key, tuple)
            or len(unit_key) != 2
            or not isinstance(members, tuple)
        ):
            raise GmailTemporalHoldoutScoreError(
                "challenge lifecycle source-gold coverage is invalid"
            )
        for member in members:
            member_key = getattr(member, "key", None)
            expected_verdict = getattr(member, "expected_verdict", None)
            if (
                not isinstance(member_key, tuple)
                or len(member_key) != 3
                or member_key in all_member_keys
                or expected_verdict not in {"supported", "uncertain"}
            ):
                raise GmailTemporalHoldoutScoreError(
                    "challenge lifecycle source-gold coverage is invalid"
                )
            all_member_keys.add(member_key)
            for category in categories_by_member[member_key]:
                member_keys_by_category[category].add(member_key)
                if expected_verdict == "supported":
                    confirmable_keys_by_category[category].add(member_key)

    if not matched_effective_member_keys.issubset(
        all_member_keys
    ) or not matched_confirmed_member_keys.issubset(all_member_keys):
        raise GmailTemporalHoldoutScoreError(
            "challenge lifecycle matched-member authority is invalid"
        )

    categories_output: dict[str, Any] = {}
    present_category_count = 0
    passing_category_count = 0
    for category in CHALLENGE_LIFECYCLE_REQUIRED_CATEGORIES:
        member_keys = member_keys_by_category[category]
        confirmable_keys = confirmable_keys_by_category[category]
        effective = _wilson_interval(
            len(member_keys & matched_effective_member_keys),
            len(member_keys),
        )
        confirmed = _wilson_interval(
            len(confirmable_keys & matched_confirmed_member_keys),
            len(confirmable_keys),
        )
        category_present = effective["denominator"] > 0
        confirmed_denominator_present = confirmed["denominator"] > 0
        effective_gate_passed = bool(
            effective["estimate"] is not None
            and effective["estimate"] >= CHALLENGE_LIFECYCLE_MIN_EFFECTIVE_RECALL
        )
        confirmed_gate_passed = (
            bool(
                confirmed["estimate"] is not None
                and confirmed["estimate"] >= CHALLENGE_LIFECYCLE_MIN_CONFIRMED_RECALL
            )
            if confirmed_denominator_present
            else None
        )
        category_gate_passed = bool(
            category_present
            and effective_gate_passed
            and confirmed_gate_passed is not False
        )
        present_category_count += int(category_present)
        passing_category_count += int(category_gate_passed)
        effective_low_n = bool(
            0 < effective["denominator"] < CHALLENGE_LIFECYCLE_LOW_N_MEMBER_THRESHOLD
        )
        confirmed_low_n = bool(
            0 < confirmed["denominator"] < CHALLENGE_LIFECYCLE_LOW_N_MEMBER_THRESHOLD
        )
        categories_output[category] = {
            "category_present": category_present,
            "confirmed_denominator_present": confirmed_denominator_present,
            "source_gold_members": effective["denominator"],
            "source_gold_confirmable_members": confirmed["denominator"],
            "effective_recall": effective,
            "confirmed_recall": confirmed,
            "effective_gate_passed": effective_gate_passed,
            "confirmed_gate_applicable": confirmed_denominator_present,
            "confirmed_gate_passed": confirmed_gate_passed,
            "minimum_effective_recall": (CHALLENGE_LIFECYCLE_MIN_EFFECTIVE_RECALL),
            "minimum_confirmed_recall": (CHALLENGE_LIFECYCLE_MIN_CONFIRMED_RECALL),
            "effective_low_n": effective_low_n,
            "confirmed_low_n": confirmed_low_n,
            "low_n": effective_low_n or confirmed_low_n,
            "gate_passed": category_gate_passed,
        }

    all_required_categories_present = present_category_count == len(
        CHALLENGE_LIFECYCLE_REQUIRED_CATEGORIES
    )
    all_category_gates_passed = passing_category_count == len(
        CHALLENGE_LIFECYCLE_REQUIRED_CATEGORIES
    )
    output = {
        "version": CHALLENGE_LIFECYCLE_SOURCE_GOLD_METRICS_VERSION,
        "scope": "challenge_only_non_population_stress_recall",
        "source_gold_category_authority": (
            "frozen_source_gold_semantic_member_locators"
        ),
        "metrics_must_not_be_pooled_with_primary": True,
        "minimum_effective_recall": CHALLENGE_LIFECYCLE_MIN_EFFECTIVE_RECALL,
        "minimum_confirmed_recall": CHALLENGE_LIFECYCLE_MIN_CONFIRMED_RECALL,
        "low_n_member_threshold": CHALLENGE_LIFECYCLE_LOW_N_MEMBER_THRESHOLD,
        "required_category_count": len(CHALLENGE_LIFECYCLE_REQUIRED_CATEGORIES),
        "present_category_count": present_category_count,
        "passing_category_count": passing_category_count,
        "all_required_categories_present": all_required_categories_present,
        "all_category_gates_passed": all_category_gates_passed,
        "categories": categories_output,
    }
    return output, bool(all_required_categories_present and all_category_gates_passed)


def _validated_challenge_lifecycle_source_gold_gate(value: Any) -> bool:
    """Validate published aggregate lifecycle metrics without source identities."""

    if not isinstance(value, Mapping):
        raise GmailTemporalHoldoutScoreError(
            "challenge lifecycle source-gold metrics are invalid"
        )
    categories = value.get("categories")
    if (
        value.get("version") != CHALLENGE_LIFECYCLE_SOURCE_GOLD_METRICS_VERSION
        or value.get("scope") != "challenge_only_non_population_stress_recall"
        or value.get("source_gold_category_authority")
        != "frozen_source_gold_semantic_member_locators"
        or value.get("metrics_must_not_be_pooled_with_primary") is not True
        or value.get("minimum_effective_recall")
        != CHALLENGE_LIFECYCLE_MIN_EFFECTIVE_RECALL
        or value.get("minimum_confirmed_recall")
        != CHALLENGE_LIFECYCLE_MIN_CONFIRMED_RECALL
        or value.get("low_n_member_threshold")
        != CHALLENGE_LIFECYCLE_LOW_N_MEMBER_THRESHOLD
        or value.get("required_category_count")
        != len(CHALLENGE_LIFECYCLE_REQUIRED_CATEGORIES)
        or not isinstance(categories, Mapping)
        or set(categories) != set(CHALLENGE_LIFECYCLE_REQUIRED_CATEGORIES)
    ):
        raise GmailTemporalHoldoutScoreError(
            "challenge lifecycle source-gold metrics are invalid"
        )

    present_count = 0
    passing_count = 0
    for category in CHALLENGE_LIFECYCLE_REQUIRED_CATEGORIES:
        raw = categories[category]
        if not isinstance(raw, Mapping):
            raise GmailTemporalHoldoutScoreError(
                "challenge lifecycle source-gold metrics are invalid"
            )
        effective = raw.get("effective_recall")
        confirmed = raw.get("confirmed_recall")
        if not isinstance(effective, Mapping) or not isinstance(confirmed, Mapping):
            raise GmailTemporalHoldoutScoreError(
                "challenge lifecycle source-gold metrics are invalid"
            )
        effective_numerator = effective.get("numerator")
        effective_denominator = effective.get("denominator")
        confirmed_numerator = confirmed.get("numerator")
        confirmed_denominator = confirmed.get("denominator")
        if any(
            not isinstance(item, int) or isinstance(item, bool)
            for item in (
                effective_numerator,
                effective_denominator,
                confirmed_numerator,
                confirmed_denominator,
            )
        ):
            raise GmailTemporalHoldoutScoreError(
                "challenge lifecycle source-gold metrics are invalid"
            )
        expected_effective = _wilson_interval(
            effective_numerator,
            effective_denominator,
        )
        expected_confirmed = _wilson_interval(
            confirmed_numerator,
            confirmed_denominator,
        )
        category_present = effective_denominator > 0
        confirmed_denominator_present = confirmed_denominator > 0
        effective_low_n = bool(
            0 < effective_denominator < CHALLENGE_LIFECYCLE_LOW_N_MEMBER_THRESHOLD
        )
        confirmed_low_n = bool(
            0 < confirmed_denominator < CHALLENGE_LIFECYCLE_LOW_N_MEMBER_THRESHOLD
        )
        effective_gate_passed = bool(
            expected_effective["estimate"] is not None
            and expected_effective["estimate"]
            >= CHALLENGE_LIFECYCLE_MIN_EFFECTIVE_RECALL
        )
        confirmed_gate_passed = (
            bool(
                expected_confirmed["estimate"] is not None
                and expected_confirmed["estimate"]
                >= CHALLENGE_LIFECYCLE_MIN_CONFIRMED_RECALL
            )
            if confirmed_denominator_present
            else None
        )
        category_gate_passed = bool(
            category_present
            and effective_gate_passed
            and confirmed_gate_passed is not False
        )
        if (
            dict(effective) != expected_effective
            or dict(confirmed) != expected_confirmed
            or raw.get("category_present") is not category_present
            or raw.get("confirmed_denominator_present")
            is not confirmed_denominator_present
            or raw.get("source_gold_members") != effective_denominator
            or raw.get("source_gold_confirmable_members") != confirmed_denominator
            or raw.get("minimum_effective_recall")
            != CHALLENGE_LIFECYCLE_MIN_EFFECTIVE_RECALL
            or raw.get("minimum_confirmed_recall")
            != CHALLENGE_LIFECYCLE_MIN_CONFIRMED_RECALL
            or raw.get("effective_gate_passed") is not effective_gate_passed
            or raw.get("confirmed_gate_applicable") is not confirmed_denominator_present
            or raw.get("confirmed_gate_passed") is not confirmed_gate_passed
            or raw.get("effective_low_n") is not effective_low_n
            or raw.get("confirmed_low_n") is not confirmed_low_n
            or raw.get("low_n") is not (effective_low_n or confirmed_low_n)
            or raw.get("gate_passed") is not category_gate_passed
        ):
            raise GmailTemporalHoldoutScoreError(
                "challenge lifecycle source-gold metrics are invalid"
            )
        present_count += int(category_present)
        passing_count += int(category_gate_passed)

    all_present = present_count == len(CHALLENGE_LIFECYCLE_REQUIRED_CATEGORIES)
    all_passing = passing_count == len(CHALLENGE_LIFECYCLE_REQUIRED_CATEGORIES)
    if (
        value.get("present_category_count") != present_count
        or value.get("passing_category_count") != passing_count
        or value.get("all_required_categories_present") is not all_present
        or value.get("all_category_gates_passed") is not all_passing
    ):
        raise GmailTemporalHoldoutScoreError(
            "challenge lifecycle source-gold metrics are invalid"
        )
    return bool(all_present and all_passing)


def _owner_audit_artifacts(
    *,
    samples: list[dict[str, Any]],
    runtime_batches: list[Any],
    candidates: Mapping[str, Any],
    units: tuple[Any, ...],
    effective_verdicts: Mapping[str, str],
    detailed_gold: Mapping[str, Any],
    cohort: str,
    diagnostic_only: bool,
) -> tuple[bytes, bytes, dict[str, int], int]:
    """Build owner-only exact audit population and error ledgers."""

    artifacts = candidate_gold._production_artifacts(
        runtime_batches,
        candidates,
        effective_verdicts,
    )
    artifact_scores = candidate_gold._match_production_artifacts(
        artifacts,
        units,
        candidates,
    )
    artifacts_by_id = {artifact.artifact_id: artifact for artifact in artifacts}
    invalid_artifact_ids = set(artifact_scores["invalid_artifact_ids"])
    redundant_artifact_ids = set(artifact_scores["redundant_artifact_ids"])
    false_positive_artifact_ids = invalid_artifact_ids | redundant_artifact_ids
    missed_member_keys = set(artifact_scores["missed_member_keys"])
    raw_critical_candidates = detailed_gold.get("critical_calibration_error_candidates")
    if not isinstance(raw_critical_candidates, list) or any(
        not isinstance(candidate_id, str) for candidate_id in raw_critical_candidates
    ):
        raise GmailTemporalHoldoutScoreError(
            "candidate-gold owner audit authority is invalid"
        )
    critical_candidate_ids = set(raw_critical_candidates)
    if not critical_candidate_ids.issubset(candidates):
        raise GmailTemporalHoldoutScoreError(
            "candidate-gold owner audit authority is invalid"
        )

    sample_by_id = {str(sample["sample_id"]): sample for sample in samples}
    thread_by_sample = {
        sample_id: str(sample["thread_id"])
        for sample_id, sample in sample_by_id.items()
    }
    candidate_ids_by_sample: dict[str, set[str]] = {
        sample_id: set() for sample_id in sample_by_id
    }
    for candidate_id, runtime in candidates.items():
        if runtime.sample_id not in candidate_ids_by_sample:
            raise GmailTemporalHoldoutScoreError(
                "candidate-gold owner audit coverage is invalid"
            )
        candidate_ids_by_sample[runtime.sample_id].add(candidate_id)

    artifact_ids_by_sample: dict[str, set[str]] = {
        sample_id: set() for sample_id in sample_by_id
    }
    artifact_sample: dict[str, str] = {}
    for artifact in artifacts:
        sample_ids = {
            candidates[candidate_id].sample_id
            for candidate_id in artifact.candidate_ids
        }
        if len(sample_ids) != 1:
            raise GmailTemporalHoldoutScoreError(
                "production artifact crosses holdout records"
            )
        sample_id = next(iter(sample_ids))
        artifact_sample[artifact.artifact_id] = sample_id
        artifact_ids_by_sample[sample_id].add(artifact.artifact_id)

    missed_by_sample: dict[str, set[tuple[str, str, str]]] = {
        sample_id: set() for sample_id in sample_by_id
    }
    for member_key in missed_member_keys:
        sample_id = member_key[0]
        if sample_id not in missed_by_sample:
            raise GmailTemporalHoldoutScoreError(
                "missed member crosses holdout authority"
            )
        missed_by_sample[sample_id].add(member_key)

    population_rows: list[dict[str, Any]] = []
    critical_samples = {
        candidates[candidate_id].sample_id for candidate_id in critical_candidate_ids
    }
    for sample in samples:
        sample_id = str(sample["sample_id"])
        gold = sample.get("gold")
        source_label_row_sha256 = sample.get("source_label_row_sha256")
        completed_label_row_sha256 = sample.get("completed_label_row_sha256")
        gold_label_row_sha256 = sample.get("gold_label_row_sha256")
        if (
            not isinstance(gold, Mapping)
            or not isinstance(source_label_row_sha256, str)
            or finalizer._SHA256_PATTERN.fullmatch(source_label_row_sha256) is None
            or not isinstance(completed_label_row_sha256, str)
            or finalizer._SHA256_PATTERN.fullmatch(completed_label_row_sha256) is None
            or not isinstance(gold_label_row_sha256, str)
            or finalizer._SHA256_PATTERN.fullmatch(gold_label_row_sha256) is None
            or completed_label_row_sha256 != gold_label_row_sha256
        ):
            raise GmailTemporalHoldoutScoreError(
                "candidate-gold owner audit source binding is invalid"
            )
        expected_material = gold.get("expected_material")
        expected_filter = gold.get("expected_filter")
        hard_negative = gold.get("hard_negative", False)
        if (
            not isinstance(expected_material, bool)
            or expected_filter not in {"should_admit", "should_suppress"}
            or not isinstance(hard_negative, bool)
        ):
            raise GmailTemporalHoldoutScoreError(
                "candidate-gold owner audit labels are invalid"
            )
        sample_candidate_ids = candidate_ids_by_sample[sample_id]
        sample_artifact_ids = artifact_ids_by_sample[sample_id]
        lifecycle_strata = _source_lifecycle_strata(sample)
        population_rows.append(
            {
                "version": OWNER_AUDIT_POPULATION_VERSION,
                "cohort": cohort,
                "diagnostic_only": diagnostic_only,
                "sample_id": sample_id,
                "thread_id": thread_by_sample[sample_id],
                "source_label_row_sha256": source_label_row_sha256,
                "completed_label_row_sha256": completed_label_row_sha256,
                "gold_label_row_sha256": gold_label_row_sha256,
                "expected_material": expected_material,
                "expected_filter": expected_filter,
                "hard_negative": hard_negative,
                "supported_artifact": any(
                    effective_verdicts[candidate_id] == "supported"
                    for candidate_id in sample_candidate_ids
                ),
                "uncertain_sidecar": any(
                    effective_verdicts[candidate_id] == "uncertain"
                    for candidate_id in sample_candidate_ids
                ),
                "accepted_artifact": any(
                    effective_verdicts[candidate_id] in {"supported", "uncertain"}
                    for candidate_id in sample_candidate_ids
                ),
                "false_negative": bool(missed_by_sample[sample_id]),
                "critical_calibration_error": sample_id in critical_samples,
                "false_positive_artifact": bool(
                    sample_artifact_ids & false_positive_artifact_ids
                ),
                "unmatched_artifact": bool(sample_artifact_ids & invalid_artifact_ids),
                "lifecycle_reschedule": lifecycle_strata["reschedule"],
                "lifecycle_cancellation": lifecycle_strata["cancellation"],
                "lifecycle_completion": lifecycle_strata["completion"],
                "timezone_sensitive": lifecycle_strata["timezone"],
                "routable": False,
            }
        )

    error_rows: list[dict[str, Any]] = []

    def _candidate_semantics(candidate_ids: tuple[str, ...]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for candidate_id in candidate_ids:
            candidate = candidates[candidate_id].candidate
            output.append(
                {
                    "candidate_id": candidate_id,
                    "expression_id": candidate.expression_id,
                    "subject_mention_id": candidate.subject_mention_id,
                    "lifecycle_mention_id": candidate.lifecycle_mention_id,
                    "relation": candidate.relation,
                    "kind": candidate.kind,
                    "lifecycle": candidate.lifecycle,
                    "normalized_value": candidate.normalized_value,
                    "requires_defer": candidate.requires_defer,
                    "blockers": list(candidate.blockers),
                    "risk_features": list(candidate.risk_features),
                    "repair_flags": list(candidate.repair_flags),
                }
            )
        return output

    def _append_error(
        *,
        category: str,
        sample_id: str,
        unit_id: str | None = None,
        member_id: str | None = None,
        artifact_id: str | None = None,
        candidate_id: str | None = None,
        artifact_kind: str | None = None,
        candidate_ids: tuple[str, ...] = (),
        critical: bool,
    ) -> None:
        identity = {
            "cohort": cohort,
            "category": category,
            "sample_id": sample_id,
            "unit_id": unit_id,
            "member_id": member_id,
            "artifact_id": artifact_id,
            "candidate_id": candidate_id,
        }
        sample = sample_by_id[sample_id]
        semantic_candidate_ids = (
            candidate_ids
            if candidate_ids
            else (candidate_id,)
            if candidate_id is not None
            else ()
        )
        error_rows.append(
            {
                "version": OWNER_AUDIT_ERROR_VERSION,
                "error_id": "gtae_" + _sha256_bytes(_canonical_json(identity)),
                "cohort": cohort,
                "diagnostic_only": diagnostic_only,
                "category": category,
                "sample_id": sample_id,
                "thread_id": thread_by_sample[sample_id],
                "source_label_row_sha256": sample["source_label_row_sha256"],
                "completed_label_row_sha256": sample["completed_label_row_sha256"],
                "gold_label_row_sha256": sample["gold_label_row_sha256"],
                "unit_id": unit_id,
                "member_id": member_id,
                "artifact_id": artifact_id,
                "artifact_kind": artifact_kind,
                "candidate_id": candidate_id,
                "candidate_ids": list(candidate_ids),
                "candidate_semantics": _candidate_semantics(semantic_candidate_ids),
                "critical": critical,
                "routable": False,
            }
        )

    for candidate_id in sorted(critical_candidate_ids):
        _append_error(
            category="critical_calibration_error",
            sample_id=candidates[candidate_id].sample_id,
            candidate_id=candidate_id,
            critical=True,
        )
    for sample_id, unit_id, member_id in sorted(missed_member_keys):
        _append_error(
            category="false_negative_member",
            sample_id=sample_id,
            unit_id=unit_id,
            member_id=member_id,
            critical=False,
        )
    for artifact_id in sorted(invalid_artifact_ids):
        artifact = artifacts_by_id[artifact_id]
        sample_id = artifact_sample[artifact_id]
        is_critical = (
            artifact.kind == "supported_citation"
            and not set(artifact.candidate_ids) & critical_candidate_ids
        )
        _append_error(
            category="unmatched_artifact",
            sample_id=sample_id,
            artifact_id=artifact_id,
            artifact_kind=artifact.kind,
            candidate_ids=artifact.candidate_ids,
            critical=is_critical,
        )
    for artifact_id in sorted(false_positive_artifact_ids):
        artifact = artifacts_by_id[artifact_id]
        sample_id = artifact_sample[artifact_id]
        _append_error(
            category="false_positive_artifact",
            sample_id=sample_id,
            artifact_id=artifact_id,
            artifact_kind=artifact.kind,
            candidate_ids=artifact.candidate_ids,
            # Invalid artifacts also receive the unmatched-artifact row above;
            # that primary disposition owns criticality so category membership
            # does not double-count one issue.
            critical=False,
        )

    category_counts = dict(
        sorted(Counter(row["category"] for row in error_rows).items())
    )
    critical_error_count = sum(bool(row["critical"]) for row in error_rows)
    return (
        adapter._jsonl_bytes(population_rows),
        adapter._jsonl_bytes(error_rows),
        category_counts,
        critical_error_count,
    )


def _load_bound_challenge_score(
    root: Path,
    *,
    key: bytes,
    primary_adapter_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Authenticate one separately scored challenge estimand for primary binding."""

    try:
        finalizer._private_directory(root, description="challenge score root")
        entries = list(root.iterdir())
    except (OSError, finalizer.GmailTemporalLabelFinalizerError) as exc:
        raise GmailTemporalHoldoutScoreError(
            "challenge score root is unavailable or unsafe"
        ) from exc
    expected_names = {
        "manifest.json",
        SCORE_ARTIFACT,
        OWNER_AUDIT_POPULATION_ARTIFACT,
        OWNER_AUDIT_ERRORS_ARTIFACT,
    }
    if {entry.name for entry in entries} != expected_names or any(
        entry.is_symlink() or not entry.is_file() for entry in entries
    ):
        raise GmailTemporalHoldoutScoreError("challenge score inventory is not exact")
    raws = {
        name: _private_file(root / name, description="challenge score artifact")
        for name in expected_names
    }
    manifest_raw = raws["manifest.json"]
    score_raw = raws[SCORE_ARTIFACT]
    manifest = _parse_manifest(manifest_raw, description="challenge score manifest")
    score = _parse_manifest(score_raw, description="challenge score")
    authenticator = manifest.get("manifest_hmac_sha256")
    unsigned = dict(manifest)
    unsigned.pop("manifest_hmac_sha256", None)
    expected_authenticator = hmac.new(
        key,
        MANIFEST_DOMAIN + _canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()
    expected_artifacts = {
        SCORE_ARTIFACT: _sha256_bytes(score_raw),
        OWNER_AUDIT_POPULATION_ARTIFACT: _sha256_bytes(
            raws[OWNER_AUDIT_POPULATION_ARTIFACT]
        ),
        OWNER_AUDIT_ERRORS_ARTIFACT: _sha256_bytes(raws[OWNER_AUDIT_ERRORS_ARTIFACT]),
    }
    source_bindings = (
        "source_holdout_manifest_sha256",
        "source_holdout_manifest_hmac_sha256",
        "source_gold_manifest_sha256",
        "source_gold_manifest_hmac_sha256",
        "label_authority_manifest_sha256",
        "release_evidence_class",
        "source_release_scope",
    )
    lifecycle_metrics = score.get("challenge_lifecycle_source_gold_metrics")
    lifecycle_gate_passed = _validated_challenge_lifecycle_source_gold_gate(
        lifecycle_metrics
    )
    if (
        not isinstance(authenticator, str)
        or not hmac.compare_digest(authenticator, expected_authenticator)
        or manifest.get("version") != MANIFEST_VERSION
        or manifest.get("scorer_version") != VERSION
        or manifest.get("cohort") != "challenge"
        or manifest.get("diagnostic_only")
        is not (
            manifest.get("release_scope") == "diagnostic_balanced_capability_stress"
        )
        or manifest.get("challenge_role") != "conditional_capability_stress_gate"
        or manifest.get("estimands_must_not_be_pooled") is not True
        or manifest.get("challenge_required_as_separate_promotion_gate") is not True
        or manifest.get("release_holdout_eligible") is not False
        or manifest.get("release_score_gate_passed") is not False
        or manifest.get("challenge_scoring_pending") is not False
        or manifest.get("promotion_pending") is not True
        or manifest.get("artifact_sha256") != expected_artifacts
        or manifest.get("scorer_sha256") != _sha256_bytes(Path(__file__).read_bytes())
        or manifest.get("candidate_evaluator_sha256")
        != _sha256_bytes(candidate_gold._EVALUATOR_PATH.read_bytes())
        or any(
            manifest.get(field) != primary_adapter_manifest.get(field)
            for field in source_bindings
        )
        or score.get("version") != SCORE_VERSION
        or score.get("status") != "scored"
        or score.get("cohort") != "challenge"
        or score.get("challenge_capability_gate_passed")
        is not manifest.get("challenge_capability_gate_passed")
        or score.get("challenge_base_capability_gate_passed")
        is not manifest.get("challenge_base_capability_gate_passed")
        or score.get("challenge_lifecycle_source_gold_metrics")
        != manifest.get("challenge_lifecycle_source_gold_metrics")
        or score.get("challenge_lifecycle_source_gold_gate_passed")
        is not lifecycle_gate_passed
        or manifest.get("challenge_lifecycle_source_gold_gate_passed")
        is not lifecycle_gate_passed
        or score.get("challenge_capability_gate_passed")
        is not (
            score.get("challenge_base_capability_gate_passed") is True
            and lifecycle_gate_passed
        )
        or score.get("cohort_gate_passed") is not manifest.get("cohort_gate_passed")
        or score.get("owner_audit_critical_error_records")
        != manifest.get("owner_audit_critical_error_record_count")
    ):
        raise GmailTemporalHoldoutScoreError("challenge score authority is invalid")
    gold_metrics = score.get("gold_metrics")
    if not isinstance(gold_metrics, Mapping):
        raise GmailTemporalHoldoutScoreError("challenge score metrics are invalid")
    safety_gates = {
        "challenge_cohort_gate": score.get("cohort_gate_passed") is True,
        "challenge_capability_gate": (
            score.get("challenge_capability_gate_passed") is True
        ),
        "challenge_lifecycle_source_gold_gate": lifecycle_gate_passed,
        "zero_critical_calibration_errors": (
            gold_metrics.get("critical_calibration_error_count") == 0
        ),
        "zero_supported_negative_artifacts": (
            gold_metrics.get("supported_negative_artifacts") == 0
        ),
        "zero_supported_hard_negative_artifacts": (
            gold_metrics.get("supported_hard_negative_artifacts") == 0
        ),
        "zero_owner_audit_critical_errors": (
            score.get("owner_audit_critical_error_records") == 0
        ),
    }
    return {
        "root": root,
        "raws": raws,
        "manifest": manifest,
        "manifest_raw": manifest_raw,
        "score_raw": score_raw,
        "safety_gates": safety_gates,
        "safety_gate_passed": all(safety_gates.values()),
    }


def score_gmail_temporal_holdout(
    adapter_root: Path,
    hmac_key_path: Path,
    checkpoint_paths: tuple[Path, Path, Path],
    attestation_paths: tuple[Path, Path, Path],
    output_root: Path,
    *,
    challenge_score_root: Path | None = None,
) -> dict[str, Any]:
    """Validate exactly three runs and publish authenticated aggregate scores."""

    if len(checkpoint_paths) != 3:
        raise GmailTemporalHoldoutScoreError("exactly three checkpoints are required")
    resolved_checkpoints = {Path(path).resolve() for path in checkpoint_paths}
    if len(resolved_checkpoints) != 3:
        raise GmailTemporalHoldoutScoreError(
            "three distinct checkpoint paths are required"
        )
    try:
        key = finalizer._private_hmac_key(hmac_key_path)
    except finalizer.GmailTemporalLabelFinalizerError as exc:
        raise GmailTemporalHoldoutScoreError(
            "HMAC key is unavailable or unsafe"
        ) from exc
    (
        adapter_manifest,
        adapter_manifest_raw,
        sample_path,
        sample_raw,
        samples,
    ) = _load_adapter_root(Path(adapter_root), key=key)
    cohort = str(adapter_manifest["cohort"])
    if challenge_score_root is not None and cohort != "primary":
        raise GmailTemporalHoldoutScoreError(
            "only a primary score can bind a challenge score"
        )
    bound_challenge = (
        _load_bound_challenge_score(
            Path(challenge_score_root),
            key=key,
            primary_adapter_manifest=adapter_manifest,
        )
        if challenge_score_root is not None
        else None
    )
    try:
        runtime_batches, candidates, pages = candidate_gold._runtime_batches(samples)
        units = candidate_gold._compile_gold(samples, candidates)
    except candidate_gold.CandidateGoldError as exc:
        raise GmailTemporalHoldoutScoreError(
            "candidate-gold frontier is invalid"
        ) from exc
    if not units:
        raise GmailTemporalHoldoutScoreError("candidate-gold has no semantic units")
    try:
        candidate_to_cluster, cluster_to_candidates, _cluster_to_sample = (
            ensemble._candidate_parent_clusters(runtime_batches)
        )
    except ensemble.CandidateEnsembleError as exc:
        raise GmailTemporalHoldoutScoreError(
            "candidate parent-cluster authority is invalid"
        ) from exc
    if set(candidate_to_cluster) != set(candidates):
        raise GmailTemporalHoldoutScoreError("candidate parent clusters are incomplete")
    loaded = tuple(
        _load_component(
            Path(path),
            runtime_batches=runtime_batches,
            pages=pages,
        )
        for path in checkpoint_paths
    )
    components = tuple(item[0] for item in loaded)
    checkpoint_raw = tuple(item[1] for item in loaded)
    protocols = {item[2] for item in loaded}
    source_hash_authorities = [item[3] for item in loaded]
    if len(protocols) != 1 or any(
        authority != source_hash_authorities[0]
        for authority in source_hash_authorities[1:]
    ):
        raise GmailTemporalHoldoutScoreError(
            "checkpoints do not share one provenance authority"
        )
    attestation_raw, attestation_evidence = _load_invocation_attestations(
        attestation_paths,
        key=key,
        hmac_key_path=Path(hmac_key_path),
        adapter_manifest=adapter_manifest,
        adapter_manifest_raw=adapter_manifest_raw,
        runtime_batches=runtime_batches,
        checkpoint_raw=checkpoint_raw,
        checkpoint_protocols=(loaded[0][2], loaded[1][2], loaded[2][2]),
        checkpoint_source_hashes=(
            source_hash_authorities[0],
            source_hash_authorities[1],
            source_hash_authorities[2],
        ),
        cohort=cohort,
    )
    if any(
        component.authority_fingerprint != components[0].authority_fingerprint
        for component in components[1:]
    ):
        raise GmailTemporalHoldoutScoreError(
            "checkpoints do not share one page authority"
        )

    accepted_cluster_sets = ensemble._accepted_parent_cluster_sets(
        components,
        candidate_to_cluster,
    )
    accepted_cluster_pairwise = ensemble._pairwise_jaccard(accepted_cluster_sets)
    accepted_cluster_minimum = ensemble._minimum_jaccard(accepted_cluster_pairwise)
    accepted_cluster_stable = (
        accepted_cluster_minimum >= ensemble.MIN_PAIRWISE_ACCEPTED_CLUSTER_JACCARD
    )
    accepted_member_sets = ensemble._accepted_gold_member_sets(components, units)
    accepted_member_pairwise = ensemble._pairwise_jaccard(accepted_member_sets)
    accepted_member_minimum = ensemble._minimum_jaccard(accepted_member_pairwise)
    accepted_member_stable = (
        accepted_member_minimum >= ensemble.MIN_PAIRWISE_GOLD_MEMBER_JACCARD
    )
    (
        raw_consensus,
        effective_consensus,
        cluster_review_count,
        ensemble_core_version,
        ensemble_policy_version,
        ensemble_policy_fingerprint,
    ) = _production_consensus(
        runtime_batches,
        candidates,
        cluster_to_candidates,
        components,
    )
    try:
        gold = candidate_gold.evaluate(
            sample_path,
            None,
            None,
            prevalidated_verdict_maps=(raw_consensus, effective_consensus),
            provenance_override={
                "single_run": False,
                "evidence_type": "authenticated_holdout_three_run_consensus",
                "component_run_count": 3,
            },
        )
        (
            owner_population_raw,
            owner_errors_raw,
            owner_error_category_counts,
            owner_critical_error_count,
        ) = _owner_audit_artifacts(
            samples=samples,
            runtime_batches=runtime_batches,
            candidates=candidates,
            units=units,
            effective_verdicts=effective_consensus,
            detailed_gold=gold,
            cohort=cohort,
            diagnostic_only=bool(adapter_manifest["diagnostic_only"]),
        )
        challenge_lifecycle_source_gold_metrics = None
        challenge_lifecycle_source_gold_gate_passed = None
        if cohort == "challenge":
            production_artifacts = candidate_gold._production_artifacts(
                runtime_batches,
                candidates,
                effective_consensus,
            )
            effective_artifact_scores = candidate_gold._match_production_artifacts(
                production_artifacts,
                units,
                candidates,
            )
            confirmed_artifacts = tuple(
                artifact
                for artifact in production_artifacts
                if artifact.kind == "supported_citation"
            )
            confirmed_artifact_scores = candidate_gold._match_production_artifacts(
                confirmed_artifacts,
                units,
                candidates,
            )
            (
                challenge_lifecycle_source_gold_metrics,
                challenge_lifecycle_source_gold_gate_passed,
            ) = _challenge_lifecycle_source_gold_metrics(
                samples=samples,
                units=units,
                matched_effective_member_keys=set(
                    effective_artifact_scores["matched_member_keys"]
                ),
                matched_confirmed_member_keys=set(
                    confirmed_artifact_scores["matched_member_keys"]
                ),
            )
        gold.pop("run_provenance", None)
        aggregate_gold = candidate_gold._aggregate_cli_output(gold)
        candidate_gold._assert_cli_aggregate_only(
            aggregate_gold,
            samples=samples,
            candidate_ids=set(candidates),
        )
    except candidate_gold.CandidateGoldError as exc:
        raise GmailTemporalHoldoutScoreError("candidate-gold scoring failed") from exc

    source_release_eligible = bool(adapter_manifest["release_holdout_eligible"])
    source_evidence_class = str(adapter_manifest["release_evidence_class"])
    prospective_release_evidence = (
        cohort == "primary"
        and source_evidence_class == finalizer.PROSPECTIVE_EVIDENCE_CLASS
        and adapter_manifest["prospective_unseen_source_evidence"] is True
        and adapter_manifest["historical_architecture_exposed"] is False
        and adapter_manifest["cohort_evidence_scope"]
        == "prospective_natural_operability_review_only"
    )
    owner_error_record_count = sum(owner_error_category_counts.values())
    supported_artifact_total = int(aggregate_gold["supported_artifacts"])
    supported_artifact_matched = (
        supported_artifact_total
        - int(aggregate_gold["supported_redundant_artifacts"])
        - int(aggregate_gold["supported_unmatched_artifacts"])
    )
    expected_supported_members = sum(
        member.expected_verdict == "supported"
        for unit in units
        for member in unit.members
    )
    review_metrics = aggregate_gold.get("review")
    if not isinstance(review_metrics, Mapping):
        raise GmailTemporalHoldoutScoreError("candidate-gold recall counts are invalid")
    cohort_metric_intervals_95 = {
        "supported_artifact_precision": _wilson_interval(
            supported_artifact_matched,
            supported_artifact_total,
        ),
        "effective_artifact_precision": _wilson_interval(
            int(aggregate_gold["matched_artifacts"]),
            int(aggregate_gold["production_artifacts"]),
        ),
        "accepted_negative_review_rate": _wilson_interval(
            int(aggregate_gold["accepted_negative_review_records"]),
            int(aggregate_gold["negative_records"]),
        ),
        "useful_record_recall": _wilson_interval(
            int(aggregate_gold["recalled_useful_records"]),
            int(aggregate_gold["useful_records"]),
        ),
        "end_to_end_required_member_recall": _wilson_interval(
            int(review_metrics["recalled_members"]),
            int(aggregate_gold["semantic_members"]),
        ),
        "end_to_end_exact_member_recall": _wilson_interval(
            int(review_metrics["exact_members"]),
            int(aggregate_gold["semantic_members"]),
        ),
        "end_to_end_any_unit_recall": _wilson_interval(
            int(review_metrics["any_units"]),
            int(aggregate_gold["semantic_units"]),
        ),
        "end_to_end_complete_unit_recall": _wilson_interval(
            int(review_metrics["complete_units"]),
            int(aggregate_gold["semantic_units"]),
        ),
        "end_to_end_exact_unit_recall": _wilson_interval(
            int(review_metrics["exact_units"]),
            int(aggregate_gold["semantic_units"]),
        ),
        "supported_required_member_recall": _wilson_interval(
            supported_artifact_matched,
            expected_supported_members,
        ),
        "effective_member_recall": _wilson_interval(
            int(aggregate_gold["matched_artifacts"]),
            int(aggregate_gold["semantic_members"]),
        ),
    }
    gold_gates = aggregate_gold.get("gates")
    if not isinstance(gold_gates, Mapping) or any(
        not isinstance(value, bool) for value in gold_gates.values()
    ):
        raise GmailTemporalHoldoutScoreError("candidate-gold gates are invalid")
    if not PRIMARY_OPERABILITY_GATE_NAMES.issubset(gold_gates):
        raise GmailTemporalHoldoutScoreError(
            "candidate-gold operability gates are incomplete"
        )
    primary_operability_gate_passed = all(
        bool(gold_gates[name]) for name in PRIMARY_OPERABILITY_GATE_NAMES
    )
    challenge_base_capability_gate_passed = bool(
        aggregate_gold["candidate_gate_passed"]
    )
    challenge_capability_gate_passed = bool(
        challenge_base_capability_gate_passed
        and challenge_lifecycle_source_gold_gate_passed is True
    )
    cohort_gates = {
        "exact_three_run_consensus": True,
        "independent_invocations_attested": True,
        "current_checkpoint_source_provenance": True,
        "shared_checkpoint_page_authority": True,
        "role_specific_quality_gate": (
            primary_operability_gate_passed
            if cohort == "primary"
            else challenge_base_capability_gate_passed
        ),
        "accepted_parent_cluster_stability": accepted_cluster_stable,
        "gold_semantic_member_stability": accepted_member_stable,
    }
    if cohort == "challenge":
        cohort_gates["challenge_lifecycle_source_gold_gate"] = bool(
            challenge_lifecycle_source_gold_gate_passed
        )
    cohort_gate_passed = all(cohort_gates.values())
    challenge_score_bound = bound_challenge is not None
    challenge_safety_gates = (
        dict(bound_challenge["safety_gates"]) if bound_challenge is not None else None
    )
    challenge_safety_gate_passed = (
        bool(bound_challenge["safety_gate_passed"])
        if bound_challenge is not None
        else False
    )
    two_estimand_gate_passed = bool(
        cohort == "primary"
        and cohort_gate_passed
        and challenge_score_bound
        and challenge_safety_gate_passed
    )
    promotion_prerequisites = {
        "source_holdout_eligible": source_release_eligible,
        "prospective_unseen_source_evidence": prospective_release_evidence,
        "sol_source_only_label_authority_attested": (
            adapter_manifest["sol_label_authority_attested"] is True
            and adapter_manifest["source_only_label_authority_attested"] is True
        ),
        "release_structural_scorability_verified": bool(
            adapter_manifest["release_structural_scorability_verified"]
        ),
        "automatic_apply_disabled": (
            adapter_manifest["automatic_apply_eligible"] is False
        ),
        "content_changing_canary_required": bool(
            adapter_manifest["content_changing_canary_required"]
        ),
        "challenge_score_bound": challenge_score_bound,
        "challenge_safety_gate_passed": challenge_safety_gate_passed,
        "two_estimand_gate_passed": two_estimand_gate_passed,
        "owner_audit_passed": False,
        "retrieval_parity_passed": False,
        "content_changing_canary_passed": False,
    }
    release_score_gate_passed = False
    raw_counts = Counter(raw_consensus.values())
    effective_counts = Counter(effective_consensus.values())
    score = {
        "version": SCORE_VERSION,
        "status": "scored",
        "cohort": cohort,
        "diagnostic_only": bool(adapter_manifest["diagnostic_only"]),
        "estimands_must_not_be_pooled": True,
        "primary_estimand": "natural_mail_population_operability",
        "challenge_estimand": "conditional_temporal_capability_stress_recall",
        "challenge_role": "conditional_capability_stress_gate",
        "challenge_required_as_separate_promotion_gate": True,
        "records": len(samples),
        "semantic_units": len(units),
        "semantic_members": sum(len(unit.members) for unit in units),
        "candidates": len(candidates),
        "pages_per_run": len(pages),
        "component_run_count": 3,
        "component_model_attested": candidate_gold.EXPECTED_MODEL,
        "component_reasoning_effort_attested": (
            candidate_gold.EXPECTED_REASONING_EFFORT
        ),
        "checkpoint_version": candidate_gold.EXPECTED_CHECKPOINT_VERSION,
        "minimum_exact_candidate_agreement": _minimum_pairwise_agreement(components),
        "minimum_accepted_parent_cluster_jaccard": accepted_cluster_minimum,
        "minimum_required_accepted_parent_cluster_jaccard": (
            ensemble.MIN_PAIRWISE_ACCEPTED_CLUSTER_JACCARD
        ),
        "accepted_parent_cluster_stability_passed": accepted_cluster_stable,
        "minimum_gold_semantic_member_jaccard": accepted_member_minimum,
        "minimum_required_gold_semantic_member_jaccard": (
            ensemble.MIN_PAIRWISE_GOLD_MEMBER_JACCARD
        ),
        "gold_semantic_member_stability_passed": accepted_member_stable,
        "raw_supported_consensus": raw_counts["supported"],
        "raw_uncertain_consensus": raw_counts["uncertain"],
        "raw_unsupported_consensus": raw_counts["unsupported"],
        "effective_supported_consensus": effective_counts["supported"],
        "effective_uncertain_consensus": effective_counts["uncertain"],
        "effective_unsupported_consensus": effective_counts["unsupported"],
        "cluster_review_count": cluster_review_count,
        "ensemble_core_version": ensemble_core_version,
        "ensemble_policy_version": ensemble_policy_version,
        "ensemble_policy_fingerprint": ensemble_policy_fingerprint,
        "gold_metrics": aggregate_gold,
        "cohort_metric_intervals_95": cohort_metric_intervals_95,
        "confidence_interval_method": "wilson_score_95_two_sided",
        "metrics_must_not_be_pooled_across_cohorts": True,
        "owner_audit_population_records": len(samples),
        "owner_audit_error_records": owner_error_record_count,
        "owner_audit_error_category_counts": owner_error_category_counts,
        "owner_audit_critical_error_records": owner_critical_error_count,
        "owner_audit_population_version": OWNER_AUDIT_POPULATION_VERSION,
        "owner_audit_error_version": OWNER_AUDIT_ERROR_VERSION,
        "cohort_gates": cohort_gates,
        "cohort_gate_passed": cohort_gate_passed,
        "primary_operability_gate_passed": (
            primary_operability_gate_passed if cohort == "primary" else None
        ),
        "natural_recall_metrics_diagnostic_only": cohort == "primary",
        "challenge_capability_gate_passed": (
            challenge_capability_gate_passed if cohort == "challenge" else None
        ),
        "challenge_score_bound": challenge_score_bound,
        "challenge_safety_gates": challenge_safety_gates,
        "challenge_safety_gate_passed": challenge_safety_gate_passed,
        "two_estimand_gate_passed": two_estimand_gate_passed,
        "promotion_prerequisites": promotion_prerequisites,
        "promotion_pending": True,
        "release_evidence_class": source_evidence_class,
        "release_evidence_class_applies_to": "primary_natural_cohort",
        "cohort_evidence_scope": adapter_manifest["cohort_evidence_scope"],
        "cohort_metrics_must_not_be_pooled": True,
        "challenge_population_inference_eligible": False,
        "development_baseline_cohort_overlap_count": adapter_manifest[
            "development_baseline_cohort_overlap_count"
        ],
        "release_scope": adapter_manifest["release_scope"],
        "retrospective_calibration_eligible": adapter_manifest[
            "retrospective_calibration_eligible"
        ],
        "automatic_apply_eligible": False,
        "content_changing_canary_required": adapter_manifest[
            "content_changing_canary_required"
        ],
        "sol_label_authority_attested": adapter_manifest[
            "sol_label_authority_attested"
        ],
        "source_only_label_authority_attested": adapter_manifest[
            "source_only_label_authority_attested"
        ],
        "label_authority_provenance_cryptographically_verified": False,
        "source_release_holdout_eligible": source_release_eligible,
        "release_holdout_eligible": source_release_eligible,
        "release_score_gate_passed": release_score_gate_passed,
        "independent_invocations_attested": True,
        "invocation_provenance_cryptographically_verified": False,
        "distinct_checkpoint_paths_verified": True,
        "ensemble_pending": False,
        "challenge_scoring_pending": cohort == "primary" and not challenge_score_bound,
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    if cohort == "challenge":
        score.update(
            {
                "challenge_base_capability_gate_passed": (
                    challenge_base_capability_gate_passed
                ),
                "challenge_lifecycle_source_gold_metrics": (
                    challenge_lifecycle_source_gold_metrics
                ),
                "challenge_lifecycle_source_gold_gate_passed": (
                    challenge_lifecycle_source_gold_gate_passed
                ),
            }
        )
    # The score itself must remain aggregate-only.  Hashes and opaque protocol
    # authority live only in the protected authenticated manifest.
    try:
        candidate_gold._assert_cli_aggregate_only(
            score,
            samples=samples,
            candidate_ids=set(candidates),
        )
    except candidate_gold.CandidateGoldError as exc:
        raise GmailTemporalHoldoutScoreError(
            "holdout score contains private runtime identity"
        ) from exc
    score_raw = _canonical_json(score) + b"\n"
    scorer_sha256 = _sha256_bytes(Path(__file__).read_bytes())
    score_manifest = {
        "version": MANIFEST_VERSION,
        "scorer_version": VERSION,
        "source_adapter_manifest_sha256": _sha256_bytes(adapter_manifest_raw),
        "source_adapter_manifest_hmac_sha256": adapter_manifest["manifest_hmac_sha256"],
        "source_holdout_manifest_sha256": adapter_manifest[
            "source_holdout_manifest_sha256"
        ],
        "source_holdout_manifest_hmac_sha256": adapter_manifest[
            "source_holdout_manifest_hmac_sha256"
        ],
        "source_gold_manifest_sha256": adapter_manifest["source_gold_manifest_sha256"],
        "source_gold_manifest_hmac_sha256": adapter_manifest[
            "source_gold_manifest_hmac_sha256"
        ],
        "bound_challenge_score_manifest_sha256": (
            _sha256_bytes(bound_challenge["manifest_raw"])
            if bound_challenge is not None
            else None
        ),
        "bound_challenge_score_manifest_hmac_sha256": (
            bound_challenge["manifest"]["manifest_hmac_sha256"]
            if bound_challenge is not None
            else None
        ),
        "bound_challenge_score_sha256": (
            _sha256_bytes(bound_challenge["score_raw"])
            if bound_challenge is not None
            else None
        ),
        "source_candidate_gold_samples_sha256": _sha256_bytes(sample_raw),
        "checkpoint_1_sha256": _sha256_bytes(checkpoint_raw[0]),
        "checkpoint_2_sha256": _sha256_bytes(checkpoint_raw[1]),
        "checkpoint_3_sha256": _sha256_bytes(checkpoint_raw[2]),
        "attestation_1_sha256": _sha256_bytes(attestation_raw[0]),
        "attestation_2_sha256": _sha256_bytes(attestation_raw[1]),
        "attestation_3_sha256": _sha256_bytes(attestation_raw[2]),
        "checkpoint_protocol_fingerprint": next(iter(protocols)),
        "checkpoint_source_module_sha256": dict(
            sorted(source_hash_authorities[0].items())
        ),
        "checkpoint_authority_fingerprint": components[0].authority_fingerprint,
        "candidate_evaluator_sha256": _sha256_bytes(
            candidate_gold._EVALUATOR_PATH.read_bytes()
        ),
        "ensemble_evaluator_sha256": _sha256_bytes(_ENSEMBLE_PATH.read_bytes()),
        "scorer_sha256": scorer_sha256,
        "artifact_sha256": {
            SCORE_ARTIFACT: _sha256_bytes(score_raw),
            OWNER_AUDIT_POPULATION_ARTIFACT: _sha256_bytes(owner_population_raw),
            OWNER_AUDIT_ERRORS_ARTIFACT: _sha256_bytes(owner_errors_raw),
        },
        "owner_audit_population_artifact": OWNER_AUDIT_POPULATION_ARTIFACT,
        "owner_audit_population_version": OWNER_AUDIT_POPULATION_VERSION,
        "owner_audit_population_record_count": len(samples),
        "owner_audit_population_coverage": "exact_selected_cohort_record_order",
        "owner_audit_error_artifact": OWNER_AUDIT_ERRORS_ARTIFACT,
        "owner_audit_error_version": OWNER_AUDIT_ERROR_VERSION,
        "owner_audit_error_record_count": owner_error_record_count,
        "owner_audit_error_category_counts": owner_error_category_counts,
        "owner_audit_critical_error_record_count": owner_critical_error_count,
        "owner_audit_error_categories_are_disjoint": False,
        "invalid_artifacts_emit_false_positive_and_unmatched_memberships": True,
        "critical_count_uses_primary_dispositions_without_overlap": True,
        "owner_audit_error_coverage": (
            "exact_critical_fp_fn_and_unmatched_artifact_identities"
        ),
        "record_count": len(samples),
        "cohort_metric_intervals_95": cohort_metric_intervals_95,
        "confidence_interval_method": "wilson_score_95_two_sided",
        "metrics_must_not_be_pooled_across_cohorts": True,
        "candidate_count": len(candidates),
        "page_count_per_run": len(pages),
        "component_run_count": 3,
        "cohort": cohort,
        "diagnostic_only": bool(adapter_manifest["diagnostic_only"]),
        "estimands_must_not_be_pooled": True,
        "primary_estimand": "natural_mail_population_operability",
        "challenge_estimand": "conditional_temporal_capability_stress_recall",
        "challenge_role": "conditional_capability_stress_gate",
        "challenge_required_as_separate_promotion_gate": True,
        "labeled_cohort_reroll_forbidden": adapter_manifest[
            "labeled_cohort_reroll_forbidden"
        ],
        "freeze_no_reroll_scope": adapter_manifest["freeze_no_reroll_scope"],
        "freeze_authority_independently_reverified_downstream": adapter_manifest[
            "freeze_authority_independently_reverified_downstream"
        ],
        "all_labeled_attempts_must_be_retained": adapter_manifest[
            "all_labeled_attempts_must_be_retained"
        ],
        "underpowered_primary_action": adapter_manifest["underpowered_primary_action"],
        "underpowered_challenge_action": adapter_manifest[
            "underpowered_challenge_action"
        ],
        "release_evidence_class": source_evidence_class,
        "release_evidence_class_applies_to": "primary_natural_cohort",
        "cohort_evidence_scope": adapter_manifest["cohort_evidence_scope"],
        "cohort_metrics_must_not_be_pooled": True,
        "challenge_population_inference_eligible": False,
        "development_baseline_cohort_overlap_count": adapter_manifest[
            "development_baseline_cohort_overlap_count"
        ],
        "source_release_scope": adapter_manifest["source_release_scope"],
        "release_scope": adapter_manifest["release_scope"],
        "prospective_unseen_source_evidence": adapter_manifest[
            "prospective_unseen_source_evidence"
        ],
        "historical_architecture_exposed": adapter_manifest[
            "historical_architecture_exposed"
        ],
        "retrospective_calibration_eligible": adapter_manifest[
            "retrospective_calibration_eligible"
        ],
        "semantic_development_overlap_status": adapter_manifest[
            "semantic_development_overlap_status"
        ],
        "automatic_apply_eligible": False,
        "content_changing_canary_required": adapter_manifest[
            "content_changing_canary_required"
        ],
        "label_authority_manifest_sha256": adapter_manifest[
            "label_authority_manifest_sha256"
        ],
        "label_authority_version": adapter_manifest["label_authority_version"],
        "label_authority_model": adapter_manifest["label_authority_model"],
        "label_authority_reasoning_effort": adapter_manifest[
            "label_authority_reasoning_effort"
        ],
        "label_authority_invocation_count": adapter_manifest[
            "label_authority_invocation_count"
        ],
        "sol_label_authority_attested": adapter_manifest[
            "sol_label_authority_attested"
        ],
        "source_only_label_authority_attested": adapter_manifest[
            "source_only_label_authority_attested"
        ],
        "label_authority_provenance_cryptographically_verified": False,
        "release_structural_scorability_verified": adapter_manifest[
            "release_structural_scorability_verified"
        ],
        "cohort_gate_passed": cohort_gate_passed,
        "primary_operability_gate_passed": (
            primary_operability_gate_passed if cohort == "primary" else None
        ),
        "natural_recall_metrics_diagnostic_only": cohort == "primary",
        "challenge_capability_gate_passed": (
            challenge_capability_gate_passed if cohort == "challenge" else None
        ),
        "challenge_score_bound": challenge_score_bound,
        "challenge_safety_gates": challenge_safety_gates,
        "challenge_safety_gate_passed": challenge_safety_gate_passed,
        "two_estimand_gate_passed": two_estimand_gate_passed,
        "promotion_pending": True,
        "source_release_holdout_eligible": source_release_eligible,
        "release_holdout_eligible": source_release_eligible,
        "release_score_gate_passed": release_score_gate_passed,
        "independent_invocations_attested": True,
        "invocation_provenance_cryptographically_verified": False,
        "challenge_scoring_pending": cohort == "primary" and not challenge_score_bound,
        "private_file_mode": "0600",
        "private_directory_mode": "0700",
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    if cohort == "challenge":
        score_manifest.update(
            {
                "challenge_base_capability_gate_passed": (
                    challenge_base_capability_gate_passed
                ),
                "challenge_lifecycle_source_gold_metrics": (
                    challenge_lifecycle_source_gold_metrics
                ),
                "challenge_lifecycle_source_gold_gate_passed": (
                    challenge_lifecycle_source_gold_gate_passed
                ),
            }
        )
    authenticator = hmac.new(
        key,
        MANIFEST_DOMAIN + _canonical_json(score_manifest),
        hashlib.sha256,
    ).hexdigest()
    manifest_raw = (
        _canonical_json({**score_manifest, "manifest_hmac_sha256": authenticator})
        + b"\n"
    )

    # Re-read all inputs after scoring so a concurrent mutation cannot be
    # silently absorbed between provenance validation and publication.
    if (
        _private_file(
            Path(adapter_root) / "manifest.json",
            description="candidate-gold manifest",
        )
        != adapter_manifest_raw
        or _private_file(sample_path, description="candidate-gold samples")
        != sample_raw
        or any(
            _private_file(Path(path), description="verifier checkpoint") != raw
            for path, raw in zip(checkpoint_paths, checkpoint_raw, strict=True)
        )
        or any(
            _private_file(Path(path), description="invocation attestation") != raw
            for path, raw in zip(attestation_paths, attestation_raw, strict=True)
        )
        or any(
            not _retained_run_evidence_unchanged(
                evidence,
                hmac_key_path=Path(hmac_key_path),
            )
            for evidence in attestation_evidence
        )
        or (
            bound_challenge is not None
            and any(
                _private_file(
                    Path(bound_challenge["root"]) / name,
                    description="challenge score artifact",
                )
                != raw
                for name, raw in bound_challenge["raws"].items()
            )
        )
    ):
        raise GmailTemporalHoldoutScoreError("scoring evidence changed while read")
    try:
        adapter._publish(
            Path(output_root),
            {
                SCORE_ARTIFACT: score_raw,
                OWNER_AUDIT_POPULATION_ARTIFACT: owner_population_raw,
                OWNER_AUDIT_ERRORS_ARTIFACT: owner_errors_raw,
                "manifest.json": manifest_raw,
            },
        )
    except adapter.GmailTemporalCandidateGoldAdapterError as exc:
        raise GmailTemporalHoldoutScoreError(
            "score output could not be published"
        ) from exc
    return {
        "version": VERSION,
        "status": "scored",
        "cohort": cohort,
        "diagnostic_only": bool(adapter_manifest["diagnostic_only"]),
        "records": len(samples),
        "semantic_units": len(units),
        "semantic_members": sum(len(unit.members) for unit in units),
        "candidates": len(candidates),
        "component_runs": 3,
        "candidate_gate_passed": bool(aggregate_gold["candidate_gate_passed"]),
        "owner_audit_population_records": len(samples),
        "owner_audit_error_records": owner_error_record_count,
        "owner_audit_critical_error_records": owner_critical_error_count,
        "release_evidence_class": source_evidence_class,
        "release_scope": adapter_manifest["release_scope"],
        "automatic_apply_eligible": False,
        "sol_label_authority_attested": adapter_manifest[
            "sol_label_authority_attested"
        ],
        "cohort_gate_passed": cohort_gate_passed,
        "primary_operability_gate_passed": (
            primary_operability_gate_passed if cohort == "primary" else None
        ),
        "natural_recall_metrics_diagnostic_only": cohort == "primary",
        "challenge_capability_gate_passed": (
            challenge_capability_gate_passed if cohort == "challenge" else None
        ),
        "challenge_score_bound": challenge_score_bound,
        "challenge_safety_gate_passed": challenge_safety_gate_passed,
        "two_estimand_gate_passed": two_estimand_gate_passed,
        "promotion_pending": True,
        "release_holdout_eligible": source_release_eligible,
        "release_score_gate_passed": release_score_gate_passed,
        "ensemble_pending": False,
        "challenge_scoring_pending": cohort == "primary" and not challenge_score_bound,
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter-root", type=Path, required=True)
    parser.add_argument("--hmac-key", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, action="append", default=[])
    parser.add_argument("--attestation", type=Path, action="append", default=[])
    parser.add_argument("--challenge-score-root", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    if len(args.checkpoint) != 3 or len(args.attestation) != 3:
        print(
            json.dumps(
                {
                    "version": VERSION,
                    "status": "failed",
                    "error": "gmail_temporal_holdout_scoring_failed",
                    "private_content_printed": False,
                },
                sort_keys=True,
            )
        )
        raise SystemExit(2)
    try:
        result = score_gmail_temporal_holdout(
            args.adapter_root,
            args.hmac_key,
            tuple(args.checkpoint),  # type: ignore[arg-type]
            tuple(args.attestation),  # type: ignore[arg-type]
            args.output_root,
            challenge_score_root=args.challenge_score_root,
        )
    except (GmailTemporalHoldoutScoreError, OSError, ValueError):
        result = {
            "version": VERSION,
            "status": "failed",
            "error": "gmail_temporal_holdout_scoring_failed",
            "private_content_printed": False,
        }
        print(json.dumps(result, sort_keys=True))
        raise SystemExit(2) from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
