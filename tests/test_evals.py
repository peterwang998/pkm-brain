from __future__ import annotations

import json
from pathlib import Path

from pkm_brain.evals import purge_retrieval_eval_telemetry, run_eval
from pkm_brain.db import connection, dumps
from pkm_brain.paths import BrainPaths
from pkm_brain.retrieval_fixtures import load_retrieval_golden_cases
from pkm_brain.retrieval_fixtures import RETRIEVAL_GOLDEN_CASES
from pkm_brain.service import BrainService


def test_packaged_retrieval_fixture_base_is_portable() -> None:
    kinds = {}
    for case in RETRIEVAL_GOLDEN_CASES:
        kinds[case["kind"]] = kinds.get(case["kind"], 0) + 1

    assert len(RETRIEVAL_GOLDEN_CASES) >= 3
    assert kinds["public_contract_probe"] >= 2
    assert kinds["negative_control"] >= 1
    assert all(not case["expected_source_ids"] for case in RETRIEVAL_GOLDEN_CASES)


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
        "relations",
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
    assert report_json["package_version"] == result["package_version"]
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


def test_retrieval_eval_reads_local_golden_queries(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths)
    svc.init_workspace()
    note = paths.inbox / "sqlite-decision.md"
    note.write_text(
        "# SQLite Decision\n\nSQLite stores retrieval metadata locally.\n",
        encoding="utf-8",
    )
    svc.ingest()
    with connection(paths.sqlite_path) as conn:
        document_id = conn.execute("SELECT id FROM documents LIMIT 1").fetchone()["id"]
    paths.golden_queries_file.write_text(
        f"""
- id: local-sqlite
  query: SQLite retrieval metadata
  kind: local_query
  expected_verdict: found
  expected_sources:
    - document:{document_id}
""",
        encoding="utf-8",
    )

    loaded = load_retrieval_golden_cases(paths)
    result = run_eval(paths, suite="retrieval")

    report = result["reports"][0]
    local_case = [case for case in report["cases"] if case["id"] == "local-sqlite"][0]
    assert loaded[-1]["origin"] == "local"
    assert loaded[-1]["expected_source_ids"] == [f"document:{document_id}"]
    assert report["metrics"]["case_count_by_origin"]["local"] == 1
    assert report["metrics"]["metrics_by_origin"]["local"]["fixture_count"] == 1
    assert local_case["origin"] == "local"
    assert local_case["source_hit"] is True
    with connection(paths.sqlite_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM retrieval_events").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM context_lineage_events WHERE retrieval_event_id IS NOT NULL"
            ).fetchone()[0]
            == 0
        )


def test_purge_retrieval_eval_telemetry_preserves_real_retrievals(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    golden_query = str(RETRIEVAL_GOLDEN_CASES[0]["query"])
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, source_ids, observed_at, confidence,
              status, metadata, created_at
            ) VALUES ('fact_eval', 'Eval-exposed fact', 'topic:test', '[]',
                      '2026-01-01T00:00:00Z', 0.9, 'active', '{}',
                      '2026-01-01T00:00:00Z')
            """
        )
        for event_id, query in (
            ("retrieval_eval", golden_query),
            ("retrieval_real", "A real user query"),
        ):
            conn.execute(
                """
                INSERT INTO retrieval_events(
                  id, query, timestamp, caller, returned_chunk_ids,
                  selected_chunk_ids, citation_snapshots, debug
                ) VALUES (?, ?, '2026-01-01T00:00:00Z', 'retrieve_context',
                          '[]', '[]', '[]', '{}')
                """,
                (event_id, query),
            )
            conn.execute(
                """
                INSERT INTO context_lineage_events(
                  id, target_type, target_id, event_type, retrieval_event_id,
                  query, weight, metadata, created_at
                ) VALUES (?, 'fact', 'fact_eval', 'exposed', ?, ?, 1.0, ?,
                          '2026-01-01T00:00:00Z')
                """,
                (f"lineage_{event_id}", event_id, query, dumps({"rank": 1})),
            )

    preview = purge_retrieval_eval_telemetry(paths)

    assert preview["status"] == "dry_run"
    assert preview["matched_retrieval_event_count"] == 1
    assert preview["removed_lineage_event_count"] == 1
    assert preview["affected_fact_count"] == 1
    with connection(paths.sqlite_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM context_lineage_events").fetchone()[0]
            == 2
        )

    result = purge_retrieval_eval_telemetry(paths, dry_run=False)

    assert result["status"] == "applied"
    with connection(paths.sqlite_path) as conn:
        callers = {
            row["id"]: row["caller"]
            for row in conn.execute("SELECT id, caller FROM retrieval_events")
        }
        lineage_ids = {
            row["retrieval_event_id"]
            for row in conn.execute(
                "SELECT retrieval_event_id FROM context_lineage_events"
            )
        }
    assert callers == {
        "retrieval_eval": "eval:retrieval_legacy",
        "retrieval_real": "retrieve_context",
    }
    assert lineage_ids == {"retrieval_real"}
    assert (
        purge_retrieval_eval_telemetry(paths, dry_run=False)[
            "matched_retrieval_event_count"
        ]
        == 0
    )


def test_topology_eval_uses_real_candidate_fixture_metrics(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    result = run_eval(paths, suite="topology")

    report = result["reports"][0]
    assert result["passed"] is True
    assert report["fixture_count"] >= 5
    assert report["metrics"]["merge_split_f1"] >= report["threshold"]["merge_split_f1"]
    assert "candidate_generation_smoke" not in report["metrics"]
    assert report["metrics"]["false_negative_keys"] == []


def test_conflict_eval_blocks_opposite_meaning_auto_merge(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    result = run_eval(paths, suite="conflict")

    report = result["reports"][0]
    cases = {case["id"]: case for case in report["cases"]}
    opposite = cases["opposite_meaning_high_overlap_not_merge"]
    assert result["passed"] is True
    assert report["metrics"]["false_auto_merge_count"] == 0
    assert report["metrics"]["false_auto_supersede_count"] == 0
    assert opposite["actual_contradiction"] is True
    assert opposite["actual_merge"] is False


def test_extraction_eval_excludes_legacy_facts_from_span_gate(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, page_hint, section_hint, source_ids,
              observed_at, confidence, status, metadata, created_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fact_no_span",
                "A fact without spans should fail extraction coverage.",
                "concept:test:summary",
                "concepts/test.md",
                "Summary",
                "[]",
                "2026-06-25T00:00:00+00:00",
                0.8,
                "active",
                "{}",
                "2026-06-25T00:00:00+00:00",
                None,
            ),
        )

    result = run_eval(paths, suite="extraction")

    report = result["reports"][0]
    assert result["passed"] is True
    assert report["passed"] is True
    assert report["fixture_count"] == 0
    assert report["metrics"]["span_coverage"] == 1.0
    assert report["metrics"]["eligible_fact_count"] == 0
    assert report["metrics"]["legacy_excluded_count"] == 1


def test_extraction_eval_fails_when_llm_fact_lacks_span_coverage(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, page_hint, section_hint, source_ids,
              source_spans, observed_at, confidence, status, metadata,
              created_at, last_seen_at, extraction_method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fact_llm_no_span",
                "An LLM extracted fact without spans should fail extraction coverage.",
                "concept:test:summary",
                "concepts/test.md",
                "Summary",
                "[]",
                "[]",
                "2026-06-25T00:00:00+00:00",
                0.8,
                "active",
                "{}",
                "2026-06-25T00:00:00+00:00",
                None,
                "llm",
            ),
        )

    result = run_eval(paths, suite="extraction")

    report = result["reports"][0]
    assert result["passed"] is False
    assert report["passed"] is False
    assert report["fixture_count"] == 1
    assert report["metrics"]["span_coverage"] == 0.0


def test_extraction_eval_label_fixture_allows_clean_auto_slice(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    paths.evals.mkdir(parents=True, exist_ok=True)
    labels_path = paths.evals / "extraction_labels.jsonl"
    labels_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "clean_routed_fact",
                        "statement": "The clean fact is routed to a real page.",
                        "page_hint": "concepts/clean.md",
                        "expected_page_hint": "concepts/clean.md",
                        "keep": True,
                        "supported_by_quote": True,
                        "route_correct": True,
                        "auto_eligible": True,
                    }
                ),
                json.dumps(
                    {
                        "id": "fallback_review_fact",
                        "statement": "Fallback-routed facts are valid residue, not clean auto facts.",
                        "page_hint": "concepts/extracted-facts.md",
                        "keep": True,
                        "supported_by_quote": True,
                        "route_correct": False,
                        "auto_eligible": False,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_eval(paths, suite="extraction")

    report = result["reports"][0]
    assert result["passed"] is True
    assert report["passed"] is True
    assert report["metrics"]["label_policy"] == "labeled"
    assert report["metrics"]["label_case_count"] == 2
    assert report["metrics"]["auto_eligible_count"] == 1
    assert report["metrics"]["auto_support_precision"] == 1.0
    assert report["metrics"]["auto_route_accuracy"] == 1.0
    assert report["metrics"]["fallback_auto_eligible_count"] == 0


def test_extraction_eval_label_fixture_blocks_fallback_auto_eligible_fact(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    paths.evals.mkdir(parents=True, exist_ok=True)
    labels_path = paths.evals / "extraction_labels.jsonl"
    labels_path.write_text(
        json.dumps(
            {
                "id": "bad_fallback_auto_fact",
                "statement": "Fallback facts must not be promoted automatically.",
                "page_hint": "concepts/extracted-facts.md",
                "keep": True,
                "supported_by_quote": True,
                "route_correct": True,
                "auto_eligible": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_eval(paths, suite="extraction")

    report = result["reports"][0]
    assert result["passed"] is False
    assert report["passed"] is False
    assert report["metrics"]["fallback_auto_eligible_count"] == 1
    assert report["metrics"]["fallback_auto_eligible_case_ids"] == [
        "bad_fallback_auto_fact"
    ]
