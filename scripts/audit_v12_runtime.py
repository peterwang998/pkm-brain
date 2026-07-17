#!/usr/bin/env python3
"""Read-only corpus audit for a Brain v12 regeneration run."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any


def connect_immutable(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def rows(connection: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql).fetchall()]


def scalar(connection: sqlite3.Connection, sql: str) -> int:
    value = connection.execute(sql).fetchone()[0]
    return int(value or 0)


def fact_attribution_cte(status: str = "active") -> str:
    return f"""
      attributed AS (
        SELECT DISTINCT c.document_id, f.id AS fact_id, f.extraction_method
        FROM facts f
        JOIN json_each(
          CASE WHEN json_valid(f.source_spans) THEN f.source_spans ELSE '[]' END
        ) span
        JOIN chunks c ON c.id = json_extract(span.value, '$.chunk_id')
        WHERE f.status = '{status}'
      )
    """


def cohort_audit(
    current: sqlite3.Connection, previous_path: Path
) -> list[dict[str, Any]]:
    current.execute(
        "ATTACH DATABASE ? AS v11",
        (f"file:{previous_path}?mode=ro&immutable=1",),
    )
    result = rows(
        current,
        f"""
        WITH v11_zero AS (
          SELECT d.id, d.title, w.status AS v11_status,
                 coalesce(json_extract(w.metadata, '$.validation.raw_fact_count'), 0)
                   AS v11_raw_facts,
                 coalesce(json_extract(w.metadata, '$.validation.rejected_count'), 0)
                   AS v11_rejected
          FROM v11.cos_stage_watermarks w
          JOIN v11.documents d ON d.id = w.document_id
          WHERE w.stage = 'extractor'
            AND w.prompt_version = 'extractor-evidence-units-v11-parity-event-time'
            AND coalesce(json_extract(w.metadata, '$.candidate_count'), 0)
                - coalesce(
                    json_extract(
                      w.metadata,
                      '$.validation.structured_event_candidate_count'
                    ),
                    0
                  ) = 0
            AND (
              SELECT coalesce(sum(length(c.text)), 0)
              FROM v11.chunks c
              WHERE c.document_id = d.id
            ) >= 1000
        ),
        final_w AS (
          SELECT w.document_id,
                 w.status AS v12_status,
                 coalesce(
                   json_extract(w.metadata, '$.validation.llm_candidate_count'),
                   coalesce(json_extract(w.metadata, '$.candidate_count'), 0)
                     - coalesce(
                         json_extract(
                           w.metadata,
                           '$.validation.structured_event_candidate_count'
                         ),
                         0
                       ),
                   0
                 ) AS v12_llm_candidates,
                 row_number() OVER (
                   PARTITION BY w.document_id
                   ORDER BY w.processed_at DESC, w.rowid DESC
                 ) AS rn
          FROM cos_stage_watermarks w
          WHERE w.stage = 'extractor'
            AND w.prompt_version = 'extractor-evidence-units-v12-parity-recovery'
        ),
        {fact_attribution_cte()}
        SELECT z.id, z.title, z.v11_status, z.v11_raw_facts, z.v11_rejected,
               coalesce(w.v12_status, 'missing') AS v12_status,
               coalesce(w.v12_llm_candidates, 0) AS v12_llm_candidates,
               count(DISTINCT CASE
                 WHEN a.extraction_method = 'llm' THEN a.fact_id
               END) AS v12_active_llm_facts,
               CASE WHEN count(DISTINCT CASE
                           WHEN a.extraction_method = 'llm' THEN a.fact_id
                         END) > 0
                    THEN 'recovered' ELSE 'not_recovered' END AS outcome
        FROM v11_zero z
        LEFT JOIN final_w w ON w.document_id = z.id AND w.rn = 1
        LEFT JOIN attributed a ON a.document_id = z.id
        GROUP BY z.id, z.title, z.v11_status, z.v11_raw_facts, z.v11_rejected,
                 w.v12_status, w.v12_llm_candidates
        ORDER BY outcome, z.title
        """,
    )
    current.execute("DETACH DATABASE v11")
    return result


def usage_audit(path: Path, run_id: str) -> list[dict[str, Any]]:
    groups: Counter[tuple[str, str, str, str, str]] = Counter()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("run_id") != run_id and item.get("cycle_id") != run_id:
            continue
        key = (
            str(item.get("role") or ""),
            str(item.get("provider") or ""),
            str(item.get("model") or ""),
            str(item.get("reasoning_effort") or ""),
            str(item.get("status") or ""),
        )
        groups[key] += 1
    return [
        {
            "role": key[0],
            "provider": key[1],
            "model": key[2],
            "reasoning_effort": key[3],
            "status": key[4],
            "requests": count,
        }
        for key, count in sorted(groups.items())
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--llm-log", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()

    connection = connect_immutable(args.current)
    cohort = cohort_audit(connection, args.previous)
    summary = json.loads(args.summary.read_text(encoding="utf-8"))

    result: dict[str, Any] = {
        "run_id": args.run_id,
        "facts_by_status_method_model": rows(
            connection,
            """
            SELECT status, extraction_method,
                   coalesce(extractor_model, '(none)') AS extractor_model,
                   count(*) AS fact_count
            FROM facts
            GROUP BY status, extraction_method, extractor_model
            ORDER BY status, extraction_method, extractor_model
            """,
        ),
        "active_fact_count": scalar(
            connection, "SELECT count(*) FROM facts WHERE status = 'active'"
        ),
        "active_exact_statement_duplicates": rows(
            connection,
            """
            SELECT lower(trim(statement)) AS normalized_statement,
                   count(*) AS duplicate_count
            FROM facts
            WHERE status = 'active'
            GROUP BY lower(trim(statement))
            HAVING count(*) > 1
            ORDER BY duplicate_count DESC, normalized_statement
            """,
        ),
        "provenance": {
            "active_invalid_source_spans_json": scalar(
                connection,
                "SELECT count(*) FROM facts WHERE status='active' AND NOT json_valid(source_spans)",
            ),
            "active_empty_source_spans": scalar(
                connection,
                "SELECT count(*) FROM facts WHERE status='active' AND json_array_length(source_spans)=0",
            ),
            "active_empty_evidence_quote": scalar(
                connection,
                "SELECT count(*) FROM facts WHERE status='active' AND trim(coalesce(evidence_quote,''))=''",
            ),
            "unresolved_active_span_entries": scalar(
                connection,
                """
                SELECT count(*)
                FROM facts f
                JOIN json_each(
                  CASE WHEN json_valid(f.source_spans) THEN f.source_spans ELSE '[]' END
                ) span
                LEFT JOIN chunks c ON c.id = json_extract(span.value, '$.chunk_id')
                WHERE f.status='active' AND c.id IS NULL
                """,
            ),
        },
        "v12_watermarks": rows(
            connection,
            """
            SELECT status, count(*) AS document_count,
                   sum(coalesce(json_extract(metadata, '$.candidate_count'), 0))
                     AS candidate_count,
                   sum(coalesce(
                     json_extract(metadata, '$.validation.llm_candidate_count'),
                     coalesce(json_extract(metadata, '$.candidate_count'), 0)
                       - coalesce(json_extract(
                           metadata,
                           '$.validation.structured_event_candidate_count'
                         ), 0),
                     0
                   )) AS llm_candidate_count
            FROM cos_stage_watermarks
            WHERE stage='extractor'
              AND prompt_version='extractor-evidence-units-v12-parity-recovery'
            GROUP BY status
            ORDER BY status
            """,
        ),
        "substantive_v12_zero_llm_candidate_documents": rows(
            connection,
            """
            SELECT d.id, d.title, w.status,
                   coalesce(json_extract(w.metadata, '$.validation.llm_candidate_count'), 0)
                     AS llm_candidate_count,
                   (SELECT coalesce(sum(length(c.text)), 0)
                    FROM chunks c WHERE c.document_id=d.id) AS source_chars
            FROM cos_stage_watermarks w
            JOIN documents d ON d.id=w.document_id
            WHERE w.stage='extractor'
              AND w.prompt_version='extractor-evidence-units-v12-parity-recovery'
              AND coalesce(json_extract(w.metadata, '$.validation.llm_candidate_count'), 0)=0
              AND (SELECT coalesce(sum(length(c.text)), 0)
                   FROM chunks c WHERE c.document_id=d.id) >= 1000
            ORDER BY d.title
            """,
        ),
        "v11_zero_yield_cohort": cohort,
        "v11_zero_yield_cohort_summary": {
            "document_count": len(cohort),
            "recovered_count": sum(item["outcome"] == "recovered" for item in cohort),
            "active_llm_fact_attributions": sum(
                int(item["v12_active_llm_facts"]) for item in cohort
            ),
            "llm_candidates": sum(int(item["v12_llm_candidates"]) for item in cohort),
        },
        "event_counts": rows(
            connection,
            """
            SELECT status, event_time_kind, extraction_method, count(*) AS fact_count
            FROM facts
            WHERE event_time_kind IS NOT NULL
            GROUP BY status, event_time_kind, extraction_method
            ORDER BY status, event_time_kind, extraction_method
            """,
        ),
        "active_event_integrity_violations": rows(
            connection,
            """
            WITH timed AS (
              SELECT f.id, f.statement, f.event_time_kind,
                     sum(CASE WHEN fe.is_primary=1 THEN 1 ELSE 0 END)
                       AS primary_links,
                     sum(CASE WHEN fe.is_primary=1
                                   AND e.entity_type='event'
                                   AND e.status='active'
                              THEN 1 ELSE 0 END)
                       AS active_primary_event_links
              FROM facts f
              LEFT JOIN fact_entities fe ON fe.fact_id=f.id
              LEFT JOIN entities e ON e.id=fe.entity_id
              WHERE f.status='active' AND f.event_time_kind IS NOT NULL
              GROUP BY f.id
            )
            SELECT * FROM timed
            WHERE primary_links<>1 OR active_primary_event_links<>1
            ORDER BY id
            """,
        ),
        "active_structured_event_duplicate_signatures": rows(
            connection,
            """
            WITH structured AS (
              SELECT f.id,
                     coalesce(
                       nullif(lower(trim(json_extract(
                         f.metadata, '$.structured_event_occurrence.title_key'
                       ))), ''),
                       lower(trim(e.name))
                     ) AS occurrence_key,
                     f.event_time_kind, f.event_start_at,
                     coalesce(f.event_end_at, '') AS event_end_at,
                     f.event_time_precision
              FROM facts f
              JOIN fact_entities fe ON fe.fact_id=f.id AND fe.is_primary=1
              JOIN entities e ON e.id=fe.entity_id
              WHERE f.status='active'
                AND f.extraction_method='structured_metadata'
                AND f.event_time_kind IS NOT NULL
            )
            SELECT occurrence_key, event_time_kind, event_start_at, event_end_at,
                   event_time_precision, count(*) AS duplicate_count
            FROM structured
            GROUP BY occurrence_key, event_time_kind, event_start_at, event_end_at,
                     event_time_precision
            HAVING count(*)>1
            ORDER BY duplicate_count DESC, occurrence_key
            """,
        ),
        "event_examples": rows(
            connection,
            """
            WITH examples AS (
              SELECT f.id, f.statement, f.extraction_method, f.event_time_kind,
                     f.event_start_at, f.event_end_at, f.event_time_precision,
                     e.name AS primary_event,
                     (SELECT d.title
                      FROM json_each(f.source_spans) span
                      JOIN chunks c ON c.id=json_extract(span.value, '$.chunk_id')
                      JOIN documents d ON d.id=c.document_id
                      LIMIT 1) AS source_document,
                     row_number() OVER (
                       PARTITION BY f.extraction_method, f.event_time_kind
                       ORDER BY f.event_start_at, f.id
                     ) AS sample_rank
              FROM facts f
              JOIN fact_entities fe ON fe.fact_id=f.id AND fe.is_primary=1
              JOIN entities e ON e.id=fe.entity_id
              WHERE f.status='active' AND f.event_time_kind IS NOT NULL
            )
            SELECT id, statement, extraction_method, event_time_kind,
                   event_start_at, event_end_at, event_time_precision,
                   primary_event, source_document
            FROM examples
            WHERE sample_rank <= 3
            ORDER BY extraction_method, event_time_kind, event_start_at
            """,
        ),
        "summary_validation": {
            key: summary["extraction"]["validation"][key]
            for key in (
                "raw_fact_count",
                "accepted_count",
                "rejected_count",
                "total_rejected_count",
                "dropped_count",
                "structured_event_candidate_count",
                "contract_recovery_warning_count",
                "temporal_enrichment_warning_count",
                "schema_errors",
                "route_resolution_counts",
                "canonical_route_count",
                "canonical_route_rate",
                "fallback_count",
                "invalid_route_destination_count",
            )
        },
        "summary_actions": {
            "candidate_count": summary["extraction"]["candidate_count"],
            "action_status_counts": summary["extraction"]["action_status_counts"],
            "critic_decision_counts": summary["extraction"]["critic_decision_counts"],
            "post_counts": summary["post_counts"],
            "timing": summary["extraction"]["timing"],
        },
        "llm_usage": usage_audit(args.llm_log, args.run_id),
    }
    connection.close()
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
