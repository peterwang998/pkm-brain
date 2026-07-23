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


def _prevalidated_result(
    sample_path: Path,
    verdicts: dict[str, str],
) -> dict[str, Any]:
    return candidate_evaluator.evaluate(
        sample_path,
        None,
        None,
        prevalidated_verdict_maps=(verdicts, verdicts),
        provenance_override={"single_run": False, "test_fixture": True},
    )


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
    assert result["frontier_candidates"] == 96
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


def test_artifact_scoring_collapses_same_signature_sidecar_aliases(
    tmp_path: Path,
) -> None:
    (
        sample_path,
        _,
        _,
        _,
        _,
        candidates,
        _,
        units,
        _,
    ) = _candidate_fixture(tmp_path)
    oracle, selected = _oracle_verdicts(candidates, units)
    completion = next(
        member
        for unit in units
        for member in unit.members
        if member.key == ("syn_lifecycle_06", "review_meeting_completion", "completion")
    )
    partial_aliases = [
        candidate_id
        for candidate_id, quality in candidate_evaluator._member_matches(
            completion
        ).items()
        if quality == 0.5
    ]
    assert len(partial_aliases) == 2
    assert (
        len(
            {
                candidate_evaluator._artifact_hypothesis(candidates[candidate_id])
                for candidate_id in partial_aliases
            }
        )
        == 1
    )

    one_alias = dict(oracle)
    all_aliases = dict(oracle)
    one_alias[selected[completion.key]] = "unsupported"
    all_aliases[selected[completion.key]] = "unsupported"
    one_alias[partial_aliases[0]] = "uncertain"
    for candidate_id in partial_aliases:
        all_aliases[candidate_id] = "uncertain"

    one_result = _prevalidated_result(sample_path, one_alias)
    all_result = _prevalidated_result(sample_path, all_aliases)

    for key in (
        "production_artifacts",
        "uncertainty_sidecars",
        "uncertainty_hypotheses",
        "matched_artifacts",
        "redundant_artifacts",
        "unmatched_artifacts",
        "effective_artifact_precision",
        "effective_member_recall",
    ):
        assert all_result[key] == one_result[key]
    assert all_result["uncertainty_hypotheses"] == 5
    assert all_result["uncertainty_hypothesis_purity"] == 1.0


def test_artifact_scoring_collapsed_alias_uses_least_specific_grounding(
    tmp_path: Path,
) -> None:
    (
        sample_path,
        _,
        _,
        _,
        _,
        candidates,
        _,
        units,
        _,
    ) = _candidate_fixture(tmp_path)
    oracle, selected = _oracle_verdicts(candidates, units)
    member = next(
        member
        for unit in units
        for member in unit.members
        if member.key == ("syn_clear_05", "cedar_forum_schedule", "schedule")
    )
    matches = candidate_evaluator._member_matches(member)
    aliases_by_hypothesis: dict[tuple[Any, ...], list[str]] = {}
    for candidate_id in matches:
        hypothesis = candidate_evaluator._artifact_hypothesis(candidates[candidate_id])
        aliases_by_hypothesis.setdefault(hypothesis, []).append(candidate_id)
    mixed_aliases = next(
        candidate_ids
        for candidate_ids in aliases_by_hypothesis.values()
        if {matches[candidate_id] for candidate_id in candidate_ids} == {0.5, 1.0}
    )
    exact = next(
        candidate_id for candidate_id in mixed_aliases if matches[candidate_id] == 1.0
    )
    partial = next(
        candidate_id for candidate_id in mixed_aliases if matches[candidate_id] == 0.5
    )

    partial_only = dict(oracle)
    partial_only[selected[member.key]] = "unsupported"
    partial_only[partial] = "uncertain"
    both_aliases = dict(partial_only)
    both_aliases[exact] = "uncertain"

    partial_result = _prevalidated_result(sample_path, partial_only)
    both_result = _prevalidated_result(sample_path, both_aliases)
    partial_quality = {
        tuple(item["member_key"]): item["quality"]
        for item in partial_result["matched_effective_members"]
    }
    both_quality = {
        tuple(item["member_key"]): item["quality"]
        for item in both_result["matched_effective_members"]
    }

    assert partial_quality[member.key] == 0.5
    assert both_quality[member.key] == 0.5
    assert (
        both_result["review"]["exact_units"] == partial_result["review"]["exact_units"]
    )
    assert (
        both_result["uncertainty_hypotheses"]
        == partial_result["uncertainty_hypotheses"]
    )


def test_artifact_scoring_rejects_unmatched_alias_in_collapsed_hypothesis(
    tmp_path: Path,
) -> None:
    (
        _,
        _,
        _,
        _,
        _,
        candidates,
        _,
        units,
        _,
    ) = _candidate_fixture(tmp_path)
    member = next(
        member
        for unit in units
        for member in unit.members
        if member.key == ("syn_clear_05", "cedar_forum_schedule", "schedule")
    )
    matches = candidate_evaluator._member_matches(member)
    aliases_by_hypothesis: dict[tuple[Any, ...], list[str]] = {}
    for candidate_id in matches:
        hypothesis = candidate_evaluator._artifact_hypothesis(candidates[candidate_id])
        aliases_by_hypothesis.setdefault(hypothesis, []).append(candidate_id)
    mixed_aliases = next(
        candidate_ids
        for candidate_ids in aliases_by_hypothesis.values()
        if {matches[candidate_id] for candidate_id in candidate_ids} == {0.5, 1.0}
    )
    exact = next(
        candidate_id for candidate_id in mixed_aliases if matches[candidate_id] == 1.0
    )
    partial = next(
        candidate_id for candidate_id in mixed_aliases if matches[candidate_id] == 0.5
    )
    exact_only_member = candidate_evaluator.GoldMember(
        key=member.key,
        expected_verdict=member.expected_verdict,
        baseline_grade=member.baseline_grade,
        alternatives=(
            candidate_evaluator.GoldAlternative(
                quality="exact",
                expected_verdict=member.expected_verdict,
                candidate_ids=frozenset({exact}),
            ),
        ),
    )
    artifact = candidate_evaluator.ProductionArtifact(
        artifact_id="uncertainty:test",
        kind="uncertainty_sidecar",
        candidate_ids=(exact, partial),
        hypotheses=(candidate_evaluator._artifact_hypothesis(candidates[exact]),),
    )

    scores = candidate_evaluator._match_production_artifacts(
        (artifact,),
        (
            candidate_evaluator.GoldUnit(
                key=(member.key[0], member.key[1]),
                baseline_grade="exact",
                members=(exact_only_member,),
            ),
        ),
        candidates,
    )

    assert scores["matched_artifact_ids"] == set()
    assert scores["invalid_artifact_ids"] == {artifact.artifact_id}
    assert scores["unmatched_hypothesis_count"] == 1


def test_artifact_scoring_uses_least_specific_pure_sidecar_hypothesis(
    tmp_path: Path,
) -> None:
    (
        sample_path,
        _,
        _,
        _,
        _,
        candidates,
        _,
        units,
        _,
    ) = _candidate_fixture(tmp_path)
    oracle, selected = _oracle_verdicts(candidates, units)
    member = next(
        member
        for unit in units
        for member in unit.members
        if member.key
        == ("syn_mixed_01", "atlas_interview_current_schedule", "schedule")
    )
    matches = candidate_evaluator._member_matches(member)
    expected = candidate_evaluator._member_expected_verdicts(member)
    exact = next(
        candidate_id
        for candidate_id, quality in matches.items()
        if quality == 1.0 and expected[candidate_id] == "supported"
    )
    partial = next(
        candidate_id for candidate_id, quality in matches.items() if quality == 0.5
    )
    assert candidate_evaluator._artifact_hypothesis(
        candidates[exact]
    ) != candidate_evaluator._artifact_hypothesis(candidates[partial])
    verdicts = dict(oracle)
    verdicts[selected[member.key]] = "unsupported"
    verdicts[exact] = "uncertain"
    verdicts[partial] = "uncertain"

    result = _prevalidated_result(sample_path, verdicts)
    matched = {
        tuple(item["member_key"]): item["quality"]
        for item in result["matched_effective_members"]
    }

    assert matched[member.key] == 0.5
    assert result["impure_uncertainty_sidecars"] == 0


def test_artifact_scoring_rejects_cross_member_sidecar(
    tmp_path: Path,
) -> None:
    (
        sample_path,
        _,
        _,
        _,
        runtime_batches,
        candidates,
        _,
        units,
        _,
    ) = _candidate_fixture(tmp_path)
    oracle, selected = _oracle_verdicts(candidates, units)
    occurrence = next(
        member
        for unit in units
        for member in unit.members
        if member.key == ("syn_lifecycle_06", "review_meeting_occurrence", "occurrence")
    )
    completion = next(
        member
        for unit in units
        for member in unit.members
        if member.key == ("syn_lifecycle_06", "review_meeting_completion", "completion")
    )
    candidate_to_cluster = {
        candidate_id: cluster.cluster_id
        for runtime_batch in runtime_batches
        for page in runtime_batch.pages
        for cluster in page.clusters
        for candidate_id in cluster.candidate_ids
    }
    partial_completion = next(
        candidate_id
        for candidate_id, quality in candidate_evaluator._member_matches(
            completion
        ).items()
        if quality == 0.5
    )
    occurrence_candidate = next(
        candidate_id
        for candidate_id in candidate_evaluator._member_matches(occurrence)
        if candidate_to_cluster[candidate_id]
        == candidate_to_cluster[partial_completion]
    )
    verdicts = dict(oracle)
    verdicts[selected[occurrence.key]] = "unsupported"
    verdicts[selected[completion.key]] = "unsupported"
    verdicts[occurrence_candidate] = "uncertain"
    verdicts[partial_completion] = "uncertain"

    result = _prevalidated_result(sample_path, verdicts)

    assert result["impure_uncertainty_sidecars"] == 1
    assert result["unmatched_artifacts"] == 1
    assert result["effective_member_recall"] == 34 / 36
    assert result["missed_members"] == 2
    assert result["gates"]["uncertainty_hypothesis_purity"] is False
    assert result["candidate_gate_passed"] is False


def test_artifact_scoring_rejects_unmatched_sidecar_hypothesis(
    tmp_path: Path,
) -> None:
    (
        sample_path,
        _,
        _,
        _,
        runtime_batches,
        candidates,
        _,
        units,
        _,
    ) = _candidate_fixture(tmp_path)
    oracle, selected = _oracle_verdicts(candidates, units)
    member = next(
        member
        for unit in units
        for member in unit.members
        if member.key == ("syn_clear_01", "nimbus_interview_schedule", "schedule")
    )
    candidate_to_cluster = {
        candidate_id: cluster.cluster_id
        for runtime_batch in runtime_batches
        for page in runtime_batch.pages
        for cluster in page.clusters
        for candidate_id in cluster.candidate_ids
    }
    gold_ids = set(candidate_evaluator._member_matches(member))
    selected_id = selected[member.key]
    unmatched = next(
        candidate_id
        for candidate_id in candidates
        if candidate_id not in gold_ids
        and candidate_to_cluster[candidate_id] == candidate_to_cluster[selected_id]
    )
    verdicts = dict(oracle)
    verdicts[selected_id] = "uncertain"
    verdicts[unmatched] = "uncertain"

    result = _prevalidated_result(sample_path, verdicts)

    assert result["impure_uncertainty_sidecars"] == 1
    assert result["unmatched_uncertainty_hypotheses"] == 1
    assert result["unmatched_artifacts"] == 1
    assert ":".join(member.key) in result["missed_member_keys"]
    assert result["gates"]["uncertainty_hypothesis_purity"] is False


def test_artifact_scoring_counts_second_supported_citation_as_redundant(
    tmp_path: Path,
) -> None:
    (
        sample_path,
        _,
        _,
        _,
        _,
        candidates,
        _,
        units,
        _,
    ) = _candidate_fixture(tmp_path)
    oracle, _ = _oracle_verdicts(candidates, units)
    member = next(
        member
        for unit in units
        for member in unit.members
        if member.key == ("syn_lifecycle_06", "review_meeting_occurrence", "occurrence")
    )
    exact_ids = [
        candidate_id
        for candidate_id, quality in candidate_evaluator._member_matches(member).items()
        if quality == 1.0
    ]
    assert len(exact_ids) == 2
    verdicts = dict(oracle)
    for candidate_id in exact_ids:
        verdicts[candidate_id] = "supported"

    result = _prevalidated_result(sample_path, verdicts)

    assert result["supported_redundant_artifacts"] == 1
    assert result["redundant_artifacts"] == 1
    assert result["supported_artifact_precision"] == 32 / 33
    assert result["gates"]["no_redundant_artifacts"] is False
    assert result["candidate_gate_passed"] is False


def test_artifact_scoring_exact_supported_beats_partial_sidecar_once(
    tmp_path: Path,
) -> None:
    (
        sample_path,
        _,
        _,
        _,
        _,
        candidates,
        _,
        units,
        _,
    ) = _candidate_fixture(tmp_path)
    oracle, selected = _oracle_verdicts(candidates, units)
    occurrence = next(
        member
        for unit in units
        for member in unit.members
        if member.key == ("syn_lifecycle_06", "review_meeting_occurrence", "occurrence")
    )
    completion = next(
        member
        for unit in units
        for member in unit.members
        if member.key == ("syn_lifecycle_06", "review_meeting_completion", "completion")
    )
    partial_aliases = [
        candidate_id
        for candidate_id, quality in candidate_evaluator._member_matches(
            completion
        ).items()
        if quality == 0.5
    ]
    verdicts = dict(oracle)
    verdicts[selected[occurrence.key]] = "unsupported"
    for candidate_id in partial_aliases:
        verdicts[candidate_id] = "uncertain"
    # Mirror the v17 artifact mix exactly: 30 citations plus six singleton
    # sidecars, while leaving the extra completion artifact and missing
    # occurrence unchanged.
    verdicts[selected[("syn_clear_01", "nimbus_interview_schedule", "schedule")]] = (
        "uncertain"
    )

    result = _prevalidated_result(sample_path, verdicts)
    reversed_result = _prevalidated_result(
        sample_path,
        dict(reversed(tuple(verdicts.items()))),
    )

    assert result["production_artifacts"] == 36
    assert result["supported_artifacts"] == 30
    assert result["uncertainty_sidecars"] == 6
    assert result["uncertainty_hypotheses"] == 6
    assert result["matched_artifacts"] == 35
    assert result["redundant_artifacts"] == 1
    assert result["unmatched_artifacts"] == 0
    assert result["effective_artifact_precision"] == 35 / 36
    assert result["effective_member_recall"] == 35 / 36
    assert result["missed_member_keys"] == [
        "syn_lifecycle_06:review_meeting_occurrence:occurrence"
    ]
    assert result["gates"]["no_redundant_artifacts"] is False
    assert result["gates"]["no_duplicate_aliases"] is False
    assert result["candidate_gate_passed"] is False
    for key in (
        "matched_artifacts",
        "redundant_artifacts",
        "effective_artifact_precision",
        "effective_member_recall",
        "missed_member_keys",
    ):
        assert reversed_result[key] == result[key]


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
    material_sample_ids = {
        str(sample["sample_id"])
        for sample in candidate_evaluator._load_jsonl(sample_path)
        if sample["gold"]["expected_material"] is True
    }
    default_negative = next(
        candidate_id
        for candidate_id in set(candidates) - positive_ids
        if candidates[candidate_id].sample_id in material_sample_ids
    )
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


def test_candidate_gold_rejects_underconfidence_below_personal_release_recall(
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
    assert result["gates"]["supported_required_member_recall"] is False
    assert result["gates"]["supported_to_uncertain_rate"] is True
    assert result["candidate_gate_passed"] is False


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


def test_candidate_gold_scores_effective_lifecycle_calibration(
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
    member = next(
        member
        for unit in units
        for member in unit.members
        if member.key == ("syn_dense_01", "dense_beta_workshop", "schedule")
    )
    exact_candidate_id = selected_by_member[member.key]
    expected_by_candidate = candidate_evaluator._member_expected_verdicts(member)
    base_candidate_id = next(
        candidate_id
        for candidate_id in candidate_evaluator._member_matches(member)
        if expected_by_candidate[candidate_id] == "uncertain"
        and candidates[candidate_id].candidate.lifecycle == "none"
        and candidates[candidate_id].subject_surface.lower() == "workshop"
    )
    verdicts[exact_candidate_id] = "unsupported"
    verdicts[base_candidate_id] = "supported"
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

    assert result["raw_supported_overclaim_count"] == 1
    assert result["raw_critical_calibration_error_count"] == 1
    assert result["effective_verdict_change_count"] == 1
    assert result["effective_verdict_change_kinds"] == ["supported_to_uncertain"]
    assert result["supported_overclaim_count"] == 0
    assert result["critical_calibration_error_count"] == 0
    assert result["raw_supported_candidates"] == result["supported_candidates"] + 1
    assert result["review"]["required_member_recall"] == 1.0
    assert result["candidate_gate_passed"] is True


def test_candidate_gold_cli_prints_only_aggregate_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (
        sample_path,
        checkpoint_path,
        manifest_path,
        samples,
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
    critical_candidate_id = selected_by_member[uncertain_member.key]
    verdicts[critical_candidate_id] = "supported"
    _write_candidate_evidence(
        sample_path=sample_path,
        checkpoint_path=checkpoint_path,
        manifest_path=manifest_path,
        pages=pages,
        verdicts=verdicts,
        provenance=provenance,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate_gmail_temporal_candidate_gold.py",
            str(sample_path),
            str(checkpoint_path),
            str(manifest_path),
        ],
    )

    candidate_evaluator.main()

    printed = json.loads(capsys.readouterr().out)
    serialized = json.dumps(printed, ensure_ascii=False, sort_keys=True)

    def contains_sequence(value: Any) -> bool:
        if isinstance(value, dict):
            return any(contains_sequence(item) for item in value.values())
        return isinstance(value, list)

    assert printed["private_content_printed"] is False
    assert printed["critical_calibration_error_count"] == 1
    assert "critical_calibration_error_candidates" not in printed
    assert "matched_effective_member_keys" not in printed
    assert "matched_effective_members" not in printed
    assert contains_sequence(printed) is False
    assert critical_candidate_id not in serialized
    for sample in samples:
        assert json.dumps(sample["sample_id"])[1:-1] not in serialized
        assert json.dumps(sample["text"])[1:-1] not in serialized
        for unit in sample["gold"]["semantic_units"]:
            assert json.dumps(unit["unit_id"])[1:-1] not in serialized
            for member in unit["members"]:
                assert json.dumps(member["member_id"])[1:-1] not in serialized


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
