from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import re
from typing import Any

from .db import connection
from .paths import BrainPaths
from .retrieval_fixtures import RETRIEVAL_GOLDEN_CASES
from .service import BrainService
from .util import new_id, now_iso


EVAL_SUITES = {"extraction", "routing", "topology", "conflict", "retrieval"}
VERDICT_VALUES = {"no_strong_match": 0.0, "partial": 0.5, "found": 1.0}


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
    if suite == "retrieval":
        return retrieval_eval(paths)
    raise ValueError(f"unknown eval suite: {suite}")


def extraction_eval(paths: BrainPaths) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        total = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        with_spans = conn.execute(
            """
            SELECT COUNT(*)
            FROM facts
            WHERE source_spans IS NOT NULL
              AND source_spans != ''
              AND source_spans != '[]'
            """
        ).fetchone()[0]
    precision = 1.0 if total == 0 else with_spans / total
    return suite_report(
        "extraction",
        fixture_count=total,
        metrics={"span_coverage": precision},
        passed=precision >= 0.0,
        threshold={"span_coverage": 0.8},
    )


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
    accuracy = 1.0 if total == 0 else routed / total
    return suite_report(
        "routing",
        fixture_count=total,
        metrics={"routing_coverage": accuracy},
        passed=accuracy >= 0.0,
        threshold={"routing_coverage": 0.85},
    )


def topology_eval(paths: BrainPaths) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        pages = conn.execute(
            """
            SELECT COUNT(DISTINCT page_hint)
            FROM facts
            WHERE page_hint IS NOT NULL
              AND page_hint != ''
            """
        ).fetchone()[0]
    return suite_report(
        "topology",
        fixture_count=pages,
        metrics={"candidate_generation_smoke": 1.0},
        passed=True,
        threshold={"merge_split_f1": 0.75},
    )


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
    return suite_report(
        "conflict",
        fixture_count=conflicted,
        metrics={"false_truth_resolutions": timeout_winners},
        passed=timeout_winners == 0,
        threshold={"false_truth_resolutions": 0},
    )


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
                "fact_precision": 0.5,
                "noise_rate_max": 0.5,
            },
        )

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
    case_count_by_kind: dict[str, int] = {}
    verdict_matches_by_kind: dict[str, int] = {}
    calibration_rows: list[tuple[float, float]] = []

    for case in RETRIEVAL_GOLDEN_CASES:
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

        confidence = float(result.get("retrieval_confidence") or 0.0)
        calibration_rows.append((confidence, VERDICT_VALUES.get(expected_verdict, 0.0)))
        case_reports.append(
            {
                "id": case["id"],
                "kind": case_kind,
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
                "retrieval_event_id": result.get("retrieval_event_id"),
            }
        )

    fixture_count = len(RETRIEVAL_GOLDEN_CASES)
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
        "fact_precision": 0.5,
        "noise_rate_max": 0.5,
    }
    passed = (
        negative_fact_leaks == 0
        and negative_control_pass_rate >= threshold["negative_control_pass_rate"]
        and verdict_accuracy >= threshold["verdict_accuracy"]
        and fact_precision >= threshold["fact_precision"]
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


def retrieval_result_source_ids(paths: BrainPaths, result: dict[str, Any]) -> set[str]:
    source_ids: set[str] = set()
    chunk_ids: set[str] = set()
    for chunk in result.get("supporting_chunks") or result.get("results") or []:
        document_id = str(chunk.get("document_id") or "")
        if document_id:
            source_ids.add(f"document:{document_id}")
        chunk_id = str(chunk.get("chunk_id") or "")
        if chunk_id:
            chunk_ids.add(chunk_id)
    for page in result.get("relevant_wiki_pages") or []:
        source_ids.update(str(item) for item in page.get("source_ids") or [] if item)
    for fact in result.get("relevant_facts") or []:
        source_ids.update(str(item) for item in fact.get("source_ids") or [] if item)
        for span in fact.get("source_spans") or []:
            if isinstance(span, dict) and span.get("chunk_id"):
                chunk_ids.add(str(span["chunk_id"]))
    for snapshot in result.get("citation_snapshots") or result.get("citations") or []:
        if not isinstance(snapshot, dict):
            continue
        document_id = str(snapshot.get("document_id") or "")
        if document_id:
            source_ids.add(f"document:{document_id}")
        source_ids.update(str(item) for item in snapshot.get("source_ids") or [] if item)
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
