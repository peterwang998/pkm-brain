from __future__ import annotations

import json
import importlib.util
import sys
from pathlib import Path

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


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(item, sort_keys=True) + "\n" for item in rows),
        encoding="utf-8",
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
