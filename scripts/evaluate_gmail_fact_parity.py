#!/usr/bin/env python3
"""Score original-Brain versus Brain V2 fact retention from opaque labels.

The evaluator consumes private arm-blind semantic-unit labels, a private
alignment artifact, every private extractor output, and a private manifest.
It verifies their hashes before scoring.  Labels carry member-level judgments
for the original arm and each V2 run, so a V2 member cannot inherit the
original member's support, scope, or critical-error judgment.

Only aggregate counts, opaque IDs, digests, and rates are returned.  Gmail
text, fact statements, provider identifiers, and local paths are never
printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any


VERSION = "gmail_fact_parity_evaluation_v2"
LABEL_VERSION = "gmail_fact_parity_unit_v2"
MANIFEST_VERSION = "gmail_fact_parity_manifest_v2"
MIN_RETENTION = 0.95
MIN_PRECISION = 0.95
MIN_RUN_AGREEMENT = 0.95
MIN_V2_RUNS = 3
STAGES = ("candidate", "review", "persisted")
CLASSIFICATIONS = {"non_temporal", "temporal", "not_fact"}
CRITICAL_ERRORS = {"none", "unsupported", "wrong_scope", "wrong_entity", "other"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UNIT_ID_RE = re.compile(r"^gfp_u_[0-9a-f]{32}$")
_THREAD_ID_RE = re.compile(r"^gfp_t_[0-9a-f]{32}$")
_MEMBER_ID_RE = re.compile(r"^gfp_a_[0-9a-f]{32}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_LABEL_KEYS = {
    "version",
    "unit_id",
    "thread_id",
    "useful",
    "classification",
    "original",
    "v2",
}
_ARM_KEYS = {"stage_counts", "members"}
_MEMBER_KEYS = {
    "member_id",
    "supported",
    "scope_correct",
    "critical_error",
    "stages",
}
_MANIFEST_KEYS = {
    "version",
    "labels_sha256",
    "alignment_sha256",
    "evaluator_sha256",
    "cohort_sha256",
    "packet_sha256",
    "label_unit_count",
    "thread_count",
    "message_count",
    "packet_count",
    "original_run",
    "v2_runs",
}
_RUN_KEYS = {
    "run_id",
    "commit",
    "prompt_version",
    "model",
    "reasoning_effort",
    "output_sha256",
    "stage_members",
}


class GmailFactParityError(ValueError):
    """Raised when parity evidence is malformed, stale, or unreconciled."""


def _private_file(path: Path, *, allow_empty: bool = False) -> None:
    if path.is_symlink() or not path.is_file():
        raise GmailFactParityError("input must be a regular non-symlink file")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise GmailFactParityError("input artifacts must have mode 0600")
    if not allow_empty and path.stat().st_size == 0:
        raise GmailFactParityError("input artifacts must not be empty")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _file_identity(path: Path) -> tuple[int, int]:
    metadata = path.stat()
    return metadata.st_dev, metadata.st_ino


def _evaluator_path() -> Path:
    return Path(__file__)


def _positive_int(value: Any, name: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise GmailFactParityError(f"{name} is invalid")
    return value


def _stage_counts(value: Any, name: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != set(STAGES):
        raise GmailFactParityError(f"{name} stage counts are invalid")
    return {
        stage: _positive_int(value[stage], f"{name}.{stage}", allow_zero=True)
        for stage in STAGES
    }


def _stage_membership(value: Any, name: str) -> dict[str, bool]:
    if (
        not isinstance(value, Mapping)
        or set(value) != set(STAGES)
        or any(not isinstance(value[stage], bool) for stage in STAGES)
    ):
        raise GmailFactParityError(f"{name} stage membership is invalid")
    stages = {stage: value[stage] for stage in STAGES}
    if not stages["candidate"]:
        raise GmailFactParityError(f"{name} member must exist at candidate stage")
    if stages["persisted"] and not stages["review"]:
        raise GmailFactParityError(f"{name} stage membership is not monotonic")
    return stages


def _member(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _MEMBER_KEYS:
        raise GmailFactParityError(f"{name} member schema is invalid")
    member_id = value.get("member_id")
    critical_error = value.get("critical_error")
    if (
        not isinstance(member_id, str)
        or _MEMBER_ID_RE.fullmatch(member_id) is None
        or not isinstance(value.get("supported"), bool)
        or not isinstance(value.get("scope_correct"), bool)
        or critical_error not in CRITICAL_ERRORS
    ):
        raise GmailFactParityError(f"{name} member judgment is invalid")
    return {
        **dict(value),
        "stages": _stage_membership(value.get("stages"), name),
    }


def _arm_evidence(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _ARM_KEYS:
        raise GmailFactParityError(f"{name} arm evidence is invalid")
    stage_counts = _stage_counts(value.get("stage_counts"), name)
    raw_members = value.get("members")
    if not isinstance(raw_members, list):
        raise GmailFactParityError(f"{name} members are invalid")
    members = [
        _member(item, f"{name}.member[{index}]")
        for index, item in enumerate(raw_members)
    ]
    member_ids = [item["member_id"] for item in members]
    if len(member_ids) != len(set(member_ids)):
        raise GmailFactParityError(f"{name} contains duplicate member IDs")
    for stage in STAGES:
        actual = sum(item["stages"][stage] for item in members)
        if actual != stage_counts[stage]:
            raise GmailFactParityError(f"{name}.{stage} stage count is unreconciled")
    return {"stage_counts": stage_counts, "members": members}


def _run(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _RUN_KEYS:
        raise GmailFactParityError(f"{name} run manifest is invalid")
    run_id = value.get("run_id")
    strings = (
        value.get("commit"),
        value.get("prompt_version"),
        value.get("model"),
        value.get("reasoning_effort"),
    )
    output_digest = value.get("output_sha256")
    if (
        not isinstance(run_id, str)
        or _RUN_ID_RE.fullmatch(run_id) is None
        or any(not isinstance(item, str) or not item for item in strings)
        or not isinstance(output_digest, str)
        or _SHA256_RE.fullmatch(output_digest) is None
    ):
        raise GmailFactParityError(f"{name} run provenance is invalid")
    return {
        **dict(value),
        "stage_members": _stage_counts(value.get("stage_members"), name),
    }


def _load_manifest(path: Path, labels_path: Path) -> dict[str, Any]:
    _private_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GmailFactParityError("manifest is not valid JSON") from exc
    if not isinstance(value, Mapping) or set(value) != _MANIFEST_KEYS:
        raise GmailFactParityError("manifest schema is invalid")
    if value.get("version") != MANIFEST_VERSION:
        raise GmailFactParityError("manifest version is invalid")
    for name in (
        "labels_sha256",
        "alignment_sha256",
        "evaluator_sha256",
        "cohort_sha256",
        "packet_sha256",
    ):
        digest = value.get(name)
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            raise GmailFactParityError(f"manifest {name} is invalid")
    if value["labels_sha256"] != _sha256(labels_path):
        raise GmailFactParityError("manifest does not match the label artifact")
    if value["evaluator_sha256"] != _sha256(_evaluator_path()):
        raise GmailFactParityError("manifest does not match the evaluator artifact")
    counts = {
        name: _positive_int(value.get(name), name)
        for name in (
            "label_unit_count",
            "thread_count",
            "message_count",
            "packet_count",
        )
    }
    original = _run(value.get("original_run"), "original")
    raw_v2 = value.get("v2_runs")
    if not isinstance(raw_v2, list) or len(raw_v2) < MIN_V2_RUNS:
        raise GmailFactParityError(f"at least {MIN_V2_RUNS} V2 runs are required")
    v2_runs = [_run(item, f"v2[{index}]") for index, item in enumerate(raw_v2)]
    run_ids = [item["run_id"] for item in v2_runs]
    if len(run_ids) != len(set(run_ids)) or original["run_id"] in run_ids:
        raise GmailFactParityError("run IDs must be unique")
    return {
        **dict(value),
        **counts,
        "original_run": original,
        "v2_runs": v2_runs,
    }


def _load_labels(
    path: Path, *, original_run_id: str, v2_run_ids: set[str]
) -> list[dict[str, Any]]:
    _private_file(path)
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GmailFactParityError("labels are not valid JSONL") from exc
    if not rows:
        raise GmailFactParityError("labels are empty")

    seen_units: set[str] = set()
    seen_members: dict[str, set[str]] = {
        original_run_id: set(),
        **{run_id: set() for run_id in v2_run_ids},
    }
    validated: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != _LABEL_KEYS:
            raise GmailFactParityError("label schema is invalid")
        unit_id = row.get("unit_id")
        thread_id = row.get("thread_id")
        classification = row.get("classification")
        if (
            row.get("version") != LABEL_VERSION
            or not isinstance(unit_id, str)
            or _UNIT_ID_RE.fullmatch(unit_id) is None
            or unit_id in seen_units
            or not isinstance(thread_id, str)
            or _THREAD_ID_RE.fullmatch(thread_id) is None
            or not isinstance(row.get("useful"), bool)
            or classification not in CLASSIFICATIONS
        ):
            raise GmailFactParityError("label values are invalid")

        original = _arm_evidence(row.get("original"), f"{unit_id}.original")
        raw_v2 = row.get("v2")
        if not isinstance(raw_v2, Mapping) or set(raw_v2) != v2_run_ids:
            raise GmailFactParityError("label V2 run coverage is incomplete")
        v2 = {
            run_id: _arm_evidence(value, f"{unit_id}.{run_id}")
            for run_id, value in raw_v2.items()
        }

        for run_id, evidence in ((original_run_id, original), *v2.items()):
            for member in evidence["members"]:
                member_id = member["member_id"]
                if member_id in seen_members[run_id]:
                    raise GmailFactParityError(
                        f"{run_id} member is aligned to multiple semantic units"
                    )
                seen_members[run_id].add(member_id)

        seen_units.add(unit_id)
        validated.append({**dict(row), "original": original, "v2": v2})
    return validated


def _member_good(member: Mapping[str, Any]) -> bool:
    return bool(
        member["supported"]
        and member["scope_correct"]
        and member["critical_error"] == "none"
    )


def _fact_member_good(row: Mapping[str, Any], member: Mapping[str, Any]) -> bool:
    return bool(row["classification"] != "not_fact" and _member_good(member))


def _members_at_stage(evidence: Mapping[str, Any], stage: str) -> list[dict[str, Any]]:
    return [member for member in evidence["members"] if member["stages"][stage]]


def _has_good_evidence(
    row: Mapping[str, Any], evidence: Mapping[str, Any], stage: str
) -> bool:
    return any(
        _fact_member_good(row, member) for member in _members_at_stage(evidence, stage)
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _verify_bound_artifacts(
    *,
    labels_path: Path,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    alignment_path: Path | None,
    run_output_paths: Mapping[str, Path] | None,
) -> dict[str, str]:
    if alignment_path is None:
        raise GmailFactParityError("alignment artifact is required")
    _private_file(alignment_path)
    protected_identities = {
        _file_identity(labels_path),
        _file_identity(manifest_path),
    }
    alignment_identity = _file_identity(alignment_path)
    if alignment_identity in protected_identities:
        raise GmailFactParityError("alignment artifact must be a distinct file")
    if _sha256(alignment_path) != manifest["alignment_sha256"]:
        raise GmailFactParityError("manifest does not match the alignment artifact")

    expected_runs = {
        manifest["original_run"]["run_id"]: manifest["original_run"],
        **{item["run_id"]: item for item in manifest["v2_runs"]},
    }
    if run_output_paths is None or set(run_output_paths) != set(expected_runs):
        raise GmailFactParityError("run output artifact coverage is incomplete")

    protected_identities.add(alignment_identity)
    output_identities: set[tuple[int, int]] = set()
    output_digests: dict[str, str] = {}
    for run_id, run in expected_runs.items():
        path = Path(run_output_paths[run_id])
        _private_file(path)
        identity = _file_identity(path)
        if identity in protected_identities or identity in output_identities:
            raise GmailFactParityError("run output artifacts must be distinct files")
        output_identities.add(identity)
        digest = _sha256(path)
        if digest != run["output_sha256"]:
            raise GmailFactParityError(f"{run_id} output artifact is stale")
        output_digests[run_id] = digest
    return output_digests


def evaluate_gmail_fact_parity(
    labels_path: Path,
    manifest_path: Path,
    alignment_path: Path | None = None,
    run_output_paths: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    """Validate and score opaque semantic-unit parity evidence."""

    _private_file(labels_path)
    manifest = _load_manifest(manifest_path, labels_path)
    output_digests = _verify_bound_artifacts(
        labels_path=labels_path,
        manifest_path=manifest_path,
        manifest=manifest,
        alignment_path=alignment_path,
        run_output_paths=run_output_paths,
    )
    v2_run_ids = {item["run_id"] for item in manifest["v2_runs"]}
    labels = _load_labels(
        labels_path,
        original_run_id=manifest["original_run"]["run_id"],
        v2_run_ids=v2_run_ids,
    )
    if len(labels) != manifest["label_unit_count"]:
        raise GmailFactParityError("manifest label count is stale")
    label_threads = {row["thread_id"] for row in labels}
    if len(label_threads) > manifest["thread_count"]:
        raise GmailFactParityError("labeled threads exceed the cohort")

    expected_runs = {
        manifest["original_run"]["run_id"]: manifest["original_run"],
        **{item["run_id"]: item for item in manifest["v2_runs"]},
    }
    for run_id, run in expected_runs.items():
        for stage in STAGES:
            actual = sum(
                (
                    row["original"]["stage_counts"][stage]
                    if run_id == manifest["original_run"]["run_id"]
                    else row["v2"][run_id]["stage_counts"][stage]
                )
                for row in labels
            )
            if actual != run["stage_members"][stage]:
                raise GmailFactParityError(
                    f"{run_id}.{stage} membership count is unreconciled"
                )

    denominators = {
        stage: [
            row
            for row in labels
            if row["useful"]
            and row["classification"] == "non_temporal"
            and _has_good_evidence(row, row["original"], stage)
        ]
        for stage in STAGES
    }
    for stage, denominator in denominators.items():
        if not denominator:
            raise GmailFactParityError(
                f"no useful original non-temporal units were labeled at {stage} stage"
            )

    run_results: dict[str, Any] = {}
    for run in manifest["v2_runs"]:
        run_id = run["run_id"]
        stage_results: dict[str, Any] = {}
        for stage in STAGES:
            present_rows = [
                row for row in labels if row["v2"][run_id]["stage_counts"][stage] > 0
            ]
            present_members = [
                (row, member)
                for row in labels
                for member in _members_at_stage(row["v2"][run_id], stage)
            ]
            good_members = [
                member
                for row, member in present_members
                if _fact_member_good(row, member)
            ]
            retained = sum(
                _has_good_evidence(row, row["v2"][run_id], stage)
                for row in denominators[stage]
            )
            raw_retained = sum(
                row["v2"][run_id]["stage_counts"][stage] > 0
                for row in denominators[stage]
            )
            duplicate_members = sum(
                max(0, row["v2"][run_id]["stage_counts"][stage] - 1) for row in labels
            )
            critical_members = sum(
                member["critical_error"] != "none" for _, member in present_members
            )
            critical_units = sum(
                any(
                    member["critical_error"] != "none"
                    for member in _members_at_stage(row["v2"][run_id], stage)
                )
                for row in labels
            )
            stage_results[stage] = {
                "original_useful_non_temporal_units": len(denominators[stage]),
                "raw_present_units": raw_retained,
                "retained_units": retained,
                "retention": _ratio(retained, len(denominators[stage])),
                "v2_units": len(present_rows),
                "v2_members": len(present_members),
                "supported_scope_correct_members": len(good_members),
                "precision": _ratio(len(good_members), len(present_members)),
                "duplicate_members": duplicate_members,
                "critical_error_members": critical_members,
                "critical_error_units": critical_units,
            }

        all_members = [
            (row, member) for row in labels for member in row["v2"][run_id]["members"]
        ]
        critical_members = sum(
            member["critical_error"] != "none" for _, member in all_members
        )
        critical_units = sum(
            any(
                member["critical_error"] != "none"
                for member in row["v2"][run_id]["members"]
            )
            for row in labels
        )

        by_thread: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in denominators["candidate"]:
            by_thread[row["thread_id"]].append(row)
        thread_recalls = [
            sum(_has_good_evidence(row, row["v2"][run_id], "candidate") for row in rows)
            / len(rows)
            for rows in by_thread.values()
        ]
        useful_thread_coverage = sum(value > 0 for value in thread_recalls)
        run_results[run_id] = {
            "stages": stage_results,
            "useful_thread_count": len(thread_recalls),
            "useful_thread_coverage": useful_thread_coverage,
            "useful_thread_coverage_rate": _ratio(
                useful_thread_coverage, len(thread_recalls)
            ),
            "macro_candidate_recall": (
                sum(thread_recalls) / len(thread_recalls) if thread_recalls else None
            ),
            "critical_error_members": critical_members,
            "critical_error_units": critical_units,
            "gates": {
                **{
                    f"{stage}_retention": value["retention"] is not None
                    and value["retention"] >= MIN_RETENTION
                    for stage, value in stage_results.items()
                },
                **{
                    f"{stage}_precision": value["precision"] is not None
                    and value["precision"] >= MIN_PRECISION
                    for stage, value in stage_results.items()
                },
                "no_critical_errors": critical_members == 0,
                "no_duplicate_members": all(
                    value["duplicate_members"] == 0 for value in stage_results.values()
                ),
            },
        }

    ordered_runs = [item["run_id"] for item in manifest["v2_runs"]]
    agreement_pairs: list[dict[str, Any]] = []
    for index, first in enumerate(ordered_runs):
        for second in ordered_runs[index + 1 :]:
            for stage in STAGES:
                eligible = denominators[stage]
                agreements = sum(
                    _has_good_evidence(row, row["v2"][first], stage)
                    == _has_good_evidence(row, row["v2"][second], stage)
                    for row in eligible
                )
                rate = agreements / len(eligible)
                agreement_pairs.append(
                    {
                        "first": first,
                        "second": second,
                        "stage": stage,
                        "agreed_units": agreements,
                        "unit_count": len(eligible),
                        "agreement": rate,
                        "passed": rate >= MIN_RUN_AGREEMENT,
                    }
                )

    all_run_gates = all(
        all(result["gates"].values()) for result in run_results.values()
    )
    assert alignment_path is not None
    return {
        "version": VERSION,
        "manifest_version": manifest["version"],
        "provenance": {
            "manifest_sha256": _sha256(manifest_path),
            "labels_sha256": manifest["labels_sha256"],
            "alignment_sha256": manifest["alignment_sha256"],
            "evaluator_sha256": manifest["evaluator_sha256"],
            "run_output_sha256": output_digests,
            "cohort_sha256": manifest["cohort_sha256"],
            "packet_sha256": manifest["packet_sha256"],
        },
        "cohort": {
            "threads": manifest["thread_count"],
            "messages": manifest["message_count"],
            "packets": manifest["packet_count"],
            "labeled_units": len(labels),
            "labeled_threads": len(label_threads),
        },
        "runs": run_results,
        "run_agreement": agreement_pairs,
        "gate_passed": all_run_gates
        and bool(agreement_pairs)
        and all(item["passed"] for item in agreement_pairs),
        "private_content_printed": False,
    }


def _run_output_arguments(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        if "=" not in value:
            raise GmailFactParityError(
                "run output arguments must use RUN_ID=PATH syntax"
            )
        run_id, raw_path = value.split("=", 1)
        if _RUN_ID_RE.fullmatch(run_id) is None or not raw_path or run_id in result:
            raise GmailFactParityError("run output argument is invalid")
        result[run_id] = Path(raw_path)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("labels", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("alignment", type=Path)
    parser.add_argument(
        "--run-output",
        action="append",
        default=[],
        metavar="RUN_ID=PATH",
        help="private output artifact for one manifest run (repeat for every run)",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            evaluate_gmail_fact_parity(
                args.labels,
                args.manifest,
                args.alignment,
                _run_output_arguments(args.run_output),
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
