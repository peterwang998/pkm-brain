from __future__ import annotations

import json
from pathlib import Path

from pkm_brain.evals import run_eval
from pkm_brain.paths import BrainPaths
from pkm_brain.retrieval_fixtures import RETRIEVAL_GOLDEN_CASES
from pkm_brain.service import BrainService


def test_retrieval_fixture_base_is_stratified() -> None:
    kinds = {}
    for case in RETRIEVAL_GOLDEN_CASES:
        kinds[case["kind"]] = kinds.get(case["kind"], 0) + 1

    assert len(RETRIEVAL_GOLDEN_CASES) >= 70
    assert kinds["historical_session_query"] >= 30
    assert kinds["source_document_query"] >= 10
    assert kinds["fact_query"] >= 10
    assert kinds["negative_control"] >= 15


def test_eval_run_writes_rebuildable_report(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    result = run_eval(paths)

    assert result["passed"] is True
    assert {report["suite"] for report in result["reports"]} == {
        "extraction",
        "routing",
        "topology",
        "conflict",
        "retrieval",
    }
    retrieval_report = [
        report for report in result["reports"] if report["suite"] == "retrieval"
    ][0]
    assert retrieval_report["metrics"]["skipped"] is True
    report_path = Path(result["report_path"])
    assert report_path.exists()
    assert report_path.name.startswith(
        f"eval-all-v{result['package_version']}-{result['generated_date']}-"
    )
    assert report_path.name.endswith(f"{result['id']}.json")
    report_json = json.loads(report_path.read_text(encoding="utf-8"))
    assert report_json["id"] == result["id"]
    assert report_json["package_version"] == "0.1.0"
    assert report_json["generated_date"] == result["generated_at"][:10]


def test_retrieval_eval_can_run_directly_on_empty_brain(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    result = run_eval(paths, suite="retrieval")

    assert result["passed"] is True
    assert result["reports"][0]["suite"] == "retrieval"
    assert result["reports"][0]["fixture_count"] == 0
    assert Path(result["report_path"]).name.startswith(
        f"eval-retrieval-v{result['package_version']}-{result['generated_date']}-"
    )
