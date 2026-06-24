from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .db import connection
from .paths import BrainPaths
from .wiki import GENERATED_MARKER, parse_frontmatter
from .wiki_facts import (
    CHIEF_OF_STAFF_MARKER,
    clean_statement_fragment,
    canonical_page_hint_for_fact,
    compact_statement,
    curate_managed_pages,
    entity_key_for_change,
    find_existing_fact,
    normalized_statement,
    record_curation_run,
    reconcile_open_fact_questions,
    resolve_fact_groups,
    topic_for_path,
    upsert_candidate_facts,
    wiki_fact_dashboard,
)
from .wiki_proposals import stable_unique


MIGRATION_NAME = "wiki_fact_backfill_v1"
FACT_SECTIONS = {
    "Summary",
    "Key Points",
    "Current State",
    "Goals",
    "Decisions",
    "Timeline",
    "Definition",
    "Why It Matters",
    "How It Works",
    "Related Decisions",
    "Context",
    "Decision",
    "Rationale",
    "Alternatives Considered",
    "Consequences",
    "Role",
    "Relevant Projects",
    "Interaction History",
    "Events",
    "Current Status",
    "Notes",
    "Extracted Facts",
    "Question",
    "Current Understanding",
    "Needed Evidence",
}
IGNORED_PAGE_TYPES = {"index", "reference"}
IGNORED_FILENAMES = {"index.md", "log.md"}
EMPTY_FACT_MARKERS = {
    "",
    "none",
    "none.",
    "n/a",
    "no active facts yet",
    "no active facts yet.",
    "no extracted facts yet",
    "no extracted facts yet.",
    "no source evidence recorded",
    "no source evidence recorded.",
}


def migrate_existing_wiki_to_facts(
    paths: BrainPaths,
    *,
    dry_run: bool = True,
    overwrite_existing: bool = False,
    include_references: bool = False,
) -> dict[str, Any]:
    """One-time bootstrap from existing wiki Markdown into the fact ledger.

    This is intentionally separate from the steady-state proposal/source
    curation path. It imports legacy wiki claims as low-confidence additive
    facts so newer source-derived facts can merge, supersede, or raise direct
    conflict questions without treating every old bullet as mutually exclusive.
    """
    candidates = collect_wiki_fact_migration_candidates(
        paths,
        include_references=include_references,
    )
    skipped_existing = existing_candidate_count(paths, candidates)
    new_candidates = [
        candidate
        for candidate in candidates
        if not candidate_already_exists(paths, candidate)
    ]
    page_hints = stable_unique(candidate["page_hint"] for candidate in candidates)
    page_summaries = migration_page_summaries(candidates)
    preview = {
        "migration": MIGRATION_NAME,
        "dry_run": dry_run,
        "page_count": len(page_summaries),
        "candidate_count": len(candidates),
        "new_candidate_count": len(new_candidates),
        "skipped_existing": skipped_existing,
        "pages": page_summaries,
        "candidate_preview": [
            {
                "statement": candidate["statement"],
                "page_hint": candidate["page_hint"],
                "section_hint": candidate["section_hint"],
                "source_ids": candidate["source_ids"],
                "confidence": candidate["confidence"],
            }
            for candidate in new_candidates[:80]
        ],
    }
    if dry_run:
        return preview

    upsert_result = upsert_candidate_facts(paths, new_candidates)
    resolve_result = resolve_fact_groups(paths, upsert_result["entity_keys"])
    reconcile_result = reconcile_open_fact_questions(paths, overwrite_existing=overwrite_existing)
    curation_result = curate_managed_pages(
        paths,
        page_hints=page_hints,
        overwrite_existing=overwrite_existing,
    )
    summary = {
        "migration": MIGRATION_NAME,
        "candidates": len(candidates),
        "new_candidates": len(new_candidates),
        "facts_created": len(upsert_result["created_fact_ids"]),
        "facts_updated": len(upsert_result["updated_fact_ids"]),
        "auto_merged": upsert_result["auto_merged"] + resolve_result["auto_merged"] + reconcile_result["merged_facts"],
        "auto_superseded": resolve_result["auto_superseded"],
        "questions_created": len(resolve_result["created_question_ids"]),
        "questions_dismissed": len(reconcile_result["dismissed_question_ids"]),
        "pages_written": len([page for page in curation_result["pages"] if page.get("written")]),
        "pages_previewed": len([page for page in curation_result["pages"] if not page.get("written")]),
        "lint_errors": curation_result["lint_errors"],
    }
    run_id = record_curation_run(paths, MIGRATION_NAME, "one_time_migration", "ok", summary)
    return {
        **preview,
        "dry_run": False,
        "run_id": run_id,
        "created_fact_ids": upsert_result["created_fact_ids"],
        "updated_fact_ids": upsert_result["updated_fact_ids"],
        "auto_merged": summary["auto_merged"],
        "resolved": resolve_result,
        "reconciled": {
            "dismissed_question_ids": reconcile_result["dismissed_question_ids"],
            "updated_question_ids": reconcile_result["updated_question_ids"],
            "merged_facts": reconcile_result["merged_facts"],
        },
        "curation": curation_result,
        "dashboard": wiki_fact_dashboard(paths),
    }


def collect_wiki_fact_migration_candidates(
    paths: BrainPaths,
    *,
    include_references: bool = False,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if not paths.wiki.exists():
        return candidates
    for path in sorted(paths.wiki.rglob("*.md")):
        relative_path = path.relative_to(paths.wiki).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        frontmatter, body = parse_frontmatter(text)
        if frontmatter is None:
            continue
        if should_skip_wiki_migration_page(path, relative_path, frontmatter, text, include_references=include_references):
            continue
        source_ids = stable_unique(str(source_id) for source_id in frontmatter.get("source_ids") or [])
        observed_at = str(frontmatter.get("updated_at") or frontmatter.get("created_at") or "")
        confidence = migration_confidence(frontmatter, text, source_ids)
        page_hint = canonical_page_hint_for_fact(relative_path)
        topic = topic_for_path(page_hint)
        for section_name, section_text in markdown_h2_sections(body):
            if section_name not in FACT_SECTIONS:
                continue
            for index, statement in enumerate(extract_fact_statements(section_text)):
                entity_key = entity_key_for_change(topic, page_hint, section_name)
                candidates.append(
                    {
                        "statement": statement,
                        "entity_key": entity_key,
                        "page_hint": page_hint,
                        "section_hint": section_name,
                        "source_ids": source_ids,
                        "observed_at": observed_at or None,
                        "confidence": confidence,
                        "metadata": {
                            "migration": MIGRATION_NAME,
                            "source": "existing_wiki",
                            "operation": "wiki_backfill",
                            "target_path": page_hint,
                            "original_target_path": relative_path if page_hint != relative_path else None,
                            "section_name": section_name,
                            "statement_index": index,
                            "page_title": frontmatter.get("title") or path.stem,
                            "page_type": frontmatter.get("page_type") or "",
                            "generated": GENERATED_MARKER in text,
                        },
                    }
                )
    return candidates


def should_skip_wiki_migration_page(
    path: Path,
    relative_path: str,
    frontmatter: dict[str, Any],
    text: str,
    *,
    include_references: bool,
) -> bool:
    if path.name in IGNORED_FILENAMES:
        return True
    if CHIEF_OF_STAFF_MARKER in text or bool(frontmatter.get("managed")):
        return True
    page_type = str(frontmatter.get("page_type") or "")
    if page_type in IGNORED_PAGE_TYPES and not include_references:
        return True
    return relative_path.startswith("references/") and not include_references


def markdown_h2_sections(body: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", body, flags=re.MULTILINE))
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections.append((match.group(1).strip(), body[start:end].strip()))
    return sections


def extract_fact_statements(section_text: str) -> list[str]:
    statements: list[str] = []
    current_bullet: list[str] = []
    paragraphs: list[str] = []
    for raw_line in section_text.splitlines():
        line = raw_line.rstrip()
        bullet_match = re.match(r"^\s*[-*]\s+(.*)$", line)
        if bullet_match:
            if current_bullet:
                statements.append(clean_fact_statement(" ".join(current_bullet)))
                current_bullet = []
            current_bullet.append(bullet_match.group(1))
            continue
        if current_bullet and (not line.strip() or re.match(r"^\s{2,}\S", line)):
            if line.strip():
                current_bullet.append(line.strip())
            continue
        if current_bullet:
            statements.append(clean_fact_statement(" ".join(current_bullet)))
            current_bullet = []
        paragraphs.append(line)
    if current_bullet:
        statements.append(clean_fact_statement(" ".join(current_bullet)))
    paragraph_text = "\n".join(paragraphs)
    for paragraph in re.split(r"\n\s*\n", paragraph_text):
        cleaned = clean_fact_statement(paragraph)
        if cleaned:
            statements.append(cleaned)
    return stable_unique(statement for statement in statements if is_meaningful_fact_statement(statement))


def clean_fact_statement(value: str) -> str:
    return compact_statement(clean_statement_fragment(value))


def is_meaningful_fact_statement(statement: str) -> bool:
    normalized = normalized_statement(statement)
    if normalized in EMPTY_FACT_MARKERS:
        return False
    if len(normalized) < 8:
        return False
    if normalized.startswith("source evidence"):
        return False
    return True


def migration_confidence(frontmatter: dict[str, Any], text: str, source_ids: list[str]) -> float:
    status = str(frontmatter.get("status") or "")
    if not source_ids:
        return 0.55
    if GENERATED_MARKER in text:
        return 0.68
    if status == "active":
        return 0.78
    return 0.7


def existing_candidate_count(paths: BrainPaths, candidates: list[dict[str, Any]]) -> int:
    return sum(1 for candidate in candidates if candidate_already_exists(paths, candidate))


def candidate_already_exists(paths: BrainPaths, candidate: dict[str, Any]) -> bool:
    normalized = normalized_statement(candidate["statement"])
    with connection(paths.sqlite_path) as conn:
        return bool(find_existing_fact(conn, candidate, normalized))


def migration_page_summaries(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_page: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        page_hint = candidate["page_hint"]
        summary = by_page.setdefault(
            page_hint,
            {
                "page_hint": page_hint,
                "candidate_count": 0,
                "sections": {},
                "source_count": len(candidate.get("source_ids") or []),
            },
        )
        summary["candidate_count"] += 1
        section = str(candidate.get("section_hint") or "")
        summary["sections"][section] = int(summary["sections"].get(section, 0)) + 1
    return list(by_page.values())
