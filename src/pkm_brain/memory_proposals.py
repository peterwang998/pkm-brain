from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .db import connection, dumps, loads, rows
from .llm import get_provider, parse_json_object
from .paths import BrainPaths
from .service import BrainService
from .util import new_id, now_iso


FAILURE_MEMORY_TYPE = "AgentFailurePatternMemory"
LINEAGE_MEMORY_TYPES = {
    "PreferenceMemory",
    "ProjectMemory",
    "DecisionMemory",
    "BehaviorMemory",
    "RepoInstructionMemory",
    "OpenLoopMemory",
    "FactMemory",
    "AgentFailurePatternMemory",
}


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
    skipped: list[dict[str, str]] = []
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
        try:
            memory_id = service.propose_memory(FAILURE_MEMORY_TYPE, scope, content, source_ids, confidence)
        except ValueError as exc:
            skipped.append({"content": content, "reason": str(exc)})
            continue
        created.append({"memory_id": memory_id, "content": content, "scope": scope, "source_ids": source_ids, "confidence": confidence})
        seen_signatures.append(signature)

    return {
        "created": bool(created),
        "created_count": len(created),
        "memory_ids": [item["memory_id"] for item in created],
        "memories": created,
        "skipped": skipped,
        "skipped_duplicates": skipped_duplicates,
        "provider": provider.name,
        "model": provider.model,
        "source_counts": {key: len(value) for key, value in sources.items()},
    }


def propose_memories_from_lineage(
    paths: BrainPaths,
    provider_name: str | None = "codex",
    limit: int = 12,
) -> dict[str, Any]:
    clusters = collect_lineage_memory_clusters(paths, limit=limit)
    if not clusters:
        return {"created": False, "reason": "no eligible lineage clusters found", "clusters": []}

    provider = get_provider(provider_name)
    existing = existing_active_or_proposed_memories(paths)
    prompt = lineage_memory_prompt(clusters, existing)
    parsed = parse_json_object(provider.complete(prompt))
    proposals = parsed.get("memories") or parsed.get("proposals") or []
    if not isinstance(proposals, list) or not proposals:
        return {
            "created": False,
            "reason": "provider returned no memories",
            "provider": provider.name,
            "model": provider.model,
            "clusters": clusters,
        }

    service = BrainService(paths)
    existing_signatures = [memory_signature(memory["content"]) for memory in existing]
    cluster_by_id = {cluster["cluster_id"]: cluster for cluster in clusters}
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    seen_signatures: list[str] = []
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        cluster_id = str(proposal.get("cluster_id") or "").strip()
        cluster = cluster_by_id.get(cluster_id)
        if not cluster:
            skipped.append({"cluster_id": cluster_id, "reason": "unknown cluster"})
            continue
        content = str(proposal.get("content") or "").strip()
        if not durable_memory_content(content):
            skipped.append({"cluster_id": cluster_id, "reason": "empty or non-actionable content"})
            continue
        signature = memory_signature(content)
        if is_duplicate_signature(signature, existing_signatures + seen_signatures):
            skipped.append({"cluster_id": cluster_id, "reason": "duplicate memory"})
            continue
        memory_type = str(proposal.get("memory_type") or cluster.get("suggested_memory_type") or "FactMemory")
        if memory_type not in LINEAGE_MEMORY_TYPES:
            skipped.append({"cluster_id": cluster_id, "reason": f"invalid memory_type {memory_type}"})
            continue
        scope = str(proposal.get("scope") or cluster.get("suggested_scope") or "global").strip() or "global"
        source_ids = [str(source_id) for source_id in proposal.get("source_ids", []) if str(source_id).strip()]
        if not source_ids:
            source_ids = list(cluster.get("source_ids") or [])
        if not source_ids:
            skipped.append({"cluster_id": cluster_id, "reason": "missing source_ids"})
            continue
        confidence = max(0.0, min(float(proposal.get("confidence", 0.7)), 1.0))
        try:
            memory_id = service.propose_memory(memory_type, scope, content, source_ids, confidence)
        except ValueError as exc:
            skipped.append({"cluster_id": cluster_id, "reason": str(exc)})
            continue
        rationale = str(proposal.get("rationale") or cluster.get("rationale") or "")
        record_memory_proposed_from_lineage(paths, memory_id, cluster, rationale)
        created.append(
            {
                "memory_id": memory_id,
                "memory_type": memory_type,
                "scope": scope,
                "content": content,
                "source_ids": source_ids,
                "independent_session_count": cluster["independent_session_count"],
                "last_seen_at": cluster["last_seen_at"],
                "rationale": rationale,
                "confidence": confidence,
                "negative_signals": cluster.get("negative_signals", []),
                "status": "proposed",
                "cluster_id": cluster_id,
            }
        )
        seen_signatures.append(signature)

    return {
        "created": bool(created),
        "created_count": len(created),
        "memory_ids": [item["memory_id"] for item in created],
        "memories": created,
        "skipped": skipped,
        "provider": provider.name,
        "model": provider.model,
        "clusters": clusters,
    }


def collect_lineage_memory_clusters(paths: BrainPaths, limit: int = 12) -> list[dict[str, Any]]:
    with connection(paths.sqlite_path) as conn:
        lineage = rows(
            conn,
            """
            SELECT *
            FROM context_lineage_events
            WHERE event_type IN ('agent_referenced_id', 'explicit_useful', 'explicit_not_useful')
            ORDER BY created_at DESC
            LIMIT 500
            """,
        )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in lineage:
        event = dict(row)
        event["metadata"] = loads(event.get("metadata"), {})
        grouped.setdefault((event["target_type"], event["target_id"]), []).append(event)

    clusters: list[dict[str, Any]] = []
    for (target_type, target_id), events in grouped.items():
        agent_refs = [event for event in events if event["event_type"] == "agent_referenced_id"]
        sessions = sorted({str(event.get("agent_session_id") or "") for event in agent_refs if event.get("agent_session_id")})
        useful = [event for event in events if event["event_type"] == "explicit_useful"]
        not_useful = [event for event in events if event["event_type"] == "explicit_not_useful"]
        sorted_refs = sorted(agent_refs, key=lambda event: str(event.get("created_at") or ""))
        has_later_stable_reference = len(sessions) >= 2 and len(sorted_refs) >= 2 and sorted_refs[-1]["created_at"] > sorted_refs[0]["created_at"]
        eligible = (
            len(sessions) >= 3
            or (len(sessions) >= 2 and bool(useful))
            or has_later_stable_reference
        )
        if not eligible:
            continue
        source_ids = cluster_source_ids(target_type, target_id, events)
        if not source_ids:
            continue
        last_seen_at = max(str(event.get("created_at") or "") for event in events)
        cluster_id = f"{target_type}:{target_id}"
        clusters.append(
            {
                "cluster_id": cluster_id,
                "target_type": target_type,
                "target_id": target_id,
                "source_ids": source_ids,
                "independent_session_count": len(sessions),
                "agent_reference_count": len(agent_refs),
                "explicit_useful_count": len(useful),
                "explicit_not_useful_count": len(not_useful),
                "last_seen_at": last_seen_at,
                "threshold_reason": threshold_reason(len(sessions), bool(useful), has_later_stable_reference),
                "rationale": f"Repeated independent lineage references to {cluster_id}.",
                "negative_signals": [str(event.get("metadata", {}).get("note") or "explicit not useful") for event in not_useful],
                "suggested_memory_type": suggested_memory_type(target_type, target_id),
                "suggested_scope": suggested_scope(target_type, target_id),
                "evidence": cluster_evidence(events),
            }
        )
    clusters.sort(
        key=lambda cluster: (
            int(cluster["independent_session_count"]),
            int(cluster["explicit_useful_count"]),
            str(cluster["last_seen_at"]),
        ),
        reverse=True,
    )
    return clusters[:limit]


def cluster_source_ids(target_type: str, target_id: str, events: list[dict[str, Any]]) -> list[str]:
    source_ids: list[str] = []
    if target_type == "document":
        source_ids.append(f"document:{target_id}")
    elif target_type in {"chunk", "memory", "wiki_page"}:
        source_ids.append(target_id)
    for event in events:
        metadata = event.get("metadata") or {}
        source_id = metadata.get("source_id")
        if source_id:
            source_ids.append(str(source_id))
        document_id = metadata.get("document_id")
        if document_id:
            source_ids.append(f"document:{document_id}")
        retrieval_event_id = event.get("retrieval_event_id")
        if retrieval_event_id:
            source_ids.append(f"retrieval:{retrieval_event_id}")
    return dedupe_strings(source_ids)


def threshold_reason(session_count: int, has_useful: bool, has_later_reference: bool) -> str:
    if session_count >= 3:
        return "at least 3 distinct agent sessions"
    if session_count >= 2 and has_useful:
        return "2 distinct sessions plus explicit useful feedback"
    if session_count >= 2 and has_later_reference:
        return "2 distinct sessions plus later stable-ID re-reference"
    return "not eligible"


def suggested_memory_type(target_type: str, target_id: str) -> str:
    if target_type == "wiki_page":
        if target_id.startswith("decisions/"):
            return "DecisionMemory"
        if target_id.startswith("projects/"):
            return "ProjectMemory"
    if target_type == "memory":
        return "BehaviorMemory"
    return "FactMemory"


def suggested_scope(target_type: str, target_id: str) -> str:
    if target_type == "wiki_page" and target_id.startswith("projects/"):
        return f"project:{Path(target_id).stem}"
    return "global"


def cluster_evidence(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence = []
    for event in sorted(events, key=lambda item: str(item.get("created_at") or ""), reverse=True)[:8]:
        evidence.append(
            {
                "event_type": event.get("event_type"),
                "agent_session_id": event.get("agent_session_id"),
                "retrieval_event_id": event.get("retrieval_event_id"),
                "query": event.get("query"),
                "metadata": event.get("metadata") or {},
                "created_at": event.get("created_at"),
            }
        )
    return evidence


def existing_active_or_proposed_memories(paths: BrainPaths) -> list[dict[str, Any]]:
    with connection(paths.sqlite_path) as conn:
        return [
            {
                "id": row["id"],
                "memory_type": row["memory_type"],
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
                WHERE status IN ('proposed', 'active')
                ORDER BY updated_at DESC, created_at DESC
                """,
            )
        ]


def lineage_memory_prompt(clusters: list[dict[str, Any]], existing: list[dict[str, Any]]) -> str:
    return (
        "You propose reviewed personal-knowledge memories from repeated lineage signals.\n"
        "Only propose durable, actionable memories: project conventions, environment facts, recurring failure patterns, "
        "workflow preferences, or architectural decisions.\n"
        "Never propose raw logs, generic advice, one-off task summaries, command spam, or unsupported claims.\n"
        "Every memory stays pending human review; do not imply it is approved truth.\n"
        "Use only the provided cluster evidence and include the cluster_id you used.\n"
        "Return only JSON with this shape: "
        "{\"memories\": [{\"cluster_id\": str, \"memory_type\": str, \"scope\": str, \"content\": str, "
        "\"source_ids\": [str], \"rationale\": str, \"confidence\": number}]}.\n\n"
        f"Existing active/proposed memories:\n{json.dumps(existing, indent=2)}\n\n"
        f"Eligible lineage clusters:\n{json.dumps(clusters, indent=2)}"
    )


def durable_memory_content(content: str) -> bool:
    if not content or len(content.split()) < 5:
        return False
    lowered = content.lower()
    banned = ["raw log", "command spam", "one-off", "single session"]
    return not any(term in lowered for term in banned)


def record_memory_proposed_from_lineage(paths: BrainPaths, memory_id: str, cluster: dict[str, Any], rationale: str) -> None:
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO context_lineage_events(
              id, target_type, target_id, event_type, retrieval_event_id,
              agent_session_id, query, weight, metadata, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("lineage"),
                "memory",
                memory_id,
                "memory_proposed_from_lineage",
                None,
                None,
                None,
                0.0,
                dumps({"cluster": cluster, "rationale": rationale}),
                now_iso(),
            ),
        )


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


def dedupe_strings(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


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
