from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parents[1]


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


candidate_gold = _load(
    "test_negative_review_candidate_gold",
    ROOT / "scripts" / "evaluate_gmail_temporal_candidate_gold.py",
)
benchmark = _load(
    "test_negative_review_synthetic_builder",
    ROOT / "scripts" / "build_gmail_temporal_synthetic_benchmark.py",
)


def _fixture(tmp_path: Path) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
    sample_path = tmp_path / "samples.jsonl"
    benchmark.build(sample_path)
    samples = candidate_gold._load_jsonl(sample_path)
    _runtime, candidates, _pages = candidate_gold._runtime_batches(samples)
    units = candidate_gold._compile_gold(samples, candidates)
    verdicts = {candidate_id: "unsupported" for candidate_id in candidates}
    for unit in units:
        for member in unit.members:
            matches = candidate_gold._member_matches(member)
            selected = max(matches, key=lambda value: (matches[value], value))
            verdicts[selected] = candidate_gold._member_expected_verdicts(member)[
                selected
            ]
    return sample_path, samples, {"candidates": candidates, "verdicts": verdicts}


def _write_samples(path: Path, samples: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            for row in samples
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _evaluate(path: Path, verdicts: dict[str, str]) -> dict[str, Any]:
    return candidate_gold.evaluate(
        path,
        None,
        None,
        prevalidated_verdict_maps=(verdicts, verdicts),
        provenance_override={"single_run": False, "evidence_type": "test"},
    )


def test_hard_negative_forbids_any_supported_artifact(tmp_path: Path) -> None:
    sample_path, samples, authority = _fixture(tmp_path)
    candidates = authority["candidates"]
    negative = next(
        sample
        for sample in samples
        if sample["gold"]["expected_material"] is False
        and any(
            runtime.sample_id == sample["sample_id"] for runtime in candidates.values()
        )
    )
    negative["gold"]["hard_negative"] = True
    for sample in samples:
        sample["gold"].setdefault("hard_negative", False)
    _write_samples(sample_path, samples)
    target = next(
        candidate_id
        for candidate_id, runtime in candidates.items()
        if runtime.sample_id == negative["sample_id"]
    )
    verdicts = dict(authority["verdicts"])
    verdicts[target] = "supported"

    result = _evaluate(sample_path, verdicts)

    assert result["hard_negative_records"] == 1
    assert result["supported_hard_negative_records"] == 1
    assert result["supported_hard_negative_artifacts"] == 1
    assert result["gates"]["no_supported_hard_negative_artifacts"] is False
    assert result["candidate_gate_passed"] is False


def test_ordinary_negative_review_uses_rate_gate_not_zero_tolerance(
    tmp_path: Path,
) -> None:
    sample_path, samples, authority = _fixture(tmp_path)
    candidates = authority["candidates"]
    negative = next(
        sample
        for sample in samples
        if sample["gold"]["expected_material"] is False
        and any(
            runtime.sample_id == sample["sample_id"] for runtime in candidates.values()
        )
    )
    target = next(
        candidate_id
        for candidate_id, runtime in candidates.items()
        if runtime.sample_id == negative["sample_id"]
    )
    verdicts = dict(authority["verdicts"])
    verdicts[target] = "uncertain"

    result = _evaluate(sample_path, verdicts)

    assert result["negative_records"] == 12
    assert result["accepted_negative_review_records"] == 1
    assert result["accepted_negative_review_rate"] == 1 / 12
    assert result["maximum_accepted_negative_review_rate"] == 0.05
    assert result["material_default_negative_accepted"] == 0
    assert result["gates"]["no_default_negative_accepted"] is True
    assert result["material_impure_uncertainty_sidecars"] == 0
    assert result["gates"]["uncertainty_hypothesis_purity"] is True
    assert result["gates"]["accepted_negative_review_rate"] is False
    assert result["candidate_gate_passed"] is False


def test_ordinary_negative_still_forbids_supported_artifacts(tmp_path: Path) -> None:
    sample_path, samples, authority = _fixture(tmp_path)
    candidates = authority["candidates"]
    negative = next(
        sample
        for sample in samples
        if sample["gold"]["expected_material"] is False
        and any(runtime.sample_id == sample["sample_id"] for runtime in candidates.values())
    )
    target = next(
        candidate_id
        for candidate_id, runtime in candidates.items()
        if runtime.sample_id == negative["sample_id"]
    )
    verdicts = dict(authority["verdicts"])
    verdicts[target] = "supported"

    result = _evaluate(sample_path, verdicts)

    assert result["hard_negative_records"] == 0
    assert result["supported_negative_records"] == 1
    assert result["supported_negative_artifacts"] == 1
    assert result["gates"]["no_supported_negative_artifacts"] is False
    assert result["gates"]["no_supported_overclaims"] is False
    assert result["gates"]["no_default_negative_supported"] is False
    assert result["candidate_gate_passed"] is False


def test_synthetic_samples_without_hard_negative_field_remain_compatible(
    tmp_path: Path,
) -> None:
    sample_path, _samples, authority = _fixture(tmp_path)

    result = _evaluate(sample_path, authority["verdicts"])

    assert result["hard_negative_records"] == 0
    assert result["supported_hard_negative_artifacts"] == 0
    assert result["accepted_negative_review_rate"] == 0.0
    assert result["gates"]["accepted_negative_review_rate"] is True
    assert result["gates"]["no_supported_hard_negative_artifacts"] is True
