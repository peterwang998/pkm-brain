from __future__ import annotations

import copy
import hashlib
import hmac
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT_PATH = ROOT / "scripts" / "finalize_gmail_temporal_holdout_labels.py"
ADAPTER_PATH = ROOT / "scripts" / "prepare_gmail_temporal_holdout_candidate_gold.py"
RUNNER_PATH = ROOT / "scripts" / "run_gmail_temporal_holdout_external.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "test_finalize_gmail_temporal_holdout_labels",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


finalizer = _load()


def _load_runner() -> Any:
    spec = importlib.util.spec_from_file_location(
        "test_external_runner_from_finalizer",
        RUNNER_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


external_runner = _load_runner()


def _load_adapter() -> Any:
    spec = importlib.util.spec_from_file_location(
        "test_prepare_gmail_temporal_holdout_candidate_gold_from_finalizer",
        ADAPTER_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


adapter = _load_adapter()


def _write_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    os.chmod(path, 0o600)


def _locator(
    expression: str,
    subject: str,
    *,
    relation: str,
    quality: str = "exact",
    verdict: str = "supported",
) -> dict[str, Any]:
    return {
        "quality": quality,
        "expected_verdict": verdict,
        "locator": {
            "expression": {
                "surface": expression,
                "form": "explicit_date",
                "field": "message",
            },
            "subject": {
                "surface": subject,
                "mention_type": "event" if relation == "occurrence" else "action",
                "field": "message",
            },
            "lifecycle_mention": None,
            "derived": {
                "relation": relation,
                "kind": "planned",
                "lifecycle": "none",
                "normalized_value": (
                    "2027-08-14" if relation == "occurrence" else "2027-08-15"
                ),
                "requires_defer": False,
            },
        },
    }


def _semantic_units(index: int) -> list[dict[str, Any]]:
    cohort_index = index % 10_000
    uncertain = cohort_index == 49
    units = [
        {
            "unit_id": f"meeting_{index}",
            "truth": f"Meeting {index} is planned for 2027-08-14.",
            "baseline_frontier_grade": finalizer.BASELINE_GRADE_PLACEHOLDER,
            "members": [
                {
                    "member_id": "occurrence",
                    "expected_verdict": "uncertain" if uncertain else "supported",
                    "baseline_frontier_grade": finalizer.BASELINE_GRADE_PLACEHOLDER,
                    "alternatives": [
                        _locator(
                            "August 14, 2027",
                            f"Meeting {index}",
                            relation="occurrence",
                            quality="partial" if uncertain else "exact",
                            verdict="uncertain" if uncertain else "supported",
                        )
                    ],
                }
            ],
        }
    ]
    if cohort_index < 30:
        units.append(
            {
                "unit_id": f"report_{index}",
                "truth": f"Report {index} is due by 2027-08-15.",
                "baseline_frontier_grade": finalizer.BASELINE_GRADE_PLACEHOLDER,
                "members": [
                    {
                        "member_id": "deadline",
                        "expected_verdict": "supported",
                        "baseline_frontier_grade": (
                            finalizer.BASELINE_GRADE_PLACEHOLDER
                        ),
                        "alternatives": [
                            _locator(
                                "August 15, 2027",
                                f"Submit report {index}",
                                relation="deadline",
                            )
                        ],
                    }
                ],
            }
        )
    return units


def _queue_row(index: int) -> dict[str, Any]:
    text = (
        f"Meeting {index} is scheduled for August 14, 2027. "
        f"Submit report {index} by August 15, 2027."
    )
    return {
        "version": finalizer.LABEL_QUEUE_VERSION,
        "sample_id": f"gths_{index:064x}",
        "thread_id": f"gtht_{index:064x}",
        "target": {
            "message_internal_at": "2027-08-01T09:00:00-07:00",
            "text": text,
            "source_char_count": len(text),
            "emitted_char_count": len(text),
            "sanitized_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "body_truncation_status": "not_indicated",
        },
        "thread_context": {
            "prior_available": 0,
            "prior_included": 0,
            "prior_omitted": 0,
            "later_available": 0,
            "later_included": 0,
            "later_omitted": 0,
            "source_omitted_before_count": 0,
            "source_truncated_message_count": 0,
            "messages": [],
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


def _complete(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = copy.deepcopy(rows)
    for index, row in enumerate(output):
        material = index < 50
        row.update(
            {
                "label_status": "labeled",
                "expected_material": material,
                "expected_filter": "should_admit" if material else "should_suppress",
                "hard_negative": 50 <= index < 90,
                "semantic_units": _semantic_units(index) if material else [],
                "critical_error": "none",
                "notes": None,
            }
        )
    return output


def _authenticated_builder_manifest(
    unsigned: dict[str, Any],
    key: bytes,
) -> bytes:
    authenticator = hmac.new(
        key,
        finalizer.BUILDER_MANIFEST_DOMAIN + finalizer._canonical_json(unsigned),
        hashlib.sha256,
    ).hexdigest()
    return (
        finalizer._canonical_json({**unsigned, "manifest_hmac_sha256": authenticator})
        + b"\n"
    )


def _fixture(
    base: Path,
    *,
    release_eligible: bool = True,
    challenge_count: int = 100,
) -> dict[str, Any]:
    base.mkdir(parents=True)
    os.chmod(base, 0o700)
    key_value = b"holdout-test-authentication-key!"
    assert len(key_value) >= 32
    key = base / "holdout.key"
    _write_private(key, key_value)
    root = base / "holdout"
    label_root = root / "label-queue"
    label_root.mkdir(parents=True)
    os.chmod(root, 0o700)
    os.chmod(label_root, 0o700)

    row_count = 150 if release_eligible else 90
    rows = [_queue_row(index) for index in range(row_count)]
    challenge_rows = [_queue_row(10_000 + index) for index in range(challenge_count)]
    primary_raw = finalizer._jsonl_bytes(rows)
    challenge_raw = finalizer._jsonl_bytes(challenge_rows)
    label_manifest = {
        "version": finalizer.LABEL_MANIFEST_VERSION,
        "primary_count": len(rows),
        "challenge_count": challenge_count,
        "primary_sha256": hashlib.sha256(primary_raw).hexdigest(),
        "challenge_sha256": hashlib.sha256(challenge_raw).hexdigest(),
        "diagnostic_denominator": "primary_only",
        "pipeline_predictions_present": False,
        "admission_decisions_present": False,
        "selection_strata_present": False,
        "release_holdout_eligible": release_eligible,
        "label_time_basis": finalizer.LABEL_TIME_BASIS,
        "later_context_policy": finalizer.LATER_CONTEXT_POLICY,
    }
    label_manifest_raw = finalizer._canonical_json(label_manifest) + b"\n"
    artifacts = {
        "label-queue/primary.jsonl": primary_raw,
        "label-queue/challenge.jsonl": challenge_raw,
        "label-queue/manifest.json": label_manifest_raw,
    }
    if release_eligible:

        def request_ids(index: int) -> list[str]:
            count = 2 if index == 0 else 1 if index == 1 else 0
            return [
                "gtrq_" + f"{index * 10 + sequence + 1:064x}"
                for sequence in range(count)
            ]

        authority_samples = [
            {
                "version": finalizer.SAMPLE_VERSION,
                "sample_id": row["sample_id"],
                "thread_id": row["thread_id"],
                "message_internal_at": row["target"]["message_internal_at"],
                "text": row["target"]["text"],
                "sanitized_text_sha256": row["target"]["sanitized_text_sha256"],
                "source_sha256": f"{index + 1:064x}",
                "stratum": "important_fact" if index < 50 else "noise_not_admitted",
                "expressions": [],
                "mentions": [],
                "leads": [],
                "preparation": {
                    "admission_basis": "fact" if index < 50 else "not_admitted",
                    "request_fingerprints": request_ids(index),
                    "page_count": len(request_ids(index)),
                },
                "analysis_fingerprint": f"analysis-{index}",
                "batch_plan_fingerprint": f"batch-{index}",
                "routable": False,
            }
            for index, row in enumerate(rows)
        ]
        authority_bindings = [
            {
                "version": finalizer.BINDING_VERSION,
                "sample_id": row["sample_id"],
                "source_sha256": row["source_sha256"],
                "analysis_fingerprint": row["analysis_fingerprint"],
                "batch_plan_fingerprint": row["batch_plan_fingerprint"],
                "routable": False,
            }
            for index, row in enumerate(authority_samples)
        ]
        challenge_authority_samples = [
            {
                "version": finalizer.SAMPLE_VERSION,
                "sample_id": row["sample_id"],
                "thread_id": row["thread_id"],
                "message_internal_at": row["target"]["message_internal_at"],
                "text": row["target"]["text"],
                "sanitized_text_sha256": row["target"]["sanitized_text_sha256"],
                "source_sha256": f"{20_000 + index:064x}",
                "stratum": "noise_not_admitted",
                "expressions": [],
                "mentions": [],
                "leads": [],
                "preparation": {
                    "admission_basis": "not_admitted",
                    "request_fingerprints": [],
                    "page_count": 0,
                },
                "analysis_fingerprint": f"challenge-analysis-{index}",
                "batch_plan_fingerprint": f"challenge-batch-{index}",
                "routable": False,
            }
            for index, row in enumerate(challenge_rows)
        ]
        challenge_authority_bindings = [
            {
                "version": finalizer.BINDING_VERSION,
                "sample_id": row["sample_id"],
                "source_sha256": row["source_sha256"],
                "analysis_fingerprint": row["analysis_fingerprint"],
                "batch_plan_fingerprint": row["batch_plan_fingerprint"],
                "routable": False,
            }
            for index, row in enumerate(challenge_authority_samples)
        ]
        request_rows = [
            {
                "version": finalizer.REQUEST_VERSION,
                "sample_id": sample["sample_id"],
                "request_fingerprint": request_fingerprint,
                "batch_fingerprint": f"batch-request-{index}-{sequence}",
                "frontier_fingerprint": f"frontier-{index}-{sequence}",
                "page_plan_fingerprint": f"page-plan-{index}-{sequence}",
                "page_fingerprint": f"page-{index}-{sequence}",
                "candidate_count": 1,
                "payload": {"request_fingerprint": request_fingerprint},
                "routable": False,
            }
            for index, sample in enumerate(authority_samples)
            for sequence, request_fingerprint in enumerate(
                sample["preparation"]["request_fingerprints"]
            )
        ]
        reserve_order = [
            {
                "version": finalizer.RESERVE_ORDER_VERSION,
                "position": position,
                "sample_id": f"gths_{position + 1_000:064x}",
                "thread_id": f"gtht_{position + 1_000:064x}",
            }
            for position in range(1, 76)
        ]
        reserve_bindings = [
            {
                "version": finalizer.BINDING_VERSION,
                "sample_id": row["sample_id"],
                "selection_status": "sealed_reserve",
            }
            for row in reserve_order
        ]
        artifacts.update(
            {
                "evaluation-authority/primary-samples.jsonl": (
                    finalizer._jsonl_bytes(authority_samples)
                ),
                "evaluation-authority/challenge-samples.jsonl": (
                    finalizer._jsonl_bytes(challenge_authority_samples)
                ),
                "evaluation-authority/primary-bindings.jsonl": (
                    finalizer._jsonl_bytes(authority_bindings)
                ),
                "evaluation-authority/challenge-bindings.jsonl": (
                    finalizer._jsonl_bytes(challenge_authority_bindings)
                ),
                "evaluation-authority/primary-requests.jsonl": (
                    finalizer._jsonl_bytes(request_rows)
                ),
                "evaluation-authority/challenge-requests.jsonl": b"",
                "sealed-reserve/order.jsonl": finalizer._jsonl_bytes(reserve_order),
                "sealed-reserve/bindings.jsonl": finalizer._jsonl_bytes(
                    reserve_bindings
                ),
            }
        )
        assert set(artifacts) == finalizer._RELEASE_REQUIRED_ARTIFACTS
    for name, payload in artifacts.items():
        _write_private(root / name, payload)
        os.chmod((root / name).parent, 0o700)
    root_manifest = {
        "version": finalizer.BUILDER_MANIFEST_VERSION,
        "builder_version": "gmail_temporal_holdout_builder_v5",
        "builder_sha256": hashlib.sha256(
            (ROOT / "scripts/build_gmail_temporal_holdout.py").read_bytes()
        ).hexdigest(),
        "primary_sample_count": len(rows),
        "primary_thread_count": len(rows),
        "challenge_sample_count": challenge_count,
        "reserve_sample_count": 75 if release_eligible else 0,
        "primary_request_count": 3 if release_eligible else 0,
        "challenge_request_count": 0,
        "artifact_sha256": {
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in sorted(artifacts.items())
        },
        "label_status": "unlabeled",
        "diagnostic_denominator": "primary_only",
        "labeler_artifact": "label-queue/primary.jsonl",
        "labeler_must_not_inspect_internal_artifacts": True,
        "thread_policy": "at_most_one_message_per_thread",
        "label_time_basis": finalizer.LABEL_TIME_BASIS,
        "later_context_policy": finalizer.LATER_CONTEXT_POLICY,
        "release_holdout_eligible": release_eligible,
        "release_evidence_class": (
            finalizer.PROSPECTIVE_EVIDENCE_CLASS
            if release_eligible
            else finalizer.DIAGNOSTIC_EVIDENCE_CLASS
        ),
        "release_evidence_class_applies_to": "primary_natural_cohort",
        "prior_development_overlap_proven_zero_applies_to": "primary_and_reserve",
        "primary_evidence_scope": (
            "prospective_natural_operability_review_only"
            if release_eligible
            else "diagnostic_natural_operability"
        ),
        "primary_prospective_unseen_source_evidence": release_eligible,
        "primary_historical_architecture_exposed": False,
        "challenge_evidence_scope": (
            "historical_balanced_capability_stress_review_only"
            if release_eligible
            else "diagnostic_balanced_capability_stress"
        ),
        "challenge_prospective_unseen_source_evidence": False,
        "challenge_historical_architecture_exposed": release_eligible,
        "challenge_population_inference_eligible": False,
        "challenge_required_as_separate_promotion_gate": True,
        "cohort_metrics_must_not_be_pooled": True,
        "freeze_authority_version": finalizer.FREEZE_AUTHORITY_VERSION,
        "freeze_attempt_version": finalizer.FREEZE_ATTEMPT_VERSION,
        "freeze_outcome_version": finalizer.FREEZE_OUTCOME_VERSION,
        "freeze_authority_manifest_sha256": "d" * 64,
        "freeze_attempt_id": "gthfa_" + "e" * 64,
        "freeze_attempt_sha256": "f" * 64,
        "freeze_milestone": "fixture-release-1",
        "freeze_authority_evidence_class": (
            finalizer.PROSPECTIVE_EVIDENCE_CLASS
            if release_eligible
            else finalizer.DIAGNOSTIC_EVIDENCE_CLASS
        ),
        "freeze_authority_status": finalizer.CANONICAL_FREEZE_AUTHORITY_STATUS,
        "freeze_no_reroll_scope": finalizer.FREEZE_NO_REROLL_SCOPE,
        "freeze_authority_independently_reverified_downstream": False,
        "legacy_signed_freeze_claims_downgraded": False,
        "freeze_irrevocable_from_first_materialization": True,
        "labeled_cohort_reroll_forbidden": True,
        "all_labeled_attempts_must_be_retained": True,
        "source_labels_must_be_sealed_before_verifier_outputs_opened": True,
        "primary_population_scope": (
            "new_thread_only_unseen"
            if release_eligible
            else "diagnostic_unrestricted"
        ),
        "representative_gmail_production_eligible": False,
        "prospective_existing_thread_update_gate_required": release_eligible,
        "prospective_natural_recall_continuation_required": release_eligible,
        "prospective_natural_material_minimum": 20,
        "prospective_natural_effective_recall_minimum": 0.90,
        "prospective_natural_recall_continuation_passed": False,
        "underpowered_primary_action": (
            "publish_failure_then_activate_sealed_reserve_in_authenticated_order_for_regression_diagnostic_only_then_fresh_150_100_75_required_for_release"
        ),
        "underpowered_challenge_action": (
            "publish_underpowered_result_then_versioned_redesign_no_reroll"
        ),
        "release_scope": "local_review_only" if release_eligible else "diagnostic_only",
        "prospective_unseen_source_evidence": release_eligible,
        "historical_architecture_exposed": False,
        "retrospective_calibration_eligible": False,
        "semantic_development_overlap_status": (
            "excluded_by_frozen_thread_scope"
            if release_eligible
            else "not_release_evidence"
        ),
        "automatic_apply_eligible": False,
        "content_changing_canary_required": release_eligible,
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    if release_eligible:
        root_manifest.update(
            {
                "development_baseline_present": True,
                "development_baseline_manifest_version": (
                    finalizer.DEVELOPMENT_BASELINE_MANIFEST_VERSION
                ),
                "development_baseline_thread_scope_version": (
                    finalizer.DEVELOPMENT_BASELINE_THREAD_SCOPE_VERSION
                ),
                "prior_development_overlap_proven_zero": True,
                "development_baseline_primary_overlap_count": 0,
                "development_baseline_reserve_overlap_count": 0,
                "development_baseline_challenge_overlap_count": challenge_count,
                "development_baseline_corpus_fingerprint": "gtdb_c_" + "a" * 64,
                "development_baseline_artifact_set_sha256": "b" * 64,
                "development_baseline_manifest_sha256": "c" * 64,
            }
        )
    _write_private(
        root / "manifest.json",
        _authenticated_builder_manifest(root_manifest, key_value),
    )
    completed_rows = _complete(rows)
    completed = base / "completed.jsonl"
    _write_private(completed, finalizer._jsonl_bytes(completed_rows))
    return {
        "root": root,
        "key": key,
        "key_value": key_value,
        "completed": completed,
        "completed_rows": completed_rows,
        "challenge_rows": challenge_rows,
        "output": base / "gold",
    }


def _rewrite_completed(fixture: dict[str, Any]) -> None:
    _write_private(
        fixture["completed"],
        finalizer._jsonl_bytes(fixture["completed_rows"]),
    )


def _rewrite_root_manifest(fixture: dict[str, Any], mutation: Any) -> None:
    path = fixture["root"] / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest.pop("manifest_hmac_sha256")
    mutation(manifest)
    _write_private(
        path,
        _authenticated_builder_manifest(manifest, fixture["key_value"]),
    )


def _rewrite_authenticated_artifact(
    fixture: dict[str, Any],
    name: str,
    payload: bytes,
) -> None:
    _write_private(fixture["root"] / name, payload)

    def update(manifest: dict[str, Any]) -> None:
        manifest["artifact_sha256"][name] = hashlib.sha256(payload).hexdigest()

    _rewrite_root_manifest(fixture, update)


def _label_authority_manifest(
    fixture: dict[str, Any],
    *,
    completed_challenge: Path | None = None,
) -> Path:
    challenge_rows = (
        [json.loads(line) for line in completed_challenge.read_text().splitlines()]
        if completed_challenge is not None
        else [
            {
                **row,
                "label_status": "labeled",
                "expected_material": False,
                "expected_filter": "should_suppress",
                "hard_negative": True,
                "semantic_units": [],
                "critical_error": "none",
                "notes": None,
            }
            for row in fixture["challenge_rows"]
        ]
    )
    completed_rows = [*fixture["completed_rows"], *challenge_rows]
    labels = {str(row["sample_id"]): row for row in completed_rows}

    def invoke(
        request: dict[str, Any],
        _schema: dict[str, Any],
        _model: str,
        _effort: str,
        _timeout: int,
    ) -> dict[str, Any]:
        return {
            "version": external_runner.LABEL_RESPONSE_VERSION,
            "labels": [
                {
                    "sample_id": row["sample_id"],
                    **{
                        field: copy.deepcopy(labels[str(row["sample_id"])][field])
                        for field in finalizer._LABEL_FIELDS
                    },
                }
                for row in request["records"]
            ],
        }

    run_root = fixture["root"].parent
    external_runner.run_labels(
        fixture["root"],
        fixture["key"],
        run_root,
        batch_size=8,
        invoke=invoke,
    )
    fixture["completed"] = run_root / "completed-primary.jsonl"
    return run_root / "label-authority.json"


def _completed_negative_challenge(fixture: dict[str, Any]) -> Path:
    rows = copy.deepcopy(fixture["challenge_rows"])
    for row in rows:
        row.update(
            {
                "label_status": "labeled",
                "expected_material": False,
                "expected_filter": "should_suppress",
                "hard_negative": True,
                "semantic_units": [],
                "critical_error": "none",
                "notes": None,
            }
        )
    path = fixture["root"].parent / "completed-challenge.jsonl"
    _write_private(path, finalizer._jsonl_bytes(rows))
    return path


def _completed_capability_challenge(fixture: dict[str, Any]) -> Path:
    rows = copy.deepcopy(fixture["challenge_rows"])
    for index, row in enumerate(rows):
        material = index < 50
        row.update(
            {
                "label_status": "labeled",
                "expected_material": material,
                "expected_filter": ("should_admit" if material else "should_suppress"),
                "hard_negative": 50 <= index < 90,
                "semantic_units": (_semantic_units(10_000 + index) if material else []),
                "critical_error": "none",
                "notes": None,
            }
        )
    path = fixture["root"].parent / "completed-challenge.jsonl"
    _write_private(path, finalizer._jsonl_bytes(rows))
    return path


def test_finalizes_exact_private_gold_with_authenticated_aggregate(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "valid")
    completed_challenge = _completed_capability_challenge(fixture)
    label_authority = _label_authority_manifest(
        fixture,
        completed_challenge=completed_challenge,
    )

    result = finalizer.finalize_gmail_temporal_holdout_labels(
        fixture["root"],
        fixture["completed"],
        fixture["key"],
        fixture["output"],
        completed_challenge_labels_path=completed_challenge,
        label_authority_manifest_path=label_authority,
    )

    assert result["records"] == 150
    assert result["expected_material"] == 50
    assert result["labeled_hard_negatives"] == 40
    assert result["semantic_members"] == 80
    assert result["supported_members"] == 79
    assert result["label_gate_passed"] is True
    assert result["primary_label_data_gate_passed"] is True
    assert result["challenge_label_data_gate_passed"] is True
    assert result["release_holdout_eligible"] is True
    assert result["challenge_diagnostic_ready"] is True
    assert result["challenge_records"] == 100
    assert result["external_calls"] == result["persistence_calls"] == 0
    assert "Meeting" not in json.dumps(result)
    assert stat.S_IMODE(fixture["output"].stat().st_mode) == 0o700
    assert stat.S_IMODE((fixture["output"] / "gold.jsonl").stat().st_mode) == 0o600
    manifest_raw = (fixture["output"] / "manifest.json").read_bytes()
    manifest = json.loads(manifest_raw)
    assert manifest["version"] == finalizer.GOLD_MANIFEST_VERSION
    assert manifest["finalizer_version"] == finalizer.VERSION
    assert manifest["finalizer_sha256"] == hashlib.sha256(
        SCRIPT_PATH.read_bytes()
    ).hexdigest()
    authenticator = manifest.pop("manifest_hmac_sha256")
    expected = hmac.new(
        fixture["key_value"],
        finalizer.GOLD_MANIFEST_DOMAIN + finalizer._canonical_json(manifest),
        hashlib.sha256,
    ).hexdigest()
    assert hmac.compare_digest(authenticator, expected)
    assert manifest["label_gate_passed"] is True
    assert manifest["primary_estimand"] == "natural_mail_population_operability"
    assert manifest["challenge_estimand"].endswith("stress_recall")
    assert manifest["estimands_must_not_be_pooled"] is True
    assert manifest["primary_prospective_unseen_source_evidence"] is True
    assert manifest["primary_historical_architecture_exposed"] is False
    assert manifest["challenge_prospective_unseen_source_evidence"] is False
    assert manifest["challenge_historical_architecture_exposed"] is True
    assert manifest["development_baseline_challenge_overlap_count"] == 100
    assert manifest["labeled_cohort_reroll_forbidden"] is True
    assert manifest["all_labeled_attempts_must_be_retained"] is True
    assert manifest["freeze_irrevocable_from_first_materialization"] is True
    assert manifest["freeze_no_reroll_scope"] == finalizer.FREEZE_NO_REROLL_SCOPE
    assert manifest["freeze_authority_independently_reverified_downstream"] is False
    assert (
        manifest["freeze_authority_status"]
        == finalizer.CANONICAL_FREEZE_AUTHORITY_STATUS
    )
    assert manifest["freeze_attempt_id"] == "gthfa_" + "e" * 64
    assert manifest["freeze_milestone"] == "fixture-release-1"
    assert manifest["legacy_signed_freeze_claims_downgraded"] is False
    assert manifest["primary_population_scope"] == "new_thread_only_unseen"
    assert manifest["representative_gmail_production_eligible"] is False
    assert manifest["prospective_natural_recall_continuation_passed"] is False
    assert manifest["semantic_member_count"] == 80
    assert manifest["baseline_frontier_grade_human_controlled"] is False
    assert manifest["baseline_frontier_grade_input_placeholder"] == (
        finalizer.BASELINE_GRADE_PLACEHOLDER
    )
    assert manifest["release_structural_scorability_verified"] is True
    assert manifest["sol_label_authority_attested"] is True
    assert (
        manifest["source_labels_sealed_before_verifier_outputs_opened_attested"]
        is True
    )
    assert manifest["label_authority_provenance_cryptographically_verified"] is False
    assert manifest["label_chronology_verified"] is True
    assert manifest["label_off_ledger_activity_absence_proven"] is False
    assert manifest["label_logical_run_id"].startswith("gthxr_r_")
    assert manifest["label_completed_at"] >= manifest["label_started_at"]
    assert manifest["label_authority_model"] == "gpt-5.6-sol"
    assert manifest["label_authority_reasoning_effort"] == "medium"


def test_finalizes_retrospective_calibration_as_preview_only(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "retrospective")
    label_manifest_path = fixture["root"] / "label-queue/manifest.json"
    label_manifest = json.loads(label_manifest_path.read_bytes())
    label_manifest["release_holdout_eligible"] = False
    _rewrite_authenticated_artifact(
        fixture,
        "label-queue/manifest.json",
        finalizer._canonical_json(label_manifest) + b"\n",
    )

    def make_retrospective(manifest: dict[str, Any]) -> None:
        manifest.update(
            {
                "release_holdout_eligible": False,
                "release_evidence_class": finalizer.RETROSPECTIVE_EVIDENCE_CLASS,
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
                "primary_evidence_scope": ("retrospective_natural_operability_preview"),
                "primary_prospective_unseen_source_evidence": False,
                "primary_historical_architecture_exposed": True,
                "challenge_evidence_scope": (
                    "historical_balanced_capability_stress_review_only"
                ),
                "challenge_prospective_unseen_source_evidence": False,
                "challenge_historical_architecture_exposed": True,
                    "primary_population_scope": "historical_baseline_thread_preview",
                    "prospective_existing_thread_update_gate_required": True,
                    "freeze_authority_evidence_class": (
                        finalizer.RETROSPECTIVE_EVIDENCE_CLASS
                    ),
                }
            )

    _rewrite_root_manifest(fixture, make_retrospective)
    result = finalizer.finalize_gmail_temporal_holdout_labels(
        fixture["root"],
        fixture["completed"],
        fixture["key"],
        fixture["output"],
    )

    assert result["release_holdout_eligible"] is False
    assert result["release_evidence_class"] == (finalizer.RETROSPECTIVE_EVIDENCE_CLASS)
    assert result["release_scope"] == "local_review_preview"
    assert result["retrospective_calibration_eligible"] is True
    manifest = json.loads((fixture["output"] / "manifest.json").read_bytes())
    assert manifest["release_holdout_eligible"] is False
    assert manifest["automatic_apply_eligible"] is False
    assert manifest["content_changing_canary_required"] is True
    assert manifest["primary_minimum_labeled_hard_negatives"] == 40
    assert manifest["challenge_minimum_expected_material_records"] == 30
    assert manifest["challenge_minimum_supported_members"] == 30
    assert manifest["candidate_gold_adapter_required"] is True
    assert manifest["direct_candidate_gold_evaluator_ready"] is False
    assert manifest["release_structural_scorability_verified"] is False

    # Regression: a historical challenge cohort is review-only, not
    # diagnostic.  Exercise the authenticated finalizer-to-adapter handoff so
    # this cannot regress to deriving the flag from root release eligibility.
    key = adapter.finalizer._private_hmac_key(fixture["key"])
    holdout_manifest, holdout_raw, holdout_artifacts = adapter._load_holdout(
        fixture["root"],
        key=key,
    )
    adapted_gold_manifest, _gold_raw, rows = adapter._load_gold(
        fixture["output"],
        key=key,
        holdout_manifest=holdout_manifest,
        holdout_manifest_raw=holdout_raw,
        holdout_artifacts=holdout_artifacts,
        cohort="primary",
    )
    assert len(rows) == 150
    assert adapted_gold_manifest["challenge_diagnostic_only"] is False
    assert adapted_gold_manifest["challenge_evidence_scope"] == (
        "historical_balanced_capability_stress_review_only"
    )
    assert manifest["challenge_expected_record_count"] == 100
    assert manifest["challenge_diagnostic_ready"] is False
    assert manifest["challenge_contributes_to_primary_release_gates"] is False
    assert not (fixture["output"] / "challenge-diagnostic-gold.jsonl").exists()
    with pytest.raises(
        finalizer.GmailTemporalLabelFinalizerError,
        match="already exists",
    ):
        finalizer.finalize_gmail_temporal_holdout_labels(
            fixture["root"],
            fixture["completed"],
            fixture["key"],
            fixture["output"],
        )


def test_rejects_tampered_sol_label_authority(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "tampered-label-authority")
    completed_challenge = _completed_negative_challenge(fixture)
    label_authority = _label_authority_manifest(
        fixture,
        completed_challenge=completed_challenge,
    )
    value = json.loads(label_authority.read_bytes())
    value["model"] = "gpt-5.6-luna"
    _write_private(label_authority, finalizer._canonical_json(value) + b"\n")

    with pytest.raises(
        finalizer.GmailTemporalLabelFinalizerError,
        match="label authority manifest is invalid",
    ):
        finalizer.finalize_gmail_temporal_holdout_labels(
            fixture["root"],
            fixture["completed"],
            fixture["key"],
            fixture["output"],
            completed_challenge_labels_path=completed_challenge,
            label_authority_manifest_path=label_authority,
        )


def test_optionally_finalizes_challenge_labels_as_diagnostic_only(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "challenge")
    challenge_completed = copy.deepcopy(fixture["challenge_rows"])
    for row in challenge_completed:
        row.update(
            {
                "label_status": "labeled",
                "expected_material": False,
                "expected_filter": "should_suppress",
                "hard_negative": True,
                "semantic_units": [],
                "critical_error": "none",
                "notes": None,
            }
        )
    completed_path = tmp_path / "challenge" / "completed-challenge.jsonl"
    _write_private(completed_path, finalizer._jsonl_bytes(challenge_completed))

    result = finalizer.finalize_gmail_temporal_holdout_labels(
        fixture["root"],
        fixture["completed"],
        fixture["key"],
        fixture["output"],
        completed_challenge_labels_path=completed_path,
    )

    assert result["challenge_diagnostic_ready"] is True
    assert result["challenge_records"] == 100
    assert (fixture["output"] / "challenge-diagnostic-gold.jsonl").is_file()
    manifest = json.loads((fixture["output"] / "manifest.json").read_bytes())
    assert manifest["challenge_completed"] is True
    assert manifest["challenge_diagnostic_only"] is False
    assert manifest["challenge_required_as_separate_promotion_gate"] is True
    assert manifest["challenge_label_data_gate_passed"] is False
    assert manifest["label_gate_passed"] is False
    assert manifest["challenge_contributes_to_primary_release_gates"] is False
    assert manifest["challenge_gold_record_count"] == 100
    assert (
        manifest["challenge_gold_sha256"]
        == hashlib.sha256(
            (fixture["output"] / "challenge-diagnostic-gold.jsonl").read_bytes()
        ).hexdigest()
    )
    assert (
        manifest["completed_challenge_labels_sha256"]
        == hashlib.sha256(completed_path.read_bytes()).hexdigest()
    )


def test_personal_release_allows_thirty_useful_records_with_sixty_members(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "thirty-useful")
    for row in fixture["completed_rows"][30:50]:
        row.update(
            {
                "expected_material": False,
                "expected_filter": "should_suppress",
                "hard_negative": False,
                "semantic_units": [],
            }
        )
    _rewrite_completed(fixture)

    result = finalizer.finalize_gmail_temporal_holdout_labels(
        fixture["root"],
        fixture["completed"],
        fixture["key"],
        fixture["output"],
    )

    assert result["expected_material"] == 30
    assert result["semantic_members"] == 60
    assert result["supported_members"] == 60
    assert result["primary_label_data_gate_passed"] is True
    assert result["challenge_label_data_gate_passed"] is False
    assert result["label_gate_passed"] is False


def test_low_primary_hard_negative_prevalence_finalizes_but_fails_data_gate(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "low-primary-hard-negatives")
    fixture["completed_rows"][89]["hard_negative"] = False
    _rewrite_completed(fixture)

    result = finalizer.finalize_gmail_temporal_holdout_labels(
        fixture["root"],
        fixture["completed"],
        fixture["key"],
        fixture["output"],
    )

    assert result["labeled_hard_negatives"] == 39
    assert result["primary_label_data_gate_passed"] is False
    assert result["label_gate_passed"] is False


def test_rejects_incomplete_optional_challenge_labels(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path / "challenge-incomplete")
    completed_path = tmp_path / "challenge-incomplete" / "completed-challenge.jsonl"
    one_row = copy.deepcopy(fixture["challenge_rows"][:1])
    one_row[0].update(
        {
            "label_status": "labeled",
            "expected_material": False,
            "expected_filter": "should_suppress",
            "hard_negative": True,
            "semantic_units": [],
            "critical_error": "none",
            "notes": None,
        }
    )
    _write_private(completed_path, finalizer._jsonl_bytes(one_row))

    with pytest.raises(
        finalizer.GmailTemporalLabelFinalizerError,
        match="exactly cover",
    ):
        finalizer.finalize_gmail_temporal_holdout_labels(
            fixture["root"],
            fixture["completed"],
            fixture["key"],
            fixture["output"],
            completed_challenge_labels_path=completed_path,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("source", "immutable source field"),
        ("order", "order or coverage"),
        ("calibration", "calibration"),
        ("grounding", "not grounded"),
        ("negative_units", "negative record"),
        ("human_baseline_grade", "evaluator-owned placeholder"),
    ],
)
def test_rejects_mutated_or_incomplete_human_gold(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    fixture = _fixture(tmp_path / mutation)
    rows = fixture["completed_rows"]
    if mutation == "source":
        rows[0]["target"]["text"] += " changed"
    elif mutation == "order":
        rows[0], rows[1] = rows[1], rows[0]
    elif mutation == "calibration":
        alternative = rows[0]["semantic_units"][0]["members"][0]["alternatives"][0]
        alternative["quality"] = "partial"
    elif mutation == "grounding":
        locator = rows[0]["semantic_units"][0]["members"][0]["alternatives"][0]
        locator["locator"]["expression"]["surface"] = "September 99, 2027"
    elif mutation == "negative_units":
        rows[50]["semantic_units"] = copy.deepcopy(rows[0]["semantic_units"])
    elif mutation == "human_baseline_grade":
        rows[0]["semantic_units"][0]["baseline_frontier_grade"] = "exact"
    _rewrite_completed(fixture)

    with pytest.raises(finalizer.GmailTemporalLabelFinalizerError, match=message):
        finalizer.finalize_gmail_temporal_holdout_labels(
            fixture["root"],
            fixture["completed"],
            fixture["key"],
            fixture["output"],
        )


def test_rejects_tampered_artifact_or_manifest_authentication(tmp_path: Path) -> None:
    artifact_fixture = _fixture(tmp_path / "artifact")
    _write_private(
        artifact_fixture["root"] / "label-queue" / "challenge.jsonl",
        b"tampered\n",
    )
    with pytest.raises(
        finalizer.GmailTemporalLabelFinalizerError,
        match="artifact commitment",
    ):
        finalizer.finalize_gmail_temporal_holdout_labels(
            artifact_fixture["root"],
            artifact_fixture["completed"],
            artifact_fixture["key"],
            artifact_fixture["output"],
        )

    manifest_fixture = _fixture(tmp_path / "manifest")
    manifest_path = manifest_fixture["root"] / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["primary_sample_count"] += 1
    _write_private(manifest_path, finalizer._canonical_json(manifest) + b"\n")
    with pytest.raises(
        finalizer.GmailTemporalLabelFinalizerError,
        match="authentication",
    ):
        finalizer.finalize_gmail_temporal_holdout_labels(
            manifest_fixture["root"],
            manifest_fixture["completed"],
            manifest_fixture["key"],
            manifest_fixture["output"],
        )


@pytest.mark.parametrize(
    ("case", "mutation"),
    [
        (
            "baseline_absent",
            lambda value: value.update(development_baseline_present=False),
        ),
        (
            "baseline_manifest_version",
            lambda value: value.update(
                development_baseline_manifest_version=(
                    "gmail_temporal_development_baseline_manifest_v1"
                )
            ),
        ),
        (
            "baseline_thread_scope_version",
            lambda value: value.update(
                development_baseline_thread_scope_version=(
                    "gmail_temporal_development_thread_scope_v0"
                )
            ),
        ),
        (
            "overlap_not_proven",
            lambda value: value.update(prior_development_overlap_proven_zero=False),
        ),
        (
            "primary_overlap",
            lambda value: value.update(development_baseline_primary_overlap_count=1),
        ),
        (
            "reserve_overlap",
            lambda value: value.update(development_baseline_reserve_overlap_count=1),
        ),
        (
            "corpus_fingerprint",
            lambda value: value.update(development_baseline_corpus_fingerprint="bad"),
        ),
        (
            "artifact_set_hash",
            lambda value: value.update(development_baseline_artifact_set_sha256="bad"),
        ),
        (
            "baseline_manifest_hash",
            lambda value: value.update(development_baseline_manifest_sha256="bad"),
        ),
        (
            "primary_size",
            lambda value: value.update(
                primary_sample_count=149,
                primary_thread_count=149,
            ),
        ),
        (
            "reserve_size",
            lambda value: value.update(reserve_sample_count=24),
        ),
        (
            "artifact_set",
            lambda value: value["artifact_sha256"].pop("sealed-reserve/bindings.jsonl"),
        ),
    ],
)
def test_release_eligibility_requires_complete_authenticated_authority(
    tmp_path: Path,
    case: str,
    mutation: Any,
) -> None:
    fixture = _fixture(tmp_path / case)
    _rewrite_root_manifest(fixture, mutation)

    with pytest.raises(
        finalizer.GmailTemporalLabelFinalizerError,
        match="release holdout authority",
    ):
        finalizer.finalize_gmail_temporal_holdout_labels(
            fixture["root"],
            fixture["completed"],
            fixture["key"],
            fixture["output"],
        )


@pytest.mark.parametrize(
    "case",
    [
        "empty_primary_samples",
        "binding_sample_mismatch",
        "binding_source_hash_mismatch",
        "empty_expected_requests",
        "duplicate_request",
        "request_order_mismatch",
        "bad_request_routable",
        "bad_request_schema",
        "request_manifest_count",
        "reserve_count_mismatch",
    ],
)
def test_release_eligibility_requires_structurally_scorable_authority(
    tmp_path: Path,
    case: str,
) -> None:
    fixture = _fixture(tmp_path / case)
    if case == "empty_primary_samples":
        name = "evaluation-authority/primary-samples.jsonl"
        payload = b""
    elif case in {"binding_sample_mismatch", "binding_source_hash_mismatch"}:
        name = "evaluation-authority/primary-bindings.jsonl"
        path = fixture["root"] / name
        rows = [json.loads(line) for line in path.read_bytes().splitlines()]
        if case == "binding_sample_mismatch":
            rows[0]["sample_id"] = f"gths_{9_999:064x}"
        else:
            rows[0]["source_sha256"] = "f" * 64
        payload = finalizer._jsonl_bytes(rows)
    elif case == "empty_expected_requests":
        name = "evaluation-authority/primary-requests.jsonl"
        payload = b""
    elif case in {
        "duplicate_request",
        "request_order_mismatch",
        "bad_request_routable",
        "bad_request_schema",
    }:
        name = "evaluation-authority/primary-requests.jsonl"
        path = fixture["root"] / name
        rows = [json.loads(line) for line in path.read_bytes().splitlines()]
        if case == "duplicate_request":
            rows[1]["request_fingerprint"] = rows[0]["request_fingerprint"]
            rows[1]["payload"]["request_fingerprint"] = rows[0]["request_fingerprint"]
        elif case == "request_order_mismatch":
            rows[0], rows[1] = rows[1], rows[0]
        elif case == "bad_request_routable":
            rows[0]["routable"] = True
        else:
            rows[0].pop("payload")
        payload = finalizer._jsonl_bytes(rows)
    elif case == "request_manifest_count":
        _rewrite_root_manifest(
            fixture,
            lambda value: value.update(primary_request_count=2),
        )
        name = "evaluation-authority/primary-requests.jsonl"
        payload = (fixture["root"] / name).read_bytes()
    else:
        name = "sealed-reserve/bindings.jsonl"
        path = fixture["root"] / name
        rows = [json.loads(line) for line in path.read_bytes().splitlines()][:-1]
        payload = finalizer._jsonl_bytes(rows)
    _rewrite_authenticated_artifact(fixture, name, payload)

    with pytest.raises(
        finalizer.GmailTemporalLabelFinalizerError,
        match="(authority|requests)",
    ):
        finalizer.finalize_gmail_temporal_holdout_labels(
            fixture["root"],
            fixture["completed"],
            fixture["key"],
            fixture["output"],
        )


def test_diagnostic_cohort_may_be_smaller_without_claiming_release_eligibility(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path / "diagnostic", release_eligible=False)

    result = finalizer.finalize_gmail_temporal_holdout_labels(
        fixture["root"],
        fixture["completed"],
        fixture["key"],
        fixture["output"],
    )

    assert result["records"] == 90
    assert result["release_holdout_eligible"] is False
    assert result["primary_label_data_gate_passed"] is True
    assert result["challenge_label_data_gate_passed"] is False
    assert result["label_gate_passed"] is False


def test_rejects_stale_target_as_of_labeling_policy(tmp_path: Path) -> None:
    root_fixture = _fixture(tmp_path / "root-label-policy")
    _rewrite_root_manifest(
        root_fixture,
        lambda value: value.update(label_time_basis="latest_thread_state"),
    )
    with pytest.raises(
        finalizer.GmailTemporalLabelFinalizerError,
        match="manifest policy",
    ):
        finalizer.finalize_gmail_temporal_holdout_labels(
            root_fixture["root"],
            root_fixture["completed"],
            root_fixture["key"],
            root_fixture["output"],
        )

    label_fixture = _fixture(tmp_path / "queue-label-policy")
    name = "label-queue/manifest.json"
    label_manifest = json.loads((label_fixture["root"] / name).read_bytes())
    label_manifest["later_context_policy"] = "rewrite_target_from_later_reply"
    _rewrite_authenticated_artifact(
        label_fixture,
        name,
        finalizer._canonical_json(label_manifest) + b"\n",
    )
    with pytest.raises(
        finalizer.GmailTemporalLabelFinalizerError,
        match="label manifest schema",
    ):
        finalizer.finalize_gmail_temporal_holdout_labels(
            label_fixture["root"],
            label_fixture["completed"],
            label_fixture["key"],
            label_fixture["output"],
        )


def test_rejects_nonprivate_or_symlinked_inputs(tmp_path: Path) -> None:
    mode_fixture = _fixture(tmp_path / "mode")
    os.chmod(mode_fixture["completed"], 0o644)
    with pytest.raises(
        finalizer.GmailTemporalLabelFinalizerError,
        match="owner-only",
    ):
        finalizer.finalize_gmail_temporal_holdout_labels(
            mode_fixture["root"],
            mode_fixture["completed"],
            mode_fixture["key"],
            mode_fixture["output"],
        )

    symlink_fixture = _fixture(tmp_path / "symlink")
    real_completed = symlink_fixture["completed"].with_name("real-completed.jsonl")
    symlink_fixture["completed"].replace(real_completed)
    symlink_fixture["completed"].symlink_to(real_completed)
    with pytest.raises(
        finalizer.GmailTemporalLabelFinalizerError,
        match="owner-only",
    ):
        finalizer.finalize_gmail_temporal_holdout_labels(
            symlink_fixture["root"],
            symlink_fixture["completed"],
            symlink_fixture["key"],
            symlink_fixture["output"],
        )


def test_cli_failure_is_static_and_does_not_print_private_arguments(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_marker = "PRIVATE-SOURCE-MARKER"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT_PATH),
            "--holdout-root",
            f"/missing/{private_marker}",
            "--completed-labels",
            "/missing/completed",
            "--hmac-key",
            "/missing/key",
            "--output-root",
            "/missing/output",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        finalizer.main()

    assert exc.value.code == 2
    output = capsys.readouterr().out
    assert private_marker not in output
    assert json.loads(output) == {
        "version": finalizer.VERSION,
        "status": "failed",
        "error": "gmail_temporal_holdout_label_finalization_failed",
        "private_content_printed": False,
    }
