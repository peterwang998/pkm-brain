from __future__ import annotations

from pathlib import Path

from pkm_brain.evals import relation_from_answer, run_eval
from pkm_brain.fact_relations import classify_fact_relation
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


def test_relation_classifier_treats_progression_as_complementary() -> None:
    existing = {
        "id": "fact_existing",
        "statement": "Alex had one interview scheduled for the role.",
        "entity_key": "person:alex:career",
        "page_hint": "career/alex.md",
    }
    candidate = {
        "id": "fact_candidate",
        "statement": "Alex is now in final rounds for the role.",
        "entity_key": "person:alex:career",
        "page_hint": "career/alex.md",
    }

    result = classify_fact_relation(candidate, existing)

    assert result.relation == "complementary"
    assert result.compatible is True


def test_relation_classifier_does_not_conflict_distinct_claims_on_broad_entity() -> None:
    existing = {
        "id": "fact_401k",
        "statement": "Northwind's 401(k) match was 100% on contributions up to 1% of base salary.",
        "entity_key": "company:northwind",
        "page_hint": "companies/northwind.md",
    }
    candidate = {
        "id": "fact_fertility",
        "statement": "Northwind offers a $10,000 lifetime fertility benefit with no annual cap.",
        "entity_key": "company:northwind",
        "page_hint": "companies/northwind.md",
    }

    result = classify_fact_relation(candidate, existing)

    assert result.relation != "contradicts"
    assert result.compatible is True


def test_relation_classifier_does_not_conflict_on_generic_expectation_overlap() -> None:
    existing = {
        "statement": "Alex does not expect to have work-life balance at Northwind.",
        "entity_key": "person:alex:career",
        "page_hint": "career/alex.md",
    }
    candidate = {
        "statement": "Alex expected another offer while evaluating Northwind.",
        "entity_key": "person:alex:career",
        "page_hint": "career/alex.md",
    }

    result = classify_fact_relation(candidate, existing)

    assert result.relation != "contradicts"
    assert result.compatible is True


def test_relation_classifier_respects_explicit_entity_mismatch() -> None:
    existing = {
        "statement": "AlphaPay renewal is enabled.",
        "entity_id": "entity_alphapay",
        "page_hint": "projects/payments.md",
    }
    candidate = {
        "statement": "BetaPay renewal is not enabled.",
        "entity_id": "entity_betapay",
        "page_hint": "projects/payments.md",
    }

    result = classify_fact_relation(candidate, existing)

    assert result.relation == "unrelated"


def test_relation_eval_does_not_treat_generic_rejection_as_contradiction_label() -> None:
    assert relation_from_answer({"decision": "dismiss", "reason": "not useful"}) is None
    assert relation_from_answer({"decision": "keep_existing"}) is None
    assert relation_from_answer({"decision": "both_true"}) == "complementary"
    assert relation_from_answer({"decision": "supports_existing"}) == "supports"


def test_relations_eval_passes_static_gate(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    result = run_eval(paths, suite="relations", report_dir=tmp_path / "reports")
    report = result["reports"][0]

    assert result["passed"] is True
    assert report["metrics"]["contradiction_recall"] >= 0.9
    assert report["metrics"]["false_conflict_rate"] <= 0.1
    assert report["metrics"]["activation"] == "approval_gated_w2a"
