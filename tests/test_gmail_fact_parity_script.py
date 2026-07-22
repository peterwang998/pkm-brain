from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "evaluate_gmail_fact_parity.py"
SPEC = importlib.util.spec_from_file_location("evaluate_gmail_fact_parity", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
parity = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = parity
SPEC.loader.exec_module(parity)

RUN_IDS = ("v2-a", "v2-b", "v2-c")


def _id(prefix: str, value: int) -> str:
    return f"{prefix}_{value:032x}"


def _private_write(path: Path, payload: str) -> None:
    path.write_text(payload, encoding="utf-8")
    os.chmod(path, 0o600)


def _member(
    member_number: int,
    *,
    supported: bool = True,
    scope_correct: bool = True,
    critical_error: str = "none",
    candidate: bool = True,
    review: bool = True,
    persisted: bool = True,
) -> dict[str, Any]:
    return {
        "member_id": _id("gfp_a", member_number),
        "supported": supported,
        "scope_correct": scope_correct,
        "critical_error": critical_error,
        "stages": {
            "candidate": candidate,
            "review": review,
            "persisted": persisted,
        },
    }


def _arm(members: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "stage_counts": {
            stage: sum(member["stages"][stage] for member in members)
            for stage in parity.STAGES
        },
        "members": members,
    }


def _run_manifest(
    run_id: str, rows: list[dict[str, Any]], output: Path
) -> dict[str, Any]:
    if run_id == "original":
        evidences = [row["original"] for row in rows]
    else:
        evidences = [row["v2"][run_id] for row in rows]
    return {
        "run_id": run_id,
        "commit": "abc123",
        "prompt_version": "prompt-v1",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "medium",
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "stage_members": {
            stage: sum(evidence["stage_counts"][stage] for evidence in evidences)
            for stage in parity.STAGES
        },
    }


def _write_labels(path: Path, rows: list[dict[str, Any]]) -> None:
    _private_write(
        path,
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
    )


def _write_manifest(files: dict[str, Any]) -> None:
    rows = files["rows"]
    manifest = {
        "version": "gmail_fact_parity_manifest_v2",
        "labels_sha256": hashlib.sha256(files["labels"].read_bytes()).hexdigest(),
        "alignment_sha256": hashlib.sha256(files["alignment"].read_bytes()).hexdigest(),
        "evaluator_sha256": hashlib.sha256(MODULE_PATH.read_bytes()).hexdigest(),
        "cohort_sha256": "1" * 64,
        "packet_sha256": "2" * 64,
        "label_unit_count": len(rows),
        "thread_count": len({row["thread_id"] for row in rows}),
        "message_count": len(rows),
        "packet_count": len(rows),
        "original_run": _run_manifest("original", rows, files["outputs"]["original"]),
        "v2_runs": [
            _run_manifest(run_id, rows, files["outputs"][run_id])
            for run_id in files["run_ids"]
        ],
    }
    files["manifest_value"] = manifest
    _private_write(files["manifest"], json.dumps(manifest, sort_keys=True))


def _write_evidence(
    tmp_path: Path,
    *,
    unit_count: int = 20,
    run_ids: tuple[str, ...] = RUN_IDS,
) -> dict[str, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    labels = tmp_path / "labels.jsonl"
    rows: list[dict[str, Any]] = []
    for index in range(unit_count):
        rows.append(
            {
                "version": "gmail_fact_parity_unit_v2",
                "unit_id": _id("gfp_u", index + 1),
                "thread_id": _id("gfp_t", (index // 2) + 1),
                "useful": True,
                "classification": "non_temporal",
                "original": _arm([_member(1_000 + index)]),
                "v2": {
                    run_id: _arm([_member((position + 2) * 1_000 + index)])
                    for position, run_id in enumerate(run_ids)
                },
            }
        )
    _write_labels(labels, rows)

    alignment = tmp_path / "alignment.jsonl"
    _private_write(alignment, '{"opaque":"PRIVATE_ALIGNMENT_SENTINEL"}\n')
    outputs: dict[str, Path] = {}
    for run_id in ("original", *run_ids):
        output = tmp_path / f"{run_id}.jsonl"
        _private_write(output, f'{{"opaque":"PRIVATE_OUTPUT_SENTINEL_{run_id}"}}\n')
        outputs[run_id] = output

    files: dict[str, Any] = {
        "labels": labels,
        "manifest": tmp_path / "manifest.json",
        "alignment": alignment,
        "outputs": outputs,
        "rows": rows,
        "run_ids": run_ids,
    }
    _write_manifest(files)
    return files


def _refresh(files: dict[str, Any]) -> None:
    _write_labels(files["labels"], files["rows"])
    _write_manifest(files)


def _evaluate(files: dict[str, Any]) -> dict[str, Any]:
    return parity.evaluate_gmail_fact_parity(
        files["labels"],
        files["manifest"],
        files["alignment"],
        files["outputs"],
    )


def test_fact_parity_scores_three_runs_with_bound_private_provenance(
    tmp_path: Path,
) -> None:
    files = _write_evidence(tmp_path)

    result = _evaluate(files)

    assert result["version"] == "gmail_fact_parity_evaluation_v2"
    assert result["gate_passed"] is True
    assert result["private_content_printed"] is False
    assert result["runs"]["v2-a"]["stages"]["candidate"] == {
        "original_useful_non_temporal_units": 20,
        "raw_present_units": 20,
        "retained_units": 20,
        "retention": 1.0,
        "v2_units": 20,
        "v2_members": 20,
        "supported_scope_correct_members": 20,
        "precision": 1.0,
        "duplicate_members": 0,
        "critical_error_members": 0,
        "critical_error_units": 0,
    }
    assert result["runs"]["v2-a"]["macro_candidate_recall"] == 1.0
    assert len(result["run_agreement"]) == 9
    assert all(item["agreement"] == 1.0 for item in result["run_agreement"])
    assert (
        result["provenance"]["evaluator_sha256"]
        == hashlib.sha256(MODULE_PATH.read_bytes()).hexdigest()
    )
    assert set(result["provenance"]["run_output_sha256"]) == {
        "original",
        *RUN_IDS,
    }
    serialized = json.dumps(result, sort_keys=True)
    assert "PRIVATE_ALIGNMENT_SENTINEL" not in serialized
    assert "PRIVATE_OUTPUT_SENTINEL" not in serialized
    assert str(tmp_path) not in serialized


def test_recall_requires_good_v2_evidence_and_precision_is_run_specific(
    tmp_path: Path,
) -> None:
    files = _write_evidence(tmp_path)
    for row in files["rows"][:2]:
        member = row["v2"]["v2-b"]["members"][0]
        member["supported"] = False
    _refresh(files)

    result = _evaluate(files)

    candidate_a = result["runs"]["v2-a"]["stages"]["candidate"]
    candidate_b = result["runs"]["v2-b"]["stages"]["candidate"]
    assert candidate_a["retention"] == 1.0
    assert candidate_a["precision"] == 1.0
    assert candidate_b["raw_present_units"] == 20
    assert candidate_b["retained_units"] == 18
    assert candidate_b["retention"] == 0.9
    assert candidate_b["precision"] == 0.9
    assert result["runs"]["v2-b"]["gates"]["candidate_retention"] is False
    assert result["runs"]["v2-b"]["gates"]["candidate_precision"] is False
    assert result["gate_passed"] is False
    pair = next(
        item
        for item in result["run_agreement"]
        if item["first"] == "v2-a"
        and item["second"] == "v2-b"
        and item["stage"] == "candidate"
    )
    assert pair["agreement"] == 0.9
    assert pair["passed"] is False


def test_member_level_critical_judgment_does_not_leak_between_runs(
    tmp_path: Path,
) -> None:
    files = _write_evidence(tmp_path)
    bad_member = files["rows"][0]["v2"]["v2-c"]["members"][0]
    bad_member["critical_error"] = "wrong_entity"
    _refresh(files)

    result = _evaluate(files)

    assert result["runs"]["v2-a"]["critical_error_members"] == 0
    assert result["runs"]["v2-a"]["gates"]["no_critical_errors"] is True
    assert result["runs"]["v2-c"]["critical_error_members"] == 1
    assert result["runs"]["v2-c"]["gates"]["no_critical_errors"] is False
    assert result["gate_passed"] is False


def test_review_and_persisted_recall_are_independently_gated(tmp_path: Path) -> None:
    files = _write_evidence(tmp_path)
    first = files["rows"][0]["v2"]["v2-b"]["members"][0]
    first["stages"]["review"] = False
    first["stages"]["persisted"] = False
    files["rows"][0]["v2"]["v2-b"] = _arm(files["rows"][0]["v2"]["v2-b"]["members"])
    _refresh(files)

    result = _evaluate(files)

    assert result["runs"]["v2-b"]["stages"]["candidate"]["retention"] == 1.0
    assert result["runs"]["v2-b"]["stages"]["review"]["retention"] == 0.95
    assert result["runs"]["v2-b"]["stages"]["persisted"]["retention"] == 0.95
    assert result["gate_passed"] is True

    second = files["rows"][1]["v2"]["v2-b"]["members"][0]
    second["stages"]["review"] = False
    second["stages"]["persisted"] = False
    files["rows"][1]["v2"]["v2-b"] = _arm(files["rows"][1]["v2"]["v2-b"]["members"])
    _refresh(files)

    result = _evaluate(files)

    assert result["runs"]["v2-b"]["stages"]["review"]["retention"] == 0.9
    assert result["runs"]["v2-b"]["gates"]["review_retention"] is False
    assert result["runs"]["v2-b"]["gates"]["persisted_retention"] is False
    assert result["gate_passed"] is False


def test_precision_is_member_based_and_duplicates_fail_closed(tmp_path: Path) -> None:
    files = _write_evidence(tmp_path)
    evidence = files["rows"][0]["v2"]["v2-a"]
    evidence["members"].append(_member(99_001, supported=False, scope_correct=False))
    files["rows"][0]["v2"]["v2-a"] = _arm(evidence["members"])
    _refresh(files)

    result = _evaluate(files)

    candidate = result["runs"]["v2-a"]["stages"]["candidate"]
    assert candidate["v2_units"] == 20
    assert candidate["v2_members"] == 21
    assert candidate["supported_scope_correct_members"] == 20
    assert candidate["precision"] == 20 / 21
    assert candidate["duplicate_members"] == 1
    assert result["runs"]["v2-a"]["gates"]["no_duplicate_members"] is False
    assert result["gate_passed"] is False


def test_fact_parity_requires_three_independent_v2_runs(tmp_path: Path) -> None:
    files = _write_evidence(tmp_path, run_ids=("v2-a", "v2-b"))

    with pytest.raises(parity.GmailFactParityError, match="at least 3 V2 runs"):
        _evaluate(files)


@pytest.mark.parametrize(
    ("missing", "match"),
    [
        ("alignment", "alignment artifact is required"),
        ("outputs", "run output artifact coverage is incomplete"),
    ],
)
def test_fact_parity_fails_closed_without_bound_artifacts(
    tmp_path: Path, missing: str, match: str
) -> None:
    files = _write_evidence(tmp_path)

    alignment = None if missing == "alignment" else files["alignment"]
    outputs = None if missing == "outputs" else files["outputs"]
    with pytest.raises(parity.GmailFactParityError, match=match):
        parity.evaluate_gmail_fact_parity(
            files["labels"], files["manifest"], alignment, outputs
        )


def test_fact_parity_rejects_stale_evaluator_alignment_and_output(
    tmp_path: Path,
) -> None:
    files = _write_evidence(tmp_path / "evaluator")
    manifest = json.loads(files["manifest"].read_text(encoding="utf-8"))
    manifest["evaluator_sha256"] = "0" * 64
    _private_write(files["manifest"], json.dumps(manifest, sort_keys=True))
    with pytest.raises(parity.GmailFactParityError, match="evaluator artifact"):
        _evaluate(files)

    files = _write_evidence(tmp_path / "alignment")
    _private_write(files["alignment"], "changed alignment\n")
    with pytest.raises(parity.GmailFactParityError, match="alignment artifact"):
        _evaluate(files)

    files = _write_evidence(tmp_path / "output")
    _private_write(files["outputs"]["v2-b"], "changed output\n")
    with pytest.raises(parity.GmailFactParityError, match="v2-b output artifact"):
        _evaluate(files)


def test_fact_parity_requires_private_distinct_artifacts(tmp_path: Path) -> None:
    files = _write_evidence(tmp_path)
    os.chmod(files["alignment"], 0o644)
    with pytest.raises(parity.GmailFactParityError, match="mode 0600"):
        _evaluate(files)

    os.chmod(files["alignment"], 0o600)
    outputs = dict(files["outputs"])
    hardlink = tmp_path / "v2-a-hardlink.jsonl"
    os.link(outputs["v2-a"], hardlink)
    outputs["v2-b"] = hardlink
    with pytest.raises(parity.GmailFactParityError, match="distinct files"):
        parity.evaluate_gmail_fact_parity(
            files["labels"], files["manifest"], files["alignment"], outputs
        )

    manifest = json.loads(files["manifest"].read_text(encoding="utf-8"))
    manifest["alignment_sha256"] = hashlib.sha256(
        files["labels"].read_bytes()
    ).hexdigest()
    _private_write(files["manifest"], json.dumps(manifest, sort_keys=True))
    with pytest.raises(parity.GmailFactParityError, match="alignment.*distinct"):
        parity.evaluate_gmail_fact_parity(
            files["labels"], files["manifest"], files["labels"], files["outputs"]
        )


def test_fact_parity_rejects_incomplete_or_reused_member_alignment(
    tmp_path: Path,
) -> None:
    files = _write_evidence(tmp_path / "coverage")
    files["rows"][0]["v2"].pop("v2-c")
    _write_labels(files["labels"], files["rows"])
    manifest = json.loads(files["manifest"].read_text(encoding="utf-8"))
    manifest["labels_sha256"] = hashlib.sha256(files["labels"].read_bytes()).hexdigest()
    _private_write(files["manifest"], json.dumps(manifest, sort_keys=True))
    with pytest.raises(parity.GmailFactParityError, match="run coverage"):
        _evaluate(files)

    files = _write_evidence(tmp_path / "reuse")
    reused = files["rows"][0]["v2"]["v2-a"]["members"][0]["member_id"]
    files["rows"][1]["v2"]["v2-a"]["members"][0]["member_id"] = reused
    _refresh(files)
    with pytest.raises(parity.GmailFactParityError, match="multiple semantic units"):
        _evaluate(files)


def test_fact_parity_rejects_unreconciled_or_nonmonotonic_member_stages(
    tmp_path: Path,
) -> None:
    files = _write_evidence(tmp_path / "count")
    files["rows"][0]["v2"]["v2-a"]["stage_counts"]["candidate"] = 2
    _write_labels(files["labels"], files["rows"])
    _write_manifest(files)
    with pytest.raises(
        parity.GmailFactParityError, match="stage count is unreconciled"
    ):
        _evaluate(files)

    files = _write_evidence(tmp_path / "monotonic")
    member = files["rows"][0]["v2"]["v2-a"]["members"][0]
    member["stages"]["review"] = False
    member["stages"]["persisted"] = True
    files["rows"][0]["v2"]["v2-a"] = _arm([member])
    _refresh(files)
    with pytest.raises(parity.GmailFactParityError, match="not monotonic"):
        _evaluate(files)


def test_old_global_judgment_schema_is_rejected(tmp_path: Path) -> None:
    files = _write_evidence(tmp_path)
    old_row = dict(files["rows"][0])
    old_row.update(
        {
            "version": "gmail_fact_parity_unit_v1",
            "supported": True,
            "scope_correct": True,
            "critical_error": "none",
        }
    )
    _private_write(files["labels"], json.dumps(old_row) + "\n")
    manifest = json.loads(files["manifest"].read_text(encoding="utf-8"))
    manifest["labels_sha256"] = hashlib.sha256(files["labels"].read_bytes()).hexdigest()
    manifest["label_unit_count"] = 1
    _private_write(files["manifest"], json.dumps(manifest))

    with pytest.raises(parity.GmailFactParityError, match="label schema"):
        _evaluate(files)


def test_run_output_argument_parser_rejects_ambiguity() -> None:
    assert parity._run_output_arguments(["v2-a=/private/a.jsonl"]) == {
        "v2-a": Path("/private/a.jsonl")
    }
    with pytest.raises(parity.GmailFactParityError, match="RUN_ID=PATH"):
        parity._run_output_arguments(["v2-a"])
    with pytest.raises(parity.GmailFactParityError, match="invalid"):
        parity._run_output_arguments(["v2-a=/a", "v2-a=/b"])
