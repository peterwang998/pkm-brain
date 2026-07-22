from __future__ import annotations

import json
import importlib.util
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest


def _load_evaluator():
    path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "evaluate_gmail_temporal_synthetic_benchmark.py"
    )
    spec = importlib.util.spec_from_file_location(
        "gmail_temporal_synthetic_benchmark_evaluator",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


evaluator = _load_evaluator()


def _load_script(name: str, filename: str):
    path = Path(__file__).resolve().parents[1] / "scripts" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


candidate_evaluator = _load_script(
    "gmail_temporal_candidate_gold_evaluator",
    "evaluate_gmail_temporal_candidate_gold.py",
)
benchmark_builder = _load_script(
    "gmail_temporal_synthetic_benchmark_builder",
    "build_gmail_temporal_synthetic_benchmark.py",
)


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in rows),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _run_provenance() -> dict[str, object]:
    source_hashes = candidate_evaluator._current_repo_module_hashes()
    source_hashes.update({"runner": "a" * 64, "base_runner": "b" * 64})
    return {
        "version": candidate_evaluator.RUN_MANIFEST_VERSION,
        "checkpoint_version": candidate_evaluator.EXPECTED_CHECKPOINT_VERSION,
        "protocol_fingerprint": "gtfproto_" + "c" * 64,
        "model": candidate_evaluator.EXPECTED_MODEL,
        "reasoning_effort": candidate_evaluator.EXPECTED_REASONING_EFFORT,
        "source_module_sha256": dict(sorted(source_hashes.items())),
    }


def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
    path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)


def _bound_manifest(
    provenance: dict[str, object],
    *,
    sample_path: Path,
    checkpoint_path: Path,
) -> dict[str, object]:
    return {
        **provenance,
        "evaluator_sha256": candidate_evaluator._sha256_file(
            candidate_evaluator._EVALUATOR_PATH
        ),
        "semantic_gold_sha256": candidate_evaluator._sha256_file(
            candidate_evaluator._SEMANTIC_GOLD_PATH
        ),
        "benchmark_builder_sha256": candidate_evaluator._sha256_file(
            candidate_evaluator._BENCHMARK_BUILDER_PATH
        ),
        "sample_sha256": candidate_evaluator._sha256_file(sample_path),
        "sample_record_count": len(candidate_evaluator._load_jsonl(sample_path)),
        "checkpoint_sha256": candidate_evaluator._sha256_file(checkpoint_path),
        "checkpoint_row_count": len(candidate_evaluator._load_jsonl(checkpoint_path)),
    }


def _candidate_fixture(tmp_path: Path):
    sample_path = tmp_path / "sample.jsonl"
    checkpoint_path = tmp_path / "checkpoint.jsonl"
    manifest_path = tmp_path / "run-manifest.json"
    benchmark_builder.build(sample_path)
    samples = candidate_evaluator._load_jsonl(sample_path)
    runtime_batches, candidates, pages = candidate_evaluator._runtime_batches(samples)
    units = candidate_evaluator._compile_gold(samples, candidates)
    provenance = _run_provenance()
    return (
        sample_path,
        checkpoint_path,
        manifest_path,
        samples,
        runtime_batches,
        candidates,
        pages,
        units,
        provenance,
    )


def _write_candidate_evidence(
    *,
    sample_path: Path,
    checkpoint_path: Path,
    manifest_path: Path,
    pages: dict[str, tuple[Any, Any]],
    verdicts: dict[str, str],
    provenance: dict[str, object],
    rows: list[dict[str, object]] | None = None,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    checkpoint_rows = rows or _checkpoint_rows(pages, verdicts, provenance)
    _write_jsonl(checkpoint_path, checkpoint_rows)
    manifest = _bound_manifest(
        provenance,
        sample_path=sample_path,
        checkpoint_path=checkpoint_path,
    )
    _write_manifest(manifest_path, manifest)
    return checkpoint_rows, manifest


def _checkpoint_rows(
    pages: dict[str, tuple[Any, Any]],
    verdicts: dict[str, str],
    manifest: dict[str, object],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for page_fingerprint, (runtime_batch, page) in pages.items():
        candidate_ids = [
            candidate_id
            for cluster in page.clusters
            for candidate_id in cluster.candidate_ids
        ]
        rows.append(
            {
                "version": manifest["checkpoint_version"],
                "sample_id": runtime_batch.sample_id,
                "source_sha256": runtime_batch.analysis.source_sha256,
                "protocol_fingerprint": manifest["protocol_fingerprint"],
                "source_module_sha256": manifest["source_module_sha256"],
                "plan_fingerprint": runtime_batch.plan_fingerprint,
                "page_case_id": candidate_evaluator._page_case_id(
                    runtime_batch,
                    page,
                ),
                "analysis_fingerprint": (runtime_batch.analysis.snapshot_fingerprint),
                "batch_fingerprint": (runtime_batch.batch.manifest.batch_fingerprint),
                "frontier_fingerprint": page.frontier_fingerprint,
                "page_fingerprint": page_fingerprint,
                "candidate_page_plan_fingerprint": (
                    runtime_batch.candidate_page_plan_fingerprint
                ),
                "candidate_page_payload_bytes": dict(
                    runtime_batch.candidate_page_payload_bytes
                )[page_fingerprint],
                "page_sequence": page.sequence,
                "batch_sequence": runtime_batch.batch.sequence,
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


def _oracle_verdicts(
    candidates: dict[str, Any],
    units: tuple[Any, ...],
) -> tuple[dict[str, str], dict[tuple[str, str, str], str]]:
    verdicts = {candidate_id: "unsupported" for candidate_id in candidates}
    selected_by_member: dict[tuple[str, str, str], str] = {}
    for unit in units:
        for member in unit.members:
            matches = candidate_evaluator._member_matches(member)
            assert matches
            selected = max(
                matches,
                key=lambda candidate_id: (matches[candidate_id], candidate_id),
            )
            verdicts[selected] = candidate_evaluator._member_expected_verdicts(member)[
                selected
            ]
            selected_by_member[member.key] = selected
    return verdicts, selected_by_member


def test_synthetic_gate_uses_embedded_record_gold_and_sol_proposal_labels(
    tmp_path: Path,
) -> None:
    sample_path = tmp_path / "sample.jsonl"
    selection_path = tmp_path / "selection.jsonl"
    labels_path = tmp_path / "labels.jsonl"
    _write_jsonl(
        sample_path,
        [
            {
                "sample_id": "useful",
                "gold": {
                    "expected_material": True,
                    "expected_filter": "should_admit",
                },
            },
            {
                "sample_id": "noise",
                "gold": {
                    "expected_material": False,
                    "expected_filter": "should_suppress",
                },
            },
        ],
    )
    _write_jsonl(
        selection_path,
        [
            {"sample_id": "useful", "associations": [{"id": "proposal"}]},
            {"sample_id": "noise", "associations": []},
        ],
    )
    _write_jsonl(
        labels_path,
        [
            {
                "sample_id": "useful",
                "materiality": "material_temporal",
                "filter_verdict": "should_admit",
                "supported_proposal_ids": ["p1"],
                "critical_error": "none",
            },
            {
                "sample_id": "noise",
                "materiality": "incidental_temporal",
                "filter_verdict": "should_suppress",
                "supported_proposal_ids": [],
                "critical_error": "none",
            },
        ],
    )

    result = evaluator.evaluate(sample_path, selection_path, labels_path)

    assert result["useful_record_recall"] == 1.0
    assert result["supported_proposal_precision"] == 1.0
    assert result["selected_noise_rate"] == 0.0
    assert result["critical_error_rate_per_proposal"] == 0.0
    assert result["synthetic_gate_passed"] is True


def test_synthetic_gate_rejects_mismatched_cohorts(tmp_path: Path) -> None:
    sample_path = tmp_path / "sample.jsonl"
    selection_path = tmp_path / "selection.jsonl"
    labels_path = tmp_path / "labels.jsonl"
    _write_jsonl(
        sample_path,
        [
            {
                "sample_id": "sample",
                "gold": {
                    "expected_material": True,
                    "expected_filter": "should_admit",
                },
            }
        ],
    )
    _write_jsonl(
        selection_path,
        [{"sample_id": "different", "associations": []}],
    )
    _write_jsonl(
        labels_path,
        [
            {
                "sample_id": "sample",
                "materiality": "material_temporal",
                "filter_verdict": "should_admit",
                "supported_proposal_ids": [],
                "critical_error": "none",
            }
        ],
    )

    with pytest.raises(
        evaluator.SyntheticBenchmarkError,
        match="cohorts do not match",
    ):
        evaluator.evaluate(sample_path, selection_path, labels_path)


def test_candidate_gold_oracle_covers_units_without_alias_inflation(
    tmp_path: Path,
) -> None:
    (
        sample_path,
        checkpoint_path,
        manifest_path,
        _,
        _,
        candidates,
        pages,
        units,
        provenance,
    ) = _candidate_fixture(tmp_path)
    verdicts, _ = _oracle_verdicts(candidates, units)
    assert all(
        alternative.expected_verdict == "uncertain"
        for unit in units
        for member in unit.members
        for alternative in member.alternatives
        if alternative.quality == "partial"
    )
    _write_candidate_evidence(
        sample_path=sample_path,
        checkpoint_path=checkpoint_path,
        manifest_path=manifest_path,
        pages=pages,
        verdicts=verdicts,
        provenance=provenance,
    )

    result = candidate_evaluator.evaluate(
        sample_path,
        checkpoint_path,
        manifest_path,
    )

    assert result["records"] == 39
    assert result["useful_records"] == 27
    assert result["semantic_units"] == 34
    assert result["semantic_members"] == 36
    assert result["frontier_candidates"] == 98
    assert result["frontier"]["any_unit_recall"] == 1.0
    assert result["frontier"]["required_member_recall"] == 1.0
    assert result["frontier"]["complete_unit_recall"] == 1.0
    assert result["frontier"]["exact_unit_recall"] == 32 / 34
    assert result["strict_supported_precision"] == 1.0
    assert result["recall_arm_precision"] == 1.0
    assert result["duplicate_alias_count"] == 0
    assert result["frontier_member_ratchet_regressions"] == 0
    assert result["frontier_upgraded_units"] == []
    assert result["candidate_gate_passed"] is True


def test_candidate_gold_rejects_checkpoint_without_exact_provenance_schema(
    tmp_path: Path,
) -> None:
    (
        sample_path,
        checkpoint_path,
        manifest_path,
        _,
        _,
        candidates,
        pages,
        units,
        provenance,
    ) = _candidate_fixture(tmp_path)
    verdicts, _ = _oracle_verdicts(candidates, units)
    rows = _checkpoint_rows(pages, verdicts, provenance)
    rows[0]["unbound_metadata"] = "old evaluator ignored this"
    _write_candidate_evidence(
        sample_path=sample_path,
        checkpoint_path=checkpoint_path,
        manifest_path=manifest_path,
        pages=pages,
        verdicts=verdicts,
        provenance=provenance,
        rows=rows,
    )

    with pytest.raises(candidate_evaluator.CandidateGoldError, match="schema is stale"):
        candidate_evaluator.evaluate(
            sample_path,
            checkpoint_path,
            manifest_path,
        )


def test_candidate_gold_rejects_manifest_for_different_source_modules(
    tmp_path: Path,
) -> None:
    (
        sample_path,
        checkpoint_path,
        manifest_path,
        _,
        _,
        candidates,
        pages,
        units,
        provenance,
    ) = _candidate_fixture(tmp_path)
    verdicts, _ = _oracle_verdicts(candidates, units)
    _, manifest = _write_candidate_evidence(
        sample_path=sample_path,
        checkpoint_path=checkpoint_path,
        manifest_path=manifest_path,
        pages=pages,
        verdicts=verdicts,
        provenance=provenance,
    )
    stale_manifest = deepcopy(manifest)
    source_hashes = dict(stale_manifest["source_module_sha256"])
    source_hashes["pkm_brain.gmail_temporal_frontier"] = "d" * 64
    stale_manifest["source_module_sha256"] = source_hashes
    _write_manifest(manifest_path, stale_manifest)

    with pytest.raises(
        candidate_evaluator.CandidateGoldError,
        match="do not match this checkout",
    ):
        candidate_evaluator.evaluate(
            sample_path,
            checkpoint_path,
            manifest_path,
        )


def test_candidate_gold_rejects_non_private_input_mode(tmp_path: Path) -> None:
    path = tmp_path / "world-readable.jsonl"
    _write_jsonl(path, [{"sample_id": "synthetic"}])
    path.chmod(0o644)

    with pytest.raises(candidate_evaluator.CandidateGoldError, match="mode 0600"):
        candidate_evaluator._load_jsonl(path)


def test_candidate_gold_rejects_unsupported_locator_occurrence(tmp_path: Path) -> None:
    (
        _,
        _,
        _,
        samples,
        _,
        candidates,
        _,
        _,
        _,
    ) = _candidate_fixture(tmp_path)
    malformed = deepcopy(samples)
    locator = malformed[0]["gold"]["semantic_units"][0]["members"][0]["alternatives"][
        0
    ]["locator"]
    locator["expression"]["occurrence"] = 1

    with pytest.raises(
        candidate_evaluator.CandidateGoldError,
        match="endpoint has invalid fields",
    ):
        candidate_evaluator._compile_gold(malformed, candidates)


def test_candidate_gold_required_member_and_complete_unit_gates_catch_old_false_pass(
    tmp_path: Path,
) -> None:
    (
        sample_path,
        checkpoint_path,
        manifest_path,
        _,
        _,
        candidates,
        pages,
        units,
        provenance,
    ) = _candidate_fixture(tmp_path)
    verdicts, selected_by_member = _oracle_verdicts(candidates, units)
    omitted_members = {
        ("syn_lifecycle_03", "hiring_interview_reschedule", "replacement_endpoint"),
        ("syn_ambiguous_04", "juniper_interview_date_options", "option_19"),
        ("syn_hard_01", "benefits_policy_effective", "effective_occurrence"),
        ("syn_hard_02", "registration_opens", "opening"),
    }
    for member_key in omitted_members:
        verdicts[selected_by_member[member_key]] = "unsupported"
    _write_candidate_evidence(
        sample_path=sample_path,
        checkpoint_path=checkpoint_path,
        manifest_path=manifest_path,
        pages=pages,
        verdicts=verdicts,
        provenance=provenance,
    )

    result = candidate_evaluator.evaluate(
        sample_path,
        checkpoint_path,
        manifest_path,
    )

    assert result["review"]["any_unit_recall"] >= 0.90
    assert result["gates"]["end_to_end_any_recall"] is False
    assert result["review"]["required_member_recall"] < 0.90
    assert result["review"]["complete_unit_recall"] < 0.90
    assert result["gates"]["end_to_end_required_member_recall"] is False
    assert result["gates"]["end_to_end_complete_unit_recall"] is False
    assert result["candidate_gate_passed"] is False


def test_candidate_gold_rejects_uncertain_default_negative_even_when_precision_passes(
    tmp_path: Path,
) -> None:
    (
        sample_path,
        checkpoint_path,
        manifest_path,
        _,
        _,
        candidates,
        pages,
        units,
        provenance,
    ) = _candidate_fixture(tmp_path)
    verdicts, _ = _oracle_verdicts(candidates, units)
    positive_ids = {
        candidate_id
        for unit in units
        for member in unit.members
        for candidate_id in candidate_evaluator._member_matches(member)
    }
    default_negative = next(iter(set(candidates) - positive_ids))
    verdicts[default_negative] = "uncertain"
    _write_candidate_evidence(
        sample_path=sample_path,
        checkpoint_path=checkpoint_path,
        manifest_path=manifest_path,
        pages=pages,
        verdicts=verdicts,
        provenance=provenance,
    )

    result = candidate_evaluator.evaluate(
        sample_path,
        checkpoint_path,
        manifest_path,
    )

    assert result["recall_arm_precision"] >= 0.90
    assert result["default_negative_supported"] == 0
    assert result["default_negative_accepted"] == 1
    assert result["gates"]["no_default_negative_supported"] is True
    assert result["gates"]["no_default_negative_accepted"] is False
    assert result["candidate_gate_passed"] is False


def test_candidate_gold_rejects_nearly_all_supported_truth_as_uncertain(
    tmp_path: Path,
) -> None:
    (
        sample_path,
        checkpoint_path,
        manifest_path,
        _,
        _,
        candidates,
        pages,
        units,
        provenance,
    ) = _candidate_fixture(tmp_path)
    verdicts, selected_by_member = _oracle_verdicts(candidates, units)
    kept_supported = False
    expected_supported_members = 0
    for unit in units:
        for member in unit.members:
            if member.expected_verdict != "supported":
                continue
            expected_supported_members += 1
            selected = selected_by_member[member.key]
            if kept_supported:
                verdicts[selected] = "uncertain"
            else:
                kept_supported = True
    _write_candidate_evidence(
        sample_path=sample_path,
        checkpoint_path=checkpoint_path,
        manifest_path=manifest_path,
        pages=pages,
        verdicts=verdicts,
        provenance=provenance,
    )

    result = candidate_evaluator.evaluate(
        sample_path,
        checkpoint_path,
        manifest_path,
    )

    assert expected_supported_members == 32
    assert result["review"]["required_member_recall"] == 1.0
    assert result["strict_supported_precision"] == 1.0
    assert result["supported_required_member_recall"] == 1 / 32
    assert result["supported_to_uncertain_rate"] == 31 / 32
    assert result["gates"]["supported_required_member_recall"] is False
    assert result["gates"]["supported_to_uncertain_rate"] is False
    assert result["candidate_gate_passed"] is False


def test_candidate_gold_allows_bounded_supported_to_uncertain_underconfidence(
    tmp_path: Path,
) -> None:
    (
        sample_path,
        checkpoint_path,
        manifest_path,
        _,
        _,
        candidates,
        pages,
        units,
        provenance,
    ) = _candidate_fixture(tmp_path)
    verdicts, selected_by_member = _oracle_verdicts(candidates, units)
    demoted = 0
    for unit in units:
        for member in unit.members:
            if member.expected_verdict == "supported" and demoted < 6:
                verdicts[selected_by_member[member.key]] = "uncertain"
                demoted += 1
    _write_candidate_evidence(
        sample_path=sample_path,
        checkpoint_path=checkpoint_path,
        manifest_path=manifest_path,
        pages=pages,
        verdicts=verdicts,
        provenance=provenance,
    )

    result = candidate_evaluator.evaluate(
        sample_path,
        checkpoint_path,
        manifest_path,
    )

    assert demoted == 6
    assert result["supported_required_member_recall"] == 26 / 32
    assert result["supported_to_uncertain_rate"] == 6 / 32
    assert result["gates"]["supported_required_member_recall"] is True
    assert result["gates"]["supported_to_uncertain_rate"] is True
    assert result["candidate_gate_passed"] is True


def test_candidate_gold_rejects_expected_uncertain_promoted_to_supported(
    tmp_path: Path,
) -> None:
    (
        sample_path,
        checkpoint_path,
        manifest_path,
        _,
        _,
        candidates,
        pages,
        units,
        provenance,
    ) = _candidate_fixture(tmp_path)
    verdicts, selected_by_member = _oracle_verdicts(candidates, units)
    uncertain_member = next(
        member
        for unit in units
        for member in unit.members
        if member.expected_verdict == "uncertain"
    )
    verdicts[selected_by_member[uncertain_member.key]] = "supported"
    _write_candidate_evidence(
        sample_path=sample_path,
        checkpoint_path=checkpoint_path,
        manifest_path=manifest_path,
        pages=pages,
        verdicts=verdicts,
        provenance=provenance,
    )

    result = candidate_evaluator.evaluate(
        sample_path,
        checkpoint_path,
        manifest_path,
    )

    assert result["strict_supported_precision"] >= 0.95
    assert result["supported_overclaim_count"] == 1
    assert result["critical_calibration_error_count"] == 1
    assert result["gates"]["no_supported_overclaims"] is False
    assert result["gates"]["no_critical_calibration_errors"] is False
    assert result["candidate_gate_passed"] is False


@pytest.mark.parametrize(
    ("tamper", "error"),
    [
        ("evaluator_sha256", "evidence artifacts do not match"),
        ("semantic_gold_sha256", "evidence artifacts do not match"),
        ("benchmark_builder_sha256", "evidence artifacts do not match"),
        ("sample_bytes", "evidence artifacts do not match"),
        ("checkpoint_bytes", "evidence artifacts do not match"),
        ("sample_record_count", "cohort counts do not match"),
        ("checkpoint_row_count", "cohort counts do not match"),
    ],
)
def test_candidate_gold_manifest_binds_code_and_cohort_artifacts(
    tmp_path: Path,
    tamper: str,
    error: str,
) -> None:
    (
        sample_path,
        checkpoint_path,
        manifest_path,
        _,
        _,
        candidates,
        pages,
        units,
        provenance,
    ) = _candidate_fixture(tmp_path)
    verdicts, _ = _oracle_verdicts(candidates, units)
    _, manifest = _write_candidate_evidence(
        sample_path=sample_path,
        checkpoint_path=checkpoint_path,
        manifest_path=manifest_path,
        pages=pages,
        verdicts=verdicts,
        provenance=provenance,
    )
    if tamper.endswith("_sha256"):
        manifest[tamper] = "f" * 64
        _write_manifest(manifest_path, manifest)
    elif tamper == "sample_bytes":
        sample_path.write_bytes(sample_path.read_bytes() + b"\n")
        sample_path.chmod(0o600)
    elif tamper == "checkpoint_bytes":
        checkpoint_path.write_bytes(checkpoint_path.read_bytes() + b"\n")
        checkpoint_path.chmod(0o600)
    else:
        manifest[tamper] = int(manifest[tamper]) - 1
        _write_manifest(manifest_path, manifest)

    with pytest.raises(candidate_evaluator.CandidateGoldError, match=error):
        candidate_evaluator.evaluate(
            sample_path,
            checkpoint_path,
            manifest_path,
        )
