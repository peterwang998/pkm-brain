from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from pkm_brain.gmail_temporal_contextual_lifecycle_public import (
    PUBLIC_CONTEXTUAL_LIFECYCLE_CASES,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "evaluate_gmail_temporal_contextual_lifecycle_public.py"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "test_contextual_lifecycle_public_evaluator",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


evaluator = _load_script()


def test_fixture_is_balanced_and_covers_fail_closed_contrasts() -> None:
    by_id = {item.case_id: item for item in PUBLIC_CONTEXTUAL_LIFECYCLE_CASES}

    assert len(by_id) == len(PUBLIC_CONTEXTUAL_LIFECYCLE_CASES) == 87
    assert sum(item.expected_outcome != "none" for item in by_id.values()) == 32
    assert sum(item.expected_outcome == "none" for item in by_id.values()) == 55
    assert {
        "positive_explicit_cancelled",
        "positive_unique_event_anaphora",
        "positive_two_event_anaphora_retained",
        "positive_reschedule_without_endpoint",
        "positive_reply_subject_lifecycle_prefix",
        "positive_two_event_explicit_beta",
        "positive_two_event_reschedule_ambiguous",
        "positive_unrelated_footer_date_cancelled",
        "positive_duplicate_alias_cancellation",
        "positive_recurrence_alias_completion",
        "negative_not_cancelled",
        "negative_quoted_cancellation",
        "negative_unrelated_hotel",
        "negative_unrelated_order",
        "negative_unrelated_subscription",
        "negative_cross_thread_anchor",
        "negative_cross_thread_matching_other_event",
        "negative_owner_without_receipt",
        "negative_standard_temporal_path",
        "negative_conflicting_lifecycle_cues",
        "negative_modal_might_cancelled",
        "negative_conditional_if_cancelled",
        "negative_denied_thought_cancelled",
        "negative_reported_then_refuted",
        "negative_stale_subject_hotel_pronoun",
        "negative_interrogative_cancelled",
        "negative_epistemic_probably_cancelled",
        "negative_epistemic_allegedly_completed",
        "negative_epistemic_appears_cancelled",
        "negative_epistemic_seems_completed",
        "negative_named_speaker_said_cancelled",
        "negative_named_speaker_reported_completed",
        "negative_according_to_report",
        "negative_false_that_cancelled",
        "negative_claim_refuted_after_cue",
        "negative_inline_ascii_quote",
        "negative_inline_curly_quote",
        "negative_embedded_interrogative",
        "negative_postposed_named_speaker",
        "negative_postposed_according_to",
        "negative_postposed_epistemic",
        "negative_unpunctuated_interrogative",
        "negative_postposed_confirmation",
        "negative_postposed_was_told",
        "negative_expected_cancellation",
        "negative_future_cancellation",
        "negative_doubted_cancellation",
        "negative_as_far_as_known",
        "negative_confirmation_request",
    }.issubset(by_id)


def test_public_experiment_meets_recall_biased_deterministic_gate() -> None:
    report = evaluator.evaluate_public_contextual_lifecycle()

    assert report["scope"] == (
        "disabled_public_deterministic_experiment_not_production_evidence"
    )
    assert report["case_count"] == 87
    assert report["positive_count"] == 32
    assert report["confirmed_positive_count"] == 17
    assert report["ambiguous_positive_count"] == 15
    assert report["matched_negative_count"] == 55
    assert report["emitted_observation_count"] == 32
    assert report["metrics"] == {
        "baseline_any_temporal_lead_presence": {
            "numerator": 7,
            "denominator": 32,
            "rate": 7 / 32,
        },
        "baseline_cue_linked_lifecycle_recall": {
            "numerator": 0,
            "denominator": 32,
            "rate": 0.0,
        },
        "effective_lifecycle_recall": {
            "numerator": 32,
            "denominator": 32,
            "rate": 1.0,
        },
        "confirmed_lifecycle_recall": {
            "numerator": 17,
            "denominator": 17,
            "rate": 1.0,
        },
        "supported_observation_precision": {
            "numerator": 17,
            "denominator": 17,
            "rate": 1.0,
        },
        "review_observation_precision": {
            "numerator": 32,
            "denominator": 32,
            "rate": 1.0,
        },
        "matched_negative_suppression": {
            "numerator": 55,
            "denominator": 55,
            "rate": 1.0,
        },
        "ambiguous_observations_without_selected_identity": {
            "count": 15,
            "denominator": 15,
            "rate": 1.0,
        },
        "critical_errors": 0,
        "authorization_errors": 0,
    }
    assert report["metric_comparison"] == {
        "previous_reported_fixture_v1": {
            "supported_observation_precision": {
                "numerator": 18,
                "denominator": 18,
                "rate": 1.0,
            },
            "effective_lifecycle_recall": {
                "numerator": 32,
                "denominator": 32,
                "rate": 1.0,
            },
            "matched_negative_suppression": {
                "numerator": 32,
                "denominator": 32,
                "rate": 1.0,
            },
            "caveat": "included_one_subset_alias_as_supported_and_only_32_negatives",
        },
        "hardened_fixture_v2": {
            "supported_observation_precision": {
                "numerator": 17,
                "denominator": 17,
                "rate": 1.0,
            },
            "effective_lifecycle_recall": {
                "numerator": 32,
                "denominator": 32,
                "rate": 1.0,
            },
            "matched_negative_suppression": {
                "numerator": 55,
                "denominator": 55,
                "rate": 1.0,
            },
            "caveat": "expanded_negative_denominator_and_exact_alias_support_only",
        },
    }
    assert all(report["gates"].values())
    assert report["gate_passed"] is True


def test_negative_cases_emit_no_observations_and_expected_omissions() -> None:
    for case in PUBLIC_CONTEXTUAL_LIFECYCLE_CASES:
        if case.expected_outcome != "none":
            continue
        outcome = evaluator._evaluate_case(case)
        assert outcome.observation is None
        assert outcome.omission_reason == case.expected_omission
        assert outcome.correct is True
        assert outcome.critical_error is False


def test_cli_emits_aggregate_public_evidence_without_source_text() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    serialized = completed.stdout

    assert report["gate_passed"] is True
    assert report["claims"] == {
        "production_integration_enabled": False,
        "private_gmail_used": False,
        "external_model_calls": 0,
        "network_calls": 0,
        "persistence_writes": 0,
        "source_text_emitted": False,
        "semantic_model_precision_measured": False,
        "deterministic_fixture_semantics_measured": True,
    }
    for case in PUBLIC_CONTEXTUAL_LIFECYCLE_CASES:
        assert case.text not in serialized
