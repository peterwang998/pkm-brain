from __future__ import annotations

import copy
import hashlib
import hmac
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "audit_gmail_temporal_holdout.py"
FINALIZER_TEST_PATH = (
    ROOT / "tests" / "test_gmail_temporal_holdout_label_finalizer_script.py"
)
ADAPTER_TEST_PATH = (
    ROOT / "tests" / "test_gmail_temporal_holdout_candidate_gold_adapter_script.py"
)


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


audit = _load("test_gmail_temporal_owner_audit", SCRIPT_PATH)
gold_fixture = _load("owner_audit_gold_fixture", FINALIZER_TEST_PATH)
real_score_fixture = _load("owner_audit_real_score_fixture", ADAPTER_TEST_PATH)


def _write_private(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_bytes(raw)
    path.chmod(0o600)


def _make_gold(base: Path, *, retrospective: bool = True) -> dict[str, Any]:
    fixture = gold_fixture._fixture(base, release_eligible=True, challenge_count=100)
    if retrospective:
        reserve_order = [
            {
                "version": gold_fixture.finalizer.RESERVE_ORDER_VERSION,
                "position": position,
                "sample_id": f"gths_{position + 1_000:064x}",
                "thread_id": f"gtht_{position + 1_000:064x}",
            }
            for position in range(1, 76)
        ]
        reserve_bindings = [
            {
                "version": gold_fixture.finalizer.BINDING_VERSION,
                "sample_id": row["sample_id"],
                "selection_status": "sealed_reserve",
            }
            for row in reserve_order
        ]
        gold_fixture._rewrite_authenticated_artifact(
            fixture,
            "sealed-reserve/order.jsonl",
            gold_fixture.finalizer._jsonl_bytes(reserve_order),
        )
        gold_fixture._rewrite_authenticated_artifact(
            fixture,
            "sealed-reserve/bindings.jsonl",
            gold_fixture.finalizer._jsonl_bytes(reserve_bindings),
        )
        label_manifest_path = fixture["root"] / "label-queue/manifest.json"
        label_manifest = json.loads(label_manifest_path.read_bytes())
        label_manifest["release_holdout_eligible"] = False
        gold_fixture._rewrite_authenticated_artifact(
            fixture,
            "label-queue/manifest.json",
            gold_fixture.finalizer._canonical_json(label_manifest) + b"\n",
        )

        def make_retrospective(manifest: dict[str, Any]) -> None:
            manifest.update(
                {
                    "release_holdout_eligible": False,
                    "release_evidence_class": (
                        gold_fixture.finalizer.RETROSPECTIVE_EVIDENCE_CLASS
                    ),
                    "release_scope": "local_review_preview",
                    "prospective_unseen_source_evidence": False,
                    "historical_architecture_exposed": True,
                    "retrospective_calibration_eligible": True,
                    "semantic_development_overlap_status": (
                        "unknown_legacy_cohort_bindings_unrecoverable"
                    ),
                    "content_changing_canary_required": True,
                    "prior_development_overlap_proven_zero": False,
                    "development_baseline_primary_overlap_count": 150,
                    "development_baseline_reserve_overlap_count": 75,
                    "development_baseline_challenge_overlap_count": 100,
                    "reserve_sample_count": 75,
                    "release_evidence_class_applies_to": "primary_natural_cohort",
                    "prior_development_overlap_proven_zero_applies_to": (
                        "primary_and_reserve"
                    ),
                    "primary_evidence_scope": (
                        "retrospective_natural_operability_preview"
                    ),
                    "primary_prospective_unseen_source_evidence": False,
                    "primary_historical_architecture_exposed": True,
                    "challenge_evidence_scope": (
                        "historical_balanced_capability_stress_review_only"
                    ),
                    "challenge_prospective_unseen_source_evidence": False,
                    "challenge_historical_architecture_exposed": True,
                    "challenge_population_inference_eligible": False,
                    "challenge_required_as_separate_promotion_gate": True,
                    "cohort_metrics_must_not_be_pooled": True,
                    "freeze_authority_evidence_class": (
                        gold_fixture.finalizer.RETROSPECTIVE_EVIDENCE_CLASS
                    ),
                    "freeze_no_reroll_scope": (
                        gold_fixture.finalizer.FREEZE_NO_REROLL_SCOPE
                    ),
                    "freeze_authority_independently_reverified_downstream": False,
                    "freeze_irrevocable_from_first_materialization": True,
                    "labeled_cohort_reroll_forbidden": True,
                    "all_labeled_attempts_must_be_retained": True,
                    "source_labels_must_be_sealed_before_verifier_outputs_opened": (
                        True
                    ),
                    "primary_population_scope": (
                        "historical_baseline_thread_preview"
                    ),
                    "representative_gmail_production_eligible": False,
                    "prospective_existing_thread_update_gate_required": True,
                    "prospective_natural_recall_continuation_required": True,
                    "prospective_natural_material_minimum": 20,
                    "prospective_natural_effective_recall_minimum": 0.90,
                    "prospective_natural_recall_continuation_passed": False,
                    "underpowered_primary_action": (
                        "publish_failure_then_activate_sealed_reserve_in_"
                        "authenticated_order_for_regression_diagnostic_only_"
                        "then_fresh_150_100_75_required_for_release"
                    ),
                    "underpowered_challenge_action": (
                        "publish_underpowered_result_then_versioned_redesign_"
                        "no_reroll"
                    ),
                }
            )

        gold_fixture._rewrite_root_manifest(fixture, make_retrospective)
        label_authority = None
    completed_challenge = gold_fixture._completed_capability_challenge(fixture)
    if retrospective:
        label_authority = None
    else:
        label_authority = gold_fixture._label_authority_manifest(
            fixture, completed_challenge=completed_challenge
        )
    gold_fixture.finalizer.finalize_gmail_temporal_holdout_labels(
        fixture["root"],
        fixture["completed"],
        fixture["key"],
        fixture["output"],
        completed_challenge_labels_path=completed_challenge,
        label_authority_manifest_path=label_authority,
    )
    fixture["completed_challenge"] = completed_challenge
    return fixture


def _candidate_semantics(candidate_id: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "expression_id": f"expression-{candidate_id}",
        "subject_mention_id": f"subject-{candidate_id}",
        "lifecycle_mention_id": None,
        "relation": "occurrence",
        "kind": "resolved",
        "lifecycle": "scheduled",
        "normalized_value": "2027-08-14",
        "requires_defer": False,
        "blockers": [],
        "risk_features": [],
        "repair_flags": [],
    }


def _score_root(
    base: Path,
    fixture: dict[str, Any],
    *,
    cohort: str,
    errors: list[tuple[str, int, bool]] | None = None,
    bound_challenge: Path | None = None,
    cohort_gate_passed: bool = True,
) -> Path:
    root = base / f"{cohort}-score"
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    gold_name = (
        "gold.jsonl" if cohort == "primary" else "challenge-diagnostic-gold.jsonl"
    )
    gold_raw = (fixture["output"] / gold_name).read_bytes()
    gold_rows = [json.loads(line) for line in gold_raw.splitlines()]
    source_raw = (fixture["root"] / f"label-queue/{cohort}.jsonl").read_bytes()
    source_rows = [json.loads(line) for line in source_raw.splitlines()]
    source_by_id = {row["sample_id"]: row for row in source_rows}
    gold_manifest_raw = (fixture["output"] / "manifest.json").read_bytes()
    gold_manifest = json.loads(gold_manifest_raw)
    holdout_manifest_raw = (fixture["root"] / "manifest.json").read_bytes()
    holdout_manifest = json.loads(holdout_manifest_raw)
    diagnostic = (
        gold_manifest["release_evidence_class"]
        != gold_fixture.finalizer.PROSPECTIVE_EVIDENCE_CLASS
    )
    population: list[dict[str, Any]] = []
    for index, row in enumerate(gold_rows):
        source = source_by_id[row["sample_id"]]
        gold_hash = hashlib.sha256(audit._canonical_json(row)).hexdigest()
        population.append(
            {
                "version": audit._POPULATION_VERSION,
                "cohort": cohort,
                "diagnostic_only": diagnostic,
                "sample_id": row["sample_id"],
                "thread_id": row["thread_id"],
                "source_label_row_sha256": hashlib.sha256(
                    audit._canonical_json(source)
                ).hexdigest(),
                "completed_label_row_sha256": gold_hash,
                "gold_label_row_sha256": gold_hash,
                "expected_material": row["expected_material"],
                "expected_filter": row["expected_filter"],
                "hard_negative": row["hard_negative"],
                "supported_artifact": index % 3 == 0,
                "uncertain_sidecar": index % 7 == 0,
                "accepted_artifact": index % 3 == 0 or index % 7 == 0,
                "false_negative": index in {1, 2},
                "critical_calibration_error": False,
                "false_positive_artifact": index == 8,
                "unmatched_artifact": index == 9,
                "lifecycle_reschedule": index == 3,
                "lifecycle_cancellation": index == 4,
                "lifecycle_completion": index == 5,
                "timezone_sensitive": index == 6,
                "routable": False,
            }
        )
    requested_errors = errors
    if requested_errors is None:
        requested_errors = (
            [
                ("false_negative_member", 1, False),
                ("unmatched_artifact", 2, False),
                ("false_positive_artifact", 51, False),
            ]
            if cohort == "primary"
            else [
                ("false_negative_member", 1, False),
                ("unmatched_artifact", 2, False),
            ]
        )
    error_rows: list[dict[str, Any]] = []
    for sequence, (category, index, critical) in enumerate(requested_errors):
        row = gold_rows[index]
        source = source_by_id[row["sample_id"]]
        candidate_id = (
            f"candidate-{cohort}-{sequence}"
            if category == "critical_calibration_error"
            else None
        )
        artifact = category in {"unmatched_artifact", "false_positive_artifact"}
        candidate_ids = [f"artifact-candidate-{cohort}-{sequence}"] if artifact else []
        semantic_ids = candidate_ids or ([candidate_id] if candidate_id else [])
        identity = {
            "cohort": cohort,
            "category": category,
            "sample_id": row["sample_id"],
            "unit_id": f"unit-{sequence}"
            if category == "false_negative_member"
            else None,
            "member_id": (
                f"member-{sequence}" if category == "false_negative_member" else None
            ),
            "artifact_id": f"artifact-{sequence}" if artifact else None,
            "candidate_id": candidate_id,
        }
        gold_hash = hashlib.sha256(audit._canonical_json(row)).hexdigest()
        error_rows.append(
            {
                "version": audit._ERROR_VERSION,
                "error_id": "gtae_"
                + hashlib.sha256(audit._canonical_json(identity)).hexdigest(),
                "cohort": cohort,
                "diagnostic_only": diagnostic,
                "category": category,
                "sample_id": row["sample_id"],
                "thread_id": row["thread_id"],
                "source_label_row_sha256": hashlib.sha256(
                    audit._canonical_json(source)
                ).hexdigest(),
                "completed_label_row_sha256": gold_hash,
                "gold_label_row_sha256": gold_hash,
                "unit_id": identity["unit_id"],
                "member_id": identity["member_id"],
                "artifact_id": identity["artifact_id"],
                "artifact_kind": "supported_citation" if artifact else None,
                "candidate_id": candidate_id,
                "candidate_ids": candidate_ids,
                "candidate_semantics": [
                    _candidate_semantics(semantic_id) for semantic_id in semantic_ids
                ],
                "critical": critical,
                "routable": False,
            }
        )
    error_rows.sort(key=lambda row: audit._ERROR_CATEGORY_ORDER[row["category"]])
    population_raw = audit._jsonl_bytes(population)
    error_raw = audit._jsonl_bytes(error_rows) if error_rows else b""
    category_counts = dict(
        sorted(
            {
                category: sum(row["category"] == category for row in error_rows)
                for category in {row["category"] for row in error_rows}
            }.items()
        )
    )
    critical_count = sum(row["critical"] for row in error_rows)
    score = {
        "version": audit._SOURCE_SCORE_VERSION,
        "status": "scored",
        "cohort": cohort,
        "diagnostic_only": diagnostic,
        "estimands_must_not_be_pooled": True,
        "metrics_must_not_be_pooled_across_cohorts": True,
        "owner_audit_population_records": len(population),
        "owner_audit_error_records": len(error_rows),
        "owner_audit_error_category_counts": category_counts,
        "owner_audit_critical_error_records": critical_count,
        "owner_audit_population_version": audit._POPULATION_VERSION,
        "owner_audit_error_version": audit._ERROR_VERSION,
        "cohort_gate_passed": cohort_gate_passed,
        "promotion_pending": True,
        "release_score_gate_passed": False,
        "private_content_printed": False,
    }
    score_raw = audit._canonical_json(score) + b"\n"
    artifacts = {
        audit.SCORE_ARTIFACT: score_raw,
        audit.POPULATION_ARTIFACT: population_raw,
        audit.ERROR_LEDGER_ARTIFACT: error_raw,
    }
    challenge_manifest = (
        json.loads((bound_challenge / "manifest.json").read_bytes())
        if bound_challenge is not None
        else None
    )
    challenge_manifest_raw = (
        (bound_challenge / "manifest.json").read_bytes()
        if bound_challenge is not None
        else None
    )
    challenge_score_raw = (
        (bound_challenge / audit.SCORE_ARTIFACT).read_bytes()
        if bound_challenge is not None
        else None
    )
    manifest = {
        "version": audit._SOURCE_SCORE_MANIFEST_VERSION,
        "scorer_version": audit._SOURCE_SCORER_VERSION,
        "scorer_sha256": hashlib.sha256(audit._SCORER_PATH.read_bytes()).hexdigest(),
        "candidate_evaluator_sha256": hashlib.sha256(
            audit._CANDIDATE_EVALUATOR_PATH.read_bytes()
        ).hexdigest(),
        "ensemble_evaluator_sha256": hashlib.sha256(
            audit._ENSEMBLE_EVALUATOR_PATH.read_bytes()
        ).hexdigest(),
        "source_holdout_manifest_sha256": hashlib.sha256(
            holdout_manifest_raw
        ).hexdigest(),
        "source_holdout_manifest_hmac_sha256": holdout_manifest["manifest_hmac_sha256"],
        "source_gold_manifest_sha256": hashlib.sha256(gold_manifest_raw).hexdigest(),
        "source_gold_manifest_hmac_sha256": gold_manifest["manifest_hmac_sha256"],
        "bound_challenge_score_manifest_sha256": (
            hashlib.sha256(challenge_manifest_raw).hexdigest()
            if challenge_manifest_raw is not None
            else None
        ),
        "bound_challenge_score_manifest_hmac_sha256": (
            challenge_manifest["manifest_hmac_sha256"]
            if challenge_manifest is not None
            else None
        ),
        "bound_challenge_score_sha256": (
            hashlib.sha256(challenge_score_raw).hexdigest()
            if challenge_score_raw is not None
            else None
        ),
        "release_evidence_class": gold_manifest["release_evidence_class"],
        "artifact_sha256": {
            name: hashlib.sha256(raw).hexdigest()
            for name, raw in sorted(artifacts.items())
        },
        "owner_audit_population_artifact": audit.POPULATION_ARTIFACT,
        "owner_audit_population_version": audit._POPULATION_VERSION,
        "owner_audit_population_record_count": len(population),
        "owner_audit_population_coverage": "exact_selected_cohort_record_order",
        "owner_audit_error_artifact": audit.ERROR_LEDGER_ARTIFACT,
        "owner_audit_error_version": audit._ERROR_VERSION,
        "owner_audit_error_record_count": len(error_rows),
        "owner_audit_error_category_counts": category_counts,
        "owner_audit_critical_error_record_count": critical_count,
        "owner_audit_error_categories_are_disjoint": False,
        "owner_audit_error_coverage": (
            "exact_critical_fp_fn_and_unmatched_artifact_identities"
        ),
        "record_count": len(population),
        "cohort": cohort,
        "diagnostic_only": diagnostic,
        "estimands_must_not_be_pooled": True,
        "metrics_must_not_be_pooled_across_cohorts": True,
        "cohort_gate_passed": cohort_gate_passed,
        "promotion_pending": True,
        "source_release_holdout_eligible": gold_manifest["release_holdout_eligible"],
        "release_holdout_eligible": gold_manifest["release_holdout_eligible"],
        "release_score_gate_passed": False,
        "challenge_scoring_pending": cohort == "primary" and bound_challenge is None,
        "private_file_mode": "0600",
        "private_directory_mode": "0700",
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    unsigned = audit._canonical_json(manifest)
    authenticator = hmac.new(
        fixture["key_value"],
        audit._SOURCE_SCORE_MANIFEST_DOMAIN + unsigned,
        hashlib.sha256,
    ).hexdigest()
    for name, raw in artifacts.items():
        _write_private(root / name, raw)
    _write_private(
        root / "manifest.json",
        audit._canonical_json({**manifest, "manifest_hmac_sha256": authenticator})
        + b"\n",
    )
    return root


def _score_pair(
    base: Path,
    fixture: dict[str, Any],
    *,
    primary_errors: list[tuple[str, int, bool]] | None = None,
    challenge_errors: list[tuple[str, int, bool]] | None = None,
    primary_gate: bool = True,
    challenge_gate: bool = True,
) -> tuple[Path, Path]:
    challenge = _score_root(
        base,
        fixture,
        cohort="challenge",
        errors=challenge_errors,
        cohort_gate_passed=challenge_gate,
    )
    primary = _score_root(
        base,
        fixture,
        cohort="primary",
        errors=primary_errors,
        bound_challenge=challenge,
        cohort_gate_passed=primary_gate,
    )
    return primary, challenge


def _complete_queues(audit_root: Path, base: Path) -> dict[str, dict[str, Path]]:
    output: dict[str, dict[str, Path]] = {}
    for cohort in audit.COHORTS:
        labels = [
            json.loads(line)
            for line in (audit_root / audit.LABEL_QUEUE_ARTIFACTS[cohort])
            .read_bytes()
            .splitlines()
        ]
        errors = [
            json.loads(line)
            for line in (audit_root / audit.ERROR_QUEUE_ARTIFACTS[cohort])
            .read_bytes()
            .splitlines()
        ]
        for row in labels:
            row.update(
                {
                    "audit_status": "reviewed",
                    "owner_disposition": "confirmed",
                    "corrected_label": None,
                    "owner_found_critical_error": False,
                    "owner_notes": None,
                }
            )
        for row in errors:
            row.update(
                {
                    "audit_status": "reviewed",
                    "owner_disposition": "confirmed_error",
                    "correction": None,
                    "owner_found_critical_error": False,
                    "owner_notes": "Confirmed against frozen private context.",
                }
            )
        label_path = base / f"completed-{cohort}-labels.jsonl"
        error_path = base / f"completed-{cohort}-errors.jsonl"
        _write_private(label_path, audit._jsonl_bytes(labels))
        _write_private(error_path, audit._jsonl_bytes(errors) if errors else b"")
        output[cohort] = {"labels": label_path, "errors": error_path}
    return output


def _prepare_fixture(
    tmp_path: Path,
    *,
    retrospective: bool = True,
    primary_errors: list[tuple[str, int, bool]] | None = None,
    challenge_errors: list[tuple[str, int, bool]] | None = None,
    primary_gate: bool = True,
    challenge_gate: bool = True,
) -> tuple[dict[str, Any], Path, Path, Path]:
    fixture = _make_gold(tmp_path / "authority", retrospective=retrospective)
    primary, challenge = _score_pair(
        tmp_path / "scores",
        fixture,
        primary_errors=primary_errors,
        challenge_errors=challenge_errors,
        primary_gate=primary_gate,
        challenge_gate=challenge_gate,
    )
    audit_root = tmp_path / "audit"
    audit.prepare_owner_audit(
        fixture["root"],
        fixture["output"],
        primary,
        challenge,
        fixture["key"],
        audit_root,
    )
    return fixture, primary, challenge, audit_root


def _finalize(
    fixture: dict[str, Any],
    primary: Path,
    challenge: Path,
    audit_root: Path,
    completed: dict[str, dict[str, Path]],
    output: Path,
) -> dict[str, Any]:
    return audit.finalize_owner_audit(
        audit_root,
        fixture["root"],
        fixture["output"],
        primary,
        challenge,
        completed["primary"]["labels"],
        completed["primary"]["errors"],
        completed["challenge"]["labels"],
        completed["challenge"]["errors"],
        fixture["key"],
        output,
    )


def test_prepare_freezes_separate_quarters_all_errors_and_private_context(
    tmp_path: Path,
) -> None:
    fixture, primary, challenge, audit_root = _prepare_fixture(tmp_path)
    result = audit.prepare_owner_audit(
        fixture["root"],
        fixture["output"],
        primary,
        challenge,
        fixture["key"],
        tmp_path / "audit-repeat",
    )

    assert result["cohorts"]["primary"] == {
        "gold_records": 150,
        "label_audit_records": 38,
        "error_audit_records": 3,
    }
    assert result["cohorts"]["challenge"] == {
        "gold_records": 100,
        "label_audit_records": 25,
        "error_audit_records": 2,
    }
    assert result["promotion_pending"] is True
    assert result["release_or_promotion_claimed"] is False
    manifest = json.loads((audit_root / "manifest.json").read_bytes())
    assert manifest["estimands_must_not_be_pooled"] is True
    for cohort in audit.COHORTS:
        assert (audit_root / audit.LABEL_QUEUE_ARTIFACTS[cohort]).read_bytes() == (
            tmp_path / "audit-repeat" / audit.LABEL_QUEUE_ARTIFACTS[cohort]
        ).read_bytes()
        summary = manifest["cohort_audits"][cohort]
        for stratum in (
            "material",
            "non_material",
            "hard_negative",
            "supported_artifact",
            "uncertain_sidecar",
            "false_negative",
            *sorted(audit._LIFECYCLE_STRATA),
        ):
            assert summary["selection_stratum_population_counts"][stratum] > 0
            assert summary["selection_stratum_selected_counts"][stratum] > 0
        errors = [
            json.loads(line)
            for line in (audit_root / audit.ERROR_QUEUE_ARTIFACTS[cohort])
            .read_bytes()
            .splitlines()
        ]
        assert all("source_label" in row and "completed_label" in row for row in errors)
        artifact_error = next(
            row for row in errors if row["error"]["category"] == "unmatched_artifact"
        )
        assert artifact_error["error"]["candidate_semantics"]
        assert (
            audit_root / audit.LABEL_QUEUE_ARTIFACTS[cohort]
        ).stat().st_mode & 0o777 == 0o600


def test_finalize_retrospective_is_aggregate_prerequisite_not_release(
    tmp_path: Path,
) -> None:
    fixture, primary, challenge, audit_root = _prepare_fixture(tmp_path)
    completed = _complete_queues(audit_root, tmp_path)
    result = _finalize(
        fixture, primary, challenge, audit_root, completed, tmp_path / "final"
    )

    assert result["cohorts"]["primary"]["label_audit_records"] == 38
    assert result["cohorts"]["challenge"]["label_audit_records"] == 25
    assert result["owner_audit_prerequisite_passed"] is True
    assert result["retrospective_preview_only"] is True
    assert result["release_audit_gate_passed"] is False
    assert result["release_or_promotion_claimed"] is False
    raw = (tmp_path / "final" / audit.FINAL_SCORE_ARTIFACT).read_text()
    assert "Meeting" not in raw
    assert "gths_" not in raw
    assert str(tmp_path) not in raw


def test_prospective_audit_still_emits_only_pending_prerequisite(
    tmp_path: Path,
) -> None:
    fixture, primary, challenge, audit_root = _prepare_fixture(
        tmp_path, retrospective=False
    )
    completed = _complete_queues(audit_root, tmp_path)
    result = _finalize(
        fixture, primary, challenge, audit_root, completed, tmp_path / "final"
    )

    assert result["retrospective_preview_only"] is False
    assert result["owner_audit_prerequisite_passed"] is True
    assert result["promotion_pending"] is True
    assert result["release_audit_gate_passed"] is False


def test_real_finalizer_adapter_dual_scorer_owner_audit_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "real-pipeline"
    raw = real_score_fixture._fixture(base, monkeypatch, include_challenge=True)
    challenge = real_score_fixture._add_scoring_evidence(
        raw,
        base=base,
        cohort="challenge",
    )
    real_score_fixture.scorer.score_gmail_temporal_holdout(
        challenge["output"],
        challenge["key"],
        challenge["checkpoints"],
        challenge["attestations"],
        challenge["score_output"],
    )
    primary = real_score_fixture._add_scoring_evidence(
        raw,
        base=base,
        cohort="primary",
    )
    real_score_fixture.scorer.score_gmail_temporal_holdout(
        primary["output"],
        primary["key"],
        primary["checkpoints"],
        primary["attestations"],
        primary["score_output"],
        challenge_score_root=challenge["score_output"],
    )
    audit_root = base / "owner-audit"
    prepared = audit.prepare_owner_audit(
        raw["root"],
        raw["gold"],
        primary["score_output"],
        challenge["score_output"],
        raw["key"],
        audit_root,
    )
    completed = _complete_queues(audit_root, base)
    result = audit.finalize_owner_audit(
        audit_root,
        raw["root"],
        raw["gold"],
        primary["score_output"],
        challenge["score_output"],
        completed["primary"]["labels"],
        completed["primary"]["errors"],
        completed["challenge"]["labels"],
        completed["challenge"]["errors"],
        raw["key"],
        base / "owner-audit-final",
    )

    assert prepared["cohorts"]["primary"]["gold_records"] == 1
    assert prepared["cohorts"]["challenge"]["gold_records"] == 1
    assert result["cohorts"]["primary"]["label_audit_records"] == 1
    assert result["cohorts"]["challenge"]["label_audit_records"] == 1
    assert result["owner_audit_prerequisite_passed"] is True
    assert result["release_or_promotion_claimed"] is False


def test_label_correction_requires_rescore_without_patching_either_estimand(
    tmp_path: Path,
) -> None:
    fixture, primary, challenge, audit_root = _prepare_fixture(tmp_path)
    completed = _complete_queues(audit_root, tmp_path)
    labels_path = completed["challenge"]["labels"]
    labels = [json.loads(line) for line in labels_path.read_bytes().splitlines()]
    target = next(row for row in labels if row["completed_label"]["hard_negative"])
    corrected = copy.deepcopy(target["completed_label"])
    corrected["hard_negative"] = False
    target.update(
        {
            "owner_disposition": "corrected",
            "corrected_label": corrected,
            "owner_notes": "Corrected challenge classification.",
        }
    )
    _write_private(labels_path, audit._jsonl_bytes(labels))

    result = _finalize(
        fixture, primary, challenge, audit_root, completed, tmp_path / "corrected"
    )
    assert result["rescore_required"] is True
    assert result["cohorts"]["challenge"]["corrections"] == 1
    assert result["owner_audit_prerequisite_passed"] is False


def test_confirmed_critical_unmatched_error_hard_fails(tmp_path: Path) -> None:
    fixture, primary, challenge, audit_root = _prepare_fixture(
        tmp_path,
        challenge_errors=[("unmatched_artifact", 2, True)],
    )
    completed = _complete_queues(audit_root, tmp_path)
    result = _finalize(
        fixture, primary, challenge, audit_root, completed, tmp_path / "critical"
    )

    assert result["cohorts"]["challenge"]["owner_audit_evidence_passed"] is False
    assert result["owner_audit_prerequisite_passed"] is False


def test_cohort_gate_not_release_score_controls_prerequisite(tmp_path: Path) -> None:
    fixture, primary, challenge, audit_root = _prepare_fixture(
        tmp_path, challenge_gate=False
    )
    completed = _complete_queues(audit_root, tmp_path)
    result = _finalize(
        fixture, primary, challenge, audit_root, completed, tmp_path / "failed-gate"
    )

    assert result["owner_audit_evidence_passed"] is True
    assert result["cohorts"]["challenge"]["source_cohort_gate_passed"] is False
    assert result["owner_audit_prerequisite_passed"] is False


def test_partial_tampered_or_unbound_cohort_evidence_fails_closed(
    tmp_path: Path,
) -> None:
    fixture, primary, challenge, audit_root = _prepare_fixture(tmp_path)
    completed = _complete_queues(audit_root, tmp_path)
    primary_labels = completed["primary"]["labels"]
    labels = [json.loads(line) for line in primary_labels.read_bytes().splitlines()]
    _write_private(primary_labels, audit._jsonl_bytes(labels[:-1]))
    with pytest.raises(audit.GmailTemporalOwnerAuditError, match="partial"):
        _finalize(
            fixture, primary, challenge, audit_root, completed, tmp_path / "partial"
        )

    queue = audit_root / audit.LABEL_QUEUE_ARTIFACTS["challenge"]
    queue.write_bytes(queue.read_bytes() + b"{}\n")
    with pytest.raises(audit.GmailTemporalOwnerAuditError, match="commitment"):
        audit._load_prepare_root(audit_root, key=fixture["key_value"])


def test_population_lineage_or_primary_challenge_binding_drift_fails(
    tmp_path: Path,
) -> None:
    fixture = _make_gold(tmp_path / "authority")
    primary, challenge = _score_pair(tmp_path / "scores", fixture)
    population_path = challenge / audit.POPULATION_ARTIFACT
    rows = [json.loads(line) for line in population_path.read_bytes().splitlines()]
    rows[0]["gold_label_row_sha256"] = "0" * 64
    population_raw = audit._jsonl_bytes(rows)
    _write_private(population_path, population_raw)
    manifest_path = challenge / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["artifact_sha256"][audit.POPULATION_ARTIFACT] = hashlib.sha256(
        population_raw
    ).hexdigest()
    unsigned = dict(manifest)
    unsigned.pop("manifest_hmac_sha256")
    authenticator = hmac.new(
        fixture["key_value"],
        audit._SOURCE_SCORE_MANIFEST_DOMAIN + audit._canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()
    _write_private(
        manifest_path,
        audit._canonical_json({**unsigned, "manifest_hmac_sha256": authenticator})
        + b"\n",
    )
    with pytest.raises(audit.GmailTemporalOwnerAuditError):
        audit.prepare_owner_audit(
            fixture["root"],
            fixture["output"],
            primary,
            challenge,
            fixture["key"],
            tmp_path / "audit",
        )


@pytest.mark.parametrize(
    "field",
    ("scorer_sha256", "candidate_evaluator_sha256", "ensemble_evaluator_sha256"),
)
def test_stale_scorer_or_evaluator_hash_fails_closed(
    tmp_path: Path, field: str
) -> None:
    fixture = _make_gold(tmp_path / "authority")
    primary, challenge = _score_pair(tmp_path / "scores", fixture)
    manifest_path = primary / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest[field] = "0" * 64
    unsigned = dict(manifest)
    unsigned.pop("manifest_hmac_sha256")
    authenticator = hmac.new(
        fixture["key_value"],
        audit._SOURCE_SCORE_MANIFEST_DOMAIN + audit._canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()
    _write_private(
        manifest_path,
        audit._canonical_json({**unsigned, "manifest_hmac_sha256": authenticator})
        + b"\n",
    )

    with pytest.raises(audit.GmailTemporalOwnerAuditError, match="manifest is invalid"):
        audit.prepare_owner_audit(
            fixture["root"],
            fixture["output"],
            primary,
            challenge,
            fixture["key"],
            tmp_path / "audit",
        )


def test_zero_count_strata_and_ceil_rounding_are_explicit() -> None:
    source_rows = []
    gold_rows = []
    population = {}
    for index in range(5):
        source = {"sample_id": f"sample-{index}"}
        gold = {
            "sample_id": source["sample_id"],
            "expected_material": index < 2,
            "hard_negative": False,
            "semantic_units": [],
        }
        row = {stratum: False for stratum in audit._SUPPLEMENTAL_STRATA - {"ambiguity"}}
        source_rows.append(source)
        gold_rows.append(gold)
        population[source["sample_id"]] = row

    selected, population_counts, selected_counts = audit._select_audit_sample(
        "primary", source_rows, gold_rows, population, key=b"x" * 32
    )

    assert len(selected) == 2
    assert population_counts["lifecycle_cancellation"] == 0
    assert selected_counts["lifecycle_cancellation"] == 0


def test_cli_failure_is_generic_and_hides_private_paths(tmp_path: Path) -> None:
    private = tmp_path / "PRIVATE-MAILBOX-NAME"
    private.mkdir(mode=0o700)
    key = private / "key"
    _write_private(key, b"x" * 32)

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "prepare",
            "--holdout-root",
            str(private / "missing-holdout"),
            "--gold-root",
            str(private / "missing-gold"),
            "--primary-score-root",
            str(private / "missing-primary-score"),
            "--challenge-score-root",
            str(private / "missing-challenge-score"),
            "--hmac-key",
            str(key),
            "--output-root",
            str(tmp_path / "output"),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 2
    assert json.loads(completed.stdout) == {
        "error": "gmail_temporal_owner_audit_failed",
        "private_content_printed": False,
        "status": "failed",
        "version": audit.VERSION,
    }
    assert "PRIVATE-MAILBOX-NAME" not in completed.stdout
    assert completed.stderr == ""
