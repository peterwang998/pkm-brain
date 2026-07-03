from __future__ import annotations

from difflib import SequenceMatcher, unified_diff
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

from .cos_actions import apply_action, propose_action
from .db import connection, dumps, loads, rows
from .llm import LLMProvider, complete_json, cos_role_provider_configured
from .paths import BrainPaths
from .util import new_id, now_iso, slugify, stable_unique, text_sha256
from .wiki import COMMON_SECTIONS, TYPE_SECTIONS, lint_wiki, parse_frontmatter


FACT_STATUSES = {
    "active",
    "superseded",
    "conflicted",
    "needs_confirmation",
    "retracted",
}
QUESTION_STATUSES = {
    "open",
    "answered",
    "dismissed",
    "needs_human",
    "auto_resolved",
    "timeout_resolved",
}
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
MATERIAL_NUMBER_WORDS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
}
MATERIAL_NUMBER_UNITS = {
    "day",
    "days",
    "week",
    "weeks",
    "month",
    "months",
    "quarter",
    "quarters",
    "year",
    "years",
    "hour",
    "hours",
    "minute",
    "minutes",
    "dollar",
    "dollars",
    "percent",
    "seat",
    "seats",
    "user",
    "users",
}
RESOLVER_SCHEMA = {
    "type": "object",
    "required": ["decision", "fact_ids", "rationale"],
    "properties": {
        "decision": {"type": "string"},
        "fact_ids": {"type": "array"},
        "keeper_fact_id": {"type": "string"},
        "rationale": {"type": "string"},
        "risk_tier": {"type": "string"},
    },
}


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
    for candidate in candidates:
        with connection(paths.sqlite_path) as conn:
            normalized = normalized_statement(candidate["statement"])
            existing = find_existing_fact(conn, candidate, normalized)
            existing_fact = row_to_fact(existing) if existing else None
        if existing_fact:
            merged = merge_fact_values(existing_fact, candidate, timestamp)
            fact_id = str(existing_fact["id"])
            fact = {
                **existing_fact,
                **merged,
                "id": fact_id,
                "status": "superseded"
                if existing_fact.get("status") == "superseded"
                else "active",
                "conflict_group_id": None,
                "last_seen_at": timestamp,
            }
            if normalized_statement(str(existing_fact.get("statement") or "")) != normalized:
                auto_merged += 1
            updated_fact_ids.append(fact_id)
        else:
            fact_id = new_id("fact")
            fact = {
                "id": fact_id,
                "statement": candidate["statement"],
                "entity_key": candidate["entity_key"],
                "page_hint": candidate["page_hint"],
                "section_hint": candidate["section_hint"] or None,
                "source_ids": candidate["source_ids"],
                "observed_at": candidate["observed_at"],
                "confidence": candidate["confidence"],
                "status": "active",
                "source_spans": candidate.get("source_spans") or [],
                "evidence_quote": candidate.get("evidence_quote"),
                "extraction_method": str(candidate.get("extraction_method") or "legacy"),
                "extractor_model": candidate.get("extractor_model"),
                "effective_at": candidate.get("effective_at"),
                "extraction_confidence": candidate.get("extraction_confidence"),
                "routing_confidence": candidate.get("routing_confidence"),
                "truth_confidence": candidate.get("truth_confidence", candidate["confidence"]),
                "metadata": candidate["metadata"],
                "created_at": timestamp,
                "last_seen_at": timestamp,
            }
            created_fact_ids.append(fact_id)
        apply_action(
            paths,
            propose_action(
                paths,
                "fact_upsert",
                action_payload={"fact": fact},
                action_features={
                    "candidate_signal": "wiki_fact_backfill",
                    "reversible": True,
                    "affected_fact_count": 1,
                    "migration": candidate.get("metadata", {}).get("migration"),
                },
                target_fact_ids=[fact_id],
                target_page_paths=[str(fact.get("page_hint") or "")],
                proposed_by="wiki_fact_migration",
                confidence=float(fact.get("truth_confidence") or fact.get("confidence") or 0.0),
                risk_tier="medium",
            )["id"],
        )
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
    entity_id = str(candidate.get("entity_id") or "").strip()
    if entity_id:
        query = """
            SELECT *
            FROM facts
            WHERE (entity_id = ? OR (entity_id IS NULL AND entity_key = ?))
              AND status != 'retracted'
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
            """
        params = (entity_id, candidate["entity_key"])
    else:
        query = """
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
            """
        params = (candidate["entity_key"],)
    for row in conn.execute(query, params):
        if normalized_statement(str(row["statement"] or "")) == normalized:
            return row
        if facts_should_merge(row_to_fact(row), candidate):
            return row
    return None


def fact_identity_group_key(fact: dict[str, Any]) -> str:
    entity_id = str(fact.get("entity_id") or "").strip()
    return f"entity_id:{entity_id}" if entity_id else f"entity_key:{fact.get('entity_key')}"


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
    truth_confidence = max(
        float(existing.get("truth_confidence") or existing.get("confidence") or 0.0),
        float(candidate.get("truth_confidence", candidate.get("confidence")) or 0.0),
    )
    extraction_confidence = max_optional_float(
        existing.get("extraction_confidence"), candidate.get("extraction_confidence")
    )
    routing_confidence = max_optional_float(
        existing.get("routing_confidence"), candidate.get("routing_confidence")
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
        "source_spans": merge_json_lists(
            existing.get("source_spans") or [], candidate.get("source_spans") or []
        ),
        "evidence_quote": candidate.get("evidence_quote")
        or existing.get("evidence_quote"),
        "extraction_method": candidate.get("extraction_method")
        or existing.get("extraction_method")
        or "legacy",
        "extractor_model": candidate.get("extractor_model")
        or existing.get("extractor_model"),
        "effective_at": candidate.get("effective_at") or existing.get("effective_at"),
        "extraction_confidence": extraction_confidence,
        "routing_confidence": routing_confidence,
        "truth_confidence": truth_confidence,
        "metadata": metadata,
    }


def max_optional_float(left: Any, right: Any) -> float | None:
    values = [float(value) for value in (left, right) if value is not None]
    return max(values) if values else None


def merge_json_lists(left: list[Any], right: list[Any]) -> list[Any]:
    merged: list[Any] = []
    seen: set[str] = set()
    for item in [*left, *right]:
        key = dumps(item)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


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


def resolve_fact_groups(
    paths: BrainPaths,
    entity_keys: list[str],
    *,
    llm_provider: LLMProvider | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    if not entity_keys:
        return {
            "auto_merged": 0,
            "auto_superseded": 0,
            "created_question_ids": [],
            "conflict_group_ids": [],
        }
    placeholders = ",".join("?" for _ in entity_keys)
    with connection(paths.sqlite_path) as conn:
        seed_rows = rows(
            conn,
            f"""
            SELECT *
            FROM facts
            WHERE entity_key IN ({placeholders})
              AND status IN ('active', 'conflicted', 'needs_confirmation')
            ORDER BY entity_key, observed_at, created_at
            """,
            entity_keys,
        )
        seed_entity_ids = stable_unique(
            [
                str(row_get(row, "entity_id") or "")
                for row in seed_rows
                if str(row_get(row, "entity_id") or "").strip()
            ]
        )
        if seed_entity_ids:
            entity_placeholders = ",".join("?" for _ in seed_entity_ids)
            fact_rows = rows(
                conn,
                f"""
                SELECT *
                FROM facts
                WHERE (
                    entity_id IN ({entity_placeholders})
                    OR (entity_id IS NULL AND entity_key IN ({placeholders}))
                )
                  AND status IN ('active', 'conflicted', 'needs_confirmation')
                ORDER BY COALESCE(entity_id, entity_key), observed_at, created_at
                """,
                [*seed_entity_ids, *entity_keys],
            )
        else:
            fact_rows = seed_rows
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in fact_rows:
        fact = row_to_fact(row)
        grouped[fact_identity_group_key(fact)].append(fact)

    auto_superseded = 0
    auto_merged = 0
    resolver_judgment_count = 0
    created_question_ids: list[str] = []
    conflict_group_ids: list[str] = []
    for identity_key, facts in grouped.items():
        entity_key = str(facts[0].get("entity_key") or identity_key)
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
        non_conflicting_additive = [
            fact for fact in additive_facts if fact not in conflicting_additive_facts
        ]
        if non_conflicting_additive:
            apply_fact_status_action(
                paths,
                "fact_supersede",
                [
                    {
                        "fact_id": fact["id"],
                        "status": "active",
                        "conflict_group_id": None,
                    }
                    for fact in non_conflicting_additive
                ],
                proposed_by="resolve_fact_groups",
                risk_tier="low",
            )
        if conflicting_additive_facts:
            replacement_facts = [*replacement_facts, *conflicting_additive_facts]
        if len(replacement_facts) <= 1:
            if replacement_facts:
                apply_fact_status_action(
                    paths,
                    "fact_supersede",
                    [
                        {
                            "fact_id": fact["id"],
                            "status": "active",
                            "conflict_group_id": None,
                        }
                        for fact in replacement_facts
                    ],
                    proposed_by="resolve_fact_groups",
                    risk_tier="low",
                )
            continue

        resolver_enabled = cos_role_provider_configured(paths, "resolver", llm_provider=llm_provider, provider=provider)
        if resolver_enabled:
            replacement_facts, merged_count = merge_exact_replacement_facts_with_actions(paths, replacement_facts)
        else:
            replacement_facts, merged_count = merge_similar_replacement_facts_with_actions(
                paths, replacement_facts
            )
        auto_merged += merged_count
        if len(replacement_facts) <= 1:
            if replacement_facts:
                apply_fact_status_action(
                    paths,
                    "fact_supersede",
                    [
                        {
                            "fact_id": fact["id"],
                            "status": "active",
                            "conflict_group_id": None,
                        }
                        for fact in replacement_facts
                    ],
                    proposed_by="resolve_fact_groups",
                    risk_tier="low",
                )
                dismiss_resolved_conflict_questions_for(
                    paths, entity_key, replacement_facts[0].get("page_hint")
            )
            continue

        if resolver_enabled:
            result = apply_resolver_judgment(
                paths,
                replacement_facts,
                llm_provider=llm_provider,
                provider=provider,
            )
            resolver_judgment_count += 1
            auto_merged += int(result.get("auto_merged") or 0)
            auto_superseded += int(result.get("auto_superseded") or 0)
            created_question_ids.extend(result.get("created_question_ids") or [])
            conflict_group_ids.extend(result.get("conflict_group_ids") or [])
            continue

        confirmed_facts = [
            fact for fact in replacement_facts if fact.get("confirmed_by_user")
        ]
        if confirmed_facts:
            keeper = choose_keeper_fact(confirmed_facts)
            updates = []
            for fact in replacement_facts:
                if fact["id"] == keeper["id"]:
                    updates.append(
                        {
                            "fact_id": fact["id"],
                            "status": "active",
                            "conflict_group_id": None,
                            "confirmed_by_user": True,
                        }
                    )
                else:
                    updates.append(
                        {
                            "fact_id": fact["id"],
                            "status": "superseded",
                            "conflict_group_id": None,
                        }
                    )
            apply_fact_status_action(
                paths,
                "resolve_conflict",
                updates,
                proposed_by="resolve_fact_groups",
                risk_tier="medium",
            )
            auto_superseded += len(replacement_facts) - 1
            dismiss_resolved_conflict_questions_for(
                paths, entity_key, keeper.get("page_hint")
            )
            continue

        ordered = sorted(replacement_facts, key=fact_recency_key)
        latest = ordered[-1]
        if fact_is_auto_winner(latest):
            older = ordered[:-1]
            supersedes_id = older[-1]["id"] if older else None
            updates = [
                {
                    "fact_id": latest["id"],
                    "status": "active",
                    "supersedes_id": supersedes_id,
                    "conflict_group_id": None,
                },
                *[
                    {
                        "fact_id": fact["id"],
                        "status": "superseded",
                        "conflict_group_id": None,
                    }
                    for fact in older
                ],
            ]
            apply_fact_status_action(
                paths,
                "fact_supersede",
                updates,
                proposed_by="resolve_fact_groups",
                risk_tier="medium",
            )
            auto_superseded += len(older)
            dismiss_resolved_conflict_questions_for(
                paths, entity_key, latest.get("page_hint")
            )
            continue

        conflict_group_id = new_id("factconflict")
        conflict_group_ids.append(conflict_group_id)
        fact_ids = [fact["id"] for fact in replacement_facts]
        action = apply_display_contested_action(
            paths, conflict_group_id, replacement_facts, fact_ids
        )
        created_question_ids.extend(
            action.get("inverse_action_json", {}).get("delete_question_ids", [])
        )
    return {
        "auto_merged": auto_merged,
        "auto_superseded": auto_superseded,
        "resolver_judgment_count": resolver_judgment_count,
        "created_question_ids": created_question_ids,
        "conflict_group_ids": conflict_group_ids,
    }


def apply_fact_status_action(
    paths: BrainPaths,
    action_type: str,
    updates: list[dict[str, Any]],
    *,
    proposed_by: str,
    risk_tier: str,
) -> dict[str, Any]:
    updates = [update for update in updates if update.get("fact_id")]
    if not updates:
        raise ValueError("fact status action requires updates")
    facts = facts_by_id(paths, [str(update["fact_id"]) for update in updates])
    return apply_action(
        paths,
        propose_action(
            paths,
            action_type,
            action_payload={"updates": updates},
            action_features={
                "truth_mutation": action_type == "resolve_conflict",
                "reversible": True,
                "affected_fact_count": len(updates),
                "eval_gate": {"suite": "conflict"},
            },
            target_fact_ids=[str(update["fact_id"]) for update in updates],
            target_page_paths=stable_unique(
                str(fact.get("page_hint") or "")
                for fact in facts
                if fact.get("page_hint")
            ),
            proposed_by=proposed_by,
            confidence=1.0 if action_type == "resolve_conflict" else None,
            risk_tier=risk_tier,
        )["id"],
    )


def apply_display_contested_action(
    paths: BrainPaths,
    conflict_group_id: str,
    facts: list[dict[str, Any]],
    fact_ids: list[str],
    *,
    proposed_by: str = "resolve_fact_groups",
    risk_tier: str = "medium",
) -> dict[str, Any]:
    return apply_action(
        paths,
        propose_action(
            paths,
            "display_contested",
            action_payload={
                "conflict_group_id": conflict_group_id,
                "fact_ids": fact_ids,
            },
                action_features={
                    "truth_mutation": False,
                    "display_uncertainty": True,
                    "reversible": True,
                    "affected_fact_count": len(fact_ids),
                    "eval_gate": {"suite": "conflict"},
                },
            target_fact_ids=fact_ids,
            target_page_paths=stable_unique(
                str(fact.get("page_hint") or "")
                for fact in facts
                if fact.get("page_hint")
            ),
            proposed_by=proposed_by,
            risk_tier=risk_tier,
        )["id"],
    )


def merge_similar_replacement_facts_with_actions(
    paths: BrainPaths, facts: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    return merge_replacement_facts_with_actions(
        paths,
        facts,
        merge_predicate=facts_should_merge,
        merge_reason="near_duplicate_replacement",
        proposed_by="resolve_fact_groups",
        risk_tier="medium",
    )


def merge_exact_replacement_facts_with_actions(
    paths: BrainPaths, facts: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    return merge_replacement_facts_with_actions(
        paths,
        facts,
        merge_predicate=facts_are_normalized_exact,
        merge_reason="exact_duplicate_replacement",
        proposed_by="resolve_fact_groups",
        risk_tier="low",
    )


def merge_replacement_facts_with_actions(
    paths: BrainPaths,
    facts: list[dict[str, Any]],
    *,
    merge_predicate: Any,
    merge_reason: str,
    proposed_by: str,
    risk_tier: str,
) -> tuple[list[dict[str, Any]], int]:
    clusters: list[list[dict[str, Any]]] = []
    for fact in sorted(facts, key=fact_recency_key, reverse=True):
        for cluster in clusters:
            if any(merge_predicate(fact, candidate) for candidate in cluster):
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
        keeper_fact = {
            **keeper,
            "statement": merged["statement"],
            "source_ids": merged["source_ids"],
            "observed_at": merged["observed_at"],
            "confidence": merged["confidence"],
            "truth_confidence": merged["confidence"],
            "status": "active",
            "supersedes_id": merged["supersedes_id"],
            "conflict_group_id": None,
            "metadata": merged["metadata"],
            "last_seen_at": timestamp,
        }
        superseded_fact_ids = [fact["id"] for fact in cluster if fact["id"] != keeper["id"]]
        apply_action(
            paths,
            propose_action(
                paths,
                "fact_merge",
                action_payload={
                    "keeper_fact": keeper_fact,
                    "superseded_fact_ids": superseded_fact_ids,
                },
                action_features={
                    "truth_mutation": False,
                    "reversible": True,
                    "affected_fact_count": len(cluster),
                    "merge_reason": merge_reason,
                    "eval_gate": {"suite": "conflict"},
                },
                target_fact_ids=[fact["id"] for fact in cluster],
                target_page_paths=stable_unique(
                    str(fact.get("page_hint") or "")
                    for fact in cluster
                    if fact.get("page_hint")
                ),
                proposed_by=proposed_by,
                confidence=float(merged["confidence"] or 0.0),
                risk_tier=risk_tier,
            )["id"],
        )
        survivors.append(get_fact(paths, keeper["id"]))
        merged_count += len(cluster) - 1
    return sorted(survivors, key=fact_recency_key), merged_count


def apply_resolver_judgment(
    paths: BrainPaths,
    facts: list[dict[str, Any]],
    *,
    llm_provider: LLMProvider | None,
    provider: str | None,
) -> dict[str, Any]:
    parsed = complete_json(
        resolver_prompt(facts),
        schema=RESOLVER_SCHEMA,
        role="resolver",
        provider=provider,
        llm_provider=llm_provider,
        paths=paths,
    )
    decision = normalize_resolver_decision(parsed.get("decision"))
    fact_ids = [str(fact_id) for fact_id in parsed.get("fact_ids") or [] if str(fact_id)]
    fact_by_id = {str(fact["id"]): fact for fact in facts}
    selected = [fact_by_id[fact_id] for fact_id in fact_ids if fact_id in fact_by_id]
    if len(selected) < 2:
        selected = facts
    if decision == "same_claim" and not any_fact_pair_conflicts(selected):
        _survivors, merged_count = merge_replacement_facts_with_actions(
            paths,
            selected,
            merge_predicate=lambda _left, _right: True,
            merge_reason="resolver_same_claim",
            proposed_by="resolver",
            risk_tier=resolver_risk_tier(parsed, default="medium"),
        )
        return {"auto_merged": merged_count, "auto_superseded": 0, "created_question_ids": [], "conflict_group_ids": []}
    if decision == "clear_supersession":
        keeper_id = str(parsed.get("keeper_fact_id") or "")
        keeper = fact_by_id.get(keeper_id) or choose_keeper_fact(selected)
        if resolver_supersession_is_safe(keeper, selected):
            updates = [
                {
                    "fact_id": keeper["id"],
                    "status": "active",
                    "supersedes_id": latest_non_keeper_id(selected, keeper),
                    "conflict_group_id": None,
                },
                *[
                    {
                        "fact_id": fact["id"],
                        "status": "superseded",
                        "conflict_group_id": None,
                    }
                    for fact in selected
                    if fact["id"] != keeper["id"]
                ],
            ]
            apply_fact_status_action(
                paths,
                "fact_supersede",
                updates,
                proposed_by="resolver",
                risk_tier=resolver_risk_tier(parsed, default="medium"),
            )
            dismiss_resolved_conflict_questions_for(paths, keeper["entity_key"], keeper.get("page_hint"))
            return {
                "auto_merged": 0,
                "auto_superseded": len(selected) - 1,
                "created_question_ids": [],
                "conflict_group_ids": [],
            }
    conflict_group_id = new_id("factconflict")
    action = apply_display_contested_action(
        paths,
        conflict_group_id,
        selected,
        [str(fact["id"]) for fact in selected],
        proposed_by="resolver",
        risk_tier="high",
    )
    return {
        "auto_merged": 0,
        "auto_superseded": 0,
        "created_question_ids": action.get("inverse_action_json", {}).get("delete_question_ids", []),
        "conflict_group_ids": [conflict_group_id],
    }


def resolver_prompt(facts: list[dict[str, Any]]) -> str:
    fact_cards = [
        {
            "id": fact.get("id"),
            "statement": fact.get("statement"),
            "entity_key": fact.get("entity_key"),
            "page_hint": fact.get("page_hint"),
            "section_hint": fact.get("section_hint"),
            "observed_at": fact.get("observed_at"),
            "confidence": fact.get("confidence"),
            "source_ids": fact.get("source_ids") or [],
            "confirmed_by_user": fact.get("confirmed_by_user"),
        }
        for fact in facts
    ]
    return (
        "Resolve this group of source-backed PKM facts. Choose exactly one decision: "
        "same_claim, clear_supersession, or contradiction. "
        "same_claim means the facts assert the same truth and can be merged. "
        "clear_supersession means one fact is clearly newer/current and the others should be superseded; "
        "provide keeper_fact_id. contradiction means the facts conflict or require external truth. "
        "Never choose a winner for a genuine contradiction. Return fact_ids, decision, keeper_fact_id when applicable, "
        "rationale, and risk_tier low/medium/high.\n\n"
        f"Facts:\n{fact_cards}"
    )


def normalize_resolver_decision(value: Any) -> str:
    decision = str(value or "").strip().lower()
    if decision in {"same_claim", "same", "merge", "duplicate"}:
        return "same_claim"
    if decision in {"clear_supersession", "supersession", "supersede", "newer_wins"}:
        return "clear_supersession"
    return "contradiction"


def resolver_risk_tier(parsed: dict[str, Any], *, default: str) -> str:
    risk_tier = str(parsed.get("risk_tier") or default).strip().lower()
    return risk_tier if risk_tier in {"low", "medium", "high"} else default


def any_fact_pair_conflicts(facts: list[dict[str, Any]]) -> bool:
    for index, left in enumerate(facts):
        for right in facts[index + 1 :]:
            if facts_directly_conflict(left, right):
                return True
    return False


def resolver_supersession_is_safe(keeper: dict[str, Any], facts: list[dict[str, Any]]) -> bool:
    if keeper.get("confirmed_by_user"):
        return True
    if not fact_is_auto_winner(keeper):
        return False
    latest = sorted(facts, key=fact_recency_key)[-1]
    return str(latest.get("id")) == str(keeper.get("id"))


def latest_non_keeper_id(facts: list[dict[str, Any]], keeper: dict[str, Any]) -> str | None:
    older = [fact for fact in sorted(facts, key=fact_recency_key) if fact["id"] != keeper["id"]]
    return str(older[-1]["id"]) if older else None


def facts_by_id(paths: BrainPaths, fact_ids: list[str]) -> list[dict[str, Any]]:
    if not fact_ids:
        return []
    placeholders = ",".join("?" for _ in fact_ids)
    with connection(paths.sqlite_path) as conn:
        return [
            row_to_fact(row)
            for row in conn.execute(
                f"SELECT * FROM facts WHERE id IN ({placeholders})",
                fact_ids,
            )
        ]


def dismiss_resolved_conflict_questions_for(
    paths: BrainPaths, entity_key: str, page_hint: Any
) -> None:
    with connection(paths.sqlite_path) as conn:
        dismiss_resolved_conflict_questions(conn, entity_key, page_hint)


def fact_recency_key(fact: dict[str, Any]) -> tuple[str, str]:
    return (str(fact.get("observed_at") or ""), str(fact.get("created_at") or ""))


def fact_is_auto_winner(fact: dict[str, Any]) -> bool:
    return float(fact.get("confidence") or 0.0) >= AUTO_SUPERSEDE_CONFIDENCE and bool(
        fact.get("source_ids")
    )


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


def facts_are_normalized_exact(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_statement = normalized_statement(str(left.get("statement") or ""))
    right_statement = normalized_statement(str(right.get("statement") or ""))
    return bool(left_statement and left_statement == right_statement)


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
    number_words = "|".join(MATERIAL_NUMBER_WORDS)
    for word, unit in re.findall(rf"\b({number_words})\s+([a-z]+)\b", lowered):
        if unit in MATERIAL_NUMBER_UNITS:
            values.add(f"{MATERIAL_NUMBER_WORDS[word]} {canonical_material_unit(unit)}")
    return values


def canonical_material_unit(unit: str) -> str:
    unit = unit.lower()
    if unit.endswith("s"):
        return unit[:-1]
    return unit


def ensure_open_conflict_question(
    conn: Any,
    conflict_group_id: str,
    facts: list[dict[str, Any]],
    fact_ids: list[str],
    *,
    action_id: str | None = None,
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
          status, context, action_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            action_id,
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
    actions: list[dict[str, Any]] = []
    if selected_fact_id:
        if selected_fact_id not in fact_ids:
            raise ValueError("selected_fact_id is not one of the question facts")
        updates = [
            {
                "fact_id": selected_fact_id,
                "status": "active",
                "confirmed_by_user": True,
                "conflict_group_id": None,
            },
            *[
                {
                    "fact_id": fact_id,
                    "status": "superseded",
                    "conflict_group_id": None,
                }
                for fact_id in fact_ids
                if fact_id != selected_fact_id
            ],
        ]
        action = apply_action(
            paths,
            propose_action(
                paths,
                "resolve_conflict",
                action_payload={"updates": updates, "question_id": question_id},
                action_features={
                    "human_confirmed": True,
                    "truth_mutation": True,
                    "reversible": True,
                    "affected_fact_count": len(updates),
                },
                target_fact_ids=fact_ids,
                target_page_paths=[str(page_hint)] if page_hint else [],
                proposed_by="question_answer",
                confidence=1.0,
                risk_tier="medium",
            )["id"],
        )
        actions.append(action)
        answer_payload = {
            "selected_fact_id": selected_fact_id,
            "answer": answer or "",
        }
    else:
        answer_text = compact_statement(answer or "", 1000)
        if not answer_text:
            raise ValueError("answer or selected_fact_id is required")
        manual_fact_id = new_id("fact")
        upsert_action = apply_action(
            paths,
            propose_action(
                paths,
                "fact_upsert",
                action_payload={
                    "fact": {
                        "id": manual_fact_id,
                        "statement": answer_text,
                        "entity_key": question.get("entity_key") or f"manual:{question_id}",
                        "page_hint": page_hint,
                        "section_hint": "Summary",
                        "source_ids": [f"manual:question:{question_id}"],
                        "observed_at": timestamp,
                        "confidence": 1.0,
                        "status": "active",
                        "confirmed_by_user": True,
                        "metadata": {"question_id": question_id, "answer": answer_text},
                        "created_at": timestamp,
                        "last_seen_at": timestamp,
                    }
                },
                action_features={
                    "human_confirmed": True,
                    "truth_mutation": True,
                    "reversible": True,
                    "affected_fact_count": 1,
                },
                target_fact_ids=[manual_fact_id],
                target_page_paths=[str(page_hint)] if page_hint else [],
                proposed_by="question_answer",
                confidence=1.0,
                risk_tier="medium",
            )["id"],
        )
        actions.append(upsert_action)
        if fact_ids:
            supersede_action = apply_action(
                paths,
                propose_action(
                    paths,
                    "fact_supersede",
                    action_payload={
                        "updates": [
                            {
                                "fact_id": fact_id,
                                "status": "superseded",
                                "conflict_group_id": None,
                            }
                            for fact_id in fact_ids
                        ],
                        "question_id": question_id,
                    },
                    action_features={
                        "human_confirmed": True,
                        "truth_mutation": True,
                        "reversible": True,
                        "affected_fact_count": len(fact_ids),
                    },
                    target_fact_ids=fact_ids,
                    target_page_paths=[str(page_hint)] if page_hint else [],
                    proposed_by="question_answer",
                    confidence=1.0,
                    risk_tier="medium",
                )["id"],
            )
            actions.append(supersede_action)
        answer_payload = {"selected_fact_id": manual_fact_id, "answer": answer_text}
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            UPDATE open_questions
            SET status = 'answered', answer = ?, answered_at = ?, action_id = ?
            WHERE id = ?
            """,
            (
                dumps(answer_payload),
                timestamp,
                actions[-1]["id"] if actions else None,
                question_id,
            ),
        )
    curation = curate_managed_pages(
        paths,
        page_hints=[str(page_hint)] if page_hint else [],
        overwrite_existing=overwrite_existing,
    )
    return {
        "question": get_question(paths, question_id),
        "actions": actions,
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
                merged_survivors, merged_count = merge_similar_replacement_facts_with_actions(
                    paths, cluster
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
            action = apply_display_contested_action(
                paths, conflict_group_id, survivors, survivor_ids
            )
            conn.execute(
                """
                UPDATE open_questions
                SET fact_ids = ?, options = ?, context = ?, action_id = ?
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
                    action["id"],
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


def managed_fact_page_summaries(paths: BrainPaths) -> list[dict[str, Any]]:
    fact_counts: dict[str, dict[str, Any]] = {}
    question_counts: dict[str, int] = {}
    indexed_pages: dict[str, dict[str, Any]] = {}
    with connection(paths.sqlite_path) as conn:
        for row in conn.execute(
            """
            SELECT page_hint,
                   COUNT(*) AS active_count,
                   MAX(COALESCE(observed_at, created_at)) AS latest_observed_at
            FROM facts
            WHERE status = 'active'
              AND page_hint IS NOT NULL
              AND page_hint != ''
            GROUP BY page_hint
            """
        ):
            fact_counts[str(row["page_hint"])] = {
                "active_fact_count": int(row["active_count"]),
                "latest_observed_at": row["latest_observed_at"],
            }
        for row in conn.execute(
            """
            SELECT page_hint, COUNT(*) AS open_count
            FROM open_questions
            WHERE status = 'open'
              AND page_hint IS NOT NULL
              AND page_hint != ''
            GROUP BY page_hint
            """
        ):
            question_counts[str(row["page_hint"])] = int(row["open_count"])
        for row in conn.execute(
            """
            SELECT *
            FROM wiki_pages
            WHERE managed = 1
            ORDER BY path
            """
        ):
            relative_path = indexed_page_relative_path(paths, str(row["path"] or ""))
            if relative_path:
                indexed_pages[relative_path] = dict(row)

    page_hints = sorted(set(fact_counts) | set(indexed_pages))
    summaries: list[dict[str, Any]] = []
    for page_hint in page_hints:
        target = safe_fact_wiki_path(paths, page_hint)
        exists = target.exists()
        current_markdown = (
            target.read_text(encoding="utf-8", errors="replace") if exists else ""
        )
        frontmatter, _body = parse_frontmatter(current_markdown) if current_markdown else ({}, "")
        indexed = indexed_pages.get(page_hint) or {}
        active_count = int(fact_counts.get(page_hint, {}).get("active_fact_count") or 0)
        managed = bool(indexed.get("managed")) or is_managed_page(current_markdown)
        summaries.append(
            {
                "relative_path": page_hint,
                "title": str(
                    (frontmatter or {}).get("title")
                    or indexed.get("title")
                    or human_title_for_path(page_hint)
                ),
                "page_type": str(
                    (frontmatter or {}).get("page_type")
                    or indexed.get("page_type")
                    or page_type_for_path(page_hint, frontmatter)
                ),
                "status": str(
                    (frontmatter or {}).get("status") or indexed.get("status") or ""
                ),
                "managed": managed,
                "exists": exists,
                "active_fact_count": active_count,
                "open_question_count": question_counts.get(page_hint, 0),
                "latest_observed_at": fact_counts.get(page_hint, {}).get(
                    "latest_observed_at"
                ),
            }
        )
    return summaries


def managed_fact_page_review(paths: BrainPaths, page_hint: str) -> dict[str, Any]:
    page_hint = canonical_page_hint_for_fact(str(page_hint or "").strip())
    target = safe_fact_wiki_path(paths, page_hint)
    current_markdown = (
        target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
    )
    active_facts = active_facts_by_page(paths, [page_hint]).get(page_hint, [])
    all_facts = facts_for_page(paths, page_hint)
    synthesis = active_page_synthesis(paths, page_hint, active_facts)
    draft_markdown = ""
    if active_facts:
        draft_markdown = render_managed_page(
            page_hint,
            active_facts,
            current_markdown,
            synthesis_markdown=synthesis.get("synthesis_markdown")
            if synthesis and not synthesis.get("stale_by_hash")
            else None,
        )
    elif current_markdown and is_managed_page(current_markdown):
        draft_markdown = render_archived_managed_page(page_hint, current_markdown)
    can_write = (
        not target.exists() or bool(current_markdown and is_managed_page(current_markdown))
    )
    diff = markdown_diff(current_markdown, draft_markdown)
    frontmatter, _body = parse_frontmatter(current_markdown) if current_markdown else ({}, "")
    return {
        "relative_path": page_hint,
        "path": str(target),
        "exists": target.exists(),
        "managed": bool(current_markdown and is_managed_page(current_markdown)),
        "can_write": can_write,
        "current_markdown": current_markdown,
        "draft_markdown": draft_markdown,
        "diff": diff,
        "would_change": current_markdown.rstrip() != draft_markdown.rstrip(),
        "frontmatter": frontmatter or {},
        "facts": all_facts,
        "active_facts": active_facts,
        "open_questions": open_questions_for_page(paths, page_hint),
        "snapshots": recent_page_snapshots(paths, page_hint),
        "synthesis": synthesis,
    }


def regenerate_managed_fact_page(
    paths: BrainPaths,
    page_hint: str,
    *,
    dry_run: bool = True,
    overwrite_existing: bool = False,
) -> dict[str, Any]:
    page_hint = canonical_page_hint_for_fact(str(page_hint or "").strip())
    if dry_run:
        return {
            "dry_run": True,
            "review": managed_fact_page_review(paths, page_hint),
            "dashboard": wiki_fact_dashboard(paths),
        }
    curation = curate_managed_pages(
        paths,
        page_hints=[page_hint],
        overwrite_existing=overwrite_existing,
    )
    return {
        "dry_run": False,
        "curation": curation,
        "review": managed_fact_page_review(paths, page_hint),
        "dashboard": wiki_fact_dashboard(paths),
    }


def create_confirmed_page_fact(
    paths: BrainPaths,
    page_hint: str,
    statement: str,
    *,
    section_hint: str = "Summary",
    supersede_fact_ids: list[str] | None = None,
    source_ids: list[str] | None = None,
    overwrite_existing: bool = False,
) -> dict[str, Any]:
    page_hint = canonical_page_hint_for_fact(str(page_hint or "").strip())
    statement = compact_statement(statement, 1000)
    if not page_hint:
        raise ValueError("page_hint is required")
    if not statement:
        raise ValueError("statement is required")
    section_hint = str(section_hint or "Summary").strip() or "Summary"
    supersede_fact_ids = stable_unique(str(fact_id) for fact_id in supersede_fact_ids or [])
    timestamp = now_iso()
    fact_id = new_id("fact")
    effective_source_ids = stable_unique(
        [*(source_ids or []), f"manual:chief-of-staff:{timestamp}"]
    )
    entity_key = entity_key_for_change(topic_for_path(page_hint), page_hint, section_hint)
    with connection(paths.sqlite_path) as conn:
        if supersede_fact_ids:
            placeholders = ",".join("?" for _ in supersede_fact_ids)
            found = [
                str(row["id"])
                for row in conn.execute(
                    f"""
                    SELECT id
                    FROM facts
                    WHERE id IN ({placeholders})
                      AND page_hint = ?
                      AND status != 'retracted'
                    """,
                    (*supersede_fact_ids, page_hint),
                )
            ]
            missing = sorted(set(supersede_fact_ids) - set(found))
            if missing:
                raise ValueError(f"supersede facts not found on page: {', '.join(missing)}")
    action = apply_action(
        paths,
        propose_action(
            paths,
            "fact_upsert",
            action_payload={
                "fact": {
                    "id": fact_id,
                    "statement": statement,
                    "entity_key": entity_key,
                    "page_hint": page_hint,
                    "section_hint": section_hint,
                    "source_ids": effective_source_ids,
                    "observed_at": timestamp,
                    "confidence": 1.0,
                    "status": "active",
                    "supersedes_id": supersede_fact_ids[0] if supersede_fact_ids else None,
                    "confirmed_by_user": True,
                    "source_spans": [],
                    "extraction_method": "manual",
                    "truth_confidence": 1.0,
                    "metadata": {
                        "source": "chief_of_staff_correction",
                        "supersede_fact_ids": supersede_fact_ids,
                    },
                    "created_at": timestamp,
                    "last_seen_at": timestamp,
                }
            },
            action_features={
                "human_confirmed": True,
                "truth_mutation": bool(supersede_fact_ids),
                "reversible": True,
                "affected_fact_count": 1 + len(supersede_fact_ids),
            },
            target_fact_ids=[fact_id],
            target_page_paths=[page_hint],
            proposed_by="chief_of_staff_correction",
            confidence=1.0,
            risk_tier="medium" if supersede_fact_ids else "low",
        )["id"],
    )
    supersede_action = None
    if supersede_fact_ids:
        supersede_action = apply_fact_status_action(
            paths,
            "fact_supersede",
            [
                {
                    "fact_id": old_fact_id,
                    "status": "superseded",
                    "conflict_group_id": None,
                }
                for old_fact_id in supersede_fact_ids
            ],
            proposed_by="chief_of_staff_correction",
            risk_tier="medium",
        )
    curation = curate_managed_pages(
        paths,
        page_hints=[page_hint],
        overwrite_existing=overwrite_existing,
    )
    return {
        "fact": get_fact(paths, fact_id),
        "action": action,
        "supersede_action": supersede_action,
        "curation": curation,
        "review": managed_fact_page_review(paths, page_hint),
        "dashboard": wiki_fact_dashboard(paths),
    }


def revert_wiki_page_snapshot(paths: BrainPaths, snapshot_id: str) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        row = conn.execute(
            "SELECT * FROM wiki_page_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
    if not row:
        raise ValueError(f"wiki page snapshot not found: {snapshot_id}")
    snapshot = row_to_page_snapshot(row)
    page_hint = snapshot["page_path"]
    target = safe_fact_wiki_path(paths, page_hint)
    current_markdown = (
        target.read_text(encoding="utf-8", errors="replace") if target.exists() else None
    )
    if snapshot["before_exists"]:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(snapshot["before_markdown"] or ""), encoding="utf-8")
    elif target.exists():
        target.unlink()
    lint_result = lint_wiki(paths)
    page_errors = [
        error
        for error in lint_result.get("errors", [])
        if error.split(":", 1)[0] == page_hint
    ]
    if page_errors:
        if current_markdown is None:
            if target.exists():
                target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(current_markdown, encoding="utf-8")
        lint_wiki(paths)
        raise ValueError("; ".join(page_errors))
    revert_snapshot_id = record_wiki_page_snapshot(
        paths,
        page_hint,
        before_markdown=current_markdown,
        after_markdown=target.read_text(encoding="utf-8", errors="replace")
        if target.exists()
        else None,
        reason="revert_managed_page_snapshot",
        metadata={"reverted_snapshot_id": snapshot_id},
    )
    action = apply_action(
        paths,
        propose_action(
            paths,
            "revert_page_snapshot",
            action_payload={
                "page_hint": page_hint,
                "snapshot_id": snapshot_id,
                "revert_snapshot_id": revert_snapshot_id,
            },
            action_features={
                "deterministic": True,
                "reversible": False,
                "projection": True,
                "affected_page_count": 1,
            },
            target_page_paths=[page_hint],
            proposed_by="page_snapshot_revert",
            risk_tier="medium",
        )["id"],
    )
    return {
        "snapshot": snapshot,
        "revert_snapshot_id": revert_snapshot_id,
        "action": action,
        "review": managed_fact_page_review(paths, page_hint),
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
        snapshot_id = record_wiki_page_snapshot(
            paths,
            relative_path,
            before_markdown=existing_text,
            after_markdown=archived_markdown.rstrip() + "\n",
            reason="archive_orphan_managed_page",
            metadata={"active_page_hints": active_page_hints},
        )
        action = apply_action(
            paths,
            propose_action(
                paths,
                "archive_page",
                action_payload={
                    "page_hint": relative_path,
                    "snapshot_id": snapshot_id,
                    "reason": "orphan_managed_page",
                },
                action_features={
                    "deterministic": True,
                    "reversible": False,
                    "projection": True,
                    "affected_page_count": 1,
                },
                target_page_paths=[relative_path],
                proposed_by="archive_orphan_managed_pages",
                risk_tier="low",
            )["id"],
        )
        archived.append(
            {
                "relative_path": relative_path,
                "path": str(path),
                "status": "archived",
                "snapshot_id": snapshot_id,
                "action_id": action["id"],
            }
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
        apply_action(
            paths,
            propose_action(
                paths,
                "rehome_fact",
                action_payload={
                    "fact_id": str(row["id"]),
                    "page_hint": canonical_page_hint,
                    "entity_key": canonical_entity_key,
                    "section_hint": section_hint or None,
                    "metadata": metadata,
                },
                action_features={
                    "candidate_signal": "canonicalize_fact_route",
                    "deterministic": True,
                    "reversible": True,
                    "affected_fact_count": 1,
                },
                target_fact_ids=[str(row["id"])],
                target_page_paths=stable_unique([original_page_hint, canonical_page_hint]),
                proposed_by="canonicalize_fact_routes",
                risk_tier="low",
            )["id"],
        )
        updated_fact_ids.append(str(row["id"]))
        affected_entity_keys.append(canonical_entity_key)
        affected_page_hints.append(canonical_page_hint)

    if updated_fact_ids:
        changed = set(updated_fact_ids)
        with connection(paths.sqlite_path) as conn:
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
    pending_snapshots: list[dict[str, Any]] = []
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
        synthesis = active_page_synthesis(paths, page_hint, facts)
        markdown = render_managed_page(
            page_hint,
            facts,
            existing_text,
            synthesis_markdown=synthesis.get("synthesis_markdown")
            if synthesis and not synthesis.get("stale_by_hash")
            else None,
        )
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
        pending_snapshots.append(
            {
                "page_hint": page_hint,
                "before_markdown": originals[target],
                "after_markdown": markdown.rstrip() + "\n",
                "reason": "regenerate_managed_fact_page",
                "metadata": {"fact_ids": page_result["fact_ids"]},
                "page_result": page_result,
            }
        )

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
        for snapshot in pending_snapshots:
            snapshot_id = record_wiki_page_snapshot(
                paths,
                snapshot["page_hint"],
                before_markdown=snapshot["before_markdown"],
                after_markdown=snapshot["after_markdown"],
                reason=snapshot["reason"],
                metadata=snapshot["metadata"],
            )
            snapshot["page_result"]["snapshot_id"] = snapshot_id
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


def facts_for_page(paths: BrainPaths, page_hint: str) -> list[dict[str, Any]]:
    with connection(paths.sqlite_path) as conn:
        return [
            row_to_fact(row)
            for row in conn.execute(
                """
                SELECT *
                FROM facts
                WHERE page_hint = ?
                  AND status != 'retracted'
                ORDER BY
                  CASE status
                    WHEN 'active' THEN 0
                    WHEN 'conflicted' THEN 1
                    WHEN 'needs_confirmation' THEN 2
                    WHEN 'superseded' THEN 3
                    ELSE 4
                  END,
                  COALESCE(observed_at, created_at) DESC
                """,
                (page_hint,),
            )
        ]


def get_fact(paths: BrainPaths, fact_id: str) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        row = conn.execute("SELECT * FROM facts WHERE id = ?", (fact_id,)).fetchone()
    if not row:
        raise ValueError(f"fact not found: {fact_id}")
    return row_to_fact(row)


def open_questions_for_page(paths: BrainPaths, page_hint: str) -> list[dict[str, Any]]:
    with connection(paths.sqlite_path) as conn:
        return [
            row_to_question(row)
            for row in conn.execute(
                """
                SELECT *
                FROM open_questions
                WHERE page_hint = ?
                  AND status = 'open'
                ORDER BY created_at DESC
                """,
                (page_hint,),
            )
        ]


def recent_page_snapshots(
    paths: BrainPaths, page_hint: str, *, limit: int = 20
) -> list[dict[str, Any]]:
    with connection(paths.sqlite_path) as conn:
        return [
            row_to_page_snapshot(row)
            for row in conn.execute(
                """
                SELECT *
                FROM wiki_page_snapshots
                WHERE page_path = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (page_hint, limit),
            )
        ]


def record_wiki_page_snapshot(
    paths: BrainPaths,
    page_hint: str,
    *,
    before_markdown: str | None,
    after_markdown: str | None,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    snapshot_id = new_id("wikisnap")
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO wiki_page_snapshots(
              id, page_path, before_markdown, after_markdown, before_exists,
              after_exists, reason, metadata, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                page_hint,
                before_markdown,
                after_markdown,
                1 if before_markdown is not None else 0,
                1 if after_markdown is not None else 0,
                reason,
                dumps(metadata or {}),
                now_iso(),
            ),
        )
    return snapshot_id


def rebuild_fact_retrieval_index(conn: Any) -> None:
    if not table_exists(conn, "retrieval_fts"):
        return
    conn.execute("DELETE FROM retrieval_fts WHERE kind = 'fact'")
    conn.execute(
        """
        INSERT INTO retrieval_fts(kind, target_id, title, text, heading_path, project, tags)
        SELECT 'fact', id, COALESCE(page_hint, entity_key), statement,
               COALESCE(section_hint, ''), '', COALESCE(source_ids, '[]')
        FROM facts
        WHERE status IN ('active', 'conflicted', 'needs_confirmation')
        """
    )


def table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'virtual') AND name = ?",
            (table,),
        ).fetchone()
    )


def markdown_diff(before: str, after: str, *, max_lines: int = 500) -> dict[str, Any]:
    lines = list(
        unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile="current",
            tofile="draft",
            lineterm="",
        )
    )
    return {
        "lines": lines[:max_lines],
        "line_count": len(lines),
        "truncated": len(lines) > max_lines,
    }


def indexed_page_relative_path(paths: BrainPaths, value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(paths.wiki.resolve()).as_posix()
        except ValueError:
            return ""
    return candidate.as_posix()


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
    page_hint: str,
    facts: list[dict[str, Any]],
    existing_text: str = "",
    *,
    synthesis_markdown: str | None = None,
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
    synthesis_block = render_synthesis_block(synthesis_markdown)
    return (
        "---\n"
        f"{yaml.safe_dump(metadata, sort_keys=False).strip()}\n"
        "---\n\n"
        f"{CHIEF_OF_STAFF_MARKER}\n\n"
        f"# {title}\n\n"
        f"{synthesis_block}"
        f"{body}\n"
    )


def render_synthesis_block(synthesis_markdown: str | None) -> str:
    if not synthesis_markdown:
        return ""
    return (
        "## Derived Synthesis\n\n"
        "_Non-canonical synthesis from active facts only. Canonical claims remain the fact bullets below._\n\n"
        f"{synthesis_markdown.strip()}\n\n"
    )


def active_page_synthesis(
    paths: BrainPaths, page_hint: str, facts: list[dict[str, Any]]
) -> dict[str, Any] | None:
    current_hash = fact_set_hash(facts)
    with connection(paths.sqlite_path) as conn:
        row = conn.execute(
            """
            SELECT *
            FROM wiki_page_syntheses
            WHERE page_hint = ?
              AND stale = 0
            ORDER BY generated_at DESC
            LIMIT 1
            """,
            (page_hint,),
        ).fetchone()
    if not row:
        return None
    synthesis = row_to_synthesis(row)
    synthesis["current_fact_hash"] = current_hash
    synthesis["stale_by_hash"] = bool(synthesis.get("fact_hash") and synthesis.get("fact_hash") != current_hash)
    if synthesis["stale_by_hash"]:
        return synthesis
    return synthesis


def fact_set_hash(facts: list[dict[str, Any]]) -> str:
    payload = [
        {
            "id": fact.get("id"),
            "statement": fact.get("statement"),
            "status": fact.get("status"),
            "source_ids": fact.get("source_ids") or [],
            "truth_confidence": fact.get("truth_confidence", fact.get("confidence")),
        }
        for fact in sorted(facts, key=lambda item: str(item.get("id") or ""))
    ]
    return text_sha256(dumps(payload))


def row_to_synthesis(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "page_hint": row["page_hint"],
        "synthesis_markdown": row["synthesis_markdown"],
        "fact_ids": loads(row["fact_ids"], []),
        "fact_hash": row["fact_hash"],
        "model": row["model"],
        "prompt_version": row["prompt_version"],
        "generated_at": row["generated_at"],
        "stale": bool(row["stale"]),
    }


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
        "managed_pages": managed_fact_page_summaries(paths),
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
        "entity_id": row_get(row, "entity_id"),
        "page_hint": row["page_hint"],
        "section_hint": row["section_hint"],
        "source_ids": loads(row["source_ids"], []),
        "observed_at": row["observed_at"],
        "confidence": row["confidence"],
        "source_spans": loads(row_get(row, "source_spans"), []),
        "evidence_quote": row_get(row, "evidence_quote"),
        "extraction_method": row_get(row, "extraction_method", "legacy"),
        "extractor_model": row_get(row, "extractor_model"),
        "effective_at": row_get(row, "effective_at"),
        "extraction_confidence": row_get(row, "extraction_confidence"),
        "routing_confidence": row_get(row, "routing_confidence"),
        "truth_confidence": row_get(row, "truth_confidence", row["confidence"]),
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
        "action_id": row_get(row, "action_id"),
        "recommended_action": loads(row_get(row, "recommended_action"), {}),
        "auto_resolve_after": row_get(row, "auto_resolve_after"),
        "risk_tier": row_get(row, "risk_tier"),
        "resolver": row_get(row, "resolver"),
        "decided_by": row_get(row, "decided_by"),
        "created_at": row["created_at"],
        "answered_at": row["answered_at"],
    }


def row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        if hasattr(row, "keys") and key not in row.keys():
            return default
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def row_to_curation_run(row: Any) -> dict[str, Any]:
    summary = loads(row["summary"], {})
    return {
        "id": row["id"],
        "source_packet_id": row["source_packet_id"],
        "group_by": row["group_by"],
        "status": row["status"],
        "summary": compact_curation_run_summary(summary),
        "created_at": row["created_at"],
    }


def compact_curation_run_summary(summary: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key in [
        "candidates",
        "new_candidates",
        "facts_created",
        "facts_updated",
        "auto_merged",
        "auto_superseded",
        "questions_created",
        "questions_dismissed",
        "pages_written",
        "pages_previewed",
    ]:
        if key in summary:
            output[key] = summary[key]
    packet = summary.get("packet")
    if isinstance(packet, dict):
        output["packet_label"] = packet.get("label") or packet.get("id")
        output["packet_items"] = packet.get("item_count")
        output["packet_pages"] = packet.get("target_count")
    absorption = summary.get("proposal_absorption")
    if isinstance(absorption, dict):
        output["absorbed_items"] = len(absorption.get("updated_item_ids") or [])
        output["already_absorbed_items"] = len(
            absorption.get("already_absorbed_item_ids") or []
        )
        output["fully_absorbed_batches"] = len(
            absorption.get("fully_absorbed_batch_ids") or []
        )
        output["partially_absorbed_batches"] = len(
            absorption.get("partially_absorbed_batch_ids") or []
        )
    lint_errors = summary.get("lint_errors") or []
    projection_errors = summary.get("projection_errors") or []
    if lint_errors:
        output["lint_errors"] = len(lint_errors)
    if projection_errors:
        output["projection_errors"] = len(projection_errors)
    return output


def row_to_page_snapshot(row: Any) -> dict[str, Any]:
    before_markdown = row["before_markdown"]
    after_markdown = row["after_markdown"]
    return {
        "id": row["id"],
        "page_path": row["page_path"],
        "before_exists": bool(row["before_exists"]),
        "after_exists": bool(row["after_exists"]),
        "reason": row["reason"],
        "metadata": loads(row["metadata"], {}),
        "created_at": row["created_at"],
        "before_markdown": before_markdown,
        "after_markdown": after_markdown,
        "before_preview": compact_statement(before_markdown or "", 180),
        "after_preview": compact_statement(after_markdown or "", 180),
    }


def fact_status_summary(facts: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(str(fact.get("status") or "") for fact in facts))
