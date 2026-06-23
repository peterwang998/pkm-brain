from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connection, dumps, loads, rows
from .llm import get_provider
from .paths import BrainPaths
from .title_utils import bounded_document_title
from .util import new_id, now_iso
from .wiki import lint_wiki


ABSORBED_BY_FACTS_STATUS = "absorbed_by_facts"
PENDING_ITEM_STATUS = "pending"
VALID_BATCH_STATUSES = {
    "proposed",
    "needs_interview",
    "approved",
    "rejected",
    "applied",
    "superseded",
    "failed",
    ABSORBED_BY_FACTS_STATUS,
}
VALID_ITEM_STATUSES = {PENDING_ITEM_STATUS, ABSORBED_BY_FACTS_STATUS}
VALID_ITEM_OPERATIONS = {
    "replace_section",
    "append_section",
    "create_page",
    "replace_page",
}
PENDING_REVIEW_STATUSES = {"proposed", "needs_interview", "approved"}
PACKET_BRIEF_MAX_PAGES = 80
PACKET_BRIEF_MAX_ITEMS_PER_PAGE = 12
PACKET_BRIEF_MARKDOWN_PREVIEW_CHARS = 1800
PACKET_BRIEF_CURRENT_PAGE_CHARS = 2500


@dataclass(frozen=True)
class WikiChange:
    target_path: str
    operation: str
    proposed_markdown: str
    rationale: str
    section_name: str | None = None
    source_ids: list[str] | None = None
    confidence: float = 0.8


def create_wiki_proposal(
    paths: BrainPaths,
    title: str,
    rationale: str,
    source_ids: list[str],
    changes: list[dict[str, Any] | WikiChange],
    confidence: float,
    author: str = "agent",
    source: str = "mcp",
    status: str = "proposed",
) -> str:
    if status not in VALID_BATCH_STATUSES:
        raise ValueError(f"invalid proposal status: {status}")
    normalized = [normalize_change(change) for change in changes]
    if not normalized:
        raise ValueError("proposal requires at least one change item")
    timestamp = now_iso()
    batch_id = new_id("wikibatch")
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO wiki_change_batches(
              id, title, rationale, author, source, status, confidence, source_ids, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                title,
                rationale,
                author,
                source,
                status,
                confidence,
                dumps(source_ids),
                timestamp,
            ),
        )
        for index, change in enumerate(normalized):
            validate_target_path(change.target_path)
            if change.operation not in VALID_ITEM_OPERATIONS:
                raise ValueError(f"invalid item operation: {change.operation}")
            conn.execute(
                """
                INSERT INTO wiki_change_items(
                  id, batch_id, order_index, target_path, operation, section_name,
                  proposed_markdown, rationale, source_ids, confidence, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("wikiitem"),
                    batch_id,
                    index,
                    change.target_path,
                    change.operation,
                    change.section_name,
                    change.proposed_markdown,
                    change.rationale,
                    dumps(change.source_ids or source_ids),
                    change.confidence,
                    PENDING_ITEM_STATUS,
                ),
            )
    return batch_id


def normalize_change(change: dict[str, Any] | WikiChange) -> WikiChange:
    if isinstance(change, WikiChange):
        return change
    return WikiChange(
        target_path=str(change["target_path"]),
        operation=str(change.get("operation") or "replace_section"),
        section_name=change.get("section_name"),
        proposed_markdown=str(change["proposed_markdown"]),
        rationale=str(change.get("rationale") or ""),
        source_ids=list(change.get("source_ids") or []),
        confidence=float(change.get("confidence", 0.8)),
    )


def validate_target_path(target_path: str) -> None:
    path = Path(target_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"target_path must be relative to wiki root: {target_path}")
    if path.suffix != ".md":
        raise ValueError(f"target_path must point to a Markdown file: {target_path}")


def list_wiki_proposals(
    paths: BrainPaths, status: str | None = None
) -> list[dict[str, Any]]:
    query = "SELECT * FROM wiki_change_batches"
    params: list[str] = []
    if status:
        query += " WHERE status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC"
    with connection(paths.sqlite_path) as conn:
        found = [row_to_batch(row) for row in conn.execute(query, params)]
    return found


def list_wiki_review_packets(
    paths: BrainPaths,
    statuses: set[str] | None = None,
    group_by: str = "topic",
) -> dict[str, Any]:
    """Return pending wiki proposals grouped for human backlog review.

    The packet layer is intentionally read-only: it reframes many proposal rows
    into topic/time/priority groups while leaving approval and apply semantics
    on the underlying proposal batches unchanged.
    """
    selected_statuses = set(statuses or PENDING_REVIEW_STATUSES)
    invalid_statuses = selected_statuses - VALID_BATCH_STATUSES
    if invalid_statuses:
        raise ValueError(
            f"invalid proposal statuses: {', '.join(sorted(invalid_statuses))}"
        )
    if group_by not in {"topic", "day", "priority"}:
        raise ValueError("group_by must be topic, day, or priority")
    if not selected_statuses:
        return empty_review_packets(group_by, selected_statuses)
    changes = pending_review_changes(paths, selected_statuses)
    page_groups = build_review_page_groups(paths, changes)
    packets = build_review_packets(page_groups, group_by)
    totals = review_packet_totals(page_groups, selected_statuses, group_by)
    totals["packet_count"] = len(packets)
    return {
        "group_by": group_by,
        "statuses": sorted(selected_statuses),
        "packets": packets,
        "count": len(packets),
        "totals": totals,
    }


def generate_wiki_review_packet_brief(
    paths: BrainPaths,
    packet_id: str,
    group_by: str = "topic",
    provider_name: str | None = None,
    answers: list[dict[str, Any]] | None = None,
    statuses: set[str] | None = None,
) -> dict[str, Any]:
    context = build_wiki_review_packet_context(
        paths, packet_id, group_by=group_by, statuses=statuses
    )
    try:
        provider = get_provider(provider_name)
        parsed = parse_json_object(
            provider.complete(wiki_review_packet_brief_prompt(context, answers or []))
        )
        output = normalize_wiki_review_packet_brief(parsed, context)
        output["provider"] = provider.name
        output["model"] = provider.model
        output["error"] = None
        return output
    except Exception as exc:
        output = fallback_wiki_review_packet_brief(context)
        output["provider"] = None
        output["model"] = None
        output["error"] = str(exc)
        return output


def build_wiki_review_packet_context(
    paths: BrainPaths,
    packet_id: str,
    group_by: str = "topic",
    statuses: set[str] | None = None,
    max_pages: int | None = PACKET_BRIEF_MAX_PAGES,
) -> dict[str, Any]:
    selected_statuses = set(statuses or PENDING_REVIEW_STATUSES)
    packet_data = list_wiki_review_packets(
        paths, statuses=selected_statuses, group_by=group_by
    )
    packet = next(
        (item for item in packet_data["packets"] if item["id"] == packet_id), None
    )
    if packet is None:
        raise ValueError(f"wiki review packet not found: {packet_id}")
    target_paths = {str(page["target_path"]) for page in packet.get("pages", [])}
    changes_by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for change in pending_review_changes(paths, selected_statuses):
        if str(change.get("target_path")) in target_paths:
            changes_by_target[str(change["target_path"])].append(change)
    pages = sorted(packet.get("pages", []), key=llm_review_page_sort_key)
    selected_pages = pages if max_pages is None else pages[:max_pages]
    return {
        "packet": packet_context_summary(packet),
        "totals": packet_data["totals"],
        "pages": [
            packet_page_context(
                paths, page, changes_by_target.get(str(page["target_path"]), [])
            )
            for page in selected_pages
        ],
        "page_count": len(pages),
        "included_page_count": len(selected_pages),
        "truncated": len(selected_pages) < len(pages),
        "aggregation_rules": {
            "topic_specific": "Aggregate within the selected packet/topic and group target pages by semantic theme and timeline.",
            "conflict_order": "Review conflicts first, then stacked revision chains, then clean single changes.",
            "recency_rule": "Prefer a newer proposal only when it is high-confidence and source-backed; otherwise ask the human reviewer.",
            "draft_rule": "Drafts are review artifacts only. They must not be treated as approved or applied.",
        },
    }


def packet_context_summary(packet: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": packet.get("id"),
        "label": packet.get("label"),
        "group_by": packet.get("group_by"),
        "target_count": packet.get("target_count"),
        "batch_count": packet.get("batch_count"),
        "item_count": packet.get("item_count"),
        "simple_page_count": packet.get("simple_page_count"),
        "stacked_page_count": packet.get("stacked_page_count"),
        "conflict_page_count": packet.get("conflict_page_count"),
        "first_created_at": packet.get("first_created_at"),
        "last_created_at": packet.get("last_created_at"),
        "operation_counts": packet.get("operation_counts") or {},
        "status_counts": packet.get("status_counts") or {},
        "review_hint": packet.get("review_hint"),
    }


def packet_page_context(
    paths: BrainPaths, page: dict[str, Any], changes: list[dict[str, Any]]
) -> dict[str, Any]:
    ordered = sorted(
        changes,
        key=lambda change: str(change.get("batch_created_at") or ""),
        reverse=True,
    )
    source_ids = stable_unique(
        source_id for change in ordered for source_id in change.get("source_ids") or []
    )
    target = paths.wiki / str(page.get("target_path") or "")
    current_markdown = (
        target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
    )
    return {
        "target_path": page.get("target_path"),
        "topic": page.get("topic"),
        "complexity": page.get("complexity"),
        "conflicts": page.get("conflicts") or [],
        "resolution_hint": page.get("resolution_hint"),
        "target_exists": page.get("target_exists"),
        "batch_count": page.get("batch_count"),
        "item_count": page.get("item_count"),
        "operation_counts": page.get("operation_counts") or {},
        "first_created_at": page.get("first_created_at"),
        "last_created_at": page.get("last_created_at"),
        "latest_batch_id": page.get("latest_batch_id"),
        "latest_title": page.get("latest_title"),
        "latest_confidence": page.get("latest_confidence"),
        "latest_preview": page.get("latest_preview"),
        "operation_groups": page.get("operation_groups") or [],
        "source_documents": source_document_context(paths, source_ids),
        "current_markdown_excerpt": truncate_text(
            current_markdown, PACKET_BRIEF_CURRENT_PAGE_CHARS
        ),
        "proposal_items": [
            packet_change_context(change)
            for change in ordered[:PACKET_BRIEF_MAX_ITEMS_PER_PAGE]
        ],
        "proposal_item_count": len(ordered),
        "proposal_items_truncated": len(ordered) > PACKET_BRIEF_MAX_ITEMS_PER_PAGE,
    }


def packet_change_context(change: dict[str, Any]) -> dict[str, Any]:
    return {
        "batch_id": change.get("batch_id"),
        "batch_title": change.get("batch_title"),
        "batch_status": change.get("batch_status"),
        "batch_confidence": change.get("batch_confidence"),
        "batch_created_at": change.get("batch_created_at"),
        "operation": change.get("operation"),
        "section_name": change.get("section_name"),
        "item_confidence": change.get("item_confidence"),
        "rationale": compact_text(
            change.get("item_rationale") or change.get("batch_rationale"), 500
        ),
        "source_ids": change.get("source_ids") or [],
        "proposed_markdown_excerpt": truncate_text(
            change.get("proposed_markdown") or "", PACKET_BRIEF_MARKDOWN_PREVIEW_CHARS
        ),
    }


def source_document_context(
    paths: BrainPaths, source_ids: list[str]
) -> list[dict[str, Any]]:
    document_ids = []
    for source_id in source_ids:
        value = str(source_id)
        if value.startswith("document:"):
            document_ids.append(value.removeprefix("document:"))
    if not document_ids:
        return []
    placeholders = ",".join("?" for _ in document_ids)
    with connection(paths.sqlite_path) as conn:
        found = rows(
            conn,
            f"""
            SELECT id, title, source_type, source_path, ingested_at
            FROM documents
            WHERE id IN ({placeholders})
            """,
            document_ids,
        )
    by_id = {str(row["id"]): row for row in found}
    documents = []
    for document_id in document_ids:
        row = by_id.get(document_id)
        if not row:
            continue
        documents.append(
            {
                "source_id": f"document:{row['id']}",
                "title": bounded_document_title(
                    str(row["title"] or ""), str(row["id"])
                ),
                "source_type": row["source_type"],
                "source_path": row["source_path"],
                "ingested_at": row["ingested_at"],
            }
        )
    return documents


def normalize_wiki_review_packet_brief(
    parsed: dict[str, Any], context: dict[str, Any]
) -> dict[str, Any]:
    return {
        "packet": context["packet"],
        "context": {
            "page_count": context["page_count"],
            "included_page_count": context["included_page_count"],
            "truncated": context["truncated"],
        },
        "summary": string_list_like(parsed.get("summary")),
        "aggregation_strategy": str(parsed.get("aggregation_strategy") or ""),
        "priority_targets": object_list(parsed.get("priority_targets")),
        "conflicts": object_list(parsed.get("conflicts")),
        "questions": normalize_questions(parsed.get("questions")),
        "consolidated_drafts": normalize_consolidated_drafts(
            parsed.get("consolidated_drafts")
        ),
        "defer_or_reject": object_list(parsed.get("defer_or_reject")),
        "raw": parsed,
    }


def fallback_wiki_review_packet_brief(context: dict[str, Any]) -> dict[str, Any]:
    questions = []
    drafts = []
    priority_targets = []
    conflicts = []
    for page in context["pages"]:
        target_path = str(page.get("target_path") or "")
        priority_targets.append(
            {
                "target_path": target_path,
                "priority": page.get("complexity"),
                "reason": page.get("resolution_hint"),
                "latest_confidence": page.get("latest_confidence"),
                "latest_created_at": page.get("last_created_at"),
            }
        )
        for conflict in page.get("conflicts") or []:
            conflicts.append(
                {
                    "target_path": target_path,
                    "issue": conflict,
                    "reason": page.get("resolution_hint"),
                }
            )
        if page.get("complexity") in {"conflict", "stacked"}:
            questions.append(
                {
                    "target_path": target_path,
                    "question": f"How should the pending changes for {target_path} be resolved?",
                    "why": page.get("resolution_hint")
                    or "The page has multiple pending changes.",
                    "blocking": True,
                }
            )
        draft = fallback_draft_for_page(page)
        if draft:
            drafts.append(draft)
    return {
        "packet": context["packet"],
        "context": {
            "page_count": context["page_count"],
            "included_page_count": context["included_page_count"],
            "truncated": context["truncated"],
        },
        "summary": [
            "Provider unavailable or returned invalid JSON; showing deterministic conflict-first aggregation.",
            "Drafts use the latest high-confidence proposal item or additive append chain where available.",
        ],
        "aggregation_strategy": "Conflict-first deterministic fallback.",
        "priority_targets": priority_targets,
        "conflicts": conflicts,
        "questions": questions,
        "consolidated_drafts": drafts,
        "defer_or_reject": [],
        "raw": {},
    }


def fallback_draft_for_page(page: dict[str, Any]) -> dict[str, Any] | None:
    items = list(page.get("proposal_items") or [])
    if not items:
        return None
    append_items = [
        item for item in reversed(items) if item.get("operation") == "append_section"
    ]
    if append_items:
        section_name = append_items[0].get("section_name")
        source_ids = stable_unique(
            source_id
            for item in append_items
            for source_id in item.get("source_ids") or []
        )
        return {
            "target_path": page.get("target_path"),
            "operation": "append_section",
            "section_name": section_name,
            "proposed_markdown": "\n\n".join(
                str(item.get("proposed_markdown_excerpt") or "").strip()
                for item in append_items
                if item.get("proposed_markdown_excerpt")
            ),
            "rationale": "Fallback additive chain assembled from pending append_section items.",
            "source_ids": source_ids,
            "source_batch_ids": stable_unique(
                item.get("batch_id") for item in append_items
            ),
            "confidence": min(
                float(
                    item.get("item_confidence") or item.get("batch_confidence") or 0.7
                )
                for item in append_items
            ),
            "review_notes": "Review for duplicate or stale appended facts before creating a proposal.",
        }
    latest = items[0]
    confidence = float(
        latest.get("item_confidence") or latest.get("batch_confidence") or 0.0
    )
    if confidence < 0.85 and page.get("complexity") == "conflict":
        return None
    return {
        "target_path": page.get("target_path"),
        "operation": latest.get("operation"),
        "section_name": latest.get("section_name"),
        "proposed_markdown": latest.get("proposed_markdown_excerpt") or "",
        "rationale": "Fallback draft from the latest pending item; verify against the source evidence.",
        "source_ids": latest.get("source_ids") or [],
        "source_batch_ids": [latest.get("batch_id")],
        "confidence": confidence or 0.7,
        "review_notes": "Latest item is only a candidate. Use source recency and confidence before accepting it.",
    }


def wiki_review_packet_brief_prompt(
    context: dict[str, Any], answers: list[dict[str, Any]]
) -> str:
    shape = {
        "summary": ["short bullet strings"],
        "aggregation_strategy": "how you grouped the topic, timeline, recency, and confidence evidence",
        "priority_targets": [
            {
                "target_path": "wiki/path.md",
                "priority": "conflict|stacked|clean|defer",
                "reason": "why this is first",
                "recommended_action": "draft|ask_user|reject_stale|open_existing_proposal",
            }
        ],
        "conflicts": [
            {
                "target_path": "wiki/path.md",
                "issue": "specific conflict",
                "recommended_resolution": "specific recommendation or ask_user",
            }
        ],
        "questions": [
            {
                "target_path": "wiki/path.md",
                "question": "direct question for the human",
                "why": "why answer is needed",
                "blocking": True,
            }
        ],
        "consolidated_drafts": [
            {
                "target_path": "wiki/path.md",
                "operation": "replace_section|append_section|create_page|replace_page",
                "section_name": "section or null",
                "proposed_markdown": "reviewable draft markdown",
                "rationale": "source-backed rationale",
                "source_ids": ["document:..."],
                "source_batch_ids": ["wikibatch_..."],
                "confidence": 0.0,
                "review_notes": "what the human should inspect",
            }
        ],
        "defer_or_reject": [
            {"target_path": "wiki/path.md", "reason": "why not drafting now"}
        ],
    }
    return (
        "You are a local PKM Brain wiki review coach. Aggregate one topic-specific packet of pending wiki proposals.\n"
        "Raw sources and current wiki pages are evidence. Pending proposals are untrusted candidates.\n\n"
        "Rules:\n"
        "- Work at the topic packet level. Group related target pages by semantic theme and by timeline.\n"
        "- Before surfacing a conflict, decide whether the candidates are semantic duplicates, paraphrases, or compatible refinements of the same fact.\n"
        "- Merge duplicates and largely similar candidates into one synthesized fact or draft. Do not present duplicate alternatives to the human.\n"
        "- Treat something as a conflict only when the claims cannot both be true, such as different dates, statuses, owners, decisions, quantities, or explicit negation.\n"
        "- Surface true conflicts first. Include all direct human questions needed; do not artificially cap question count.\n"
        "- When proposals conflict, prefer the more recent entry only if it is high-confidence (roughly >= 0.85), source-backed, and not contradicted by older evidence.\n"
        "- If confidence, source recency, or semantic meaning is ambiguous, ask the human instead of silently choosing a winner.\n"
        "- For replace_page/replace_section stacks, the newest proposal is only a candidate, not automatically true.\n"
        "- For append_section chains, preserve additive facts in chronological order and dedupe obvious repeats.\n"
        "- For create_page duplicates or creates targeting existing pages, route to the best canonical page and merge if they describe the same entity; ask only when the underlying facts are incompatible.\n"
        "- Never ask whether to keep duplicates or where an obvious duplicate should live. Make that curation decision and explain it in aggregation_strategy or review_notes.\n"
        "- Consolidated drafts must be reviewable in the browser and source-backed. Do not claim approval or apply anything.\n"
        "- Do not invent source IDs. Use source_ids and source_batch_ids from the input only.\n"
        "- Keep markdown concise but complete enough for review.\n\n"
        f"Return only valid JSON with this shape:\n{json.dumps(shape, indent=2)}\n\n"
        f"Human answers from a previous pass, if any:\n{json.dumps(answers, indent=2)}\n\n"
        f"Packet context:\n{json.dumps(context, indent=2)}"
    )


def pending_review_changes(
    paths: BrainPaths, statuses: set[str]
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in statuses)
    with connection(paths.sqlite_path) as conn:
        found = rows(
            conn,
            f"""
            SELECT b.id AS batch_id,
                   b.title AS batch_title,
                   b.rationale AS batch_rationale,
                   b.author AS batch_author,
                   b.source AS batch_source,
                   b.status AS batch_status,
                   b.confidence AS batch_confidence,
                   b.source_ids AS batch_source_ids,
                   b.created_at AS batch_created_at,
                   b.reviewed_at AS batch_reviewed_at,
                   b.applied_at AS batch_applied_at,
                   b.error AS batch_error,
                   i.id AS item_id,
                   i.order_index AS item_order_index,
                   i.target_path AS target_path,
                   i.operation AS operation,
                   i.section_name AS section_name,
                   i.proposed_markdown AS proposed_markdown,
                   i.rationale AS item_rationale,
                   i.source_ids AS item_source_ids,
                   i.confidence AS item_confidence,
                   i.status AS item_status
            FROM wiki_change_items i
            JOIN wiki_change_batches b ON b.id = i.batch_id
            WHERE b.status IN ({placeholders}) AND i.status = ?
            ORDER BY b.created_at DESC, i.order_index ASC
            """,
            (*tuple(sorted(statuses)), PENDING_ITEM_STATUS),
        )
    return [review_change_from_row(row) for row in found]


def wiki_proposal_pending_item_counts(
    paths: BrainPaths,
    statuses: set[str] | None = None,
) -> dict[str, int]:
    selected_statuses = set(statuses or PENDING_REVIEW_STATUSES)
    invalid_statuses = selected_statuses - VALID_BATCH_STATUSES
    if invalid_statuses:
        raise ValueError(
            f"invalid proposal statuses: {', '.join(sorted(invalid_statuses))}"
        )
    if not selected_statuses:
        return {}
    placeholders = ",".join("?" for _ in selected_statuses)
    with connection(paths.sqlite_path) as conn:
        found = rows(
            conn,
            f"""
            SELECT b.id AS batch_id, COUNT(i.id) AS pending_item_count
            FROM wiki_change_batches b
            JOIN wiki_change_items i ON i.batch_id = b.id
            WHERE b.status IN ({placeholders}) AND i.status = ?
            GROUP BY b.id
            """,
            (*tuple(sorted(selected_statuses)), PENDING_ITEM_STATUS),
        )
    return {str(row["batch_id"]): int(row["pending_item_count"]) for row in found}


def mark_wiki_change_items_absorbed_by_facts(
    paths: BrainPaths,
    item_ids: list[str],
    *,
    curation_run_id: str,
    packet_id: str,
    group_by: str,
) -> dict[str, Any]:
    unique_item_ids = stable_unique(
        str(item_id) for item_id in item_ids if str(item_id or "").strip()
    )
    if not unique_item_ids:
        return {
            "requested_item_count": 0,
            "updated_item_ids": [],
            "already_absorbed_item_ids": [],
            "missing_item_ids": [],
            "fully_absorbed_batch_ids": [],
            "partially_absorbed_batch_ids": [],
        }
    placeholders = ",".join("?" for _ in unique_item_ids)
    timestamp = now_iso()
    reason = (
        f"absorbed_by_facts curation_run={curation_run_id} "
        f"packet={packet_id} group_by={group_by}"
    )
    with connection(paths.sqlite_path) as conn:
        found = rows(
            conn,
            f"""
            SELECT id, batch_id, status
            FROM wiki_change_items
            WHERE id IN ({placeholders})
            """,
            tuple(unique_item_ids),
        )
        found_ids = {str(row["id"]) for row in found}
        missing_item_ids = [
            item_id for item_id in unique_item_ids if item_id not in found_ids
        ]
        pending_item_ids = [
            str(row["id"]) for row in found if row["status"] == PENDING_ITEM_STATUS
        ]
        already_absorbed_item_ids = [
            str(row["id"]) for row in found if row["status"] == ABSORBED_BY_FACTS_STATUS
        ]
        if pending_item_ids:
            update_placeholders = ",".join("?" for _ in pending_item_ids)
            conn.execute(
                f"""
                UPDATE wiki_change_items
                SET status = ?
                WHERE id IN ({update_placeholders}) AND status = ?
                """,
                (ABSORBED_BY_FACTS_STATUS, *pending_item_ids, PENDING_ITEM_STATUS),
            )
        batch_ids = stable_unique(str(row["batch_id"]) for row in found)
        fully_absorbed_batch_ids: list[str] = []
        partially_absorbed_batch_ids: list[str] = []
        pending_status_placeholders = ",".join("?" for _ in PENDING_REVIEW_STATUSES)
        for batch_id in batch_ids:
            counts = conn.execute(
                """
                SELECT
                  COUNT(*) AS total_count,
                  SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS pending_count
                FROM wiki_change_items
                WHERE batch_id = ?
                """,
                (PENDING_ITEM_STATUS, batch_id),
            ).fetchone()
            total_count = int(counts["total_count"] or 0)
            pending_count = int(counts["pending_count"] or 0)
            if total_count and pending_count == 0:
                conn.execute(
                    f"""
                    UPDATE wiki_change_batches
                    SET status = ?, reviewed_at = ?, error = ?
                    WHERE id = ? AND status IN ({pending_status_placeholders})
                    """,
                    (
                        ABSORBED_BY_FACTS_STATUS,
                        timestamp,
                        reason,
                        batch_id,
                        *tuple(sorted(PENDING_REVIEW_STATUSES)),
                    ),
                )
                fully_absorbed_batch_ids.append(batch_id)
            elif total_count:
                partially_absorbed_batch_ids.append(batch_id)
    return {
        "requested_item_count": len(unique_item_ids),
        "updated_item_ids": pending_item_ids,
        "already_absorbed_item_ids": already_absorbed_item_ids,
        "missing_item_ids": missing_item_ids,
        "fully_absorbed_batch_ids": fully_absorbed_batch_ids,
        "partially_absorbed_batch_ids": partially_absorbed_batch_ids,
    }


def review_change_from_row(row: Any) -> dict[str, Any]:
    item_source_ids = loads(row["item_source_ids"], [])
    batch_source_ids = loads(row["batch_source_ids"], [])
    return {
        "batch_id": row["batch_id"],
        "batch_title": row["batch_title"],
        "batch_rationale": row["batch_rationale"],
        "batch_author": row["batch_author"],
        "batch_source": row["batch_source"],
        "batch_status": row["batch_status"],
        "batch_confidence": row["batch_confidence"],
        "batch_source_ids": batch_source_ids,
        "batch_created_at": row["batch_created_at"],
        "batch_reviewed_at": row["batch_reviewed_at"],
        "batch_applied_at": row["batch_applied_at"],
        "batch_error": row["batch_error"],
        "item_id": row["item_id"],
        "item_order_index": row["item_order_index"],
        "target_path": row["target_path"],
        "operation": row["operation"],
        "section_name": row["section_name"],
        "proposed_markdown": row["proposed_markdown"],
        "item_rationale": row["item_rationale"],
        "item_source_ids": item_source_ids,
        "item_confidence": row["item_confidence"],
        "item_status": row["item_status"],
        "source_ids": stable_unique([*batch_source_ids, *item_source_ids]),
    }


def build_review_page_groups(
    paths: BrainPaths, changes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for change in changes:
        grouped[str(change["target_path"])].append(change)
    page_groups = [
        review_page_group(paths, target_path, items)
        for target_path, items in grouped.items()
    ]
    page_groups.sort(key=review_page_sort_key)
    return page_groups


def review_page_group(
    paths: BrainPaths, target_path: str, changes: list[dict[str, Any]]
) -> dict[str, Any]:
    ordered = sorted(
        changes, key=lambda change: str(change.get("batch_created_at") or "")
    )
    reverse_ordered = list(reversed(ordered))
    source_ids = stable_unique(
        source_id for change in ordered for source_id in change.get("source_ids") or []
    )
    batch_ids = stable_unique(change["batch_id"] for change in ordered)
    operations = Counter(str(change["operation"]) for change in ordered)
    statuses = Counter(str(change["batch_status"]) for change in ordered)
    target_exists = (paths.wiki / target_path).exists()
    operation_groups = review_operation_groups(ordered, target_exists)
    conflicts = review_conflicts(ordered, operation_groups, target_exists)
    complexity = (
        "conflict"
        if conflicts
        else "stacked"
        if len(batch_ids) > 1 or len(ordered) > 1
        else "simple"
    )
    latest = reverse_ordered[0] if reverse_ordered else {}
    proposals = review_proposal_summaries(reverse_ordered)
    first_created_at = ordered[0].get("batch_created_at") if ordered else None
    last_created_at = latest.get("batch_created_at") if latest else None
    return {
        "target_path": target_path,
        "topic": topic_for_target(target_path),
        "target_exists": target_exists,
        "batch_count": len(batch_ids),
        "item_count": len(ordered),
        "source_count": len(source_ids),
        "source_ids": source_ids,
        "operation_counts": dict(sorted(operations.items())),
        "status_counts": dict(sorted(statuses.items())),
        "first_created_at": first_created_at,
        "last_created_at": last_created_at,
        "complexity": complexity,
        "conflicts": conflicts,
        "resolution_hint": review_resolution_hint(operation_groups, conflicts),
        "latest_batch_id": latest.get("batch_id"),
        "latest_title": latest.get("batch_title"),
        "latest_status": latest.get("batch_status"),
        "latest_confidence": latest.get("batch_confidence"),
        "latest_preview": compact_text(
            latest.get("batch_rationale") or latest.get("item_rationale")
        ),
        "operation_groups": operation_groups,
        "proposals": proposals,
    }


def review_operation_groups(
    changes: list[dict[str, Any]], target_exists: bool
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for change in changes:
        grouped[
            (str(change["operation"]), str(change.get("section_name") or ""))
        ].append(change)
    output = []
    for (operation, section_name), items in sorted(
        grouped.items(), key=lambda entry: (entry[0][0], entry[0][1])
    ):
        ordered = sorted(
            items, key=lambda change: str(change.get("batch_created_at") or "")
        )
        latest = ordered[-1]
        duplicate_append_count = 0
        if operation == "append_section":
            proposed_counts = Counter(
                str(change.get("proposed_markdown") or "") for change in ordered
            )
            duplicate_append_count = sum(
                count - 1 for count in proposed_counts.values() if count > 1
            )
        output.append(
            {
                "operation": operation,
                "section_name": section_name or None,
                "item_count": len(ordered),
                "batch_count": len(set(str(change["batch_id"]) for change in ordered)),
                "old_revision_count": max(0, len(ordered) - 1)
                if operation in {"replace_page", "replace_section", "create_page"}
                else 0,
                "duplicate_append_count": duplicate_append_count,
                "latest_batch_id": latest.get("batch_id"),
                "latest_title": latest.get("batch_title"),
                "latest_created_at": latest.get("batch_created_at"),
                "resolution": operation_resolution(
                    operation, len(ordered), target_exists, duplicate_append_count
                ),
            }
        )
    return output


def operation_resolution(
    operation: str, count: int, target_exists: bool, duplicate_append_count: int
) -> str:
    if operation == "append_section":
        if duplicate_append_count:
            return f"Additive chain; {duplicate_append_count} exact duplicate append item(s) should be reviewed once."
        return "Additive chain; review items in time order."
    if operation in {"replace_page", "replace_section"}:
        if count > 1:
            return f"Replacement stack; latest candidate is the live diff and {count - 1} older revision(s) are context."
        return "Single replacement candidate."
    if operation == "create_page":
        if target_exists:
            return "Create candidate targets a page that already exists; review as stale or convert to an edit."
        if count > 1:
            return f"Multiple create candidates; choose one final page proposal and treat {count - 1} older candidate(s) as context."
        return "New page candidate."
    return "Unknown operation; inspect manually."


def review_conflicts(
    changes: list[dict[str, Any]],
    operation_groups: list[dict[str, Any]],
    target_exists: bool,
) -> list[str]:
    operations = {str(change["operation"]) for change in changes}
    conflicts: list[str] = []
    create_count = sum(1 for change in changes if change["operation"] == "create_page")
    if target_exists and create_count:
        conflicts.append("create_page targets an existing wiki page")
    if not target_exists and any(
        change["operation"] != "create_page" for change in changes
    ):
        conflicts.append("non-create edit targets a missing wiki page")
    if create_count > 1:
        conflicts.append("multiple create_page candidates target the same path")
    if "replace_page" in operations and (operations - {"replace_page"}):
        conflicts.append("whole-page replacement is mixed with section-level edits")
    return conflicts


def review_resolution_hint(
    operation_groups: list[dict[str, Any]], conflicts: list[str]
) -> str:
    if conflicts:
        return "Resolve conflicts before applying; use the latest replacement only as a candidate, not an automatic winner."
    if any(
        group["operation"] == "append_section" and int(group["item_count"]) > 1
        for group in operation_groups
    ):
        return "Review additive appends as one chronological chain and dedupe repeats."
    if any(
        group["operation"] in {"replace_page", "replace_section"}
        and int(group["old_revision_count"])
        for group in operation_groups
    ):
        return "Review the latest replacement as the live candidate; older revisions are collapsed context."
    if any(group["operation"] == "create_page" for group in operation_groups):
        return "Review as a new page candidate."
    return "Single clean proposal."


def review_proposal_summaries(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_batch: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for change in changes:
        by_batch[str(change["batch_id"])].append(change)
    proposals = []
    for batch_id, items in by_batch.items():
        first_item = items[0]
        operations = Counter(str(item["operation"]) for item in items)
        source_ids = stable_unique(
            source_id for item in items for source_id in item.get("source_ids") or []
        )
        proposals.append(
            {
                "id": batch_id,
                "title": first_item.get("batch_title") or batch_id,
                "status": first_item.get("batch_status"),
                "confidence": first_item.get("batch_confidence"),
                "created_at": first_item.get("batch_created_at"),
                "item_count": len(items),
                "operation_counts": dict(sorted(operations.items())),
                "source_count": len(source_ids),
                "preview": compact_text(first_item.get("batch_rationale")),
            }
        )
    proposals.sort(
        key=lambda proposal: str(proposal.get("created_at") or ""), reverse=True
    )
    return proposals


def build_review_packets(
    page_groups: list[dict[str, Any]], group_by: str
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for page_group in page_groups:
        key = packet_key(page_group, group_by)
        grouped[key].append(page_group)
    packets = [review_packet(key, pages, group_by) for key, pages in grouped.items()]
    packets.sort(key=review_packet_sort_key)
    return packets


def review_packet(
    key: str, page_groups: list[dict[str, Any]], group_by: str
) -> dict[str, Any]:
    batch_ids = stable_unique(
        proposal["id"] for page in page_groups for proposal in page.get("proposals", [])
    )
    source_ids = stable_unique(
        source_id for page in page_groups for source_id in page.get("source_ids") or []
    )
    operations = Counter(
        operation
        for page in page_groups
        for operation, count in page.get("operation_counts", {}).items()
        for _ in range(int(count))
    )
    statuses = Counter(
        status
        for page in page_groups
        for status, count in page.get("status_counts", {}).items()
        for _ in range(int(count))
    )
    created_dates = sorted(
        str(page.get("last_created_at") or "")
        for page in page_groups
        if page.get("last_created_at")
    )
    conflict_pages = sum(
        1 for page in page_groups if page.get("complexity") == "conflict"
    )
    stacked_pages = sum(
        1 for page in page_groups if page.get("complexity") == "stacked"
    )
    simple_pages = sum(1 for page in page_groups if page.get("complexity") == "simple")
    return {
        "id": key,
        "label": packet_label(key, group_by),
        "group_by": group_by,
        "target_count": len(page_groups),
        "batch_count": len(batch_ids),
        "item_count": sum(int(page.get("item_count") or 0) for page in page_groups),
        "source_count": len(source_ids),
        "operation_counts": dict(sorted(operations.items())),
        "status_counts": dict(sorted(statuses.items())),
        "first_created_at": created_dates[0] if created_dates else None,
        "last_created_at": created_dates[-1] if created_dates else None,
        "conflict_page_count": conflict_pages,
        "stacked_page_count": stacked_pages,
        "simple_page_count": simple_pages,
        "review_hint": packet_review_hint(simple_pages, stacked_pages, conflict_pages),
        "pages": sorted(page_groups, key=review_page_sort_key),
    }


def review_packet_totals(
    page_groups: list[dict[str, Any]], statuses: set[str], group_by: str
) -> dict[str, Any]:
    batch_ids = stable_unique(
        proposal["id"] for page in page_groups for proposal in page.get("proposals", [])
    )
    return {
        "group_by": group_by,
        "statuses": sorted(statuses),
        "packet_count": 0,
        "target_count": len(page_groups),
        "batch_count": len(batch_ids),
        "item_count": sum(int(page.get("item_count") or 0) for page in page_groups),
        "conflict_target_count": sum(
            1 for page in page_groups if page.get("complexity") == "conflict"
        ),
        "stacked_target_count": sum(
            1 for page in page_groups if page.get("complexity") == "stacked"
        ),
        "simple_target_count": sum(
            1 for page in page_groups if page.get("complexity") == "simple"
        ),
    }


def empty_review_packets(group_by: str, statuses: set[str]) -> dict[str, Any]:
    return {
        "group_by": group_by,
        "statuses": sorted(statuses),
        "packets": [],
        "count": 0,
        "totals": {
            "group_by": group_by,
            "statuses": sorted(statuses),
            "packet_count": 0,
            "target_count": 0,
            "batch_count": 0,
            "item_count": 0,
            "conflict_target_count": 0,
            "stacked_target_count": 0,
            "simple_target_count": 0,
        },
    }


def packet_key(page_group: dict[str, Any], group_by: str) -> str:
    if group_by == "day":
        return f"day:{str(page_group.get('last_created_at') or 'unknown')[:10] or 'unknown'}"
    if group_by == "priority":
        return f"priority:{page_group.get('complexity') or 'unknown'}"
    return f"topic:{page_group.get('topic') or 'other'}"


def packet_label(key: str, group_by: str) -> str:
    raw = key.split(":", 1)[1] if ":" in key else key
    if group_by == "priority":
        return {
            "simple": "Clean single changes",
            "stacked": "Stacked page changes",
            "conflict": "Needs conflict review",
        }.get(raw, raw)
    if group_by == "day":
        return raw
    return raw


def packet_review_hint(
    simple_pages: int, stacked_pages: int, conflict_pages: int
) -> str:
    if conflict_pages and not simple_pages and not stacked_pages:
        return "High-friction packet; resolve stale creates and replacement stacks deliberately."
    if simple_pages >= stacked_pages + conflict_pages:
        return "Good momentum packet; start with clean single-page decisions."
    if stacked_pages:
        return (
            "Review one target page at a time; collapsed older revisions are context."
        )
    return "Mixed packet; scan conflicts before applying anything."


def review_packet_sort_key(packet: dict[str, Any]) -> tuple[int, int, int, str]:
    if packet.get("group_by") == "priority":
        priority_rank = {
            "Clean single changes": 0,
            "Stacked page changes": 1,
            "Needs conflict review": 2,
        }
        return (
            priority_rank.get(str(packet.get("label")), 3),
            -int(packet.get("item_count") or 0),
            0,
            str(packet.get("label") or ""),
        )
    return (
        -int(packet.get("item_count") or 0),
        -int(packet.get("target_count") or 0),
        0,
        str(packet.get("label") or ""),
    )


def review_page_sort_key(page: dict[str, Any]) -> tuple[int, int, str, str]:
    complexity_rank = {"simple": 0, "stacked": 1, "conflict": 2}.get(
        str(page.get("complexity")), 3
    )
    confidence = float(page.get("latest_confidence") or 0.0)
    return (
        complexity_rank,
        -confidence,
        str(page.get("last_created_at") or ""),
        str(page.get("target_path") or ""),
    )


def topic_for_target(target_path: str) -> str:
    path = Path(target_path)
    if len(path.parts) <= 1:
        return "root"
    return path.parts[0]


def stable_unique(values: Any) -> list[Any]:
    seen = set()
    output = []
    for value in values:
        marker = json.dumps(value, sort_keys=True, default=str)
        if marker in seen:
            continue
        seen.add(marker)
        output.append(value)
    return output


def compact_text(value: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def truncate_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n...[truncated]..."


def string_list_like(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def object_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def normalize_questions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    questions = []
    for item in value:
        if isinstance(item, dict):
            question = str(item.get("question") or "").strip()
            if question:
                questions.append(
                    {
                        "target_path": str(item.get("target_path") or ""),
                        "question": question,
                        "why": str(item.get("why") or ""),
                        "blocking": bool(item.get("blocking", True)),
                    }
                )
        else:
            question = str(item).strip()
            if question:
                questions.append(
                    {
                        "target_path": "",
                        "question": question,
                        "why": "",
                        "blocking": True,
                    }
                )
    return questions


def normalize_consolidated_drafts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    drafts = []
    for item in value:
        if not isinstance(item, dict):
            continue
        operation = str(item.get("operation") or "")
        target_path = str(item.get("target_path") or "")
        proposed_markdown = str(item.get("proposed_markdown") or "")
        if (
            not target_path
            or operation not in VALID_ITEM_OPERATIONS
            or not proposed_markdown.strip()
        ):
            continue
        drafts.append(
            {
                "target_path": target_path,
                "operation": operation,
                "section_name": item.get("section_name")
                if item.get("section_name")
                else None,
                "proposed_markdown": proposed_markdown,
                "rationale": str(item.get("rationale") or ""),
                "source_ids": [
                    str(source_id) for source_id in item.get("source_ids") or []
                ],
                "source_batch_ids": [
                    str(batch_id) for batch_id in item.get("source_batch_ids") or []
                ],
                "confidence": float(item.get("confidence") or 0.0),
                "review_notes": str(item.get("review_notes") or ""),
            }
        )
    return drafts


def llm_review_page_sort_key(page: dict[str, Any]) -> tuple[int, float, int, str]:
    complexity_rank = {"conflict": 0, "stacked": 1, "simple": 2}.get(
        str(page.get("complexity")), 3
    )
    confidence = float(page.get("latest_confidence") or 0.0)
    return (
        complexity_rank,
        -confidence,
        -int(page.get("item_count") or 0),
        str(page.get("target_path") or ""),
    )


def inspect_wiki_proposal(paths: BrainPaths, batch_id: str) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        batch = conn.execute(
            "SELECT * FROM wiki_change_batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if not batch:
            raise ValueError(f"wiki proposal not found: {batch_id}")
        items = rows(
            conn,
            "SELECT * FROM wiki_change_items WHERE batch_id = ? ORDER BY order_index",
            (batch_id,),
        )
        interviews = rows(
            conn,
            "SELECT * FROM wiki_interviews WHERE batch_id = ? ORDER BY created_at",
            (batch_id,),
        )
    output = row_to_batch(batch)
    output["items"] = [row_to_item(item) for item in items]
    output["interviews"] = [row_to_interview(interview) for interview in interviews]
    return output


def reject_wiki_proposal(
    paths: BrainPaths, batch_id: str, reason: str | None = None
) -> dict[str, Any]:
    timestamp = now_iso()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE wiki_change_batches
            SET status = 'rejected', reviewed_at = ?, error = ?
            WHERE id = ?
            """,
            (timestamp, reason, batch_id),
        )
    return inspect_wiki_proposal(paths, batch_id)


def record_wiki_interview(
    paths: BrainPaths,
    batch_id: str,
    questions: list[str],
    answers: list[str],
    disposition: str,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    if disposition not in {"approved", "rejected", "needs_interview"}:
        raise ValueError("disposition must be approved, rejected, or needs_interview")
    timestamp = now_iso()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO wiki_interviews(id, batch_id, questions, answers, disposition, provider, model, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("wikiinterview"),
                batch_id,
                dumps(questions),
                dumps(answers),
                disposition,
                provider,
                model,
                timestamp,
            ),
        )
        conn.execute(
            """
            UPDATE wiki_change_batches
            SET status = ?, reviewed_at = ?
            WHERE id = ?
            """,
            (disposition, timestamp, batch_id),
        )
    return inspect_wiki_proposal(paths, batch_id)


def default_interview_questions(proposal: dict[str, Any]) -> list[str]:
    targets = ", ".join(
        sorted({item["target_path"] for item in proposal.get("items", [])})
    )
    return [
        f"Are the proposed changes to {targets} accurate and worth making durable?",
        "Is any important nuance, caveat, or source missing?",
        "Should this be approved now, rejected, or kept for another interview?",
    ]


def generate_interview_questions(
    paths: BrainPaths, batch_id: str, provider_name: str | None = None
) -> dict[str, Any]:
    proposal = inspect_wiki_proposal(paths, batch_id)
    try:
        provider = get_provider(provider_name)
        parsed = parse_json_object(provider.complete(interview_prompt(proposal)))
        questions = [
            str(item) for item in parsed.get("questions", []) if str(item).strip()
        ]
        return {
            "questions": questions or default_interview_questions(proposal),
            "provider": provider.name,
            "model": provider.model,
        }
    except Exception:
        return {
            "questions": default_interview_questions(proposal),
            "provider": None,
            "model": None,
        }


def apply_wiki_proposal(paths: BrainPaths, batch_id: str) -> dict[str, Any]:
    proposal = inspect_wiki_proposal(paths, batch_id)
    if proposal["status"] != "approved":
        raise ValueError(
            f"wiki proposal must be approved before apply; current status is {proposal['status']}"
        )
    changed_paths: list[str] = []
    originals: dict[Path, str | None] = {}
    for item in proposal["items"]:
        target = paths.wiki / item["target_path"]
        if target not in originals:
            originals[target] = (
                target.read_text(encoding="utf-8") if target.exists() else None
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        if item["operation"] == "create_page":
            if target.exists():
                raise ValueError(
                    f"target already exists for create_page: {item['target_path']}"
                )
            target.write_text(
                item["proposed_markdown"].rstrip() + "\n", encoding="utf-8"
            )
        elif item["operation"] == "replace_page":
            if not target.exists():
                raise ValueError(f"target does not exist: {item['target_path']}")
            target.write_text(
                item["proposed_markdown"].rstrip() + "\n", encoding="utf-8"
            )
        else:
            if not target.exists():
                raise ValueError(f"target does not exist: {item['target_path']}")
            text = target.read_text(encoding="utf-8")
            if item["operation"] == "replace_section":
                text = replace_section(
                    text, item["section_name"] or "", item["proposed_markdown"]
                )
            elif item["operation"] == "append_section":
                text = append_to_section(
                    text, item["section_name"] or "", item["proposed_markdown"]
                )
            else:
                raise ValueError(f"unsupported operation: {item['operation']}")
            target.write_text(text, encoding="utf-8")
        changed_paths.append(str(target))
    lint_result = lint_wiki(paths)
    if lint_result["errors"]:
        restore_originals(originals)
    timestamp = now_iso()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE wiki_change_batches
            SET status = ?, applied_at = ?, error = ?
            WHERE id = ?
            """,
            (
                "failed" if lint_result["errors"] else "applied",
                timestamp,
                "; ".join(lint_result["errors"]) or None,
                batch_id,
            ),
        )
    return {
        "batch_id": batch_id,
        "changed_paths": changed_paths,
        "lint": lint_result,
        "proposal": inspect_wiki_proposal(paths, batch_id),
    }


def restore_originals(originals: dict[Path, str | None]) -> None:
    for path, content in originals.items():
        if content is None:
            if path.exists():
                path.unlink()
        else:
            path.write_text(content, encoding="utf-8")


def replace_section(markdown: str, section_name: str, replacement: str) -> str:
    if not section_name:
        raise ValueError("replace_section requires section_name")
    pattern = re.compile(
        rf"(^##\s+{re.escape(section_name)}\s*\n)(.*?)(?=^##\s+|\Z)",
        flags=re.MULTILINE | re.DOTALL,
    )
    if pattern.search(markdown):
        return pattern.sub(
            lambda match: f"{match.group(1)}\n{replacement.strip()}\n\n",
            markdown,
            count=1,
        )
    return markdown.rstrip() + f"\n\n## {section_name}\n\n{replacement.strip()}\n"


def append_to_section(markdown: str, section_name: str, addition: str) -> str:
    if not section_name:
        raise ValueError("append_section requires section_name")
    pattern = re.compile(
        rf"(^##\s+{re.escape(section_name)}\s*\n)(.*?)(?=^##\s+|\Z)",
        flags=re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(markdown)
    if not match:
        return markdown.rstrip() + f"\n\n## {section_name}\n\n{addition.strip()}\n"
    existing = match.group(2).rstrip()
    body = f"{existing}\n\n{addition.strip()}\n\n"
    return markdown[: match.start(2)] + body + markdown[match.end(2) :]


def propose_from_sources(
    paths: BrainPaths, provider_name: str | None = None, limit: int = 8
) -> dict[str, Any]:
    provider = get_provider(provider_name)
    documents = latest_documents(paths, limit)
    if not documents:
        return {"created": False, "reason": "no source documents found"}
    prompt = proposal_prompt(documents)
    parsed = parse_json_object(provider.complete(prompt))
    changes = parsed.get("changes") or []
    if not changes:
        return {
            "created": False,
            "reason": "provider returned no changes",
            "provider": provider.name,
            "model": provider.model,
        }
    batch_id = create_wiki_proposal(
        paths,
        title=str(parsed.get("title") or "LLM wiki proposal"),
        rationale=str(
            parsed.get("rationale") or "LLM-generated proposal from recent sources."
        ),
        source_ids=list(
            parsed.get("source_ids") or [doc["source_id"] for doc in documents]
        ),
        changes=changes,
        confidence=float(parsed.get("confidence", 0.7)),
        author=f"llm:{provider.name}",
        source="nightly",
        status="needs_interview",
    )
    return {
        "created": True,
        "batch_id": batch_id,
        "provider": provider.name,
        "model": provider.model,
    }


def latest_documents(paths: BrainPaths, limit: int) -> list[dict[str, Any]]:
    with connection(paths.sqlite_path) as conn:
        found = rows(
            conn,
            """
            SELECT d.id, d.title, d.source_type, d.source_path, d.ingested_at,
                   GROUP_CONCAT(c.text, '\n\n') AS chunk_text
            FROM documents d
            LEFT JOIN chunks c ON c.document_id = d.id
            GROUP BY d.id
            ORDER BY d.ingested_at DESC
            LIMIT ?
            """,
            (limit,),
        )
    output: list[dict[str, Any]] = []
    for row in found:
        text = re.sub(r"\s+", " ", row["chunk_text"] or "").strip()
        output.append(
            {
                "source_id": f"document:{row['id']}",
                "title": bounded_document_title(row["title"], str(row["id"])),
                "source_type": row["source_type"],
                "source_path": row["source_path"],
                "ingested_at": row["ingested_at"],
                "preview": text[:1500],
            }
        )
    return output


def proposal_prompt(documents: list[dict[str, Any]]) -> str:
    return (
        "You maintain a local Markdown personal wiki. Propose source-backed wiki changes as JSON.\n"
        "Only propose durable, human-readable semantic wiki updates. Do not dump raw logs.\n"
        'Return this JSON shape exactly: {"title": str, "rationale": str, "confidence": number, '
        '"source_ids": [str], "changes": [{"target_path": str, "operation": "replace_section"|"append_section"|"create_page"|"replace_page", '
        '"section_name": str|null, "proposed_markdown": str, "rationale": str, "source_ids": [str], "confidence": number}]}.\n'
        "Target paths must be relative to wiki root, like concepts/example.md or decisions/example.md.\n\n"
        f"Sources:\n{json.dumps(documents, indent=2)}"
    )


def interview_prompt(proposal: dict[str, Any]) -> str:
    return (
        "Generate 3 concise interview questions for a human reviewer of this wiki proposal. "
        "Focus on correctness, missing nuance, and whether the proposal should be durable. "
        'Return JSON: {"questions": [str, str, str]}.\n\n'
        f"Proposal:\n{json.dumps(proposal, indent=2)}"
    )


def parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"\A```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```\Z", "", stripped)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("LLM response must be a JSON object")
    return parsed


def row_to_batch(row: Any) -> dict[str, Any]:
    output = dict(row)
    output["source_ids"] = loads(output.get("source_ids"), [])
    return output


def row_to_item(row: Any) -> dict[str, Any]:
    output = dict(row)
    output["source_ids"] = loads(output.get("source_ids"), [])
    return output


def row_to_interview(row: Any) -> dict[str, Any]:
    output = dict(row)
    output["questions"] = loads(output.get("questions"), [])
    output["answers"] = loads(output.get("answers"), [])
    return output
