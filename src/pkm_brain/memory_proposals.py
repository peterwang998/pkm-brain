from __future__ import annotations

import json
import re
from typing import Any

from .db import connection, loads, rows
from .llm import get_provider
from .paths import BrainPaths
from .service import BrainService
from .wiki_proposals import parse_json_object


FAILURE_MEMORY_TYPE = "AgentFailurePatternMemory"


def propose_failure_memories_from_sources(
    paths: BrainPaths,
    provider_name: str | None = "codex",
    limit: int = 12,
) -> dict[str, Any]:
    provider = get_provider(provider_name)
    sources = collect_failure_learning_sources(paths, limit=limit)
    if not has_failure_learning_evidence(sources):
        return {"created": False, "reason": "no failure-learning sources found", "provider": provider.name, "model": provider.model}

    existing = existing_failure_memories(paths)
    prompt = failure_memory_prompt(sources, existing)
    parsed = parse_json_object(provider.complete(prompt))
    proposals = parsed.get("memories") or parsed.get("proposals") or []
    if not isinstance(proposals, list) or not proposals:
        return {"created": False, "reason": "provider returned no memories", "provider": provider.name, "model": provider.model}

    service = BrainService(paths)
    existing_signatures = [memory_signature(memory["content"]) for memory in existing]
    created: list[dict[str, Any]] = []
    skipped_duplicates: list[str] = []
    seen_signatures: list[str] = []
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        content = str(proposal.get("content") or "").strip()
        if not content:
            continue
        signature = memory_signature(content)
        if is_duplicate_signature(signature, existing_signatures + seen_signatures):
            skipped_duplicates.append(content)
            continue
        source_ids = [str(source_id) for source_id in proposal.get("source_ids", []) if str(source_id).strip()]
        if not source_ids:
            continue
        scope = str(proposal.get("scope") or "agent:codex").strip() or "agent:codex"
        confidence = float(proposal.get("confidence", 0.7))
        memory_id = service.propose_memory(FAILURE_MEMORY_TYPE, scope, content, source_ids, confidence)
        created.append({"memory_id": memory_id, "content": content, "scope": scope, "source_ids": source_ids, "confidence": confidence})
        seen_signatures.append(signature)

    return {
        "created": bool(created),
        "created_count": len(created),
        "memory_ids": [item["memory_id"] for item in created],
        "memories": created,
        "skipped_duplicates": skipped_duplicates,
        "provider": provider.name,
        "model": provider.model,
        "source_counts": {key: len(value) for key, value in sources.items()},
    }


def collect_failure_learning_sources(paths: BrainPaths, limit: int = 12) -> dict[str, list[dict[str, Any]]]:
    with connection(paths.sqlite_path) as conn:
        sessions = [
            {
                "source_id": f"agent_session:{row['id']}",
                "summary": row["summary"],
                "files_touched": loads(row["files_touched"], []),
                "commands_run": loads(row["commands_run"], []),
                "outcome": row["outcome"],
                "unresolved_issues": loads(row["unresolved_issues"], []),
                "created_at": row["created_at"],
            }
            for row in rows(
                conn,
                """
                SELECT *
                FROM agent_sessions
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        ]
        agent_logs = [
            {
                "source_id": f"document:{row['id']}",
                "title": row["title"],
                "source_path": row["source_path"],
                "ingested_at": row["ingested_at"],
                "preview": truncate_whitespace(row["chunk_text"] or "", 2200),
            }
            for row in rows(
                conn,
                """
                SELECT d.id, d.title, d.source_path, d.ingested_at,
                       GROUP_CONCAT(c.text, '\n\n') AS chunk_text
                FROM documents d
                LEFT JOIN chunks c ON c.document_id = d.id
                WHERE d.source_type = 'agent_session_log'
                GROUP BY d.id
                ORDER BY d.ingested_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        ]
        retrievals = [
            {
                "source_id": f"retrieval:{row['id']}",
                "query": row["query"],
                "caller": row["caller"],
                "timestamp": row["timestamp"],
                "selected_chunk_ids": loads(row["selected_chunk_ids"], []),
                "debug": summarize_retrieval_debug(loads(row["debug"], {})),
            }
            for row in rows(
                conn,
                """
                SELECT *
                FROM retrieval_events
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            )
        ]
        existing_memories = [
            {
                "source_id": f"memory:{row['id']}",
                "memory_type": row["memory_type"],
                "scope": row["scope"],
                "status": row["status"],
                "content": row["content"],
                "source_ids": loads(row["source_ids"], []),
            }
            for row in rows(
                conn,
                """
                SELECT *
                FROM memories
                WHERE memory_type = ?
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                (FAILURE_MEMORY_TYPE, limit),
            )
        ]
    return {
        "agent_sessions": sessions,
        "agent_logs": agent_logs,
        "retrieval_events": retrievals,
        "existing_failure_memories": existing_memories,
    }


def has_failure_learning_evidence(sources: dict[str, list[dict[str, Any]]]) -> bool:
    if sources["agent_logs"] or sources["retrieval_events"]:
        return True
    return any(session.get("unresolved_issues") or str(session.get("outcome", "")).lower() not in {"", "success"} for session in sources["agent_sessions"])


def existing_failure_memories(paths: BrainPaths) -> list[dict[str, Any]]:
    with connection(paths.sqlite_path) as conn:
        return [
            {
                "id": row["id"],
                "status": row["status"],
                "scope": row["scope"],
                "content": row["content"],
                "source_ids": loads(row["source_ids"], []),
            }
            for row in rows(
                conn,
                """
                SELECT *
                FROM memories
                WHERE memory_type = ? AND status IN ('proposed', 'active')
                ORDER BY updated_at DESC, created_at DESC
                """,
                (FAILURE_MEMORY_TYPE,),
            )
        ]


def failure_memory_prompt(sources: dict[str, list[dict[str, Any]]], existing: list[dict[str, Any]]) -> str:
    return (
        "You maintain reviewed operational failure-pattern memories for local coding agents.\n"
        "Propose only durable, actionable patterns that would prevent repeated agent mistakes.\n"
        "Every proposal must be backed by the provided source_ids. Do not propose generic advice.\n"
        "Do not duplicate existing proposed or active failure memories.\n"
        "Return only JSON with this exact shape: "
        "{\"memories\": [{\"content\": str, \"scope\": str, \"source_ids\": [str], \"confidence\": number}]}.\n"
        "Use memory_type AgentFailurePatternMemory implicitly; do not include any other memory type.\n"
        "Use scope \"agent:codex\" for Codex-specific operational failures, \"agent:claude\" for Claude-specific failures, "
        "or \"global\" when the pattern applies to all agents.\n\n"
        f"Existing failure memories:\n{json.dumps(existing, indent=2)}\n\n"
        f"Sources:\n{json.dumps(sources, indent=2)}"
    )


def summarize_retrieval_debug(debug: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(debug, dict):
        return {}
    return {
        "important_query_terms": debug.get("important_query_terms", []),
        "selected_chunk_reasons": debug.get("selected_chunk_reasons", [])[:4],
        "suppressed_chunk_reasons": debug.get("suppressed_chunk_reasons", [])[:4],
        "fanout_counts": debug.get("fanout_counts", {}),
    }


def truncate_whitespace(text: str, limit: int) -> str:
    return re.sub(r"\s+", " ", text).strip()[:limit]


def memory_signature(content: str) -> str:
    words = [word for word in re.findall(r"[a-z0-9]+", content.lower()) if word not in SIGNATURE_STOPWORDS]
    return " ".join(words)


def is_duplicate_signature(candidate: str, existing: list[str]) -> bool:
    if not candidate:
        return True
    candidate_terms = set(candidate.split())
    for signature in existing:
        if candidate == signature:
            return True
        terms = set(signature.split())
        if not candidate_terms or not terms:
            continue
        overlap = len(candidate_terms.intersection(terms)) / len(candidate_terms.union(terms))
        if overlap >= 0.85:
            return True
    return False


SIGNATURE_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "be",
    "by",
    "for",
    "from",
    "if",
    "in",
    "into",
    "is",
    "it",
    "of",
    "on",
    "or",
    "should",
    "that",
    "the",
    "then",
    "to",
    "when",
    "with",
}
