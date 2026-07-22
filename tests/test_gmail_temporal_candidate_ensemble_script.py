from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest


def _load_script(module_name: str, filename: str):
    path = Path(__file__).resolve().parents[1] / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ensemble = _load_script(
    "gmail_temporal_candidate_ensemble_evaluator_test",
    "evaluate_gmail_temporal_candidate_ensemble.py",
)
candidate_gold = ensemble.candidate_gold
benchmark_builder = _load_script(
    "gmail_temporal_candidate_ensemble_benchmark_builder_test",
    "build_gmail_temporal_synthetic_benchmark.py",
)
manifest_builder = _load_script(
    "gmail_temporal_candidate_ensemble_manifest_builder_test",
    "build_gmail_temporal_candidate_run_manifest.py",
)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _provenance(protocol: str = "gtfproto_" + "c" * 64) -> dict[str, Any]:
    source_hashes = candidate_gold._current_repo_module_hashes()
    source_hashes.update({"runner": "a" * 64, "base_runner": "b" * 64})
    return {
        "checkpoint_version": candidate_gold.EXPECTED_CHECKPOINT_VERSION,
        "protocol_fingerprint": protocol,
        "source_module_sha256": dict(sorted(source_hashes.items())),
    }


def _checkpoint_rows(
    pages: dict[str, tuple[Any, Any]],
    verdicts: dict[str, str],
    provenance: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page_fingerprint, (runtime_batch, page) in pages.items():
        candidate_ids = [
            candidate_id
            for cluster in page.clusters
            for candidate_id in cluster.candidate_ids
        ]
        rows.append(
            {
                "version": provenance["checkpoint_version"],
                "sample_id": runtime_batch.sample_id,
                "source_sha256": runtime_batch.analysis.source_sha256,
                "protocol_fingerprint": provenance["protocol_fingerprint"],
                "source_module_sha256": provenance["source_module_sha256"],
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


def _oracle_verdicts(
    candidates: dict[str, Any],
    units: tuple[Any, ...],
) -> tuple[dict[str, str], dict[tuple[str, str, str], str]]:
    verdicts = {candidate_id: "unsupported" for candidate_id in candidates}
    selected_by_member: dict[tuple[str, str, str], str] = {}
    for unit in units:
        for member in unit.members:
            matches = candidate_gold._member_matches(member)
            selected = max(
                matches,
                key=lambda candidate_id: (matches[candidate_id], candidate_id),
            )
            verdicts[selected] = candidate_gold._member_expected_verdicts(member)[
                selected
            ]
            selected_by_member[member.key] = selected
    return verdicts, selected_by_member


def _fixture(tmp_path: Path) -> dict[str, Any]:
    sample = tmp_path / "sample.jsonl"
    benchmark_builder.build(sample)
    samples = candidate_gold._load_jsonl(sample)
    runtime_batches, candidates, pages = candidate_gold._runtime_batches(samples)
    units = candidate_gold._compile_gold(samples, candidates)
    oracle, selected = _oracle_verdicts(candidates, units)
    positive_ids = {
        candidate_id
        for unit in units
        for member in unit.members
        for candidate_id in candidate_gold._member_matches(member)
    }
    candidate_to_cluster = {
        candidate_id: cluster.cluster_id
        for runtime_batch in runtime_batches
        for page in runtime_batch.pages
        for cluster in page.clusters
        for candidate_id in cluster.candidate_ids
    }
    accepted_clusters = {
        candidate_to_cluster[candidate_id]
        for candidate_id, verdict in oracle.items()
        if verdict != "unsupported"
    }
    negatives = sorted(
        candidate_id
        for candidate_id in set(candidates) - positive_ids
        if candidate_to_cluster[candidate_id] in accepted_clusters
    )
    assert len(negatives) >= 2
    truth_id = selected[("syn_clear_01", "nimbus_interview_schedule", "schedule")]
    verdict_runs = [dict(oracle) for _ in range(3)]
    verdict_runs[0][negatives[0]] = "uncertain"
    verdict_runs[1][negatives[1]] = "uncertain"
    verdict_runs[2][truth_id] = "unsupported"

    pairs: list[tuple[Path, Path]] = []
    provenance = _provenance()
    for index, verdicts in enumerate(verdict_runs, start=1):
        checkpoint = tmp_path / f"checkpoint-{index}.jsonl"
        manifest = tmp_path / f"manifest-{index}.json"
        _write_jsonl(checkpoint, _checkpoint_rows(pages, verdicts, provenance))
        manifest_builder.build_run_manifest(sample, checkpoint, manifest)
        pairs.append((checkpoint, manifest))
    return {
        "sample": sample,
        "pairs": tuple(pairs),
        "candidate_count": len(candidates),
        "samples": samples,
        "runtime_batches": runtime_batches,
        "candidates": candidates,
        "pages": pages,
        "units": units,
        "oracle": oracle,
        "selected": selected,
        "provenance": provenance,
    }


def _replace_run_evidence(
    files: dict[str, Any],
    tmp_path: Path,
    verdict_runs: list[dict[str, str]],
    *,
    prefix: str,
) -> tuple[tuple[Path, Path], tuple[Path, Path], tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    for index, verdicts in enumerate(verdict_runs, start=1):
        checkpoint = tmp_path / f"{prefix}-checkpoint-{index}.jsonl"
        manifest = tmp_path / f"{prefix}-manifest-{index}.json"
        _write_jsonl(
            checkpoint,
            _checkpoint_rows(files["pages"], verdicts, files["provenance"]),
        )
        manifest_builder.build_run_manifest(files["sample"], checkpoint, manifest)
        pairs.append((checkpoint, manifest))
    return tuple(pairs)  # type: ignore[return-value]


def _member(files: dict[str, Any], key: tuple[str, str, str]) -> Any:
    return next(
        member
        for unit in files["units"]
        for member in unit.members
        if member.key == key
    )


def test_ensemble_rejects_one_vote_noise_and_recovers_two_vote_truth(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path)

    result = ensemble.evaluate_ensemble(files["sample"], files["pairs"])

    assert result["version"] == ensemble.ENSEMBLE_EVALUATION_VERSION
    assert result["ensemble_provenance"]["single_run"] is False
    assert result["ensemble_provenance"]["component_run_count"] == 3
    assert len(result["stability"]["all_candidate_agreement_diagnostic_only"]) == 3
    assert result["consensus"]["candidate_count"] == files["candidate_count"]
    assert result["consensus"]["positive_vote_histogram"]["1"] == 2
    assert result["gold_metrics"]["review"]["required_member_recall"] == 1.0
    assert result["gold_metrics"]["strict_supported_precision"] == 1.0
    assert result["gold_metrics"]["recall_arm_precision"] == 1.0
    assert result["gold_metrics"]["default_negative_accepted"] == 0
    assert result["gold_metrics"]["duplicate_alias_count"] == 0
    assert result["gold_metrics"]["selected_noise_records"] == 0
    assert result["release_gates"] == {
        "gold_candidate_gate": True,
        "accepted_parent_cluster_stability": True,
        "gold_semantic_member_stability": True,
        "fresh_provenance": True,
        "distinct_component_evidence_paths": True,
    }
    assert result["ensemble_gate_passed"] is True
    assert result["private_content_printed"] is False
    assert result["external_calls"] == 0


def test_duplicate_artifacts_are_diagnostic_not_invocation_proof(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path)
    first_checkpoint, first_manifest = files["pairs"][0]
    copied_checkpoint = tmp_path / "copied-checkpoint.jsonl"
    copied_manifest = tmp_path / "copied-manifest.json"
    copied_checkpoint.write_bytes(first_checkpoint.read_bytes())
    copied_manifest.write_bytes(first_manifest.read_bytes())
    copied_checkpoint.chmod(0o600)
    copied_manifest.chmod(0o600)
    pairs = (files["pairs"][0], (copied_checkpoint, copied_manifest), files["pairs"][2])

    result = ensemble.evaluate_ensemble(files["sample"], pairs)

    provenance = result["ensemble_provenance"]
    assert provenance["independent_invocations_verified"] is False
    assert provenance["distinct_checkpoint_artifact_count"] == 2
    assert provenance["distinct_manifest_artifact_count"] == 2


def test_repeated_component_paths_cannot_pass_the_release_gate(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path)
    clean_pairs = _replace_run_evidence(
        files,
        tmp_path,
        [dict(files["oracle"]) for _ in range(3)],
        prefix="clean-path-gate",
    )
    repeated_pair = clean_pairs[0]

    result = ensemble.evaluate_ensemble(
        files["sample"],
        (repeated_pair, repeated_pair, repeated_pair),
    )

    assert result["release_gates"]["gold_candidate_gate"] is True
    assert result["release_gates"]["accepted_parent_cluster_stability"] is True
    assert result["release_gates"]["gold_semantic_member_stability"] is True
    assert result["release_gates"]["fresh_provenance"] is True
    assert result["release_gates"]["distinct_component_evidence_paths"] is False
    assert result["ensemble_provenance"]["distinct_component_evidence_path_count"] == 2
    assert (
        result["ensemble_provenance"]["all_component_evidence_paths_distinct"] is False
    )
    assert result["ensemble_provenance"]["independent_invocations_verified"] is False
    assert result["ensemble_gate_passed"] is False


def test_ensemble_rejects_non_private_manifest(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    files["pairs"][1][1].chmod(0o644)

    with pytest.raises(ensemble.CandidateEnsembleError, match="unsafe"):
        ensemble.evaluate_ensemble(files["sample"], files["pairs"])


def test_ensemble_rejects_artifact_changed_after_manifest(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    checkpoint = files["pairs"][2][0]
    checkpoint.write_bytes(checkpoint.read_bytes() + b"\n")
    checkpoint.chmod(0o600)

    with pytest.raises(
        ensemble.CandidateEnsembleError,
        match="evidence artifacts do not match",
    ):
        ensemble.evaluate_ensemble(files["sample"], files["pairs"])


def test_alias_split_is_stable_at_parent_cluster_and_gold_member_levels(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path)
    target = _member(
        files,
        ("syn_mixed_01", "atlas_interview_current_schedule", "schedule"),
    )
    matches = candidate_gold._member_matches(target)
    by_signature: dict[tuple[Any, ...], list[str]] = {}
    for candidate_id in matches:
        candidate = files["candidates"][candidate_id].candidate
        signature = (
            candidate.expression_id,
            candidate.relation,
            candidate.kind,
            candidate.lifecycle,
            candidate.normalized_value,
        )
        by_signature.setdefault(signature, []).append(candidate_id)
    aliases = max(by_signature.values(), key=len)
    assert len(aliases) >= 3
    verdict_runs = [dict(files["oracle"]) for _ in range(3)]
    for run, alias in zip(verdict_runs, aliases[:3], strict=True):
        for candidate_id in matches:
            run[candidate_id] = "unsupported"
        run[alias] = "uncertain"
    pairs = _replace_run_evidence(files, tmp_path, verdict_runs, prefix="alias")

    result = ensemble.evaluate_ensemble(files["sample"], pairs)

    assert result["cluster_review_semantics"]["cluster_reviews"] == 0
    assert (
        result["stability"]["accepted_parent_clusters"]["minimum_pairwise_jaccard"]
        == 1.0
    )
    assert (
        result["stability"]["gold_semantic_members"]["minimum_pairwise_jaccard"] == 1.0
    )
    assert result["gold_metrics"]["effective_review"][
        "required_member_recall"
    ] == pytest.approx(1.0)
    assert result["ensemble_gate_passed"] is True


def test_differing_lifecycle_votes_become_cluster_review_without_candidate(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path)
    target = _member(
        files,
        ("syn_mixed_01", "atlas_interview_current_schedule", "schedule"),
    )
    matches = candidate_gold._member_matches(target)
    by_lifecycle: dict[str, str] = {}
    for candidate_id in matches:
        lifecycle = files["candidates"][candidate_id].candidate.lifecycle
        by_lifecycle.setdefault(lifecycle, candidate_id)
    assert {"scheduled", "none", "unknown"} <= set(by_lifecycle)
    verdict_runs = [dict(files["oracle"]) for _ in range(3)]
    for run, lifecycle in zip(
        verdict_runs,
        ("scheduled", "none", "unknown"),
        strict=True,
    ):
        for candidate_id in matches:
            run[candidate_id] = "unsupported"
        run[by_lifecycle[lifecycle]] = "uncertain"
    pairs = _replace_run_evidence(
        files,
        tmp_path,
        verdict_runs,
        prefix="lifecycle",
    )

    result = ensemble.evaluate_ensemble(files["sample"], pairs)

    review = result["cluster_review_semantics"]
    assert review["cluster_reviews"] == 1
    assert review["gold_matching_cluster_reviews"] == 1
    assert review["semantic_precision"] == 1.0
    assert review["incremental_recalled_semantic_members"] == 1
    assert review["candidate_count_authorized_by_cluster_review"] == 0
    assert review["supported_candidate_count_authorized_by_cluster_review"] == 0
    assert result["gold_metrics"]["review"]["required_member_recall"] < 1.0
    assert (
        result["gold_metrics"]["effective_review"]["required_member_recall"]
        == result["gold_metrics"]["review"]["required_member_recall"]
    )
    assert result["gold_metrics"]["triage_review"][
        "required_member_recall"
    ] == pytest.approx(1.0)
    assert review["primary_effective_member_recall"] == pytest.approx(35 / 36)
    assert review["triage_member_recall"] == pytest.approx(1.0)
    assert result["ensemble_provenance"]["ensemble_core_version"].endswith("_v3")
    assert result["ensemble_provenance"]["ensemble_policy_version"].endswith("_v3")
    assert result["ensemble_gate_passed"] is True


def test_positive_set_stability_gate_is_not_diluted_by_many_negatives(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path)
    verdict_runs = [dict(files["oracle"]) for _ in range(3)]
    candidate_to_cluster = {
        candidate_id: cluster.cluster_id
        for runtime_batch in files["runtime_batches"]
        for page in runtime_batch.pages
        for cluster in page.clusters
        for candidate_id in cluster.candidate_ids
    }
    removed_clusters: set[str] = set()
    for candidate_id in files["selected"].values():
        cluster_id = candidate_to_cluster[candidate_id]
        if cluster_id in removed_clusters:
            continue
        verdict_runs[2][candidate_id] = "unsupported"
        removed_clusters.add(cluster_id)
        if len(removed_clusters) == 2:
            break
    assert len(removed_clusters) == 2
    pairs = _replace_run_evidence(
        files,
        tmp_path,
        verdict_runs,
        prefix="unstable",
    )

    result = ensemble.evaluate_ensemble(files["sample"], pairs)

    all_candidate = result["stability"]["all_candidate_agreement_diagnostic_only"]
    assert min(row["accepted_presence_agreement"] for row in all_candidate) > 0.95
    assert result["stability"]["accepted_parent_clusters"]["gate_passed"] is False
    assert result["stability"]["gold_semantic_members"]["gate_passed"] is False
    assert result["release_gates"]["gold_candidate_gate"] is True
    assert result["ensemble_gate_passed"] is False


def test_ensemble_rejects_stale_current_source_provenance(tmp_path: Path) -> None:
    files = _fixture(tmp_path)
    manifest_path = files["pairs"][0][1]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_module_sha256"]["pkm_brain.gmail_temporal_frontier"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)

    with pytest.raises(
        ensemble.CandidateEnsembleError,
        match="do not match this checkout",
    ):
        ensemble.evaluate_ensemble(files["sample"], files["pairs"])


def test_aggregate_output_contains_no_runtime_ids_or_source_text(
    tmp_path: Path,
) -> None:
    files = _fixture(tmp_path)

    result = ensemble.evaluate_ensemble(files["sample"], files["pairs"])
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)

    assert not any(isinstance(value, list) for value in result["gold_metrics"].values())
    for sample in files["samples"]:
        assert json.dumps(sample["sample_id"])[1:-1] not in serialized
        assert json.dumps(sample["text"])[1:-1] not in serialized
    for candidate_id in files["candidates"]:
        assert candidate_id not in serialized


def test_nested_private_gold_diagnostics_are_not_serialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _fixture(tmp_path)
    sample_id = str(files["samples"][0]["sample_id"])
    source_text = str(files["samples"][0]["text"])
    candidate_id = next(iter(files["candidates"]))
    original_evaluate = candidate_gold.evaluate

    def evaluate_with_private_diagnostics(*args: Any, **kwargs: Any) -> dict[str, Any]:
        value = original_evaluate(*args, **kwargs)
        value["nested_private_diagnostic"] = {
            "sample": {"ids": [sample_id]},
            "candidate": {"ids": [candidate_id]},
            "source": {"texts": [source_text]},
        }
        value["review"]["nested_member_ids"] = [sample_id, candidate_id]
        return value

    monkeypatch.setattr(candidate_gold, "evaluate", evaluate_with_private_diagnostics)

    result = ensemble.evaluate_ensemble(files["sample"], files["pairs"])
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)

    assert sample_id not in serialized
    assert candidate_id not in serialized
    assert json.dumps(source_text)[1:-1] not in serialized
    assert "nested_private_diagnostic" not in result["gold_metrics"]
    assert "nested_member_ids" not in result["gold_metrics"]["review"]


def test_ensemble_cli_prints_only_aggregate_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    files = _fixture(tmp_path)
    argv = ["evaluate_gmail_temporal_candidate_ensemble.py", str(files["sample"])]
    for checkpoint, manifest in files["pairs"]:
        argv.extend((str(checkpoint), str(manifest)))
    monkeypatch.setattr(sys, "argv", argv)

    ensemble.main()

    printed = json.loads(capsys.readouterr().out)
    assert printed["private_content_printed"] is False
    assert printed["external_calls"] == 0
    assert printed["ensemble_provenance"]["single_run"] is False
    assert "text" not in printed
