#!/usr/bin/env python3
"""Evaluate three private Gmail temporal candidate runs as one ensemble.

The script is local-only. It validates every mode-0600 checkpoint and manifest,
requires identical sample/page/candidate authority, aggregates raw candidate
votes with the production three-run policy, applies production calibration, and
prints content-free agreement and semantic-gold metrics. It makes no external
calls and never represents the ensemble as one model run. Distinct paths and
artifact hashes are diagnostics only; they do not prove independent invocations.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from pkm_brain.gmail_temporal_frontier import (
    GmailTemporalCandidatePageVerdicts,
    GmailTemporalCandidateVerdict,
    gmail_temporal_candidate_ensemble_policy_fingerprint,
    plan_gmail_temporal_candidate_pages,
    validate_gmail_temporal_candidate_ensemble_verdict_set,
)


ENSEMBLE_EVALUATION_VERSION = "gmail_temporal_candidate_ensemble_evaluation_v3"
MIN_PAIRWISE_ACCEPTED_CLUSTER_JACCARD = 0.95
MIN_PAIRWISE_GOLD_MEMBER_JACCARD = 0.95
_REPO_ROOT = Path(__file__).resolve().parents[1]
_CANDIDATE_EVALUATOR_PATH = (
    _REPO_ROOT / "scripts" / "evaluate_gmail_temporal_candidate_gold.py"
)


class CandidateEnsembleError(ValueError):
    """Raised when three-run evidence is unsafe, stale, or incomparable."""


@dataclass(frozen=True)
class _ComponentRun:
    manifest: Any
    rows_by_page: dict[str, dict[str, Any]]
    raw_verdicts: dict[str, str]
    checkpoint_sha256: str
    manifest_sha256: str
    authority_fingerprint: str


def _load_candidate_evaluator() -> ModuleType:
    module_name = "_gmail_temporal_candidate_gold_ensemble_evaluator"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        module_name,
        _CANDIDATE_EVALUATOR_PATH,
    )
    if spec is None or spec.loader is None:
        raise CandidateEnsembleError("candidate evaluator could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


candidate_gold = _load_candidate_evaluator()


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise CandidateEnsembleError(
            "ensemble evidence could not be fingerprinted"
        ) from exc


def _canonical_sha256(value: Any, *, prefix: str) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return prefix + hashlib.sha256(payload).hexdigest()


def _checkpoint_authority_fingerprint(
    rows: list[dict[str, Any]],
) -> str:
    material: list[dict[str, Any]] = []
    for row in rows:
        verdicts = row["verdicts"]
        authority = {key: value for key, value in row.items() if key != "verdicts"}
        authority["candidate_ids"] = sorted(
            str(verdict["candidate_id"]) for verdict in verdicts
        )
        material.append(authority)
    material.sort(key=lambda item: str(item["page_fingerprint"]))
    return _canonical_sha256(material, prefix="gtfea_")


def _manifest_authority(manifest: Any) -> dict[str, Any]:
    return {
        "checkpoint_version": manifest.checkpoint_version,
        "protocol_fingerprint": manifest.protocol_fingerprint,
        "model": manifest.model,
        "reasoning_effort": manifest.reasoning_effort,
        "source_module_sha256": dict(manifest.source_module_sha256),
        "evaluator_sha256": manifest.evaluator_sha256,
        "semantic_gold_sha256": manifest.semantic_gold_sha256,
        "benchmark_builder_sha256": manifest.benchmark_builder_sha256,
        "sample_sha256": manifest.sample_sha256,
        "sample_record_count": manifest.sample_record_count,
        "checkpoint_row_count": manifest.checkpoint_row_count,
    }


def _load_current_component_manifest(
    path: Path,
    *,
    sample_path: Path,
    sample_count: int,
    checkpoint_path: Path,
    checkpoint_count: int,
) -> Any:
    """Require evidence built against the exact current checkout and evaluator."""

    try:
        return candidate_gold._load_run_manifest(
            path,
            sample_path=sample_path,
            sample_record_count=sample_count,
            checkpoint_path=checkpoint_path,
            checkpoint_row_count=checkpoint_count,
        )
    except candidate_gold.CandidateGoldError as exc:
        raise CandidateEnsembleError(
            f"component run manifest is stale, unsafe, or unsupported: {exc}"
        ) from exc


def _load_component(
    *,
    sample_path: Path,
    sample_count: int,
    checkpoint_path: Path,
    manifest_path: Path,
    runtime_batches: list[Any],
    pages: dict[str, tuple[Any, Any]],
) -> _ComponentRun:
    try:
        rows = candidate_gold._load_jsonl(checkpoint_path)
        manifest = _load_current_component_manifest(
            manifest_path,
            sample_path=sample_path,
            sample_count=sample_count,
            checkpoint_path=checkpoint_path,
            checkpoint_count=len(rows),
        )
        raw_verdicts, _ = candidate_gold._checkpoint_verdicts(
            rows,
            runtime_batches,
            pages,
            manifest,
        )
    except candidate_gold.CandidateGoldError as exc:
        raise CandidateEnsembleError(str(exc)) from exc
    return _ComponentRun(
        manifest=manifest,
        rows_by_page={str(row["page_fingerprint"]): row for row in rows},
        raw_verdicts=raw_verdicts,
        checkpoint_sha256=_sha256_file(checkpoint_path),
        manifest_sha256=_sha256_file(manifest_path),
        authority_fingerprint=_checkpoint_authority_fingerprint(rows),
    )


def _typed_rows_for_batch(
    component: _ComponentRun,
    runtime_batch: Any,
) -> tuple[GmailTemporalCandidatePageVerdicts, ...]:
    return tuple(
        GmailTemporalCandidatePageVerdicts(
            frontier_fingerprint=page.frontier_fingerprint,
            page_fingerprint=page.page_fingerprint,
            verdicts=tuple(
                GmailTemporalCandidateVerdict(
                    candidate_id=str(item["candidate_id"]),
                    verdict=str(item["verdict"]),  # type: ignore[arg-type]
                )
                for item in component.rows_by_page[page.page_fingerprint]["verdicts"]
            ),
        )
        for page in runtime_batch.pages
    )


def _pairwise_agreement(
    runs: tuple[_ComponentRun, _ComponentRun, _ComponentRun],
) -> list[dict[str, Any]]:
    candidate_ids = tuple(sorted(runs[0].raw_verdicts))
    output: list[dict[str, Any]] = []
    for first, second in ((0, 1), (0, 2), (1, 2)):
        first_values = runs[first].raw_verdicts
        second_values = runs[second].raw_verdicts
        exact = sum(
            first_values[candidate_id] == second_values[candidate_id]
            for candidate_id in candidate_ids
        )
        accepted = sum(
            (first_values[candidate_id] != "unsupported")
            == (second_values[candidate_id] != "unsupported")
            for candidate_id in candidate_ids
        )
        supported = sum(
            (first_values[candidate_id] == "supported")
            == (second_values[candidate_id] == "supported")
            for candidate_id in candidate_ids
        )
        output.append(
            {
                "first_run": first + 1,
                "second_run": second + 1,
                "candidate_count": len(candidate_ids),
                "exact_verdict_agreements": exact,
                "exact_verdict_agreement": exact / len(candidate_ids),
                "accepted_presence_agreements": accepted,
                "accepted_presence_agreement": accepted / len(candidate_ids),
                "supported_presence_agreements": supported,
                "supported_presence_agreement": supported / len(candidate_ids),
            }
        )
    return output


def _jaccard(first: set[Any], second: set[Any]) -> float:
    union = first | second
    return len(first & second) / len(union) if union else 1.0


def _pairwise_jaccard(
    values: tuple[set[Any], set[Any], set[Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for first, second in ((0, 1), (0, 2), (1, 2)):
        union = values[first] | values[second]
        intersection = values[first] & values[second]
        output.append(
            {
                "first_run": first + 1,
                "second_run": second + 1,
                "first_count": len(values[first]),
                "second_count": len(values[second]),
                "intersection_count": len(intersection),
                "union_count": len(union),
                "jaccard": _jaccard(values[first], values[second]),
            }
        )
    return output


def _minimum_jaccard(rows: list[dict[str, Any]]) -> float:
    return min(float(row["jaccard"]) for row in rows)


def _candidate_parent_clusters(
    runtime_batches: list[Any],
) -> tuple[dict[str, str], dict[str, set[str]], dict[str, str]]:
    candidate_to_cluster: dict[str, str] = {}
    cluster_to_candidates: dict[str, set[str]] = {}
    cluster_to_sample: dict[str, str] = {}
    for runtime_batch in runtime_batches:
        for page in runtime_batch.pages:
            for cluster in page.clusters:
                existing_sample = cluster_to_sample.setdefault(
                    cluster.cluster_id,
                    runtime_batch.sample_id,
                )
                if existing_sample != runtime_batch.sample_id:
                    raise CandidateEnsembleError(
                        "parent cluster authority is not cohort-unique"
                    )
                cluster_candidates = cluster_to_candidates.setdefault(
                    cluster.cluster_id,
                    set(),
                )
                for candidate_id in cluster.candidate_ids:
                    existing_cluster = candidate_to_cluster.setdefault(
                        candidate_id,
                        cluster.cluster_id,
                    )
                    if existing_cluster != cluster.cluster_id:
                        raise CandidateEnsembleError(
                            "candidate belongs to multiple parent clusters"
                        )
                    cluster_candidates.add(candidate_id)
    return candidate_to_cluster, cluster_to_candidates, cluster_to_sample


def _accepted_parent_cluster_sets(
    runs: tuple[_ComponentRun, _ComponentRun, _ComponentRun],
    candidate_to_cluster: Mapping[str, str],
) -> tuple[set[str], set[str], set[str]]:
    return tuple(
        {
            candidate_to_cluster[candidate_id]
            for candidate_id, verdict in component.raw_verdicts.items()
            if verdict != "unsupported"
        }
        for component in runs
    )  # type: ignore[return-value]


def _accepted_gold_member_sets(
    runs: tuple[_ComponentRun, _ComponentRun, _ComponentRun],
    units: tuple[Any, ...],
) -> tuple[
    set[tuple[str, str, str]], set[tuple[str, str, str]], set[tuple[str, str, str]]
]:
    members = tuple(member for unit in units for member in unit.members)
    return tuple(
        {
            member.key
            for member in members
            if any(
                component.raw_verdicts[candidate_id] != "unsupported"
                for candidate_id in candidate_gold._member_matches(member)
            )
        }
        for component in runs
    )  # type: ignore[return-value]


def _triage_review_scores(
    units: tuple[Any, ...],
    primary_member_quality: Mapping[tuple[str, str, str], float],
    reviewed_cluster_ids: set[str],
    candidate_to_cluster: Mapping[str, str],
) -> list[list[float]]:
    output: list[list[float]] = []
    for unit in units:
        member_scores: list[float] = []
        for member in unit.members:
            matches = candidate_gold._member_matches(member)
            member_scores.append(
                max(
                    primary_member_quality.get(member.key, 0.0),
                    max(
                        (
                            quality
                            for candidate_id, quality in matches.items()
                            if candidate_to_cluster[candidate_id]
                            in reviewed_cluster_ids
                        ),
                        default=0.0,
                    ),
                )
            )
        output.append(member_scores)
    return output


def _useful_record_recall(
    samples: list[dict[str, Any]],
    units: tuple[Any, ...],
    scores: list[list[float]],
) -> tuple[int, int, float]:
    recalled_samples = {
        unit.key[0]
        for unit, member_scores in zip(units, scores, strict=True)
        if any(score > 0.0 for score in member_scores)
    }
    useful_sample_ids = {
        str(sample["sample_id"])
        for sample in samples
        if sample["gold"].get("expected_material") is True
    }
    if not useful_sample_ids:
        raise CandidateEnsembleError("semantic benchmark has no useful records")
    recalled = len(useful_sample_ids & recalled_samples)
    return recalled, len(useful_sample_ids), recalled / len(useful_sample_ids)


def _aggregate_gold_output(
    gold: Mapping[str, Any],
    *,
    triage_review: Mapping[str, Any],
    triage_useful_record_recall: float,
) -> dict[str, Any]:
    """Return only aggregate values; evaluator diagnostic ID arrays stay private."""

    aggregate: dict[str, Any] = {
        key: value
        for key, value in gold.items()
        if isinstance(value, (bool, int, float)) or value is None
    }
    for key in ("frontier", "supported", "review"):
        value = gold.get(key)
        if isinstance(value, Mapping):
            aggregate[key] = {
                nested_key: nested_value
                for nested_key, nested_value in value.items()
                if isinstance(nested_value, (bool, int, float)) or nested_value is None
            }
    original_gates = gold.get("gates")
    if not isinstance(original_gates, Mapping) or not all(
        isinstance(value, bool) for value in original_gates.values()
    ):
        raise CandidateEnsembleError("candidate gold gates are malformed")
    aggregate["candidate_only_gates"] = dict(original_gates)
    aggregate["candidate_only_gate_passed"] = bool(gold.get("candidate_gate_passed"))
    # Cluster reviews are triage signals only.  They never improve the primary
    # production-artifact recall or precision copied from candidate gold.
    aggregate["effective_review"] = dict(aggregate["review"])
    aggregate["effective_recall_arm_precision"] = gold["effective_artifact_precision"]
    aggregate["effective_useful_record_review_recall"] = gold[
        "useful_record_review_recall"
    ]
    aggregate["triage_review"] = dict(triage_review)
    aggregate["triage_useful_record_recall"] = triage_useful_record_recall
    aggregate["gates"] = dict(original_gates)
    aggregate["candidate_gate_passed"] = bool(gold.get("candidate_gate_passed"))
    return aggregate


def _assert_aggregate_only(
    output: Mapping[str, Any],
    *,
    samples: list[dict[str, Any]],
    candidate_ids: set[str],
) -> None:
    serialized = json.dumps(
        output,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    sensitive_values = (
        candidate_ids
        | {str(sample["sample_id"]) for sample in samples}
        | {
            str(sample["text"])
            for sample in samples
            if isinstance(sample.get("text"), str) and sample["text"]
        }
    )
    for value in sensitive_values:
        encoded = json.dumps(value, ensure_ascii=False)[1:-1]
        if encoded and encoded in serialized:
            raise CandidateEnsembleError(
                "aggregate output unexpectedly contains private runtime identity"
            )


def evaluate_ensemble(
    sample_path: Path,
    checkpoint_manifest_pairs: tuple[
        tuple[Path, Path],
        tuple[Path, Path],
        tuple[Path, Path],
    ],
) -> dict[str, Any]:
    """Validate, aggregate, calibrate, and score exactly three current runs."""

    component_evidence_paths = tuple(
        path for pair in checkpoint_manifest_pairs for path in pair
    )
    all_paths = (sample_path,) + component_evidence_paths
    distinct_path_count = len({path.resolve() for path in all_paths})
    distinct_component_evidence_path_count = len(
        {path.resolve() for path in component_evidence_paths}
    )
    component_evidence_paths_distinct = distinct_component_evidence_path_count == len(
        component_evidence_paths
    )
    try:
        samples = candidate_gold._load_jsonl(sample_path)
        runtime_batches, candidates, pages = candidate_gold._runtime_batches(samples)
        units = candidate_gold._compile_gold(samples, candidates)
    except candidate_gold.CandidateGoldError as exc:
        raise CandidateEnsembleError(str(exc)) from exc
    if not units:
        raise CandidateEnsembleError("benchmark contains no semantic units")
    (
        candidate_to_cluster,
        cluster_to_candidates,
        cluster_to_sample,
    ) = _candidate_parent_clusters(runtime_batches)
    if set(candidate_to_cluster) != set(candidates):
        raise CandidateEnsembleError(
            "parent clusters do not cover the candidate authority"
        )

    components = tuple(
        _load_component(
            sample_path=sample_path,
            sample_count=len(samples),
            checkpoint_path=checkpoint_path,
            manifest_path=manifest_path,
            runtime_batches=runtime_batches,
            pages=pages,
        )
        for checkpoint_path, manifest_path in checkpoint_manifest_pairs
    )
    manifest_authorities = tuple(
        _manifest_authority(item.manifest) for item in components
    )
    if any(value != manifest_authorities[0] for value in manifest_authorities[1:]):
        raise CandidateEnsembleError(
            "ensemble manifests do not share exact sample and run authority"
        )
    if any(
        item.authority_fingerprint != components[0].authority_fingerprint
        for item in components[1:]
    ):
        raise CandidateEnsembleError(
            "ensemble checkpoints do not share exact page and candidate authority"
        )
    try:
        current_source_hashes = candidate_gold._current_repo_module_hashes()
    except candidate_gold.CandidateGoldError as exc:
        raise CandidateEnsembleError(str(exc)) from exc
    current_evaluator_sha256 = _sha256_file(candidate_gold._EVALUATOR_PATH)
    fresh_provenance_verified = all(
        component.manifest.evaluator_sha256 == current_evaluator_sha256
        and all(
            component.manifest.source_module_sha256[name] == digest
            for name, digest in current_source_hashes.items()
        )
        for component in components
    )
    if not fresh_provenance_verified:
        raise CandidateEnsembleError(
            "component source or evaluator provenance is not current"
        )

    accepted_cluster_sets = _accepted_parent_cluster_sets(
        components,
        candidate_to_cluster,
    )
    accepted_cluster_pairwise = _pairwise_jaccard(accepted_cluster_sets)
    accepted_cluster_minimum = _minimum_jaccard(accepted_cluster_pairwise)
    accepted_cluster_stability_passed = (
        accepted_cluster_minimum >= MIN_PAIRWISE_ACCEPTED_CLUSTER_JACCARD
    )
    gold_member_sets = _accepted_gold_member_sets(components, units)
    gold_member_pairwise = _pairwise_jaccard(gold_member_sets)
    gold_member_minimum = _minimum_jaccard(gold_member_pairwise)
    gold_member_stability_passed = (
        gold_member_minimum >= MIN_PAIRWISE_GOLD_MEMBER_JACCARD
    )

    raw_consensus: dict[str, str] = {}
    effective_consensus: dict[str, str] = {}
    reviewed_cluster_ids: set[str] = set()
    core_versions: set[str] = set()
    core_policy_versions: set[str] = set()
    core_policy_fingerprints: set[str] = set()
    for runtime_batch in runtime_batches:
        if not runtime_batch.pages:
            continue
        page_plan = plan_gmail_temporal_candidate_pages(
            analysis=runtime_batch.analysis,
            batch=runtime_batch.batch,
            max_clusters_per_page=4,
            max_candidates_per_page=12,
            max_payload_bytes=12_000,
        )
        ensemble_result = validate_gmail_temporal_candidate_ensemble_verdict_set(
            analysis=runtime_batch.analysis,
            batch=runtime_batch.batch,
            plan=page_plan,
            runs=tuple(
                _typed_rows_for_batch(component, runtime_batch)
                for component in components
            ),
        )
        core_versions.add(ensemble_result.version)
        core_policy_versions.add(ensemble_result.policy_version)
        core_policy_fingerprints.add(ensemble_result.policy_fingerprint)
        for review in ensemble_result.cluster_reviews:
            if review.cluster_id not in cluster_to_candidates:
                raise CandidateEnsembleError(
                    "ensemble review references an unknown parent cluster"
                )
            if review.cluster_id in reviewed_cluster_ids:
                raise CandidateEnsembleError("ensemble repeats a parent cluster review")
            reviewed_cluster_ids.add(review.cluster_id)
        for row in ensemble_result.consensus_rows:
            for verdict in row.verdicts:
                if verdict.candidate_id in raw_consensus:
                    raise CandidateEnsembleError(
                        "ensemble consensus repeats a candidate"
                    )
                raw_consensus[verdict.candidate_id] = verdict.verdict
        supported = set(ensemble_result.verdict_set.supported_candidate_ids)
        uncertain = {
            candidate_id
            for cluster in ensemble_result.verdict_set.uncertain_clusters
            for candidate_id in cluster.plausible_candidate_ids
        }
        for runtime_candidate in runtime_batch.candidates:
            candidate_id = runtime_candidate.candidate.candidate_id
            effective_consensus[candidate_id] = (
                "supported"
                if candidate_id in supported
                else "uncertain"
                if candidate_id in uncertain
                else "unsupported"
            )

    expected_candidate_ids = set(candidates)
    if (
        set(raw_consensus) != expected_candidate_ids
        or set(effective_consensus) != expected_candidate_ids
    ):
        raise CandidateEnsembleError(
            "ensemble consensus does not cover the candidate authority"
        )
    if (
        len(core_versions) != 1
        or len(core_policy_versions) != 1
        or core_policy_fingerprints
        != {gmail_temporal_candidate_ensemble_policy_fingerprint()}
    ):
        raise CandidateEnsembleError(
            "ensemble core policy provenance is missing or incoherent"
        )

    score_provenance = {
        "evidence_type": "three_run_candidate_consensus",
        "single_run": False,
        "component_run_count": 3,
    }
    try:
        gold = candidate_gold.evaluate(
            sample_path,
            None,
            None,
            prevalidated_verdict_maps=(raw_consensus, effective_consensus),
            provenance_override=score_provenance,
        )
    except candidate_gold.CandidateGoldError as exc:
        raise CandidateEnsembleError(str(exc)) from exc
    gold.pop("run_provenance", None)

    accepted_candidate_ids = {
        candidate_id
        for candidate_id, verdict in effective_consensus.items()
        if verdict != "unsupported"
    }
    raw_primary_matches = gold.get("matched_effective_members")
    if not isinstance(raw_primary_matches, list):
        raise CandidateEnsembleError("artifact member matches are malformed")
    primary_member_quality: dict[tuple[str, str, str], float] = {}
    for item in raw_primary_matches:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"member_key", "quality"}
            or not isinstance(item.get("member_key"), list)
            or len(item["member_key"]) != 3
            or not all(isinstance(value, str) for value in item["member_key"])
            or item.get("quality") not in {0.5, 1.0}
        ):
            raise CandidateEnsembleError("artifact member matches are malformed")
        member_key = tuple(item["member_key"])
        if member_key in primary_member_quality:
            raise CandidateEnsembleError("artifact member match is duplicated")
        primary_member_quality[member_key] = float(item["quality"])

    triage_review_scores = _triage_review_scores(
        units,
        primary_member_quality,
        reviewed_cluster_ids,
        candidate_to_cluster,
    )
    triage_review_metrics = candidate_gold._recall_metrics(triage_review_scores)
    (
        triage_recalled_useful_records,
        useful_record_count,
        triage_useful_record_recall,
    ) = _useful_record_recall(samples, units, triage_review_scores)

    member_matches = {
        member.key: set(candidate_gold._member_matches(member))
        for unit in units
        for member in unit.members
    }
    reviewed_member_keys = {
        member_key
        for member_key, matching_candidates in member_matches.items()
        if any(
            candidate_to_cluster[candidate_id] in reviewed_cluster_ids
            for candidate_id in matching_candidates
        )
    }
    candidate_recalled_member_keys = set(primary_member_quality)
    gold_matching_review_clusters = {
        cluster_id
        for cluster_id in reviewed_cluster_ids
        if any(
            candidate_id in matching_candidates
            for candidate_id in cluster_to_candidates[cluster_id]
            for matching_candidates in member_matches.values()
        )
    }
    incremental_reviewed_member_keys = (
        reviewed_member_keys - candidate_recalled_member_keys
    )
    semantic_member_count = sum(len(unit.members) for unit in units)
    cluster_review_count = len(reviewed_cluster_ids)
    correct_cluster_review_count = len(gold_matching_review_clusters)
    incorrect_cluster_review_count = cluster_review_count - correct_cluster_review_count
    cluster_review_semantic_precision = (
        correct_cluster_review_count / cluster_review_count
        if cluster_review_count
        else None
    )
    triage_review_item_count = int(gold["production_artifacts"]) + cluster_review_count
    noise_sample_ids = {
        str(sample["sample_id"])
        for sample in samples
        if sample["gold"].get("expected_material") is False
    }
    candidate_selected_noise_samples = {
        candidates[candidate_id].sample_id
        for candidate_id in accepted_candidate_ids
        if candidates[candidate_id].sample_id in noise_sample_ids
    }
    cluster_reviewed_noise_samples = {
        cluster_to_sample[cluster_id]
        for cluster_id in reviewed_cluster_ids
        if cluster_to_sample[cluster_id] in noise_sample_ids
    }
    triage_selected_noise_records = len(
        candidate_selected_noise_samples | cluster_reviewed_noise_samples
    )
    candidate_gates = gold.get("gates")
    if not isinstance(candidate_gates, Mapping):
        raise CandidateEnsembleError("candidate gold gates are malformed")
    aggregate_gold = _aggregate_gold_output(
        gold,
        triage_review=triage_review_metrics,
        triage_useful_record_recall=triage_useful_record_recall,
    )

    candidate_ids = tuple(sorted(expected_candidate_ids))
    positive_vote_histogram = {str(value): 0 for value in range(4)}
    for candidate_id in candidate_ids:
        positive_votes = sum(
            component.raw_verdicts[candidate_id] != "unsupported"
            for component in components
        )
        positive_vote_histogram[str(positive_votes)] += 1
    calibration_changes = {
        (raw_consensus[candidate_id], effective_consensus[candidate_id])
        for candidate_id in candidate_ids
        if raw_consensus[candidate_id] != effective_consensus[candidate_id]
    }
    common_manifest = components[0].manifest
    release_gates = {
        "gold_candidate_gate": bool(aggregate_gold["candidate_gate_passed"]),
        "accepted_parent_cluster_stability": (accepted_cluster_stability_passed),
        "gold_semantic_member_stability": gold_member_stability_passed,
        "fresh_provenance": fresh_provenance_verified,
        "distinct_component_evidence_paths": component_evidence_paths_distinct,
    }
    output = {
        "version": ENSEMBLE_EVALUATION_VERSION,
        "ensemble_provenance": {
            "evidence_type": "three_run_candidate_consensus",
            "single_run": False,
            "component_run_count": 3,
            "component_model": common_manifest.model,
            "component_reasoning_effort": common_manifest.reasoning_effort,
            "checkpoint_version": common_manifest.checkpoint_version,
            "protocol_fingerprint": common_manifest.protocol_fingerprint,
            "candidate_authority_fingerprint": components[0].authority_fingerprint,
            "ensemble_core_version": next(iter(core_versions)),
            "ensemble_policy_version": next(iter(core_policy_versions)),
            "ensemble_policy_fingerprint": next(iter(core_policy_fingerprints)),
            "fresh_provenance_verified": fresh_provenance_verified,
            "independent_invocations_verified": False,
            "distinct_path_count": distinct_path_count,
            "all_evidence_paths_distinct": distinct_path_count == len(all_paths),
            "distinct_component_evidence_path_count": (
                distinct_component_evidence_path_count
            ),
            "all_component_evidence_paths_distinct": (
                component_evidence_paths_distinct
            ),
            "distinct_checkpoint_artifact_count": len(
                {item.checkpoint_sha256 for item in components}
            ),
            "distinct_manifest_artifact_count": len(
                {item.manifest_sha256 for item in components}
            ),
            "source_module_sha256": dict(common_manifest.source_module_sha256),
            "artifact_sha256": {
                "sample": common_manifest.sample_sha256,
                "candidate_evaluator": common_manifest.evaluator_sha256,
                "ensemble_evaluator": _sha256_file(Path(__file__).resolve()),
                "semantic_gold": common_manifest.semantic_gold_sha256,
                "benchmark_builder": common_manifest.benchmark_builder_sha256,
                "component_checkpoints": [
                    item.checkpoint_sha256 for item in components
                ],
                "component_manifests": [item.manifest_sha256 for item in components],
            },
            "sample_record_count": common_manifest.sample_record_count,
            "checkpoint_page_count_per_run": common_manifest.checkpoint_row_count,
        },
        "stability": {
            "all_candidate_agreement_diagnostic_only": _pairwise_agreement(components),
            "accepted_parent_clusters": {
                "minimum_required": MIN_PAIRWISE_ACCEPTED_CLUSTER_JACCARD,
                "pairwise": accepted_cluster_pairwise,
                "minimum_pairwise_jaccard": accepted_cluster_minimum,
                "gate_passed": accepted_cluster_stability_passed,
            },
            "gold_semantic_members": {
                "minimum_required": MIN_PAIRWISE_GOLD_MEMBER_JACCARD,
                "pairwise": gold_member_pairwise,
                "minimum_pairwise_jaccard": gold_member_minimum,
                "gate_passed": gold_member_stability_passed,
            },
        },
        "consensus": {
            "candidate_count": len(candidate_ids),
            "positive_vote_histogram": positive_vote_histogram,
            "raw_supported": sum(
                value == "supported" for value in raw_consensus.values()
            ),
            "raw_uncertain": sum(
                value == "uncertain" for value in raw_consensus.values()
            ),
            "raw_unsupported": sum(
                value == "unsupported" for value in raw_consensus.values()
            ),
            "effective_supported": sum(
                value == "supported" for value in effective_consensus.values()
            ),
            "effective_uncertain": sum(
                value == "uncertain" for value in effective_consensus.values()
            ),
            "effective_unsupported": sum(
                value == "unsupported" for value in effective_consensus.values()
            ),
            "production_calibration_changes": sum(
                raw_consensus[candidate_id] != effective_consensus[candidate_id]
                for candidate_id in candidate_ids
            ),
            "production_calibration_change_kinds": sorted(
                f"{before}_to_{after}" for before, after in calibration_changes
            ),
        },
        "cluster_review_semantics": {
            "cluster_reviews": cluster_review_count,
            "gold_matching_cluster_reviews": correct_cluster_review_count,
            "nonmatching_cluster_reviews": incorrect_cluster_review_count,
            "semantic_precision": cluster_review_semantic_precision,
            "recalled_semantic_members": len(reviewed_member_keys),
            "semantic_member_count": semantic_member_count,
            "semantic_member_recall": (
                len(reviewed_member_keys) / semantic_member_count
            ),
            "triage_member_recall": triage_review_metrics["required_member_recall"],
            "primary_effective_member_recall": gold["effective_member_recall"],
            "incremental_recalled_semantic_members": len(
                incremental_reviewed_member_keys
            ),
            "incremental_semantic_member_recall": (
                len(incremental_reviewed_member_keys) / semantic_member_count
            ),
            "overlap_with_candidate_recalled_members": len(
                reviewed_member_keys & candidate_recalled_member_keys
            ),
            "candidate_count_authorized_by_cluster_review": 0,
            "supported_candidate_count_authorized_by_cluster_review": 0,
            "effective_review_artifacts": int(gold["production_artifacts"]),
            "triage_review_items": triage_review_item_count,
            "effective_selected_noise_records": int(gold["selected_noise_records"]),
            "triage_selected_noise_records": triage_selected_noise_records,
            "effective_recalled_useful_records": int(gold["recalled_useful_records"]),
            "triage_recalled_useful_records": triage_recalled_useful_records,
            "useful_records": useful_record_count,
        },
        "gold_metrics": aggregate_gold,
        "release_gates": release_gates,
        "ensemble_gate_passed": all(release_gates.values()),
        "private_content_printed": False,
        "external_calls": 0,
    }
    _assert_aggregate_only(
        output,
        samples=samples,
        candidate_ids=expected_candidate_ids,
    )
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sample", type=Path)
    for index in range(1, 4):
        parser.add_argument(f"checkpoint_{index}", type=Path)
        parser.add_argument(f"manifest_{index}", type=Path)
    args = parser.parse_args()
    pairs = tuple(
        (
            getattr(args, f"checkpoint_{index}"),
            getattr(args, f"manifest_{index}"),
        )
        for index in range(1, 4)
    )
    print(
        json.dumps(
            evaluate_ensemble(args.sample, pairs),  # type: ignore[arg-type]
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
