from __future__ import annotations

from pathlib import Path

from pkm_brain.evals import relation_from_answer, run_eval
from pkm_brain.fact_relations import classify_fact_relation, explicitly_dated
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


def test_relation_classifier_does_not_conflict_distinct_claims_on_broad_entity() -> (
    None
):
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


def test_source_and_knowledge_timestamps_do_not_make_fact_world_dated() -> None:
    fact = {
        "statement": "Atlas is in beta.",
        "observed_at": "2026-03-01T12:00:00+00:00",
        "created_at": "2026-03-02T12:00:00+00:00",
    }

    assert explicitly_dated(fact) is False
    assert explicitly_dated({**fact, "valid_from": "2026-03-01"}) is True


def test_same_statement_in_distinct_intervals_is_update_not_duplicate() -> None:
    existing = {
        "id": "fact_march",
        "statement": "Atlas is in beta.",
        "entity_key": "project:atlas:phase",
        "temporal_kind": "time_bound",
        "valid_from": "2026-03-01",
        "valid_to": "2026-04-01",
        "valid_time_precision": "day",
    }
    candidate = {
        **existing,
        "id": "fact_may",
        "valid_from": "2026-05-01",
        "valid_to": "2026-06-01",
    }

    relation = classify_fact_relation(candidate, existing)

    assert relation.relation == "updates"


def test_conflicting_deadline_dates_do_not_imply_temporal_update() -> None:
    existing = {
        "id": "fact_deadline_july_20",
        "statement": "The Atlas proposal deadline is July 20, 2026.",
        "entity_key": "project:atlas:deadline",
        "page_hint": "projects/atlas.md",
    }
    candidate = {
        "id": "fact_deadline_july_30",
        "statement": "The Atlas proposal deadline is July 30, 2026.",
        "entity_key": "project:atlas:deadline",
        "page_hint": "projects/atlas.md",
    }

    relation = classify_fact_relation(candidate, existing)

    assert relation.relation == "contradicts"
    assert relation.compatible is False


def test_deadline_change_with_non_overlapping_valid_intervals_is_update() -> None:
    existing = {
        "id": "fact_deadline_july_20",
        "statement": "The Atlas proposal deadline is July 20, 2026.",
        "entity_key": "project:atlas:deadline",
        "page_hint": "projects/atlas.md",
        "temporal_kind": "time_bound",
        "valid_from": "2026-07-01",
        "valid_to": "2026-07-21",
        "valid_time_precision": "day",
    }
    candidate = {
        **existing,
        "id": "fact_deadline_july_30",
        "statement": "The Atlas proposal deadline is July 30, 2026.",
        "valid_from": "2026-07-21",
        "valid_to": "2026-08-01",
    }

    relation = classify_fact_relation(candidate, existing)

    assert relation.relation == "updates"


def test_relation_eval_does_not_treat_generic_rejection_as_contradiction_label() -> (
    None
):
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
