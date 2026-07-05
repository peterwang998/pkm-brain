from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .db import connection, rows
from .cos_policy import latest_eval_report, report_for_suite
from .extraction import (
    EXTRACTION_PROMPT_VERSION,
    extraction_policy_for_source_type,
    load_extraction_config,
    recent_source_cards,
)
from .evals import EXTRACTION_LABELS_FILENAME, load_extraction_label_cases
from .llm import cos_provider_status
from .paths import BrainPaths
from .service import BrainService
from .util import now_iso
from .wiki import parse_frontmatter
from .wiki_facts import is_managed_page


HUMAN_OPEN_QUESTION_STATUSES = {"answered", "dismissed"}
REFERENCE_PAGE_PREFIXES = ("references/", "agent_session_log/")


def export_human_state(paths: BrainPaths, output_dir: Path | None = None) -> dict[str, Any]:
    BrainService(paths).init_workspace()
    generated_at = now_iso()
    target_dir = output_dir or default_regeneration_artifact_dir(paths, generated_at)
    target_dir.mkdir(parents=True, exist_ok=True)
    output_path = target_dir / "human_state.json"
    payload = human_state_payload(paths, generated_at=generated_at)
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "status": "ok",
        "generated_at": generated_at,
        "path": str(output_path),
        "counts": payload["counts"],
    }


def backup_runtime_brain(paths: BrainPaths, output_dir: Path | None = None) -> dict[str, Any]:
    BrainService(paths).init_workspace()
    generated_at = now_iso()
    target_dir = output_dir or default_regeneration_artifact_dir(paths, generated_at)
    target_dir.mkdir(parents=True, exist_ok=True)
    db_target = target_dir / "brain.sqlite"
    wiki_target = target_dir / "wiki"
    shutil.copy2(paths.sqlite_path, db_target)
    if wiki_target.exists():
        shutil.rmtree(wiki_target)
    shutil.copytree(paths.wiki, wiki_target)
    metadata = {
        "generated_at": generated_at,
        "source_home": str(paths.home),
        "db_source": str(paths.sqlite_path),
        "wiki_source": str(paths.wiki),
        "db_backup": str(db_target),
        "wiki_backup": str(wiki_target),
        "db_bytes": db_target.stat().st_size,
        "wiki_file_count": sum(1 for item in wiki_target.rglob("*") if item.is_file()),
    }
    metadata_path = target_dir / "backup_metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"status": "ok", "path": str(target_dir), **metadata}


def rebuild_facts_from_sources(
    paths: BrainPaths,
    *,
    from_sources: bool,
    dry_run: bool,
    source_types: list[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    BrainService(paths).init_workspace()
    if not from_sources:
        raise ValueError("rebuild-facts currently requires --from-sources")
    if not dry_run:
        raise ValueError(
            "destructive rebuild-facts apply is intentionally not implemented yet; "
            "run --dry-run and get explicit approval before adding --apply"
        )
    generated_at = now_iso()
    source_type_filter = {item for item in source_types or [] if item}
    extraction_config = load_extraction_config(paths)
    selected_cards = recent_source_cards(
        paths,
        limit=max(1, limit or 10_000_000),
        changed_only=False,
        prompt_version=EXTRACTION_PROMPT_VERSION,
        extraction_config=extraction_config,
    )
    if source_type_filter:
        selected_cards = [
            card
            for card in selected_cards
            if str(card.get("source_type") or "") in source_type_filter
        ]
    if limit is not None:
        selected_cards = selected_cards[: max(0, limit)]
    inventory = regeneration_inventory(paths, extraction_config, source_type_filter)
    human_state = human_state_counts(paths)
    providers = cos_provider_status(paths)
    eval_gate = extraction_eval_gate_status(paths)
    notes = [
        "Dry-run only: no facts, wiki pages, contracts, syntheses, or raw sources were modified.",
        "agent_session_log sources are excluded unless config/source-type filters explicitly include them.",
        "Run export-human-state and backup-runtime before any future --apply implementation.",
    ]
    if not eval_gate["ready"]:
        notes.append(
            "Autonomous fact upsert is blocked until a passing labeled extraction eval report exists."
        )
    return {
        "status": "dry_run",
        "generated_at": generated_at,
        "from_sources": True,
        "apply_supported": False,
        "source_type_filter": sorted(source_type_filter),
        "limit": limit,
        "provider_ready": provider_ready_summary(providers),
        "embedding": BrainService(paths).embedding_provider.status(check_available=False),
        "extraction_eval_gate": eval_gate,
        "scope": {
            **inventory,
            "selected_document_count": len(selected_cards),
            "selected_source_types": count_by_key(selected_cards, "source_type"),
            "selected_window_count": sum(len(card.get("windows") or []) for card in selected_cards),
            "selected_chunk_count": sum(len(card.get("chunks") or []) for card in selected_cards),
            "selected_documents": [
                {
                    "document_id": str(card.get("document_id") or ""),
                    "title": str(card.get("title") or ""),
                    "source_type": str(card.get("source_type") or ""),
                    "window_count": len(card.get("windows") or []),
                    "chunk_count": len(card.get("chunks") or []),
                }
                for card in selected_cards[:50]
            ],
        },
        "would_preserve": human_state,
        "would_purge_or_rebuild": {
            "facts": inventory["fact_count"],
            "fact_entities": table_count(paths, "fact_entities"),
            "managed_wiki_pages": inventory["managed_wiki_page_count"],
            "page_contracts": table_count(paths, "page_contracts"),
            "wiki_page_syntheses": table_count(paths, "wiki_page_syntheses"),
            "extraction_watermarks": table_count(paths, "extraction_watermarks"),
            "fact_retrieval_fts": table_count(paths, "fact_fts"),
        },
        "notes": notes,
    }


def human_state_payload(paths: BrainPaths, *, generated_at: str) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        confirmed_facts = [
            public_row(row)
            for row in conn.execute(
                """
                SELECT *
                FROM facts
                WHERE confirmed_by_user = 1
                ORDER BY created_at, id
                """
            )
        ]
        open_questions = [
            public_row(row)
            for row in conn.execute(
                """
                SELECT *
                FROM open_questions
                WHERE status IN ('answered', 'dismissed')
                ORDER BY created_at, id
                """
            )
        ]
        conflicted_facts = [
            public_row(row)
            for row in conn.execute(
                """
                SELECT *
                FROM facts
                WHERE status = 'conflicted'
                ORDER BY created_at, id
                """
            )
        ]
    hand_pages = hand_authored_pages(paths)
    return {
        "generated_at": generated_at,
        "source_home": str(paths.home),
        "counts": {
            "confirmed_facts": len(confirmed_facts),
            "answered_or_dismissed_open_questions": len(open_questions),
            "conflicted_facts": len(conflicted_facts),
            "hand_authored_pages": len(hand_pages),
        },
        "confirmed_facts": confirmed_facts,
        "answered_or_dismissed_open_questions": open_questions,
        "conflicted_facts": conflicted_facts,
        "hand_authored_pages": hand_pages,
    }


def hand_authored_pages(paths: BrainPaths) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if not paths.wiki.exists():
        return output
    for path in sorted(paths.wiki.rglob("*.md")):
        relative_path = path.relative_to(paths.wiki).as_posix()
        if relative_path.startswith(REFERENCE_PAGE_PREFIXES):
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        if is_managed_page(content):
            continue
        frontmatter, _body = parse_frontmatter(content)
        output.append(
            {
                "relative_path": relative_path,
                "frontmatter": frontmatter or {},
                "content": content,
            }
        )
    return output


def human_state_counts(paths: BrainPaths) -> dict[str, int]:
    payload = human_state_payload(paths, generated_at=now_iso())
    return dict(payload["counts"])


def regeneration_inventory(
    paths: BrainPaths,
    extraction_config: dict[str, Any],
    source_type_filter: set[str],
) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        docs = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, source_type
                FROM documents
                WHERE status = 'active'
                ORDER BY ingested_at DESC, id DESC
                """
            )
        ]
        fact_rows = rows(
            conn,
            """
            SELECT COALESCE(extraction_method, 'legacy') AS method, status, COUNT(*) AS count
            FROM facts
            GROUP BY method, status
            ORDER BY method, status
            """,
        )
        fact_count = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    eligible_docs = [
        doc
        for doc in docs
        if (not source_type_filter or str(doc["source_type"]) in source_type_filter)
        and extraction_policy_for_source_type(extraction_config, str(doc["source_type"]))["extract"]
    ]
    managed_pages = managed_wiki_page_count(paths)
    return {
        "active_document_count": len(docs),
        "active_documents_by_source_type": count_by_key(docs, "source_type"),
        "eligible_document_count": len(eligible_docs),
        "eligible_documents_by_source_type": count_by_key(eligible_docs, "source_type"),
        "fact_count": int(fact_count),
        "facts_by_method_status": [
            {
                "method": str(row["method"]),
                "status": str(row["status"]),
                "count": int(row["count"]),
            }
            for row in fact_rows
        ],
        "managed_wiki_page_count": managed_pages,
    }


def managed_wiki_page_count(paths: BrainPaths) -> int:
    if not paths.wiki.exists():
        return 0
    return sum(
        1
        for path in paths.wiki.rglob("*.md")
        if is_managed_page(path.read_text(encoding="utf-8", errors="replace"))
    )


def provider_ready_summary(status: dict[str, Any]) -> dict[str, Any]:
    roles = status.get("roles") or []
    missing = [
        str(role.get("role"))
        for role in roles
        if not role.get("configured") or role.get("missing")
    ]
    return {
        "all_ready": not missing,
        "missing_or_unready_roles": missing,
        "warnings": status.get("warnings") or [],
    }


def count_by_key(rows_: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows_:
        value = str(row.get(key) or "")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def extraction_eval_gate_status(paths: BrainPaths) -> dict[str, Any]:
    label_cases = load_extraction_label_cases(paths)
    with connection(paths.sqlite_path) as conn:
        report = latest_eval_report(conn, "extraction")
    suite_report = report_for_suite(report, "extraction") if report else None
    metrics = suite_report.get("metrics", {}) if isinstance(suite_report, dict) else {}
    ready = bool(
        suite_report
        and suite_report.get("passed")
        and not metrics.get("skipped")
        and metrics.get("label_policy") == "labeled"
        and int(metrics.get("label_case_count") or 0) > 0
    )
    reason = "passing labeled extraction eval report found"
    if not suite_report:
        reason = "no extraction eval report found"
    elif metrics.get("skipped"):
        reason = "latest extraction eval report was skipped"
    elif not suite_report.get("passed"):
        reason = "latest extraction eval report did not pass"
    elif metrics.get("label_policy") != "labeled":
        reason = "latest extraction eval report is unlabeled"
    elif int(metrics.get("label_case_count") or 0) <= 0:
        reason = "latest extraction eval report has no label cases"
    return {
        "ready": ready,
        "reason": reason,
        "latest_report_id": report.get("id") if report else None,
        "latest_report_path": report.get("report_path") if report else None,
        "suite_passed": bool(suite_report.get("passed")) if suite_report else False,
        "label_policy": metrics.get("label_policy"),
        "label_case_count": int(metrics.get("label_case_count") or 0),
        "label_file": str(paths.evals / EXTRACTION_LABELS_FILENAME),
        "label_file_case_count": len(label_cases),
    }


def table_count(paths: BrainPaths, table: str) -> int:
    with connection(paths.sqlite_path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if not exists:
            return 0
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def public_row(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def default_regeneration_artifact_dir(paths: BrainPaths, generated_at: str) -> Path:
    stamp = generated_at.replace(":", "").replace("-", "")
    return paths.home.parent / f"{paths.home.name}-runtime-backups" / f"regeneration-{stamp}"
