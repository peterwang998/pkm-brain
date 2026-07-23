from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

from pkm_brain.gmail_temporal_public_v20 import (
    PUBLIC_GMAIL_TEMPORAL_V20_CASES,
)
from pkm_brain.gmail_temporal_synthetic_gold import SEMANTIC_GOLD


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "evaluate_gmail_temporal_public_v20.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "test_evaluate_gmail_temporal_public_v20",
        SCRIPT,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


benchmark = _load_script()


def test_fixture_is_separate_from_v19_and_covers_required_contrasts() -> None:
    by_id = {item.case_id: item for item in PUBLIC_GMAIL_TEMPORAL_V20_CASES}

    assert len(by_id) == len(PUBLIC_GMAIL_TEMPORAL_V20_CASES) == 90
    assert not set(by_id).intersection(SEMANTIC_GOLD)
    assert {
        "lex_policy_effective_date",
        "lex_applications_open",
        "lex_policy_in_force",
        "lex_enrollment_begins",
        "lex_registration_open_from",
        "lex_rule_applies_as_of",
        "lex_applications_are_open_from",
        "lex_applications_will_be_open_from",
        "lex_applications_remain_open_from",
        "lex_enrollment_starts",
        "neg_keep_applications_open",
        "neg_reviewed_applications_open",
        "neg_use_applications_open",
        "neg_registration_cross_segment",
        "neg_advertising_applications_open",
        "neg_stores_are_open_from",
        "neg_report_will_be_open_from",
        "neg_application_starts_with_questionnaire",
        "neg_applications_are_open_for_qa",
        "neg_applications_are_open_cross_segment",
        "neg_advertising_applications_will_be_open_from",
        "reschedule_direct",
        "reschedule_inverse",
        "reschedule_new_date",
        "reschedule_now_instead",
        "reschedule_arrow_forward",
        "reschedule_arrow_reverse",
        "reschedule_replacement_postponed",
        "reschedule_replacement_moved",
        "ambiguous_new_date_options",
        "ambiguous_now_instead_options",
        "ambiguous_arrow_replacements",
        "ambiguous_direct_replacements",
        "ambiguous_connector_or_on",
        "ambiguous_connector_or_at",
        "ambiguous_connector_or_perhaps",
        "ambiguous_connector_or_maybe",
        "ambiguous_connector_or_maybe_at",
        "ambiguous_connector_or_possibly",
        "ambiguous_connector_or_potentially",
        "ambiguous_connector_or_conceivably",
        "ambiguous_inverse_old_options",
        "ambiguous_three_replacements",
        "ambiguous_replacement_only_postponed_options",
        "ambiguous_replacement_only_new_date_options",
        "ambiguous_replacement_only_three_options",
        "ambiguous_old_only_options",
        "ambiguous_leading_inverse_replacements",
        "ambiguous_leading_now_replacements",
        "ambiguous_leading_arrow_olds",
        "ambiguous_leading_new_date_replacements",
        "ambiguous_collapsed_old_slot",
        "ambiguous_both_endpoint_slots",
        "ambiguous_collapsed_both_slots",
        "abbreviated_direct_or_day",
        "abbreviated_direct_slash_day",
        "abbreviated_replacement_only_or_day",
        "abbreviated_replacement_only_slash_day",
        "abbreviated_inverse_or_day",
        "abbreviated_inverse_slash_day",
        "abbreviated_old_slot_or_day",
        "abbreviated_old_slot_slash_day",
        "abbreviated_ordinal_day",
        "abbreviated_day_first",
        "abbreviated_invalid_day_unresolved",
        "abbreviated_invalid_slash_day_unresolved",
        "abbreviated_raw_direct_ordinal_article",
        "abbreviated_raw_direct_shared_trailing_year",
        "abbreviated_raw_replacement_only_ordinal_article",
        "abbreviated_raw_inverse_ordinal_article",
        "abbreviated_raw_old_slot_ordinal_article",
        "abbreviated_negative_prose",
        "abbreviated_negative_count",
        "abbreviated_negative_full_date_slash",
        "lifecycle_cancelled_exact",
        "lifecycle_date_free_cancellation",
    }.issubset(by_id)


def test_public_v20_reports_each_structural_metric_separately() -> None:
    report = benchmark.evaluate_public_v20()
    metrics = report["metrics"]

    assert report["case_count"] == 90
    assert "selected_negative_false_positives" not in metrics
    assert metrics["positive_candidate_bearing_recall"] == {
        "numerator": 71,
        "denominator": 71,
        "rate": 1.0,
    }
    assert metrics["matched_non_advertising_negative_candidate_free"] == {
        "candidate_free_count": 15,
        "candidate_bearing_count": 0,
        "denominator": 15,
        "rate": 1.0,
    }
    assert metrics["all_selected_negative_candidate_bearing"] == {
        "count": 2,
        "denominator": 17,
        "rate": 2 / 17,
    }
    assert metrics["fixture_selected_negative_artifacts_not_semantic_precision"] == {
        "count": 0,
        "denominator": 17,
        "rate": 0.0,
        "not_semantic_precision": True,
    }
    assert metrics["exact_lifecycle_grouping_role_coverage"] == {
        "numerator": 10,
        "denominator": 10,
        "rate": 1.0,
    }
    assert metrics["ambiguous_case_fail_closed_coverage"] == {
        "numerator": 48,
        "denominator": 48,
        "rate": 1.0,
    }
    assert metrics["cancellation_lifecycle_limit_coverage"] == {
        "numerator": 4,
        "denominator": 4,
        "rate": 1.0,
    }
    assert metrics["abbreviated_shared_month_day_discovery_coverage"] == {
        "numerator": 20,
        "denominator": 20,
        "rate": 1.0,
    }
    assert metrics["abbreviated_shared_month_day_positive_inventory_coverage"] == {
        "numerator": 12,
        "denominator": 12,
        "rate": 1.0,
    }
    assert metrics["abbreviated_shared_month_day_negative_exclusion_coverage"] == {
        "numerator": 8,
        "denominator": 8,
        "rate": 1.0,
    }
    assert metrics["abbreviated_shared_month_day_invalid_date_inventory_coverage"] == {
        "numerator": 2,
        "denominator": 2,
        "rate": 1.0,
    }
    assert metrics["raw_abbreviated_tail_fallback_coverage"] == {
        "numerator": 2,
        "denominator": 2,
        "rate": 1.0,
    }
    assert metrics["raw_abbreviated_source_guard_coverage"] == {
        "numerator": 5,
        "denominator": 5,
        "rate": 1.0,
    }
    assert metrics["critical_structural_errors"] == {
        "count": 0,
        "case_count": 0,
        "case_ids": [],
        "error_codes": [],
    }
    assert report["gate_passed"] is True


def test_p1_candidate_exposure_and_advertising_fixture_selection_are_distinct() -> None:
    by_id = {item.case_id: item for item in PUBLIC_GMAIL_TEMPORAL_V20_CASES}

    for case_id in (
        "neg_keep_applications_open",
        "neg_reviewed_applications_open",
        "neg_use_applications_open",
        "neg_registration_cross_segment",
    ):
        outcome = benchmark._evaluate_case(by_id[case_id])
        assert outcome.candidates == ()
        assert outcome.projection.artifacts == ()

    advertising = benchmark._evaluate_case(by_id["neg_advertising_applications_open"])
    assert advertising.candidates
    assert advertising.projection.artifacts == ()


def test_intake_recall_and_lookalike_controls_stay_structurally_separate() -> None:
    by_id = {item.case_id: item for item in PUBLIC_GMAIL_TEMPORAL_V20_CASES}

    for case_id in (
        "lex_applications_are_open_from",
        "lex_applications_will_be_open_from",
        "lex_applications_remain_open_from",
        "lex_enrollment_starts",
    ):
        outcome = benchmark._evaluate_case(by_id[case_id])
        assert outcome.case.positive_candidate is True
        assert outcome.case.selected_negative is False
        assert outcome.candidates
        assert outcome.errors == ()

    for case_id in (
        "neg_stores_are_open_from",
        "neg_report_will_be_open_from",
        "neg_application_starts_with_questionnaire",
        "neg_applications_are_open_for_qa",
        "neg_applications_are_open_cross_segment",
    ):
        outcome = benchmark._evaluate_case(by_id[case_id])
        assert outcome.case.selected_negative is True
        assert outcome.case.advertising_negative is False
        assert outcome.candidates == ()
        assert outcome.projection.artifacts == ()
        assert outcome.errors == ()

    advertising = benchmark._evaluate_case(
        by_id["neg_advertising_applications_will_be_open_from"]
    )
    assert advertising.case.selected_negative is True
    assert advertising.case.advertising_negative is True
    assert advertising.candidates
    assert advertising.projection.artifacts == ()
    assert advertising.errors == ()


def test_final_alternative_connectors_and_residual_slots_fail_closed() -> None:
    by_id = {item.case_id: item for item in PUBLIC_GMAIL_TEMPORAL_V20_CASES}
    case_ids = (
        "ambiguous_connector_or_on",
        "ambiguous_connector_or_at",
        "ambiguous_connector_or_perhaps",
        "ambiguous_connector_or_maybe",
        "ambiguous_connector_or_maybe_at",
        "ambiguous_replacement_only_postponed_options",
        "ambiguous_replacement_only_new_date_options",
        "ambiguous_replacement_only_three_options",
        "ambiguous_old_only_options",
        "ambiguous_leading_inverse_replacements",
        "ambiguous_leading_now_replacements",
        "ambiguous_leading_arrow_olds",
        "ambiguous_leading_new_date_replacements",
        "ambiguous_collapsed_old_slot",
        "ambiguous_both_endpoint_slots",
        "ambiguous_collapsed_both_slots",
    )

    for case_id in case_ids:
        outcome = benchmark._evaluate_case(by_id[case_id])
        assert outcome.candidates
        assert outcome.group_matches is True
        assert outcome.fail_closed is True
        assert outcome.errors == ()
        group = outcome.projection.groups[0]
        assert (group.kind, group.coverage) == ("reschedule", "conflicted")
        assert group.candidate_authorization is False
        assert group.routable is False


def test_generic_hedges_quarantine_every_reschedule_endpoint() -> None:
    by_id = {item.case_id: item for item in PUBLIC_GMAIL_TEMPORAL_V20_CASES}

    for case_id in (
        "ambiguous_connector_or_possibly",
        "ambiguous_connector_or_potentially",
        "ambiguous_connector_or_conceivably",
    ):
        outcome = benchmark._evaluate_case(by_id[case_id])
        assert outcome.candidates
        assert outcome.group_matches is True
        assert outcome.fail_closed is True
        assert outcome.errors == ()
        group = outcome.projection.groups[0]
        assert tuple(member.role for member in group.members) == (
            "unresolved",
            "unresolved",
            "unresolved",
        )
        assert group.reasons == ("reschedule_endpoint_connector_unresolved",)


def test_abbreviated_shared_month_day_inventory_and_roles_are_exact() -> None:
    by_id = {item.case_id: item for item in PUBLIC_GMAIL_TEMPORAL_V20_CASES}
    case_ids = (
        "abbreviated_direct_or_day",
        "abbreviated_direct_slash_day",
        "abbreviated_replacement_only_or_day",
        "abbreviated_replacement_only_slash_day",
        "abbreviated_inverse_or_day",
        "abbreviated_inverse_slash_day",
        "abbreviated_old_slot_or_day",
        "abbreviated_old_slot_slash_day",
        "abbreviated_ordinal_day",
        "abbreviated_day_first",
        "abbreviated_invalid_day_unresolved",
        "abbreviated_invalid_slash_day_unresolved",
    )

    for case_id in case_ids:
        outcome = benchmark._evaluate_case(by_id[case_id])
        assert outcome.candidates
        assert outcome.abbreviated_day_matches is True
        assert outcome.group_matches is True
        assert outcome.fail_closed is True
        assert outcome.errors == ()

    for case_id in (
        "abbreviated_invalid_day_unresolved",
        "abbreviated_invalid_slash_day_unresolved",
    ):
        invalid = benchmark._evaluate_case(by_id[case_id])
        shorthand = next(
            item
            for item in invalid.analysis.expressions
            if item.form == "abbreviated_shared_month_day"
        )
        assert shorthand.resolution_status == "unresolved"
        assert shorthand.normalized_options == ()
        assert shorthand.calendar_date_options == ()
        assert shorthand.blockers == (
            "reschedule_endpoint_alternatives_unresolved",
            "invalid_calendar_date",
        )


def test_raw_abbreviated_tail_guard_remains_a_fail_closed_fallback() -> None:
    by_id = {item.case_id: item for item in PUBLIC_GMAIL_TEMPORAL_V20_CASES}

    for case_id in (
        "abbreviated_direct_or_day",
        "abbreviated_replacement_only_slash_day",
    ):
        outcome = benchmark._evaluate_case(by_id[case_id])
        assert outcome.raw_fallback_matches is True
        assert outcome.errors == ()


def test_raw_source_guards_cover_uninventoried_abbreviated_endpoints() -> None:
    by_id = {item.case_id: item for item in PUBLIC_GMAIL_TEMPORAL_V20_CASES}
    case_ids = (
        "abbreviated_raw_direct_ordinal_article",
        "abbreviated_raw_direct_shared_trailing_year",
        "abbreviated_raw_replacement_only_ordinal_article",
        "abbreviated_raw_inverse_ordinal_article",
        "abbreviated_raw_old_slot_ordinal_article",
    )

    for case_id in case_ids:
        outcome = benchmark._evaluate_case(by_id[case_id])
        assert outcome.case.raw_source_guard_case is True
        assert outcome.abbreviated_day_matches is True
        assert not any(
            item.form == "abbreviated_shared_month_day"
            for item in outcome.analysis.expressions
        )
        assert outcome.group_matches is True
        assert outcome.fail_closed is True
        assert outcome.errors == ()


def test_abbreviated_parser_negatives_are_not_semantic_negative_fixtures() -> None:
    by_id = {item.case_id: item for item in PUBLIC_GMAIL_TEMPORAL_V20_CASES}

    prose = benchmark._evaluate_case(by_id["abbreviated_negative_prose"])
    assert prose.abbreviated_day_matches is True
    assert prose.case.positive_candidate is False
    assert prose.case.selected_negative is False
    assert prose.candidates == ()
    assert prose.projection.artifacts == ()

    for case_id in (
        "abbreviated_negative_count",
        "abbreviated_negative_full_date_slash",
    ):
        outcome = benchmark._evaluate_case(by_id[case_id])
        assert outcome.abbreviated_day_matches is True
        assert outcome.case.positive_candidate is True
        assert outcome.case.selected_negative is False
        assert outcome.candidates
        assert not any(
            item.form == "abbreviated_shared_month_day"
            for item in outcome.analysis.expressions
        )
        assert outcome.errors == ()


def test_report_is_content_free_and_disclaims_semantic_precision() -> None:
    report = benchmark.evaluate_public_v20()
    serialized = json.dumps(report, sort_keys=True)

    assert all(case.text not in serialized for case in PUBLIC_GMAIL_TEMPORAL_V20_CASES)
    assert report["claims"] == {
        "public_synthetic_text_only": True,
        "private_email_records": 0,
        "external_model_calls": 0,
        "network_calls": 0,
        "persistence_writes": 0,
        "retrieval_measured": False,
        "promoted_semantic_precision_measured": False,
        "fixture_verdicts_are_model_judgments": False,
        "deterministic_negative_candidate_metrics_only": True,
        "abbreviated_parser_guards_measured_separately_from_semantic_negatives": True,
        "fixture_selected_negative_artifact_metric_is_semantic_precision": False,
        "grouped_metadata_counted_as_promoted_semantic_precision": False,
        "source_text_emitted": False,
    }


def test_cli_emits_only_the_content_free_report() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)

    assert completed.stderr == ""
    assert report == benchmark.evaluate_public_v20()
    assert report["scope"] == (
        "public_only_deterministic_structure_not_semantic_precision"
    )
