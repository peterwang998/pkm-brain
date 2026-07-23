from __future__ import annotations

import copy
import hashlib
import hmac
import importlib.util
import json
import os
import stat
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pkm_brain.gmail_temporal_batching import plan_gmail_temporal_selector_batches
from pkm_brain.gmail_temporal_frontier import (
    build_gmail_temporal_candidate_frontier,
    plan_gmail_temporal_candidate_pages,
)
from pkm_brain.gmail_temporal_leads import analyze_gmail_temporal_leads
from pkm_brain.gmail_temporal_runner import _request_for_page


ROOT = Path(__file__).parents[1]
SCRIPT_PATH = ROOT / "scripts" / "prepare_gmail_temporal_holdout_candidate_gold.py"
SCORER_PATH = ROOT / "scripts" / "score_gmail_temporal_holdout.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location(
        "test_prepare_gmail_temporal_holdout_candidate_gold",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


adapter = _load()
finalizer = adapter.finalizer


def _load_scorer() -> Any:
    spec = importlib.util.spec_from_file_location(
        "test_score_gmail_temporal_holdout",
        SCORER_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


scorer = _load_scorer()
external_runner = scorer.external_runner


def _write_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    os.chmod(path, 0o600)


def _without_chunk(value: Any) -> dict[str, Any]:
    output = asdict(value)
    output.pop("chunk_id", None)
    return output


def _locator(
    *,
    text: str,
    expression: Any,
    subject: Any,
    lifecycle: Any | None,
    candidate: Any,
    normalized_value: str | None = None,
) -> dict[str, Any]:
    return {
        "expression": {
            "surface": text[expression.start : expression.end],
            "form": expression.form,
            "field": expression.field,
        },
        "subject": {
            "surface": text[subject.start : subject.end],
            "mention_type": subject.mention_type,
            "field": subject.field,
        },
        "lifecycle_mention": (
            None
            if lifecycle is None
            else {
                "surface": text[lifecycle.start : lifecycle.end],
                "lifecycle_role": lifecycle.lifecycle_role,
                "field": lifecycle.field,
            }
        ),
        "derived": {
            "relation": candidate.relation,
            "kind": candidate.kind,
            "lifecycle": candidate.lifecycle,
            "normalized_value": (
                candidate.normalized_value
                if normalized_value is None
                else normalized_value
            ),
            "requires_defer": candidate.requires_defer,
        },
    }


def _authenticated_builder_manifest(value: dict[str, Any], key: bytes) -> bytes:
    authenticator = hmac.new(
        key,
        finalizer.BUILDER_MANIFEST_DOMAIN + finalizer._canonical_json(value),
        hashlib.sha256,
    ).hexdigest()
    return (
        finalizer._canonical_json({**value, "manifest_hmac_sha256": authenticator})
        + b"\n"
    )


def _fixture(
    base: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    include_challenge: bool = False,
    source_text: str = "Planning meeting is scheduled for August 14, 2027.",
) -> dict[str, Any]:
    monkeypatch.setattr(finalizer, "PRIMARY_MIN_LABELED_HARD_NEGATIVES", 0)
    monkeypatch.setattr(finalizer, "CHALLENGE_MIN_EXPECTED_MATERIAL_RECORDS", 1)
    monkeypatch.setattr(finalizer, "CHALLENGE_MIN_SEMANTIC_MEMBERS", 1)
    monkeypatch.setattr(finalizer, "CHALLENGE_MIN_SUPPORTED_MEMBERS", 1)
    monkeypatch.setattr(finalizer, "CHALLENGE_MIN_LABELED_HARD_NEGATIVES", 0)
    base.mkdir(parents=True)
    os.chmod(base, 0o700)
    key_value = b"candidate-gold-adapter-test-key!"
    assert len(key_value) >= 32
    key_path = base / "holdout.key"
    _write_private(key_path, key_value)

    sample_id = "gths_" + "1" * 64
    thread_id = "gtht_" + "2" * 64
    timestamp = "2027-08-01T09:00:00-07:00"
    text = source_text
    analysis = analyze_gmail_temporal_leads(
        text=text,
        message_internal_at=timestamp,
        fact_admitted=True,
        temporal_review_rescue=False,
        chunk_id=sample_id,
    )
    plan = plan_gmail_temporal_selector_batches(text=text, analysis=analysis)
    request_rows: list[dict[str, Any]] = []
    candidates: list[Any] = []
    for batch in plan.batches:
        frontier = build_gmail_temporal_candidate_frontier(
            analysis=analysis,
            batch=batch,
        )
        page_plan = plan_gmail_temporal_candidate_pages(
            analysis=analysis,
            batch=batch,
        )
        candidates.extend(frontier.candidates)
        for page in page_plan.pages:
            request = _request_for_page(
                batch=batch,
                frontier=frontier,
                page_plan=page_plan,
                page=page,
            )
            request_rows.append(
                {
                    "version": "gmail_temporal_holdout_request_v1",
                    "sample_id": sample_id,
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
    assert candidates and request_rows
    target = candidates[0]
    expressions = {item.expression_id: item for item in analysis.expressions}
    mentions = {item.mention_id: item for item in analysis.mentions}
    expression = expressions[target.expression_id]
    subject = mentions[target.subject_mention_id]
    lifecycle = (
        None
        if target.lifecycle_mention_id is None
        else mentions[target.lifecycle_mention_id]
    )
    sample = {
        "version": "gmail_temporal_holdout_sample_v2",
        "sample_id": sample_id,
        "thread_id": thread_id,
        "selection_partition": "natural_historical_diagnostic",
        "stratum": "important_fact",
        "message_internal_at": timestamp,
        "source_prior_message_count": 0,
        "source_later_message_count": 0,
        "source_omitted_before_count": 0,
        "thread_truncated_message_count": 0,
        "target_body_truncation_status": "not_indicated",
        "text": text,
        "sanitized_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "analysis_fingerprint": analysis.snapshot_fingerprint,
        "batch_plan_fingerprint": plan.plan_fingerprint,
        "preparation": {
            "admission_basis": "fact",
            "disposition": "complete_review_projection",
            "error_bucket": None,
            "expression_count": len(analysis.expressions),
            "mention_count": len(analysis.mentions),
            "candidate_count": len(candidates),
            "page_count": len(request_rows),
            "request_fingerprints": [
                row["request_fingerprint"] for row in request_rows
            ],
        },
        "policy": {},
        "selection_strata": ["candidate_bearing"],
        "expressions": [_without_chunk(item) for item in analysis.expressions],
        "mentions": [_without_chunk(item) for item in analysis.mentions],
        "leads": [_without_chunk(item) for item in analysis.leads],
        "routable": False,
    }
    binding = {
        "version": "gmail_temporal_holdout_binding_v1",
        "sample_id": sample_id,
        "document_id": "document-1",
        "gmail_message_id": "message-1",
        "gmail_account_key": "account-1",
        "gmail_thread_id": "thread-1",
        "gmail_source_revision": "revision-1",
        "source_sha256": sample["source_sha256"],
        "candidate_fingerprint": "candidate-fingerprint",
        "analysis_fingerprint": analysis.snapshot_fingerprint,
        "batch_plan_fingerprint": plan.plan_fingerprint,
        "target_fingerprint": "gtrt_" + "3" * 64,
        "routable": False,
    }
    source_row = {
        "version": finalizer.LABEL_QUEUE_VERSION,
        "sample_id": sample_id,
        "thread_id": thread_id,
        "target": {
            "message_internal_at": timestamp,
            "text": text,
            "source_char_count": len(text),
            "emitted_char_count": len(text),
            "sanitized_text_sha256": sample["sanitized_text_sha256"],
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
    completed = copy.deepcopy(source_row)
    exact_locator = _locator(
        text=text,
        expression=expression,
        subject=subject,
        lifecycle=lifecycle,
        candidate=target,
    )
    absent_locator = copy.deepcopy(exact_locator)
    absent_locator["derived"]["normalized_value"] = "2099-01-01"
    completed.update(
        {
            "label_status": "labeled",
            "expected_material": True,
            "expected_filter": "should_admit",
            "hard_negative": False,
            "semantic_units": [
                {
                    "unit_id": "meeting_occurrence",
                    "truth": "The planning meeting is scheduled for 2027-08-14.",
                    "baseline_frontier_grade": finalizer.BASELINE_GRADE_PLACEHOLDER,
                    "members": [
                        {
                            "member_id": "occurrence",
                            "expected_verdict": "supported",
                            "baseline_frontier_grade": (
                                finalizer.BASELINE_GRADE_PLACEHOLDER
                            ),
                            "alternatives": [
                                {
                                    "quality": "exact",
                                    "expected_verdict": "supported",
                                    "locator": exact_locator,
                                }
                            ],
                        }
                    ],
                },
                {
                    "unit_id": "missing_variant",
                    "truth": "A deliberately unavailable variant is required.",
                    "baseline_frontier_grade": finalizer.BASELINE_GRADE_PLACEHOLDER,
                    "members": [
                        {
                            "member_id": "unavailable",
                            "expected_verdict": "supported",
                            "baseline_frontier_grade": (
                                finalizer.BASELINE_GRADE_PLACEHOLDER
                            ),
                            "alternatives": [
                                {
                                    "quality": "exact",
                                    "expected_verdict": "supported",
                                    "locator": absent_locator,
                                }
                            ],
                        }
                    ],
                },
            ],
            "critical_error": "none",
            "notes": None,
        }
    )

    challenge_samples: list[dict[str, Any]] = []
    challenge_bindings: list[dict[str, Any]] = []
    challenge_requests: list[dict[str, Any]] = []
    challenge_source_rows: list[dict[str, Any]] = []
    challenge_completed_rows: list[dict[str, Any]] = []
    challenge_candidates: list[Any] = []
    if include_challenge:
        challenge_sample_id = "gths_" + "4" * 64
        challenge_thread_id = "gtht_" + "5" * 64
        challenge_timestamp = "2027-08-02T09:00:00-07:00"
        challenge_text = (
            "The planning meeting on September 21, 2027 at 3:00 PM UTC was "
            "rescheduled. The obsolete meeting on September 19, 2027 at 1:00 PM "
            "UTC was cancelled. The workshop on September 22, 2027 at 4:00 PM UTC "
            "was completed."
        )
        challenge_analysis = analyze_gmail_temporal_leads(
            text=challenge_text,
            message_internal_at=challenge_timestamp,
            fact_admitted=True,
            temporal_review_rescue=False,
            chunk_id=challenge_sample_id,
        )
        challenge_plan = plan_gmail_temporal_selector_batches(
            text=challenge_text,
            analysis=challenge_analysis,
        )
        for batch in challenge_plan.batches:
            frontier = build_gmail_temporal_candidate_frontier(
                analysis=challenge_analysis,
                batch=batch,
            )
            page_plan = plan_gmail_temporal_candidate_pages(
                analysis=challenge_analysis,
                batch=batch,
            )
            challenge_candidates.extend(frontier.candidates)
            for page in page_plan.pages:
                request = _request_for_page(
                    batch=batch,
                    frontier=frontier,
                    page_plan=page_plan,
                    page=page,
                )
                challenge_requests.append(
                    {
                        "version": "gmail_temporal_holdout_request_v1",
                        "sample_id": challenge_sample_id,
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
        assert challenge_candidates and challenge_requests
        challenge_expressions = {
            item.expression_id: item for item in challenge_analysis.expressions
        }
        challenge_mentions = {
            item.mention_id: item for item in challenge_analysis.mentions
        }

        def challenge_target(lifecycle_role: str) -> Any:
            return next(
                candidate
                for candidate in challenge_candidates
                if candidate.lifecycle_mention_id is not None
                and challenge_mentions[candidate.lifecycle_mention_id].lifecycle_role
                == lifecycle_role
            )

        challenge_targets = {
            lifecycle_role: challenge_target(lifecycle_role)
            for lifecycle_role in ("rescheduled", "cancelled", "completed")
        }

        def challenge_semantic_unit(
            lifecycle_role: str,
            expected_verdict: str,
        ) -> dict[str, Any]:
            target = challenge_targets[lifecycle_role]
            expression = challenge_expressions[target.expression_id]
            subject = challenge_mentions[target.subject_mention_id]
            lifecycle = challenge_mentions[target.lifecycle_mention_id]
            return {
                "unit_id": f"challenge_{lifecycle_role}",
                "truth": f"The source asserts a {lifecycle_role} lifecycle state.",
                "baseline_frontier_grade": finalizer.BASELINE_GRADE_PLACEHOLDER,
                "members": [
                    {
                        "member_id": lifecycle_role,
                        "expected_verdict": expected_verdict,
                        "baseline_frontier_grade": (
                            finalizer.BASELINE_GRADE_PLACEHOLDER
                        ),
                        "alternatives": [
                            {
                                "quality": "exact",
                                "expected_verdict": expected_verdict,
                                "locator": _locator(
                                    text=challenge_text,
                                    expression=expression,
                                    subject=subject,
                                    lifecycle=lifecycle,
                                    candidate=target,
                                ),
                            }
                        ],
                    }
                ],
            }

        challenge_sample = {
            "version": "gmail_temporal_holdout_sample_v2",
            "sample_id": challenge_sample_id,
            "thread_id": challenge_thread_id,
            "selection_partition": "challenge_diagnostic",
            "stratum": "important_fact",
            "message_internal_at": challenge_timestamp,
            "source_prior_message_count": 0,
            "source_later_message_count": 0,
            "source_omitted_before_count": 0,
            "thread_truncated_message_count": 0,
            "target_body_truncation_status": "not_indicated",
            "text": challenge_text,
            "sanitized_text_sha256": hashlib.sha256(
                challenge_text.encode()
            ).hexdigest(),
            "source_sha256": hashlib.sha256(challenge_text.encode()).hexdigest(),
            "analysis_fingerprint": challenge_analysis.snapshot_fingerprint,
            "batch_plan_fingerprint": challenge_plan.plan_fingerprint,
            "preparation": {
                "admission_basis": "fact",
                "disposition": "complete_review_projection",
                "error_bucket": None,
                "expression_count": len(challenge_analysis.expressions),
                "mention_count": len(challenge_analysis.mentions),
                "candidate_count": len(challenge_candidates),
                "page_count": len(challenge_requests),
                "request_fingerprints": [
                    row["request_fingerprint"] for row in challenge_requests
                ],
            },
            "policy": {},
            "selection_strata": ["candidate_bearing"],
            "expressions": [
                _without_chunk(item) for item in challenge_analysis.expressions
            ],
            "mentions": [_without_chunk(item) for item in challenge_analysis.mentions],
            "leads": [_without_chunk(item) for item in challenge_analysis.leads],
            "routable": False,
        }
        challenge_binding = {
            **binding,
            "sample_id": challenge_sample_id,
            "document_id": "document-challenge",
            "gmail_message_id": "message-challenge",
            "gmail_thread_id": "thread-challenge",
            "source_sha256": challenge_sample["source_sha256"],
            "analysis_fingerprint": challenge_analysis.snapshot_fingerprint,
            "batch_plan_fingerprint": challenge_plan.plan_fingerprint,
            "target_fingerprint": "gtrt_" + "6" * 64,
        }
        challenge_source = copy.deepcopy(source_row)
        challenge_source.update(
            {
                "sample_id": challenge_sample_id,
                "thread_id": challenge_thread_id,
                "target": {
                    "message_internal_at": challenge_timestamp,
                    "text": challenge_text,
                    "source_char_count": len(challenge_text),
                    "emitted_char_count": len(challenge_text),
                    "sanitized_text_sha256": challenge_sample["sanitized_text_sha256"],
                    "body_truncation_status": "not_indicated",
                },
            }
        )
        challenge_completed = copy.deepcopy(challenge_source)
        challenge_completed.update(
            {
                "label_status": "labeled",
                "expected_material": True,
                "expected_filter": "should_admit",
                "hard_negative": False,
                "semantic_units": [
                    challenge_semantic_unit("rescheduled", "uncertain"),
                    challenge_semantic_unit("cancelled", "supported"),
                    challenge_semantic_unit("completed", "supported"),
                ],
                "critical_error": "none",
                "notes": None,
            }
        )
        challenge_samples.append(challenge_sample)
        challenge_bindings.append(challenge_binding)
        challenge_source_rows.append(challenge_source)
        challenge_completed_rows.append(challenge_completed)

    root = base / "holdout"
    root.mkdir()
    os.chmod(root, 0o700)
    primary_queue_raw = finalizer._jsonl_bytes([source_row])
    challenge_queue_raw = finalizer._jsonl_bytes(challenge_source_rows)
    label_manifest = {
        "version": finalizer.LABEL_MANIFEST_VERSION,
        "primary_count": 1,
        "challenge_count": len(challenge_source_rows),
        "primary_sha256": hashlib.sha256(primary_queue_raw).hexdigest(),
        "challenge_sha256": hashlib.sha256(challenge_queue_raw).hexdigest(),
        "diagnostic_denominator": "primary_only",
        "pipeline_predictions_present": False,
        "admission_decisions_present": False,
        "selection_strata_present": False,
        "label_time_basis": finalizer.LABEL_TIME_BASIS,
        "later_context_policy": finalizer.LATER_CONTEXT_POLICY,
        "release_holdout_eligible": False,
    }
    artifacts = {
        "label-queue/primary.jsonl": primary_queue_raw,
        "label-queue/challenge.jsonl": challenge_queue_raw,
        "label-queue/manifest.json": finalizer._canonical_json(label_manifest) + b"\n",
        "evaluation-authority/primary-samples.jsonl": finalizer._jsonl_bytes([sample]),
        "evaluation-authority/challenge-samples.jsonl": finalizer._jsonl_bytes(
            challenge_samples
        ),
        "evaluation-authority/primary-bindings.jsonl": finalizer._jsonl_bytes(
            [binding]
        ),
        "evaluation-authority/challenge-bindings.jsonl": finalizer._jsonl_bytes(
            challenge_bindings
        ),
        "evaluation-authority/primary-requests.jsonl": finalizer._jsonl_bytes(
            request_rows
        ),
        "evaluation-authority/challenge-requests.jsonl": finalizer._jsonl_bytes(
            challenge_requests
        ),
        "sealed-reserve/order.jsonl": b"",
        "sealed-reserve/bindings.jsonl": b"",
    }
    for name, raw in artifacts.items():
        _write_private(root / name, raw)
    for directory in (
        root / "label-queue",
        root / "evaluation-authority",
        root / "sealed-reserve",
    ):
        os.chmod(directory, 0o700)
    root_manifest = {
        "version": finalizer.BUILDER_MANIFEST_VERSION,
        "builder_version": "gmail_temporal_holdout_builder_v5",
        "builder_sha256": hashlib.sha256(
            (ROOT / "scripts/build_gmail_temporal_holdout.py").read_bytes()
        ).hexdigest(),
        "primary_sample_count": 1,
        "primary_thread_count": 1,
        "challenge_sample_count": len(challenge_samples),
        "challenge_thread_count": len(challenge_samples),
        "reserve_sample_count": 0,
        "primary_request_count": len(request_rows),
        "primary_candidate_count": len(candidates),
        "primary_page_count": len(request_rows),
        "challenge_request_count": len(challenge_requests),
        "challenge_candidate_count": len(challenge_candidates),
        "challenge_page_count": len(challenge_requests),
        "artifact_sha256": {
            name: hashlib.sha256(raw).hexdigest()
            for name, raw in sorted(artifacts.items())
        },
        "label_status": "unlabeled",
        "diagnostic_denominator": "primary_only",
        "labeler_artifact": "label-queue/primary.jsonl",
        "labeler_must_not_inspect_internal_artifacts": True,
        "thread_policy": "at_most_one_message_per_thread",
        "label_time_basis": finalizer.LABEL_TIME_BASIS,
        "later_context_policy": finalizer.LATER_CONTEXT_POLICY,
        "release_evidence_class": finalizer.DIAGNOSTIC_EVIDENCE_CLASS,
        "release_evidence_class_applies_to": "primary_natural_cohort",
        "prior_development_overlap_proven_zero_applies_to": "primary_and_reserve",
        "primary_evidence_scope": "diagnostic_natural_operability",
        "primary_prospective_unseen_source_evidence": False,
        "primary_historical_architecture_exposed": False,
        "challenge_evidence_scope": "diagnostic_balanced_capability_stress",
        "challenge_prospective_unseen_source_evidence": False,
        "challenge_historical_architecture_exposed": False,
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
        "freeze_authority_evidence_class": finalizer.DIAGNOSTIC_EVIDENCE_CLASS,
        "freeze_authority_status": finalizer.CANONICAL_FREEZE_AUTHORITY_STATUS,
        "freeze_no_reroll_scope": finalizer.FREEZE_NO_REROLL_SCOPE,
        "freeze_authority_independently_reverified_downstream": False,
        "legacy_signed_freeze_claims_downgraded": False,
        "freeze_irrevocable_from_first_materialization": True,
        "labeled_cohort_reroll_forbidden": True,
        "all_labeled_attempts_must_be_retained": True,
        "source_labels_must_be_sealed_before_verifier_outputs_opened": True,
        "primary_population_scope": "diagnostic_unrestricted",
        "representative_gmail_production_eligible": False,
        "prospective_existing_thread_update_gate_required": False,
        "prospective_natural_recall_continuation_required": False,
        "prospective_natural_material_minimum": 20,
        "prospective_natural_effective_recall_minimum": 0.90,
        "prospective_natural_recall_continuation_passed": False,
        "underpowered_primary_action": (
            "publish_failure_then_activate_sealed_reserve_in_authenticated_order_for_regression_diagnostic_only_then_fresh_150_100_75_required_for_release"
        ),
        "underpowered_challenge_action": (
            "publish_underpowered_result_then_versioned_redesign_no_reroll"
        ),
        "development_baseline_primary_overlap_count": 0,
        "development_baseline_challenge_overlap_count": 0,
        "release_scope": "diagnostic_only",
        "prospective_unseen_source_evidence": False,
        "historical_architecture_exposed": False,
        "retrospective_calibration_eligible": False,
        "semantic_development_overlap_status": "not_release_evidence",
        "automatic_apply_eligible": False,
        "content_changing_canary_required": False,
        "release_holdout_eligible": False,
        "external_calls": 0,
        "persistence_calls": 0,
        "private_content_printed": False,
        "routable": False,
    }
    _write_private(
        root / "manifest.json",
        _authenticated_builder_manifest(root_manifest, key_value),
    )
    completed_path = base / "completed.jsonl"
    _write_private(completed_path, finalizer._jsonl_bytes([completed]))
    challenge_completed_path = None
    if include_challenge:
        challenge_completed_path = base / "completed-challenge.jsonl"
        _write_private(
            challenge_completed_path,
            finalizer._jsonl_bytes(challenge_completed_rows),
        )
    gold_root = base / "gold"
    finalizer.finalize_gmail_temporal_holdout_labels(
        root,
        completed_path,
        key_path,
        gold_root,
        completed_challenge_labels_path=challenge_completed_path,
    )
    return {
        "root": root,
        "gold": gold_root,
        "key": key_path,
        "key_value": key_value,
        "output": base / "adapted",
        "challenge_output": base / "adapted-challenge",
        "private_marker": "Planning meeting",
    }


def test_prepares_authenticated_candidate_gold_with_authoritative_grades(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path / "valid", monkeypatch)

    result = adapter.prepare_gmail_temporal_holdout_candidate_gold(
        fixture["root"],
        fixture["gold"],
        fixture["key"],
        fixture["output"],
    )

    assert result["records"] == 1
    assert result["semantic_units"] == 2
    assert result["candidate_gold_sample_compatible"] is True
    assert result["release_holdout_eligible"] is False
    assert fixture["private_marker"] not in json.dumps(result)
    assert stat.S_IMODE(fixture["output"].stat().st_mode) == 0o700
    assert (
        stat.S_IMODE(
            (fixture["output"] / adapter.OUTPUT_SAMPLE_ARTIFACT).stat().st_mode
        )
        == 0o600
    )
    rows = adapter._load_jsonl(
        (fixture["output"] / adapter.OUTPUT_SAMPLE_ARTIFACT).read_bytes(),
        description="adapted samples",
    )
    units = rows[0]["gold"]["semantic_units"]
    assert rows[0]["gold"]["hard_negative"] is False
    assert [unit["baseline_frontier_grade"] for unit in units] == [
        "exact",
        "absent",
    ]
    assert [unit["members"][0]["baseline_frontier_grade"] for unit in units] == [
        "exact",
        "absent",
    ]
    manifest_raw = (fixture["output"] / "manifest.json").read_bytes()
    manifest = json.loads(manifest_raw)
    authenticator = manifest.pop("manifest_hmac_sha256")
    expected = hmac.new(
        fixture["key_value"],
        adapter.MANIFEST_DOMAIN + adapter._canonical_json(manifest),
        hashlib.sha256,
    ).hexdigest()
    assert hmac.compare_digest(authenticator, expected)
    assert manifest["baseline_frontier_grade_human_controlled"] is False
    assert manifest["baseline_unit_grade_counts"] == {
        "absent": 1,
        "exact": 1,
        "partial": 0,
    }
    assert (
        manifest["freeze_authority_status"]
        == finalizer.CANONICAL_FREEZE_AUTHORITY_STATUS
    )
    assert manifest["freeze_attempt_id"] == "gthfa_" + "e" * 64
    assert manifest["freeze_irrevocable_from_first_materialization"] is True
    assert manifest["freeze_no_reroll_scope"] == finalizer.FREEZE_NO_REROLL_SCOPE
    assert manifest["freeze_authority_independently_reverified_downstream"] is False
    assert manifest["label_chronology_verified"] is False
    assert manifest["label_logical_run_id"] is None


def test_prepares_challenge_candidate_gold_as_diagnostic_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(
        tmp_path / "challenge-valid",
        monkeypatch,
        include_challenge=True,
    )

    result = adapter.prepare_gmail_temporal_holdout_candidate_gold(
        fixture["root"],
        fixture["gold"],
        fixture["key"],
        fixture["challenge_output"],
        cohort="challenge",
    )

    assert result["cohort"] == "challenge"
    assert result["diagnostic_only"] is True
    assert result["records"] == 1
    assert result["release_holdout_eligible"] is False
    manifest = json.loads((fixture["challenge_output"] / "manifest.json").read_bytes())
    assert manifest["cohort"] == "challenge"
    assert manifest["diagnostic_denominator"] == "challenge_only"
    assert manifest["challenge_contributes_to_primary_release_gates"] is False
    assert manifest["release_holdout_eligible"] is False


def test_challenge_adapter_rejects_resigned_primary_gold_as_challenge_gold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(
        tmp_path / "challenge-gold-mix",
        monkeypatch,
        include_challenge=True,
    )
    primary_raw = (fixture["gold"] / "gold.jsonl").read_bytes()
    challenge_path = fixture["gold"] / "challenge-diagnostic-gold.jsonl"
    _write_private(challenge_path, primary_raw)
    manifest_path = fixture["gold"] / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest.pop("manifest_hmac_sha256")
    digest = hashlib.sha256(primary_raw).hexdigest()
    manifest["artifact_sha256"]["challenge-diagnostic-gold.jsonl"] = digest
    manifest["challenge_gold_sha256"] = digest
    authenticator = hmac.new(
        fixture["key_value"],
        finalizer.GOLD_MANIFEST_DOMAIN + finalizer._canonical_json(manifest),
        hashlib.sha256,
    ).hexdigest()
    _write_private(
        manifest_path,
        finalizer._canonical_json({**manifest, "manifest_hmac_sha256": authenticator})
        + b"\n",
    )

    with pytest.raises(
        adapter.GmailTemporalCandidateGoldAdapterError,
        match="gold authority is invalid",
    ):
        adapter.prepare_gmail_temporal_holdout_candidate_gold(
            fixture["root"],
            fixture["gold"],
            fixture["key"],
            fixture["challenge_output"],
            cohort="challenge",
        )


def test_rejects_human_controlled_baseline_even_with_resigned_gold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path / "human-grade", monkeypatch)
    gold_path = fixture["gold"] / "gold.jsonl"
    rows = adapter._load_jsonl(gold_path.read_bytes(), description="gold")
    rows[0]["semantic_units"][0]["baseline_frontier_grade"] = "exact"
    gold_raw = adapter._jsonl_bytes(rows)
    _write_private(gold_path, gold_raw)
    manifest_path = fixture["gold"] / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest.pop("manifest_hmac_sha256")
    manifest["artifact_sha256"] = {"gold.jsonl": hashlib.sha256(gold_raw).hexdigest()}
    authenticator = hmac.new(
        fixture["key_value"],
        finalizer.GOLD_MANIFEST_DOMAIN + finalizer._canonical_json(manifest),
        hashlib.sha256,
    ).hexdigest()
    _write_private(
        manifest_path,
        finalizer._canonical_json({**manifest, "manifest_hmac_sha256": authenticator})
        + b"\n",
    )

    with pytest.raises(
        adapter.GmailTemporalCandidateGoldAdapterError,
        match="gold authority is invalid",
    ):
        adapter.prepare_gmail_temporal_holdout_candidate_gold(
            fixture["root"],
            fixture["gold"],
            fixture["key"],
            fixture["output"],
        )


def test_rejects_tampered_holdout_request_commitment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path / "request", monkeypatch)
    request_path = fixture["root"] / "evaluation-authority" / "primary-requests.jsonl"
    _write_private(request_path, request_path.read_bytes() + b"tampered\n")

    with pytest.raises(
        adapter.GmailTemporalCandidateGoldAdapterError,
        match="holdout authority is invalid",
    ):
        adapter.prepare_gmail_temporal_holdout_candidate_gold(
            fixture["root"],
            fixture["gold"],
            fixture["key"],
            fixture["output"],
        )


def test_rejects_nonprivate_gold_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path / "mode", monkeypatch)
    os.chmod(fixture["gold"] / "gold.jsonl", 0o644)

    with pytest.raises(
        adapter.GmailTemporalCandidateGoldAdapterError,
        match="unavailable or unsafe",
    ):
        adapter.prepare_gmail_temporal_holdout_candidate_gold(
            fixture["root"],
            fixture["gold"],
            fixture["key"],
            fixture["output"],
        )


def test_cli_failure_is_static_and_private_content_free(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_marker = "PRIVATE-GMAIL-SOURCE-MARKER"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT_PATH),
            "--holdout-root",
            f"/missing/{private_marker}",
            "--gold-root",
            "/missing/gold",
            "--hmac-key",
            "/missing/key",
            "--output-root",
            "/missing/output",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        adapter.main()

    assert exc.value.code == 2
    output = capsys.readouterr().out
    assert private_marker not in output
    assert json.loads(output) == {
        "version": adapter.VERSION,
        "status": "failed",
        "error": "gmail_temporal_holdout_candidate_gold_preparation_failed",
        "private_content_printed": False,
    }


def _checkpoint_rows(
    pages: dict[str, tuple[Any, Any]],
    verdicts: dict[str, str],
) -> list[dict[str, Any]]:
    candidate_gold = adapter.candidate_evaluator
    source_hashes = candidate_gold._current_repo_module_hashes()
    source_hashes.update({"runner": "a" * 64, "base_runner": "b" * 64})
    protocol = "gtfproto_" + "c" * 64
    rows: list[dict[str, Any]] = []
    for page_fingerprint, (runtime_batch, page) in pages.items():
        candidate_ids = [
            candidate_id
            for cluster in page.clusters
            for candidate_id in cluster.candidate_ids
        ]
        rows.append(
            {
                "version": candidate_gold.EXPECTED_CHECKPOINT_VERSION,
                "sample_id": runtime_batch.sample_id,
                "source_sha256": runtime_batch.analysis.source_sha256,
                "protocol_fingerprint": protocol,
                "source_module_sha256": dict(sorted(source_hashes.items())),
                "plan_fingerprint": runtime_batch.plan_fingerprint,
                "page_case_id": candidate_gold._page_case_id(runtime_batch, page),
                "batch_fingerprint": runtime_batch.batch.manifest.batch_fingerprint,
                "analysis_fingerprint": runtime_batch.analysis.snapshot_fingerprint,
                "frontier_fingerprint": page.frontier_fingerprint,
                "page_fingerprint": page_fingerprint,
                "candidate_page_plan_fingerprint": (
                    runtime_batch.candidate_page_plan_fingerprint
                ),
                "candidate_page_payload_bytes": dict(
                    runtime_batch.candidate_page_payload_bytes
                )[page_fingerprint],
                "batch_sequence": runtime_batch.batch.sequence,
                "page_sequence": page.sequence,
                "page_count": len(runtime_batch.pages),
                "verdicts": [
                    {
                        "candidate_id": candidate_id,
                        "verdict": verdicts[candidate_id],
                    }
                    for candidate_id in candidate_ids
                ],
            }
        )
    return rows


def _scoring_fixture(
    base: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cohort: str = "primary",
    source_text: str = "Planning meeting is scheduled for August 14, 2027.",
) -> dict[str, Any]:
    fixture = _fixture(
        base,
        monkeypatch,
        include_challenge=cohort == "challenge",
        source_text=source_text,
    )
    return _add_scoring_evidence(fixture, base=base, cohort=cohort)


def _add_scoring_evidence(
    fixture: dict[str, Any],
    *,
    base: Path,
    cohort: str,
) -> dict[str, Any]:
    fixture = dict(fixture)
    selected_output = (
        fixture["output"] if cohort == "primary" else fixture["challenge_output"]
    )
    adapter.prepare_gmail_temporal_holdout_candidate_gold(
        fixture["root"],
        fixture["gold"],
        fixture["key"],
        selected_output,
        cohort=cohort,
    )
    fixture["output"] = selected_output
    adapter_manifest_path = fixture["output"] / "manifest.json"
    adapter_manifest = json.loads(adapter_manifest_path.read_bytes())
    adapter_manifest.pop("manifest_hmac_sha256")
    adapter_manifest.update(
        {
            "sol_label_authority_attested": True,
            "source_only_label_authority_attested": True,
            "label_authority_version": finalizer.LABEL_AUTHORITY_VERSION,
            "label_authority_model": finalizer.LABEL_AUTHORITY_MODEL,
            "label_authority_reasoning_effort": (
                finalizer.LABEL_AUTHORITY_REASONING_EFFORT
            ),
                "label_authority_invocation_count": 1,
                "label_authority_manifest_sha256": "a" * 64,
                "label_chronology_verified": True,
                "label_logical_run_id": "gthxr_r_" + "b" * 64,
                "label_plan_sha256": "c" * 64,
                "label_plan_hmac_sha256": "d" * 64,
                "label_started_at": "2026-07-20T10:00:00+00:00",
                "label_completed_at": "2026-07-20T10:05:00+00:00",
                "label_receipt_set_sha256": "e" * 64,
            }
        )
    adapter_authenticator = hmac.new(
        fixture["key_value"],
        adapter.MANIFEST_DOMAIN + adapter._canonical_json(adapter_manifest),
        hashlib.sha256,
    ).hexdigest()
    _write_private(
        adapter_manifest_path,
        adapter._canonical_json(
            {**adapter_manifest, "manifest_hmac_sha256": adapter_authenticator}
        )
        + b"\n",
    )
    candidate_gold = adapter.candidate_evaluator
    sample_path = fixture["output"] / adapter.OUTPUT_SAMPLE_ARTIFACT
    samples = candidate_gold._load_jsonl(sample_path)
    _runtime_batches, candidates, pages = candidate_gold._runtime_batches(samples)
    units = candidate_gold._compile_gold(samples, candidates)
    verdicts = {candidate_id: "unsupported" for candidate_id in candidates}
    for unit in units:
        for member in unit.members:
            matches = candidate_gold._member_matches(member)
            if matches:
                selected = max(matches, key=lambda value: (matches[value], value))
                verdicts[selected] = candidate_gold._member_expected_verdicts(member)[
                    selected
                ]
    fail_next_call = [False]

    def verifier_response(
        request: dict[str, Any],
        _schema: dict[str, Any],
        _model: str,
        _reasoning_effort: str,
        _timeout: int,
    ) -> dict[str, Any]:
        if fail_next_call[0]:
            fail_next_call[0] = False
            raise RuntimeError("bounded synthetic retry")
        response_pages: list[dict[str, Any]] = []
        for payload in request["requests"]:
            candidates = payload["page"]["candidates"]
            response_pages.append(
                {
                    "request_fingerprint": payload["request_fingerprint"],
                    "verdicts": [
                        {
                            "candidate_id": candidate["candidate_id"],
                            "verdict": verdicts[candidate["candidate_id"]],
                        }
                        for candidate in candidates
                    ],
                }
            )
        return {
            "version": external_runner.VERIFIER_RESPONSE_VERSION,
            "pages": response_pages,
        }

    checkpoints: list[Path] = []
    attestations: list[Path] = []
    run_roots: list[Path] = []
    for index in range(1, 4):
        fail_next_call[0] = True
        run_root = base / f"{cohort}-run-{index}"
        with pytest.raises(
            external_runner.GmailTemporalExternalRunnerError,
            match="bounded retry",
        ):
            external_runner.run_verifier(
                fixture["root"],
                fixture["output"],
                fixture["key"],
                run_root,
                cohort=cohort,
                run_ordinal=index,
                batch_size=1,
                invoke=verifier_response,
            )
        external_runner.run_verifier(
            fixture["root"],
            fixture["output"],
            fixture["key"],
            run_root,
            cohort=cohort,
            run_ordinal=index,
            batch_size=1,
            invoke=verifier_response,
        )
        checkpoints.append(run_root / "checkpoint.jsonl")
        attestations.append(run_root / "attestation.json")
        run_roots.append(run_root)
    fixture["checkpoints"] = tuple(checkpoints)
    fixture["attestations"] = tuple(attestations)
    fixture["run_roots"] = tuple(run_roots)
    fixture["score_output"] = base / f"score-{cohort}"
    return fixture


def _rewrite_attestation(
    fixture: dict[str, Any],
    index: int,
    *,
    resign: bool = True,
    **updates: Any,
) -> None:
    path = fixture["attestations"][index]
    value = json.loads(path.read_bytes())
    value.update(updates)
    value.pop("attestation_hmac_sha256", None)
    authenticator = (
        hmac.new(
            fixture["key_value"],
            scorer.ATTESTATION_DOMAIN + scorer._canonical_json(value),
            hashlib.sha256,
        ).hexdigest()
        if resign
        else "0" * 64
    )
    _write_private(
        path,
        scorer._canonical_json({**value, "attestation_hmac_sha256": authenticator})
        + b"\n",
    )


def _rewrite_adapter_manifest(
    fixture: dict[str, Any],
    **updates: Any,
) -> bytes:
    path = fixture["output"] / "manifest.json"
    value = json.loads(path.read_bytes())
    value.pop("manifest_hmac_sha256", None)
    value.update(updates)
    authenticator = hmac.new(
        fixture["key_value"],
        adapter.MANIFEST_DOMAIN + adapter._canonical_json(value),
        hashlib.sha256,
    ).hexdigest()
    raw = (
        adapter._canonical_json({**value, "manifest_hmac_sha256": authenticator})
        + b"\n"
    )
    _write_private(path, raw)
    return raw


def test_scores_three_authenticated_runs_without_synthetic_manifests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _scoring_fixture(tmp_path / "score-valid", monkeypatch)
    attestations = [json.loads(path.read_bytes()) for path in fixture["attestations"]]
    assert all(value["version"] == external_runner.VERIFIER_ATTESTATION_V2 for value in attestations)
    assert all(
        value["external_calls"]
        == value["invocation_count"]
        == len(value["invocation_ids"])
        and value["external_calls"] > 1
        for value in attestations
    )
    assert len({value["logical_run_id"] for value in attestations}) == 3
    all_invocation_ids = [
        invocation_id
        for value in attestations
        for invocation_id in value["invocation_ids"]
    ]
    assert len(all_invocation_ids) == len(set(all_invocation_ids))
    for run_root, value in zip(
        fixture["run_roots"], attestations, strict=True
    ):
        recomputed = external_runner.recompute_call_set_hashes(
            run_root,
            fixture["key"],
        )
        assert all(
            recomputed[field] == value[field]
            for field in (
                "invocation_ids",
                "request_set_sha256",
                "response_set_sha256",
                "receipt_set_sha256",
            )
        )

    result = scorer.score_gmail_temporal_holdout(
        fixture["output"],
        fixture["key"],
        fixture["checkpoints"],
        fixture["attestations"],
        fixture["score_output"],
    )

    assert result["component_runs"] == 3
    assert result["ensemble_pending"] is False
    assert result["challenge_scoring_pending"] is True
    assert result["release_holdout_eligible"] is False
    assert result["release_score_gate_passed"] is False
    assert fixture["private_marker"] not in json.dumps(result)
    score = json.loads((fixture["score_output"] / scorer.SCORE_ARTIFACT).read_bytes())
    assert score["minimum_exact_candidate_agreement"] == 1.0
    assert score["gold_metrics"]["hard_negative_records"] == 0
    assert score["gold_metrics"]["accepted_negative_review_rate"] == 0.0
    assert score["independent_invocations_attested"] is True
    assert score["invocation_provenance_cryptographically_verified"] is False
    assert score["promotion_prerequisites"]["source_holdout_eligible"] is False
    assert score["promotion_pending"] is True
    assert score["cohort_evidence_scope"] == "diagnostic_natural_operability"
    assert score["cohort_metrics_must_not_be_pooled"] is True
    assert score["challenge_population_inference_eligible"] is False
    assert score["development_baseline_cohort_overlap_count"] == 0
    assert set(path.name for path in fixture["score_output"].iterdir()) == {
        "manifest.json",
        scorer.SCORE_ARTIFACT,
        scorer.OWNER_AUDIT_POPULATION_ARTIFACT,
        scorer.OWNER_AUDIT_ERRORS_ARTIFACT,
    }
    population = adapter._load_jsonl(
        (fixture["score_output"] / scorer.OWNER_AUDIT_POPULATION_ARTIFACT).read_bytes(),
        description="owner audit population",
    )
    errors = adapter._load_jsonl(
        (fixture["score_output"] / scorer.OWNER_AUDIT_ERRORS_ARTIFACT).read_bytes(),
        description="owner audit errors",
    )
    assert len(population) == 1
    assert population[0]["cohort"] == "primary"
    assert population[0]["diagnostic_only"] is True
    assert population[0]["source_label_row_sha256"]
    gold_rows = adapter._load_jsonl(
        (fixture["gold"] / "gold.jsonl").read_bytes(),
        description="finalizer gold",
    )
    expected_gold_row_sha256 = hashlib.sha256(
        adapter._canonical_json(gold_rows[0])
    ).hexdigest()
    assert population[0]["gold_label_row_sha256"] == expected_gold_row_sha256
    assert population[0]["completed_label_row_sha256"] == expected_gold_row_sha256
    assert {row["category"] for row in errors} == {"false_negative_member"}
    manifest_raw = (fixture["score_output"] / "manifest.json").read_bytes()
    manifest = json.loads(manifest_raw)
    authenticator = manifest.pop("manifest_hmac_sha256")
    expected = hmac.new(
        fixture["key_value"],
        scorer.MANIFEST_DOMAIN + scorer._canonical_json(manifest),
        hashlib.sha256,
    ).hexdigest()
    assert hmac.compare_digest(authenticator, expected)
    assert (
        manifest["checkpoint_1_sha256"]
        == hashlib.sha256(fixture["checkpoints"][0].read_bytes()).hexdigest()
    )
    interval = score["cohort_metric_intervals_95"]["supported_artifact_precision"]
    assert interval["numerator"] == 1
    assert interval["denominator"] == 1
    assert 0.0 <= interval["wilson_95_lower"] <= interval["wilson_95_upper"] <= 1.0


def test_owner_error_ledger_has_gold_binding_and_bounded_candidate_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _scoring_fixture(
        tmp_path / "score-owner-error",
        monkeypatch,
        source_text=(
            "Planning meeting is scheduled for August 14, 2027. "
            "Budget review is scheduled for August 15, 2027."
        ),
    )
    candidate_gold = adapter.candidate_evaluator
    samples = candidate_gold._load_jsonl(
        fixture["output"] / adapter.OUTPUT_SAMPLE_ARTIFACT
    )
    _runtime, candidates, _pages = candidate_gold._runtime_batches(samples)
    units = candidate_gold._compile_gold(samples, candidates)
    matched = {
        candidate_id
        for unit in units
        for member in unit.members
        for candidate_id in candidate_gold._member_matches(member)
    }
    unmatched = next(
        candidate_id for candidate_id in candidates if candidate_id not in matched
    )
    for index, checkpoint in enumerate(fixture["checkpoints"]):
        rows = adapter._load_jsonl(
            checkpoint.read_bytes(),
            description="checkpoint",
        )
        for row in rows:
            for verdict in row["verdicts"]:
                if verdict["candidate_id"] == unmatched:
                    verdict["verdict"] = "supported"
        _write_private(checkpoint, adapter._jsonl_bytes(rows))
        _rewrite_attestation(
            fixture,
            index,
            checkpoint_sha256=hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        )

    result = scorer.score_gmail_temporal_holdout(
        fixture["output"],
        fixture["key"],
        fixture["checkpoints"],
        fixture["attestations"],
        fixture["score_output"],
    )

    errors = adapter._load_jsonl(
        (fixture["score_output"] / scorer.OWNER_AUDIT_ERRORS_ARTIFACT).read_bytes(),
        description="owner audit errors",
    )
    unmatched_rows = [
        row
        for row in errors
        if row["category"] == "unmatched_artifact" and unmatched in row["candidate_ids"]
    ]
    false_positive_rows = [
        row
        for row in errors
        if row["category"] == "false_positive_artifact"
        and unmatched in row["candidate_ids"]
    ]
    assert len(unmatched_rows) == 1
    assert len(false_positive_rows) == 1
    assert unmatched_rows[0]["critical"] is True
    assert false_positive_rows[0]["critical"] is False
    assert unmatched_rows[0]["candidate_semantics"][0]["candidate_id"] == unmatched
    assert unmatched_rows[0]["candidate_semantics"][0]["normalized_value"]
    assert unmatched_rows[0]["gold_label_row_sha256"]
    assert result["owner_audit_critical_error_records"] == 1


def test_scores_challenge_cohort_without_primary_release_contribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _scoring_fixture(
        tmp_path / "score-challenge",
        monkeypatch,
        cohort="challenge",
    )

    result = scorer.score_gmail_temporal_holdout(
        fixture["output"],
        fixture["key"],
        fixture["checkpoints"],
        fixture["attestations"],
        fixture["score_output"],
    )

    assert result["cohort"] == "challenge"
    assert result["diagnostic_only"] is True
    assert result["challenge_scoring_pending"] is False
    assert result["release_holdout_eligible"] is False
    assert result["release_score_gate_passed"] is False
    score = json.loads((fixture["score_output"] / scorer.SCORE_ARTIFACT).read_bytes())
    assert score["cohort"] == "challenge"
    assert score["diagnostic_only"] is True
    assert score["promotion_prerequisites"]["source_holdout_eligible"] is False
    assert score["challenge_capability_gate_passed"] is True
    assert score["challenge_lifecycle_source_gold_gate_passed"] is True
    lifecycle_metrics = score["challenge_lifecycle_source_gold_metrics"]
    assert lifecycle_metrics["all_required_categories_present"] is True
    assert lifecycle_metrics["all_category_gates_passed"] is True
    assert set(lifecycle_metrics["categories"]) == {
        "reschedule",
        "cancellation",
        "completion",
        "timezone",
    }
    for category in lifecycle_metrics["categories"].values():
        assert (
            category["effective_recall"]["numerator"]
            == category["effective_recall"]["denominator"]
        )
        assert category["effective_recall"]["interval_defined"] is True
        assert category["low_n"] is True
    assert (
        lifecycle_metrics["categories"]["reschedule"]["confirmed_gate_applicable"]
        is False
    )
    assert (
        lifecycle_metrics["categories"]["reschedule"]["confirmed_gate_passed"] is None
    )
    population = adapter._load_jsonl(
        (fixture["score_output"] / scorer.OWNER_AUDIT_POPULATION_ARTIFACT).read_bytes(),
        description="challenge owner audit population",
    )
    assert population[0]["cohort"] == "challenge"
    assert population[0]["diagnostic_only"] is True


def _lifecycle_metric_fixture(
    category_counts: dict[str, int],
) -> tuple[list[dict[str, Any]], tuple[Any, ...], set[tuple[str, str, str]]]:
    sample_id = "gths_" + "9" * 64
    semantic_units: list[dict[str, Any]] = []
    compiled_units: list[Any] = []
    member_keys: set[tuple[str, str, str]] = set()
    lifecycle_by_category = {
        "reschedule": "rescheduled_old",
        "cancellation": "cancelled",
        "completion": "completed",
        "timezone": "none",
    }
    for category, count in category_counts.items():
        for index in range(count):
            unit_id = f"{category}_{index}"
            member_id = "member"
            member_key = (sample_id, unit_id, member_id)
            member_keys.add(member_key)
            lifecycle = lifecycle_by_category[category]
            normalized_value = (
                "2027-08-14T16:30:00+00:00" if category == "timezone" else "2027-08-14"
            )
            semantic_units.append(
                {
                    "unit_id": unit_id,
                    "members": [
                        {
                            "member_id": member_id,
                            "alternatives": [
                                {
                                    "locator": {
                                        "lifecycle_mention": (
                                            None
                                            if category == "timezone"
                                            else {
                                                "lifecycle_role": lifecycle,
                                            }
                                        ),
                                        "derived": {
                                            "lifecycle": lifecycle,
                                            "normalized_value": normalized_value,
                                        },
                                    }
                                }
                            ],
                        }
                    ],
                }
            )
            compiled_units.append(
                SimpleNamespace(
                    key=(sample_id, unit_id),
                    members=(
                        SimpleNamespace(
                            key=member_key,
                            expected_verdict="supported",
                        ),
                    ),
                )
            )
    return (
        [{"sample_id": sample_id, "gold": {"semantic_units": semantic_units}}],
        tuple(compiled_units),
        member_keys,
    )


def test_lifecycle_category_miss_fails_when_global_recall_still_meets_gate() -> None:
    samples, units, member_keys = _lifecycle_metric_fixture(
        {
            "reschedule": 1,
            "cancellation": 7,
            "completion": 6,
            "timezone": 6,
        }
    )
    missed_reschedule = next(
        member_key for member_key in member_keys if member_key[1] == "reschedule_0"
    )
    matched = member_keys - {missed_reschedule}
    assert len(matched) / len(member_keys) == 0.95

    metrics, gate_passed = scorer._challenge_lifecycle_source_gold_metrics(
        samples=samples,
        units=units,
        matched_effective_member_keys=matched,
        matched_confirmed_member_keys=matched,
    )

    assert metrics["all_required_categories_present"] is True
    assert metrics["categories"]["reschedule"]["effective_recall"]["numerator"] == 0
    assert metrics["categories"]["reschedule"]["effective_recall"]["denominator"] == 1
    assert metrics["categories"]["reschedule"]["confirmed_recall"]["estimate"] == 0.0
    assert metrics["categories"]["cancellation"]["gate_passed"] is True
    assert gate_passed is False
    assert scorer._validated_challenge_lifecycle_source_gold_gate(metrics) is False


def test_lifecycle_gate_fails_closed_when_required_category_is_absent() -> None:
    samples, units, member_keys = _lifecycle_metric_fixture(
        {
            "reschedule": 1,
            "cancellation": 1,
            "completion": 1,
        }
    )

    metrics, gate_passed = scorer._challenge_lifecycle_source_gold_metrics(
        samples=samples,
        units=units,
        matched_effective_member_keys=member_keys,
        matched_confirmed_member_keys=member_keys,
    )

    timezone = metrics["categories"]["timezone"]
    assert timezone["category_present"] is False
    assert timezone["effective_recall"] == {
        "numerator": 0,
        "denominator": 0,
        "estimate": None,
        "wilson_95_lower": None,
        "wilson_95_upper": None,
        "interval_defined": False,
    }
    assert timezone["gate_passed"] is False
    assert metrics["all_required_categories_present"] is False
    assert gate_passed is False


def test_primary_score_binds_separate_authenticated_challenge_without_pooling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "score-two-estimand"
    raw_fixture = _fixture(base, monkeypatch, include_challenge=True)
    challenge = _add_scoring_evidence(
        raw_fixture,
        base=base,
        cohort="challenge",
    )
    scorer.score_gmail_temporal_holdout(
        challenge["output"],
        challenge["key"],
        challenge["checkpoints"],
        challenge["attestations"],
        challenge["score_output"],
    )
    primary = _add_scoring_evidence(
        raw_fixture,
        base=base,
        cohort="primary",
    )

    result = scorer.score_gmail_temporal_holdout(
        primary["output"],
        primary["key"],
        primary["checkpoints"],
        primary["attestations"],
        primary["score_output"],
        challenge_score_root=challenge["score_output"],
    )

    assert result["challenge_score_bound"] is True
    assert result["challenge_scoring_pending"] is False
    assert result["challenge_safety_gate_passed"] is True
    assert result["two_estimand_gate_passed"] is True
    assert result["promotion_pending"] is True
    assert result["release_score_gate_passed"] is False
    score = json.loads((primary["score_output"] / scorer.SCORE_ARTIFACT).read_bytes())
    assert score["metrics_must_not_be_pooled_across_cohorts"] is True
    assert score["natural_recall_metrics_diagnostic_only"] is True
    assert score["challenge_score_bound"] is True
    assert "challenge_lifecycle_source_gold_metrics" not in score
    assert (
        score["challenge_safety_gates"]["challenge_lifecycle_source_gold_gate"] is True
    )
    manifest = json.loads((primary["score_output"] / "manifest.json").read_bytes())
    assert (
        manifest["bound_challenge_score_manifest_sha256"]
        == hashlib.sha256(
            (challenge["score_output"] / "manifest.json").read_bytes()
        ).hexdigest()
    )


def test_challenge_scorer_rejects_primary_checkpoint_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _scoring_fixture(tmp_path / "score-primary-mix", monkeypatch)
    challenge = _scoring_fixture(
        tmp_path / "score-challenge-mix",
        monkeypatch,
        cohort="challenge",
    )
    mixed_checkpoints = (
        primary["checkpoints"][0],
        challenge["checkpoints"][1],
        challenge["checkpoints"][2],
    )

    with pytest.raises(
        scorer.GmailTemporalHoldoutScoreError,
        match="checkpoint",
    ):
        scorer.score_gmail_temporal_holdout(
            challenge["output"],
            challenge["key"],
            mixed_checkpoints,
            challenge["attestations"],
            challenge["score_output"],
        )
    assert not challenge["score_output"].exists()


def test_scorer_rejects_checkpoint_that_no_longer_matches_frozen_hash_or_frontier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _scoring_fixture(tmp_path / "score-tamper", monkeypatch)
    checkpoint = fixture["checkpoints"][1]
    _write_private(checkpoint, checkpoint.read_bytes() + b"{}\n")

    with pytest.raises(
        scorer.GmailTemporalHoldoutScoreError,
        match="checkpoint",
    ):
        scorer.score_gmail_temporal_holdout(
            fixture["output"],
            fixture["key"],
            fixture["checkpoints"],
            fixture["attestations"],
            fixture["score_output"],
        )


@pytest.mark.parametrize(
    ("case", "updates", "resign"),
    [
        ("tampered-hmac", {}, False),
        (
            "model-mismatch",
            {"model": "gpt-5.5"},
            True,
        ),
        (
            "effort-mismatch",
            {"reasoning_effort": "high"},
            True,
        ),
        (
            "checkpoint-mismatch",
            {"checkpoint_sha256": "0" * 64},
            True,
        ),
        (
            "frozen-request-mismatch",
            {"frozen_request_artifact_sha256": "0" * 64},
            True,
        ),
        (
            "partition-mismatch",
            {"request_partition_sha256": "0" * 64},
            True,
        ),
        (
            "request-set-mismatch",
            {"request_set_sha256": "0" * 64},
            True,
        ),
        (
            "false-exact-coverage",
            {"exact_request_coverage": False},
            True,
        ),
        (
            "external-call-count-mismatch",
            {"external_calls": 999},
            True,
        ),
        (
            "protocol-mismatch",
            {"protocol_fingerprint": "gtfproto_" + "0" * 64},
            True,
        ),
    ],
)
def test_scorer_rejects_invalid_invocation_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    updates: dict[str, Any],
    resign: bool,
) -> None:
    fixture = _scoring_fixture(tmp_path / case, monkeypatch)
    _rewrite_attestation(fixture, 1, resign=resign, **updates)

    with pytest.raises(
        scorer.GmailTemporalHoldoutScoreError,
        match="independent invocation attestation",
    ):
        scorer.score_gmail_temporal_holdout(
            fixture["output"],
            fixture["key"],
            fixture["checkpoints"],
            fixture["attestations"],
            fixture["score_output"],
        )
    assert not fixture["score_output"].exists()


def test_scorer_rejects_authenticated_v1_invocation_attestation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _scoring_fixture(tmp_path / "attestation-v1", monkeypatch)
    checkpoint = fixture["checkpoints"][0]
    adapter_manifest_raw = (fixture["output"] / "manifest.json").read_bytes()
    value = {
        "version": "gmail_temporal_holdout_invocation_attestation_v1",
        "run_ordinal": 1,
        "invocation_id": "external-codex-invocation-1",
        "provider": "external-codex",
        "model": adapter.candidate_evaluator.EXPECTED_MODEL,
        "reasoning_effort": adapter.candidate_evaluator.EXPECTED_REASONING_EFFORT,
        "started_at": "2027-08-01T10:01:00+00:00",
        "completed_at": "2027-08-01T10:01:30+00:00",
        "adapter_manifest_sha256": hashlib.sha256(
            adapter_manifest_raw
        ).hexdigest(),
        "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        "cohort": "primary",
        "independent_invocation": True,
        "external_calls": 1,
        "routable": False,
    }
    authenticator = hmac.new(
        fixture["key_value"],
        b"gmail_temporal_holdout_invocation_attestation_v1\0"
        + scorer._canonical_json(value),
        hashlib.sha256,
    ).hexdigest()
    _write_private(
        fixture["attestations"][0],
        scorer._canonical_json(
            {**value, "attestation_hmac_sha256": authenticator}
        )
        + b"\n",
    )

    with pytest.raises(
        scorer.GmailTemporalHoldoutScoreError,
        match="independent invocation attestation",
    ):
        scorer.score_gmail_temporal_holdout(
            fixture["output"],
            fixture["key"],
            fixture["checkpoints"],
            fixture["attestations"],
            fixture["score_output"],
        )
    assert not fixture["score_output"].exists()


def test_scorer_rejects_retained_request_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _scoring_fixture(tmp_path / "retained-request-tamper", monkeypatch)
    request_path = next((fixture["run_roots"][0] / "calls").glob("*/*/request.json"))
    request = json.loads(request_path.read_bytes())
    request["contract"] += " tampered"
    _write_private(
        request_path,
        external_runner._canonical_json(request) + b"\n",
    )

    with pytest.raises(
        scorer.GmailTemporalHoldoutScoreError,
        match="independent invocation attestation",
    ):
        scorer.score_gmail_temporal_holdout(
            fixture["output"],
            fixture["key"],
            fixture["checkpoints"],
            fixture["attestations"],
            fixture["score_output"],
        )
    assert not fixture["score_output"].exists()


def test_scorer_rejects_current_source_hash_mismatch_when_resigned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _scoring_fixture(tmp_path / "source-hash-mismatch", monkeypatch)
    value = json.loads(fixture["attestations"][0].read_bytes())
    source_hashes = dict(value["source_module_sha256"])
    source_hashes["runner"] = "0" * 64
    _rewrite_attestation(
        fixture,
        0,
        source_module_sha256=source_hashes,
    )

    with pytest.raises(
        scorer.GmailTemporalHoldoutScoreError,
        match="independent invocation attestation",
    ):
        scorer.score_gmail_temporal_holdout(
            fixture["output"],
            fixture["key"],
            fixture["checkpoints"],
            fixture["attestations"],
            fixture["score_output"],
        )
    assert not fixture["score_output"].exists()


def test_scorer_rejects_duplicate_invocation_ids_even_when_resigned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _scoring_fixture(tmp_path / "duplicate-invocation", monkeypatch)
    first = json.loads(fixture["attestations"][0].read_bytes())
    _rewrite_attestation(
        fixture,
        1,
        invocation_ids=first["invocation_ids"],
        invocation_count=first["invocation_count"],
        external_calls=first["external_calls"],
    )

    with pytest.raises(
        scorer.GmailTemporalHoldoutScoreError,
        match="independent invocation attestation",
    ):
        scorer.score_gmail_temporal_holdout(
            fixture["output"],
            fixture["key"],
            fixture["checkpoints"],
            fixture["attestations"],
            fixture["score_output"],
        )
    assert not fixture["score_output"].exists()


def test_scorer_rejects_duplicate_logical_run_ids_even_when_resigned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _scoring_fixture(tmp_path / "duplicate-logical-run", monkeypatch)
    first = json.loads(fixture["attestations"][0].read_bytes())
    _rewrite_attestation(
        fixture,
        1,
        logical_run_id=first["logical_run_id"],
    )

    with pytest.raises(
        scorer.GmailTemporalHoldoutScoreError,
        match="independent invocation attestation",
    ):
        scorer.score_gmail_temporal_holdout(
            fixture["output"],
            fixture["key"],
            fixture["checkpoints"],
            fixture["attestations"],
            fixture["score_output"],
        )
    assert not fixture["score_output"].exists()


def test_scorer_rejects_copied_attestations_for_copied_checkpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _scoring_fixture(tmp_path / "copied-runs", monkeypatch)
    _write_private(
        fixture["attestations"][1],
        fixture["attestations"][0].read_bytes(),
    )

    with pytest.raises(
        scorer.GmailTemporalHoldoutScoreError,
        match="independent invocation attestation",
    ):
        scorer.score_gmail_temporal_holdout(
            fixture["output"],
            fixture["key"],
            fixture["checkpoints"],
            fixture["attestations"],
            fixture["score_output"],
        )
    assert not fixture["score_output"].exists()


def test_release_eligible_adapter_still_rejects_copied_run_attestations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _scoring_fixture(tmp_path / "release-copied-runs", monkeypatch)
    manifest_raw = _rewrite_adapter_manifest(
        fixture,
        diagnostic_only=False,
        source_release_evidence_class=finalizer.PROSPECTIVE_EVIDENCE_CLASS,
        release_evidence_class=finalizer.PROSPECTIVE_EVIDENCE_CLASS,
        source_release_scope="local_review_only",
        cohort_evidence_scope="prospective_natural_operability_review_only",
        release_scope="prospective_natural_operability_review_only",
        source_prospective_unseen_source_evidence=True,
        prospective_unseen_source_evidence=True,
        source_historical_architecture_exposed=False,
        historical_architecture_exposed=False,
        retrospective_calibration_eligible=False,
        source_semantic_development_overlap_status=("excluded_by_frozen_thread_scope"),
        semantic_development_overlap_status="excluded_by_frozen_thread_scope",
        content_changing_canary_required=True,
        label_authority_manifest_sha256="a" * 64,
        label_authority_version=finalizer.LABEL_AUTHORITY_VERSION,
        label_authority_model=finalizer.LABEL_AUTHORITY_MODEL,
        label_authority_reasoning_effort=(finalizer.LABEL_AUTHORITY_REASONING_EFFORT),
        label_authority_invocation_count=1,
        sol_label_authority_attested=True,
        source_only_label_authority_attested=True,
        release_structural_scorability_verified=True,
        source_primary_release_holdout_eligible=True,
        source_release_holdout_eligible=True,
        release_holdout_eligible=True,
    )
    manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
    for index in range(3):
        _rewrite_attestation(
            fixture,
            index,
            adapter_manifest_sha256=manifest_sha256,
        )
    copied = fixture["attestations"][0].read_bytes()
    _write_private(fixture["attestations"][1], copied)
    _write_private(fixture["attestations"][2], copied)

    with pytest.raises(
        scorer.GmailTemporalHoldoutScoreError,
        match="independent invocation attestation",
    ):
        scorer.score_gmail_temporal_holdout(
            fixture["output"],
            fixture["key"],
            fixture["checkpoints"],
            fixture["attestations"],
            fixture["score_output"],
        )
    assert not fixture["score_output"].exists()


def test_scorer_cli_forwards_optional_challenge_score_root(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}

    def _fake_score(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {
            "version": scorer.VERSION,
            "status": "scored",
            "private_content_printed": False,
        }

    monkeypatch.setattr(scorer, "score_gmail_temporal_holdout", _fake_score)
    argv = [
        str(SCORER_PATH),
        "--adapter-root",
        "/adapter",
        "--hmac-key",
        "/key",
    ]
    for index in range(3):
        argv.extend(("--checkpoint", f"/checkpoint-{index}"))
    for index in range(3):
        argv.extend(("--attestation", f"/attestation-{index}"))
    argv.extend(
        (
            "--challenge-score-root",
            "/challenge-score",
            "--output-root",
            "/output",
        )
    )
    monkeypatch.setattr(sys, "argv", argv)

    scorer.main()

    assert captured["kwargs"] == {"challenge_score_root": Path("/challenge-score")}
    assert json.loads(capsys.readouterr().out)["status"] == "scored"


@pytest.mark.parametrize(
    ("checkpoint_count", "attestation_count"),
    [(2, 3), (3, 2), (4, 3), (3, 4)],
)
def test_scorer_cli_rejects_any_count_other_than_three_without_private_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    checkpoint_count: int,
    attestation_count: int,
) -> None:
    marker = "PRIVATE-ARITY-MARKER"
    argv = [
        str(SCORER_PATH),
        "--adapter-root",
        "/missing/adapter",
        "--hmac-key",
        "/missing/key",
    ]
    for index in range(checkpoint_count):
        argv.extend(("--checkpoint", f"/missing/{marker}-checkpoint-{index}"))
    for index in range(attestation_count):
        argv.extend(("--attestation", f"/missing/{marker}-attestation-{index}"))
    argv.extend(("--output-root", "/missing/output"))
    monkeypatch.setattr(
        sys,
        "argv",
        argv,
    )

    with pytest.raises(SystemExit) as exc:
        scorer.main()

    assert exc.value.code == 2
    output = capsys.readouterr().out
    assert marker not in output
    assert json.loads(output) == {
        "version": scorer.VERSION,
        "status": "failed",
        "error": "gmail_temporal_holdout_scoring_failed",
        "private_content_printed": False,
    }
