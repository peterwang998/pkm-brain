from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .contracts import generate_initial_contracts
from .db import connection, rows
from .cos_policy import latest_eval_report, report_for_suite
from .extraction import (
    EXTRACTION_PROMPT_VERSION,
    extract_recent_documents,
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
from .wiki_facts import (
    canonicalize_fact_routes,
    curate_all_managed_fact_pages,
    is_managed_page,
    rebuild_fact_retrieval_index,
    resolve_fact_groups,
)


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
    db_target.unlink(missing_ok=True)
    with sqlite3.connect(paths.sqlite_path) as source_conn:
        with sqlite3.connect(db_target) as target_conn:
            source_conn.backup(target_conn)
    if wiki_target.exists():
        shutil.rmtree(wiki_target)
    shutil.copytree(paths.wiki, wiki_target)
    metadata = {
        "generated_at": generated_at,
        "source_home": str(paths.home),
        "db_source": str(paths.sqlite_path),
        "wiki_source": str(paths.wiki),
        "db_backup": str(db_target),
        "db_backup_method": "sqlite_online_backup",
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
    offset: int = 0,
    reset: bool | None = None,
) -> dict[str, Any]:
    BrainService(paths).init_workspace()
    if not from_sources:
        raise ValueError("rebuild-facts currently requires --from-sources")
    generated_at = now_iso()
    source_type_filter = {item for item in source_types or [] if item}
    extraction_config = load_extraction_config(paths)
    selected_cards = recent_source_cards(
        paths,
        limit=10_000_000 if source_type_filter or offset > 0 else max(1, limit or 10_000_000),
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
    if offset > 0:
        selected_cards = selected_cards[offset:]
    if limit is not None:
        selected_cards = selected_cards[: max(0, limit)]
    inventory = regeneration_inventory(paths, extraction_config, source_type_filter)
    human_state = human_state_counts(paths)
    providers = cos_provider_status(paths)
    eval_gate = extraction_eval_gate_status(paths)
    should_reset = (offset <= 0) if reset is None else bool(reset)
    if not dry_run:
        return apply_rebuild_facts_from_sources(
            paths,
            generated_at=generated_at,
            source_type_filter=source_type_filter,
            limit=limit,
            offset=offset,
            reset=should_reset,
            selected_cards=selected_cards,
            eval_gate=eval_gate,
            providers=providers,
        )
    notes = [
        "Dry-run only: no facts, wiki pages, contracts, syntheses, or raw sources were modified.",
        "agent_session_log sources are excluded unless config/source-type filters explicitly include them.",
        "Apply mode automatically writes export-human-state and backup-runtime artifacts before reset.",
    ]
    if not eval_gate["ready"]:
        notes.append(
            "Autonomous fact upsert is blocked until a passing labeled extraction eval report exists."
        )
    return {
        "status": "dry_run",
        "generated_at": generated_at,
        "from_sources": True,
        "apply_supported": True,
        "reset_on_apply": should_reset,
        "source_type_filter": sorted(source_type_filter),
        "limit": limit,
        "offset": offset,
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
            "page_contracts": "seeded/preserved before extraction",
            "wiki_page_syntheses": table_count(paths, "wiki_page_syntheses"),
            "cos_stage_watermarks": table_count(paths, "cos_stage_watermarks"),
            "fact_retrieval_fts": table_count(paths, "fact_fts"),
        },
        "notes": notes,
    }


def apply_rebuild_facts_from_sources(
    paths: BrainPaths,
    *,
    generated_at: str,
    source_type_filter: set[str],
    limit: int | None,
    offset: int,
    reset: bool,
    selected_cards: list[dict[str, Any]],
    eval_gate: dict[str, Any],
    providers: dict[str, Any],
) -> dict[str, Any]:
    if not eval_gate.get("ready"):
        raise ValueError(f"rebuild-facts apply blocked: {eval_gate['reason']}")
    provider_ready = provider_ready_summary(providers)
    if not provider_ready["all_ready"]:
        missing = ", ".join(provider_ready["missing_or_unready_roles"])
        raise ValueError(f"rebuild-facts apply blocked: provider roles not ready: {missing}")
    if provider_ready["warnings"]:
        raise ValueError(
            "rebuild-facts apply blocked: provider separation warnings: "
            + "; ".join(str(warning) for warning in provider_ready["warnings"])
        )
    run_id = f"regen_{generated_at.replace(':', '').replace('-', '').replace('+', '')}"
    artifact_dir = default_regeneration_artifact_dir(paths, generated_at)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_runtime_brain(paths, output_dir=artifact_dir)
    human_export = export_human_state(paths, output_dir=artifact_dir)
    human_payload = json.loads(Path(human_export["path"]).read_text(encoding="utf-8"))

    seeded_contracts = seed_route_contracts(paths) if reset else skipped_step("not_reset")
    reset_result = reset_rebuild_derived_state(paths, run_id=run_id) if reset else skipped_step("not_reset")

    extraction = extract_recent_documents(
        paths,
        limit=limit or 10_000_000,
        offset=offset,
        shadow=False,
        changed_only=False,
        run_id=run_id,
        critic_disagreement_mode="reject",
        source_types=sorted(source_type_filter),
    )
    entity_keys = active_fact_entity_keys(paths)
    resolver = resolve_fact_groups(paths, entity_keys) if entity_keys else skipped_step("no_active_fact_entity_keys")
    confirmations = reapply_confirmed_facts(paths, human_payload, run_id=run_id)
    first_curation = curate_all_managed_fact_pages(paths, overwrite_existing=True)
    route = canonicalize_fact_routes(paths)
    route_resolver = (
        resolve_fact_groups(paths, route["entity_keys"])
        if route.get("entity_keys")
        else skipped_step("no_rerouted_entity_keys")
    )
    final_curation = (
        curate_all_managed_fact_pages(paths, overwrite_existing=True)
        if route.get("updated_fact_ids")
        else first_curation
    )
    with connection(paths.sqlite_path) as conn:
        rebuild_fact_retrieval_index(conn)
    summary = {
        "status": "applied",
        "generated_at": generated_at,
        "run_id": run_id,
        "from_sources": True,
        "reset": reset,
        "source_type_filter": sorted(source_type_filter),
        "limit": limit,
        "offset": offset,
        "selected_document_count": len(selected_cards),
        "selected_documents": [
            {
                "document_id": str(card.get("document_id") or ""),
                "title": str(card.get("title") or ""),
                "source_type": str(card.get("source_type") or ""),
                "window_count": len(card.get("windows") or []),
            }
            for card in selected_cards[:50]
        ],
        "artifacts": {
            "directory": str(artifact_dir),
            "backup": backup,
            "human_state_export": human_export,
        },
        "seeded_contracts": seeded_contracts,
        "reset_result": reset_result,
        "extraction": extraction_summary(extraction),
        "resolver": resolver,
        "reapplied_confirmations": confirmations,
        "route_canonicalization": route,
        "route_resolver": route_resolver,
        "curation": curation_summary(final_curation),
        "post_counts": regeneration_post_counts(paths),
        "notes": [
            "raw sources were not modified",
            "legacy facts were archived, not deleted" if reset else "continuation tranche did not reset derived state",
            "page contracts were seeded/preserved before extraction to avoid a cold empty route pool",
        ],
    }
    write_regeneration_summary(artifact_dir, summary)
    return summary


def seed_route_contracts(paths: BrainPaths) -> dict[str, Any]:
    result = generate_initial_contracts(paths, apply=True)
    return {
        "status": "ok",
        "existing_contract_count": result["existing_contract_count"],
        "proposed_count": len(result["contracts"]),
        "applied_count": len(result["actions"]),
        "page_hints": [contract["page_hint"] for contract in result["contracts"][:50]],
    }


def reset_rebuild_derived_state(paths: BrainPaths, *, run_id: str) -> dict[str, Any]:
    timestamp = now_iso()
    managed_files = remove_managed_wiki_files(paths)
    with connection(paths.sqlite_path) as conn:
        fact_count = int(conn.execute("SELECT COUNT(*) FROM facts WHERE status != 'archived'").fetchone()[0])
        entity_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM entities WHERE COALESCE(status, 'active') = 'active'"
            ).fetchone()[0]
        )
        active_question_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM open_questions WHERE status IN ('open', 'needs_human')"
            ).fetchone()[0]
        )
        conn.execute(
            """
            UPDATE facts
            SET status = 'archived',
                metadata = json_set(
                  COALESCE(NULLIF(metadata, ''), '{}'),
                  '$.regeneration_archive',
                  json(?)
                )
            WHERE status != 'archived'
            """,
            (
                json.dumps(
                    {
                        "run_id": run_id,
                        "archived_at": timestamp,
                        "reason": "from_source_regeneration_reset",
                    }
                ),
            ),
        )
        conn.execute("DELETE FROM fact_entities")
        conn.execute(
            """
            UPDATE entities
            SET status = 'archived'
            WHERE COALESCE(status, 'active') = 'active'
            """
        )
        conn.execute(
            """
            UPDATE open_questions
            SET status = 'dismissed',
                answer = ?,
                answered_at = ?
            WHERE status IN ('open', 'needs_human')
            """,
            (
                json.dumps(
                    {
                        "reason": "dismissed before from-source regeneration",
                        "run_id": run_id,
                    }
                ),
                timestamp,
            ),
        )
        conn.execute("DELETE FROM wiki_page_syntheses")
        conn.execute("DELETE FROM wiki_pages WHERE COALESCE(managed, 0) = 1")
        conn.execute("DELETE FROM cos_stage_watermarks WHERE stage = 'extractor'")
        conn.execute("DELETE FROM retrieval_fts WHERE kind = 'fact'")
        rebuild_fact_retrieval_index(conn)
    return {
        "status": "ok",
        "archived_fact_count": fact_count,
        "archived_entity_count": entity_count,
        "dismissed_open_question_count": active_question_count,
        "removed_managed_wiki_file_count": len(managed_files),
        "removed_managed_wiki_files": managed_files[:50],
        "cleared_fact_entity_links": True,
        "cleared_wiki_page_syntheses": True,
        "cleared_extractor_watermarks": True,
    }


def remove_managed_wiki_files(paths: BrainPaths) -> list[str]:
    removed: list[str] = []
    if not paths.wiki.exists():
        return removed
    for path in sorted(paths.wiki.rglob("*.md")):
        relative = path.relative_to(paths.wiki).as_posix()
        content = path.read_text(encoding="utf-8", errors="replace")
        if not is_managed_page(content):
            continue
        path.unlink()
        removed.append(relative)
    prune_empty_dirs(paths.wiki)
    return removed


def prune_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted((item for item in root.rglob("*") if item.is_dir()), reverse=True):
        try:
            path.rmdir()
        except OSError:
            continue


def active_fact_entity_keys(paths: BrainPaths) -> list[str]:
    with connection(paths.sqlite_path) as conn:
        return [
            str(row["entity_key"])
            for row in conn.execute(
                """
                SELECT DISTINCT entity_key
                FROM facts
                WHERE status IN ('active', 'conflicted', 'needs_confirmation')
                  AND COALESCE(entity_key, '') != ''
                ORDER BY entity_key
                """
            )
        ]


def reapply_confirmed_facts(
    paths: BrainPaths, human_payload: dict[str, Any], *, run_id: str
) -> dict[str, Any]:
    confirmed = [
        fact
        for fact in human_payload.get("confirmed_facts", [])
        if isinstance(fact, dict)
    ]
    if not confirmed:
        return {"matched_count": 0, "unmatched_count": 0, "matched_fact_ids": [], "unmatched": []}
    with connection(paths.sqlite_path) as conn:
        active_facts = rows(
            conn,
            """
            SELECT id, statement, page_hint, source_ids, metadata
            FROM facts
            WHERE status = 'active'
            ORDER BY created_at DESC, id
            """,
        )
        by_statement: dict[str, list[Any]] = {}
        for fact in active_facts:
            by_statement.setdefault(statement_key(str(fact["statement"] or "")), []).append(fact)
        matched_ids: list[str] = []
        unmatched: list[dict[str, Any]] = []
        for old_fact in confirmed:
            key = statement_key(str(old_fact.get("statement") or ""))
            candidates = by_statement.get(key) or []
            match = best_confirmation_match(old_fact, candidates)
            if match is None:
                unmatched.append(
                    {
                        "old_fact_id": old_fact.get("id"),
                        "statement": old_fact.get("statement"),
                    }
                )
                continue
            metadata = json.loads(match["metadata"] or "{}")
            metadata["confirmed_by_user_reapplied"] = {
                "run_id": run_id,
                "old_fact_id": old_fact.get("id"),
                "reapplied_at": now_iso(),
            }
            conn.execute(
                """
                UPDATE facts
                SET confirmed_by_user = 1,
                    metadata = ?
                WHERE id = ?
                """,
                (json.dumps(metadata, sort_keys=True), match["id"]),
            )
            matched_ids.append(str(match["id"]))
    return {
        "matched_count": len(matched_ids),
        "unmatched_count": len(unmatched),
        "matched_fact_ids": matched_ids,
        "unmatched": unmatched[:50],
        "preserved_open_question_dispositions": len(
            human_payload.get("answered_or_dismissed_open_questions", [])
        ),
        "note": "answered/dismissed question dispositions are exported; exact fact confirmations are reapplied automatically",
    }


def best_confirmation_match(old_fact: dict[str, Any], candidates: list[Any]) -> Any | None:
    if not candidates:
        return None
    old_page_hint = str(old_fact.get("page_hint") or "")
    old_sources = set(str(item) for item in json_list(old_fact.get("source_ids")))
    same_page = [fact for fact in candidates if str(fact["page_hint"] or "") == old_page_hint]
    if same_page:
        return same_page[0]
    if old_sources:
        for fact in candidates:
            new_sources = set(str(item) for item in json_list(fact["source_ids"]))
            if old_sources & new_sources:
                return fact
    return candidates[0]


def statement_key(value: str) -> str:
    return " ".join(str(value or "").lower().split())


def json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def extraction_summary(result: dict[str, Any]) -> dict[str, Any]:
    actions = result.get("actions") or []
    candidates = result.get("candidates") or []
    return {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "document_count": len(result.get("documents") or []),
        "candidate_count": len(candidates),
        "action_count": len(actions),
        "action_status_counts": count_by_key(actions, "status"),
        "critic_decision_counts": count_by_key(actions, "critic_decision"),
        "timing": result.get("timing") or {},
        "validation": result.get("validation") or {},
    }


def curation_summary(result: dict[str, Any]) -> dict[str, Any]:
    pages = (result.get("curation") or {}).get("pages") or []
    return {
        "page_count": result.get("page_count"),
        "written_count": len([page for page in pages if page.get("written")]),
        "projection_error_count": sum(len(page.get("projection_errors") or []) for page in pages),
        "archived_orphan_count": len(result.get("archived_orphans") or []),
        "lint_error_count": len((result.get("lint") or {}).get("errors") or []),
    }


def regeneration_post_counts(paths: BrainPaths) -> dict[str, int]:
    return {
        "active_facts": table_count_where(paths, "facts", "status = 'active'"),
        "archived_facts": table_count_where(paths, "facts", "status = 'archived'"),
        "rejected_actions": table_count_where(paths, "cos_actions", "status = 'rejected'"),
        "needs_human_actions": table_count_where(paths, "cos_actions", "status = 'needs_human'"),
        "open_questions": table_count_where(paths, "open_questions", "status IN ('open', 'needs_human')"),
        "active_entities": table_count_where(paths, "entities", "COALESCE(status, 'active') = 'active'"),
        "active_contracts": table_count_where(paths, "page_contracts", "status = 'active'"),
    }


def table_count_where(paths: BrainPaths, table: str, where_sql: str) -> int:
    with connection(paths.sqlite_path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if not exists:
            return 0
        return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where_sql}").fetchone()[0])


def write_regeneration_summary(artifact_dir: Path, summary: dict[str, Any]) -> None:
    (artifact_dir / "rebuild_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def skipped_step(reason: str) -> dict[str, Any]:
    return {"status": "skipped", "reason": reason}


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
