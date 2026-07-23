from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pkm_brain.gmail_temporal_batching import (
    GmailTemporalSelectorBatch,
    plan_gmail_temporal_selector_batches,
)
from pkm_brain.gmail_temporal_frontier import (
    GmailTemporalCandidatePage,
    GmailTemporalCandidatePageVerdicts,
    GmailTemporalCandidateVerdict,
    GmailTemporalVerificationCandidate,
    build_gmail_temporal_candidate_frontier,
    gmail_temporal_candidate_page_payload,
    plan_gmail_temporal_candidate_pages,
    validate_gmail_temporal_candidate_verdict_set,
)
from pkm_brain.gmail_temporal_leads import (
    TemporalLeadAnalysis,
    analyze_gmail_temporal_leads,
)


MIN_END_TO_END_ANY_RECALL = 0.95
MIN_END_TO_END_REQUIRED_MEMBER_RECALL = 0.95
MIN_END_TO_END_COMPLETE_UNIT_RECALL = 0.90
MIN_END_TO_END_EXACT_UNIT_RECALL = 0.90
MIN_USEFUL_RECORD_RECALL = 0.95
# Personal-production release bar: at least 90% of members expected to be
# directly supported must stay out of the uncertainty lane.  The separate
# review-recall gate preserves the recall-biased escape hatch.
MIN_SUPPORTED_REQUIRED_MEMBER_RECALL = 0.90
MIN_STRICT_SUPPORTED_PRECISION = 0.95
MIN_RECALL_ARM_PRECISION = 0.90
MAX_SUPPORTED_TO_UNCERTAIN_RATE = 0.20
MAX_ACCEPTED_NEGATIVE_REVIEW_RATE = 0.05
RUN_MANIFEST_VERSION = "gmail_temporal_candidate_benchmark_run_v1"
EXPECTED_CHECKPOINT_VERSION = "gmail_temporal_frontier_luna_checkpoint_v7"
EXPECTED_MODEL = "gpt-5.6-luna"
EXPECTED_REASONING_EFFORT = "medium"
_GRADE_VALUE = {"absent": 0, "partial": 1, "exact": 2}
_QUALITY_SCORE = {"partial": 0.5, "exact": 1.0}
_VERDICTS = {"supported", "uncertain", "unsupported"}
_CHECKPOINT_KEYS = {
    "version",
    "sample_id",
    "source_sha256",
    "protocol_fingerprint",
    "source_module_sha256",
    "plan_fingerprint",
    "page_case_id",
    "batch_fingerprint",
    "analysis_fingerprint",
    "frontier_fingerprint",
    "page_fingerprint",
    "candidate_page_plan_fingerprint",
    "candidate_page_payload_bytes",
    "batch_sequence",
    "page_sequence",
    "page_count",
    "verdicts",
}
_RUN_MANIFEST_KEYS = {
    "version",
    "checkpoint_version",
    "protocol_fingerprint",
    "model",
    "reasoning_effort",
    "source_module_sha256",
    "evaluator_sha256",
    "semantic_gold_sha256",
    "benchmark_builder_sha256",
    "sample_sha256",
    "sample_record_count",
    "checkpoint_sha256",
    "checkpoint_row_count",
}
_PROVENANCE_MODULE_KEYS = {
    "runner",
    "base_runner",
    "pkm_brain.gmail_llm",
    "pkm_brain.gmail_temporal_batching",
    "pkm_brain.gmail_temporal_frontier",
    "pkm_brain.gmail_temporal_leads",
    "pkm_brain.gmail_temporal_reduction",
    "pkm_brain.gmail_temporal_selection",
    "pkm_brain.gmail_temporal_verifier",
}
_REPO_MODULE_FILES = {
    name: Path(__file__).resolve().parents[1]
    / "src"
    / "pkm_brain"
    / f"{name.rsplit('.', 1)[1]}.py"
    for name in _PROVENANCE_MODULE_KEYS
    if name.startswith("pkm_brain.")
}
_REPO_ROOT = Path(__file__).resolve().parents[1]
_EVALUATOR_PATH = Path(__file__).resolve()
_SEMANTIC_GOLD_PATH = (
    _REPO_ROOT / "src" / "pkm_brain" / "gmail_temporal_synthetic_gold.py"
)
_BENCHMARK_BUILDER_PATH = (
    _REPO_ROOT / "scripts" / "build_gmail_temporal_synthetic_benchmark.py"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_PROTOCOL_PATTERN = re.compile(r"^gtfproto_[0-9a-f]{64}$")


class CandidateGoldError(ValueError):
    """Raised when semantic gold or raw verifier evidence is invalid or stale."""


@dataclass(frozen=True)
class RuntimeCandidate:
    sample_id: str
    candidate: GmailTemporalVerificationCandidate
    expression_surface: str
    expression_form: str
    expression_field: str
    subject_surface: str
    subject_type: str
    subject_field: str
    lifecycle_surface: str | None
    lifecycle_role: str | None
    lifecycle_field: str | None


@dataclass(frozen=True)
class RuntimeBatch:
    sample_id: str
    analysis: TemporalLeadAnalysis
    batch: GmailTemporalSelectorBatch
    pages: tuple[GmailTemporalCandidatePage, ...]
    candidates: tuple[RuntimeCandidate, ...]
    plan_fingerprint: str
    candidate_page_plan_fingerprint: str
    candidate_page_payload_bytes: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class GoldAlternative:
    quality: str
    expected_verdict: str
    candidate_ids: frozenset[str]


@dataclass(frozen=True)
class GoldMember:
    key: tuple[str, str, str]
    expected_verdict: str
    baseline_grade: str
    alternatives: tuple[GoldAlternative, ...]


@dataclass(frozen=True)
class GoldUnit:
    key: tuple[str, str]
    baseline_grade: str
    members: tuple[GoldMember, ...]


@dataclass(frozen=True)
class RunManifest:
    checkpoint_version: str
    protocol_fingerprint: str
    model: str
    reasoning_effort: str
    source_module_sha256: Mapping[str, str]
    evaluator_sha256: str
    semantic_gold_sha256: str
    benchmark_builder_sha256: str
    sample_sha256: str
    sample_record_count: int
    checkpoint_sha256: str
    checkpoint_row_count: int


ArtifactHypothesis = tuple[str, str, str, str, str | None]


@dataclass(frozen=True)
class ProductionArtifact:
    """One production-visible citation or non-routable uncertainty sidecar."""

    artifact_id: str
    kind: str
    candidate_ids: tuple[str, ...]
    hypotheses: tuple[ArtifactHypothesis, ...]


@dataclass(frozen=True)
class ArtifactMatchEdge:
    artifact_id: str
    member_key: tuple[str, str, str]
    quality: float
    priority: int


def _validate_private_artifact(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise CandidateGoldError("input must be a regular non-symlink file")
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise CandidateGoldError("input metadata could not be read") from exc
    if mode != 0o600:
        raise CandidateGoldError("input artifact must have mode 0600")


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    _validate_private_artifact(path)
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise CandidateGoldError("input could not be read as JSONL") from exc
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise CandidateGoldError("input is empty or malformed")
    return rows


def _current_repo_module_hashes() -> dict[str, str]:
    try:
        return {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in sorted(_REPO_MODULE_FILES.items())
        }
    except OSError as exc:
        raise CandidateGoldError(
            "current source modules could not be fingerprinted"
        ) from exc


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise CandidateGoldError(
            "evidence artifact could not be fingerprinted"
        ) from exc


def _load_run_manifest(
    path: Path,
    *,
    sample_path: Path,
    sample_record_count: int,
    checkpoint_path: Path,
    checkpoint_row_count: int,
) -> RunManifest:
    _validate_private_artifact(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CandidateGoldError("run manifest could not be read as JSON") from exc
    if not isinstance(value, Mapping) or set(value) != _RUN_MANIFEST_KEYS:
        raise CandidateGoldError("run manifest schema is invalid")
    source_hashes = value.get("source_module_sha256")
    count_fields = (
        value.get("sample_record_count"),
        value.get("checkpoint_row_count"),
    )
    if (
        value.get("version") != RUN_MANIFEST_VERSION
        or value.get("checkpoint_version") != EXPECTED_CHECKPOINT_VERSION
        or value.get("model") != EXPECTED_MODEL
        or value.get("reasoning_effort") != EXPECTED_REASONING_EFFORT
        or not isinstance(value.get("protocol_fingerprint"), str)
        or _PROTOCOL_PATTERN.fullmatch(str(value.get("protocol_fingerprint"))) is None
        or not isinstance(source_hashes, Mapping)
        or set(source_hashes) != _PROVENANCE_MODULE_KEYS
        or any(
            not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None
            for digest in source_hashes.values()
        )
        or any(
            not isinstance(value.get(field), str)
            or _SHA256_PATTERN.fullmatch(str(value.get(field))) is None
            for field in (
                "evaluator_sha256",
                "semantic_gold_sha256",
                "benchmark_builder_sha256",
                "sample_sha256",
                "checkpoint_sha256",
            )
        )
        or any(
            not isinstance(count, int) or isinstance(count, bool) or count < 1
            for count in count_fields
        )
    ):
        raise CandidateGoldError("run manifest provenance is invalid or unsupported")
    normalized_hashes = {
        str(name): str(digest) for name, digest in source_hashes.items()
    }
    current_hashes = _current_repo_module_hashes()
    if any(
        normalized_hashes[name] != digest for name, digest in current_hashes.items()
    ):
        raise CandidateGoldError(
            "run manifest source modules do not match this checkout"
        )
    current_artifact_hashes = {
        "evaluator_sha256": _sha256_file(_EVALUATOR_PATH),
        "semantic_gold_sha256": _sha256_file(_SEMANTIC_GOLD_PATH),
        "benchmark_builder_sha256": _sha256_file(_BENCHMARK_BUILDER_PATH),
        "sample_sha256": _sha256_file(sample_path),
        "checkpoint_sha256": _sha256_file(checkpoint_path),
    }
    if any(value[name] != digest for name, digest in current_artifact_hashes.items()):
        raise CandidateGoldError("run manifest evidence artifacts do not match")
    if (
        value["sample_record_count"] != sample_record_count
        or value["checkpoint_row_count"] != checkpoint_row_count
    ):
        raise CandidateGoldError("run manifest evidence cohort counts do not match")
    return RunManifest(
        checkpoint_version=str(value["checkpoint_version"]),
        protocol_fingerprint=str(value["protocol_fingerprint"]),
        model=str(value["model"]),
        reasoning_effort=str(value["reasoning_effort"]),
        source_module_sha256=dict(sorted(normalized_hashes.items())),
        evaluator_sha256=str(value["evaluator_sha256"]),
        semantic_gold_sha256=str(value["semantic_gold_sha256"]),
        benchmark_builder_sha256=str(value["benchmark_builder_sha256"]),
        sample_sha256=str(value["sample_sha256"]),
        sample_record_count=int(value["sample_record_count"]),
        checkpoint_sha256=str(value["checkpoint_sha256"]),
        checkpoint_row_count=int(value["checkpoint_row_count"]),
    )


def _ratio(numerator: int | float, denominator: int) -> float:
    if denominator < 1:
        raise CandidateGoldError("metric has no denominator")
    return numerator / denominator


def _is_admitted(sample: Mapping[str, Any]) -> bool:
    return str(sample.get("stratum", "")).startswith(("important_", "durable_"))


def _runtime_batches(
    samples: list[dict[str, Any]],
) -> tuple[
    list[RuntimeBatch],
    dict[str, RuntimeCandidate],
    dict[str, tuple[RuntimeBatch, GmailTemporalCandidatePage]],
]:
    output: list[RuntimeBatch] = []
    candidates: dict[str, RuntimeCandidate] = {}
    pages: dict[str, tuple[RuntimeBatch, GmailTemporalCandidatePage]] = {}
    seen_samples: set[str] = set()
    for sample in samples:
        sample_id = sample.get("sample_id")
        text = sample.get("text")
        gold = sample.get("gold")
        if (
            not isinstance(sample_id, str)
            or not sample_id
            or sample_id in seen_samples
            or not isinstance(text, str)
            or not isinstance(gold, Mapping)
            or gold.get("semantic_schema_version") != "gmail_temporal_semantic_gold_v1"
            or gold.get("unmatched_candidates") != "unsupported"
        ):
            raise CandidateGoldError("sample or embedded semantic gold is malformed")
        seen_samples.add(sample_id)
        admitted = _is_admitted(sample)
        rescued = not admitted and str(sample.get("stratum", "")).startswith(
            "suppressed_"
        )
        analysis = analyze_gmail_temporal_leads(
            text=text,
            message_internal_at=sample.get("message_internal_at"),
            fact_admitted=admitted,
            temporal_review_rescue=rescued,
            chunk_id=sample_id,
        )
        expected_expressions = sample.get("expressions")
        expected_mentions = sample.get("mentions")
        if not isinstance(expected_expressions, list) or not isinstance(
            expected_mentions, list
        ):
            raise CandidateGoldError("sample endpoint inventory is missing")
        if {item.expression_id for item in analysis.expressions} != {
            item.get("expression_id") for item in expected_expressions
        } or {item.mention_id for item in analysis.mentions} != {
            item.get("mention_id") for item in expected_mentions
        }:
            raise CandidateGoldError("sample endpoint inventory is stale")
        plan = plan_gmail_temporal_selector_batches(text=text, analysis=analysis)
        expressions = {item.expression_id: item for item in analysis.expressions}
        mentions = {item.mention_id: item for item in analysis.mentions}
        for batch in plan.batches:
            frontier = build_gmail_temporal_candidate_frontier(
                analysis=analysis,
                batch=batch,
            )
            page_plan = plan_gmail_temporal_candidate_pages(
                analysis=analysis,
                batch=batch,
                max_clusters_per_page=4,
                max_candidates_per_page=12,
                max_payload_bytes=12_000,
            )
            page_payload_bytes = tuple(
                (
                    page.page_fingerprint,
                    len(
                        gmail_temporal_candidate_page_payload(
                            frontier=frontier,
                            page=page,
                        ).encode("utf-8")
                    ),
                )
                for page in page_plan.pages
            )
            runtime_candidates: list[RuntimeCandidate] = []
            for candidate in frontier.candidates:
                expression = expressions[candidate.expression_id]
                subject = mentions[candidate.subject_mention_id]
                lifecycle = (
                    mentions[candidate.lifecycle_mention_id]
                    if candidate.lifecycle_mention_id is not None
                    else None
                )
                runtime = RuntimeCandidate(
                    sample_id=sample_id,
                    candidate=candidate,
                    expression_surface=text[expression.start : expression.end],
                    expression_form=expression.form,
                    expression_field=expression.field,
                    subject_surface=text[subject.start : subject.end],
                    subject_type=subject.mention_type,
                    subject_field=subject.field,
                    lifecycle_surface=(
                        text[lifecycle.start : lifecycle.end]
                        if lifecycle is not None
                        else None
                    ),
                    lifecycle_role=(
                        lifecycle.lifecycle_role if lifecycle is not None else None
                    ),
                    lifecycle_field=lifecycle.field if lifecycle is not None else None,
                )
                candidate_id = candidate.candidate_id
                if candidate_id in candidates:
                    raise CandidateGoldError("candidate IDs are not cohort-unique")
                candidates[candidate_id] = runtime
                runtime_candidates.append(runtime)
            runtime_batch = RuntimeBatch(
                sample_id=sample_id,
                analysis=analysis,
                batch=batch,
                pages=page_plan.pages,
                candidates=tuple(runtime_candidates),
                plan_fingerprint=plan.plan_fingerprint,
                candidate_page_plan_fingerprint=page_plan.plan_fingerprint,
                candidate_page_payload_bytes=page_payload_bytes,
            )
            output.append(runtime_batch)
            for page in page_plan.pages:
                if page.page_fingerprint in pages:
                    raise CandidateGoldError("page fingerprints are not cohort-unique")
                pages[page.page_fingerprint] = (runtime_batch, page)
    return output, candidates, pages


def _locator_matches(locator: Mapping[str, Any], runtime: RuntimeCandidate) -> bool:
    if set(locator) != {"expression", "subject", "lifecycle_mention", "derived"}:
        raise CandidateGoldError("candidate locator has invalid fields")
    expression = locator.get("expression")
    subject = locator.get("subject")
    lifecycle = locator.get("lifecycle_mention")
    derived = locator.get("derived")
    if not all(isinstance(item, Mapping) for item in (expression, subject, derived)):
        raise CandidateGoldError("candidate locator endpoint is malformed")
    if set(expression) != {"surface", "form", "field"} or set(subject) != {
        "surface",
        "mention_type",
        "field",
    }:
        raise CandidateGoldError("candidate locator endpoint has invalid fields")
    if lifecycle is not None and (
        not isinstance(lifecycle, Mapping)
        or set(lifecycle) != {"surface", "lifecycle_role", "field"}
    ):
        raise CandidateGoldError("candidate lifecycle locator is malformed")
    if set(derived) != {
        "relation",
        "kind",
        "lifecycle",
        "normalized_value",
        "requires_defer",
    }:
        raise CandidateGoldError("candidate derived locator has invalid fields")
    candidate = runtime.candidate
    return (
        expression.get("surface") == runtime.expression_surface
        and expression.get("form") == runtime.expression_form
        and expression.get("field") == runtime.expression_field
        and subject.get("surface") == runtime.subject_surface
        and subject.get("mention_type") == runtime.subject_type
        and subject.get("field") == runtime.subject_field
        and (
            lifecycle is None
            and runtime.lifecycle_surface is None
            or isinstance(lifecycle, Mapping)
            and lifecycle.get("surface") == runtime.lifecycle_surface
            and lifecycle.get("lifecycle_role") == runtime.lifecycle_role
            and lifecycle.get("field") == runtime.lifecycle_field
        )
        and derived.get("relation") == candidate.relation
        and derived.get("kind") == candidate.kind
        and derived.get("lifecycle") == candidate.lifecycle
        and derived.get("normalized_value") == candidate.normalized_value
        and derived.get("requires_defer") == candidate.requires_defer
    )


def _compile_gold(
    samples: list[dict[str, Any]],
    candidates: Mapping[str, RuntimeCandidate],
) -> tuple[GoldUnit, ...]:
    by_sample: dict[str, list[RuntimeCandidate]] = defaultdict(list)
    for candidate in candidates.values():
        by_sample[candidate.sample_id].append(candidate)
    units: list[GoldUnit] = []
    seen_units: set[tuple[str, str]] = set()
    seen_members: set[tuple[str, str, str]] = set()
    candidate_memberships: dict[str, set[tuple[str, str, str]]] = defaultdict(set)
    for sample in samples:
        sample_id = str(sample["sample_id"])
        raw_units = sample["gold"].get("semantic_units")
        if not isinstance(raw_units, list):
            raise CandidateGoldError("semantic units must be a list")
        for raw_unit in raw_units:
            if not isinstance(raw_unit, Mapping) or set(raw_unit) != {
                "unit_id",
                "truth",
                "baseline_frontier_grade",
                "members",
            }:
                raise CandidateGoldError("semantic unit is malformed")
            unit_id = raw_unit.get("unit_id")
            baseline = raw_unit.get("baseline_frontier_grade")
            raw_members = raw_unit.get("members")
            unit_key = (sample_id, str(unit_id))
            if (
                not isinstance(unit_id, str)
                or not unit_id
                or unit_key in seen_units
                or baseline not in _GRADE_VALUE
                or not isinstance(raw_unit.get("truth"), str)
                or not isinstance(raw_members, list)
                or not raw_members
            ):
                raise CandidateGoldError("semantic unit identity is invalid")
            seen_units.add(unit_key)
            members: list[GoldMember] = []
            for raw_member in raw_members:
                if not isinstance(raw_member, Mapping) or set(raw_member) != {
                    "member_id",
                    "expected_verdict",
                    "baseline_frontier_grade",
                    "alternatives",
                }:
                    raise CandidateGoldError("semantic member is malformed")
                member_id = raw_member.get("member_id")
                expected_verdict = raw_member.get("expected_verdict")
                member_baseline = raw_member.get("baseline_frontier_grade")
                alternatives = raw_member.get("alternatives")
                member_key = (sample_id, unit_id, str(member_id))
                if (
                    not isinstance(member_id, str)
                    or not member_id
                    or member_key in seen_members
                    or expected_verdict not in {"supported", "uncertain"}
                    or member_baseline not in _GRADE_VALUE
                    or not isinstance(alternatives, list)
                    or not alternatives
                ):
                    raise CandidateGoldError("semantic member identity is invalid")
                seen_members.add(member_key)
                compiled_alternatives: list[GoldAlternative] = []
                for raw_alternative in alternatives:
                    if (
                        not isinstance(raw_alternative, Mapping)
                        or set(raw_alternative)
                        != {"quality", "expected_verdict", "locator"}
                        or raw_alternative.get("quality") not in _QUALITY_SCORE
                        or raw_alternative.get("expected_verdict")
                        not in {"supported", "uncertain"}
                        or not isinstance(raw_alternative.get("locator"), Mapping)
                    ):
                        raise CandidateGoldError("semantic alternative is malformed")
                    quality = str(raw_alternative["quality"])
                    alternative_expected_verdict = str(
                        raw_alternative["expected_verdict"]
                    )
                    if (
                        quality == "partial"
                        and alternative_expected_verdict != "uncertain"
                    ) or (
                        quality == "exact"
                        and alternative_expected_verdict != expected_verdict
                    ):
                        raise CandidateGoldError(
                            "semantic alternative verdict calibration is inconsistent"
                        )
                    matches = frozenset(
                        item.candidate.candidate_id
                        for item in by_sample[sample_id]
                        if _locator_matches(raw_alternative["locator"], item)
                    )
                    if len(matches) > 1:
                        raise CandidateGoldError(
                            "semantic locator is ambiguous within its sample"
                        )
                    for candidate_id in matches:
                        candidate_memberships[candidate_id].add(member_key)
                    compiled_alternatives.append(
                        GoldAlternative(
                            quality=quality,
                            expected_verdict=alternative_expected_verdict,
                            candidate_ids=matches,
                        )
                    )
                members.append(
                    GoldMember(
                        key=member_key,
                        expected_verdict=str(expected_verdict),
                        baseline_grade=str(member_baseline),
                        alternatives=tuple(compiled_alternatives),
                    )
                )
            units.append(
                GoldUnit(
                    key=unit_key,
                    baseline_grade=str(baseline),
                    members=tuple(members),
                )
            )
    if any(len(values) > 1 for values in candidate_memberships.values()):
        raise CandidateGoldError("one candidate satisfies multiple semantic members")
    return tuple(units)


def _page_case_id(
    runtime_batch: RuntimeBatch,
    page: GmailTemporalCandidatePage,
) -> str:
    material = "\0".join(
        (
            runtime_batch.sample_id,
            runtime_batch.analysis.snapshot_fingerprint,
            runtime_batch.plan_fingerprint,
            page.page_fingerprint,
        )
    ).encode("utf-8")
    return "gtfc_" + hashlib.sha256(material).hexdigest()[:32]


def _checkpoint_verdicts(
    rows: list[dict[str, Any]],
    runtime_batches: list[RuntimeBatch],
    pages: Mapping[str, tuple[RuntimeBatch, GmailTemporalCandidatePage]],
    manifest: RunManifest,
) -> tuple[dict[str, str], dict[str, str]]:
    rows_by_page: dict[str, dict[str, Any]] = {}
    for row in rows:
        if set(row) != _CHECKPOINT_KEYS:
            raise CandidateGoldError("checkpoint schema is stale")
        page_fingerprint = row.get("page_fingerprint")
        if (
            not isinstance(page_fingerprint, str)
            or page_fingerprint not in pages
            or page_fingerprint in rows_by_page
        ):
            raise CandidateGoldError("checkpoint contains a stale or duplicate page")
        runtime_batch, page = pages[page_fingerprint]
        batch = runtime_batch.batch
        payload_bytes = dict(runtime_batch.candidate_page_payload_bytes)[
            page.page_fingerprint
        ]
        if (
            row.get("version") != manifest.checkpoint_version
            or row.get("protocol_fingerprint") != manifest.protocol_fingerprint
            or row.get("source_module_sha256") != dict(manifest.source_module_sha256)
            or row.get("sample_id") != runtime_batch.sample_id
            or row.get("source_sha256") != runtime_batch.analysis.source_sha256
            or row.get("plan_fingerprint") != runtime_batch.plan_fingerprint
            or row.get("page_case_id") != _page_case_id(runtime_batch, page)
            or row.get("analysis_fingerprint")
            != runtime_batch.analysis.snapshot_fingerprint
            or row.get("batch_fingerprint") != batch.manifest.batch_fingerprint
            or row.get("frontier_fingerprint") != page.frontier_fingerprint
            or row.get("candidate_page_plan_fingerprint")
            != runtime_batch.candidate_page_plan_fingerprint
            or row.get("candidate_page_payload_bytes") != payload_bytes
            or row.get("page_sequence") != page.sequence
            or row.get("batch_sequence") != batch.sequence
            or row.get("page_count") != len(runtime_batch.pages)
        ):
            raise CandidateGoldError(
                "checkpoint provenance or fingerprint binding is stale"
            )
        raw_verdicts = row.get("verdicts")
        if not isinstance(raw_verdicts, list):
            raise CandidateGoldError("checkpoint verdicts are malformed")
        expected_ids = {
            candidate_id
            for cluster in page.clusters
            for candidate_id in cluster.candidate_ids
        }
        actual_ids = [
            item.get("candidate_id")
            for item in raw_verdicts
            if isinstance(item, Mapping)
        ]
        if (
            len(actual_ids) != len(raw_verdicts)
            or len(actual_ids) != len(set(actual_ids))
            or set(actual_ids) != expected_ids
            or any(
                set(item) != {"candidate_id", "verdict"}
                or item.get("verdict") not in _VERDICTS
                for item in raw_verdicts
                if isinstance(item, Mapping)
            )
        ):
            raise CandidateGoldError("checkpoint does not cover its page exactly")
        rows_by_page[page_fingerprint] = row
    if set(rows_by_page) != set(pages):
        raise CandidateGoldError("checkpoint does not cover the page cohort exactly")

    raw_verdict_by_candidate: dict[str, str] = {}
    effective_verdict_by_candidate: dict[str, str] = {}
    for runtime_batch in runtime_batches:
        if not runtime_batch.pages:
            continue
        typed_rows = tuple(
            GmailTemporalCandidatePageVerdicts(
                frontier_fingerprint=page.frontier_fingerprint,
                page_fingerprint=page.page_fingerprint,
                verdicts=tuple(
                    GmailTemporalCandidateVerdict(
                        candidate_id=str(item["candidate_id"]),
                        verdict=str(item["verdict"]),  # type: ignore[arg-type]
                    )
                    for item in rows_by_page[page.page_fingerprint]["verdicts"]
                ),
            )
            for page in runtime_batch.pages
        )
        plan = plan_gmail_temporal_candidate_pages(
            analysis=runtime_batch.analysis,
            batch=runtime_batch.batch,
            max_clusters_per_page=4,
            max_candidates_per_page=12,
            max_payload_bytes=12_000,
        )
        verdict_set = validate_gmail_temporal_candidate_verdict_set(
            analysis=runtime_batch.analysis,
            batch=runtime_batch.batch,
            plan=plan,
            rows=typed_rows,
        )
        supported_ids = set(verdict_set.supported_candidate_ids)
        uncertain_ids = {
            candidate_id
            for uncertainty in verdict_set.uncertain_clusters
            for candidate_id in uncertainty.plausible_candidate_ids
        }
        if supported_ids & uncertain_ids:
            raise CandidateGoldError("effective verdict sets overlap")
        for row in typed_rows:
            for verdict in row.verdicts:
                if verdict.candidate_id in raw_verdict_by_candidate:
                    raise CandidateGoldError("checkpoint repeats a candidate")
                raw_verdict_by_candidate[verdict.candidate_id] = verdict.verdict
                effective_verdict_by_candidate[verdict.candidate_id] = (
                    "supported"
                    if verdict.candidate_id in supported_ids
                    else "uncertain"
                    if verdict.candidate_id in uncertain_ids
                    else "unsupported"
                )
    expected_candidates = {
        item.candidate.candidate_id
        for batch in runtime_batches
        for item in batch.candidates
    }
    if (
        set(raw_verdict_by_candidate) != expected_candidates
        or set(effective_verdict_by_candidate) != expected_candidates
    ):
        raise CandidateGoldError("checkpoint omits one or more candidates")
    return raw_verdict_by_candidate, effective_verdict_by_candidate


def _member_matches(member: GoldMember) -> dict[str, float]:
    output: dict[str, float] = {}
    for alternative in member.alternatives:
        for candidate_id in alternative.candidate_ids:
            output[candidate_id] = max(
                output.get(candidate_id, 0.0),
                _QUALITY_SCORE[alternative.quality],
            )
    return output


def _member_expected_verdicts(member: GoldMember) -> dict[str, str]:
    output: dict[str, str] = {}
    for alternative in member.alternatives:
        for candidate_id in alternative.candidate_ids:
            existing = output.get(candidate_id)
            if existing is not None and existing != alternative.expected_verdict:
                raise CandidateGoldError(
                    "one semantic candidate has conflicting verdict calibration"
                )
            output[candidate_id] = alternative.expected_verdict
    return output


def _artifact_hypothesis(runtime: RuntimeCandidate) -> ArtifactHypothesis:
    candidate = runtime.candidate
    return (
        candidate.expression_id,
        candidate.relation,
        candidate.kind,
        candidate.lifecycle,
        candidate.normalized_value,
    )


def _hypothesis_sort_key(value: ArtifactHypothesis) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _production_artifacts(
    runtime_batches: list[RuntimeBatch],
    candidates: Mapping[str, RuntimeCandidate],
    verdicts: Mapping[str, str],
) -> tuple[ProductionArtifact, ...]:
    """Project effective verdicts into the artifacts a consumer actually sees.

    Every supported citation remains its own artifact.  All uncertain candidates
    in one validated parent cluster form one sidecar, and aliases with the same
    semantic signature collapse to one hypothesis inside that sidecar.
    """

    candidate_to_cluster: dict[str, str] = {}
    for runtime_batch in runtime_batches:
        for page in runtime_batch.pages:
            for cluster in page.clusters:
                for candidate_id in cluster.candidate_ids:
                    previous = candidate_to_cluster.setdefault(
                        candidate_id,
                        cluster.cluster_id,
                    )
                    if previous != cluster.cluster_id:
                        raise CandidateGoldError(
                            "candidate belongs to multiple parent clusters"
                        )
    if set(candidate_to_cluster) != set(candidates):
        raise CandidateGoldError("parent clusters do not cover candidate authority")

    artifacts: list[ProductionArtifact] = []
    for candidate_id in sorted(
        candidate_id
        for candidate_id, verdict in verdicts.items()
        if verdict == "supported"
    ):
        hypothesis = _artifact_hypothesis(candidates[candidate_id])
        artifacts.append(
            ProductionArtifact(
                artifact_id=f"supported:{candidate_id}",
                kind="supported_citation",
                candidate_ids=(candidate_id,),
                hypotheses=(hypothesis,),
            )
        )

    uncertain_by_cluster: dict[str, list[str]] = defaultdict(list)
    for candidate_id, verdict in verdicts.items():
        if verdict == "uncertain":
            uncertain_by_cluster[candidate_to_cluster[candidate_id]].append(
                candidate_id
            )
    for cluster_id in sorted(uncertain_by_cluster):
        candidate_ids = tuple(sorted(uncertain_by_cluster[cluster_id]))
        hypotheses = tuple(
            sorted(
                {
                    _artifact_hypothesis(candidates[candidate_id])
                    for candidate_id in candidate_ids
                },
                key=_hypothesis_sort_key,
            )
        )
        artifacts.append(
            ProductionArtifact(
                artifact_id=f"uncertainty:{cluster_id}",
                kind="uncertainty_sidecar",
                candidate_ids=candidate_ids,
                hypotheses=hypotheses,
            )
        )
    return tuple(artifacts)


def _artifact_match_edges(
    artifacts: tuple[ProductionArtifact, ...],
    units: tuple[GoldUnit, ...],
    candidates: Mapping[str, RuntimeCandidate],
) -> tuple[
    dict[str, ArtifactMatchEdge],
    set[str],
    dict[str, int],
]:
    """Compile each artifact's sole permissible semantic edge.

    An uncertainty sidecar is pure only when every distinct hypothesis maps to
    exactly the same gold member.  Candidate aliases within one hypothesis are
    deliberately collapsed before this check.
    """

    candidate_memberships: dict[
        str,
        list[tuple[tuple[str, str, str], float, str]],
    ] = defaultdict(list)
    for unit in units:
        for member in unit.members:
            expected = _member_expected_verdicts(member)
            for candidate_id, quality in _member_matches(member).items():
                candidate_memberships[candidate_id].append(
                    (member.key, quality, expected[candidate_id])
                )

    edges: dict[str, ArtifactMatchEdge] = {}
    pure_sidecars: set[str] = set()
    sidecar_unmatched_hypotheses: dict[str, int] = {}
    for artifact in artifacts:
        if artifact.kind == "supported_citation":
            memberships = candidate_memberships.get(artifact.candidate_ids[0], [])
            valid = [
                (member_key, quality)
                for member_key, quality, expected_verdict in memberships
                if quality == 1.0 and expected_verdict == "supported"
            ]
            if len(valid) > 1:
                raise CandidateGoldError(
                    "one supported citation satisfies multiple semantic members"
                )
            if valid:
                member_key, quality = valid[0]
                edges[artifact.artifact_id] = ArtifactMatchEdge(
                    artifact_id=artifact.artifact_id,
                    member_key=member_key,
                    quality=quality,
                    priority=0,
                )
            continue

        candidates_by_hypothesis: dict[ArtifactHypothesis, list[str]] = defaultdict(
            list
        )
        for candidate_id in artifact.candidate_ids:
            candidates_by_hypothesis[
                _artifact_hypothesis(candidates[candidate_id])
            ].append(candidate_id)
        if set(candidates_by_hypothesis) != set(artifact.hypotheses):
            raise CandidateGoldError("uncertainty hypotheses are internally stale")

        hypothesis_members: list[dict[tuple[str, str, str], float]] = []
        unmatched_hypotheses = 0
        for hypothesis in artifact.hypotheses:
            quality_by_member: dict[tuple[str, str, str], list[float]] = defaultdict(
                list
            )
            has_unmatched_alias = False
            for candidate_id in candidates_by_hypothesis[hypothesis]:
                memberships = candidate_memberships.get(candidate_id, [])
                if not memberships:
                    has_unmatched_alias = True
                    continue
                for member_key, quality, _ in memberships:
                    quality_by_member[member_key].append(quality)
            if has_unmatched_alias or not quality_by_member:
                unmatched_hypotheses += 1
                member_scores: dict[tuple[str, str, str], float] = {}
            else:
                # Candidate IDs sharing one semantic signature remain distinct
                # grounding alternatives.  The collapsed hypothesis therefore
                # inherits its least-specific alias rather than allowing an
                # exact alias to conceal a partial one.
                member_scores = {
                    member_key: min(qualities)
                    for member_key, qualities in quality_by_member.items()
                }
            hypothesis_members.append(member_scores)
        sidecar_unmatched_hypotheses[artifact.artifact_id] = unmatched_hypotheses

        # Every hypothesis must authorize one and only one common member.  A
        # hypothesis that can mean two members is cross-member, not "close
        # enough"; an unmatched hypothesis likewise contaminates the sidecar.
        singleton_members = [
            next(iter(member_scores))
            for member_scores in hypothesis_members
            if len(member_scores) == 1
        ]
        if (
            len(singleton_members) != len(hypothesis_members)
            or len(set(singleton_members)) != 1
        ):
            continue
        member_key = singleton_members[0]
        # A sidecar is only as specific as its least-specific live hypothesis:
        # exact plus partial ambiguity must stay partial.
        quality = min(member_scores[member_key] for member_scores in hypothesis_members)
        pure_sidecars.add(artifact.artifact_id)
        edges[artifact.artifact_id] = ArtifactMatchEdge(
            artifact_id=artifact.artifact_id,
            member_key=member_key,
            quality=quality,
            priority=1 if quality == 1.0 else 2,
        )
    return edges, pure_sidecars, sidecar_unmatched_hypotheses


def _match_production_artifacts(
    artifacts: tuple[ProductionArtifact, ...],
    units: tuple[GoldUnit, ...],
    candidates: Mapping[str, RuntimeCandidate],
) -> dict[str, Any]:
    """Deterministic maximum matching under the release preference order.

    Gold compilation and the sidecar-purity rule leave each artifact with at
    most one semantic edge.  Sorting those edges by exact-supported, then
    exact-uncertainty, then partial-uncertainty therefore produces a
    maximum-cardinality one-to-one matching while resolving competition for a
    member in the required order.
    """

    edges, pure_sidecars, sidecar_unmatched_hypotheses = _artifact_match_edges(
        artifacts,
        units,
        candidates,
    )
    matched_members: dict[tuple[str, str, str], ArtifactMatchEdge] = {}
    matched_artifacts: set[str] = set()
    redundant_artifacts: set[str] = set()
    invalid_artifacts = {
        artifact.artifact_id
        for artifact in artifacts
        if artifact.artifact_id not in edges
    }
    for edge in sorted(
        edges.values(),
        key=lambda item: (item.priority, item.artifact_id, item.member_key),
    ):
        if edge.member_key in matched_members:
            redundant_artifacts.add(edge.artifact_id)
            continue
        matched_members[edge.member_key] = edge
        matched_artifacts.add(edge.artifact_id)

    scores_by_unit = [
        [
            (
                matched_members[member.key].quality
                if member.key in matched_members
                else 0.0
            )
            for member in unit.members
        ]
        for unit in units
    ]
    all_member_keys = {member.key for unit in units for member in unit.members}
    sidecars = tuple(
        artifact for artifact in artifacts if artifact.kind == "uncertainty_sidecar"
    )
    supported_artifacts = tuple(
        artifact for artifact in artifacts if artifact.kind == "supported_citation"
    )
    return {
        "scores_by_unit": scores_by_unit,
        "matched_member_keys": set(matched_members),
        "matched_member_quality": {
            member_key: edge.quality for member_key, edge in matched_members.items()
        },
        "missed_member_keys": all_member_keys - set(matched_members),
        "matched_artifact_ids": matched_artifacts,
        "redundant_artifact_ids": redundant_artifacts,
        "invalid_artifact_ids": invalid_artifacts,
        "artifact_count": len(artifacts),
        "supported_artifact_count": len(supported_artifacts),
        "sidecar_count": len(sidecars),
        "hypothesis_count": sum(len(artifact.hypotheses) for artifact in sidecars),
        "pure_sidecar_count": len(pure_sidecars),
        "impure_sidecar_count": len(sidecars) - len(pure_sidecars),
        "unmatched_hypothesis_count": sum(sidecar_unmatched_hypotheses.values()),
    }


def _selection_scores(
    units: tuple[GoldUnit, ...],
    selected: set[str],
) -> list[list[float]]:
    return [
        [
            max(
                (
                    score
                    for candidate_id, score in _member_matches(member).items()
                    if candidate_id in selected
                ),
                default=0.0,
            )
            for member in unit.members
        ]
        for unit in units
    ]


def _grade(score: float) -> str:
    if score == 1.0:
        return "exact"
    if score > 0.0:
        return "partial"
    return "absent"


def _recall_metrics(scores_by_unit: list[list[float]]) -> dict[str, float | int]:
    flat_scores = [score for unit_scores in scores_by_unit for score in unit_scores]
    unit_scores = [sum(scores) / len(scores) for scores in scores_by_unit]
    exact_units = sum(
        all(score == 1.0 for score in scores) for scores in scores_by_unit
    )
    complete_units = sum(
        all(score > 0.0 for score in scores) for scores in scores_by_unit
    )
    any_units = sum(any(score > 0.0 for score in scores) for scores in scores_by_unit)
    return {
        "exact_members": sum(score == 1.0 for score in flat_scores),
        "recalled_members": sum(score > 0.0 for score in flat_scores),
        "exact_member_recall": _ratio(
            sum(score == 1.0 for score in flat_scores),
            len(flat_scores),
        ),
        "required_member_recall": _ratio(
            sum(score > 0.0 for score in flat_scores),
            len(flat_scores),
        ),
        "exact_units": exact_units,
        "complete_units": complete_units,
        "any_units": any_units,
        "exact_unit_recall": _ratio(exact_units, len(scores_by_unit)),
        "complete_unit_recall": _ratio(complete_units, len(scores_by_unit)),
        "any_unit_recall": _ratio(any_units, len(scores_by_unit)),
        "soft_unit_recall": _ratio(sum(unit_scores), len(unit_scores)),
    }


def evaluate(
    sample_path: Path,
    checkpoint_path: Path | None,
    run_manifest_path: Path | None,
    *,
    prevalidated_verdict_maps: tuple[
        Mapping[str, str],
        Mapping[str, str],
    ]
    | None = None,
    provenance_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    samples = _load_jsonl(sample_path)
    if prevalidated_verdict_maps is None:
        if checkpoint_path is None or run_manifest_path is None:
            raise CandidateGoldError(
                "checkpoint and manifest are required for a single-run evaluation"
            )
        checkpoint_rows = _load_jsonl(checkpoint_path)
        manifest = _load_run_manifest(
            run_manifest_path,
            sample_path=sample_path,
            sample_record_count=len(samples),
            checkpoint_path=checkpoint_path,
            checkpoint_row_count=len(checkpoint_rows),
        )
    else:
        if checkpoint_path is not None or run_manifest_path is not None:
            raise CandidateGoldError(
                "prevalidated verdict maps cannot be mixed with single-run evidence"
            )
        if (
            not isinstance(prevalidated_verdict_maps, tuple)
            or len(prevalidated_verdict_maps) != 2
            or not all(
                isinstance(values, Mapping) for values in prevalidated_verdict_maps
            )
            or not isinstance(provenance_override, Mapping)
            or provenance_override.get("single_run") is not False
        ):
            raise CandidateGoldError(
                "prevalidated verdict maps require explicit non-single-run provenance"
            )
        checkpoint_rows = []
        manifest = None
    runtime_batches, candidates, pages = _runtime_batches(samples)
    units = _compile_gold(samples, candidates)
    if not units:
        raise CandidateGoldError("benchmark contains no semantic units")
    expected_material_by_sample: dict[str, bool] = {}
    hard_negative_by_sample: dict[str, bool] = {}
    for sample in samples:
        sample_id = str(sample["sample_id"])
        expected_material = sample["gold"].get("expected_material")
        hard_negative = sample["gold"].get("hard_negative", False)
        if not isinstance(expected_material, bool):
            raise CandidateGoldError("record materiality gold is malformed")
        if not isinstance(hard_negative, bool) or (expected_material and hard_negative):
            raise CandidateGoldError("record hard-negative gold is malformed")
        expected_material_by_sample[sample_id] = expected_material
        hard_negative_by_sample[sample_id] = hard_negative
    if prevalidated_verdict_maps is None:
        if manifest is None:
            raise CandidateGoldError("single-run manifest is unavailable")
        raw_verdicts, verdicts = _checkpoint_verdicts(
            checkpoint_rows,
            runtime_batches,
            pages,
            manifest,
        )
        run_provenance: dict[str, Any] = {
            "manifest_version": RUN_MANIFEST_VERSION,
            "checkpoint_version": manifest.checkpoint_version,
            "protocol_fingerprint": manifest.protocol_fingerprint,
            "model": manifest.model,
            "reasoning_effort": manifest.reasoning_effort,
            "source_module_sha256": dict(manifest.source_module_sha256),
            "artifact_sha256": {
                "evaluator": manifest.evaluator_sha256,
                "semantic_gold": manifest.semantic_gold_sha256,
                "benchmark_builder": manifest.benchmark_builder_sha256,
                "sample": manifest.sample_sha256,
                "checkpoint": manifest.checkpoint_sha256,
            },
            "sample_record_count": manifest.sample_record_count,
            "checkpoint_row_count": manifest.checkpoint_row_count,
        }
    else:
        raw_verdicts = dict(prevalidated_verdict_maps[0])
        verdicts = dict(prevalidated_verdict_maps[1])
        expected_candidate_ids = set(candidates)
        if (
            set(raw_verdicts) != expected_candidate_ids
            or set(verdicts) != expected_candidate_ids
            or any(value not in _VERDICTS for value in raw_verdicts.values())
            or any(value not in _VERDICTS for value in verdicts.values())
        ):
            raise CandidateGoldError(
                "prevalidated verdict maps do not cover the candidate authority"
            )
        run_provenance = dict(provenance_override)
    frontier_ids = set(candidates)
    supported_ids = {
        candidate_id
        for candidate_id, verdict in verdicts.items()
        if verdict == "supported"
    }
    uncertain_ids = {
        candidate_id
        for candidate_id, verdict in verdicts.items()
        if verdict == "uncertain"
    }
    accepted_ids = supported_ids | uncertain_ids
    raw_supported_ids = {
        candidate_id
        for candidate_id, verdict in raw_verdicts.items()
        if verdict == "supported"
    }
    raw_uncertain_ids = {
        candidate_id
        for candidate_id, verdict in raw_verdicts.items()
        if verdict == "uncertain"
    }

    artifacts = _production_artifacts(runtime_batches, candidates, verdicts)
    artifact_scores = _match_production_artifacts(artifacts, units, candidates)
    supported_artifacts = tuple(
        artifact for artifact in artifacts if artifact.kind == "supported_citation"
    )
    supported_artifact_scores = _match_production_artifacts(
        supported_artifacts,
        units,
        candidates,
    )
    negative_artifact_ids = {
        artifact.artifact_id
        for artifact in artifacts
        if artifact.candidate_ids
        and not expected_material_by_sample[
            candidates[artifact.candidate_ids[0]].sample_id
        ]
    }
    material_invalid_artifact_ids = set(artifact_scores["invalid_artifact_ids"]) - (
        negative_artifact_ids
    )
    sidecar_artifact_ids = {
        artifact.artifact_id
        for artifact in artifacts
        if artifact.kind == "uncertainty_sidecar"
    }
    supported_artifact_ids = {
        artifact.artifact_id
        for artifact in artifacts
        if artifact.kind == "supported_citation"
    }
    material_impure_sidecar_ids = material_invalid_artifact_ids & sidecar_artifact_ids
    material_supported_overclaim_ids = (
        material_invalid_artifact_ids & supported_artifact_ids
    )

    frontier_scores = _selection_scores(units, frontier_ids)
    supported_scores = supported_artifact_scores["scores_by_unit"]
    review_scores = artifact_scores["scores_by_unit"]
    frontier_unit_scores = [sum(scores) / len(scores) for scores in frontier_scores]
    review_unit_scores = [sum(scores) / len(scores) for scores in review_scores]
    unit_ratchet_regressions = sum(
        _GRADE_VALUE[_grade(score)] < _GRADE_VALUE[unit.baseline_grade]
        for unit, score in zip(units, frontier_unit_scores, strict=True)
    )
    member_ratchet_regressions: list[str] = []
    for unit, member_scores in zip(units, frontier_scores, strict=True):
        for member, score in zip(unit.members, member_scores, strict=True):
            if _GRADE_VALUE[_grade(score)] < _GRADE_VALUE[member.baseline_grade]:
                member_ratchet_regressions.append(":".join(member.key))
    frontier_upgrades = [
        f"{unit.key[0]}:{unit.key[1]}"
        for unit, score in zip(units, frontier_unit_scores, strict=True)
        if _GRADE_VALUE[_grade(score)] > _GRADE_VALUE[unit.baseline_grade]
    ]
    frontier_non_absent = sum(score > 0 for score in frontier_unit_scores)
    review_recovered = sum(
        frontier_score > 0 and review_score > 0
        for frontier_score, review_score in zip(
            frontier_unit_scores, review_unit_scores, strict=True
        )
    )

    matches_by_candidate: dict[str, list[tuple[GoldMember, float]]] = defaultdict(list)
    expected_verdict_by_candidate: dict[str, str] = {}
    duplicate_alias_count = 0
    calibration_mismatches = 0
    calibration_mismatch_members: list[str] = []
    for unit in units:
        for member in unit.members:
            matches = _member_matches(member)
            expected_by_candidate = _member_expected_verdicts(member)
            for candidate_id, quality in matches.items():
                matches_by_candidate[candidate_id].append((member, quality))
                existing_expected = expected_verdict_by_candidate.get(candidate_id)
                if (
                    existing_expected is not None
                    and existing_expected != expected_by_candidate[candidate_id]
                ):
                    raise CandidateGoldError(
                        "one candidate has conflicting semantic verdict calibration"
                    )
                expected_verdict_by_candidate[candidate_id] = expected_by_candidate[
                    candidate_id
                ]
            selected = [
                candidate_id for candidate_id in matches if candidate_id in accepted_ids
            ]
            if selected:
                duplicate_alias_count += len(selected) - 1
                representative = max(
                    selected,
                    key=lambda candidate_id: (
                        matches[candidate_id],
                        verdicts[candidate_id] == expected_by_candidate[candidate_id],
                        verdicts[candidate_id] == "supported",
                        candidate_id,
                    ),
                )
                expected_verdict = expected_by_candidate[representative]
                if verdicts[representative] != expected_verdict:
                    calibration_mismatches += 1
                    calibration_mismatch_members.append(
                        ":".join(
                            (
                                *member.key,
                                expected_verdict,
                                verdicts[representative],
                            )
                        )
                    )

    correct_supported = 0
    raw_correct_supported = 0
    correct_review = 0
    correct_uncertain = 0
    expected_supported_members = 0
    supported_to_uncertain_members: list[str] = []
    default_negative_supported = 0
    default_negative_accepted = 0
    for unit in units:
        for member in unit.members:
            matches = _member_matches(member)
            expected_by_candidate = _member_expected_verdicts(member)
            member_supported = any(
                candidate_id in supported_ids
                and expected_by_candidate[candidate_id] == "supported"
                for candidate_id in matches
            )
            correct_supported += int(member_supported)
            raw_correct_supported += int(
                any(
                    candidate_id in raw_supported_ids
                    and expected_by_candidate[candidate_id] == "supported"
                    for candidate_id in matches
                )
            )
            if member.expected_verdict == "supported":
                expected_supported_members += 1
                if not member_supported and any(
                    candidate_id in uncertain_ids for candidate_id in matches
                ):
                    supported_to_uncertain_members.append(":".join(member.key))
            correct_review += int(
                any(
                    (
                        candidate_id in supported_ids
                        and expected_by_candidate[candidate_id] == "supported"
                    )
                    or candidate_id in uncertain_ids
                    and quality in {0.5, 1.0}
                    for candidate_id, quality in matches.items()
                )
            )
            correct_uncertain += int(
                any(
                    candidate_id in uncertain_ids and quality in {0.5, 1.0}
                    for candidate_id, quality in matches.items()
                )
            )
    for candidate_id in supported_ids:
        if candidate_id not in matches_by_candidate:
            default_negative_supported += 1
            default_negative_accepted += 1
    for candidate_id in uncertain_ids:
        if candidate_id not in matches_by_candidate:
            default_negative_accepted += 1

    units_by_sample: dict[str, list[int]] = defaultdict(list)
    for index, unit in enumerate(units):
        units_by_sample[unit.key[0]].append(index)
    useful_records = 0
    recalled_useful_records = 0
    selected_noise_records = 0
    negative_records = 0
    hard_negative_records = 0
    supported_hard_negative_records = 0
    supported_hard_negative_artifacts = 0
    supported_negative_records = 0
    supported_negative_artifacts = 0
    material_default_negative_supported = 0
    material_default_negative_accepted = 0
    for sample in samples:
        sample_id = str(sample["sample_id"])
        expected_material = expected_material_by_sample[sample_id]
        hard_negative = hard_negative_by_sample[sample_id]
        sample_candidate_ids = {
            candidate_id
            for candidate_id, runtime in candidates.items()
            if runtime.sample_id == sample_id
        }
        if expected_material:
            useful_records += 1
            recalled_useful_records += int(
                any(
                    any(score > 0 for score in review_scores[index])
                    for index in units_by_sample[sample_id]
                )
            )
        else:
            negative_records += 1
            accepted = bool(sample_candidate_ids & accepted_ids)
            selected_noise_records += int(accepted)
            supported_count = len(sample_candidate_ids & supported_ids)
            supported_negative_artifacts += supported_count
            supported_negative_records += int(supported_count > 0)
            if hard_negative:
                hard_negative_records += 1
                supported_hard_negative_artifacts += supported_count
                supported_hard_negative_records += int(supported_count > 0)

    for candidate_id in supported_ids:
        runtime = candidates[candidate_id]
        if (
            expected_material_by_sample[runtime.sample_id]
            and candidate_id not in matches_by_candidate
        ):
            material_default_negative_supported += 1
            material_default_negative_accepted += 1
    for candidate_id in uncertain_ids:
        runtime = candidates[candidate_id]
        if (
            expected_material_by_sample[runtime.sample_id]
            and candidate_id not in matches_by_candidate
        ):
            material_default_negative_accepted += 1

    frontier_metrics = _recall_metrics(frontier_scores)
    supported_metrics = _recall_metrics(supported_scores)
    review_metrics = _recall_metrics(review_scores)
    matched_supported_artifacts = len(supported_artifact_scores["matched_artifact_ids"])
    matched_effective_artifacts = len(artifact_scores["matched_artifact_ids"])
    supported_artifact_count = int(artifact_scores["supported_artifact_count"])
    effective_artifact_count = int(artifact_scores["artifact_count"])
    supported_artifact_precision = (
        matched_supported_artifacts / supported_artifact_count
        if supported_artifact_count
        else 1.0
    )
    effective_artifact_precision = (
        matched_effective_artifacts / effective_artifact_count
        if effective_artifact_count
        else 1.0
    )
    supported_member_count = len(supported_artifact_scores["matched_member_keys"])
    effective_member_count = len(artifact_scores["matched_member_keys"])
    semantic_member_count = sum(len(unit.members) for unit in units)
    supported_member_recall = _ratio(
        supported_member_count,
        expected_supported_members,
    )
    effective_member_recall = _ratio(
        effective_member_count,
        semantic_member_count,
    )
    # Backward-compatible names now deliberately expose artifact-level values.
    strict_supported_precision = supported_artifact_precision
    recall_arm_precision = effective_artifact_precision
    uncertain_truth_precision = (
        _ratio(correct_uncertain, len(uncertain_ids)) if uncertain_ids else None
    )
    supported_required_member_recall = supported_member_recall
    supported_to_uncertain_rate = _ratio(
        len(supported_to_uncertain_members),
        expected_supported_members,
    )
    critical_calibration_error_candidates = sorted(
        candidate_id
        for candidate_id in supported_ids
        if expected_verdict_by_candidate.get(candidate_id) == "uncertain"
    )
    raw_critical_calibration_error_candidates = sorted(
        candidate_id
        for candidate_id in raw_supported_ids
        if expected_verdict_by_candidate.get(candidate_id) == "uncertain"
    )
    supported_overclaim_count = len(supported_artifact_scores["invalid_artifact_ids"])
    raw_supported_overclaim_count = len(raw_supported_ids) - raw_correct_supported
    effective_verdict_changes = {
        (raw_verdicts[candidate_id], verdicts[candidate_id])
        for candidate_id in verdicts
        if raw_verdicts[candidate_id] != verdicts[candidate_id]
    }
    effective_verdict_change_count = sum(
        raw_verdicts[candidate_id] != verdicts[candidate_id]
        for candidate_id in verdicts
    )
    default_negative_count = len(candidates) - len(matches_by_candidate)
    useful_record_recall = _ratio(recalled_useful_records, useful_records)
    accepted_negative_review_rate = (
        selected_noise_records / negative_records if negative_records else 0.0
    )
    gates = {
        "end_to_end_any_recall": review_metrics["any_unit_recall"]
        >= MIN_END_TO_END_ANY_RECALL,
        "end_to_end_required_member_recall": review_metrics["required_member_recall"]
        >= MIN_END_TO_END_REQUIRED_MEMBER_RECALL,
        "end_to_end_complete_unit_recall": review_metrics["complete_unit_recall"]
        >= MIN_END_TO_END_COMPLETE_UNIT_RECALL,
        "end_to_end_exact_unit_recall": review_metrics["exact_unit_recall"]
        >= MIN_END_TO_END_EXACT_UNIT_RECALL,
        "useful_record_recall": useful_record_recall >= MIN_USEFUL_RECORD_RECALL,
        "supported_required_member_recall": supported_required_member_recall
        >= MIN_SUPPORTED_REQUIRED_MEMBER_RECALL,
        "supported_member_recall": supported_member_recall
        >= MIN_SUPPORTED_REQUIRED_MEMBER_RECALL,
        "supported_to_uncertain_rate": supported_to_uncertain_rate
        <= MAX_SUPPORTED_TO_UNCERTAIN_RATE,
        "strict_supported_precision": strict_supported_precision
        >= MIN_STRICT_SUPPORTED_PRECISION,
        "supported_artifact_precision": supported_artifact_precision
        >= MIN_STRICT_SUPPORTED_PRECISION,
        "recall_arm_precision": recall_arm_precision >= MIN_RECALL_ARM_PRECISION,
        "effective_member_recall": effective_member_recall
        >= MIN_END_TO_END_REQUIRED_MEMBER_RECALL,
        "effective_artifact_precision": effective_artifact_precision
        >= MIN_RECALL_ARM_PRECISION,
        "uncertainty_hypothesis_purity": not material_impure_sidecar_ids,
        "accepted_negative_review_rate": accepted_negative_review_rate
        <= MAX_ACCEPTED_NEGATIVE_REVIEW_RATE,
        "no_supported_hard_negative_artifacts": (
            supported_hard_negative_artifacts == 0
        ),
        "no_supported_negative_artifacts": supported_negative_artifacts == 0,
        "no_redundant_artifacts": not artifact_scores["redundant_artifact_ids"],
        # Compatibility key: aliases inside one sidecar hypothesis are no
        # longer duplicates, but a second production artifact for one member is.
        "no_duplicate_aliases": not artifact_scores["redundant_artifact_ids"],
        "no_supported_overclaims": (
            not material_supported_overclaim_ids
            and supported_negative_artifacts == 0
        ),
        "no_critical_calibration_errors": not critical_calibration_error_candidates,
        "no_default_negative_supported": default_negative_supported == 0,
        # Compatibility key, now evaluated at artifact/hypothesis granularity.
        "no_default_negative_accepted": material_default_negative_accepted == 0,
        "frontier_ratchet": (
            unit_ratchet_regressions == 0 and not member_ratchet_regressions
        ),
        "frontier_unit_ratchet": unit_ratchet_regressions == 0,
        "frontier_member_ratchet": not member_ratchet_regressions,
    }
    return {
        "run_provenance": run_provenance,
        "records": len(samples),
        "useful_records": useful_records,
        "recalled_useful_records": recalled_useful_records,
        "semantic_units": len(units),
        "semantic_members": sum(len(unit.members) for unit in units),
        "frontier_candidates": len(candidates),
        "verifier_pages": len(pages),
        "supported_candidates": len(supported_ids),
        "uncertain_candidates": len(uncertain_ids),
        "accepted_candidates": len(accepted_ids),
        "raw_supported_candidates": len(raw_supported_ids),
        "raw_uncertain_candidates": len(raw_uncertain_ids),
        "production_artifacts": effective_artifact_count,
        "supported_artifacts": supported_artifact_count,
        "uncertainty_sidecars": int(artifact_scores["sidecar_count"]),
        "uncertainty_hypotheses": int(artifact_scores["hypothesis_count"]),
        "pure_uncertainty_sidecars": int(artifact_scores["pure_sidecar_count"]),
        "impure_uncertainty_sidecars": int(artifact_scores["impure_sidecar_count"]),
        "material_impure_uncertainty_sidecars": len(material_impure_sidecar_ids),
        "unmatched_uncertainty_hypotheses": int(
            artifact_scores["unmatched_hypothesis_count"]
        ),
        "uncertainty_hypothesis_purity": (
            int(artifact_scores["pure_sidecar_count"])
            / int(artifact_scores["sidecar_count"])
            if int(artifact_scores["sidecar_count"])
            else 1.0
        ),
        "matched_artifacts": matched_effective_artifacts,
        "redundant_artifacts": len(artifact_scores["redundant_artifact_ids"]),
        "unmatched_artifacts": len(artifact_scores["invalid_artifact_ids"]),
        "material_unmatched_artifacts": len(material_invalid_artifact_ids),
        "supported_redundant_artifacts": len(
            supported_artifact_scores["redundant_artifact_ids"]
        ),
        "supported_unmatched_artifacts": len(
            supported_artifact_scores["invalid_artifact_ids"]
        ),
        "missed_members": len(artifact_scores["missed_member_keys"]),
        "missed_member_keys": sorted(
            ":".join(member_key) for member_key in artifact_scores["missed_member_keys"]
        ),
        "matched_effective_member_keys": sorted(
            ":".join(member_key)
            for member_key in artifact_scores["matched_member_keys"]
        ),
        "matched_effective_members": [
            {
                "member_key": list(member_key),
                "quality": quality,
            }
            for member_key, quality in sorted(
                artifact_scores["matched_member_quality"].items()
            )
        ],
        "effective_verdict_change_count": effective_verdict_change_count,
        "effective_verdict_change_kinds": sorted(
            f"{before}_to_{after}" for before, after in effective_verdict_changes
        ),
        "frontier": frontier_metrics,
        "supported": supported_metrics,
        "review": review_metrics,
        "conditional_selector_any_recall": _ratio(
            review_recovered,
            frontier_non_absent,
        ),
        "useful_record_review_recall": useful_record_recall,
        "strict_supported_precision": strict_supported_precision,
        "supported_artifact_precision": supported_artifact_precision,
        "supported_member_recall": supported_member_recall,
        "supported_required_member_recall": supported_required_member_recall,
        "supported_to_uncertain_members": len(supported_to_uncertain_members),
        "supported_to_uncertain_rate": supported_to_uncertain_rate,
        "supported_to_uncertain_member_keys": sorted(supported_to_uncertain_members),
        "recall_arm_precision": recall_arm_precision,
        "effective_artifact_precision": effective_artifact_precision,
        "effective_member_recall": effective_member_recall,
        "uncertain_truth_precision": uncertain_truth_precision,
        "supported_overclaim_count": supported_overclaim_count,
        "material_supported_overclaim_count": len(material_supported_overclaim_ids),
        "raw_supported_overclaim_count": raw_supported_overclaim_count,
        "critical_calibration_error_count": len(critical_calibration_error_candidates),
        "critical_calibration_error_candidates": (
            critical_calibration_error_candidates
        ),
        "raw_critical_calibration_error_count": len(
            raw_critical_calibration_error_candidates
        ),
        "raw_critical_calibration_error_candidates": (
            raw_critical_calibration_error_candidates
        ),
        "accepted_semantic_error_count": (
            len(artifact_scores["redundant_artifact_ids"])
            + len(artifact_scores["invalid_artifact_ids"])
        ),
        "duplicate_alias_count": duplicate_alias_count,
        "verdict_calibration_mismatches": calibration_mismatches,
        "verdict_calibration_mismatch_members": sorted(calibration_mismatch_members),
        "default_negative_candidates": default_negative_count,
        "default_negative_supported": default_negative_supported,
        "default_negative_accepted": default_negative_accepted,
        "material_default_negative_supported": (material_default_negative_supported),
        "material_default_negative_accepted": material_default_negative_accepted,
        "negative_records": negative_records,
        "selected_noise_records": selected_noise_records,
        "accepted_negative_review_records": selected_noise_records,
        "accepted_negative_review_rate": accepted_negative_review_rate,
        "maximum_accepted_negative_review_rate": (MAX_ACCEPTED_NEGATIVE_REVIEW_RATE),
        "hard_negative_records": hard_negative_records,
        "supported_hard_negative_records": supported_hard_negative_records,
        "supported_hard_negative_artifacts": supported_hard_negative_artifacts,
        "supported_negative_records": supported_negative_records,
        "supported_negative_artifacts": supported_negative_artifacts,
        "frontier_ratchet_regressions": (
            unit_ratchet_regressions + len(member_ratchet_regressions)
        ),
        "frontier_unit_ratchet_regressions": unit_ratchet_regressions,
        "frontier_member_ratchet_regressions": len(member_ratchet_regressions),
        "frontier_member_regression_members": sorted(member_ratchet_regressions),
        "frontier_upgraded_units": sorted(frontier_upgrades),
        "review_partial_units": sorted(
            f"{unit.key[0]}:{unit.key[1]}"
            for unit, score in zip(units, review_unit_scores, strict=True)
            if 0.0 < score < 1.0
        ),
        "review_absent_units": sorted(
            f"{unit.key[0]}:{unit.key[1]}"
            for unit, score in zip(units, review_unit_scores, strict=True)
            if score == 0.0
        ),
        "gates": gates,
        "candidate_gate_passed": all(gates.values()),
    }


_CLI_OMIT = object()


def _aggregate_cli_value(value: Any) -> Any:
    """Keep scalar aggregate structure while dropping identity-bearing arrays."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, nested in value.items():
            filtered = _aggregate_cli_value(nested)
            if filtered is not _CLI_OMIT:
                output[str(key)] = filtered
        return output
    return _CLI_OMIT


def _aggregate_cli_output(result: Mapping[str, Any]) -> dict[str, Any]:
    filtered = _aggregate_cli_value(result)
    if not isinstance(filtered, dict):
        raise CandidateGoldError("candidate evaluation aggregate is malformed")
    filtered["private_content_printed"] = False
    return filtered


def _assert_cli_aggregate_only(
    output: Mapping[str, Any],
    *,
    samples: list[dict[str, Any]],
    candidate_ids: set[str],
) -> None:
    """Fail closed if CLI output retains runtime identity or source content."""

    def contains_sequence(value: Any) -> bool:
        if isinstance(value, Mapping):
            return any(contains_sequence(item) for item in value.values())
        return isinstance(value, (list, tuple, set, frozenset))

    if contains_sequence(output):
        raise CandidateGoldError(
            "candidate evaluation aggregate contains non-aggregate diagnostics"
        )

    sensitive_values = set(candidate_ids)
    for sample in samples:
        for key in ("sample_id", "text"):
            value = sample.get(key)
            if isinstance(value, str) and value:
                sensitive_values.add(value)
        gold = sample.get("gold")
        if not isinstance(gold, Mapping):
            continue
        raw_units = gold.get("semantic_units")
        if not isinstance(raw_units, list):
            continue
        for raw_unit in raw_units:
            if not isinstance(raw_unit, Mapping):
                continue
            for key in ("unit_id", "truth"):
                value = raw_unit.get(key)
                if isinstance(value, str) and value:
                    sensitive_values.add(value)
            raw_members = raw_unit.get("members")
            if not isinstance(raw_members, list):
                continue
            for raw_member in raw_members:
                if not isinstance(raw_member, Mapping):
                    continue
                member_id = raw_member.get("member_id")
                if isinstance(member_id, str) and member_id:
                    sensitive_values.add(member_id)

    serialized = json.dumps(
        output,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    for value in sensitive_values:
        encoded = json.dumps(value, ensure_ascii=False)[1:-1]
        if encoded and encoded in serialized:
            raise CandidateGoldError(
                "candidate evaluation aggregate contains private runtime identity"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sample", type=Path)
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("run_manifest", type=Path)
    args = parser.parse_args()
    result = evaluate(args.sample, args.checkpoint, args.run_manifest)
    samples = _load_jsonl(args.sample)
    _, candidates, _ = _runtime_batches(samples)
    aggregate = _aggregate_cli_output(result)
    _assert_cli_aggregate_only(
        aggregate,
        samples=samples,
        candidate_ids=set(candidates),
    )
    print(
        json.dumps(
            aggregate,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
