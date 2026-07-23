from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "audit_gmail_temporal_frontier.py"
SPEC = importlib.util.spec_from_file_location(
    "audit_gmail_temporal_frontier", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
audit_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit_module
SPEC.loader.exec_module(audit_module)


def _write_private_cohort(path: Path) -> None:
    records = [
        {
            "sample_id": "useful",
            "stratum": "important_high_confidence",
            "message_internal_at": "2027-08-10T09:00:00-07:00",
            "text": "The Atlas interview is scheduled for August 14, 2027.",
        },
        {
            "sample_id": "noise",
            "stratum": "suppressed_advertising_temporal",
            "message_internal_at": "2027-08-10T09:00:00-07:00",
            "text": "Save 20% before August 18, 2027.",
        },
    ]
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def test_frontier_audit_reports_content_free_current_coverage(tmp_path: Path) -> None:
    cohort = tmp_path / "cohort.jsonl"
    _write_private_cohort(cohort)

    result = audit_module.audit_gmail_temporal_frontier(cohort)

    assert result["version"] == "gmail_temporal_frontier_audit_v3"
    assert result["private_content_printed"] is False
    assert result["external_calls"] == 0
    assert result["private_file_mode"] == "0o600"
    assert len(result["cohort_sha256"]) == 64
    assert result["counts"]["records"] == 2
    assert result["counts"]["covered_expressions"] == result["counts"]["expressions"]
    assert result["counts"]["batch_omissions"] == 0
    assert result["counts"]["frontier_candidates"] > 0
    assert result["admitted_frontier_gaps"] == []
    assert result["stratum_coverage"]["important_high_confidence"] == {
        "empty_frontier_batches": 0,
        "expressions_with_candidates": 1,
        "expressions_without_candidates": 0,
        "incomplete_frontier_batches": 0,
        "omitted_candidate_mentions": 0,
        "records": 1,
        "with_candidates": 1,
        "with_expressions": 1,
        "with_leads": 1,
        "with_mentions": 1,
        "with_no_expression_candidates": 0,
        "with_pages": 1,
        "with_partial_expression_coverage": 0,
    }
    assert result["maxima"]["candidates_per_page"] <= 12
    assert result["maxima"]["clusters_per_page"] <= 4
    assert result["maxima"]["page_payload_bytes"] <= 12_000
    serialized = json.dumps(result)
    assert "Atlas" not in serialized
    assert "Save 20%" not in serialized


def test_frontier_audit_counts_only_deferred_singleton_event_fallbacks(
    tmp_path: Path,
) -> None:
    cohort = tmp_path / "singleton-cohort.jsonl"
    records = [
        {
            "sample_id": "singleton-fact",
            "stratum": "important_ambiguous",
            "message_internal_at": "2027-05-01T10:00:00-07:00",
            "text": "The workshop update is ready. May 14, 2027.",
        },
        {
            "sample_id": "singleton-rescue",
            "stratum": "suppressed_routine_temporal",
            "message_internal_at": "2027-05-01T10:00:00-07:00",
            "text": "The interview update is ready. May 15, 2027.",
        },
        {
            "sample_id": "singleton-action-negative",
            "stratum": "durable_lead",
            "message_internal_at": "2027-05-01T10:00:00-07:00",
            "text": "Please submit the form. May 16, 2027.",
        },
    ]
    cohort.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    os.chmod(cohort, 0o600)

    result = audit_module.audit_gmail_temporal_frontier(cohort)

    counts = result["counts"]
    assert counts["records"] == 3
    assert counts["singleton_fallback_batches"] == 2
    assert counts["singleton_fallback_fact_batches"] == 1
    assert counts["singleton_fallback_temporal_rescue_batches"] == 1
    assert counts["singleton_fallback_candidates"] == 2
    assert counts["singleton_fallback_candidates_requiring_defer"] == 2
    assert counts["singleton_fallback_candidates_not_deferred"] == 0
    assert counts["records_with_singleton_fallback"] == 2
    assert counts["records_added_by_singleton_fallback"] == 2
    assert counts["records_with_candidates"] == 2
    serialized = json.dumps(result)
    assert "workshop update" not in serialized
    assert "interview update" not in serialized
    assert "submit the form" not in serialized


def test_frontier_audit_reports_partial_per_expression_coverage(
    tmp_path: Path,
) -> None:
    cohort = tmp_path / "partial-cohort.jsonl"
    row = {
        "sample_id": "partial-options",
        "stratum": "important_ambiguous",
        "message_internal_at": "2027-05-01T10:00:00-07:00",
        "text": (
            "Possible dates are May 14, 2027 or May 15, 2027. I will confirm tomorrow."
        ),
    }
    cohort.write_text(json.dumps(row) + "\n", encoding="utf-8")
    os.chmod(cohort, 0o600)

    result = audit_module.audit_gmail_temporal_frontier(cohort)

    counts = result["counts"]
    assert counts["expressions"] == 3
    assert counts["expressions_with_candidates"] == 1
    assert counts["expressions_without_candidates"] == 2
    assert counts["records_with_candidates"] == 1
    assert counts["records_with_partial_expression_coverage"] == 1
    assert counts["records_with_no_expression_candidates"] == 0
    assert len(result["admitted_frontier_gaps"]) == 1
    gap = result["admitted_frontier_gaps"][0]
    assert gap["expressions"] == 3
    assert gap["expressions_without_candidates"] == 2
    assert "Possible dates" not in json.dumps(result)


def test_frontier_audit_rejects_non_private_or_duplicate_cohort(
    tmp_path: Path,
) -> None:
    cohort = tmp_path / "cohort.jsonl"
    _write_private_cohort(cohort)
    os.chmod(cohort, 0o644)
    with pytest.raises(
        audit_module.GmailTemporalFrontierAuditError,
        match="mode 0600",
    ):
        audit_module.audit_gmail_temporal_frontier(cohort)

    _write_private_cohort(cohort)
    duplicate = json.loads(cohort.read_text(encoding="utf-8").splitlines()[0])
    with cohort.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(duplicate) + "\n")
    with pytest.raises(
        audit_module.GmailTemporalFrontierAuditError,
        match="invalid required fields",
    ):
        audit_module.audit_gmail_temporal_frontier(cohort)
