from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


MIN_USEFUL_RECORD_RECALL = 0.85
MIN_SUPPORTED_PROPOSAL_PRECISION = 0.90
MAX_CRITICAL_ERROR_RATE = 0.01


class SyntheticBenchmarkError(ValueError):
    """Raised when benchmark evidence is stale, incomplete, or malformed."""


def _load(path: Path) -> list[dict[str, Any]]:
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise SyntheticBenchmarkError("benchmark input could not be read") from exc
    if not rows or any(not isinstance(item, dict) for item in rows):
        raise SyntheticBenchmarkError("benchmark input is empty or malformed")
    identifiers = [item.get("sample_id") for item in rows]
    if (
        any(not isinstance(value, str) or not value for value in identifiers)
        or len(identifiers) != len(set(identifiers))
    ):
        raise SyntheticBenchmarkError("benchmark sample IDs are invalid")
    return rows


def _ratio(numerator: int, denominator: int) -> float:
    if denominator < 1:
        raise SyntheticBenchmarkError("benchmark metric has no denominator")
    return numerator / denominator


def evaluate(
    sample_path: Path,
    selection_path: Path,
    sol_labels_path: Path,
) -> dict[str, Any]:
    samples = {item["sample_id"]: item for item in _load(sample_path)}
    selections = {item["sample_id"]: item for item in _load(selection_path)}
    labels = {item["sample_id"]: item for item in _load(sol_labels_path)}
    if set(samples) != set(selections) or set(samples) != set(labels):
        raise SyntheticBenchmarkError("benchmark cohorts do not match")

    useful_records = 0
    selected_useful_records = 0
    supported_useful_records = 0
    noise_records = 0
    selected_noise_records = 0
    presented_proposals = 0
    supported_proposals = 0
    critical_errors = 0
    gold_judge_disagreements = 0

    for sample_id, sample in samples.items():
        gold = sample.get("gold")
        selection = selections[sample_id]
        label = labels[sample_id]
        if not isinstance(gold, Mapping):
            raise SyntheticBenchmarkError("sample is missing embedded gold")
        expected_material = gold.get("expected_material")
        expected_filter = gold.get("expected_filter")
        associations = selection.get("associations")
        supported_ids = label.get("supported_proposal_ids")
        if (
            not isinstance(expected_material, bool)
            or expected_filter not in {"should_admit", "should_suppress"}
            or not isinstance(associations, list)
            or not isinstance(supported_ids, list)
            or any(not isinstance(value, str) for value in supported_ids)
        ):
            raise SyntheticBenchmarkError("benchmark row is malformed")

        selected = bool(associations)
        if expected_material:
            useful_records += 1
            selected_useful_records += int(selected)
            supported_useful_records += int(bool(supported_ids))
        else:
            noise_records += 1
            selected_noise_records += int(selected)

        expected_materiality = (
            "material_temporal" if expected_material else None
        )
        materiality = label.get("materiality")
        filter_verdict = label.get("filter_verdict")
        if expected_material:
            gold_judge_disagreements += int(
                materiality != expected_materiality
                or filter_verdict != expected_filter
            )
        else:
            gold_judge_disagreements += int(
                materiality == "material_temporal"
                or filter_verdict != expected_filter
            )

        presented_proposals += len(associations)
        supported_proposals += len(supported_ids)
        critical_errors += int(label.get("critical_error") != "none")

    useful_record_recall = _ratio(
        supported_useful_records,
        useful_records,
    )
    frontier_selection_recall = _ratio(
        selected_useful_records,
        useful_records,
    )
    proposal_precision = _ratio(supported_proposals, presented_proposals)
    critical_error_rate_per_record = _ratio(critical_errors, len(samples))
    critical_error_rate_per_proposal = _ratio(
        critical_errors,
        presented_proposals,
    )
    selected_noise_rate = _ratio(selected_noise_records, noise_records)
    gates = {
        "useful_record_recall": useful_record_recall
        >= MIN_USEFUL_RECORD_RECALL,
        "supported_proposal_precision": proposal_precision
        >= MIN_SUPPORTED_PROPOSAL_PRECISION,
        "critical_error_rate": critical_error_rate_per_proposal
        < MAX_CRITICAL_ERROR_RATE,
        "no_selected_noise": selected_noise_records == 0,
        "judge_matches_embedded_record_gold": gold_judge_disagreements == 0,
    }
    return {
        "records": len(samples),
        "useful_records": useful_records,
        "noise_records": noise_records,
        "selected_useful_records": selected_useful_records,
        "supported_useful_records": supported_useful_records,
        "selected_noise_records": selected_noise_records,
        "presented_proposals": presented_proposals,
        "supported_proposals": supported_proposals,
        "critical_errors": critical_errors,
        "gold_judge_disagreements": gold_judge_disagreements,
        "frontier_selection_recall": frontier_selection_recall,
        "useful_record_recall": useful_record_recall,
        "supported_proposal_precision": proposal_precision,
        "selected_noise_rate": selected_noise_rate,
        "critical_error_rate_per_record": critical_error_rate_per_record,
        "critical_error_rate_per_proposal": critical_error_rate_per_proposal,
        "gates": gates,
        "synthetic_gate_passed": all(gates.values()),
        "private_content_printed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sample", type=Path)
    parser.add_argument("selection", type=Path)
    parser.add_argument("sol_labels", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            evaluate(args.sample, args.selection, args.sol_labels),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
