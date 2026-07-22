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
MIN_SUPPORTED_REQUIRED_MEMBER_RECALL = 0.80
MIN_STRICT_SUPPORTED_PRECISION = 0.95
MIN_RECALL_ARM_PRECISION = 0.90
MAX_SUPPORTED_TO_UNCERTAIN_RATE = 0.20
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
) -> dict[str, str]:
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

    verdict_by_candidate: dict[str, str] = {}
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
        validate_gmail_temporal_candidate_verdict_set(
            analysis=runtime_batch.analysis,
            batch=runtime_batch.batch,
            plan=plan,
            rows=typed_rows,
        )
        for row in typed_rows:
            for verdict in row.verdicts:
                if verdict.candidate_id in verdict_by_candidate:
                    raise CandidateGoldError("checkpoint repeats a candidate")
                verdict_by_candidate[verdict.candidate_id] = verdict.verdict
    expected_candidates = {
        item.candidate.candidate_id
        for batch in runtime_batches
        for item in batch.candidates
    }
    if set(verdict_by_candidate) != expected_candidates:
        raise CandidateGoldError("checkpoint omits one or more candidates")
    return verdict_by_candidate


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
    checkpoint_path: Path,
    run_manifest_path: Path,
) -> dict[str, Any]:
    samples = _load_jsonl(sample_path)
    checkpoint_rows = _load_jsonl(checkpoint_path)
    manifest = _load_run_manifest(
        run_manifest_path,
        sample_path=sample_path,
        sample_record_count=len(samples),
        checkpoint_path=checkpoint_path,
        checkpoint_row_count=len(checkpoint_rows),
    )
    runtime_batches, candidates, pages = _runtime_batches(samples)
    units = _compile_gold(samples, candidates)
    if not units:
        raise CandidateGoldError("benchmark contains no semantic units")
    verdicts = _checkpoint_verdicts(
        checkpoint_rows,
        runtime_batches,
        pages,
        manifest,
    )
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

    frontier_scores = _selection_scores(units, frontier_ids)
    supported_scores = _selection_scores(units, supported_ids)
    review_scores = _selection_scores(units, accepted_ids)
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
    for sample in samples:
        sample_id = str(sample["sample_id"])
        expected_material = sample["gold"].get("expected_material")
        if not isinstance(expected_material, bool):
            raise CandidateGoldError("record materiality gold is malformed")
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
            selected_noise_records += int(bool(sample_candidate_ids & accepted_ids))

    frontier_metrics = _recall_metrics(frontier_scores)
    supported_metrics = _recall_metrics(supported_scores)
    review_metrics = _recall_metrics(review_scores)
    strict_supported_precision = _ratio(correct_supported, len(supported_ids))
    recall_arm_precision = _ratio(correct_review, len(accepted_ids))
    uncertain_truth_precision = (
        _ratio(correct_uncertain, len(uncertain_ids)) if uncertain_ids else None
    )
    supported_required_member_recall = _ratio(
        correct_supported,
        expected_supported_members,
    )
    supported_to_uncertain_rate = _ratio(
        len(supported_to_uncertain_members),
        expected_supported_members,
    )
    critical_calibration_error_candidates = sorted(
        candidate_id
        for candidate_id in supported_ids
        if expected_verdict_by_candidate.get(candidate_id) == "uncertain"
    )
    supported_overclaim_count = len(supported_ids) - correct_supported
    default_negative_count = len(candidates) - len(matches_by_candidate)
    useful_record_recall = _ratio(recalled_useful_records, useful_records)
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
        "supported_to_uncertain_rate": supported_to_uncertain_rate
        <= MAX_SUPPORTED_TO_UNCERTAIN_RATE,
        "strict_supported_precision": strict_supported_precision
        >= MIN_STRICT_SUPPORTED_PRECISION,
        "recall_arm_precision": recall_arm_precision >= MIN_RECALL_ARM_PRECISION,
        "no_selected_noise": selected_noise_records == 0,
        "no_duplicate_aliases": duplicate_alias_count == 0,
        "no_supported_overclaims": supported_overclaim_count == 0,
        "no_critical_calibration_errors": not critical_calibration_error_candidates,
        "no_default_negative_supported": default_negative_supported == 0,
        "no_default_negative_accepted": default_negative_accepted == 0,
        "frontier_ratchet": (
            unit_ratchet_regressions == 0 and not member_ratchet_regressions
        ),
        "frontier_unit_ratchet": unit_ratchet_regressions == 0,
        "frontier_member_ratchet": not member_ratchet_regressions,
    }
    return {
        "run_provenance": {
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
        },
        "records": len(samples),
        "useful_records": useful_records,
        "semantic_units": len(units),
        "semantic_members": sum(len(unit.members) for unit in units),
        "frontier_candidates": len(candidates),
        "verifier_pages": len(pages),
        "supported_candidates": len(supported_ids),
        "uncertain_candidates": len(uncertain_ids),
        "accepted_candidates": len(accepted_ids),
        "frontier": frontier_metrics,
        "supported": supported_metrics,
        "review": review_metrics,
        "conditional_selector_any_recall": _ratio(
            review_recovered,
            frontier_non_absent,
        ),
        "useful_record_review_recall": useful_record_recall,
        "strict_supported_precision": strict_supported_precision,
        "supported_required_member_recall": supported_required_member_recall,
        "supported_to_uncertain_members": len(supported_to_uncertain_members),
        "supported_to_uncertain_rate": supported_to_uncertain_rate,
        "supported_to_uncertain_member_keys": sorted(supported_to_uncertain_members),
        "recall_arm_precision": recall_arm_precision,
        "uncertain_truth_precision": uncertain_truth_precision,
        "supported_overclaim_count": supported_overclaim_count,
        "critical_calibration_error_count": len(critical_calibration_error_candidates),
        "critical_calibration_error_candidates": (
            critical_calibration_error_candidates
        ),
        "accepted_semantic_error_count": len(accepted_ids) - correct_review,
        "duplicate_alias_count": duplicate_alias_count,
        "verdict_calibration_mismatches": calibration_mismatches,
        "verdict_calibration_mismatch_members": sorted(calibration_mismatch_members),
        "default_negative_candidates": default_negative_count,
        "default_negative_supported": default_negative_supported,
        "default_negative_accepted": default_negative_accepted,
        "selected_noise_records": selected_noise_records,
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
        "private_content_printed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sample", type=Path)
    parser.add_argument("checkpoint", type=Path)
    parser.add_argument("run_manifest", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            evaluate(args.sample, args.checkpoint, args.run_manifest),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
