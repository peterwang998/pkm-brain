from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "evaluate_gmail_temporal_thread_retrieval_public.py"


def _load():
    spec = importlib.util.spec_from_file_location(
        "test_evaluate_gmail_temporal_thread_retrieval_public",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_public_sidecar_recovers_all_required_context_without_displacement() -> None:
    report = _load().evaluate_public_temporal_thread_retrieval()

    assert report["evidence_class"] == ("public_synthetic_constructed_regression_only")
    assert report["population_claim_authorized"] is False
    assert report["production_integration_enabled"] is False
    assert report["cases"] == 16
    assert report["queries"] == 24
    assert report["control_queries"] == 5
    assert report["metrics"]["baseline_macro_required_source_recall"]["rate"] == (
        0.5729166666666666
    )
    assert report["metrics"]["answer_union_macro_required_source_recall"]["rate"] == 1.0
    assert report["metrics"]["baseline_complete_required_set_recall"]["rate"] == (
        5 / 24
    )
    assert report["metrics"]["answer_union_complete_required_set_recall"]["rate"] == 1.0
    assert report["metrics"]["canonical_direct_ranking_preservation"]["rate"] == 1.0
    assert report["metrics"]["protected_direct_retention"]["rate"] == 1.0
    assert report["metrics"]["control_exact_preservation"]["rate"] == 1.0
    assert report["metrics"]["safe_context_precision"]["rate"] == 1.0
    assert report["metrics"]["verified_context_required_precision"]["rate"] == 1.0
    assert report["metrics"]["required_before_review_ordering"] == {
        "numerator": 1,
        "denominator": 1,
        "rate": 1.0,
    }
    assert report["metrics"]["lifecycle_current_head_recall"]["rate"] == 1.0
    assert report["metrics"]["forbidden_context_count"] == 0
    assert report["metrics"]["future_or_excluded_context_count"] == 0
    assert report["all_gates_passed"] is True


def test_long_status_final_head_and_rank_ten_protection_are_explicit() -> None:
    report = _load().evaluate_public_temporal_thread_retrieval()
    harbor = [item for item in report["query_results"] if item["case_id"] == "harbor"]
    beacon = next(
        item for item in report["query_results"] if item["case_id"] == "beacon"
    )

    assert len(harbor) == 3
    assert all(item["current_head_correct"] is True for item in harbor)
    assert all(item["context_count"] == 1 for item in harbor)
    assert beacon["complete_required_set"] is True
    assert beacon["context_count"] == 3
    assert beacon["protected_missing_count"] == 0


def test_assertion_scope_and_multi_event_forbidden_counts_are_zero() -> None:
    report = _load().evaluate_public_temporal_thread_retrieval()
    targeted = [
        item
        for item in report["query_results"]
        if item["case_id"] in {"mercury", "apollo", "weekly", "cedar"}
    ]

    assert targeted
    assert all(item["forbidden_context_count"] == 0 for item in targeted)
    assert all(item["unsafe_context_count"] == 0 for item in targeted)
    assert all(item["complete_required_set"] is True for item in targeted)


def test_unique_event_pronoun_review_follows_required_verified_context() -> None:
    report = _load().evaluate_public_temporal_thread_retrieval()
    vega = next(
        item for item in report["query_results"] if item["query_id"] == "vega-timeline"
    )

    assert vega["context_count"] == 2
    assert vega["review_context_count"] == 1
    assert vega["required_before_review"] is True
    assert vega["complete_required_set"] is True


def test_typed_alias_controls_reject_sibling_and_mixed_content_wrong_keys() -> None:
    report = _load().evaluate_public_temporal_thread_retrieval()
    controls = {
        item["query_id"]: item
        for item in report["query_results"]
        if item["query_id"]
        in {
            "apollo-filing-with-demo-anchor",
            "mixed-atlas-wrong-key-anchor",
        }
    }

    assert set(controls) == {
        "apollo-filing-with-demo-anchor",
        "mixed-atlas-wrong-key-anchor",
    }
    assert all(
        item["target_event_identity_resolved"] is False for item in controls.values()
    )
    assert all(item["context_count"] == 0 for item in controls.values())
    assert all(item["protected_missing_count"] == 0 for item in controls.values())
