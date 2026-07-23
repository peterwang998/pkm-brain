#!/usr/bin/env python3
"""Prepare and finalize private owner audits for the Gmail temporal holdout.

``prepare`` authenticates both score estimands and freezes separate owner-only
queues for each: a deterministic HMAC-stratified quarter of completed labels
and every score-identified error.  Primary and challenge denominators are never
pooled.
``finalize`` requires an explicit owner disposition for every frozen row and
publishes only authenticated aggregates.  Corrections invalidate the old score
and require a full re-score; they never patch a numerator locally.
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
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any


VERSION = "gmail_temporal_owner_audit_v1"
PREPARE_MANIFEST_VERSION = "gmail_temporal_owner_audit_prepare_manifest_v1"
FINAL_MANIFEST_VERSION = "gmail_temporal_owner_audit_final_manifest_v1"
LABEL_QUEUE_VERSION = "gmail_temporal_owner_label_audit_queue_v1"
ERROR_QUEUE_VERSION = "gmail_temporal_owner_error_audit_queue_v1"
FINAL_SCORE_VERSION = "gmail_temporal_owner_audit_score_v1"
PREPARE_MANIFEST_DOMAIN = b"gmail_temporal_owner_audit_prepare_manifest_v1\0"
FINAL_MANIFEST_DOMAIN = b"gmail_temporal_owner_audit_final_manifest_v1\0"
SELECTION_DOMAIN = b"gmail_temporal_owner_audit_selection_v1\0"

COHORTS = ("primary", "challenge")
LABEL_QUEUE_ARTIFACTS = {
    "primary": "primary-label-audit-queue.jsonl",
    "challenge": "challenge-label-audit-queue.jsonl",
}
ERROR_QUEUE_ARTIFACTS = {
    "primary": "primary-error-audit-queue.jsonl",
    "challenge": "challenge-error-audit-queue.jsonl",
}
# Kept as primary aliases for callers that only need to identify the natural-mail
# queue.  The audit protocol itself always requires both cohorts.
LABEL_QUEUE_ARTIFACT = LABEL_QUEUE_ARTIFACTS["primary"]
ERROR_QUEUE_ARTIFACT = ERROR_QUEUE_ARTIFACTS["primary"]
FINAL_SCORE_ARTIFACT = "owner-audit.json"
MANIFEST_ARTIFACT = "manifest.json"
SCORE_ARTIFACT = "score.json"
POPULATION_ARTIFACT = "owner-audit-population.jsonl"
ERROR_LEDGER_ARTIFACT = "owner-audit-errors.jsonl"

PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
AUDIT_NUMERATOR = 1
AUDIT_DENOMINATOR = 4
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ERROR_CATEGORIES = {
    "critical_calibration_error",
    "false_negative_member",
    "false_positive_artifact",
    "unmatched_artifact",
}
_LIFECYCLE_STRATA = {
    "lifecycle_cancellation",
    "lifecycle_completion",
    "lifecycle_reschedule",
    "timezone_sensitive",
}
_SUPPLEMENTAL_STRATA = {
    "ambiguity",
    "false_negative",
    "supported_artifact",
    "uncertain_sidecar",
    "accepted_artifact",
    "critical_calibration_error",
    "false_positive_artifact",
    "unmatched_artifact",
    *_LIFECYCLE_STRATA,
}
_POPULATION_VERSION = "gmail_temporal_owner_audit_population_v1"
_ERROR_VERSION = "gmail_temporal_owner_audit_error_v1"
_SOURCE_SCORE_MANIFEST_VERSION = "gmail_temporal_holdout_score_manifest_v3"
_SOURCE_SCORE_VERSION = "gmail_temporal_holdout_score_v3"
_SOURCE_SCORE_MANIFEST_DOMAIN = b"gmail_temporal_holdout_score_manifest_v3\0"
_POPULATION_FIELDS = {
    "version",
    "cohort",
    "diagnostic_only",
    "sample_id",
    "thread_id",
    "source_label_row_sha256",
    "completed_label_row_sha256",
    "gold_label_row_sha256",
    "expected_material",
    "expected_filter",
    "hard_negative",
    "supported_artifact",
    "uncertain_sidecar",
    "accepted_artifact",
    "false_negative",
    "critical_calibration_error",
    "false_positive_artifact",
    "unmatched_artifact",
    "lifecycle_reschedule",
    "lifecycle_cancellation",
    "lifecycle_completion",
    "timezone_sensitive",
    "routable",
}
_ERROR_FIELDS = {
    "version",
    "error_id",
    "cohort",
    "diagnostic_only",
    "category",
    "sample_id",
    "thread_id",
    "source_label_row_sha256",
    "completed_label_row_sha256",
    "gold_label_row_sha256",
    "unit_id",
    "member_id",
    "artifact_id",
    "artifact_kind",
    "candidate_id",
    "candidate_ids",
    "candidate_semantics",
    "critical",
    "routable",
}
_CANDIDATE_SEMANTIC_FIELDS = {
    "candidate_id",
    "expression_id",
    "subject_mention_id",
    "lifecycle_mention_id",
    "relation",
    "kind",
    "lifecycle",
    "normalized_value",
    "requires_defer",
    "blockers",
    "risk_features",
    "repair_flags",
}
_ERROR_CATEGORY_ORDER = {
    "critical_calibration_error": 0,
    "false_negative_member": 1,
    "unmatched_artifact": 2,
    "false_positive_artifact": 3,
}

_ROOT = Path(__file__).resolve().parents[1]
_FINALIZER_PATH = _ROOT / "scripts" / "finalize_gmail_temporal_holdout_labels.py"
_SCORER_PATH = _ROOT / "scripts" / "score_gmail_temporal_holdout.py"
_CANDIDATE_EVALUATOR_PATH = (
    _ROOT / "scripts" / "evaluate_gmail_temporal_candidate_gold.py"
)
_ENSEMBLE_EVALUATOR_PATH = (
    _ROOT / "scripts" / "evaluate_gmail_temporal_candidate_ensemble.py"
)
_SOURCE_SCORER_VERSION = "gmail_temporal_holdout_scorer_v3"


class GmailTemporalOwnerAuditError(ValueError):
    """Raised when owner-audit evidence is incomplete or inconsistent."""


def _load_script(name: str, path: Path) -> ModuleType:
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise GmailTemporalOwnerAuditError("required local authority is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(name, None)
        raise GmailTemporalOwnerAuditError(
            "required local authority is unavailable"
        ) from exc
    return module


finalizer = _load_script(
    "_gmail_temporal_holdout_finalizer_for_owner_audit", _FINALIZER_PATH
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


def _private_file(path: Path, *, description: str) -> bytes:
    try:
        return finalizer._private_regular_file(path, description=description)
    except finalizer.GmailTemporalLabelFinalizerError as exc:
        raise GmailTemporalOwnerAuditError(
            f"{description} is unavailable or unsafe"
        ) from exc


def _private_directory(path: Path, *, description: str) -> None:
    try:
        finalizer._private_directory(path, description=description)
    except finalizer.GmailTemporalLabelFinalizerError as exc:
        raise GmailTemporalOwnerAuditError(
            f"{description} is unavailable or unsafe"
        ) from exc


def _hmac_key(path: Path) -> bytes:
    try:
        return finalizer._private_hmac_key(path)
    except finalizer.GmailTemporalLabelFinalizerError as exc:
        raise GmailTemporalOwnerAuditError("HMAC key is unavailable or unsafe") from exc


def _parse_json(raw: bytes, *, description: str) -> Any:
    try:
        return finalizer._parse_json(raw, description=description)
    except finalizer.GmailTemporalLabelFinalizerError as exc:
        raise GmailTemporalOwnerAuditError(f"{description} is malformed") from exc


def _canonical_jsonl(
    raw: bytes, *, description: str, allow_empty: bool = False
) -> list[dict[str, Any]]:
    if not raw:
        if allow_empty:
            return []
        raise GmailTemporalOwnerAuditError(f"{description} is malformed")
    if not raw.endswith(b"\n") or any(not line for line in raw.splitlines()):
        raise GmailTemporalOwnerAuditError(f"{description} is malformed")
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        value = _parse_json(line, description=description)
        if not isinstance(value, dict) or line != _canonical_json(value):
            raise GmailTemporalOwnerAuditError(f"{description} is malformed")
        rows.append(value)
    return rows


def _manifest_bytes(manifest: Mapping[str, Any], *, key: bytes, domain: bytes) -> bytes:
    unsigned = _canonical_json(dict(manifest))
    authenticator = hmac.new(key, domain + unsigned, hashlib.sha256).hexdigest()
    return (
        _canonical_json({**dict(manifest), "manifest_hmac_sha256": authenticator})
        + b"\n"
    )


def _publish(output_root: Path, artifacts: Mapping[str, bytes]) -> None:
    try:
        finalizer._publish_frozen(Path(output_root), artifacts)
    except finalizer.GmailTemporalLabelFinalizerError as exc:
        raise GmailTemporalOwnerAuditError(
            "audit output could not be published"
        ) from exc


def _load_holdout_and_gold(
    holdout_root: Path,
    gold_root: Path,
    *,
    key: bytes,
) -> dict[str, Any]:
    try:
        holdout_manifest, holdout_manifest_raw = finalizer._load_builder_manifest(
            Path(holdout_root) / MANIFEST_ARTIFACT,
            key=key,
        )
        holdout_artifacts = finalizer._verify_artifact_inventory(
            Path(holdout_root),
            artifact_sha256=holdout_manifest["artifact_sha256"],
        )
    except (KeyError, finalizer.GmailTemporalLabelFinalizerError) as exc:
        raise GmailTemporalOwnerAuditError("holdout authority is invalid") from exc
    source_rows_by_cohort: dict[str, list[dict[str, Any]]] = {}
    source_raw_by_cohort: dict[str, bytes] = {}
    for cohort in COHORTS:
        source_raw = holdout_artifacts.get(f"label-queue/{cohort}.jsonl")
        if not isinstance(source_raw, bytes):
            raise GmailTemporalOwnerAuditError(
                f"{cohort} source authority is incomplete"
            )
        source_rows = _canonical_jsonl(
            source_raw, description=f"{cohort} source authority"
        )
        try:
            finalizer._validate_source_queue(source_rows)
        except finalizer.GmailTemporalLabelFinalizerError as exc:
            raise GmailTemporalOwnerAuditError(
                f"{cohort} source authority is invalid"
            ) from exc
        source_rows_by_cohort[cohort] = source_rows
        source_raw_by_cohort[cohort] = source_raw

    _private_directory(Path(gold_root), description="gold root")
    try:
        entries = list(Path(gold_root).iterdir())
    except OSError as exc:
        raise GmailTemporalOwnerAuditError("gold inventory is unavailable") from exc
    gold_manifest_raw = _private_file(
        Path(gold_root) / MANIFEST_ARTIFACT, description="gold manifest"
    )
    gold_manifest = _parse_json(gold_manifest_raw, description="gold manifest")
    if (
        not isinstance(gold_manifest, dict)
        or gold_manifest_raw != _canonical_json(gold_manifest) + b"\n"
        or gold_manifest.get("version") != finalizer.GOLD_MANIFEST_VERSION
    ):
        raise GmailTemporalOwnerAuditError("gold manifest is invalid")
    authenticator = gold_manifest.get("manifest_hmac_sha256")
    unsigned = dict(gold_manifest)
    unsigned.pop("manifest_hmac_sha256", None)
    expected_authenticator = hmac.new(
        key,
        finalizer.GOLD_MANIFEST_DOMAIN + _canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()
    if not isinstance(authenticator, str) or not hmac.compare_digest(
        authenticator, expected_authenticator
    ):
        raise GmailTemporalOwnerAuditError("gold manifest authentication failed")
    artifact_hashes = gold_manifest.get("artifact_sha256")
    if (
        not isinstance(artifact_hashes, dict)
        or set(artifact_hashes) != {"gold.jsonl", "challenge-diagnostic-gold.jsonl"}
        or {entry.name for entry in entries} != {MANIFEST_ARTIFACT, *artifact_hashes}
    ):
        raise GmailTemporalOwnerAuditError("gold inventory is not exact")
    gold_raw_by_cohort: dict[str, bytes] = {}
    gold_rows_by_cohort: dict[str, list[dict[str, Any]]] = {}
    for name, digest in artifact_hashes.items():
        raw = _private_file(Path(gold_root) / name, description="gold artifact")
        if (
            not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
            or digest != _sha256_bytes(raw)
        ):
            raise GmailTemporalOwnerAuditError("gold artifact commitment failed")
    if (
        gold_manifest.get("source_holdout_manifest_sha256")
        != _sha256_bytes(holdout_manifest_raw)
        or gold_manifest.get("source_holdout_manifest_hmac_sha256")
        != holdout_manifest["manifest_hmac_sha256"]
        or gold_manifest.get("source_primary_label_queue_sha256")
        != _sha256_bytes(source_raw_by_cohort["primary"])
        or gold_manifest.get("source_challenge_label_queue_sha256")
        != _sha256_bytes(source_raw_by_cohort["challenge"])
        or gold_manifest.get("release_evidence_class")
        != holdout_manifest["release_evidence_class"]
        or gold_manifest.get("challenge_completed") is not True
        or gold_manifest.get("challenge_diagnostic_ready") is not True
        or gold_manifest.get("challenge_required_as_separate_promotion_gate")
        is not True
        or gold_manifest.get("estimands_must_not_be_pooled") is not True
    ):
        raise GmailTemporalOwnerAuditError("gold source binding is invalid")
    for cohort, name in (
        ("primary", "gold.jsonl"),
        ("challenge", "challenge-diagnostic-gold.jsonl"),
    ):
        gold_raw = _private_file(Path(gold_root) / name, description=f"{cohort} gold")
        if artifact_hashes.get(name) != _sha256_bytes(gold_raw):
            raise GmailTemporalOwnerAuditError("gold artifact commitment failed")
        gold_rows = _canonical_jsonl(gold_raw, description=f"{cohort} gold")
        try:
            validated, counts = finalizer._validate_completed_labels(
                source_rows_by_cohort[cohort],
                gold_rows,
            )
        except finalizer.GmailTemporalLabelFinalizerError as exc:
            raise GmailTemporalOwnerAuditError(f"{cohort} gold is invalid") from exc
        count_field = (
            "gold_record_count"
            if cohort == "primary"
            else "challenge_gold_record_count"
        )
        if (
            _jsonl_bytes(validated) != gold_raw
            or gold_manifest.get(count_field) != counts["records"]
            or (
                cohort == "primary"
                and gold_manifest.get("gold_thread_count") != counts["threads"]
            )
            or (
                cohort == "challenge"
                and gold_manifest.get("challenge_gold_sha256")
                != _sha256_bytes(gold_raw)
            )
        ):
            raise GmailTemporalOwnerAuditError(f"{cohort} gold coverage is invalid")
        gold_raw_by_cohort[cohort] = gold_raw
        gold_rows_by_cohort[cohort] = gold_rows
    return {
        "holdout_manifest": holdout_manifest,
        "holdout_manifest_raw": holdout_manifest_raw,
        "gold_manifest": gold_manifest,
        "gold_manifest_raw": gold_manifest_raw,
        "source_rows": source_rows_by_cohort,
        "source_raw": source_raw_by_cohort,
        "gold_rows": gold_rows_by_cohort,
        "gold_raw": gold_raw_by_cohort,
    }


def _load_score_root(
    score_root: Path,
    *,
    key: bytes,
    expected_cohort: str,
) -> tuple[
    dict[str, Any],
    bytes,
    dict[str, Any],
    bytes,
    list[dict[str, Any]],
    bytes,
    list[dict[str, Any]],
    bytes,
]:
    _private_directory(Path(score_root), description="score root")
    try:
        entries = list(Path(score_root).iterdir())
    except OSError as exc:
        raise GmailTemporalOwnerAuditError("score inventory is unavailable") from exc
    manifest_raw = _private_file(
        Path(score_root) / MANIFEST_ARTIFACT, description="score manifest"
    )
    manifest = _parse_json(manifest_raw, description="score manifest")
    if (
        not isinstance(manifest, dict)
        or manifest_raw != _canonical_json(manifest) + b"\n"
        or manifest.get("scorer_version") != _SOURCE_SCORER_VERSION
        or manifest.get("scorer_sha256") != _sha256_bytes(_SCORER_PATH.read_bytes())
        or manifest.get("candidate_evaluator_sha256")
        != _sha256_bytes(_CANDIDATE_EVALUATOR_PATH.read_bytes())
        or manifest.get("ensemble_evaluator_sha256")
        != _sha256_bytes(_ENSEMBLE_EVALUATOR_PATH.read_bytes())
    ):
        raise GmailTemporalOwnerAuditError("score manifest is invalid")
    if manifest.get("version") != _SOURCE_SCORE_MANIFEST_VERSION:
        raise GmailTemporalOwnerAuditError("score manifest is invalid")
    authenticator = manifest.get("manifest_hmac_sha256")
    unsigned = dict(manifest)
    unsigned.pop("manifest_hmac_sha256", None)
    expected_authenticator = hmac.new(
        key, _SOURCE_SCORE_MANIFEST_DOMAIN + _canonical_json(unsigned), hashlib.sha256
    ).hexdigest()
    if not isinstance(authenticator, str) or not hmac.compare_digest(
        authenticator, expected_authenticator
    ):
        raise GmailTemporalOwnerAuditError("score manifest authentication failed")
    artifact_hashes = manifest.get("artifact_sha256")
    required_artifacts = {
        SCORE_ARTIFACT,
        POPULATION_ARTIFACT,
        ERROR_LEDGER_ARTIFACT,
    }
    if (
        not isinstance(artifact_hashes, dict)
        or set(artifact_hashes) != required_artifacts
        or {entry.name for entry in entries} != {MANIFEST_ARTIFACT, *required_artifacts}
    ):
        raise GmailTemporalOwnerAuditError("score inventory is not exact")
    raw_by_name: dict[str, bytes] = {}
    for name in sorted(required_artifacts):
        raw = _private_file(Path(score_root) / name, description="score artifact")
        digest = artifact_hashes.get(name)
        if (
            not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
            or digest != _sha256_bytes(raw)
        ):
            raise GmailTemporalOwnerAuditError("score artifact commitment failed")
        raw_by_name[name] = raw
    score_raw = raw_by_name[SCORE_ARTIFACT]
    score = _parse_json(score_raw, description="score")
    if not isinstance(score, dict) or score_raw != _canonical_json(score) + b"\n":
        raise GmailTemporalOwnerAuditError("score is invalid")
    if (
        score.get("version") != _SOURCE_SCORE_VERSION
        or expected_cohort not in COHORTS
        or manifest.get("cohort") != expected_cohort
        or score.get("cohort") != expected_cohort
        or not isinstance(manifest.get("diagnostic_only"), bool)
        or score.get("diagnostic_only") is not manifest["diagnostic_only"]
        or manifest.get("estimands_must_not_be_pooled") is not True
        or manifest.get("metrics_must_not_be_pooled_across_cohorts") is not True
        or score.get("estimands_must_not_be_pooled") is not True
        or score.get("metrics_must_not_be_pooled_across_cohorts") is not True
        or not isinstance(manifest.get("cohort_gate_passed"), bool)
        or score.get("cohort_gate_passed") is not manifest["cohort_gate_passed"]
        or manifest.get("promotion_pending") is not True
        or score.get("promotion_pending") is not True
        or manifest.get("release_score_gate_passed") is not False
        or score.get("release_score_gate_passed") is not False
    ):
        raise GmailTemporalOwnerAuditError("score cohort policy is invalid")
    population_raw = raw_by_name[POPULATION_ARTIFACT]
    errors_raw = raw_by_name[ERROR_LEDGER_ARTIFACT]
    population = _canonical_jsonl(population_raw, description="owner audit population")
    errors = _canonical_jsonl(
        errors_raw,
        description="owner error ledger",
        allow_empty=True,
    )
    category_counts = dict(
        sorted(Counter(row.get("category") for row in errors).items())
    )
    critical_count = sum(row.get("critical") is True for row in errors)
    if (
        manifest.get("owner_audit_population_artifact") != POPULATION_ARTIFACT
        or manifest.get("owner_audit_population_version") != _POPULATION_VERSION
        or manifest.get("owner_audit_population_record_count") != len(population)
        or manifest.get("owner_audit_population_coverage")
        != "exact_selected_cohort_record_order"
        or manifest.get("owner_audit_error_artifact") != ERROR_LEDGER_ARTIFACT
        or manifest.get("owner_audit_error_version") != _ERROR_VERSION
        or manifest.get("owner_audit_error_record_count") != len(errors)
        or manifest.get("owner_audit_error_category_counts") != category_counts
        or manifest.get("owner_audit_critical_error_record_count") != critical_count
        or manifest.get("owner_audit_error_categories_are_disjoint") is not False
        or manifest.get("owner_audit_error_coverage")
        != "exact_critical_fp_fn_and_unmatched_artifact_identities"
        or score.get("owner_audit_population_records") != len(population)
        or score.get("owner_audit_error_records") != len(errors)
        or score.get("owner_audit_error_category_counts") != category_counts
        or score.get("owner_audit_critical_error_records") != critical_count
        or score.get("owner_audit_population_version") != _POPULATION_VERSION
        or score.get("owner_audit_error_version") != _ERROR_VERSION
    ):
        raise GmailTemporalOwnerAuditError("score owner-audit contract is invalid")
    return (
        manifest,
        manifest_raw,
        score,
        score_raw,
        population,
        population_raw,
        errors,
        errors_raw,
    )


def _gold_lifecycle_and_ambiguity(row: Mapping[str, Any]) -> tuple[set[str], bool]:
    lifecycle: set[str] = set()
    ambiguous = False
    for unit in row.get("semantic_units", []):
        for member in unit.get("members", []):
            ambiguous |= member.get("expected_verdict") == "uncertain"
            for alternative in member.get("alternatives", []):
                ambiguous |= alternative.get("quality") == "partial"
                locator = alternative.get("locator")
                if not isinstance(locator, Mapping):
                    continue
                mention = locator.get("lifecycle_mention")
                if isinstance(mention, Mapping):
                    role = str(mention.get("lifecycle_role", "")).lower()
                    if "resched" in role:
                        lifecycle.add("lifecycle_reschedule")
                    if "cancel" in role:
                        lifecycle.add("lifecycle_cancellation")
                    if "complet" in role:
                        lifecycle.add("lifecycle_completion")
                derived = locator.get("derived")
                if isinstance(derived, Mapping):
                    role = str(derived.get("lifecycle", "")).lower()
                    if "resched" in role:
                        lifecycle.add("lifecycle_reschedule")
                    if "cancel" in role:
                        lifecycle.add("lifecycle_cancellation")
                    if "complet" in role:
                        lifecycle.add("lifecycle_completion")
                    ambiguous |= derived.get("requires_defer") is True
                    normalized = str(derived.get("normalized_value", ""))
                    if re.search(
                        r"(?:Z|[+-]\d\d:\d\d|\b(?:UTC|GMT|P[SD]T)\b)", normalized
                    ):
                        lifecycle.add("timezone_sensitive")
    return lifecycle, ambiguous


def _population_by_sample(
    rows: list[dict[str, Any]],
    *,
    source_rows: list[dict[str, Any]],
    gold_rows: list[dict[str, Any]],
    cohort: str,
    diagnostic_only: bool,
) -> dict[str, dict[str, Any]]:
    source_by_id = {str(row["sample_id"]): row for row in source_rows}
    gold_by_id = {str(row["sample_id"]): row for row in gold_rows}
    expected_order = [str(row["sample_id"]) for row in gold_rows]
    if set(source_by_id) != set(gold_by_id):
        raise GmailTemporalOwnerAuditError("owner audit authority coverage is invalid")
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        sample_id = row.get("sample_id")
        source = source_by_id.get(str(sample_id))
        gold = gold_by_id.get(str(sample_id))
        if (
            set(row) != _POPULATION_FIELDS
            or row.get("version") != _POPULATION_VERSION
            or not isinstance(sample_id, str)
            or source is None
            or gold is None
            or sample_id in output
            or row.get("cohort") != cohort
            or row.get("diagnostic_only") is not diagnostic_only
            or row.get("thread_id") != gold.get("thread_id")
            or row.get("source_label_row_sha256")
            != _sha256_bytes(_canonical_json(source))
            or row.get("completed_label_row_sha256")
            != _sha256_bytes(_canonical_json(gold))
            or row.get("gold_label_row_sha256") != _sha256_bytes(_canonical_json(gold))
            or row.get("expected_material") is not gold.get("expected_material")
            or row.get("expected_filter") != gold.get("expected_filter")
            or row.get("hard_negative") is not gold.get("hard_negative")
            or any(
                not isinstance(row.get(field), bool)
                for field in (
                    "supported_artifact",
                    "uncertain_sidecar",
                    "accepted_artifact",
                    "false_negative",
                    "critical_calibration_error",
                    "false_positive_artifact",
                    "unmatched_artifact",
                    "lifecycle_reschedule",
                    "lifecycle_cancellation",
                    "lifecycle_completion",
                    "timezone_sensitive",
                )
            )
            or row.get("routable") is not False
        ):
            raise GmailTemporalOwnerAuditError(
                "owner audit population binding is invalid"
            )
        output[sample_id] = row
    if list(output) != expected_order:
        raise GmailTemporalOwnerAuditError(
            "owner audit population coverage or order is incomplete"
        )
    return output


def _row_strata(gold: Mapping[str, Any], population: Mapping[str, Any]) -> set[str]:
    material = gold.get("expected_material") is True
    hard_negative = gold.get("hard_negative") is True
    strata = {
        "material" if material else "non_material",
        "hard_negative" if hard_negative else "not_hard_negative",
    }
    lifecycle, gold_ambiguous = _gold_lifecycle_and_ambiguity(gold)
    strata.update(lifecycle)
    for stratum in _SUPPLEMENTAL_STRATA - {"ambiguity"}:
        if population[stratum] is True:
            strata.add(stratum)
    if gold_ambiguous:
        strata.add("ambiguity")
    return strata


def _selection_score(key: bytes, cohort: str, sample_id: str) -> str:
    return hmac.new(
        key,
        SELECTION_DOMAIN + cohort.encode("ascii") + b"\0" + sample_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _select_audit_sample(
    cohort: str,
    source_rows: list[dict[str, Any]],
    gold_rows: list[dict[str, Any]],
    population: Mapping[str, Mapping[str, Any]],
    *,
    key: bytes,
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, int]]:
    source_by_id = {str(row["sample_id"]): row for row in source_rows}
    target = math.ceil(len(gold_rows) * AUDIT_NUMERATOR / AUDIT_DENOMINATOR)
    entries: list[dict[str, Any]] = []
    for gold in gold_rows:
        sample_id = str(gold["sample_id"])
        strata = _row_strata(gold, population[sample_id])
        base = (
            "material"
            if "material" in strata
            else "hard_negative"
            if "hard_negative" in strata
            else "ordinary_non_material"
        )
        entries.append(
            {
                "sample_id": sample_id,
                "gold": gold,
                "strata": strata,
                "base": base,
                "rank": _selection_score(key, cohort, sample_id),
            }
        )
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        groups.setdefault(str(entry["base"]), []).append(entry)
    if target < len(groups):
        raise GmailTemporalOwnerAuditError("audit sample is too small for strata")
    for values in groups.values():
        values.sort(key=lambda item: (item["rank"], item["sample_id"]))
    allocations = {name: 1 for name in groups}
    remaining = target - len(groups)
    capacities = {name: len(values) - 1 for name, values in groups.items()}
    capacity_total = sum(capacities.values())
    remainders: list[tuple[float, str]] = []
    if capacity_total:
        for name in sorted(groups):
            exact = remaining * capacities[name] / capacity_total
            addition = min(capacities[name], math.floor(exact))
            allocations[name] += addition
            remainders.append((exact - addition, name))
        unallocated = target - sum(allocations.values())
        for _fraction, name in sorted(remainders, key=lambda item: (-item[0], item[1])):
            if not unallocated:
                break
            if allocations[name] < len(groups[name]):
                allocations[name] += 1
                unallocated -= 1
    selected = {
        str(entry["sample_id"]): entry
        for name, values in groups.items()
        for entry in values[: allocations[name]]
    }
    population_counts = Counter(
        stratum for entry in entries for stratum in entry["strata"]
    )
    required_supplemental = {
        stratum for stratum in _SUPPLEMENTAL_STRATA if population_counts[stratum] > 0
    }
    for stratum in sorted(required_supplemental):
        if any(stratum in entry["strata"] for entry in selected.values()):
            continue
        candidates = [
            entry
            for entry in entries
            if stratum in entry["strata"] and entry["sample_id"] not in selected
        ]
        if not candidates:
            raise GmailTemporalOwnerAuditError("audit stratum cannot be covered")
        replacement = min(
            candidates, key=lambda item: (item["rank"], item["sample_id"])
        )
        replaceable = [
            entry
            for entry in selected.values()
            if entry["base"] == replacement["base"]
            and all(
                any(
                    candidate["sample_id"] != entry["sample_id"]
                    and required in candidate["strata"]
                    for candidate in selected.values()
                )
                for required in required_supplemental & set(entry["strata"])
            )
        ]
        if not replaceable:
            raise GmailTemporalOwnerAuditError("audit strata cannot be covered")
        removed = max(replaceable, key=lambda item: (item["rank"], item["sample_id"]))
        selected.pop(str(removed["sample_id"]))
        selected[str(replacement["sample_id"])] = replacement
    if len(selected) != target:
        raise GmailTemporalOwnerAuditError("audit sample size is invalid")
    selected_counts = Counter(
        stratum for entry in selected.values() for stratum in entry["strata"]
    )
    for stratum in required_supplemental | {
        "material",
        "non_material",
        "hard_negative",
    }:
        if population_counts[stratum] and not selected_counts[stratum]:
            raise GmailTemporalOwnerAuditError("audit stratum coverage is incomplete")
    rows = [
        {
            "version": LABEL_QUEUE_VERSION,
            "cohort": cohort,
            "sample_id": entry["sample_id"],
            "source_label_row_sha256": _sha256_bytes(
                _canonical_json(source_by_id[str(entry["sample_id"])])
            ),
            "gold_label_row_sha256": _sha256_bytes(_canonical_json(entry["gold"])),
            "strata": sorted(entry["strata"]),
            "source_label": source_by_id[str(entry["sample_id"])],
            "completed_label": entry["gold"],
            "audit_status": "pending",
            "owner_disposition": None,
            "corrected_label": None,
            "owner_found_critical_error": None,
            "owner_notes": None,
        }
        for entry in sorted(selected.values(), key=lambda item: item["sample_id"])
    ]
    all_strata = sorted(
        {"material", "non_material", "hard_negative", *_SUPPLEMENTAL_STRATA}
    )
    return (
        rows,
        {stratum: population_counts[stratum] for stratum in all_strata},
        {stratum: selected_counts[stratum] for stratum in all_strata},
    )


def _validate_error_ledger(
    rows: list[dict[str, Any]],
    *,
    source_rows: list[dict[str, Any]],
    gold_rows: list[dict[str, Any]],
    cohort: str,
    diagnostic_only: bool,
) -> tuple[list[dict[str, Any]], dict[str, int], int]:
    source_by_id = {str(row["sample_id"]): row for row in source_rows}
    gold_by_id = {str(row["sample_id"]): row for row in gold_rows}
    seen_ids: set[str] = set()
    counts = Counter()
    critical_count = 0
    previous_category_rank = -1

    def valid_semantics(value: Any, expected_ids: list[str]) -> bool:
        if (
            not isinstance(value, list)
            or len(value) != len(expected_ids)
            or len(value) > 32
        ):
            return False
        observed_ids: list[str] = []
        for semantic in value:
            if (
                not isinstance(semantic, dict)
                or set(semantic) != _CANDIDATE_SEMANTIC_FIELDS
            ):
                return False
            candidate_id = semantic.get("candidate_id")
            if not isinstance(candidate_id, str):
                return False
            observed_ids.append(candidate_id)
            for field in (
                "expression_id",
                "subject_mention_id",
                "relation",
                "kind",
                "lifecycle",
            ):
                if not isinstance(semantic.get(field), str):
                    return False
            for field in ("lifecycle_mention_id", "normalized_value"):
                if semantic.get(field) is not None and not isinstance(
                    semantic.get(field), str
                ):
                    return False
            if not isinstance(semantic.get("requires_defer"), bool):
                return False
            for field in ("blockers", "risk_features", "repair_flags"):
                items = semantic.get(field)
                if (
                    not isinstance(items, list)
                    or len(items) > 32
                    or any(not isinstance(item, str) for item in items)
                ):
                    return False
        return observed_ids == expected_ids

    for row in rows:
        error_id = row.get("error_id")
        category = row.get("category")
        sample_id = row.get("sample_id")
        source = source_by_id.get(str(sample_id))
        gold = gold_by_id.get(str(sample_id))
        candidate_ids = row.get("candidate_ids")
        if not isinstance(candidate_ids, list) or any(
            not isinstance(candidate_id, str) for candidate_id in candidate_ids
        ):
            raise GmailTemporalOwnerAuditError("owner error ledger is invalid")
        semantic_ids = (
            candidate_ids
            if candidate_ids
            else [row["candidate_id"]]
            if isinstance(row.get("candidate_id"), str)
            else []
        )
        if (
            set(row) != _ERROR_FIELDS
            or row.get("version") != _ERROR_VERSION
            or not isinstance(error_id, str)
            or re.fullmatch(r"gtae_[0-9a-f]{64}", error_id) is None
            or error_id in seen_ids
            or category not in _ERROR_CATEGORIES
            or not isinstance(sample_id, str)
            or source is None
            or gold is None
            or row.get("cohort") != cohort
            or row.get("diagnostic_only") is not diagnostic_only
            or row.get("thread_id") != gold.get("thread_id")
            or row.get("source_label_row_sha256")
            != _sha256_bytes(_canonical_json(source))
            or row.get("completed_label_row_sha256")
            != _sha256_bytes(_canonical_json(gold))
            or row.get("gold_label_row_sha256") != _sha256_bytes(_canonical_json(gold))
            or not isinstance(row.get("critical"), bool)
            or row.get("routable") is not False
            or not valid_semantics(row.get("candidate_semantics"), semantic_ids)
        ):
            raise GmailTemporalOwnerAuditError("owner error ledger is invalid")
        category_rank = _ERROR_CATEGORY_ORDER[str(category)]
        if category_rank < previous_category_rank:
            raise GmailTemporalOwnerAuditError("owner error ledger order is invalid")
        previous_category_rank = category_rank
        if category == "critical_calibration_error":
            valid_identity = (
                row["unit_id"] is None
                and row["member_id"] is None
                and row["artifact_id"] is None
                and row["artifact_kind"] is None
                and isinstance(row["candidate_id"], str)
                and not candidate_ids
                and row["critical"] is True
            )
        elif category == "false_negative_member":
            valid_identity = (
                isinstance(row["unit_id"], str)
                and isinstance(row["member_id"], str)
                and row["artifact_id"] is None
                and row["artifact_kind"] is None
                and row["candidate_id"] is None
                and not candidate_ids
                and row["critical"] is False
            )
        else:
            valid_identity = (
                row["unit_id"] is None
                and row["member_id"] is None
                and isinstance(row["artifact_id"], str)
                and isinstance(row["artifact_kind"], str)
                and row["candidate_id"] is None
                and bool(candidate_ids)
                and (category == "unmatched_artifact" or row["critical"] is False)
            )
        if not valid_identity:
            raise GmailTemporalOwnerAuditError("owner error ledger identity is invalid")
        seen_ids.add(error_id)
        counts[str(category)] += 1
        critical_count += int(row["critical"])
    return (
        rows,
        {
            category: counts[category]
            for category in sorted(_ERROR_CATEGORIES)
            if counts[category]
        },
        critical_count,
    )


def _error_queue_rows(
    rows: list[dict[str, Any]],
    *,
    source_rows: list[dict[str, Any]],
    gold_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    source_by_id = {str(row["sample_id"]): row for row in source_rows}
    gold_by_id = {str(row["sample_id"]): row for row in gold_rows}
    return [
        {
            "version": ERROR_QUEUE_VERSION,
            "cohort": row["cohort"],
            "error_id": row["error_id"],
            "source_error_sha256": _sha256_bytes(_canonical_json(row)),
            "error": row,
            "source_label": source_by_id[str(row["sample_id"])],
            "completed_label": gold_by_id[str(row["sample_id"])],
            "audit_status": "pending",
            "owner_disposition": None,
            "correction": None,
            "owner_found_critical_error": None,
            "owner_notes": None,
        }
        for row in rows
    ]


def prepare_owner_audit(
    holdout_root: Path,
    gold_root: Path,
    primary_score_root: Path,
    challenge_score_root: Path,
    hmac_key_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Freeze separate 25% label samples and complete error queues per cohort."""

    key = _hmac_key(Path(hmac_key_path))
    authority = _load_holdout_and_gold(Path(holdout_root), Path(gold_root), key=key)
    score_roots = {
        "primary": Path(primary_score_root),
        "challenge": Path(challenge_score_root),
    }
    scores = {
        cohort: _load_score_root(score_roots[cohort], key=key, expected_cohort=cohort)
        for cohort in COHORTS
    }
    holdout_manifest = authority["holdout_manifest"]
    holdout_manifest_raw = authority["holdout_manifest_raw"]
    gold_manifest = authority["gold_manifest"]
    gold_manifest_raw = authority["gold_manifest_raw"]
    for cohort in COHORTS:
        score_manifest = scores[cohort][0]
        if (
            score_manifest.get("source_gold_manifest_sha256")
            != _sha256_bytes(gold_manifest_raw)
            or score_manifest.get("source_gold_manifest_hmac_sha256")
            != gold_manifest["manifest_hmac_sha256"]
            or score_manifest.get("source_holdout_manifest_sha256")
            != _sha256_bytes(holdout_manifest_raw)
            or score_manifest.get("source_holdout_manifest_hmac_sha256")
            != holdout_manifest["manifest_hmac_sha256"]
            or score_manifest.get("release_evidence_class")
            != gold_manifest["release_evidence_class"]
        ):
            raise GmailTemporalOwnerAuditError(
                f"{cohort} score source-gold binding is invalid"
            )
    primary_manifest = scores["primary"][0]
    challenge_manifest = scores["challenge"][0]
    if (
        primary_manifest.get("bound_challenge_score_manifest_sha256")
        != _sha256_bytes(scores["challenge"][1])
        or primary_manifest.get("bound_challenge_score_manifest_hmac_sha256")
        != challenge_manifest["manifest_hmac_sha256"]
        or primary_manifest.get("bound_challenge_score_sha256")
        != _sha256_bytes(scores["challenge"][3])
        or primary_manifest.get("challenge_scoring_pending") is not False
    ):
        raise GmailTemporalOwnerAuditError(
            "primary score does not bind the exact challenge score"
        )

    artifacts: dict[str, bytes] = {}
    cohort_summary: dict[str, dict[str, Any]] = {}
    for cohort in COHORTS:
        score_manifest, _manifest_raw, _score, _score_raw = scores[cohort][:4]
        population_rows, population_raw, error_rows, error_raw = scores[cohort][4:]
        diagnostic_only = score_manifest["diagnostic_only"]
        source_rows = authority["source_rows"][cohort]
        gold_rows = authority["gold_rows"][cohort]
        population = _population_by_sample(
            population_rows,
            source_rows=source_rows,
            gold_rows=gold_rows,
            cohort=cohort,
            diagnostic_only=diagnostic_only,
        )
        validated_errors, error_counts, critical_error_count = _validate_error_ledger(
            error_rows,
            source_rows=source_rows,
            gold_rows=gold_rows,
            cohort=cohort,
            diagnostic_only=diagnostic_only,
        )
        if (
            score_manifest.get("owner_audit_error_category_counts") != error_counts
            or score_manifest.get("owner_audit_critical_error_record_count")
            != critical_error_count
        ):
            raise GmailTemporalOwnerAuditError(
                f"{cohort} score error coverage is incomplete"
            )
        label_queue, population_counts, selected_counts = _select_audit_sample(
            cohort,
            source_rows,
            gold_rows,
            population,
            key=key,
        )
        error_queue = _error_queue_rows(
            validated_errors,
            source_rows=source_rows,
            gold_rows=gold_rows,
        )
        label_raw = _jsonl_bytes(label_queue)
        error_queue_raw = _jsonl_bytes(error_queue) if error_queue else b""
        artifacts[LABEL_QUEUE_ARTIFACTS[cohort]] = label_raw
        artifacts[ERROR_QUEUE_ARTIFACTS[cohort]] = error_queue_raw
        cohort_summary[cohort] = {
            "gold_record_count": len(gold_rows),
            "label_audit_record_count": len(label_queue),
            "selection_stratum_population_counts": population_counts,
            "selection_stratum_selected_counts": selected_counts,
            "error_audit_record_count": len(error_queue),
            "error_category_counts": error_counts,
            "source_critical_error_count": critical_error_count,
            "source_cohort_gate_passed": score_manifest["cohort_gate_passed"],
        }
    evidence_class = str(gold_manifest["release_evidence_class"])
    retrospective_preview = evidence_class == finalizer.RETROSPECTIVE_EVIDENCE_CLASS
    manifest = {
        "version": PREPARE_MANIFEST_VERSION,
        "audit_version": VERSION,
        "source_holdout_manifest_sha256": _sha256_bytes(holdout_manifest_raw),
        "source_holdout_manifest_hmac_sha256": holdout_manifest["manifest_hmac_sha256"],
        "source_gold_manifest_sha256": _sha256_bytes(gold_manifest_raw),
        "source_gold_manifest_hmac_sha256": gold_manifest["manifest_hmac_sha256"],
        "source_primary_gold_sha256": _sha256_bytes(authority["gold_raw"]["primary"]),
        "source_challenge_gold_sha256": _sha256_bytes(
            authority["gold_raw"]["challenge"]
        ),
        "source_primary_score_manifest_sha256": _sha256_bytes(scores["primary"][1]),
        "source_primary_score_manifest_hmac_sha256": scores["primary"][0][
            "manifest_hmac_sha256"
        ],
        "source_primary_score_sha256": _sha256_bytes(scores["primary"][3]),
        "source_primary_owner_audit_population_sha256": _sha256_bytes(
            scores["primary"][5]
        ),
        "source_primary_owner_error_ledger_sha256": _sha256_bytes(scores["primary"][7]),
        "source_challenge_score_manifest_sha256": _sha256_bytes(scores["challenge"][1]),
        "source_challenge_score_manifest_hmac_sha256": scores["challenge"][0][
            "manifest_hmac_sha256"
        ],
        "source_challenge_score_sha256": _sha256_bytes(scores["challenge"][3]),
        "source_challenge_owner_audit_population_sha256": _sha256_bytes(
            scores["challenge"][5]
        ),
        "source_challenge_owner_error_ledger_sha256": _sha256_bytes(
            scores["challenge"][7]
        ),
        "artifact_sha256": {
            name: _sha256_bytes(raw) for name, raw in sorted(artifacts.items())
        },
        "cohort_audits": cohort_summary,
        "label_audit_fraction_numerator": AUDIT_NUMERATOR,
        "label_audit_fraction_denominator": AUDIT_DENOMINATOR,
        "label_audit_rounding": "ceiling",
        "selection_policy": "hmac_ranked_base_quota_plus_availability_capped_surface_coverage_v1",
        "error_coverage": "every_score_error_exactly_once_within_each_cohort",
        "estimands_must_not_be_pooled": True,
        "cohort_audit_denominators_are_separate": True,
        "release_evidence_class": evidence_class,
        "retrospective_preview_only": retrospective_preview,
        "source_release_holdout_eligible": bool(
            gold_manifest["release_holdout_eligible"]
        ),
        "source_primary_cohort_gate_passed": scores["primary"][0]["cohort_gate_passed"],
        "source_challenge_cohort_gate_passed": scores["challenge"][0][
            "cohort_gate_passed"
        ],
        "promotion_pending": True,
        "release_or_promotion_claimed": False,
        "owner_judgment_independence": "owner_attestation_not_cryptographically_independent",
        "private_file_mode": "0600",
        "private_directory_mode": "0700",
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    manifest_raw = _manifest_bytes(
        manifest,
        key=key,
        domain=PREPARE_MANIFEST_DOMAIN,
    )
    current_authority = _load_holdout_and_gold(
        Path(holdout_root), Path(gold_root), key=key
    )
    current_scores = {
        cohort: _load_score_root(score_roots[cohort], key=key, expected_cohort=cohort)
        for cohort in COHORTS
    }
    if (
        current_authority["holdout_manifest_raw"] != holdout_manifest_raw
        or current_authority["gold_manifest_raw"] != gold_manifest_raw
        or current_authority["gold_raw"] != authority["gold_raw"]
        or any(
            current_scores[cohort][index] != scores[cohort][index]
            for cohort in COHORTS
            for index in (1, 3, 5, 7)
        )
    ):
        raise GmailTemporalOwnerAuditError("owner audit evidence changed while read")
    _publish(Path(output_root), {**artifacts, MANIFEST_ARTIFACT: manifest_raw})
    return {
        "version": VERSION,
        "status": "prepared",
        "cohorts": {
            cohort: {
                "gold_records": cohort_summary[cohort]["gold_record_count"],
                "label_audit_records": cohort_summary[cohort][
                    "label_audit_record_count"
                ],
                "error_audit_records": cohort_summary[cohort][
                    "error_audit_record_count"
                ],
            }
            for cohort in COHORTS
        },
        "estimands_must_not_be_pooled": True,
        "retrospective_preview_only": retrospective_preview,
        "release_audit_gate_passed": False,
        "promotion_pending": True,
        "release_or_promotion_claimed": False,
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
    }


def _load_prepare_root(
    root: Path,
    *,
    key: bytes,
) -> tuple[
    dict[str, Any],
    bytes,
    dict[str, list[dict[str, Any]]],
    dict[str, bytes],
    dict[str, list[dict[str, Any]]],
    dict[str, bytes],
]:
    _private_directory(root, description="owner audit root")
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise GmailTemporalOwnerAuditError(
            "owner audit inventory is unavailable"
        ) from exc
    expected_names = {
        MANIFEST_ARTIFACT,
        *LABEL_QUEUE_ARTIFACTS.values(),
        *ERROR_QUEUE_ARTIFACTS.values(),
    }
    if {entry.name for entry in entries} != expected_names:
        raise GmailTemporalOwnerAuditError("owner audit inventory is not exact")
    manifest_raw = _private_file(
        root / MANIFEST_ARTIFACT, description="owner audit manifest"
    )
    manifest = _parse_json(manifest_raw, description="owner audit manifest")
    if (
        not isinstance(manifest, dict)
        or manifest_raw != _canonical_json(manifest) + b"\n"
        or manifest.get("version") != PREPARE_MANIFEST_VERSION
        or manifest.get("audit_version") != VERSION
    ):
        raise GmailTemporalOwnerAuditError("owner audit manifest is invalid")
    authenticator = manifest.get("manifest_hmac_sha256")
    unsigned = dict(manifest)
    unsigned.pop("manifest_hmac_sha256", None)
    expected_authenticator = hmac.new(
        key,
        PREPARE_MANIFEST_DOMAIN + _canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()
    if not isinstance(authenticator, str) or not hmac.compare_digest(
        authenticator, expected_authenticator
    ):
        raise GmailTemporalOwnerAuditError("owner audit manifest authentication failed")
    label_raws = {
        cohort: _private_file(
            root / LABEL_QUEUE_ARTIFACTS[cohort],
            description=f"{cohort} label audit queue",
        )
        for cohort in COHORTS
    }
    error_raws = {
        cohort: _private_file(
            root / ERROR_QUEUE_ARTIFACTS[cohort],
            description=f"{cohort} error audit queue",
        )
        for cohort in COHORTS
    }
    expected_hashes = {
        **{
            LABEL_QUEUE_ARTIFACTS[cohort]: _sha256_bytes(label_raws[cohort])
            for cohort in COHORTS
        },
        **{
            ERROR_QUEUE_ARTIFACTS[cohort]: _sha256_bytes(error_raws[cohort])
            for cohort in COHORTS
        },
    }
    if manifest.get("artifact_sha256") != dict(sorted(expected_hashes.items())):
        raise GmailTemporalOwnerAuditError("owner audit artifact commitment failed")
    labels = {
        cohort: _canonical_jsonl(
            label_raws[cohort], description=f"{cohort} label audit queue"
        )
        for cohort in COHORTS
    }
    errors = {
        cohort: _canonical_jsonl(
            error_raws[cohort],
            description=f"{cohort} error audit queue",
            allow_empty=True,
        )
        for cohort in COHORTS
    }
    cohort_audits = manifest.get("cohort_audits")
    if not isinstance(cohort_audits, dict) or set(cohort_audits) != set(COHORTS):
        raise GmailTemporalOwnerAuditError("owner audit cohort coverage is invalid")
    for cohort in COHORTS:
        summary = cohort_audits.get(cohort)
        if (
            not isinstance(summary, dict)
            or summary.get("label_audit_record_count") != len(labels[cohort])
            or summary.get("error_audit_record_count") != len(errors[cohort])
        ):
            raise GmailTemporalOwnerAuditError("owner audit queue coverage is invalid")
    return manifest, manifest_raw, labels, label_raws, errors, error_raws


_LABEL_AUDIT_FIELDS = {
    "audit_status",
    "owner_disposition",
    "corrected_label",
    "owner_found_critical_error",
    "owner_notes",
}
_ERROR_AUDIT_FIELDS = {
    "audit_status",
    "owner_disposition",
    "correction",
    "owner_found_critical_error",
    "owner_notes",
}


def _validate_completed_label_audit(
    source: list[dict[str, Any]],
    completed: list[dict[str, Any]],
) -> dict[str, int]:
    if len(source) != len(completed):
        raise GmailTemporalOwnerAuditError("completed label audit is partial")
    counts = Counter()
    seen: set[str] = set()
    for original, reviewed in zip(source, completed, strict=True):
        if set(original) != set(reviewed) or any(
            _canonical_json(reviewed.get(field)) != _canonical_json(original.get(field))
            for field in set(original) - _LABEL_AUDIT_FIELDS
        ):
            raise GmailTemporalOwnerAuditError(
                "completed label audit changed frozen evidence"
            )
        sample_id = reviewed.get("sample_id")
        disposition = reviewed.get("owner_disposition")
        critical = reviewed.get("owner_found_critical_error")
        correction = reviewed.get("corrected_label")
        notes = reviewed.get("owner_notes")
        if (
            not isinstance(sample_id, str)
            or sample_id in seen
            or reviewed.get("audit_status") != "reviewed"
            or disposition not in {"confirmed", "corrected"}
            or not isinstance(critical, bool)
            or (notes is not None and (not isinstance(notes, str) or not notes.strip()))
        ):
            raise GmailTemporalOwnerAuditError(
                "completed label audit disposition is invalid"
            )
        seen.add(sample_id)
        if disposition == "confirmed":
            if correction is not None:
                raise GmailTemporalOwnerAuditError(
                    "confirmed label audit contains a correction"
                )
            counts["confirmed"] += 1
        else:
            if not isinstance(correction, dict) or not isinstance(notes, str):
                raise GmailTemporalOwnerAuditError(
                    "corrected label audit lacks an explicit correction"
                )
            original_label = original["completed_label"]
            try:
                validated, _counts = finalizer._validate_completed_labels(
                    [original_label],
                    [correction],
                )
            except finalizer.GmailTemporalLabelFinalizerError as exc:
                raise GmailTemporalOwnerAuditError(
                    "corrected label is invalid"
                ) from exc
            if validated != [correction] or all(
                _canonical_json(correction.get(field))
                == _canonical_json(original_label.get(field))
                for field in finalizer._LABEL_FIELDS
            ):
                raise GmailTemporalOwnerAuditError(
                    "corrected label does not change the reviewed label"
                )
            counts["corrected"] += 1
        counts["owner_found_critical"] += int(critical)
    return {
        "confirmed": counts["confirmed"],
        "corrected": counts["corrected"],
        "owner_found_critical": counts["owner_found_critical"],
    }


def _validate_completed_error_audit(
    source: list[dict[str, Any]],
    completed: list[dict[str, Any]],
) -> dict[str, Any]:
    if len(source) != len(completed):
        raise GmailTemporalOwnerAuditError("completed error audit is partial")
    dispositions = Counter()
    categories = Counter()
    seen: set[str] = set()
    corrections = 0
    owner_critical = 0
    confirmed_critical = 0
    allowed_dispositions = {
        "confirmed_error",
        "corrected_gold_label",
        "corrected_system_output",
        "reclassified_non_error",
    }
    for original, reviewed in zip(source, completed, strict=True):
        if set(original) != set(reviewed) or any(
            _canonical_json(reviewed.get(field)) != _canonical_json(original.get(field))
            for field in set(original) - _ERROR_AUDIT_FIELDS
        ):
            raise GmailTemporalOwnerAuditError(
                "completed error audit changed frozen evidence"
            )
        error_id = reviewed.get("error_id")
        disposition = reviewed.get("owner_disposition")
        correction = reviewed.get("correction")
        critical = reviewed.get("owner_found_critical_error")
        notes = reviewed.get("owner_notes")
        category = original["error"].get("category")
        if (
            not isinstance(error_id, str)
            or error_id in seen
            or reviewed.get("audit_status") != "reviewed"
            or disposition not in allowed_dispositions
            or not isinstance(critical, bool)
            or not isinstance(notes, str)
            or not notes.strip()
        ):
            raise GmailTemporalOwnerAuditError(
                "completed error audit disposition is invalid"
            )
        seen.add(error_id)
        if disposition == "confirmed_error":
            if correction is not None:
                raise GmailTemporalOwnerAuditError(
                    "confirmed error audit contains a correction"
                )
            confirmed_critical += int(original["error"].get("critical") is True)
        elif not isinstance(correction, dict) or not correction:
            raise GmailTemporalOwnerAuditError(
                "changed error disposition lacks an explicit correction"
            )
        else:
            corrections += 1
        dispositions[str(disposition)] += 1
        categories[str(category)] += 1
        owner_critical += int(critical)
    return {
        "dispositions": dict(sorted(dispositions.items())),
        "categories": {
            category: categories[category] for category in sorted(_ERROR_CATEGORIES)
        },
        "corrections": corrections,
        "owner_found_critical": owner_critical,
        "confirmed_critical": confirmed_critical,
    }


def _assert_aggregate_only(value: Mapping[str, Any]) -> None:
    forbidden = ("_id", "path", "sha256", "text", "truth", "notes")

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if any(fragment in str(key).lower() for fragment in forbidden):
                    raise GmailTemporalOwnerAuditError(
                        "owner audit result contains private evidence"
                    )
                visit(nested)
        elif isinstance(item, (list, tuple, set, frozenset)):
            raise GmailTemporalOwnerAuditError(
                "owner audit result contains non-aggregate evidence"
            )

    visit(value)


def finalize_owner_audit(
    audit_root: Path,
    holdout_root: Path,
    gold_root: Path,
    primary_score_root: Path,
    challenge_score_root: Path,
    completed_primary_label_audit_path: Path,
    completed_primary_error_audit_path: Path,
    completed_challenge_label_audit_path: Path,
    completed_challenge_error_audit_path: Path,
    hmac_key_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Validate exact owner dispositions and publish aggregate-only evidence."""

    key = _hmac_key(Path(hmac_key_path))
    (
        audit_manifest,
        audit_manifest_raw,
        label_queues,
        label_queue_raws,
        error_queues,
        error_queue_raws,
    ) = _load_prepare_root(Path(audit_root), key=key)
    authority = _load_holdout_and_gold(Path(holdout_root), Path(gold_root), key=key)
    score_roots = {
        "primary": Path(primary_score_root),
        "challenge": Path(challenge_score_root),
    }
    scores = {
        cohort: _load_score_root(score_roots[cohort], key=key, expected_cohort=cohort)
        for cohort in COHORTS
    }
    if (
        audit_manifest.get("source_holdout_manifest_sha256")
        != _sha256_bytes(authority["holdout_manifest_raw"])
        or audit_manifest.get("source_holdout_manifest_hmac_sha256")
        != authority["holdout_manifest"]["manifest_hmac_sha256"]
        or audit_manifest.get("source_gold_manifest_sha256")
        != _sha256_bytes(authority["gold_manifest_raw"])
        or audit_manifest.get("source_gold_manifest_hmac_sha256")
        != authority["gold_manifest"]["manifest_hmac_sha256"]
        or audit_manifest.get("source_primary_gold_sha256")
        != _sha256_bytes(authority["gold_raw"]["primary"])
        or audit_manifest.get("source_challenge_gold_sha256")
        != _sha256_bytes(authority["gold_raw"]["challenge"])
        or any(
            audit_manifest.get(f"source_{cohort}_score_manifest_sha256")
            != _sha256_bytes(scores[cohort][1])
            or audit_manifest.get(f"source_{cohort}_score_manifest_hmac_sha256")
            != scores[cohort][0]["manifest_hmac_sha256"]
            or audit_manifest.get(f"source_{cohort}_score_sha256")
            != _sha256_bytes(scores[cohort][3])
            or audit_manifest.get(f"source_{cohort}_owner_audit_population_sha256")
            != _sha256_bytes(scores[cohort][5])
            or audit_manifest.get(f"source_{cohort}_owner_error_ledger_sha256")
            != _sha256_bytes(scores[cohort][7])
            for cohort in COHORTS
        )
    ):
        raise GmailTemporalOwnerAuditError("owner audit source evidence changed")
    completed_label_paths = {
        "primary": Path(completed_primary_label_audit_path),
        "challenge": Path(completed_challenge_label_audit_path),
    }
    completed_error_paths = {
        "primary": Path(completed_primary_error_audit_path),
        "challenge": Path(completed_challenge_error_audit_path),
    }
    completed_label_raws = {
        cohort: _private_file(
            completed_label_paths[cohort],
            description=f"completed {cohort} label audit",
        )
        for cohort in COHORTS
    }
    completed_error_raws = {
        cohort: _private_file(
            completed_error_paths[cohort],
            description=f"completed {cohort} error audit",
        )
        for cohort in COHORTS
    }
    cohort_results: dict[str, dict[str, Any]] = {}
    for cohort in COHORTS:
        completed_labels = _canonical_jsonl(
            completed_label_raws[cohort],
            description=f"completed {cohort} label audit",
        )
        completed_errors = _canonical_jsonl(
            completed_error_raws[cohort],
            description=f"completed {cohort} error audit",
            allow_empty=True,
        )
        label_counts = _validate_completed_label_audit(
            label_queues[cohort], completed_labels
        )
        error_counts = _validate_completed_error_audit(
            error_queues[cohort], completed_errors
        )
        rescore = bool(label_counts["corrected"] or error_counts["corrections"])
        owner_critical = (
            label_counts["owner_found_critical"] + error_counts["owner_found_critical"]
        )
        critical_errors = owner_critical + error_counts["confirmed_critical"]
        evidence_passed = not rescore and critical_errors == 0
        cohort_results[cohort] = {
            "label_audit_records": len(label_queues[cohort]),
            "label_audit_confirmed": label_counts["confirmed"],
            "label_audit_corrected": label_counts["corrected"],
            "error_audit_records": len(error_queues[cohort]),
            "error_audit_dispositions": error_counts["dispositions"],
            "error_category_counts": error_counts["categories"],
            "owner_found_critical_errors": owner_critical,
            "confirmed_source_critical_errors": error_counts["confirmed_critical"],
            "corrections": label_counts["corrected"] + error_counts["corrections"],
            "rescore_required": rescore,
            "owner_audit_evidence_passed": evidence_passed,
            "source_cohort_gate_passed": scores[cohort][0]["cohort_gate_passed"],
            "cohort_audit_prerequisite_passed": (
                evidence_passed and scores[cohort][0]["cohort_gate_passed"]
            ),
        }
    rescore_required = any(
        cohort_results[cohort]["rescore_required"] for cohort in COHORTS
    )
    owner_audit_evidence_passed = all(
        cohort_results[cohort]["owner_audit_evidence_passed"] for cohort in COHORTS
    )
    owner_audit_prerequisite_passed = all(
        cohort_results[cohort]["cohort_audit_prerequisite_passed"] for cohort in COHORTS
    )
    retrospective_preview = bool(audit_manifest["retrospective_preview_only"])
    result = {
        "version": FINAL_SCORE_VERSION,
        "status": "complete",
        "cohort_audits": cohort_results,
        "estimands_must_not_be_pooled": True,
        "cohort_audit_denominators_are_separate": True,
        "rescore_required": rescore_required,
        "owner_audit_complete": True,
        "owner_audit_evidence_passed": owner_audit_evidence_passed,
        "owner_audit_prerequisite_passed": owner_audit_prerequisite_passed,
        "owner_judgment_independence": "owner_attestation_not_cryptographically_independent",
        "retrospective_preview_only": retrospective_preview,
        "release_audit_gate_passed": False,
        "promotion_pending": True,
        "release_or_promotion_claimed": False,
        "pending_final_authenticated_rollup": True,
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    _assert_aggregate_only(result)
    result_raw = _canonical_json(result) + b"\n"
    manifest = {
        "version": FINAL_MANIFEST_VERSION,
        "audit_version": VERSION,
        "source_prepare_manifest_sha256": _sha256_bytes(audit_manifest_raw),
        "source_prepare_manifest_hmac_sha256": audit_manifest["manifest_hmac_sha256"],
        **{
            f"source_{cohort}_label_audit_queue_sha256": _sha256_bytes(
                label_queue_raws[cohort]
            )
            for cohort in COHORTS
        },
        **{
            f"source_{cohort}_error_audit_queue_sha256": _sha256_bytes(
                error_queue_raws[cohort]
            )
            for cohort in COHORTS
        },
        **{
            f"completed_{cohort}_label_audit_sha256": _sha256_bytes(
                completed_label_raws[cohort]
            )
            for cohort in COHORTS
        },
        **{
            f"completed_{cohort}_error_audit_sha256": _sha256_bytes(
                completed_error_raws[cohort]
            )
            for cohort in COHORTS
        },
        "source_gold_manifest_sha256": _sha256_bytes(authority["gold_manifest_raw"]),
        "source_gold_manifest_hmac_sha256": authority["gold_manifest"][
            "manifest_hmac_sha256"
        ],
        **{
            f"source_{cohort}_score_manifest_sha256": _sha256_bytes(scores[cohort][1])
            for cohort in COHORTS
        },
        **{
            f"source_{cohort}_score_manifest_hmac_sha256": scores[cohort][0][
                "manifest_hmac_sha256"
            ]
            for cohort in COHORTS
        },
        "artifact_sha256": {FINAL_SCORE_ARTIFACT: _sha256_bytes(result_raw)},
        "cohort_audits": cohort_results,
        "estimands_must_not_be_pooled": True,
        "rescore_required": rescore_required,
        "owner_audit_evidence_passed": owner_audit_evidence_passed,
        "owner_audit_prerequisite_passed": owner_audit_prerequisite_passed,
        "release_evidence_class": audit_manifest["release_evidence_class"],
        "retrospective_preview_only": retrospective_preview,
        "release_audit_gate_passed": False,
        "promotion_pending": True,
        "release_or_promotion_claimed": False,
        "pending_final_authenticated_rollup": True,
        "owner_judgment_independence": "owner_attestation_not_cryptographically_independent",
        "private_file_mode": "0600",
        "private_directory_mode": "0700",
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    manifest_raw = _manifest_bytes(manifest, key=key, domain=FINAL_MANIFEST_DOMAIN)
    if (
        any(
            _private_file(
                completed_label_paths[cohort],
                description=f"completed {cohort} label audit",
            )
            != completed_label_raws[cohort]
            or _private_file(
                completed_error_paths[cohort],
                description=f"completed {cohort} error audit",
            )
            != completed_error_raws[cohort]
            for cohort in COHORTS
        )
        or _load_prepare_root(Path(audit_root), key=key)[1] != audit_manifest_raw
        or _load_holdout_and_gold(Path(holdout_root), Path(gold_root), key=key)[
            "gold_manifest_raw"
        ]
        != authority["gold_manifest_raw"]
        or any(
            _load_score_root(score_roots[cohort], key=key, expected_cohort=cohort)[1]
            != scores[cohort][1]
            for cohort in COHORTS
        )
    ):
        raise GmailTemporalOwnerAuditError("owner audit evidence changed while read")
    _publish(
        Path(output_root),
        {FINAL_SCORE_ARTIFACT: result_raw, MANIFEST_ARTIFACT: manifest_raw},
    )
    return {
        "version": VERSION,
        "status": "complete",
        "cohorts": {
            cohort: {
                "label_audit_records": cohort_results[cohort]["label_audit_records"],
                "error_audit_records": cohort_results[cohort]["error_audit_records"],
                "corrections": cohort_results[cohort]["corrections"],
                "owner_audit_evidence_passed": cohort_results[cohort][
                    "owner_audit_evidence_passed"
                ],
                "cohort_audit_prerequisite_passed": cohort_results[cohort][
                    "cohort_audit_prerequisite_passed"
                ],
                "source_cohort_gate_passed": cohort_results[cohort][
                    "source_cohort_gate_passed"
                ],
            }
            for cohort in COHORTS
        },
        "rescore_required": rescore_required,
        "owner_audit_evidence_passed": owner_audit_evidence_passed,
        "owner_audit_prerequisite_passed": owner_audit_prerequisite_passed,
        "retrospective_preview_only": retrospective_preview,
        "release_audit_gate_passed": False,
        "promotion_pending": True,
        "release_or_promotion_claimed": False,
        "pending_final_authenticated_rollup": True,
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
    }


def _failure() -> None:
    print(
        json.dumps(
            {
                "version": VERSION,
                "status": "failed",
                "error": "gmail_temporal_owner_audit_failed",
                "private_content_printed": False,
            },
            sort_keys=True,
        )
    )
    raise SystemExit(2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--holdout-root", type=Path, required=True)
    prepare.add_argument("--gold-root", type=Path, required=True)
    prepare.add_argument("--primary-score-root", type=Path, required=True)
    prepare.add_argument("--challenge-score-root", type=Path, required=True)
    prepare.add_argument("--hmac-key", type=Path, required=True)
    prepare.add_argument("--output-root", type=Path, required=True)
    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--audit-root", type=Path, required=True)
    finalize.add_argument("--holdout-root", type=Path, required=True)
    finalize.add_argument("--gold-root", type=Path, required=True)
    finalize.add_argument("--primary-score-root", type=Path, required=True)
    finalize.add_argument("--challenge-score-root", type=Path, required=True)
    finalize.add_argument("--completed-primary-label-audit", type=Path, required=True)
    finalize.add_argument("--completed-primary-error-audit", type=Path, required=True)
    finalize.add_argument("--completed-challenge-label-audit", type=Path, required=True)
    finalize.add_argument("--completed-challenge-error-audit", type=Path, required=True)
    finalize.add_argument("--hmac-key", type=Path, required=True)
    finalize.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare_owner_audit(
                args.holdout_root,
                args.gold_root,
                args.primary_score_root,
                args.challenge_score_root,
                args.hmac_key,
                args.output_root,
            )
        else:
            result = finalize_owner_audit(
                args.audit_root,
                args.holdout_root,
                args.gold_root,
                args.primary_score_root,
                args.challenge_score_root,
                args.completed_primary_label_audit,
                args.completed_primary_error_audit,
                args.completed_challenge_label_audit,
                args.completed_challenge_error_audit,
                args.hmac_key,
                args.output_root,
            )
    except (GmailTemporalOwnerAuditError, OSError, ValueError, KeyError):
        _failure()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
