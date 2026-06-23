from __future__ import annotations

from difflib import SequenceMatcher
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

from .db import connection, dumps, loads, rows
from .paths import BrainPaths
from .util import new_id, now_iso, slugify, text_sha256
from .wiki import COMMON_SECTIONS, TYPE_SECTIONS, lint_wiki, parse_frontmatter
from .wiki_proposals import (
    PENDING_REVIEW_STATUSES,
    build_wiki_review_packet_context,
    mark_wiki_change_items_absorbed_by_facts,
    pending_review_changes,
    stable_unique,
)


FACT_STATUSES = {
    "active",
    "superseded",
    "conflicted",
    "needs_confirmation",
    "retracted",
}
QUESTION_STATUSES = {"open", "answered", "dismissed"}
REPLACEMENT_OPERATIONS = {"replace_page", "replace_section", "create_page"}
AUTO_SUPERSEDE_CONFIDENCE = 0.85
NEAR_DUPLICATE_SEQUENCE_RATIO = 0.88
NEAR_DUPLICATE_TOKEN_OVERLAP = 0.82
NEAR_DUPLICATE_TOKEN_JACCARD = 0.58
RELATED_SOURCE_TOKEN_OVERLAP = 0.48
RELATED_SOURCE_TOKEN_JACCARD = 0.30
ANCHOR_TOKEN_COVERAGE = 0.58
CHIEF_OF_STAFF_MARKER = "<!-- generated-by: pkm-brain chief-of-staff-facts v1 -->"
MANAGED_TAG = "managed"
NO_SECTION_FACTS = "No maintained facts for this section."
NO_RELATED_PAGES = "No related pages recorded."
NO_OPEN_QUESTIONS = "No open factual conflicts."
NO_SOURCE_EVIDENCE = "No source evidence recorded."
STATEMENT_COMPACT_LIMIT = 420
FACT_STOPWORDS = {
    "a",
    "about",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "has",
    "have",
    "in",
    "into",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "use",
    "using",
    "with",
}
FACT_LOW_SIGNAL_TOKENS = {
    "ability",
    "administrative",
    "based",
    "broad",
    "broadly",
    "capability",
    "capabilities",
    "center",
    "centered",
    "concrete",
    "context",
    "core",
    "directly",
    "focus",
    "focused",
    "framing",
    "immediate",
    "important",
    "inside",
    "merely",
    "model",
    "organization",
    "organizational",
    "part",
    "primary",
    "problem",
    "prompt",
    "related",
    "simply",
    "solution",
    "strong",
    "stronger",
    "useful",
    "underlying",
}
FACT_TOKEN_ALIASES = {
    "admin": "administrator",
    "admins": "administrator",
    "administrator": "administrator",
    "administrators": "administrator",
    "analyses": "analysis",
    "childrens": "children",
    "children": "child",
    "collaboration": "collaborate",
    "collaborating": "collaborate",
    "collaborative": "collaborate",
    "collaborator": "collaborate",
    "consume": "consume",
    "consumption": "consume",
    "cross": "cross-functional",
    "dashboard": "dashboard",
    "dashboards": "dashboard",
    "distribute": "share",
    "distributed": "share",
    "distributing": "share",
    "distribution": "share",
    "finance": "financial",
    "finops": "finops",
    "group": "group",
    "groups": "group",
    "identity": "identity",
    "leader": "leader",
    "leaders": "leader",
    "leads": "leader",
    "organization": "org",
    "organizations": "org",
    "permission": "access",
    "permissions": "access",
    "publish": "share",
    "published": "share",
    "publishing": "share",
    "kids": "child",
    "kid": "child",
    "recreating": "recreate",
    "recreated": "recreate",
    "recreate": "recreate",
    "re": "recreate",
    "reporting": "report",
    "reports": "report",
    "shared": "share",
    "sharing": "share",
    "teammate": "team",
    "teammates": "team",
    "teams": "team",
    "visibility": "access",
    "visible": "access",
    "view": "access",
    "viewer": "access",
}
NEGATION_TOKENS = {
    "no",
    "not",
    "never",
    "without",
    "cannot",
    "cant",
    "won't",
    "wont",
    "isnt",
    "isn't",
}
MATERIAL_CONTRAST_GROUPS = [
    {"old", "older", "oldest", "new", "newer", "newest", "latest"},
    {"before", "after"},
    {"increase", "increases", "increased", "decrease", "decreases", "decreased"},
    {"higher", "high", "lower", "low"},
    {"accept", "accepted", "approve", "approved", "reject", "rejected"},
    {"active", "inactive", "archived", "stale", "superseded"},
    {"yes", "true", "no", "false"},
]


def absorb_wiki_packet_into_facts(
    paths: BrainPaths,
    packet_id: str,
    group_by: str = "topic",
    *,
    overwrite_existing: bool = False,
) -> dict[str, Any]:
    """Convert one proposal packet into facts, questions, and managed drafts."""
    try:
        context = build_wiki_review_packet_context(
            paths,
            packet_id,
            group_by=group_by,
            max_pages=None,
        )
    except ValueError as exc:
        if "wiki review packet not found" not in str(exc):
            raise
        return no_pending_packet_absorption(paths, packet_id, group_by)
    page_contexts = {
        str(page.get("target_path") or ""): page for page in context.get("pages", [])
    }
    target_paths = set(page_contexts)
    changes = [
        change
        for change in pending_review_changes(paths, PENDING_REVIEW_STATUSES)
        if str(change.get("target_path") or "") in target_paths
    ]
    candidates = [
        candidate_fact_from_change(
            context, page_contexts[str(change["target_path"])], change
        )
        for change in changes
    ]
    candidates = [candidate for candidate in candidates if candidate["statement"]]
    upsert_result = upsert_candidate_facts(paths, candidates)
    resolve_result = resolve_fact_groups(paths, upsert_result["entity_keys"])
    page_hints = stable_unique(
        candidate["page_hint"]
        for candidate in candidates
        if str(candidate.get("page_hint") or "").strip()
    )
    curation_result = curate_managed_pages(
        paths,
        page_hints=page_hints,
        overwrite_existing=overwrite_existing,
    )
    projection_errors = [
        error
        for page in curation_result.get("pages", [])
        for error in page.get("projection_errors") or []
    ]
    status = "failed" if curation_result["lint_errors"] or projection_errors else "ok"
    absorbed_item_ids = stable_unique(
        str(candidate.get("metadata", {}).get("item_id") or "")
        for candidate in candidates
        if str(candidate.get("metadata", {}).get("item_id") or "").strip()
    )
    summary = {
        "packet": context["packet"],
        "candidates": len(candidates),
        "facts_created": len(upsert_result["created_fact_ids"]),
        "facts_updated": len(upsert_result["updated_fact_ids"]),
        "auto_merged": upsert_result["auto_merged"] + resolve_result["auto_merged"],
        "auto_superseded": resolve_result["auto_superseded"],
        "questions_created": len(resolve_result["created_question_ids"]),
        "pages_written": len(
            [page for page in curation_result["pages"] if page.get("written")]
        ),
        "pages_previewed": len(
            [page for page in curation_result["pages"] if not page.get("written")]
        ),
        "lint_errors": curation_result["lint_errors"],
        "projection_errors": projection_errors,
        "proposal_absorption": {"skipped": "curation_failed"} if status != "ok" else {},
    }
    run_id = record_curation_run(paths, packet_id, group_by, status, summary)
    proposal_absorption: dict[str, Any]
    if status == "ok":
        proposal_absorption = mark_wiki_change_items_absorbed_by_facts(
            paths,
            absorbed_item_ids,
            curation_run_id=run_id,
            packet_id=packet_id,
            group_by=group_by,
        )
        summary["proposal_absorption"] = proposal_absorption
        update_curation_run_summary(paths, run_id, summary)
    else:
        proposal_absorption = summary["proposal_absorption"]
    return {
        "run_id": run_id,
        "packet": context["packet"],
        "candidate_count": len(candidates),
        "created_fact_ids": upsert_result["created_fact_ids"],
        "updated_fact_ids": upsert_result["updated_fact_ids"],
        "auto_merged": upsert_result["auto_merged"] + resolve_result["auto_merged"],
        "resolved": resolve_result,
        "curation": curation_result,
        "proposal_absorption": proposal_absorption,
        "dashboard": wiki_fact_dashboard(paths),
    }


def no_pending_packet_absorption(
    paths: BrainPaths, packet_id: str, group_by: str
) -> dict[str, Any]:
    packet = {"id": packet_id, "label": packet_id, "group_by": group_by}
    proposal_absorption = {
        "requested_item_count": 0,
        "updated_item_ids": [],
        "already_absorbed_item_ids": [],
        "missing_item_ids": [],
        "fully_absorbed_batch_ids": [],
        "partially_absorbed_batch_ids": [],
        "skipped": "no_pending_items",
    }
    summary = {
        "packet": packet,
        "candidates": 0,
        "facts_created": 0,
        "facts_updated": 0,
        "auto_merged": 0,
        "auto_superseded": 0,
        "questions_created": 0,
        "pages_written": 0,
        "pages_previewed": 0,
        "lint_errors": [],
        "projection_errors": [],
        "proposal_absorption": proposal_absorption,
    }
    run_id = record_curation_run(paths, packet_id, group_by, "ok", summary)
    return {
        "run_id": run_id,
        "packet": packet,
        "candidate_count": 0,
        "created_fact_ids": [],
        "updated_fact_ids": [],
        "auto_merged": 0,
        "resolved": {
            "entity_keys": [],
            "created_question_ids": [],
            "auto_merged": 0,
            "auto_superseded": 0,
        },
        "curation": {"pages": [], "lint": None, "lint_errors": []},
        "proposal_absorption": proposal_absorption,
        "dashboard": wiki_fact_dashboard(paths),
    }


def candidate_fact_from_change(
    context: dict[str, Any],
    page: dict[str, Any],
    change: dict[str, Any],
) -> dict[str, Any]:
    target_path = str(change.get("target_path") or page.get("target_path") or "")
    operation = str(change.get("operation") or "")
    section_hint = str(
        change.get("section_name") or default_section_for_operation(operation) or ""
    )
    statement = statement_from_change(change)
    page_hint = canonical_page_hint_for_fact(target_path)
    topic = topic_for_path(page_hint)
    entity_key = entity_key_for_change(topic, page_hint, section_hint)
    source_ids = stable_unique(
        str(source_id) for source_id in change.get("source_ids") or []
    )
    return {
        "statement": statement,
        "entity_key": entity_key,
        "page_hint": page_hint,
        "section_hint": section_hint,
        "source_ids": source_ids,
        "observed_at": change.get("batch_created_at"),
        "confidence": float(
            change.get("item_confidence") or change.get("batch_confidence") or 0.0
        ),
        "metadata": {
            "packet_id": context["packet"].get("id"),
            "packet_label": context["packet"].get("label"),
            "batch_id": change.get("batch_id"),
            "item_id": change.get("item_id"),
            "batch_title": change.get("batch_title"),
            "batch_status": change.get("batch_status"),
            "operation": operation,
            "target_path": page_hint,
            "original_target_path": target_path if page_hint != target_path else None,
            "section_name": section_hint or None,
            "rationale": change.get("item_rationale")
            or change.get("batch_rationale")
            or "",
            "target_exists": page.get("target_exists"),
            "page_complexity": page.get("complexity"),
            "page_conflicts": page.get("conflicts") or [],
        },
    }


def statement_from_change(change: dict[str, Any]) -> str:
    markdown = str(change.get("proposed_markdown") or "")
    operation = str(change.get("operation") or "")
    section_name = str(change.get("section_name") or "")
    if operation in {"create_page", "replace_page"}:
        frontmatter, body = parse_frontmatter(markdown)
        summary = section_body(body, "Summary") or section_body(body, "Definition")
        if summary:
            return compact_statement(summary)
        title = str((frontmatter or {}).get("title") or "").strip()
        body_without_headings = re.sub(r"^#+\s+.*$", "", body, flags=re.MULTILINE)
        text = compact_statement(body_without_headings)
        if title and text:
            return compact_statement(f"{title}: {text}")
        if title:
            return title
        return text
    if section_name:
        return compact_statement(markdown)
    return compact_statement(markdown)


def compact_statement(value: Any, limit: int = STATEMENT_COMPACT_LIMIT) -> str:
    text = clean_statement_fragment(value)
    text = re.sub(r"^[-*]\s+", "", text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def clean_statement_fragment(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", r"\2", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = "\n".join(clean_markdown_heading_line(line) for line in text.splitlines())
    text = re.sub(r"\s+#{1,6}\s+", ". ", text)
    text = re.sub(r"(?m)^\s*[-*]\s+", "", text)
    text = re.sub(r"(?m)^\s*\d+[.)]\s+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text


def clean_markdown_heading_line(line: str) -> str:
    if not re.match(r"^\s*#{1,6}\s+", line):
        return line
    stripped = line.strip()
    without_marker = re.sub(r"^#{1,6}\s+", "", stripped)
    if len(without_marker) <= 100 and not re.search(r"[.!?]", without_marker):
        return ""
    return without_marker


def section_body(markdown: str, section_name: str) -> str:
    match = re.search(
        rf"^##\s+{re.escape(section_name)}\s*\n(.*?)(?=^##\s+|\Z)",
        markdown,
        flags=re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def default_section_for_operation(operation: str) -> str:
    if operation == "create_page":
        return "Summary"
    if operation == "replace_page":
        return "Summary"
    return ""


def topic_for_path(target_path: str) -> str:
    path = Path(target_path)
    return path.parts[0] if len(path.parts) > 1 else "root"


CAREER_NON_ENTITY_PREFIXES = {
    "agent",
    "ai",
    "career",
    "decision",
    "final",
    "interview",
    "loop",
    "native",
    "opportunity",
    "pm",
    "product",
    "role",
    "senior",
}


def canonical_page_hint_for_fact(target_path: str) -> str:
    raw = str(target_path or "").strip()
    if not raw:
        return raw
    path = Path(raw)
    parts = path.parts
    if len(parts) >= 2 and parts[0] == "career":
        stem_part = (
            parts[2]
            if len(parts) >= 3 and parts[1] in {"opportunities", "interviews"}
            else parts[1]
        )
        token = Path(stem_part).stem.split("-", 1)[0].lower()
        if (
            token
            and not token[0].isdigit()
            and token not in CAREER_NON_ENTITY_PREFIXES
            and len(token) >= 3
        ):
            return f"career/{token}.md"
    return raw


def entity_key_for_change(topic: str, target_path: str, section_hint: str) -> str:
    stem = Path(target_path).with_suffix("").as_posix()
    return f"{slugify(topic)}:{slugify(stem)}:{slugify(section_hint or 'page')}"


def upsert_candidate_facts(
    paths: BrainPaths, candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    created_fact_ids: list[str] = []
    updated_fact_ids: list[str] = []
    auto_merged = 0
    timestamp = now_iso()
    entity_keys = stable_unique(candidate["entity_key"] for candidate in candidates)
    with connection(paths.sqlite_path) as conn:
        for candidate in candidates:
            normalized = normalized_statement(candidate["statement"])
            existing = find_existing_fact(conn, candidate, normalized)
            if existing:
                merged = merge_fact_values(row_to_fact(existing), candidate, timestamp)
                conn.execute(
                    """
                    UPDATE facts
                    SET statement = ?, source_ids = ?, observed_at = ?, confidence = ?,
                        metadata = ?,
                        last_seen_at = ?,
                        status = CASE WHEN status = 'superseded' THEN 'superseded' ELSE 'active' END,
                        conflict_group_id = NULL
                    WHERE id = ?
                    """,
                    (
                        merged["statement"],
                        dumps(merged["source_ids"]),
                        merged["observed_at"],
                        merged["confidence"],
                        dumps(merged["metadata"]),
                        timestamp,
                        existing["id"],
                    ),
                )
                if normalized_statement(str(existing["statement"] or "")) != normalized:
                    auto_merged += 1
                updated_fact_ids.append(str(existing["id"]))
                continue
            fact_id = new_id("fact")
            conn.execute(
                """
                INSERT INTO facts(
                  id, statement, entity_key, page_hint, section_hint, source_ids,
                  observed_at, confidence, status, metadata, created_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fact_id,
                    candidate["statement"],
                    candidate["entity_key"],
                    candidate["page_hint"],
                    candidate["section_hint"] or None,
                    dumps(candidate["source_ids"]),
                    candidate["observed_at"],
                    candidate["confidence"],
                    "active",
                    dumps(candidate["metadata"]),
                    timestamp,
                    timestamp,
                ),
            )
            created_fact_ids.append(fact_id)
    return {
        "created_fact_ids": created_fact_ids,
        "updated_fact_ids": updated_fact_ids,
        "entity_keys": entity_keys,
        "auto_merged": auto_merged,
    }


def normalized_statement(statement: str) -> str:
    return re.sub(r"\W+", " ", statement).strip().lower()


def find_existing_fact(
    conn: Any, candidate: dict[str, Any], normalized: str
) -> Any | None:
    for row in conn.execute(
        """
        SELECT *
        FROM facts
        WHERE entity_key = ? AND status != 'retracted'
        ORDER BY
          CASE status
            WHEN 'active' THEN 0
            WHEN 'conflicted' THEN 1
            WHEN 'needs_confirmation' THEN 2
            WHEN 'superseded' THEN 3
            ELSE 4
          END,
          confirmed_by_user DESC,
          created_at DESC
        """,
        (candidate["entity_key"],),
    ):
        if normalized_statement(str(row["statement"] or "")) == normalized:
            return row
        if facts_should_merge(row_to_fact(row), candidate):
            return row
    return None


def merge_fact_values(
    existing: dict[str, Any], candidate: dict[str, Any], timestamp: str
) -> dict[str, Any]:
    source_ids = stable_unique(
        [*existing.get("source_ids", []), *candidate.get("source_ids", [])]
    )
    confidence = max(
        float(existing.get("confidence") or 0.0),
        float(candidate.get("confidence") or 0.0),
    )
    observed_at = (
        max(
            str(existing.get("observed_at") or ""),
            str(candidate.get("observed_at") or ""),
        )
        or None
    )
    statement = choose_canonical_statement(existing, candidate)
    metadata = dict(existing.get("metadata") or {})
    merged_candidates = list(metadata.get("merged_candidates") or [])
    merged_candidates.append(
        {
            "statement": candidate["statement"],
            "source_ids": candidate.get("source_ids") or [],
            "confidence": candidate.get("confidence"),
            "observed_at": candidate.get("observed_at"),
            "metadata": candidate.get("metadata") or {},
            "merged_at": timestamp,
        }
    )
    metadata.update(candidate.get("metadata") or {})
    metadata["merged_candidates"] = merged_candidates
    metadata["merge_reason"] = (
        "near-duplicate fact automatically merged before conflict review"
    )
    return {
        "statement": statement,
        "source_ids": source_ids,
        "observed_at": observed_at,
        "confidence": confidence,
        "metadata": metadata,
    }


def choose_canonical_statement(
    existing: dict[str, Any], candidate: dict[str, Any]
) -> str:
    existing_statement = str(existing.get("statement") or "")
    candidate_statement = str(candidate.get("statement") or "")
    existing_confidence = float(existing.get("confidence") or 0.0)
    candidate_confidence = float(candidate.get("confidence") or 0.0)
    if candidate_confidence >= existing_confidence + 0.05:
        return candidate_statement
    if existing_confidence >= candidate_confidence + 0.05:
        return existing_statement
    candidate_quality = statement_quality_score(candidate_statement)
    existing_quality = statement_quality_score(existing_statement)
    if candidate_quality > existing_quality:
        return candidate_statement
    if existing_quality > candidate_quality:
        return existing_statement
    if statement_information_score(candidate_statement) > statement_information_score(
        existing_statement
    ):
        return candidate_statement
    if statement_information_score(existing_statement) > statement_information_score(
        candidate_statement
    ):
        return existing_statement
    if len(candidate_statement) < len(existing_statement):
        return candidate_statement
    return existing_statement


def statement_information_score(statement: str) -> tuple[int, int]:
    tokens = fact_tokens(statement)
    return (len(tokens), len(statement))


def statement_quality_score(statement: str) -> tuple[int, int, int, int]:
    stripped = str(statement or "").strip()
    noise_penalty = 0
    if re.search(r"(^|\s)#{1,6}\s+", stripped):
        noise_penalty += 4
    if re.search(r"\[\[|\]\]|\]\(|```", stripped):
        noise_penalty += 3
    if len(stripped) > STATEMENT_COMPACT_LIMIT:
        noise_penalty += 2
    if len(stripped) > 320:
        noise_penalty += 1
    complete_sentence = int(stripped.endswith((".", "?", "!")))
    useful_tokens = min(len(fact_tokens(stripped)), 60)
    concise_bonus = -abs(min(len(stripped), 420) - 220)
    return (-noise_penalty, complete_sentence, useful_tokens, concise_bonus)


def resolve_fact_groups(paths: BrainPaths, entity_keys: list[str]) -> dict[str, Any]:
    if not entity_keys:
        return {
            "auto_merged": 0,
            "auto_superseded": 0,
            "created_question_ids": [],
            "conflict_group_ids": [],
        }
    placeholders = ",".join("?" for _ in entity_keys)
    with connection(paths.sqlite_path) as conn:
        fact_rows = rows(
            conn,
            f"""
            SELECT *
            FROM facts
            WHERE entity_key IN ({placeholders})
              AND status IN ('active', 'conflicted', 'needs_confirmation')
            ORDER BY entity_key, section_hint, observed_at, created_at
            """,
            entity_keys,
        )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in fact_rows:
        fact = row_to_fact(row)
        grouped[(fact["entity_key"], str(fact.get("section_hint") or ""))].append(fact)

    auto_superseded = 0
    auto_merged = 0
    created_question_ids: list[str] = []
    conflict_group_ids: list[str] = []
    with connection(paths.sqlite_path) as conn:
        for (entity_key, _section), facts in grouped.items():
            replacement_facts = [
                fact
                for fact in facts
                if str(fact.get("metadata", {}).get("operation") or "")
                in REPLACEMENT_OPERATIONS
            ]
            additive_facts = [fact for fact in facts if fact not in replacement_facts]
            conflicting_additive_facts = [
                fact
                for fact in additive_facts
                if any(
                    facts_directly_conflict(fact, replacement)
                    for replacement in replacement_facts
                )
            ]
            for fact in additive_facts:
                if fact in conflicting_additive_facts:
                    continue
                conn.execute(
                    "UPDATE facts SET status = 'active', conflict_group_id = NULL WHERE id = ?",
                    (fact["id"],),
                )
            if conflicting_additive_facts:
                replacement_facts = [*replacement_facts, *conflicting_additive_facts]
            if len(replacement_facts) <= 1:
                for fact in replacement_facts:
                    conn.execute(
                        "UPDATE facts SET status = 'active', conflict_group_id = NULL WHERE id = ?",
                        (fact["id"],),
                    )
                continue

            replacement_facts, merged_count = merge_similar_replacement_facts(
                conn, replacement_facts
            )
            auto_merged += merged_count
            if len(replacement_facts) <= 1:
                for fact in replacement_facts:
                    conn.execute(
                        "UPDATE facts SET status = 'active', conflict_group_id = NULL WHERE id = ?",
                        (fact["id"],),
                    )
                    dismiss_resolved_conflict_questions(
                        conn, entity_key, fact.get("page_hint")
                    )
                continue
            confirmed_facts = [
                fact for fact in replacement_facts if fact.get("confirmed_by_user")
            ]
            if confirmed_facts:
                keeper = choose_keeper_fact(confirmed_facts)
                for fact in replacement_facts:
                    if fact["id"] == keeper["id"]:
                        conn.execute(
                            """
                            UPDATE facts
                            SET status = 'active', conflict_group_id = NULL, confirmed_by_user = 1
                            WHERE id = ?
                            """,
                            (fact["id"],),
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE facts
                            SET status = 'superseded', conflict_group_id = NULL
                            WHERE id = ?
                            """,
                            (fact["id"],),
                        )
                auto_superseded += len(replacement_facts) - 1
                dismiss_resolved_conflict_questions(
                    conn, entity_key, keeper.get("page_hint")
                )
                continue

            ordered = sorted(replacement_facts, key=fact_recency_key)
            latest = ordered[-1]
            if fact_is_auto_winner(latest):
                older = ordered[:-1]
                supersedes_id = older[-1]["id"] if older else None
                conn.execute(
                    """
                    UPDATE facts
                    SET status = 'active', supersedes_id = ?, conflict_group_id = NULL
                    WHERE id = ?
                    """,
                    (supersedes_id, latest["id"]),
                )
                for fact in older:
                    conn.execute(
                        """
                        UPDATE facts
                        SET status = 'superseded', conflict_group_id = NULL
                        WHERE id = ?
                        """,
                        (fact["id"],),
                    )
                auto_superseded += len(older)
                dismiss_resolved_conflict_questions(
                    conn, entity_key, latest.get("page_hint")
                )
                continue

            conflict_group_id = new_id("factconflict")
            conflict_group_ids.append(conflict_group_id)
            fact_ids = [fact["id"] for fact in replacement_facts]
            for fact in replacement_facts:
                conn.execute(
                    """
                    UPDATE facts
                    SET status = 'conflicted', conflict_group_id = ?
                    WHERE id = ?
                    """,
                    (conflict_group_id, fact["id"]),
                )
            question_id = ensure_open_conflict_question(
                conn, conflict_group_id, replacement_facts, fact_ids
            )
            if question_id:
                created_question_ids.append(question_id)
    return {
        "auto_merged": auto_merged,
        "auto_superseded": auto_superseded,
        "created_question_ids": created_question_ids,
        "conflict_group_ids": conflict_group_ids,
    }


def fact_recency_key(fact: dict[str, Any]) -> tuple[str, str]:
    return (str(fact.get("observed_at") or ""), str(fact.get("created_at") or ""))


def fact_is_auto_winner(fact: dict[str, Any]) -> bool:
    return float(fact.get("confidence") or 0.0) >= AUTO_SUPERSEDE_CONFIDENCE and bool(
        fact.get("source_ids")
    )


def merge_similar_replacement_facts(
    conn: Any, facts: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    clusters: list[list[dict[str, Any]]] = []
    for fact in sorted(facts, key=fact_recency_key, reverse=True):
        for cluster in clusters:
            if any(facts_should_merge(fact, candidate) for candidate in cluster):
                cluster.append(fact)
                break
        else:
            clusters.append([fact])

    survivors: list[dict[str, Any]] = []
    merged_count = 0
    timestamp = now_iso()
    for cluster in clusters:
        if len(cluster) == 1:
            survivors.append(cluster[0])
            continue
        keeper = choose_keeper_fact(cluster)
        merged = merge_fact_cluster_values(cluster, keeper, timestamp)
        conn.execute(
            """
            UPDATE facts
            SET statement = ?, source_ids = ?, observed_at = ?, confidence = ?,
                status = 'active', supersedes_id = ?, conflict_group_id = NULL,
                metadata = ?, last_seen_at = ?
            WHERE id = ?
            """,
            (
                merged["statement"],
                dumps(merged["source_ids"]),
                merged["observed_at"],
                merged["confidence"],
                merged["supersedes_id"],
                dumps(merged["metadata"]),
                timestamp,
                keeper["id"],
            ),
        )
        for fact in cluster:
            if fact["id"] == keeper["id"]:
                continue
            conn.execute(
                """
                UPDATE facts
                SET status = 'superseded', conflict_group_id = NULL
                WHERE id = ?
                """,
                (fact["id"],),
            )
        survivors.append(
            {
                **keeper,
                "statement": merged["statement"],
                "source_ids": merged["source_ids"],
                "observed_at": merged["observed_at"],
                "confidence": merged["confidence"],
                "status": "active",
                "supersedes_id": merged["supersedes_id"],
                "conflict_group_id": None,
                "metadata": merged["metadata"],
                "last_seen_at": timestamp,
            }
        )
        merged_count += len(cluster) - 1
    return sorted(survivors, key=fact_recency_key), merged_count


def choose_keeper_fact(facts: list[dict[str, Any]]) -> dict[str, Any]:
    return max(
        facts,
        key=lambda fact: (
            bool(fact.get("confirmed_by_user")),
            float(fact.get("confidence") or 0.0),
            str(fact.get("observed_at") or ""),
            statement_information_score(str(fact.get("statement") or "")),
            str(fact.get("created_at") or ""),
        ),
    )


def merge_fact_cluster_values(
    facts: list[dict[str, Any]],
    keeper: dict[str, Any],
    timestamp: str,
) -> dict[str, Any]:
    source_ids = stable_unique(
        source_id for fact in facts for source_id in fact.get("source_ids") or []
    )
    observed_at = max(str(fact.get("observed_at") or "") for fact in facts) or None
    confidence = max(float(fact.get("confidence") or 0.0) for fact in facts)
    statement = choose_best_cluster_statement(facts, keeper)
    merged_fact_ids = [fact["id"] for fact in facts if fact["id"] != keeper["id"]]
    supersedes_id = max(
        (fact for fact in facts if fact["id"] != keeper["id"]),
        key=fact_recency_key,
        default={},
    ).get("id")
    metadata = dict(keeper.get("metadata") or {})
    merged_facts = list(metadata.get("merged_facts") or [])
    merged_facts.extend(
        {
            "fact_id": fact["id"],
            "statement": fact["statement"],
            "source_ids": fact.get("source_ids") or [],
            "confidence": fact.get("confidence"),
            "observed_at": fact.get("observed_at"),
            "metadata": fact.get("metadata") or {},
            "merged_at": timestamp,
        }
        for fact in facts
        if fact["id"] != keeper["id"]
    )
    metadata["merged_facts"] = merged_facts
    metadata["merged_fact_ids"] = stable_unique(
        [*metadata.get("merged_fact_ids", []), *merged_fact_ids]
    )
    metadata["merge_reason"] = (
        "near-duplicate replacement facts automatically synthesized before conflict review"
    )
    return {
        "statement": statement,
        "source_ids": source_ids,
        "observed_at": observed_at,
        "confidence": confidence,
        "supersedes_id": supersedes_id,
        "metadata": metadata,
    }


def choose_best_cluster_statement(
    facts: list[dict[str, Any]], keeper: dict[str, Any]
) -> str:
    return max(
        [str(fact.get("statement") or "") for fact in facts],
        key=lambda statement: (
            max(
                float(fact.get("confidence") or 0.0)
                for fact in facts
                if str(fact.get("statement") or "") == statement
            ),
            statement_information_score(statement),
            statement == str(keeper.get("statement") or ""),
        ),
    )


def dismiss_resolved_conflict_questions(
    conn: Any, entity_key: str, page_hint: Any
) -> None:
    found = conn.execute(
        """
        SELECT *
        FROM open_questions
        WHERE status = 'open'
          AND kind = 'conflict'
          AND entity_key = ?
          AND (page_hint = ? OR (page_hint IS NULL AND ? IS NULL))
        """,
        (entity_key, page_hint, page_hint),
    ).fetchall()
    for question in found:
        fact_ids = loads(question["fact_ids"], [])
        if not fact_ids:
            continue
        placeholders = ",".join("?" for _ in fact_ids)
        statuses = [
            str(row["status"])
            for row in conn.execute(
                f"SELECT status FROM facts WHERE id IN ({placeholders})",
                fact_ids,
            )
        ]
        if statuses and "conflicted" not in statuses:
            conn.execute(
                """
                UPDATE open_questions
                SET status = 'dismissed',
                    answer = ?,
                    answered_at = ?
                WHERE id = ?
                """,
                (
                    dumps(
                        {
                            "reason": "underlying facts were merged or superseded automatically"
                        }
                    ),
                    now_iso(),
                    question["id"],
                ),
            )


def facts_are_largely_same(left: str, right: str) -> bool:
    return fact_similarity_signals(left, right)["largely_same"]


def facts_should_merge(left: dict[str, Any], right: dict[str, Any]) -> bool:
    signals = fact_similarity_signals(
        str(left.get("statement") or ""), str(right.get("statement") or "")
    )
    if signals["largely_same"]:
        return True
    if signals["contradiction"]:
        return False
    if not facts_share_meaningful_sources(left, right):
        return False
    return bool(signals["source_backed_match"] or signals["anchor_match"])


def fact_similarity_signals(left: str, right: str) -> dict[str, Any]:
    left_normalized = normalized_statement(left)
    right_normalized = normalized_statement(right)
    empty_result = {
        "largely_same": False,
        "source_backed_match": False,
        "anchor_match": False,
        "contradiction": False,
        "sequence_ratio": 0.0,
        "token_overlap": 0.0,
        "token_jaccard": 0.0,
        "anchor_coverage": 0.0,
    }
    if not left_normalized or not right_normalized:
        return empty_result
    if left_normalized == right_normalized:
        return {
            **empty_result,
            "largely_same": True,
            "sequence_ratio": 1.0,
            "token_overlap": 1.0,
            "token_jaccard": 1.0,
            "anchor_coverage": 1.0,
        }
    if has_material_contradiction_cues(left_normalized, right_normalized):
        return {**empty_result, "contradiction": True}
    sequence_ratio = SequenceMatcher(None, left_normalized, right_normalized).ratio()
    left_tokens = set(fact_tokens(left_normalized))
    right_tokens = set(fact_tokens(right_normalized))
    if not left_tokens or not right_tokens:
        return {**empty_result, "sequence_ratio": sequence_ratio}
    overlap = len(left_tokens & right_tokens)
    token_overlap = overlap / min(len(left_tokens), len(right_tokens))
    token_jaccard = overlap / len(left_tokens | right_tokens)
    anchor_coverage = fact_anchor_coverage(left_tokens, right_tokens)
    largely_same = sequence_ratio >= NEAR_DUPLICATE_SEQUENCE_RATIO or (
        token_overlap >= NEAR_DUPLICATE_TOKEN_OVERLAP
        and token_jaccard >= NEAR_DUPLICATE_TOKEN_JACCARD
    )
    source_backed_match = (
        token_overlap >= RELATED_SOURCE_TOKEN_OVERLAP
        and token_jaccard >= RELATED_SOURCE_TOKEN_JACCARD
    )
    anchor_match = anchor_coverage >= ANCHOR_TOKEN_COVERAGE
    return {
        "largely_same": largely_same,
        "source_backed_match": source_backed_match,
        "anchor_match": anchor_match,
        "contradiction": False,
        "sequence_ratio": sequence_ratio,
        "token_overlap": token_overlap,
        "token_jaccard": token_jaccard,
        "anchor_coverage": anchor_coverage,
    }


def facts_share_meaningful_sources(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_sources = set(str(source_id) for source_id in left.get("source_ids") or [])
    right_sources = set(str(source_id) for source_id in right.get("source_ids") or [])
    if not left_sources or not right_sources:
        return False
    overlap = left_sources & right_sources
    return (
        len(overlap) >= 2
        or len(overlap) / min(len(left_sources), len(right_sources)) >= 0.5
    )


def facts_directly_conflict(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return has_material_contradiction_cues(
        str(left.get("statement") or "").lower(),
        str(right.get("statement") or "").lower(),
    )


def fact_anchor_coverage(left_tokens: set[str], right_tokens: set[str]) -> float:
    left_anchors = fact_anchor_tokens(left_tokens)
    right_anchors = fact_anchor_tokens(right_tokens)
    if not left_anchors or not right_anchors:
        return 0.0
    return len(left_anchors & right_anchors) / min(
        len(left_anchors), len(right_anchors)
    )


def fact_anchor_tokens(tokens: set[str]) -> set[str]:
    return {
        token
        for token in tokens
        if len(token) >= 5
        and token not in FACT_LOW_SIGNAL_TOKENS
        and token not in FACT_STOPWORDS
    }


def fact_tokens(statement: str) -> list[str]:
    normalized = statement.lower()
    normalized = normalized.replace("children's", "children").replace(
        "childrens", "children"
    )
    normalized = re.sub(r"\bre[- ]?creat(?:e|ed|es|ing)\b", "recreate", normalized)
    raw_tokens = re.findall(r"[a-z0-9]+", normalized)
    tokens = []
    for token in raw_tokens:
        canonical = canonical_fact_token(token)
        if canonical and canonical not in FACT_STOPWORDS:
            tokens.append(canonical)
    return tokens


def canonical_fact_token(token: str) -> str:
    token = FACT_TOKEN_ALIASES.get(token, token)
    if token in {"ai", "llm", "api", "ui"}:
        return token
    if token.endswith("ss"):
        return FACT_TOKEN_ALIASES.get(token, token)
    if len(token) > 5 and token.endswith("ies"):
        token = f"{token[:-3]}y"
    elif len(token) > 6 and token.endswith("ing"):
        token = token[:-3]
    elif len(token) > 5 and token.endswith("ed"):
        token = token[:-2]
    elif len(token) > 4 and token.endswith("s"):
        token = token[:-1]
    return FACT_TOKEN_ALIASES.get(token, token)


def has_material_contradiction_cues(left: str, right: str) -> bool:
    if has_material_negation_conflict(left, right):
        return True
    if extracted_material_values(left) != extracted_material_values(right):
        left_values = extracted_material_values(left)
        right_values = extracted_material_values(right)
        if left_values and right_values:
            return True
    left_tokens = set(re.findall(r"[a-z]+", left.lower()))
    right_tokens = set(re.findall(r"[a-z]+", right.lower()))
    for group in MATERIAL_CONTRAST_GROUPS:
        left_hits = left_tokens & group
        right_hits = right_tokens & group
        if left_hits and right_hits and left_hits != right_hits:
            return True
    return False


def has_material_negation_conflict(left: str, right: str) -> bool:
    left_negated = negated_content_token_sets(left)
    right_negated = negated_content_token_sets(right)
    if left_negated and not right_negated:
        return any(
            negated_tokens_match_statement(tokens, right) for tokens in left_negated
        )
    if right_negated and not left_negated:
        return any(
            negated_tokens_match_statement(tokens, left) for tokens in right_negated
        )
    return False


def negated_content_token_sets(statement: str) -> list[set[str]]:
    lowered = statement.lower()
    lowered = re.sub(r"\bnot\s+(?:merely|simply|just|only)\b", "", lowered)
    raw_tokens = re.findall(r"[a-z']+", lowered)
    negated: list[set[str]] = []
    for index, token in enumerate(raw_tokens):
        if token not in NEGATION_TOKENS:
            continue
        window = raw_tokens[index + 1 : index + 7]
        tokens = {
            canonical
            for canonical in (canonical_fact_token(item) for item in window)
            if canonical
            and canonical not in FACT_STOPWORDS
            and canonical not in FACT_LOW_SIGNAL_TOKENS
            and canonical not in NEGATION_TOKENS
        }
        if tokens:
            negated.append(tokens)
    return negated


def negated_tokens_match_statement(negated_tokens: set[str], statement: str) -> bool:
    statement_tokens = {
        token for token in fact_tokens(statement) if token not in FACT_LOW_SIGNAL_TOKENS
    }
    overlap = negated_tokens & statement_tokens
    if len(overlap) >= 2:
        return True
    return any(
        len(token) >= 5 and token not in {"broad", "complete", "full", "partial"}
        for token in overlap
    )


def extracted_material_values(statement: str) -> set[str]:
    months = (
        "jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|"
        "aug|august|sep|sept|september|oct|october|nov|november|dec|december"
    )
    weekdays = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    patterns = [
        rf"\b(?:{months})\s+\d{{1,2}}(?:,\s*\d{{4}})?\b",
        r"\b\d{4}-\d{2}-\d{2}\b",
        rf"\b(?:{weekdays})\b",
        r"\b\d+(?:\.\d+)?%?\b",
        r"\$[\d,]+(?:\.\d+)?",
    ]
    values: set[str] = set()
    lowered = statement.lower()
    for pattern in patterns:
        values.update(re.findall(pattern, lowered))
    return values


def ensure_open_conflict_question(
    conn: Any,
    conflict_group_id: str,
    facts: list[dict[str, Any]],
    fact_ids: list[str],
) -> str | None:
    first_fact = facts[0]
    existing = conn.execute(
        """
        SELECT id
        FROM open_questions
        WHERE status = 'open' AND kind = 'conflict' AND entity_key = ? AND page_hint = ?
        """,
        (first_fact["entity_key"], first_fact.get("page_hint")),
    ).fetchone()
    if existing:
        return None
    question_id = new_id("question")
    title = human_title_for_path(
        str(first_fact.get("page_hint") or first_fact["entity_key"])
    )
    conn.execute(
        """
        INSERT INTO open_questions(
          id, kind, entity_key, page_hint, fact_ids, question, options,
          status, context, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            question_id,
            "conflict",
            first_fact["entity_key"],
            first_fact.get("page_hint"),
            dumps(fact_ids),
            f"What is currently true for {title}?",
            dumps(question_options_for_facts(facts)),
            "open",
            dumps({"conflict_group_id": conflict_group_id}),
            now_iso(),
        ),
    )
    return question_id


def question_options_for_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "fact_id": fact["id"],
            "label": option_label(fact),
            "statement": fact["statement"],
            "confidence": fact["confidence"],
            "observed_at": fact.get("observed_at"),
            "source_ids": fact.get("source_ids") or [],
        }
        for fact in sorted(facts, key=fact_recency_key, reverse=True)
    ]


def option_label(fact: dict[str, Any]) -> str:
    observed = str(fact.get("observed_at") or "undated")[:10]
    confidence = float(fact.get("confidence") or 0.0)
    return f"{observed} ({confidence:.2f}): {compact_statement(fact['statement'], 140)}"


def answer_open_question(
    paths: BrainPaths,
    question_id: str,
    *,
    selected_fact_id: str | None = None,
    answer: str | None = None,
    overwrite_existing: bool = False,
) -> dict[str, Any]:
    timestamp = now_iso()
    with connection(paths.sqlite_path) as conn:
        question_row = conn.execute(
            "SELECT * FROM open_questions WHERE id = ?", (question_id,)
        ).fetchone()
        if not question_row:
            raise ValueError(f"open question not found: {question_id}")
        question = row_to_question(question_row)
        if question["status"] != "open":
            raise ValueError(f"question is not open: {question_id}")
        fact_ids = [str(fact_id) for fact_id in question.get("fact_ids") or []]
        page_hint = question.get("page_hint")
        if selected_fact_id:
            if selected_fact_id not in fact_ids:
                raise ValueError("selected_fact_id is not one of the question facts")
            conn.execute(
                """
                UPDATE facts
                SET status = 'active', confirmed_by_user = 1, conflict_group_id = NULL
                WHERE id = ?
                """,
                (selected_fact_id,),
            )
            for fact_id in fact_ids:
                if fact_id != selected_fact_id:
                    conn.execute(
                        """
                        UPDATE facts
                        SET status = 'superseded', conflict_group_id = NULL
                        WHERE id = ?
                        """,
                        (fact_id,),
                    )
            answer_payload = {
                "selected_fact_id": selected_fact_id,
                "answer": answer or "",
            }
        else:
            answer_text = compact_statement(answer or "", 1000)
            if not answer_text:
                raise ValueError("answer or selected_fact_id is required")
            manual_fact_id = new_id("fact")
            conn.execute(
                """
                INSERT INTO facts(
                  id, statement, entity_key, page_hint, section_hint, source_ids,
                  observed_at, confidence, status, confirmed_by_user, metadata,
                  created_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manual_fact_id,
                    answer_text,
                    question.get("entity_key") or f"manual:{question_id}",
                    page_hint,
                    "Summary",
                    dumps([f"manual:question:{question_id}"]),
                    timestamp,
                    1.0,
                    "active",
                    1,
                    dumps({"question_id": question_id, "answer": answer_text}),
                    timestamp,
                    timestamp,
                ),
            )
            for fact_id in fact_ids:
                conn.execute(
                    """
                    UPDATE facts
                    SET status = 'superseded', conflict_group_id = NULL
                    WHERE id = ?
                    """,
                    (fact_id,),
                )
            answer_payload = {"selected_fact_id": manual_fact_id, "answer": answer_text}
        conn.execute(
            """
            UPDATE open_questions
            SET status = 'answered', answer = ?, answered_at = ?
            WHERE id = ?
            """,
            (dumps(answer_payload), timestamp, question_id),
        )
    curation = curate_managed_pages(
        paths,
        page_hints=[str(page_hint)] if page_hint else [],
        overwrite_existing=overwrite_existing,
    )
    return {
        "question": get_question(paths, question_id),
        "curation": curation,
        "dashboard": wiki_fact_dashboard(paths),
    }


def reconcile_open_fact_questions(
    paths: BrainPaths, *, overwrite_existing: bool = False
) -> dict[str, Any]:
    """Merge duplicate alternatives inside existing open questions."""
    route_result = canonicalize_fact_routes(paths)
    route_resolve_result = (
        resolve_fact_groups(paths, route_result["entity_keys"])
        if route_result["entity_keys"]
        else {
            "auto_merged": 0,
            "auto_superseded": 0,
            "created_question_ids": [],
            "conflict_group_ids": [],
        }
    )
    dismissed_question_ids: list[str] = []
    updated_question_ids: list[str] = []
    merged_facts = 0
    page_hints: list[str] = list(route_result["page_hints"])
    timestamp = now_iso()
    with connection(paths.sqlite_path) as conn:
        question_rows = rows(
            conn,
            """
            SELECT *
            FROM open_questions
            WHERE status = 'open' AND kind = 'conflict'
            ORDER BY created_at
            """,
        )
        for question_row in question_rows:
            question = row_to_question(question_row)
            fact_ids = [str(fact_id) for fact_id in question.get("fact_ids") or []]
            if len(fact_ids) < 2:
                continue
            placeholders = ",".join("?" for _ in fact_ids)
            facts = [
                row_to_fact(row)
                for row in conn.execute(
                    f"SELECT * FROM facts WHERE id IN ({placeholders})",
                    fact_ids,
                )
            ]
            if len(facts) < 2:
                continue
            clusters = cluster_mergeable_facts(facts)
            if all(len(cluster) == 1 for cluster in clusters):
                continue
            survivors: list[dict[str, Any]] = []
            for cluster in clusters:
                if len(cluster) == 1:
                    survivors.append(cluster[0])
                    continue
                merged_survivors, merged_count = merge_similar_replacement_facts(
                    conn, cluster
                )
                survivors.extend(merged_survivors)
                merged_facts += merged_count
            survivors = sorted(survivors, key=fact_recency_key, reverse=True)
            page_hints.extend(
                str(fact.get("page_hint") or "")
                for fact in survivors
                if fact.get("page_hint")
            )
            if len(survivors) <= 1:
                conn.execute(
                    """
                    UPDATE open_questions
                    SET status = 'dismissed',
                        answer = ?,
                        answered_at = ?
                    WHERE id = ?
                    """,
                    (
                        dumps(
                            {
                                "reason": "duplicate alternatives merged by chief-of-staff reconciliation"
                            }
                        ),
                        timestamp,
                        question["id"],
                    ),
                )
                dismissed_question_ids.append(question["id"])
                continue
            survivor_ids = [fact["id"] for fact in survivors]
            conflict_group_id = str(
                question.get("context", {}).get("conflict_group_id")
                or new_id("factconflict")
            )
            for fact in survivors:
                conn.execute(
                    """
                    UPDATE facts
                    SET status = 'conflicted', conflict_group_id = ?
                    WHERE id = ?
                    """,
                    (conflict_group_id, fact["id"]),
                )
            conn.execute(
                """
                UPDATE open_questions
                SET fact_ids = ?, options = ?, context = ?
                WHERE id = ?
                """,
                (
                    dumps(survivor_ids),
                    dumps(question_options_for_facts(survivors)),
                    dumps(
                        {
                            **question.get("context", {}),
                            "conflict_group_id": conflict_group_id,
                            "reconciled_at": timestamp,
                        }
                    ),
                    question["id"],
                ),
            )
            updated_question_ids.append(question["id"])
    curation = curate_managed_pages(
        paths,
        page_hints=stable_unique(page_hints),
        overwrite_existing=overwrite_existing,
    )
    return {
        "dismissed_question_ids": stable_unique(
            [*route_result["dismissed_question_ids"], *dismissed_question_ids]
        ),
        "updated_question_ids": updated_question_ids,
        "merged_facts": merged_facts + route_resolve_result["auto_merged"],
        "rerouted_fact_ids": route_result["updated_fact_ids"],
        "route_resolution": route_resolve_result,
        "curation": curation,
        "dashboard": wiki_fact_dashboard(paths),
    }


def curate_all_managed_fact_pages(
    paths: BrainPaths, *, overwrite_existing: bool = False
) -> dict[str, Any]:
    """Regenerate managed wiki pages from the current active fact ledger."""
    page_hints = active_fact_page_hints(paths)
    curation = curate_managed_pages(
        paths,
        page_hints=page_hints,
        overwrite_existing=overwrite_existing,
    )
    archived_orphans = archive_orphan_managed_pages(paths, active_page_hints=page_hints)
    final_lint = lint_wiki(paths)
    return {
        "page_hints": page_hints,
        "page_count": len(page_hints),
        "curation": curation,
        "archived_orphans": archived_orphans,
        "lint": final_lint,
        "dashboard": wiki_fact_dashboard(paths),
    }


def active_fact_page_hints(paths: BrainPaths) -> list[str]:
    with connection(paths.sqlite_path) as conn:
        return stable_unique(
            str(row["page_hint"] or "")
            for row in conn.execute(
                """
                SELECT DISTINCT page_hint
                FROM facts
                WHERE status = 'active'
                  AND page_hint IS NOT NULL
                  AND page_hint != ''
                ORDER BY page_hint
                """
            )
        )


def archive_orphan_managed_pages(
    paths: BrainPaths, *, active_page_hints: list[str]
) -> list[dict[str, Any]]:
    active_paths = {str(page_hint or "") for page_hint in active_page_hints}
    archived: list[dict[str, Any]] = []
    if not paths.wiki.exists():
        return archived
    for path in sorted(paths.wiki.rglob("*.md")):
        relative_path = path.relative_to(paths.wiki).as_posix()
        if relative_path in active_paths:
            continue
        existing_text = path.read_text(encoding="utf-8", errors="replace")
        if not is_managed_page(existing_text):
            continue
        archived_markdown = render_archived_managed_page(relative_path, existing_text)
        path.write_text(archived_markdown.rstrip() + "\n", encoding="utf-8")
        archived.append(
            {"relative_path": relative_path, "path": str(path), "status": "archived"}
        )
    return archived


def render_archived_managed_page(page_hint: str, existing_text: str) -> str:
    frontmatter, _body = parse_frontmatter(existing_text)
    title = str((frontmatter or {}).get("title") or human_title_for_path(page_hint))
    page_type = page_type_for_path(page_hint, frontmatter)
    today = now_iso()[:10]
    page_id = str((frontmatter or {}).get("id") or stable_page_id(page_hint, title))
    source_ids = stable_unique(
        str(source_id) for source_id in (frontmatter or {}).get("source_ids") or []
    )
    metadata = {
        "title": title,
        "page_type": page_type,
        "id": page_id,
        "status": "archived",
        "created_at": str((frontmatter or {}).get("created_at") or today),
        "updated_at": today,
        "source_ids": source_ids,
        "related": list((frontmatter or {}).get("related") or []),
        "tags": stable_unique([*((frontmatter or {}).get("tags") or []), MANAGED_TAG]),
        "managed": True,
        "fact_ids": [],
    }
    body_sections = managed_page_sections(page_type, [], source_ids)
    body_sections["Summary"] = (
        "This managed page has no active facts in the fact ledger and has been archived. "
        "If new source-backed facts are routed here later, the curator can regenerate it as an active page."
    )
    body = "\n\n".join(
        f"## {heading}\n\n{content}" for heading, content in body_sections.items()
    )
    return (
        "---\n"
        f"{yaml.safe_dump(metadata, sort_keys=False).strip()}\n"
        "---\n\n"
        f"{CHIEF_OF_STAFF_MARKER}\n\n"
        f"# {title}\n\n"
        f"{body}\n"
    )


def canonicalize_fact_routes(paths: BrainPaths) -> dict[str, Any]:
    timestamp = now_iso()
    updated_fact_ids: list[str] = []
    affected_entity_keys: list[str] = []
    affected_page_hints: list[str] = []
    dismissed_question_ids: list[str] = []
    with connection(paths.sqlite_path) as conn:
        fact_rows = rows(
            conn,
            """
            SELECT *
            FROM facts
            WHERE status != 'retracted'
              AND page_hint IS NOT NULL
            """,
        )
        for row in fact_rows:
            original_page_hint = str(row["page_hint"] or "")
            canonical_page_hint = canonical_page_hint_for_fact(original_page_hint)
            if not canonical_page_hint or canonical_page_hint == original_page_hint:
                continue
            section_hint = str(row["section_hint"] or "")
            canonical_entity_key = entity_key_for_change(
                topic_for_path(canonical_page_hint), canonical_page_hint, section_hint
            )
            metadata = loads(row["metadata"], {})
            metadata.setdefault("original_page_hint", original_page_hint)
            metadata.setdefault("original_entity_key", row["entity_key"])
            metadata["canonical_page_hint"] = canonical_page_hint
            metadata["canonicalized_at"] = timestamp
            conn.execute(
                """
                UPDATE facts
                SET page_hint = ?,
                    entity_key = ?,
                    metadata = ?
                WHERE id = ?
                """,
                (canonical_page_hint, canonical_entity_key, dumps(metadata), row["id"]),
            )
            updated_fact_ids.append(str(row["id"]))
            affected_entity_keys.append(canonical_entity_key)
            affected_page_hints.append(canonical_page_hint)

        if updated_fact_ids:
            changed = set(updated_fact_ids)
            for question in rows(
                conn,
                """
                SELECT *
                FROM open_questions
                WHERE status = 'open'
                """,
            ):
                fact_ids = {str(fact_id) for fact_id in loads(question["fact_ids"], [])}
                if not fact_ids.intersection(changed):
                    continue
                conn.execute(
                    """
                    UPDATE open_questions
                    SET status = 'dismissed',
                        answer = ?,
                        answered_at = ?
                    WHERE id = ?
                    """,
                    (
                        dumps(
                            {
                                "reason": "facts rerouted to canonical page before conflict review"
                            }
                        ),
                        timestamp,
                        question["id"],
                    ),
                )
                dismissed_question_ids.append(str(question["id"]))
    return {
        "updated_fact_ids": stable_unique(updated_fact_ids),
        "entity_keys": stable_unique(affected_entity_keys),
        "page_hints": stable_unique(affected_page_hints),
        "dismissed_question_ids": stable_unique(dismissed_question_ids),
    }


def cluster_mergeable_facts(facts: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    clusters: list[list[dict[str, Any]]] = []
    for fact in sorted(facts, key=fact_recency_key, reverse=True):
        for cluster in clusters:
            if any(facts_should_merge(fact, candidate) for candidate in cluster):
                cluster.append(fact)
                break
        else:
            clusters.append([fact])
    return clusters


def curate_managed_pages(
    paths: BrainPaths,
    *,
    page_hints: list[str],
    overwrite_existing: bool = False,
) -> dict[str, Any]:
    page_hints = stable_unique(
        page_hint for page_hint in page_hints if str(page_hint or "").strip()
    )
    if not page_hints:
        return {"pages": [], "lint": None, "lint_errors": []}
    facts_by_page = active_facts_by_page(paths, page_hints)
    before_lint = lint_wiki(paths)
    originals: dict[Path, str | None] = {}
    pages: list[dict[str, Any]] = []
    changed_paths: list[str] = []
    for page_hint in page_hints:
        facts = facts_by_page.get(page_hint) or []
        if not facts:
            continue
        try:
            target = safe_fact_wiki_path(paths, page_hint)
        except ValueError as exc:
            pages.append(
                {
                    "page_hint": page_hint,
                    "written": False,
                    "reason": str(exc),
                    "facts": facts,
                }
            )
            continue
        existing_text = (
            target.read_text(encoding="utf-8", errors="replace")
            if target.exists()
            else ""
        )
        markdown = render_managed_page(page_hint, facts, existing_text)
        projection_errors = duplicate_fact_projection_errors(page_hint, markdown)
        can_write = (
            not target.exists() or overwrite_existing or is_managed_page(existing_text)
        )
        page_result = {
            "page_hint": page_hint,
            "path": str(target),
            "relative_path": page_hint,
            "fact_ids": [fact["id"] for fact in facts],
            "markdown": markdown,
            "projection_errors": projection_errors,
            "written": False,
            "reason": "",
        }
        if projection_errors:
            page_result["reason"] = "managed write failed duplicate projection check"
            pages.append(page_result)
            continue
        if not can_write:
            page_result["reason"] = "existing page is not managed; showing draft only"
            pages.append(page_result)
            continue
        originals[target] = existing_text if target.exists() else None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(markdown.rstrip() + "\n", encoding="utf-8")
        page_result["written"] = True
        pages.append(page_result)
        changed_paths.append(page_hint)

    lint_result = lint_wiki(paths)
    before_errors = set(before_lint.get("errors") or [])
    changed_errors = [
        error
        for error in lint_result.get("errors", [])
        if error not in before_errors and error.split(":", 1)[0] in set(changed_paths)
    ]
    if changed_errors:
        restore_page_originals(originals)
        lint_result = lint_wiki(paths)
        for page in pages:
            if page.get("relative_path") in changed_paths:
                page["written"] = False
                page["reason"] = "managed write failed wiki lint"
    else:
        sync_managed_page_index(paths, pages)
    return {"pages": pages, "lint": lint_result, "lint_errors": changed_errors}


def active_facts_by_page(
    paths: BrainPaths, page_hints: list[str]
) -> dict[str, list[dict[str, Any]]]:
    placeholders = ",".join("?" for _ in page_hints)
    with connection(paths.sqlite_path) as conn:
        fact_rows = rows(
            conn,
            f"""
            SELECT *
            FROM facts
            WHERE status = 'active' AND page_hint IN ({placeholders})
            ORDER BY page_hint, observed_at, created_at
            """,
            page_hints,
        )
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in fact_rows:
        fact = row_to_fact(row)
        grouped[str(fact.get("page_hint") or "")].append(fact)
    return grouped


def safe_fact_wiki_path(paths: BrainPaths, relative_path: str) -> Path:
    raw = str(relative_path or "").strip()
    path = Path(raw)
    if not raw or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"wiki path must be relative to wiki root: {raw}")
    if path.suffix != ".md":
        raise ValueError(f"wiki path must point to a Markdown file: {raw}")
    root = paths.wiki.resolve()
    target = (paths.wiki / path).resolve()
    target.relative_to(root)
    return target


def is_managed_page(markdown: str) -> bool:
    frontmatter, _body = parse_frontmatter(markdown)
    return CHIEF_OF_STAFF_MARKER in markdown or bool((frontmatter or {}).get("managed"))


def render_managed_page(
    page_hint: str, facts: list[dict[str, Any]], existing_text: str = ""
) -> str:
    frontmatter, _body = parse_frontmatter(existing_text)
    title = str((frontmatter or {}).get("title") or human_title_for_path(page_hint))
    page_type = page_type_for_path(page_hint, frontmatter)
    source_ids = stable_unique(
        source_id for fact in facts for source_id in fact.get("source_ids") or []
    )
    fact_ids = [fact["id"] for fact in facts]
    today = now_iso()[:10]
    page_id = str((frontmatter or {}).get("id") or stable_page_id(page_hint, title))
    metadata = {
        "title": title,
        "page_type": page_type,
        "id": page_id,
        "status": "active",
        "created_at": str((frontmatter or {}).get("created_at") or today),
        "updated_at": today,
        "source_ids": source_ids,
        "related": list((frontmatter or {}).get("related") or []),
        "tags": stable_unique([*((frontmatter or {}).get("tags") or []), MANAGED_TAG]),
        "managed": True,
        "fact_ids": fact_ids,
    }
    body_sections = managed_page_sections(page_type, facts, source_ids)
    body = "\n\n".join(
        f"## {heading}\n\n{content}" for heading, content in body_sections.items()
    )
    return (
        "---\n"
        f"{yaml.safe_dump(metadata, sort_keys=False).strip()}\n"
        "---\n\n"
        f"{CHIEF_OF_STAFF_MARKER}\n\n"
        f"# {title}\n\n"
        f"{body}\n"
    )


def page_type_for_path(page_hint: str, frontmatter: dict[str, Any] | None) -> str:
    existing = str((frontmatter or {}).get("page_type") or "")
    if existing:
        return existing
    root = Path(page_hint).parts[0] if Path(page_hint).parts else ""
    return {
        "projects": "project",
        "concepts": "concept",
        "decisions": "decision",
        "people": "person",
        "open_loops": "open_loop",
        "timelines": "timeline",
        "references": "reference",
    }.get(root, "concept")


def managed_page_sections(
    page_type: str,
    facts: list[dict[str, Any]],
    source_ids: list[str],
) -> dict[str, str]:
    sections: dict[str, str] = {}
    routed_facts = route_facts_to_sections(page_type, facts)
    source_bullets = bullet_list(source_ids) if source_ids else NO_SOURCE_EVIDENCE
    sections["Summary"] = synthesized_page_summary(facts, source_ids)
    sections["Key Points"] = fact_bullet_list(routed_facts.pop("Key Points", []))
    for heading in TYPE_SECTIONS.get(page_type, []):
        if heading in sections:
            continue
        sections[heading] = fact_bullet_list(routed_facts.pop(heading, []))
    for heading, routed in routed_facts.items():
        if heading in sections:
            continue
        sections[heading] = fact_bullet_list(routed)
    sections["Source Evidence"] = source_bullets
    sections["Related Pages"] = NO_RELATED_PAGES
    sections["Open Questions"] = NO_OPEN_QUESTIONS
    for heading in COMMON_SECTIONS:
        sections.setdefault(heading, NO_SECTION_FACTS)
    return sections


def route_facts_to_sections(
    page_type: str, facts: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    routed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    emitted_fact_ids: set[str] = set()
    emitted_statements: list[str] = []
    allowed_sections = (
        set(TYPE_SECTIONS.get(page_type, [])) | set(COMMON_SECTIONS) | {"Key Points"}
    )
    for fact in sorted(facts, key=render_fact_priority_key, reverse=True):
        fact_id = str(fact.get("id") or "")
        if fact_id and fact_id in emitted_fact_ids:
            continue
        statement = compact_statement(fact.get("statement") or "")
        if not statement:
            continue
        if any(
            facts_render_as_same(statement, emitted) for emitted in emitted_statements
        ):
            continue
        heading = section_for_fact(page_type, fact, allowed_sections)
        routed[heading].append(fact)
        if fact_id:
            emitted_fact_ids.add(fact_id)
        emitted_statements.append(statement)
    return routed


def render_fact_priority_key(
    fact: dict[str, Any],
) -> tuple[int, float, str, str, tuple[int, int, int, int]]:
    return (
        int(bool(fact.get("confirmed_by_user"))),
        float(fact.get("confidence") or 0.0),
        str(fact.get("observed_at") or ""),
        str(fact.get("created_at") or ""),
        statement_quality_score(str(fact.get("statement") or "")),
    )


def section_for_fact(
    page_type: str, fact: dict[str, Any], allowed_sections: set[str]
) -> str:
    section_hint = str(fact.get("section_hint") or "").strip()
    normalized_hint = normalize_section_name(section_hint)
    if normalized_hint in {"summary", "key points", "page"}:
        return "Key Points"
    if section_hint in allowed_sections and section_hint not in {
        "Summary",
        "Source Evidence",
        "Related Pages",
    }:
        return section_hint
    inferred = infer_section_from_statement(
        page_type, str(fact.get("statement") or ""), allowed_sections
    )
    if inferred:
        return inferred
    fallback = fallback_fact_section(page_type)
    return fallback if fallback in allowed_sections else "Key Points"


def normalize_section_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def infer_section_from_statement(
    page_type: str, statement: str, allowed_sections: set[str]
) -> str:
    lowered = statement.lower()
    candidates: list[str] = []
    if page_type == "concept":
        if any(
            token in lowered
            for token in (
                "matters",
                "important",
                "because",
                "strategic",
                "risk",
                "value",
            )
        ):
            candidates.append("Why It Matters")
        if any(
            token in lowered
            for token in (
                "works",
                "flow",
                "process",
                "model",
                "architecture",
                "staged",
                "progression",
            )
        ):
            candidates.append("How It Works")
    if page_type == "project":
        if any(token in lowered for token in ("goal", "objective", "target")):
            candidates.append("Goals")
        if any(
            token in lowered for token in ("decided", "decision", "choose", "chosen")
        ):
            candidates.append("Decisions")
        if re.search(
            r"\b20\d{2}-\d{2}|\bjan|\bfeb|\bmar|\bapr|\bmay|\bjun|\bjul|\baug|\bsep|\boct|\bnov|\bdec",
            lowered,
        ):
            candidates.append("Timeline")
    if page_type == "decision":
        if any(token in lowered for token in ("because", "rationale", "reason")):
            candidates.append("Rationale")
        if any(token in lowered for token in ("alternative", "option")):
            candidates.append("Alternatives Considered")
        if any(token in lowered for token in ("impact", "consequence", "result")):
            candidates.append("Consequences")
    if page_type == "open_loop":
        if "?" in statement:
            candidates.append("Question")
        if any(token in lowered for token in ("need", "evidence", "confirm")):
            candidates.append("Needed Evidence")
    for candidate in candidates:
        if candidate in allowed_sections:
            return candidate
    return ""


def fallback_fact_section(page_type: str) -> str:
    return {
        "project": "Current State",
        "concept": "Definition",
        "decision": "Context",
        "person": "Role",
        "open_loop": "Current Understanding",
        "timeline": "Events",
        "reference": "Notes",
    }.get(page_type, "Key Points")


def synthesized_page_summary(facts: list[dict[str, Any]], source_ids: list[str]) -> str:
    if not facts:
        return "No active facts yet."
    fact_count = len(facts)
    source_count = len(source_ids)
    recent_observed = max(str(fact.get("observed_at") or "") for fact in facts).strip()
    recent_text = (
        f" Most recent source observation: {recent_observed}."
        if recent_observed
        else ""
    )
    source_text = (
        f"{source_count} source" if source_count == 1 else f"{source_count} sources"
    )
    fact_text = (
        f"{fact_count} active fact" if fact_count == 1 else f"{fact_count} active facts"
    )
    return (
        f"This managed page is maintained from {fact_text} across {source_text}. "
        "Each source-backed assertion is routed to one section below; unresolved disagreements are tracked as open questions."
        f"{recent_text}"
    )


def fact_bullet_list(facts: list[dict[str, Any]]) -> str:
    if not facts:
        return NO_SECTION_FACTS
    bullets: list[str] = []
    seen: list[str] = []
    for fact in facts:
        statement = compact_statement(fact.get("statement") or "")
        normalized = normalized_statement(statement)
        if not statement or any(
            normalized == normalized_statement(prior)
            or facts_render_as_same(statement, prior)
            for prior in seen
        ):
            continue
        seen.append(statement)
        bullets.append(f"- {statement}")
    return "\n".join(bullets) if bullets else NO_SECTION_FACTS


def bullet_list(values: list[str]) -> str:
    return "\n".join(f"- {value}" for value in values)


def facts_render_as_same(left: str, right: str) -> bool:
    signals = fact_similarity_signals(left, right)
    if signals["contradiction"]:
        return False
    return bool(
        signals["largely_same"]
        or (signals["source_backed_match"] and signals["anchor_match"])
    )


def duplicate_fact_projection_errors(page_hint: str, markdown: str) -> list[str]:
    repeated = duplicate_fact_projection_bullets(markdown)
    return [
        f"{page_hint}: duplicate managed fact bullet appears in multiple sections: {bullet}"
        for bullet in repeated
    ]


def duplicate_fact_projection_bullets(markdown: str) -> list[str]:
    _frontmatter, body = parse_frontmatter(markdown)
    section_matches = list(re.finditer(r"^##\s+(.+?)\s*$", body, flags=re.MULTILINE))
    bullets_by_text: dict[str, set[str]] = defaultdict(set)
    ignored_sections = {"Source Evidence", "Related Pages", "Open Questions"}
    ignored_bullets = {
        "- None.",
        f"- {NO_SECTION_FACTS}",
        f"- {NO_RELATED_PAGES}",
        f"- {NO_OPEN_QUESTIONS}",
        f"- {NO_SOURCE_EVIDENCE}",
    }
    for index, match in enumerate(section_matches):
        section = match.group(1).strip()
        if section in ignored_sections:
            continue
        start = match.end()
        end = (
            section_matches[index + 1].start()
            if index + 1 < len(section_matches)
            else len(body)
        )
        for line in body[start:end].splitlines():
            stripped = line.strip()
            if not stripped.startswith("- ") or stripped in ignored_bullets:
                continue
            normalized = normalized_statement(stripped[2:])
            if normalized:
                bullets_by_text[normalized].add(section)
    repeated: list[str] = []
    for normalized, sections in bullets_by_text.items():
        if len(sections) > 1:
            repeated.append(normalized)
    return repeated


def stable_page_id(page_hint: str, title: str) -> str:
    return f"managed-{slugify(title)}-{text_sha256(page_hint)[:8]}"


def human_title_for_path(page_hint: str) -> str:
    stem = Path(page_hint).stem or page_hint
    return re.sub(r"\s+", " ", stem.replace("_", " ").replace("-", " ")).strip().title()


def restore_page_originals(originals: dict[Path, str | None]) -> None:
    for path, content in originals.items():
        if content is None:
            if path.exists():
                path.unlink()
        else:
            path.write_text(content, encoding="utf-8")


def sync_managed_page_index(paths: BrainPaths, pages: list[dict[str, Any]]) -> None:
    written_pages = [page for page in pages if page.get("written")]
    if not written_pages:
        return
    with connection(paths.sqlite_path) as conn:
        for page in written_pages:
            conn.execute(
                """
                UPDATE wiki_pages
                SET managed = 1, fact_ids = ?
                WHERE path = ?
                """,
                (dumps(page.get("fact_ids") or []), str(page.get("path") or "")),
            )


def record_curation_run(
    paths: BrainPaths,
    packet_id: str,
    group_by: str,
    status: str,
    summary: dict[str, Any],
) -> str:
    run_id = new_id("wikicurate")
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO wiki_curation_runs(id, source_packet_id, group_by, status, summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, packet_id, group_by, status, dumps(summary), now_iso()),
        )
    return run_id


def update_curation_run_summary(
    paths: BrainPaths, run_id: str, summary: dict[str, Any]
) -> None:
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE wiki_curation_runs
            SET summary = ?
            WHERE id = ?
            """,
            (dumps(summary), run_id),
        )


def wiki_fact_dashboard(paths: BrainPaths) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        status_counts = {
            str(row["status"]): int(row["count"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM facts GROUP BY status"
            )
        }
        question_counts = {
            str(row["status"]): int(row["count"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM open_questions GROUP BY status"
            )
        }
        recent_facts = [
            row_to_fact(row)
            for row in conn.execute(
                """
                SELECT *
                FROM facts
                ORDER BY COALESCE(last_seen_at, created_at) DESC
                LIMIT 80
                """
            )
        ]
        open_questions = [
            row_to_question(row)
            for row in conn.execute(
                """
                SELECT *
                FROM open_questions
                WHERE status = 'open'
                ORDER BY created_at DESC
                LIMIT 50
                """
            )
        ]
        recent_runs = [
            row_to_curation_run(row)
            for row in conn.execute(
                """
                SELECT *
                FROM wiki_curation_runs
                ORDER BY created_at DESC
                LIMIT 20
                """
            )
        ]
    return {
        "counts": {
            "facts": sum(status_counts.values()),
            "by_status": dict(sorted(status_counts.items())),
            "questions": sum(question_counts.values()),
            "questions_by_status": dict(sorted(question_counts.items())),
        },
        "open_questions": open_questions,
        "recent_facts": recent_facts,
        "recent_runs": recent_runs,
    }


def get_question(paths: BrainPaths, question_id: str) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        row = conn.execute(
            "SELECT * FROM open_questions WHERE id = ?", (question_id,)
        ).fetchone()
    if not row:
        raise ValueError(f"open question not found: {question_id}")
    return row_to_question(row)


def row_to_fact(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "statement": row["statement"],
        "entity_key": row["entity_key"],
        "page_hint": row["page_hint"],
        "section_hint": row["section_hint"],
        "source_ids": loads(row["source_ids"], []),
        "observed_at": row["observed_at"],
        "confidence": row["confidence"],
        "status": row["status"],
        "supersedes_id": row["supersedes_id"],
        "conflict_group_id": row["conflict_group_id"],
        "confirmed_by_user": bool(row["confirmed_by_user"]),
        "metadata": loads(row["metadata"], {}),
        "created_at": row["created_at"],
        "last_seen_at": row["last_seen_at"],
    }


def row_to_question(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "entity_key": row["entity_key"],
        "page_hint": row["page_hint"],
        "fact_ids": loads(row["fact_ids"], []),
        "question": row["question"],
        "options": loads(row["options"], []),
        "status": row["status"],
        "answer": loads(row["answer"], None),
        "context": loads(row["context"], {}),
        "created_at": row["created_at"],
        "answered_at": row["answered_at"],
    }


def row_to_curation_run(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "source_packet_id": row["source_packet_id"],
        "group_by": row["group_by"],
        "status": row["status"],
        "summary": loads(row["summary"], {}),
        "created_at": row["created_at"],
    }


def fact_status_summary(facts: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(str(fact.get("status") or "") for fact in facts))
