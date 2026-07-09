from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import re
from typing import Any

from .db import connection, loads, rows
from .fact_relations import (
    CONTRADICTION_RECALL_THRESHOLD,
    FALSE_CONFLICT_RATE_THRESHOLD,
    classify_fact_relation,
)
from .gardener import deterministic_topology_candidates, tokenize_signal
from .paths import BrainPaths
from .retrieval_fixtures import load_retrieval_golden_cases
from .service import BrainService
from .util import new_id, now_iso
from .wiki_facts import fact_is_auto_winner, fact_similarity_signals, facts_should_merge


EVAL_SUITES = {"extraction", "routing", "topology", "conflict", "relations", "retrieval"}
VERDICT_VALUES = {"no_strong_match": 0.0, "partial": 0.5, "found": 1.0}
EXTRACTION_LABELS_FILENAME = "extraction_labels.jsonl"
EXTRACTION_FALLBACK_PAGE_HINTS = {"concepts/extracted-facts.md"}


def run_eval(
    paths: BrainPaths,
    *,
    suite: str | None = None,
    report_dir: Path | None = None,
) -> dict[str, Any]:
    selected = [suite] if suite else sorted(EVAL_SUITES)
    unknown = [item for item in selected if item not in EVAL_SUITES]
    if unknown:
        raise ValueError(f"unknown eval suite: {', '.join(unknown)}")
    reports = [run_eval_suite(paths, item) for item in selected]
    generated_at = now_iso()
    package_version = current_package_version()
    result = {
        "id": new_id("eval"),
        "generated_at": generated_at,
        "generated_date": generated_at[:10],
        "package_version": package_version,
        "suite": suite or "all",
        "reports": reports,
        "passed": all(report["passed"] for report in reports),
    }
    output_dir = report_dir or paths.home / "reports" / "evals"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / eval_report_filename(result)
    output_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report_path"] = str(output_path)
    return result


def current_package_version() -> str:
    try:
        return version("pkm-brain")
    except PackageNotFoundError:
        return "0.0.0+unknown"


def eval_report_filename(result: dict[str, Any]) -> str:
    suite = safe_filename_part(str(result.get("suite") or "all"))
    package_version = safe_filename_part(str(result.get("package_version") or "unknown"))
    generated_date = safe_filename_part(str(result.get("generated_date") or str(result.get("generated_at") or "")[:10]))
    eval_id = safe_filename_part(str(result.get("id") or new_id("eval")))
    return f"eval-{suite}-v{package_version}-{generated_date}-{eval_id}.json"


def safe_filename_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    return cleaned.strip("-") or "unknown"


def run_eval_suite(paths: BrainPaths, suite: str) -> dict[str, Any]:
    if suite == "extraction":
        return extraction_eval(paths)
    if suite == "routing":
        return routing_eval(paths)
    if suite == "topology":
        return topology_eval(paths)
    if suite == "conflict":
        return conflict_eval(paths)
    if suite == "relations":
        return relations_eval(paths)
    if suite == "retrieval":
        return retrieval_eval(paths)
    raise ValueError(f"unknown eval suite: {suite}")


def extraction_eval(paths: BrainPaths) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        total_facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        total = conn.execute(
            """
            SELECT COUNT(*)
            FROM facts
            WHERE COALESCE(extraction_method, 'legacy') != 'legacy'
            """
        ).fetchone()[0]
        with_spans = conn.execute(
            """
            SELECT COUNT(*)
            FROM facts
            WHERE COALESCE(extraction_method, 'legacy') != 'legacy'
              AND source_spans IS NOT NULL
              AND source_spans != ''
              AND source_spans != '[]'
            """
        ).fetchone()[0]
    label_cases = load_extraction_label_cases(paths)
    label_metrics, label_reports, label_passed = evaluate_extraction_label_cases(label_cases)
    threshold = {
        "span_coverage": 0.8,
        "auto_support_precision": 1.0,
        "auto_route_accuracy": 1.0,
        "fallback_auto_eligible_count": 0,
        "unsupported_auto_eligible_count": 0,
        "route_mismatch_auto_eligible_count": 0,
        "min_auto_eligible_count": 1,
    }
    legacy_excluded = total_facts - total
    if total_facts == 0 and not label_cases:
        return suite_report(
            "extraction",
            fixture_count=0,
            metrics={
                "skipped": True,
                "reason": "no facts to evaluate",
                **label_metrics,
            },
            passed=True,
            threshold=threshold,
        )
    span_coverage = with_spans / total if total else 1.0
    span_passed = span_coverage >= threshold["span_coverage"]
    metrics = {
        "span_coverage": span_coverage,
        "eligible_fact_count": total,
        "legacy_excluded_count": legacy_excluded,
        **label_metrics,
    }
    if total == 0:
        metrics["policy"] = "legacy facts are excluded from extraction span coverage"
    return suite_report(
        "extraction",
        fixture_count=max(total, len(label_cases)),
        metrics=metrics,
        passed=span_passed and (label_passed if label_cases else True),
        threshold=threshold,
        cases=label_reports if label_cases else None,
    )


def load_extraction_label_cases(paths: BrainPaths) -> list[dict[str, Any]]:
    label_path = paths.evals / EXTRACTION_LABELS_FILENAME
    if not label_path.exists():
        return []
    cases: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(label_path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parsed = json.loads(line)
        if not isinstance(parsed, dict):
            raise ValueError(f"{label_path}:{line_number} must be a JSON object")
        parsed.setdefault("id", f"line_{line_number}")
        cases.append(parsed)
    return cases


def evaluate_extraction_label_cases(
    cases: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    if not cases:
        return {
            "label_policy": "unlabeled",
            "label_case_count": 0,
            "label_file": f"evals/{EXTRACTION_LABELS_FILENAME}",
        }, [], False
    reports = [evaluate_extraction_label_case(case) for case in cases]
    auto_reports = [report for report in reports if report["auto_eligible"]]
    keep_reports = [report for report in reports if report["keep"]]
    unsupported_auto = [report["id"] for report in auto_reports if not report["supported_by_quote"]]
    route_mismatch_auto = [report["id"] for report in auto_reports if not report["route_correct"]]
    fallback_auto = [report["id"] for report in auto_reports if report["fallback_route"]]
    metrics = {
        "label_policy": "labeled",
        "label_file": f"evals/{EXTRACTION_LABELS_FILENAME}",
        "label_case_count": len(reports),
        "keep_count": len(keep_reports),
        "auto_eligible_count": len(auto_reports),
        "keep_precision": round(ratio(len(keep_reports), len(reports)), 3),
        "auto_support_precision": round(
            ratio(
                len([report for report in auto_reports if report["supported_by_quote"]]),
                len(auto_reports),
            ),
            3,
        ),
        "auto_route_accuracy": round(
            ratio(
                len([report for report in auto_reports if report["route_correct"]]),
                len(auto_reports),
            ),
            3,
        ),
        "fallback_auto_eligible_count": len(fallback_auto),
        "fallback_auto_eligible_case_ids": fallback_auto,
        "unsupported_auto_eligible_count": len(unsupported_auto),
        "unsupported_auto_eligible_case_ids": unsupported_auto,
        "route_mismatch_auto_eligible_count": len(route_mismatch_auto),
        "route_mismatch_auto_eligible_case_ids": route_mismatch_auto,
    }
    passed = (
        len(auto_reports) > 0
        and not unsupported_auto
        and not route_mismatch_auto
        and not fallback_auto
    )
    return metrics, reports, passed


def evaluate_extraction_label_case(case: dict[str, Any]) -> dict[str, Any]:
    page_hint = canonical_label_page_hint(case.get("page_hint"))
    expected_page_hint = canonical_label_page_hint(case.get("expected_page_hint"))
    fallback_route = page_hint in EXTRACTION_FALLBACK_PAGE_HINTS
    keep = label_bool(case, "keep", default=label_bool(case, "expected_keep", default=True))
    supported_by_quote = label_bool(case, "supported_by_quote", default=keep)
    if expected_page_hint:
        route_correct = page_hint == expected_page_hint
    else:
        route_correct = label_bool(case, "route_correct", default=keep and not fallback_route)
    auto_eligible = label_bool(
        case,
        "auto_eligible",
        default=label_bool(
            case,
            "expected_auto_eligible",
            default=keep and supported_by_quote and route_correct and not fallback_route,
        ),
    )
    return {
        "id": str(case.get("id") or ""),
        "statement": str(case.get("statement") or "")[:240],
        "page_hint": page_hint,
        "expected_page_hint": expected_page_hint,
        "keep": keep,
        "supported_by_quote": supported_by_quote,
        "route_correct": route_correct,
        "fallback_route": fallback_route,
        "auto_eligible": auto_eligible,
        "issue_label": str(case.get("issue_label") or ""),
    }


def label_bool(case: dict[str, Any], key: str, *, default: bool) -> bool:
    if key not in case:
        return default
    value = case.get(key)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "keep", "pass"}


def canonical_label_page_hint(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return raw if raw.endswith(".md") else f"{raw}.md"


def routing_eval(paths: BrainPaths) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        routed = conn.execute(
            """
            SELECT COUNT(*)
            FROM facts
            WHERE page_hint IS NOT NULL
              AND page_hint != ''
              AND entity_key IS NOT NULL
              AND entity_key != ''
            """
        ).fetchone()[0]
    threshold = {"routing_coverage": 0.85}
    if total == 0:
        return suite_report(
            "routing",
            fixture_count=0,
            metrics={"skipped": True, "reason": "no facts to evaluate"},
            passed=True,
            threshold=threshold,
        )
    accuracy = routed / total
    return suite_report(
        "routing",
        fixture_count=total,
        metrics={"routing_coverage": accuracy},
        passed=accuracy >= threshold["routing_coverage"],
        threshold=threshold,
    )


def topology_eval(paths: BrainPaths) -> dict[str, Any]:
    pages, contracts, expected = topology_fixture()
    candidates = deterministic_topology_candidates(pages, contracts)
    actual = {str(candidate.get("candidate_key") or "") for candidate in candidates}
    expected_keys = set(expected)
    true_positive = actual & expected_keys
    false_positive = actual - expected_keys
    false_negative = expected_keys - actual
    precision = len(true_positive) / len(actual) if actual else 1.0
    recall = len(true_positive) / len(expected_keys) if expected_keys else 1.0
    f1 = (
        (2 * precision * recall) / (precision + recall)
        if precision + recall
        else 0.0
    )
    threshold = {"merge_split_f1": 0.75, "candidate_precision": 0.8}
    return suite_report(
        "topology",
        fixture_count=len(expected_keys),
        metrics={
            "candidate_precision": round(precision, 3),
            "candidate_recall": round(recall, 3),
            "merge_split_f1": round(f1, 3),
            "candidate_generation_count": len(actual),
            "true_positive_count": len(true_positive),
            "false_positive_keys": sorted(false_positive),
            "false_negative_keys": sorted(false_negative),
        },
        passed=f1 >= threshold["merge_split_f1"]
        and precision >= threshold["candidate_precision"],
        threshold=threshold,
    )


def topology_fixture() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], set[str]]:
    pages = [
        topology_page(
            "concepts/alpha-payment.md",
            [
                {
                    "id": "fact_alpha_left",
                    "statement": "AlphaPay payment retry uses Stripe Checkout for renewal invoices.",
                    "entity_key": "product:alphapay:billing",
                    "section_hint": "Summary",
                    "source_ids": ["document:alpha-billing"],
                },
                {
                    "id": "fact_alpha_left_second",
                    "statement": "AlphaPay billing recovery follows the same renewal invoice runbook.",
                    "entity_key": "product:alphapay:billing",
                    "section_hint": "Summary",
                    "source_ids": ["document:alpha-billing"],
                },
            ],
        ),
        topology_page(
            "concepts/alpha-payments.md",
            [
                {
                    "id": "fact_alpha_right",
                    "statement": "AlphaPay payment retry uses Stripe Checkout for renewal invoices.",
                    "entity_key": "product:alphapay:billing",
                    "section_hint": "Summary",
                    "source_ids": ["document:alpha-billing"],
                },
                {
                    "id": "fact_alpha_right_second",
                    "statement": "AlphaPay billing recovery follows the same renewal invoice runbook.",
                    "entity_key": "product:alphapay:billing",
                    "section_hint": "Summary",
                    "source_ids": ["document:alpha-billing"],
                },
            ],
        ),
        topology_page(
            "concepts/patio-ev-outlet.md",
            [
                {
                    "id": "fact_ev_single",
                    "statement": "The Tesla wall connector permit belongs with the home EV charging project.",
                    "entity_key": "project:home-ev-charging:electrical",
                    "section_hint": "Electrical",
                    "source_ids": ["document:ev-plan"],
                }
            ],
        ),
        topology_page(
            "projects/home-ev-charging.md",
            [
                {
                    "id": "fact_ev_destination_a",
                    "statement": "Home EV charging work includes the Tesla wall connector permit.",
                    "entity_key": "project:home-ev-charging:electrical",
                    "section_hint": "Electrical",
                    "source_ids": ["document:ev-plan"],
                },
                {
                    "id": "fact_ev_destination_b",
                    "statement": "Home EV charging has a panel-load review before installation.",
                    "entity_key": "project:home-ev-charging:electrical",
                    "section_hint": "Risks",
                    "source_ids": ["document:ev-load"],
                },
            ],
        ),
        topology_page(
            "projects/sprawling-platform.md",
            [
                {
                    "id": f"fact_sprawl_{index}",
                    "statement": f"Sprawling platform {section.lower()} item needs a narrower home.",
                    "entity_key": "project:sprawling-platform:summary",
                    "section_hint": section,
                    "source_ids": [f"document:sprawl-{index}"],
                }
                for index, section in enumerate(
                    ["Pricing", "Technical", "Customers", "Risks", "Roadmap"]
                )
            ],
        ),
    ]
    contracts = {
        "projects/home-ev-charging.md": {
            "id": "contract_ev",
            "page_hint": "projects/home-ev-charging.md",
            "canonical_entity": "Home EV Charging",
            "page_scope": "Facts about Home EV Charging.",
            "retrieval_purpose": "Answer questions about Home EV Charging.",
            "what_belongs_here": "Tesla wall connector, EV charging, electrical panel, and permit facts.",
            "what_does_not_belong_here": "Mango orchard irrigation sensor facts.",
            "related_pages": [],
        }
    }
    expected = {
        "page_merge:concepts/alpha-payment.md,concepts/alpha-payments.md:",
        "edit_contract:concepts/alpha-payment.md:",
        "edit_contract:concepts/alpha-payments.md:",
        "rehome_fact:concepts/patio-ev-outlet.md,projects/home-ev-charging.md:fact_ev_single",
        "page_split:projects/sprawling-platform.md:",
        "edit_contract:projects/sprawling-platform.md:",
    }
    return pages, contracts, expected


def topology_page(page_hint: str, facts: list[dict[str, Any]]) -> dict[str, Any]:
    section_counts: dict[str, int] = {}
    entity_keys: set[str] = set()
    source_ids: set[str] = set()
    fact_tokens: set[str] = set()
    for fact in facts:
        section = str(fact.get("section_hint") or "Summary")
        section_counts[section] = section_counts.get(section, 0) + 1
        entity_keys.add(str(fact.get("entity_key") or ""))
        source_ids.update(str(source_id) for source_id in fact.get("source_ids") or [])
        fact_tokens.update(
            tokenize_signal(
                fact.get("statement"),
                fact.get("entity_key"),
                fact.get("section_hint"),
                fact.get("source_ids"),
            )
        )
    return {
        "relative_path": page_hint,
        "title": Path(page_hint).stem.replace("-", " ").title(),
        "active_fact_count": len(facts),
        "facts": facts,
        "fact_ids": [str(fact["id"]) for fact in facts],
        "fact_statements": [str(fact["statement"]) for fact in facts],
        "entity_keys": sorted(entity_keys),
        "source_ids": sorted(source_ids),
        "section_counts": section_counts,
        "fact_tokens": sorted(fact_tokens),
        "page_tokens": sorted(tokenize_signal(page_hint)),
    }


def conflict_eval(paths: BrainPaths) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        conflicted = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE status = 'conflicted'"
        ).fetchone()[0]
        timeout_winners = conn.execute(
            """
            SELECT COUNT(*)
            FROM open_questions
            WHERE decided_by = 'timeout_default'
              AND status = 'timeout_resolved'
              AND answer IS NOT NULL
              AND answer != ''
            """
        ).fetchone()[0]
    fixture_cases = conflict_fixture_cases()
    case_reports = [evaluate_conflict_fixture_case(case) for case in fixture_cases]
    contradiction_cases = [
        case for case in case_reports if case["expected_contradiction"]
    ]
    predicted_contradictions = [
        case for case in case_reports if case["actual_contradiction"]
    ]
    contradiction_true_positive = [
        case
        for case in case_reports
        if case["expected_contradiction"] and case["actual_contradiction"]
    ]
    expected_merges = [case for case in case_reports if case["expected_merge"]]
    actual_merges = [case for case in case_reports if case["actual_merge"]]
    merge_true_positive = [
        case for case in case_reports if case["expected_merge"] and case["actual_merge"]
    ]
    expected_auto_supersede = [
        case for case in case_reports if case["expected_auto_supersede"]
    ]
    actual_auto_supersede = [
        case for case in case_reports if case["actual_auto_supersede"]
    ]
    auto_supersede_true_positive = [
        case
        for case in case_reports
        if case["expected_auto_supersede"] and case["actual_auto_supersede"]
    ]
    false_auto_merge = [
        case["id"]
        for case in case_reports
        if case["actual_merge"] and not case["expected_merge"]
    ]
    false_auto_supersede = [
        case["id"]
        for case in case_reports
        if case["actual_auto_supersede"] and not case["expected_auto_supersede"]
    ]
    contradiction_recall = ratio(len(contradiction_true_positive), len(contradiction_cases))
    contradiction_precision = ratio(len(contradiction_true_positive), len(predicted_contradictions))
    merge_recall = ratio(len(merge_true_positive), len(expected_merges))
    merge_precision = ratio(len(merge_true_positive), len(actual_merges))
    auto_supersede_recall = ratio(len(auto_supersede_true_positive), len(expected_auto_supersede))
    auto_supersede_precision = ratio(len(auto_supersede_true_positive), len(actual_auto_supersede))
    threshold = {
        "false_truth_resolutions": 0,
        "false_auto_merge_count": 0,
        "false_auto_supersede_count": 0,
        "contradiction_recall": 1.0,
        "merge_precision": 1.0,
        "auto_supersede_precision": 1.0,
    }
    metrics = {
        "false_truth_resolutions": timeout_winners,
        "conflicted_fact_count": conflicted,
        "fixture_case_count": len(case_reports),
        "contradiction_precision": round(contradiction_precision, 3),
        "contradiction_recall": round(contradiction_recall, 3),
        "merge_precision": round(merge_precision, 3),
        "merge_recall": round(merge_recall, 3),
        "auto_supersede_precision": round(auto_supersede_precision, 3),
        "auto_supersede_recall": round(auto_supersede_recall, 3),
        "false_auto_merge_count": len(false_auto_merge),
        "false_auto_merge_case_ids": false_auto_merge,
        "false_auto_supersede_count": len(false_auto_supersede),
        "false_auto_supersede_case_ids": false_auto_supersede,
    }
    passed = (
        timeout_winners == 0
        and not false_auto_merge
        and not false_auto_supersede
        and contradiction_recall >= threshold["contradiction_recall"]
        and merge_precision >= threshold["merge_precision"]
        and auto_supersede_precision >= threshold["auto_supersede_precision"]
    )
    return suite_report(
        "conflict",
        fixture_count=len(case_reports),
        metrics=metrics,
        passed=passed,
        threshold=threshold,
        cases=case_reports,
    )


def conflict_fixture_cases() -> list[dict[str, Any]]:
    return [
        {
            "id": "near_duplicate_replacement_merges",
            "left": "AlphaPay retry billing uses Stripe Checkout for renewal invoices.",
            "right": "AlphaPay payment retry uses Stripe Checkout for renewal invoices.",
            "expected_merge": True,
            "expected_contradiction": False,
            "newer_fact": conflict_fact(
                "fact_newer_duplicate",
                "AlphaPay payment retry uses Stripe Checkout for renewal invoices.",
                confidence=0.86,
            ),
            "expected_auto_supersede": True,
        },
        {
            "id": "opposite_meaning_high_overlap_not_merge",
            "left": "AlphaPay auto-renewal is enabled by default for annual plans.",
            "right": "AlphaPay auto-renewal is not enabled by default for annual plans.",
            "expected_merge": False,
            "expected_contradiction": True,
            "newer_fact": conflict_fact(
                "fact_low_confidence_opposite",
                "AlphaPay auto-renewal is not enabled by default for annual plans.",
                confidence=0.61,
            ),
            "expected_auto_supersede": False,
        },
        {
            "id": "material_value_contradiction_not_merge",
            "left": "The CloudZero monthly budget cap is 500 dollars.",
            "right": "The CloudZero monthly budget cap is 750 dollars.",
            "expected_merge": False,
            "expected_contradiction": True,
            "newer_fact": conflict_fact(
                "fact_value_change",
                "The CloudZero monthly budget cap is 750 dollars.",
                confidence=0.91,
            ),
            "expected_auto_supersede": True,
        },
        {
            "id": "unsourced_change_not_auto_supersede",
            "left": "The review queue SLA is two days.",
            "right": "The review queue SLA is three days.",
            "expected_merge": False,
            "expected_contradiction": True,
            "newer_fact": conflict_fact(
                "fact_unsourced_change",
                "The review queue SLA is three days.",
                confidence=0.95,
                source_ids=[],
            ),
            "expected_auto_supersede": False,
        },
    ]


def conflict_fact(
    fact_id: str,
    statement: str,
    *,
    confidence: float,
    source_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": fact_id,
        "statement": statement,
        "entity_key": "concepts:conflict-eval:summary",
        "page_hint": "concepts/conflict-eval.md",
        "section_hint": "Summary",
        "source_ids": ["document:conflict-eval"] if source_ids is None else source_ids,
        "confidence": confidence,
        "observed_at": "2026-06-26T00:00:00+00:00",
        "metadata": {"operation": "replace_page"},
    }


def evaluate_conflict_fixture_case(case: dict[str, Any]) -> dict[str, Any]:
    left_fact = conflict_fact("fact_left", str(case["left"]), confidence=0.8)
    right_fact = conflict_fact("fact_right", str(case["right"]), confidence=0.8)
    signals = fact_similarity_signals(str(case["left"]), str(case["right"]))
    actual_merge = facts_should_merge(left_fact, right_fact)
    actual_auto_supersede = fact_is_auto_winner(case["newer_fact"])
    return {
        "id": case["id"],
        "expected_merge": bool(case["expected_merge"]),
        "actual_merge": actual_merge,
        "expected_contradiction": bool(case["expected_contradiction"]),
        "actual_contradiction": bool(signals["contradiction"]),
        "expected_auto_supersede": bool(case["expected_auto_supersede"]),
        "actual_auto_supersede": actual_auto_supersede,
        "sequence_ratio": round(float(signals["sequence_ratio"]), 3),
        "token_overlap": round(float(signals["token_overlap"]), 3),
        "token_jaccard": round(float(signals["token_jaccard"]), 3),
        "anchor_coverage": round(float(signals["anchor_coverage"]), 3),
    }


def relations_eval(paths: BrainPaths) -> dict[str, Any]:
    fixture_cases = relation_fixture_cases()
    mined_cases = mine_answered_relation_cases(paths)
    case_reports = [evaluate_relation_case(case) for case in [*fixture_cases, *mined_cases]]
    contradiction_cases = [
        case for case in case_reports if case["expected_relation"] == "contradicts"
    ]
    predicted_contradictions = [
        case for case in case_reports if case["actual_relation"] == "contradicts"
    ]
    contradiction_true_positive = [
        case
        for case in case_reports
        if case["expected_relation"] == "contradicts"
        and case["actual_relation"] == "contradicts"
    ]
    non_contradiction_cases = [
        case for case in case_reports if case["expected_relation"] != "contradicts"
    ]
    false_conflicts = [
        case
        for case in non_contradiction_cases
        if case["actual_relation"] == "contradicts"
    ]
    exact_relation_matches = [
        case
        for case in case_reports
        if case["expected_relation"] == case["actual_relation"]
    ]
    contradiction_recall = ratio(len(contradiction_true_positive), len(contradiction_cases))
    false_conflict_rate = ratio(len(false_conflicts), len(non_contradiction_cases))
    relation_accuracy = ratio(len(exact_relation_matches), len(case_reports))
    threshold = {
        "contradiction_recall": CONTRADICTION_RECALL_THRESHOLD,
        "false_conflict_rate_max": FALSE_CONFLICT_RATE_THRESHOLD,
    }
    metrics = {
        "label_policy": "fixture_plus_mined" if mined_cases else "fixture_only",
        "label_case_count": len(case_reports),
        "static_fixture_count": len(fixture_cases),
        "mined_case_count": len(mined_cases),
        "contradiction_case_count": len(contradiction_cases),
        "predicted_contradiction_count": len(predicted_contradictions),
        "contradiction_recall": round(contradiction_recall, 3),
        "false_conflict_rate": round(false_conflict_rate, 3),
        "false_conflict_count": len(false_conflicts),
        "false_conflict_case_ids": [case["id"] for case in false_conflicts],
        "relation_accuracy": round(relation_accuracy, 3),
        "classifier_mode": "deterministic_eval_only",
        "activation": "disabled",
    }
    passed = (
        bool(case_reports)
        and contradiction_recall >= threshold["contradiction_recall"]
        and false_conflict_rate <= threshold["false_conflict_rate_max"]
    )
    return suite_report(
        "relations",
        fixture_count=len(case_reports),
        metrics=metrics,
        passed=passed,
        threshold=threshold,
        cases=case_reports,
    )


def relation_fixture_cases() -> list[dict[str, Any]]:
    base = {
        "entity_key": "project:alphapay:summary",
        "page_hint": "projects/alphapay.md",
        "source_ids": ["document:alpha-a"],
    }
    return [
        relation_case(
            "duplicate_same_claim",
            "AlphaPay uses Stripe Checkout for renewal invoices.",
            "AlphaPay uses Stripe Checkout for renewal invoices.",
            "duplicate",
            existing={**base},
            candidate={**base},
        ),
        relation_case(
            "supports_new_source",
            "AlphaPay uses Stripe Checkout for renewal invoices.",
            "AlphaPay uses Stripe Checkout for renewal invoices.",
            "supports",
            existing={**base, "source_ids": ["document:alpha-a"]},
            candidate={**base, "source_ids": ["document:alpha-b"]},
        ),
        relation_case(
            "refines_same_claim",
            "AlphaPay uses Stripe Checkout.",
            "AlphaPay uses Stripe Checkout for renewal invoices after failed card payments.",
            "refines",
            existing={**base},
            candidate={**base},
        ),
        relation_case(
            "temporal_update",
            "As of 2026-06-01, the CloudZero monthly budget cap is 500 dollars.",
            "As of 2026-07-09, the CloudZero monthly budget cap is 750 dollars.",
            "updates",
            existing={
                "entity_key": "account:cloudzero:budget",
                "page_hint": "tools/cloudzero.md",
                "observed_at": "2026-06-01T00:00:00+00:00",
                "source_ids": ["document:cloudzero-june"],
            },
            candidate={
                "entity_key": "account:cloudzero:budget",
                "page_hint": "tools/cloudzero.md",
                "observed_at": "2026-07-09T00:00:00+00:00",
                "source_ids": ["document:cloudzero-july"],
            },
        ),
        relation_case(
            "both_true_progression",
            "Peter had one interview scheduled for the role.",
            "Peter is now in final rounds for the role.",
            "complementary",
            existing={
                "entity_key": "person:peter:career",
                "page_hint": "career/peter.md",
                "source_ids": ["document:career-a"],
            },
            candidate={
                "entity_key": "person:peter:career",
                "page_hint": "career/peter.md",
                "source_ids": ["document:career-b"],
            },
        ),
        relation_case(
            "negation_contradiction",
            "AlphaPay auto-renewal is enabled by default for annual plans.",
            "AlphaPay auto-renewal is not enabled by default for annual plans.",
            "contradicts",
            existing={**base},
            candidate={**base},
        ),
        relation_case(
            "unrelated_different_entity",
            "AlphaPay uses Stripe Checkout for renewal invoices.",
            "The patio outlet permit belongs with the home EV project.",
            "unrelated",
            existing={**base},
            candidate={
                "entity_key": "project:home-ev:electrical",
                "page_hint": "projects/home-ev.md",
                "source_ids": ["document:ev"],
            },
        ),
    ]


def relation_case(
    case_id: str,
    existing_statement: str,
    candidate_statement: str,
    expected_relation: str,
    *,
    existing: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": case_id,
        "origin": "static_fixture",
        "expected_relation": expected_relation,
        "existing": {
            "id": f"{case_id}_existing",
            "statement": existing_statement,
            **existing,
        },
        "candidate": {
            "id": f"{case_id}_candidate",
            "statement": candidate_statement,
            **candidate,
        },
    }


def mine_answered_relation_cases(paths: BrainPaths, *, limit: int = 100) -> list[dict[str, Any]]:
    if not paths.sqlite_path.exists():
        return []
    with connection(paths.sqlite_path) as conn:
        if not eval_table_exists(conn, "open_questions"):
            return []
        question_rows = rows(
            conn,
            """
            SELECT *
            FROM open_questions
            WHERE kind IN ('fact_conflict_review', 'conflict')
              AND status NOT IN ('open', 'needs_human')
              AND answer IS NOT NULL
              AND answer != ''
            ORDER BY COALESCE(answered_at, created_at) DESC
            LIMIT ?
            """,
            (limit,),
        )
    cases: list[dict[str, Any]] = []
    for question in question_rows:
        answer = loads(question["answer"], {})
        expected_relation = relation_from_answer(answer)
        if expected_relation is None:
            continue
        options = loads(question["options"], [])
        candidate = first_option_fact(options, "candidate_fact")
        existing = first_option_fact(options, "existing_fact") or first_option_fact(options, None)
        if not candidate or not existing:
            continue
        case_id = f"mined_{question['id']}"
        cases.append(
            {
                "id": case_id,
                "origin": "answered_queue",
                "question_id": question["id"],
                "expected_relation": expected_relation,
                "candidate": candidate,
                "existing": existing,
            }
        )
    return cases


def relation_from_answer(answer: Any) -> str | None:
    if not isinstance(answer, dict):
        return None
    explicit = str(answer.get("relation") or answer.get("resolution") or "").strip()
    if explicit in {
        "duplicate",
        "supports",
        "refines",
        "updates",
        "complementary",
        "contradicts",
        "unrelated",
    }:
        return explicit
    decision = str(answer.get("decision") or "").strip()
    if decision == "both_true":
        return "complementary"
    if decision in {"dismiss", "reject", "keep_existing"}:
        return "contradicts"
    return None


def first_option_fact(options: list[Any], option_type: str | None) -> dict[str, Any] | None:
    for option in options:
        if not isinstance(option, dict):
            continue
        if option_type is not None and option.get("option_type") != option_type:
            continue
        statement = str(option.get("statement") or "").strip()
        if not statement:
            continue
        fact_id = str(option.get("fact_id") or option.get("id") or "").strip()
        return {
            "id": fact_id or None,
            "statement": statement,
            "entity_key": option.get("entity_key"),
            "page_hint": option.get("page_hint"),
            "source_ids": option.get("source_ids") or [],
            "source_spans": option.get("source_spans") or [],
            "evidence_quote": option.get("evidence_quote"),
            "observed_at": option.get("observed_at"),
        }
    return None


def evaluate_relation_case(case: dict[str, Any]) -> dict[str, Any]:
    result = classify_fact_relation(case["candidate"], case["existing"]).as_dict()
    return {
        "id": case["id"],
        "origin": case.get("origin") or "unknown",
        "question_id": case.get("question_id"),
        "expected_relation": case["expected_relation"],
        "actual_relation": result["relation"],
        "compatible": result["compatible"],
        "confidence": result["confidence"],
        "rationale": result["rationale"],
        "matched": case["expected_relation"] == result["relation"],
        "candidate_statement": str(case["candidate"].get("statement") or "")[:220],
        "existing_statement": str(case["existing"].get("statement") or "")[:220],
    }


def eval_table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
            (table,),
        ).fetchone()
    )


def ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 1.0
    return numerator / denominator


def retrieval_eval(paths: BrainPaths) -> dict[str, Any]:
    svc = BrainService(paths)
    svc.init_workspace()
    with connection(paths.sqlite_path) as conn:
        document_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        fact_count = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    if document_count == 0 and fact_count == 0:
        return suite_report(
            "retrieval",
            fixture_count=0,
            metrics={
                "skipped": True,
                "reason": "no documents or facts in this brain home",
            },
            passed=True,
            threshold={
                "negative_control_fact_leak_count": 0,
                "negative_control_pass_rate": 0.9,
                "verdict_accuracy": 0.7,
                "source_hit_rate": 0.8,
                "fact_precision": 0.5,
                "confidence_ece_max": 0.1,
                "noise_rate_max": 0.5,
            },
        )

    golden_cases = load_retrieval_golden_cases(paths)
    case_reports: list[dict[str, Any]] = []
    verdict_matches = 0
    source_hits = 0
    source_expected = 0
    total_facts = 0
    relevant_facts = 0
    negative_controls = 0
    negative_control_passes = 0
    negative_fact_leaks = 0
    noisy_chunks = 0
    total_chunks = 0
    semantic_probe_count = 0
    semantic_probe_lexical_hits = 0
    semantic_probe_vector_hits = 0
    semantic_probe_vector_gain_ids: list[str] = []
    semantic_probe_vector_hit_ids: list[str] = []
    case_count_by_kind: dict[str, int] = {}
    verdict_matches_by_kind: dict[str, int] = {}
    calibration_rows: list[tuple[float, float]] = []

    for case in golden_cases:
        result = svc.retrieve_context(str(case["query"]), debug=True)
        case_kind = str(case.get("kind") or "uncategorized")
        case_count_by_kind[case_kind] = case_count_by_kind.get(case_kind, 0) + 1
        actual_verdict = str(result.get("retrieval_verdict") or "")
        expected_verdict = str(case["expected_verdict"])
        verdict_match = actual_verdict == expected_verdict
        verdict_matches += int(verdict_match)
        verdict_matches_by_kind[case_kind] = verdict_matches_by_kind.get(case_kind, 0) + int(verdict_match)

        expected_sources = set(case.get("expected_source_ids") or [])
        returned_sources = retrieval_result_source_ids(paths, result)
        source_hit = bool(expected_sources.intersection(returned_sources)) if expected_sources else None
        if expected_sources:
            source_expected += 1
            source_hits += int(bool(source_hit))

        chunks = result.get("supporting_chunks") or []
        facts = result.get("relevant_facts") or []
        noisy_count = sum(1 for chunk in chunks if chunk.get("retrieval_noise_reasons"))
        noisy_chunks += noisy_count
        total_chunks += len(chunks)
        fact_relevant_count = sum(1 for fact in facts if retrieval_fact_is_relevant(fact))
        total_facts += len(facts)
        relevant_facts += fact_relevant_count

        if case.get("kind") == "negative_control":
            negative_controls += 1
            negative_pass = actual_verdict == "no_strong_match" and not facts
            negative_control_passes += int(negative_pass)
            negative_fact_leaks += int(bool(facts))

        semantic_expected_sources = set(case.get("expected_vector_source_ids") or [])
        semantic_lexical_source_hit = None
        semantic_vector_source_hit = None
        semantic_lexical_rank = None
        semantic_vector_rank = None
        if semantic_expected_sources:
            fanout = (result.get("retrieval_debug") or {}).get("fanout") or {}
            lexical_rows = fanout.get("lexical") or []
            vector_rows = fanout.get("vector") or []
            semantic_lexical_rank = first_fanout_source_rank(lexical_rows, semantic_expected_sources)
            semantic_vector_rank = first_fanout_source_rank(vector_rows, semantic_expected_sources)
            semantic_lexical_source_hit = semantic_lexical_rank is not None
            semantic_vector_source_hit = semantic_vector_rank is not None
            semantic_probe_count += 1
            semantic_probe_lexical_hits += int(semantic_lexical_source_hit)
            semantic_probe_vector_hits += int(semantic_vector_source_hit)
            if semantic_vector_source_hit:
                semantic_probe_vector_hit_ids.append(str(case["id"]))
            if semantic_vector_source_hit and not semantic_lexical_source_hit:
                semantic_probe_vector_gain_ids.append(str(case["id"]))

        confidence = float(result.get("retrieval_confidence") or 0.0)
        calibration_rows.append((confidence, VERDICT_VALUES.get(expected_verdict, 0.0)))
        case_reports.append(
            {
                "id": case["id"],
                "kind": case_kind,
                "origin": str(case.get("origin") or "unknown"),
                "fixture_source": str(case.get("fixture_source") or ""),
                "expected_verdict": expected_verdict,
                "actual_verdict": actual_verdict,
                "confidence": confidence,
                "verdict_match": verdict_match,
                "source_hit": source_hit,
                "expected_source_ids": sorted(expected_sources),
                "returned_source_count": len(returned_sources),
                "facts": len(facts),
                "relevant_facts": fact_relevant_count,
                "chunks": len(chunks),
                "noisy_chunks": noisy_count,
                "semantic_expected_source_ids": sorted(semantic_expected_sources),
                "semantic_lexical_source_hit": semantic_lexical_source_hit,
                "semantic_vector_source_hit": semantic_vector_source_hit,
                "semantic_lexical_rank": semantic_lexical_rank,
                "semantic_vector_rank": semantic_vector_rank,
                "retrieval_event_id": result.get("retrieval_event_id"),
            }
        )

    fixture_count = len(golden_cases)
    verdict_accuracy = verdict_matches / fixture_count if fixture_count else 1.0
    source_hit_rate = source_hits / source_expected if source_expected else 1.0
    fact_precision = relevant_facts / total_facts if total_facts else 1.0
    negative_control_pass_rate = (
        negative_control_passes / negative_controls if negative_controls else 1.0
    )
    noise_rate = noisy_chunks / total_chunks if total_chunks else 0.0
    calibration_error = expected_calibration_error(calibration_rows)
    metrics = {
        "verdict_accuracy": round(verdict_accuracy, 3),
        "source_hit_rate": round(source_hit_rate, 3),
        "fact_precision": round(fact_precision, 3),
        "confidence_ece": round(calibration_error, 3),
        "noise_rate": round(noise_rate, 3),
        "negative_control_pass_rate": round(negative_control_pass_rate, 3),
        "negative_control_fact_leak_count": negative_fact_leaks,
        "fixture_count": fixture_count,
        "embedding_provider": svc.embedding_provider.provider,
        "semantic_probe_count": semantic_probe_count,
        "semantic_probe_lexical_source_hit_rate": round(
            ratio(semantic_probe_lexical_hits, semantic_probe_count), 3
        ),
        "semantic_probe_vector_source_hit_rate": round(
            ratio(semantic_probe_vector_hits, semantic_probe_count), 3
        ),
        "semantic_probe_vector_gain_count": len(semantic_probe_vector_gain_ids),
        "semantic_probe_vector_hit_case_ids": semantic_probe_vector_hit_ids,
        "semantic_probe_vector_gain_case_ids": semantic_probe_vector_gain_ids,
        "case_count_by_origin": count_cases_by_field(case_reports, "origin"),
        "metrics_by_origin": retrieval_metrics_by_origin(case_reports),
        "case_count_by_kind": dict(sorted(case_count_by_kind.items())),
        "verdict_accuracy_by_kind": {
            kind: round(verdict_matches_by_kind.get(kind, 0) / count, 3)
            for kind, count in sorted(case_count_by_kind.items())
        },
    }
    threshold = {
        "negative_control_fact_leak_count": 0,
        "negative_control_pass_rate": 0.9,
        "verdict_accuracy": 0.7,
        "source_hit_rate": 0.8,
        "fact_precision": 0.5,
        "confidence_ece_max": 0.1,
        "noise_rate_max": 0.5,
    }
    passed = (
        negative_fact_leaks == 0
        and negative_control_pass_rate >= threshold["negative_control_pass_rate"]
        and verdict_accuracy >= threshold["verdict_accuracy"]
        and source_hit_rate >= threshold["source_hit_rate"]
        and fact_precision >= threshold["fact_precision"]
        and calibration_error <= threshold["confidence_ece_max"]
        and noise_rate <= threshold["noise_rate_max"]
    )
    return suite_report(
        "retrieval",
        fixture_count=fixture_count,
        metrics=metrics,
        passed=passed,
        threshold=threshold,
        cases=case_reports,
    )


def first_fanout_source_rank(rows: list[dict[str, Any]], expected_sources: set[str]) -> int | None:
    for rank, row in enumerate(rows, start=1):
        document_id = str(row.get("document_id") or "")
        if document_id and f"document:{document_id}" in expected_sources:
            return rank
    return None


def count_cases_by_field(case_reports: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for report in case_reports:
        value = str(report.get(field) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def retrieval_metrics_by_origin(case_reports: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        origin: retrieval_case_report_metrics(
            [report for report in case_reports if str(report.get("origin") or "unknown") == origin]
        )
        for origin in count_cases_by_field(case_reports, "origin")
    }


def retrieval_case_report_metrics(case_reports: list[dict[str, Any]]) -> dict[str, Any]:
    if not case_reports:
        return {
            "fixture_count": 0,
            "verdict_accuracy": 1.0,
            "source_hit_rate": 1.0,
            "negative_control_pass_rate": 1.0,
            "negative_control_fact_leak_count": 0,
            "semantic_probe_count": 0,
            "semantic_probe_vector_source_hit_rate": 1.0,
        }
    source_expected_reports = [report for report in case_reports if report.get("expected_source_ids")]
    negative_reports = [report for report in case_reports if report.get("kind") == "negative_control"]
    semantic_reports = [report for report in case_reports if report.get("semantic_expected_source_ids")]
    negative_passes = [
        report
        for report in negative_reports
        if report.get("actual_verdict") == "no_strong_match" and not int(report.get("facts") or 0)
    ]
    return {
        "fixture_count": len(case_reports),
        "verdict_accuracy": round(
            ratio(len([report for report in case_reports if report.get("verdict_match")]), len(case_reports)),
            3,
        ),
        "source_hit_rate": round(
            ratio(
                len([report for report in source_expected_reports if report.get("source_hit")]),
                len(source_expected_reports),
            ),
            3,
        ),
        "negative_control_pass_rate": round(ratio(len(negative_passes), len(negative_reports)), 3),
        "negative_control_fact_leak_count": len(
            [report for report in negative_reports if int(report.get("facts") or 0) > 0]
        ),
        "semantic_probe_count": len(semantic_reports),
        "semantic_probe_vector_source_hit_rate": round(
            ratio(
                len([report for report in semantic_reports if report.get("semantic_vector_source_hit")]),
                len(semantic_reports),
            ),
            3,
        ),
    }


def retrieval_result_source_ids(paths: BrainPaths, result: dict[str, Any]) -> set[str]:
    source_ids: set[str] = set()
    chunk_ids: set[str] = set()

    def add_source_id(value: Any) -> None:
        source_id = str(value or "")
        if not source_id:
            return
        source_ids.add(source_id)
        if source_id.startswith("chunk:"):
            chunk_id = source_id.split(":", 1)[1]
            if chunk_id:
                chunk_ids.add(chunk_id)

    for chunk in result.get("supporting_chunks") or result.get("results") or []:
        document_id = str(chunk.get("document_id") or "")
        if document_id:
            source_ids.add(f"document:{document_id}")
        chunk_id = str(chunk.get("chunk_id") or "")
        if chunk_id:
            chunk_ids.add(chunk_id)
    for page in result.get("relevant_wiki_pages") or []:
        for item in page.get("source_ids") or []:
            add_source_id(item)
    for fact in result.get("relevant_facts") or []:
        for item in fact.get("source_ids") or []:
            add_source_id(item)
        for span in fact.get("source_spans") or []:
            if isinstance(span, dict) and span.get("chunk_id"):
                chunk_ids.add(str(span["chunk_id"]))
    for snapshot in result.get("citation_snapshots") or []:
        if not isinstance(snapshot, dict):
            continue
        document_id = str(snapshot.get("document_id") or "")
        if document_id:
            source_ids.add(f"document:{document_id}")
        for item in snapshot.get("source_ids") or []:
            add_source_id(item)
        for span in snapshot.get("source_spans") or []:
            if isinstance(span, dict) and span.get("chunk_id"):
                chunk_ids.add(str(span["chunk_id"]))
    if chunk_ids:
        placeholders = ",".join("?" for _ in chunk_ids)
        with connection(paths.sqlite_path) as conn:
            for row in conn.execute(
                f"SELECT id, document_id FROM chunks WHERE id IN ({placeholders})",
                sorted(chunk_ids),
            ):
                if row["document_id"]:
                    source_ids.add(f"document:{row['document_id']}")
    return source_ids


def retrieval_fact_is_relevant(fact: dict[str, Any]) -> bool:
    if fact.get("matched_specific_query_terms"):
        return True
    return len(fact.get("matched_query_terms") or []) >= 2


def expected_calibration_error(rows: list[tuple[float, float]], bins: int = 5) -> float:
    if not rows:
        return 0.0
    total = 0.0
    for bucket in range(bins):
        low = bucket / bins
        high = (bucket + 1) / bins
        if bucket == bins - 1:
            bucket_rows = [(conf, value) for conf, value in rows if low <= conf <= high]
        else:
            bucket_rows = [(conf, value) for conf, value in rows if low <= conf < high]
        if not bucket_rows:
            continue
        avg_confidence = sum(conf for conf, _ in bucket_rows) / len(bucket_rows)
        avg_actual = sum(value for _, value in bucket_rows) / len(bucket_rows)
        total += (len(bucket_rows) / len(rows)) * abs(avg_confidence - avg_actual)
    return total


def suite_report(
    suite: str,
    *,
    fixture_count: int,
    metrics: dict[str, Any],
    passed: bool,
    threshold: dict[str, Any],
    cases: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    report = {
        "suite": suite,
        "fixture_count": fixture_count,
        "metrics": metrics,
        "threshold": threshold,
        "passed": passed,
    }
    if cases is not None:
        report["cases"] = cases
    return report
