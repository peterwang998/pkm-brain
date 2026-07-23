#!/usr/bin/env python3
"""Prepare authenticated Gmail holdout labels for candidate-gold scoring.

This adapter is local-only.  It authenticates the frozen holdout and finalized
gold bundles, proves exact selected-cohort sample/binding/request/label coverage,
recomputes every baseline-frontier grade from the pre-label evaluation
authority, and publishes an owner-only sample file understood by
``evaluate_gmail_temporal_candidate_gold.py``.  It performs no model, network,
or Brain persistence calls and emits only aggregate status.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from pkm_brain.gmail_temporal_frontier import (
    build_gmail_temporal_candidate_frontier,
)
from pkm_brain.gmail_temporal_runner import _request_for_page


VERSION = "gmail_temporal_holdout_candidate_gold_adapter_v4"
MANIFEST_VERSION = "gmail_temporal_holdout_candidate_gold_manifest_v4"
MANIFEST_DOMAIN = b"gmail_temporal_holdout_candidate_gold_manifest_v4\0"
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
OUTPUT_SAMPLE_ARTIFACT = "candidate-gold-samples.jsonl"
EXPECTED_HOLDOUT_ARTIFACTS = frozenset(
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

_ROOT = Path(__file__).resolve().parents[1]
_FINALIZER_PATH = _ROOT / "scripts" / "finalize_gmail_temporal_holdout_labels.py"
_EVALUATOR_PATH = _ROOT / "scripts" / "evaluate_gmail_temporal_candidate_gold.py"
_SAMPLE_KEYS = {
    "version",
    "sample_id",
    "thread_id",
    "selection_partition",
    "stratum",
    "message_internal_at",
    "source_prior_message_count",
    "source_later_message_count",
    "source_omitted_before_count",
    "thread_truncated_message_count",
    "target_body_truncation_status",
    "text",
    "sanitized_text_sha256",
    "source_sha256",
    "analysis_fingerprint",
    "batch_plan_fingerprint",
    "preparation",
    "policy",
    "selection_strata",
    "expressions",
    "mentions",
    "leads",
    "routable",
}
_PREPARATION_KEYS = {
    "admission_basis",
    "disposition",
    "error_bucket",
    "expression_count",
    "mention_count",
    "candidate_count",
    "page_count",
    "request_fingerprints",
}
_BINDING_KEYS = {
    "version",
    "sample_id",
    "document_id",
    "gmail_message_id",
    "gmail_account_key",
    "gmail_thread_id",
    "gmail_source_revision",
    "source_sha256",
    "candidate_fingerprint",
    "analysis_fingerprint",
    "batch_plan_fingerprint",
    "target_fingerprint",
    "routable",
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
_GRADE_SCORE = {"absent": 0.0, "partial": 0.5, "exact": 1.0}


class GmailTemporalCandidateGoldAdapterError(ValueError):
    """Raised when holdout authority cannot safely become scorer input."""


def _load_script(name: str, path: Path) -> ModuleType:
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise GmailTemporalCandidateGoldAdapterError(
            "required local evaluator could not be loaded"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(name, None)
        raise GmailTemporalCandidateGoldAdapterError(
            "required local evaluator could not be loaded"
        ) from exc
    return module


finalizer = _load_script(
    "_gmail_temporal_holdout_label_finalizer_for_adapter",
    _FINALIZER_PATH,
)
candidate_evaluator = _load_script(
    "_gmail_temporal_candidate_gold_for_holdout_adapter",
    _EVALUATOR_PATH,
)


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


def _load_jsonl(raw: bytes, *, description: str) -> list[dict[str, Any]]:
    try:
        return finalizer._load_jsonl(raw, description=description)
    except finalizer.GmailTemporalLabelFinalizerError as exc:
        raise GmailTemporalCandidateGoldAdapterError(
            f"{description} is invalid"
        ) from exc


def _private_regular_file(path: Path, *, description: str) -> bytes:
    try:
        return finalizer._private_regular_file(path, description=description)
    except finalizer.GmailTemporalLabelFinalizerError as exc:
        raise GmailTemporalCandidateGoldAdapterError(
            f"{description} is unavailable or unsafe"
        ) from exc


def _private_directory(path: Path, *, description: str) -> None:
    try:
        finalizer._private_directory(path, description=description)
    except finalizer.GmailTemporalLabelFinalizerError as exc:
        raise GmailTemporalCandidateGoldAdapterError(
            f"{description} is unavailable or unsafe"
        ) from exc


def _load_holdout(
    root: Path,
    *,
    key: bytes,
) -> tuple[dict[str, Any], bytes, dict[str, bytes]]:
    try:
        manifest, manifest_raw = finalizer._load_builder_manifest(
            root / "manifest.json",
            key=key,
        )
        artifacts = finalizer._verify_artifact_inventory(
            root,
            artifact_sha256=manifest["artifact_sha256"],
        )
        label_manifest = finalizer._load_label_manifest(
            artifacts["label-queue/manifest.json"],
            artifacts=artifacts,
            root_manifest=manifest,
        )
    except (KeyError, finalizer.GmailTemporalLabelFinalizerError) as exc:
        raise GmailTemporalCandidateGoldAdapterError(
            "holdout authority is invalid"
        ) from exc
    if set(manifest["artifact_sha256"]) != EXPECTED_HOLDOUT_ARTIFACTS:
        raise GmailTemporalCandidateGoldAdapterError(
            "holdout artifact authority is incomplete"
        )
    if label_manifest["primary_count"] != manifest["primary_sample_count"]:
        raise GmailTemporalCandidateGoldAdapterError(
            "holdout primary authority is inconsistent"
        )
    return manifest, manifest_raw, artifacts


def _load_gold(
    root: Path,
    *,
    key: bytes,
    holdout_manifest: Mapping[str, Any],
    holdout_manifest_raw: bytes,
    holdout_artifacts: Mapping[str, bytes],
    cohort: str,
) -> tuple[dict[str, Any], bytes, list[dict[str, Any]]]:
    _private_directory(root, description="gold root")
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise GmailTemporalCandidateGoldAdapterError(
            "gold inventory is unavailable"
        ) from exc
    if "manifest.json" not in {item.name for item in entries} or any(
        not item.is_file() or item.is_symlink() for item in entries
    ):
        raise GmailTemporalCandidateGoldAdapterError("gold inventory is not exact")
    manifest_raw = _private_regular_file(
        root / "manifest.json",
        description="gold manifest",
    )
    try:
        value = finalizer._parse_json(manifest_raw, description="gold manifest")
    except finalizer.GmailTemporalLabelFinalizerError as exc:
        raise GmailTemporalCandidateGoldAdapterError(
            "gold manifest is invalid"
        ) from exc
    if not isinstance(value, dict) or manifest_raw != _canonical_json(value) + b"\n":
        raise GmailTemporalCandidateGoldAdapterError("gold manifest is invalid")
    authenticator = value.get("manifest_hmac_sha256")
    unsigned = dict(value)
    unsigned.pop("manifest_hmac_sha256", None)
    expected = hmac.new(
        key,
        finalizer.GOLD_MANIFEST_DOMAIN + _canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()
    if not isinstance(authenticator, str) or not hmac.compare_digest(
        authenticator,
        expected,
    ):
        raise GmailTemporalCandidateGoldAdapterError(
            "gold manifest authentication failed"
        )
    artifact_sha256 = value.get("artifact_sha256")
    if (
        not isinstance(artifact_sha256, dict)
        or set(artifact_sha256)
        not in (
            {"gold.jsonl"},
            {"gold.jsonl", "challenge-diagnostic-gold.jsonl"},
        )
        or {item.name for item in entries} != {"manifest.json", *artifact_sha256}
    ):
        raise GmailTemporalCandidateGoldAdapterError("gold inventory is not exact")
    gold_artifacts: dict[str, bytes] = {}
    for name, digest in artifact_sha256.items():
        raw = _private_regular_file(root / name, description="gold artifact")
        if not isinstance(digest, str) or not hmac.compare_digest(
            _sha256_bytes(raw), digest
        ):
            raise GmailTemporalCandidateGoldAdapterError(
                "gold artifact commitment failed"
            )
        gold_artifacts[name] = raw
    gold_raw = gold_artifacts["gold.jsonl"]
    required = {
        "version": finalizer.GOLD_MANIFEST_VERSION,
        "finalizer_version": finalizer.VERSION,
        "finalizer_sha256": _sha256_bytes(Path(finalizer.__file__).read_bytes()),
        "coverage": "exact_primary_queue_order_and_membership",
        "source_fields": "canonical_json_immutable",
        "baseline_frontier_grade_authority": (
            "adapter_recomputed_from_frozen_primary_evaluation_authority"
        ),
        "baseline_frontier_grade_input_placeholder": (
            finalizer.BASELINE_GRADE_PLACEHOLDER
        ),
        "baseline_frontier_grade_human_controlled": False,
        "candidate_gold_adapter_required": True,
        "direct_candidate_gold_evaluator_ready": False,
        "label_time_basis": finalizer.LABEL_TIME_BASIS,
        "later_context_policy": finalizer.LATER_CONTEXT_POLICY,
        "challenge_role": "conditional_capability_stress_gate",
        "challenge_required_as_separate_promotion_gate": True,
        "challenge_contributes_to_primary_release_gates": False,
        "estimands_must_not_be_pooled": True,
        "cohort_metrics_must_not_be_pooled": True,
        "release_evidence_class_applies_to": "primary_natural_cohort",
        "challenge_population_inference_eligible": False,
        "primary_estimand": "natural_mail_population_operability",
        "challenge_estimand": "conditional_temporal_capability_stress_recall",
        "automatic_apply_eligible": False,
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
        "label_chronology_scope": (
            "authenticated_retained_run_only_no_off_ledger_absence_claim"
        ),
        "label_off_ledger_activity_absence_proven": False,
    }
    if any(
        value.get(key_name) != expected_value
        for key_name, expected_value in required.items()
    ):
        raise GmailTemporalCandidateGoldAdapterError("gold manifest policy is invalid")
    evidence_fields = (
        "release_evidence_class",
        "release_evidence_class_applies_to",
        "primary_evidence_scope",
        "primary_prospective_unseen_source_evidence",
        "primary_historical_architecture_exposed",
        "challenge_evidence_scope",
        "challenge_prospective_unseen_source_evidence",
        "challenge_historical_architecture_exposed",
        "development_baseline_challenge_overlap_count",
        "challenge_population_inference_eligible",
        "cohort_metrics_must_not_be_pooled",
        "freeze_authority_version",
        "freeze_attempt_version",
        "freeze_outcome_version",
        "freeze_authority_manifest_sha256",
        "freeze_attempt_id",
        "freeze_attempt_sha256",
        "freeze_milestone",
        "freeze_authority_evidence_class",
        "freeze_authority_status",
        "freeze_no_reroll_scope",
        "freeze_authority_independently_reverified_downstream",
        "legacy_signed_freeze_claims_downgraded",
        "freeze_irrevocable_from_first_materialization",
        "labeled_cohort_reroll_forbidden",
        "all_labeled_attempts_must_be_retained",
        "underpowered_primary_action",
        "underpowered_challenge_action",
        "release_scope",
        "prospective_unseen_source_evidence",
        "historical_architecture_exposed",
        "retrospective_calibration_eligible",
        "semantic_development_overlap_status",
        "content_changing_canary_required",
    )
    sol_attested = value.get("sol_label_authority_attested")
    source_only_attested = value.get("source_only_label_authority_attested")
    label_authority_sha256 = value.get("label_authority_manifest_sha256")
    label_authority_invocations = value.get("label_authority_invocation_count")
    label_authority_present = sol_attested is True and source_only_attested is True
    label_chronology_verified = value.get("label_chronology_verified")
    label_started_at = finalizer._parse_aware_datetime(value.get("label_started_at"))
    label_completed_at = finalizer._parse_aware_datetime(
        value.get("label_completed_at")
    )
    primary_label_gate = value.get("primary_label_data_gate_passed")
    challenge_label_gate = value.get("challenge_label_data_gate_passed")
    label_gate = value.get("label_gate_passed")
    challenge_ready = value.get("challenge_diagnostic_ready")
    expected_effective_release = bool(
        holdout_manifest.get("release_holdout_eligible")
        and label_authority_present
        and challenge_ready is True
        and label_gate is True
    )
    if (
        value.get("source_holdout_manifest_sha256")
        != _sha256_bytes(holdout_manifest_raw)
        or value.get("source_holdout_manifest_hmac_sha256")
        != holdout_manifest.get("manifest_hmac_sha256")
        or value.get("source_label_manifest_sha256")
        != _sha256_bytes(holdout_artifacts["label-queue/manifest.json"])
        or value.get("source_primary_label_queue_sha256")
        != _sha256_bytes(holdout_artifacts["label-queue/primary.jsonl"])
        or value.get("source_challenge_label_queue_sha256")
        != _sha256_bytes(holdout_artifacts["label-queue/challenge.jsonl"])
        or value.get("source_release_holdout_eligible")
        is not holdout_manifest.get("release_holdout_eligible")
        or value.get("release_holdout_eligible") is not expected_effective_release
        or value.get("release_structural_scorability_verified")
        is not expected_effective_release
        or not isinstance(primary_label_gate, bool)
        or not isinstance(challenge_label_gate, bool)
        or not isinstance(label_gate, bool)
        or label_gate is not (primary_label_gate and challenge_label_gate)
        or not isinstance(challenge_ready, bool)
        or value.get("challenge_diagnostic_only")
        is not (
            holdout_manifest.get("challenge_evidence_scope")
            == "diagnostic_balanced_capability_stress"
        )
        or value.get("primary_minimum_labeled_hard_negatives")
        != finalizer.PRIMARY_MIN_LABELED_HARD_NEGATIVES
        or value.get("challenge_minimum_expected_material_records")
        != finalizer.CHALLENGE_MIN_EXPECTED_MATERIAL_RECORDS
        or value.get("challenge_minimum_semantic_members")
        != finalizer.CHALLENGE_MIN_SEMANTIC_MEMBERS
        or value.get("challenge_minimum_supported_members")
        != finalizer.CHALLENGE_MIN_SUPPORTED_MEMBERS
        or value.get("challenge_minimum_labeled_hard_negatives")
        != finalizer.CHALLENGE_MIN_LABELED_HARD_NEGATIVES
        or not isinstance(sol_attested, bool)
        or not isinstance(source_only_attested, bool)
        or sol_attested is not source_only_attested
        or label_chronology_verified is not label_authority_present
        or value.get("label_authority_provenance_cryptographically_verified")
        is not False
        or not isinstance(label_authority_invocations, int)
        or isinstance(label_authority_invocations, bool)
        or label_authority_invocations < 0
        or (
            label_authority_present
            and (
                value.get("label_authority_version")
                != finalizer.LABEL_AUTHORITY_VERSION
                or value.get("label_authority_model") != finalizer.LABEL_AUTHORITY_MODEL
                or value.get("label_authority_reasoning_effort")
                != finalizer.LABEL_AUTHORITY_REASONING_EFFORT
                or label_authority_invocations < 1
                or not isinstance(label_authority_sha256, str)
                or finalizer._SHA256_PATTERN.fullmatch(label_authority_sha256) is None
                or finalizer._LOGICAL_RUN_ID_PATTERN.fullmatch(
                    str(value.get("label_logical_run_id"))
                )
                is None
                or any(
                    not isinstance(value.get(field), str)
                    or finalizer._SHA256_PATTERN.fullmatch(str(value[field])) is None
                    for field in (
                        "label_plan_sha256",
                        "label_plan_hmac_sha256",
                        "label_receipt_set_sha256",
                    )
                )
                or label_started_at is None
                or label_completed_at is None
                or label_completed_at < label_started_at
            )
        )
        or (
            not label_authority_present
            and (
                value.get("label_authority_version") is not None
                or value.get("label_authority_model") is not None
                or value.get("label_authority_reasoning_effort") is not None
                or label_authority_invocations != 0
                or label_authority_sha256 is not None
                or value.get("label_logical_run_id") is not None
                or value.get("label_plan_sha256") is not None
                or value.get("label_plan_hmac_sha256") is not None
                or value.get("label_started_at") is not None
                or value.get("label_completed_at") is not None
                or value.get("label_receipt_set_sha256") is not None
            )
        )
        or any(
            value.get(field) != holdout_manifest.get(field) for field in evidence_fields
        )
    ):
        raise GmailTemporalCandidateGoldAdapterError(
            "gold does not bind the holdout authority"
        )
    if cohort == "challenge":
        if (
            value.get("challenge_diagnostic_ready") is not True
            or "challenge-diagnostic-gold.jsonl" not in gold_artifacts
        ):
            raise GmailTemporalCandidateGoldAdapterError(
                "challenge diagnostic gold is unavailable"
            )
        selected_raw = gold_artifacts["challenge-diagnostic-gold.jsonl"]
        expected_count = value.get("challenge_gold_record_count")
        description = "challenge diagnostic gold"
    else:
        selected_raw = gold_raw
        expected_count = value.get("gold_record_count")
        description = "primary gold artifact"
    rows = _load_jsonl(selected_raw, description=description)
    if expected_count != len(rows) or _jsonl_bytes(rows) != selected_raw:
        raise GmailTemporalCandidateGoldAdapterError("gold coverage is invalid")
    return value, manifest_raw, rows


def _validate_sample_rows(rows: list[dict[str, Any]]) -> None:
    seen_samples: set[str] = set()
    seen_threads: set[str] = set()
    for row in rows:
        preparation = row.get("preparation")
        sample_id = row.get("sample_id")
        thread_id = row.get("thread_id")
        text = row.get("text")
        if (
            set(row) != _SAMPLE_KEYS
            or row.get("version") != "gmail_temporal_holdout_sample_v2"
            or row.get("routable") is not False
            or not isinstance(sample_id, str)
            or not isinstance(thread_id, str)
            or sample_id in seen_samples
            or thread_id in seen_threads
            or not isinstance(text, str)
            or row.get("sanitized_text_sha256") != _sha256_bytes(text.encode("utf-8"))
            or not isinstance(preparation, dict)
            or set(preparation) != _PREPARATION_KEYS
            or not isinstance(row.get("expressions"), list)
            or not isinstance(row.get("mentions"), list)
            or not isinstance(row.get("leads"), list)
            or not isinstance(row.get("policy"), dict)
            or not isinstance(row.get("selection_strata"), list)
        ):
            raise GmailTemporalCandidateGoldAdapterError(
                "primary sample authority is invalid"
            )
        request_fingerprints = preparation.get("request_fingerprints")
        counts = (
            preparation.get("expression_count"),
            preparation.get("mention_count"),
            preparation.get("candidate_count"),
            preparation.get("page_count"),
        )
        if (
            not isinstance(request_fingerprints, list)
            or any(
                not isinstance(value, str) or not value
                for value in request_fingerprints
            )
            or len(request_fingerprints) != len(set(request_fingerprints))
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in counts
            )
            or counts[0] != len(row["expressions"])
            or counts[1] != len(row["mentions"])
            or counts[3] != len(request_fingerprints)
        ):
            raise GmailTemporalCandidateGoldAdapterError(
                "primary sample preparation is invalid"
            )
        seen_samples.add(sample_id)
        seen_threads.add(thread_id)


def _validate_bindings(
    rows: list[dict[str, Any]],
    *,
    samples: list[dict[str, Any]],
) -> None:
    if len(rows) != len(samples):
        raise GmailTemporalCandidateGoldAdapterError(
            "primary binding coverage is incomplete"
        )
    for binding, sample in zip(rows, samples, strict=True):
        if (
            set(binding) != _BINDING_KEYS
            or binding.get("version") != "gmail_temporal_holdout_binding_v1"
            or binding.get("sample_id") != sample["sample_id"]
            or binding.get("source_sha256") != sample["source_sha256"]
            or binding.get("analysis_fingerprint") != sample["analysis_fingerprint"]
            or binding.get("batch_plan_fingerprint") != sample["batch_plan_fingerprint"]
            or binding.get("routable") is not False
        ):
            raise GmailTemporalCandidateGoldAdapterError(
                "primary binding authority is invalid"
            )


def _validate_gold_coverage(
    gold_rows: list[dict[str, Any]],
    *,
    samples: list[dict[str, Any]],
    source_rows: list[dict[str, Any]],
) -> dict[str, int]:
    if len(gold_rows) != len(samples) or len(source_rows) != len(samples):
        raise GmailTemporalCandidateGoldAdapterError(
            "primary gold coverage is incomplete"
        )
    try:
        finalizer._validate_source_queue(source_rows)
        _validated, counts = finalizer._validate_completed_labels(
            source_rows,
            gold_rows,
        )
    except finalizer.GmailTemporalLabelFinalizerError as exc:
        raise GmailTemporalCandidateGoldAdapterError(
            "primary gold authority is invalid"
        ) from exc
    for sample, source, gold in zip(samples, source_rows, gold_rows, strict=True):
        target = source["target"]
        if (
            sample["sample_id"] != source["sample_id"]
            or sample["sample_id"] != gold["sample_id"]
            or sample["thread_id"] != source["thread_id"]
            or sample["thread_id"] != gold["thread_id"]
            or sample["message_internal_at"] != target["message_internal_at"]
            or sample["text"] != target["text"]
            or sample["sanitized_text_sha256"] != target["sanitized_text_sha256"]
        ):
            raise GmailTemporalCandidateGoldAdapterError(
                "primary sample and gold authority do not match"
            )
    return counts


def _inventory_by_id(
    sample: Mapping[str, Any],
    *,
    field: str,
    id_field: str,
) -> dict[str, Mapping[str, Any]]:
    values = sample.get(field)
    if not isinstance(values, list):
        raise GmailTemporalCandidateGoldAdapterError(
            "primary endpoint inventory is invalid"
        )
    output: dict[str, Mapping[str, Any]] = {}
    text = sample["text"]
    for value in values:
        if not isinstance(value, Mapping):
            raise GmailTemporalCandidateGoldAdapterError(
                "primary endpoint inventory is invalid"
            )
        identity = value.get(id_field)
        start = value.get("start")
        end = value.get("end")
        if (
            not isinstance(identity, str)
            or not identity
            or identity in output
            or not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not 0 <= start < end <= len(text)
        ):
            raise GmailTemporalCandidateGoldAdapterError(
                "primary endpoint inventory is invalid"
            )
        output[identity] = value
    return output


def _request_candidates(
    rows: list[dict[str, Any]],
    *,
    samples: list[dict[str, Any]],
    root_manifest: Mapping[str, Any],
    cohort: str,
) -> tuple[dict[str, tuple[dict[str, Any], ...]], int]:
    sample_by_id = {str(row["sample_id"]): row for row in samples}
    requests_by_sample: dict[str, list[dict[str, Any]]] = {
        sample_id: [] for sample_id in sample_by_id
    }
    candidates_by_sample: dict[str, list[dict[str, Any]]] = {
        sample_id: [] for sample_id in sample_by_id
    }
    seen_requests: set[str] = set()
    seen_pages: set[str] = set()
    seen_candidates: set[str] = set()
    for row in rows:
        sample_id = row.get("sample_id")
        payload = row.get("payload")
        if (
            set(row) != _REQUEST_KEYS
            or row.get("version") != "gmail_temporal_holdout_request_v1"
            or row.get("routable") is not False
            or sample_id not in sample_by_id
            or not isinstance(payload, dict)
        ):
            raise GmailTemporalCandidateGoldAdapterError(
                "primary request authority is invalid"
            )
        request_id = row.get("request_fingerprint")
        page_id = row.get("page_fingerprint")
        page = payload.get("page")
        batch = payload.get("batch")
        if (
            not isinstance(request_id, str)
            or not request_id
            or request_id in seen_requests
            or not isinstance(page_id, str)
            or not page_id
            or page_id in seen_pages
            or payload.get("request_fingerprint") != request_id
            or not isinstance(page, dict)
            or not isinstance(batch, dict)
            or page.get("page_fingerprint") != page_id
            or page.get("frontier_fingerprint") != row.get("frontier_fingerprint")
        ):
            raise GmailTemporalCandidateGoldAdapterError(
                "primary request binding is invalid"
            )
        candidates = page.get("candidates")
        if (
            not isinstance(candidates, list)
            or len(candidates) != row.get("candidate_count")
            or not candidates
        ):
            raise GmailTemporalCandidateGoldAdapterError(
                "primary request candidate coverage is invalid"
            )
        candidate_ids: list[str] = []
        for candidate in candidates:
            candidate_id = (
                candidate.get("candidate_id") if isinstance(candidate, dict) else None
            )
            if (
                not isinstance(candidate_id, str)
                or not candidate_id
                or candidate_id in seen_candidates
            ):
                raise GmailTemporalCandidateGoldAdapterError(
                    "primary request candidate coverage is invalid"
                )
            candidate_ids.append(candidate_id)
            seen_candidates.add(candidate_id)
            candidates_by_sample[str(sample_id)].append(dict(candidate))
        cluster_candidate_ids = [
            candidate_id
            for cluster in page.get("clusters", [])
            if isinstance(cluster, dict)
            for candidate_id in cluster.get("candidate_ids", [])
        ]
        if cluster_candidate_ids != candidate_ids:
            raise GmailTemporalCandidateGoldAdapterError(
                "primary request page coverage is invalid"
            )
        seen_requests.add(request_id)
        seen_pages.add(page_id)
        requests_by_sample[str(sample_id)].append(row)

    if len(rows) != root_manifest.get(f"{cohort}_request_count"):
        raise GmailTemporalCandidateGoldAdapterError(
            "primary request count is inconsistent"
        )
    if len(seen_pages) != root_manifest.get(f"{cohort}_page_count"):
        raise GmailTemporalCandidateGoldAdapterError(
            "primary page count is inconsistent"
        )
    if len(seen_candidates) != root_manifest.get(f"{cohort}_candidate_count"):
        raise GmailTemporalCandidateGoldAdapterError(
            "primary candidate count is inconsistent"
        )
    for sample_id, sample in sample_by_id.items():
        preparation = sample["preparation"]
        actual = requests_by_sample[sample_id]
        if [row["request_fingerprint"] for row in actual] != preparation[
            "request_fingerprints"
        ] or len(actual) != preparation["page_count"]:
            raise GmailTemporalCandidateGoldAdapterError(
                "primary request order or coverage is inconsistent"
            )
        if len(candidates_by_sample[sample_id]) != preparation["candidate_count"]:
            raise GmailTemporalCandidateGoldAdapterError(
                "primary candidate coverage is inconsistent"
            )
    return {
        sample_id: tuple(candidates)
        for sample_id, candidates in candidates_by_sample.items()
    }, len(seen_pages)


def _locator_matches_frozen_candidate(
    locator: Mapping[str, Any],
    *,
    candidate: Mapping[str, Any],
    expressions: Mapping[str, Mapping[str, Any]],
    mentions: Mapping[str, Mapping[str, Any]],
    text: str,
) -> bool:
    expression = locator.get("expression")
    subject = locator.get("subject")
    lifecycle = locator.get("lifecycle_mention")
    derived = locator.get("derived")
    if not all(isinstance(value, Mapping) for value in (expression, subject, derived)):
        raise GmailTemporalCandidateGoldAdapterError("semantic locator is invalid")
    expression_value = expressions.get(str(candidate.get("expression_id")))
    subject_value = mentions.get(str(candidate.get("subject_mention_id")))
    lifecycle_id = candidate.get("lifecycle_mention_id")
    lifecycle_value = None if lifecycle_id is None else mentions.get(str(lifecycle_id))
    if (
        expression_value is None
        or subject_value is None
        or (lifecycle_id is not None and lifecycle_value is None)
    ):
        raise GmailTemporalCandidateGoldAdapterError(
            "frozen candidate endpoint is unavailable"
        )

    def surface(value: Mapping[str, Any]) -> str:
        return text[int(value["start"]) : int(value["end"])]

    return (
        expression.get("surface") == surface(expression_value)
        and expression.get("form") == expression_value.get("form")
        and expression.get("field") == expression_value.get("field")
        and subject.get("surface") == surface(subject_value)
        and subject.get("mention_type") == subject_value.get("mention_type")
        and subject.get("field") == subject_value.get("field")
        and (
            lifecycle is None
            and lifecycle_value is None
            or isinstance(lifecycle, Mapping)
            and lifecycle_value is not None
            and lifecycle.get("surface") == surface(lifecycle_value)
            and lifecycle.get("lifecycle_role") == lifecycle_value.get("lifecycle_role")
            and lifecycle.get("field") == lifecycle_value.get("field")
        )
        and derived.get("relation") == candidate.get("relation")
        and derived.get("kind") == candidate.get("kind")
        and derived.get("lifecycle") == candidate.get("lifecycle")
        and derived.get("normalized_value") == candidate.get("normalized_value")
        and derived.get("requires_defer") == candidate.get("requires_defer")
    )


def _grade(score: float) -> str:
    if score == 1.0:
        return "exact"
    if score > 0.0:
        return "partial"
    return "absent"


def _adapt_gold_units(
    units: Any,
    *,
    sample: Mapping[str, Any],
    candidates: tuple[dict[str, Any], ...],
) -> tuple[list[dict[str, Any]], Counter[str]]:
    if not isinstance(units, list):
        raise GmailTemporalCandidateGoldAdapterError("semantic gold is invalid")
    expressions = _inventory_by_id(
        sample,
        field="expressions",
        id_field="expression_id",
    )
    mentions = _inventory_by_id(
        sample,
        field="mentions",
        id_field="mention_id",
    )
    output = copy.deepcopy(units)
    counts: Counter[str] = Counter()
    for unit in output:
        if not isinstance(unit, dict):
            raise GmailTemporalCandidateGoldAdapterError("semantic gold is invalid")
        if unit.get("baseline_frontier_grade") != finalizer.BASELINE_GRADE_PLACEHOLDER:
            raise GmailTemporalCandidateGoldAdapterError(
                "human-controlled baseline grade is forbidden"
            )
        members = unit.get("members")
        if not isinstance(members, list) or not members:
            raise GmailTemporalCandidateGoldAdapterError("semantic gold is invalid")
        member_scores: list[float] = []
        for member in members:
            if (
                not isinstance(member, dict)
                or member.get("baseline_frontier_grade")
                != finalizer.BASELINE_GRADE_PLACEHOLDER
            ):
                raise GmailTemporalCandidateGoldAdapterError(
                    "human-controlled baseline grade is forbidden"
                )
            alternatives = member.get("alternatives")
            if not isinstance(alternatives, list) or not alternatives:
                raise GmailTemporalCandidateGoldAdapterError("semantic gold is invalid")
            score = 0.0
            for alternative in alternatives:
                if not isinstance(alternative, Mapping):
                    raise GmailTemporalCandidateGoldAdapterError(
                        "semantic alternative is invalid"
                    )
                quality = alternative.get("quality")
                locator = alternative.get("locator")
                if quality not in {"exact", "partial"} or not isinstance(
                    locator,
                    Mapping,
                ):
                    raise GmailTemporalCandidateGoldAdapterError(
                        "semantic alternative is invalid"
                    )
                matches = sum(
                    _locator_matches_frozen_candidate(
                        locator,
                        candidate=candidate,
                        expressions=expressions,
                        mentions=mentions,
                        text=str(sample["text"]),
                    )
                    for candidate in candidates
                )
                if matches > 1:
                    raise GmailTemporalCandidateGoldAdapterError(
                        "semantic locator is ambiguous in frozen authority"
                    )
                if matches == 1:
                    score = max(score, _GRADE_SCORE[str(quality)])
            member_grade = _grade(score)
            member["baseline_frontier_grade"] = member_grade
            counts[f"member_{member_grade}"] += 1
            member_scores.append(score)
        unit_grade = _grade(sum(member_scores) / len(member_scores))
        unit["baseline_frontier_grade"] = unit_grade
        counts[f"unit_{unit_grade}"] += 1
    return output, counts


def _adapt_samples(
    samples: list[dict[str, Any]],
    gold_rows: list[dict[str, Any]],
    candidates_by_sample: Mapping[str, tuple[dict[str, Any], ...]],
    *,
    cohort: str,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    output: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for sample, gold in zip(samples, gold_rows, strict=True):
        units, unit_counts = _adapt_gold_units(
            gold["semantic_units"],
            sample=sample,
            candidates=candidates_by_sample[str(sample["sample_id"])],
        )
        counts.update(unit_counts)
        output.append(
            {
                "sample_id": sample["sample_id"],
                "thread_id": sample["thread_id"],
                "stratum": sample["stratum"],
                "message_internal_at": sample["message_internal_at"],
                "text": sample["text"],
                "expressions": sample["expressions"],
                "mentions": sample["mentions"],
                "leads": sample["leads"],
                "gold": {
                    "expected_material": gold["expected_material"],
                    "expected_filter": gold["expected_filter"],
                    "hard_negative": gold["hard_negative"],
                    "risk_bucket": f"blind_{cohort}_holdout",
                    "semantic_schema_version": "gmail_temporal_semantic_gold_v1",
                    "semantic_units": units,
                    "unmatched_candidates": "unsupported",
                },
            }
        )
    return output, counts


def _validate_current_candidate_authority(
    adapted: list[dict[str, Any]],
    request_rows: list[dict[str, Any]],
) -> int:
    """Prove frozen requests still equal the current deterministic frontier."""

    try:
        runtime_batches, runtime_candidates, _pages = (
            candidate_evaluator._runtime_batches(adapted)
        )
        candidate_evaluator._compile_gold(adapted, runtime_candidates)
    except candidate_evaluator.CandidateGoldError as exc:
        raise GmailTemporalCandidateGoldAdapterError(
            "adapted candidate-gold authority is incompatible"
        ) from exc
    expected_rows: list[dict[str, Any]] = []
    for runtime in runtime_batches:
        frontier = build_gmail_temporal_candidate_frontier(
            analysis=runtime.analysis,
            batch=runtime.batch,
        )
        for page in runtime.pages:
            request = _request_for_page(
                batch=runtime.batch,
                frontier=frontier,
                page_plan=SimpleNamespace(
                    plan_fingerprint=runtime.candidate_page_plan_fingerprint
                ),
                page=page,
            )
            expected_rows.append(
                {
                    "version": "gmail_temporal_holdout_request_v1",
                    "sample_id": runtime.sample_id,
                    "request_fingerprint": request.request_fingerprint,
                    "batch_fingerprint": request.batch_fingerprint,
                    "frontier_fingerprint": request.frontier_fingerprint,
                    "page_plan_fingerprint": request.page_plan_fingerprint,
                    "page_fingerprint": request.page_fingerprint,
                    "candidate_count": request.candidate_count,
                    "payload": json.loads(request.payload),
                    "routable": False,
                }
            )
    if _jsonl_bytes(expected_rows) != _jsonl_bytes(request_rows):
        raise GmailTemporalCandidateGoldAdapterError(
            "frozen request authority does not match the current frontier"
        )
    return len(runtime_candidates)


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


def _publish(output_root: Path, artifacts: Mapping[str, bytes]) -> None:
    root = Path(output_root)
    parent = root.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise GmailTemporalCandidateGoldAdapterError("output parent is unsafe")
    parent.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    if root.exists() or root.is_symlink():
        raise GmailTemporalCandidateGoldAdapterError("output already exists")
    temporary = Path(tempfile.mkdtemp(prefix=f".{root.name}-", dir=parent))
    os.chmod(temporary, PRIVATE_DIRECTORY_MODE)
    try:
        for name, payload in sorted(artifacts.items()):
            _write_private_new(temporary / name, payload)
        os.replace(temporary, root)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def prepare_gmail_temporal_holdout_candidate_gold(
    holdout_root: Path,
    gold_root: Path,
    hmac_key_path: Path,
    output_root: Path,
    *,
    cohort: str = "primary",
) -> dict[str, Any]:
    """Publish authenticated selected-cohort samples for the current scorer."""

    if cohort not in {"primary", "challenge"}:
        raise GmailTemporalCandidateGoldAdapterError("holdout cohort is invalid")
    try:
        key = finalizer._private_hmac_key(hmac_key_path)
    except finalizer.GmailTemporalLabelFinalizerError as exc:
        raise GmailTemporalCandidateGoldAdapterError(
            "HMAC key is unavailable or unsafe"
        ) from exc
    holdout_manifest, holdout_manifest_raw, artifacts = _load_holdout(
        Path(holdout_root),
        key=key,
    )
    gold_manifest, gold_manifest_raw, gold_rows = _load_gold(
        Path(gold_root),
        key=key,
        holdout_manifest=holdout_manifest,
        holdout_manifest_raw=holdout_manifest_raw,
        holdout_artifacts=artifacts,
        cohort=cohort,
    )
    sample_raw = artifacts[f"evaluation-authority/{cohort}-samples.jsonl"]
    binding_raw = artifacts[f"evaluation-authority/{cohort}-bindings.jsonl"]
    request_raw = artifacts[f"evaluation-authority/{cohort}-requests.jsonl"]
    source_label_raw = artifacts[f"label-queue/{cohort}.jsonl"]
    samples = _load_jsonl(sample_raw, description="primary sample authority")
    bindings = _load_jsonl(binding_raw, description="primary binding authority")
    requests = (
        []
        if not request_raw
        else _load_jsonl(request_raw, description="primary request authority")
    )
    source_rows = _load_jsonl(source_label_raw, description="primary label authority")
    if any(
        _jsonl_bytes(rows) != raw
        for rows, raw in (
            (samples, sample_raw),
            (bindings, binding_raw),
            (requests, request_raw),
            (source_rows, source_label_raw),
        )
    ):
        raise GmailTemporalCandidateGoldAdapterError(
            "primary authority is not canonical"
        )
    _validate_sample_rows(samples)
    _validate_bindings(bindings, samples=samples)
    gold_counts = _validate_gold_coverage(
        gold_rows,
        samples=samples,
        source_rows=source_rows,
    )
    candidates_by_sample, page_count = _request_candidates(
        requests,
        samples=samples,
        root_manifest=holdout_manifest,
        cohort=cohort,
    )
    adapted, grade_counts = _adapt_samples(
        samples,
        gold_rows,
        candidates_by_sample,
        cohort=cohort,
    )
    for output, source_row, gold_row in zip(
        adapted,
        source_rows,
        gold_rows,
        strict=True,
    ):
        output["source_label_row_sha256"] = _sha256_bytes(_canonical_json(source_row))
        gold_label_row_sha256 = _sha256_bytes(_canonical_json(gold_row))
        output["completed_label_row_sha256"] = gold_label_row_sha256
        output["gold_label_row_sha256"] = gold_label_row_sha256
    runtime_candidate_count = _validate_current_candidate_authority(adapted, requests)
    if runtime_candidate_count != holdout_manifest[f"{cohort}_candidate_count"]:
        raise GmailTemporalCandidateGoldAdapterError(
            "current candidate coverage is inconsistent"
        )
    sample_output_raw = _jsonl_bytes(adapted)
    source_primary_release_eligible = bool(holdout_manifest["release_holdout_eligible"])
    primary_release_eligible = bool(gold_manifest["release_holdout_eligible"])
    release_eligible = primary_release_eligible if cohort == "primary" else False
    source_release_evidence_class = str(holdout_manifest["release_evidence_class"])
    source_release_scope = str(holdout_manifest["release_scope"])
    overlap_field = f"development_baseline_{cohort}_overlap_count"
    raw_cohort_overlap_count = holdout_manifest.get(overlap_field, 0)
    if (
        not isinstance(raw_cohort_overlap_count, int)
        or isinstance(raw_cohort_overlap_count, bool)
        or raw_cohort_overlap_count < 0
    ):
        raise GmailTemporalCandidateGoldAdapterError(
            "cohort development overlap authority is invalid"
        )
    cohort_overlap_count = raw_cohort_overlap_count
    source_prospective_unseen = bool(
        holdout_manifest["prospective_unseen_source_evidence"]
    )
    source_historical_exposed = bool(
        holdout_manifest["historical_architecture_exposed"]
    )
    source_overlap_status = str(holdout_manifest["semantic_development_overlap_status"])
    cohort_evidence_scope = str(gold_manifest[f"{cohort}_evidence_scope"])
    cohort_prospective_unseen = bool(
        gold_manifest[f"{cohort}_prospective_unseen_source_evidence"]
    )
    cohort_historical_exposed = bool(
        gold_manifest[f"{cohort}_historical_architecture_exposed"]
    )
    cohort_overlap_status = (
        "challenge_stress_contains_development_baseline_overlap"
        if cohort == "challenge" and cohort_overlap_count > 0
        else source_overlap_status
    )
    diagnostic_only = (
        source_release_evidence_class == finalizer.DIAGNOSTIC_EVIDENCE_CLASS
        if cohort == "primary"
        else bool(gold_manifest["challenge_diagnostic_only"])
    )
    effective_release_scope = cohort_evidence_scope
    manifest = {
        "version": MANIFEST_VERSION,
        "adapter_version": VERSION,
        "cohort": cohort,
        "diagnostic_only": diagnostic_only,
        "source_holdout_manifest_sha256": _sha256_bytes(holdout_manifest_raw),
        "source_holdout_manifest_hmac_sha256": holdout_manifest["manifest_hmac_sha256"],
        "source_gold_manifest_sha256": _sha256_bytes(gold_manifest_raw),
        "source_gold_manifest_hmac_sha256": gold_manifest["manifest_hmac_sha256"],
        "source_cohort_samples_sha256": _sha256_bytes(sample_raw),
        "source_cohort_bindings_sha256": _sha256_bytes(binding_raw),
        "source_cohort_requests_sha256": _sha256_bytes(request_raw),
        "source_cohort_labels_sha256": _sha256_bytes(source_label_raw),
        "candidate_evaluator_sha256": _sha256_bytes(_EVALUATOR_PATH.read_bytes()),
        "artifact_sha256": {OUTPUT_SAMPLE_ARTIFACT: _sha256_bytes(sample_output_raw)},
        "record_count": len(adapted),
        "thread_count": len({row["thread_id"] for row in samples}),
        "expected_material_count": gold_counts["expected_material"],
        "expected_suppressed_count": gold_counts["expected_suppressed"],
        "labeled_hard_negative_count": gold_counts["labeled_hard_negatives"],
        "semantic_unit_count": gold_counts["units"],
        "semantic_member_count": gold_counts["members"],
        "candidate_count": runtime_candidate_count,
        "request_count": len(requests),
        "page_count": page_count,
        "baseline_unit_grade_counts": {
            grade: grade_counts[f"unit_{grade}"]
            for grade in ("absent", "partial", "exact")
        },
        "baseline_member_grade_counts": {
            grade: grade_counts[f"member_{grade}"]
            for grade in ("absent", "partial", "exact")
        },
        "coverage": "exact_cohort_sample_binding_request_and_gold",
        "baseline_frontier_grade_authority": (
            f"frozen_{cohort}_evaluation_authority_only"
        ),
        "baseline_frontier_grade_human_controlled": False,
        "candidate_gold_sample_compatible": True,
        "diagnostic_denominator": f"{cohort}_only",
        "estimands_must_not_be_pooled": True,
        "primary_estimand": "natural_mail_population_operability",
        "challenge_estimand": "conditional_temporal_capability_stress_recall",
        "challenge_role": "conditional_capability_stress_gate",
        "challenge_required_as_separate_promotion_gate": True,
        "challenge_contributes_to_primary_release_gates": False,
        "cohort_metrics_must_not_be_pooled": True,
        "challenge_population_inference_eligible": False,
        "release_evidence_class_applies_to": "primary_natural_cohort",
        "freeze_authority_version": holdout_manifest["freeze_authority_version"],
        "freeze_attempt_version": holdout_manifest["freeze_attempt_version"],
        "freeze_outcome_version": holdout_manifest["freeze_outcome_version"],
        "freeze_authority_manifest_sha256": holdout_manifest[
            "freeze_authority_manifest_sha256"
        ],
        "freeze_attempt_id": holdout_manifest["freeze_attempt_id"],
        "freeze_attempt_sha256": holdout_manifest["freeze_attempt_sha256"],
        "freeze_milestone": holdout_manifest["freeze_milestone"],
        "freeze_authority_evidence_class": holdout_manifest[
            "freeze_authority_evidence_class"
        ],
        "freeze_authority_status": holdout_manifest["freeze_authority_status"],
        "freeze_no_reroll_scope": holdout_manifest["freeze_no_reroll_scope"],
        "freeze_authority_independently_reverified_downstream": holdout_manifest[
            "freeze_authority_independently_reverified_downstream"
        ],
        "legacy_signed_freeze_claims_downgraded": holdout_manifest.get(
            "legacy_signed_freeze_claims_downgraded", False
        ),
        "freeze_irrevocable_from_first_materialization": holdout_manifest[
            "freeze_irrevocable_from_first_materialization"
        ],
        "labeled_cohort_reroll_forbidden": holdout_manifest[
            "labeled_cohort_reroll_forbidden"
        ],
        "all_labeled_attempts_must_be_retained": holdout_manifest[
            "all_labeled_attempts_must_be_retained"
        ],
        "underpowered_primary_action": holdout_manifest["underpowered_primary_action"],
        "underpowered_challenge_action": holdout_manifest[
            "underpowered_challenge_action"
        ],
        "source_release_evidence_class": source_release_evidence_class,
        "release_evidence_class": source_release_evidence_class,
        "source_release_scope": source_release_scope,
        "cohort_evidence_scope": cohort_evidence_scope,
        "release_scope": effective_release_scope,
        "source_prospective_unseen_source_evidence": source_prospective_unseen,
        "prospective_unseen_source_evidence": cohort_prospective_unseen,
        "source_historical_architecture_exposed": source_historical_exposed,
        "historical_architecture_exposed": cohort_historical_exposed,
        "retrospective_calibration_eligible": holdout_manifest[
            "retrospective_calibration_eligible"
        ],
        "source_semantic_development_overlap_status": source_overlap_status,
        "semantic_development_overlap_status": cohort_overlap_status,
        "development_baseline_cohort_overlap_count": cohort_overlap_count,
        "automatic_apply_eligible": False,
        "content_changing_canary_required": holdout_manifest[
            "content_changing_canary_required"
        ],
        "label_authority_manifest_sha256": gold_manifest[
            "label_authority_manifest_sha256"
        ],
        "label_authority_version": gold_manifest["label_authority_version"],
        "label_authority_model": gold_manifest["label_authority_model"],
        "label_authority_reasoning_effort": gold_manifest[
            "label_authority_reasoning_effort"
        ],
        "label_authority_invocation_count": gold_manifest[
            "label_authority_invocation_count"
        ],
        "sol_label_authority_attested": gold_manifest["sol_label_authority_attested"],
        "source_only_label_authority_attested": gold_manifest[
            "source_only_label_authority_attested"
        ],
        "label_chronology_verified": gold_manifest["label_chronology_verified"],
        "label_chronology_scope": gold_manifest["label_chronology_scope"],
        "label_off_ledger_activity_absence_proven": gold_manifest[
            "label_off_ledger_activity_absence_proven"
        ],
        "label_logical_run_id": gold_manifest["label_logical_run_id"],
        "label_plan_sha256": gold_manifest["label_plan_sha256"],
        "label_plan_hmac_sha256": gold_manifest["label_plan_hmac_sha256"],
        "label_started_at": gold_manifest["label_started_at"],
        "label_completed_at": gold_manifest["label_completed_at"],
        "label_receipt_set_sha256": gold_manifest["label_receipt_set_sha256"],
        "label_authority_provenance_cryptographically_verified": False,
        "release_structural_scorability_verified": (
            bool(gold_manifest["release_structural_scorability_verified"])
            if cohort == "primary"
            else False
        ),
        "primary_label_data_gate_passed": gold_manifest[
            "primary_label_data_gate_passed"
        ],
        "challenge_label_data_gate_passed": gold_manifest[
            "challenge_label_data_gate_passed"
        ],
        "label_data_gate_passed": gold_manifest["label_gate_passed"],
        "source_primary_release_holdout_eligible": (source_primary_release_eligible),
        "source_release_holdout_eligible": release_eligible,
        "release_holdout_eligible": release_eligible,
        "private_file_mode": "0600",
        "private_directory_mode": "0700",
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    authenticator = hmac.new(
        key,
        MANIFEST_DOMAIN + _canonical_json(manifest),
        hashlib.sha256,
    ).hexdigest()
    manifest_raw = (
        _canonical_json({**manifest, "manifest_hmac_sha256": authenticator}) + b"\n"
    )
    _publish(
        Path(output_root),
        {
            OUTPUT_SAMPLE_ARTIFACT: sample_output_raw,
            "manifest.json": manifest_raw,
        },
    )
    return {
        "version": VERSION,
        "status": "prepared",
        "cohort": cohort,
        "diagnostic_only": diagnostic_only,
        "records": len(adapted),
        "semantic_units": gold_counts["units"],
        "semantic_members": gold_counts["members"],
        "candidates": runtime_candidate_count,
        "requests": len(requests),
        "release_holdout_eligible": release_eligible,
        "release_evidence_class": source_release_evidence_class,
        "release_scope": effective_release_scope,
        "retrospective_calibration_eligible": bool(
            holdout_manifest["retrospective_calibration_eligible"]
        ),
        "automatic_apply_eligible": False,
        "content_changing_canary_required": bool(
            holdout_manifest["content_changing_canary_required"]
        ),
        "sol_label_authority_attested": bool(
            gold_manifest["sol_label_authority_attested"]
        ),
        "source_only_label_authority_attested": bool(
            gold_manifest["source_only_label_authority_attested"]
        ),
        "label_authority_provenance_cryptographically_verified": False,
        "release_structural_scorability_verified": bool(
            gold_manifest["release_structural_scorability_verified"]
        )
        if cohort == "primary"
        else False,
        "candidate_gold_sample_compatible": True,
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--holdout-root", type=Path, required=True)
    parser.add_argument("--gold-root", type=Path, required=True)
    parser.add_argument("--hmac-key", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--cohort", choices=("primary", "challenge"), default="primary")
    args = parser.parse_args()
    try:
        result = prepare_gmail_temporal_holdout_candidate_gold(
            args.holdout_root,
            args.gold_root,
            args.hmac_key,
            args.output_root,
            cohort=args.cohort,
        )
    except (GmailTemporalCandidateGoldAdapterError, OSError, ValueError):
        result = {
            "version": VERSION,
            "status": "failed",
            "error": "gmail_temporal_holdout_candidate_gold_preparation_failed",
            "private_content_printed": False,
        }
        print(json.dumps(result, sort_keys=True))
        raise SystemExit(2) from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
