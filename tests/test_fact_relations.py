from __future__ import annotations

from pathlib import Path

from pkm_brain.evals import run_eval
from pkm_brain.fact_relations import classify_fact_relation
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


def test_relation_classifier_treats_progression_as_complementary() -> None:
    existing = {
        "id": "fact_existing",
        "statement": "Peter had one interview scheduled for the role.",
        "entity_key": "person:peter:career",
        "page_hint": "career/peter.md",
    }
    candidate = {
        "id": "fact_candidate",
        "statement": "Peter is now in final rounds for the role.",
        "entity_key": "person:peter:career",
        "page_hint": "career/peter.md",
    }

    result = classify_fact_relation(candidate, existing)

    assert result.relation == "complementary"
    assert result.compatible is True


def test_relations_eval_passes_static_gate(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    result = run_eval(paths, suite="relations", report_dir=tmp_path / "reports")
    report = result["reports"][0]

    assert result["passed"] is True
    assert report["metrics"]["contradiction_recall"] >= 0.9
    assert report["metrics"]["false_conflict_rate"] <= 0.1
    assert report["metrics"]["activation"] == "disabled"
