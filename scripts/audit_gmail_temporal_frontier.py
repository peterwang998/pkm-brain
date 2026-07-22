#!/usr/bin/env python3
"""Audit a private Gmail temporal cohort without printing message content.

The input is a mode-0600 JSONL cohort whose records contain ``sample_id``,
``text``, ``stratum``, and ``message_internal_at``.  The audit rebuilds the
current deterministic evidence inventory, selector packets, candidate frontier,
and page plan locally.  Its stdout contains aggregate counts and fingerprints
only; it never invokes a model or writes a projection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
from collections import Counter
from pathlib import Path
from typing import Any

from pkm_brain.gmail_temporal_batching import (
    GMAIL_TEMPORAL_SINGLETON_EVENT_FALLBACK_DIAGNOSTIC,
    plan_gmail_temporal_selector_batches,
    validate_gmail_temporal_batch_citation,
)
from pkm_brain.gmail_temporal_frontier import (
    build_gmail_temporal_candidate_frontier,
    gmail_temporal_candidate_page_payload,
    plan_gmail_temporal_candidate_pages,
)
from pkm_brain.gmail_temporal_leads import analyze_gmail_temporal_leads
from pkm_brain.gmail_temporal_selection import (
    GMAIL_TEMPORAL_HARD_SCOPE_BLOCKERS,
    GMAIL_TEMPORAL_SUBJECT_TYPES,
    validate_gmail_temporal_selection,
)


AUDIT_VERSION = "gmail_temporal_frontier_audit_v1"
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_PATHS = {
    name: _REPO_ROOT / "src" / "pkm_brain" / f"{name}.py"
    for name in (
        "gmail_temporal_batching",
        "gmail_temporal_frontier",
        "gmail_temporal_leads",
        "gmail_temporal_reduction",
        "gmail_temporal_selection",
    )
}


class GmailTemporalFrontierAuditError(ValueError):
    """Raised when the private cohort is unsafe, malformed, or ambiguous."""


def _gap_candidate_attempts(analysis: Any, plan: Any) -> dict[str, Any]:
    """Describe rejected frontier attempts using semantics and blockers only."""

    attempts: Counter[str] = Counter()
    blockers: Counter[str] = Counter()
    hard_blockers: Counter[str] = Counter()
    semantics: Counter[str] = Counter()
    for batch in plan.batches:
        for expression in batch.expressions:
            for mention in batch.mentions:
                if mention.mention_type not in GMAIL_TEMPORAL_SUBJECT_TYPES:
                    continue
                attempts["attempts"] += 1
                lead_id = next(
                    (
                        item.lead_id
                        for item in batch.lead_hints
                        if item.expression_id == expression.expression_id
                        and item.mention_id == mention.mention_id
                    ),
                    None,
                )
                try:
                    citation = validate_gmail_temporal_batch_citation(
                        batch,
                        batch_fingerprint=batch.manifest.batch_fingerprint,
                        expression_id=expression.expression_id,
                        subject_mention_id=mention.mention_id,
                        lifecycle_mention_id=None,
                        selected_lead_id=lead_id,
                    )
                    selection = validate_gmail_temporal_selection(
                        analysis,
                        {
                            "analysis_fingerprint": analysis.snapshot_fingerprint,
                            "decision": "select_for_review",
                            "associations": [
                                {
                                    "expression_id": citation.expression_id,
                                    "subject_mention_id": citation.subject_mention_id,
                                    "lifecycle_mention_id": None,
                                    "selected_lead_id": citation.selected_lead_id,
                                }
                            ],
                        },
                        batch=batch,
                    )
                except ValueError:
                    attempts["validation_errors"] += 1
                    continue
                if len(selection.associations) != 1:
                    attempts["missing_association"] += 1
                    continue
                association = selection.associations[0]
                attempts["validated"] += 1
                blockers.update(association.blockers)
                hard = GMAIL_TEMPORAL_HARD_SCOPE_BLOCKERS.intersection(
                    association.blockers
                )
                hard_blockers.update(hard)
                attempts["hard_scope_rejected"] += int(bool(hard))
                semantics[
                    "/".join(
                        (
                            association.relation,
                            association.kind,
                            association.lifecycle,
                            selection.decision,
                        )
                    )
                ] += 1
    return {
        "counts": dict(sorted(attempts.items())),
        "blockers": dict(sorted(blockers.items())),
        "hard_scope_blockers": dict(sorted(hard_blockers.items())),
        "semantics": dict(sorted(semantics.items())),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_private_jsonl(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise GmailTemporalFrontierAuditError(
            "cohort must be a regular non-symlink file"
        )
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise GmailTemporalFrontierAuditError("cohort must have mode 0600")
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError) as exc:
        raise GmailTemporalFrontierAuditError(
            "cohort could not be read as JSONL"
        ) from exc
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise GmailTemporalFrontierAuditError("cohort is empty or malformed")
    seen: set[str] = set()
    for row in rows:
        sample_id = row.get("sample_id")
        text = row.get("text")
        stratum = row.get("stratum")
        message_internal_at = row.get("message_internal_at")
        if (
            not isinstance(sample_id, str)
            or not sample_id
            or sample_id in seen
            or not isinstance(text, str)
            or not isinstance(stratum, str)
            or not stratum
            or (
                message_internal_at is not None
                and not isinstance(message_internal_at, str)
            )
        ):
            raise GmailTemporalFrontierAuditError(
                "cohort records have invalid required fields"
            )
        seen.add(sample_id)
    return rows


def audit_gmail_temporal_frontier(path: Path) -> dict[str, Any]:
    """Return content-free aggregate coverage for the current deterministic path."""

    rows = _load_private_jsonl(path)
    cohort_sha256 = _sha256(path)
    counts: Counter[str] = Counter(
        {
            "records": len(rows),
            "singleton_fallback_batches": 0,
            "singleton_fallback_fact_batches": 0,
            "singleton_fallback_temporal_rescue_batches": 0,
            "singleton_fallback_candidates": 0,
            "singleton_fallback_candidates_requiring_defer": 0,
            "singleton_fallback_candidates_not_deferred": 0,
            "records_with_singleton_fallback": 0,
            "records_added_by_singleton_fallback": 0,
        }
    )
    strata: Counter[str] = Counter()
    stratum_coverage: dict[str, Counter[str]] = {}
    relations: Counter[str] = Counter()
    lifecycles: Counter[str] = Counter()
    candidate_blockers: Counter[str] = Counter()
    maxima: Counter[str] = Counter()
    admitted_frontier_gaps: list[dict[str, Any]] = []

    for row in rows:
        text = row["text"]
        stratum = row["stratum"]
        sample_id = row["sample_id"]
        admitted = stratum.startswith(("important_", "durable_"))
        rescue = stratum.startswith("suppressed_")
        strata[stratum] += 1
        coverage = stratum_coverage.setdefault(stratum, Counter())
        coverage["records"] += 1
        counts["admitted_records"] += int(admitted)
        counts["rescue_records"] += int(rescue)

        analysis = analyze_gmail_temporal_leads(
            text=text,
            message_internal_at=row.get("message_internal_at"),
            fact_admitted=admitted,
            temporal_review_rescue=rescue,
            chunk_id=sample_id,
        )
        plan = plan_gmail_temporal_selector_batches(text=text, analysis=analysis)
        counts["expressions"] += len(analysis.expressions)
        counts["mentions"] += len(analysis.mentions)
        counts["leads"] += len(analysis.leads)
        counts["candidate_edges"] += analysis.candidate_edge_count
        counts["retained_edges"] += analysis.retained_edge_count
        counts["graph_truncated_records"] += int(analysis.graph_truncated)
        counts["batches"] += len(plan.batches)
        counts["covered_expressions"] += len(plan.covered_expression_ids)
        counts["batch_omissions"] += len(plan.omissions)
        counts["records_with_expressions"] += int(bool(analysis.expressions))
        counts["records_with_mentions"] += int(bool(analysis.mentions))
        counts["records_with_leads"] += int(bool(analysis.leads))
        coverage["with_expressions"] += int(bool(analysis.expressions))
        coverage["with_mentions"] += int(bool(analysis.mentions))
        coverage["with_leads"] += int(bool(analysis.leads))
        if admitted:
            counts["admitted_with_expressions"] += int(bool(analysis.expressions))
            counts["admitted_with_mentions"] += int(bool(analysis.mentions))
            counts["admitted_with_leads"] += int(bool(analysis.leads))
        maxima["batches_per_record"] = max(
            maxima["batches_per_record"], len(plan.batches)
        )

        record_candidates = 0
        record_pages = 0
        record_empty_frontiers = 0
        record_incomplete_frontiers = 0
        record_singleton_fallback_batches = 0
        record_singleton_fallback_candidates = 0
        for batch in plan.batches:
            singleton_fallback = (
                GMAIL_TEMPORAL_SINGLETON_EVENT_FALLBACK_DIAGNOSTIC in batch.diagnostics
            )
            record_singleton_fallback_batches += int(singleton_fallback)
            counts["singleton_fallback_batches"] += int(singleton_fallback)
            if singleton_fallback:
                counts[
                    f"singleton_fallback_{analysis.association_admission_basis}_batches"
                ] += 1
            frontier = build_gmail_temporal_candidate_frontier(
                analysis=analysis,
                batch=batch,
            )
            page_plan = plan_gmail_temporal_candidate_pages(
                analysis=analysis,
                batch=batch,
            )
            candidate_count = len(frontier.candidates)
            page_count = len(page_plan.pages)
            record_candidates += candidate_count
            if singleton_fallback:
                record_singleton_fallback_candidates += candidate_count
                counts["singleton_fallback_candidates"] += candidate_count
                counts["singleton_fallback_candidates_requiring_defer"] += sum(
                    item.requires_defer for item in frontier.candidates
                )
                counts["singleton_fallback_candidates_not_deferred"] += sum(
                    not item.requires_defer for item in frontier.candidates
                )
            record_pages += page_count
            record_empty_frontiers += int(not frontier.candidates)
            record_incomplete_frontiers += int(not frontier.complete)
            counts["frontier_candidates"] += candidate_count
            counts["frontier_empty_batches"] += int(not frontier.candidates)
            counts["frontier_incomplete_batches"] += int(not frontier.complete)
            counts["omitted_candidate_mentions"] += (
                frontier.omitted_candidate_mention_count
            )
            counts["pages"] += page_count
            counts["overflow_batches"] += int(page_count > 1)
            parent_clusters = {
                cluster.cluster_id
                for page in page_plan.pages
                for cluster in page.clusters
            }
            decision_units = {
                cluster.decision_unit_id
                for page in page_plan.pages
                for cluster in page.clusters
            }
            counts["parent_clusters"] += len(parent_clusters)
            counts["decision_units"] += len(decision_units)
            maxima["candidates_per_batch"] = max(
                maxima["candidates_per_batch"], candidate_count
            )
            maxima["pages_per_batch"] = max(maxima["pages_per_batch"], page_count)
            for candidate in frontier.candidates:
                relations[candidate.relation] += 1
                lifecycles[candidate.lifecycle] += 1
                counts["candidates_requiring_defer"] += int(candidate.requires_defer)
                counts["candidates_with_supporting_lead"] += int(
                    candidate.supporting_lead_present
                )
                candidate_blockers.update(candidate.blockers)
            for page in page_plan.pages:
                page_candidates = sum(
                    len(cluster.candidate_ids) for cluster in page.clusters
                )
                payload_bytes = len(
                    gmail_temporal_candidate_page_payload(
                        frontier=frontier,
                        page=page,
                    ).encode("utf-8")
                )
                maxima["candidates_per_page"] = max(
                    maxima["candidates_per_page"], page_candidates
                )
                maxima["clusters_per_page"] = max(
                    maxima["clusters_per_page"], len(page.clusters)
                )
                maxima["page_payload_bytes"] = max(
                    maxima["page_payload_bytes"], payload_bytes
                )

        counts["records_with_candidates"] += int(record_candidates > 0)
        counts["records_with_pages"] += int(record_pages > 0)
        coverage["with_candidates"] += int(record_candidates > 0)
        coverage["with_pages"] += int(record_pages > 0)
        counts["records_with_singleton_fallback"] += int(
            record_singleton_fallback_batches > 0
        )
        counts["records_added_by_singleton_fallback"] += int(
            record_singleton_fallback_candidates > 0
            and record_candidates == record_singleton_fallback_candidates
        )
        if admitted:
            counts["admitted_with_candidates"] += int(record_candidates > 0)
            counts["admitted_with_pages"] += int(record_pages > 0)
            if analysis.expressions and record_candidates == 0:
                record_material = (
                    "gmail-temporal-frontier-gap-v1\0"
                    + cohort_sha256
                    + "\0"
                    + sample_id
                ).encode("utf-8")
                admitted_frontier_gaps.append(
                    {
                        "opaque_record_key": hashlib.sha256(
                            record_material
                        ).hexdigest()[:20],
                        "stratum": stratum,
                        "expressions": len(analysis.expressions),
                        "expression_forms": dict(
                            sorted(
                                Counter(
                                    item.form for item in analysis.expressions
                                ).items()
                            )
                        ),
                        "expression_blockers": dict(
                            sorted(
                                Counter(
                                    blocker
                                    for item in analysis.expressions
                                    for blocker in item.blockers
                                ).items()
                            )
                        ),
                        "mentions": len(analysis.mentions),
                        "mention_types": dict(
                            sorted(
                                Counter(
                                    item.mention_type for item in analysis.mentions
                                ).items()
                            )
                        ),
                        "mention_blockers": dict(
                            sorted(
                                Counter(
                                    blocker
                                    for item in analysis.mentions
                                    for blocker in item.blockers
                                ).items()
                            )
                        ),
                        "leads": len(analysis.leads),
                        "lead_modes": dict(
                            sorted(
                                Counter(
                                    item.association_mode for item in analysis.leads
                                ).items()
                            )
                        ),
                        "lead_confidence_tiers": dict(
                            sorted(
                                Counter(
                                    item.confidence_tier for item in analysis.leads
                                ).items()
                            )
                        ),
                        "lead_blockers": dict(
                            sorted(
                                Counter(
                                    blocker
                                    for item in analysis.leads
                                    for blocker in item.blockers
                                ).items()
                            )
                        ),
                        "batches": len(plan.batches),
                        "empty_frontiers": record_empty_frontiers,
                        "incomplete_frontiers": record_incomplete_frontiers,
                        "batch_omissions": len(plan.omissions),
                        "graph_truncated": analysis.graph_truncated,
                        "candidate_attempts": _gap_candidate_attempts(
                            analysis,
                            plan,
                        ),
                    }
                )

    return {
        "version": AUDIT_VERSION,
        "cohort_sha256": cohort_sha256,
        "source_module_sha256": {
            name: _sha256(source_path)
            for name, source_path in sorted(_SOURCE_PATHS.items())
        },
        "counts": dict(sorted(counts.items())),
        "strata": dict(sorted(strata.items())),
        "stratum_coverage": {
            name: dict(sorted(values.items()))
            for name, values in sorted(stratum_coverage.items())
        },
        "candidate_relations": dict(sorted(relations.items())),
        "candidate_lifecycles": dict(sorted(lifecycles.items())),
        "candidate_blockers": dict(sorted(candidate_blockers.items())),
        "maxima": dict(sorted(maxima.items())),
        "admitted_frontier_gaps": admitted_frontier_gaps,
        "private_file_mode": "0o600",
        "private_content_printed": False,
        "external_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cohort", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            audit_gmail_temporal_frontier(args.cohort),
            sort_keys=True,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
