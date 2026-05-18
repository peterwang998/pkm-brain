from __future__ import annotations

from typing import Any

from .db import loads, rows, connection
from .paths import BrainPaths


VALID_MEMORY_TYPES = {
    "PreferenceMemory",
    "ProjectMemory",
    "DecisionMemory",
    "BehaviorMemory",
    "RepoInstructionMemory",
    "OpenLoopMemory",
    "FactMemory",
    "AgentFailurePatternMemory",
}
VALID_MEMORY_STATUSES = {"proposed", "active", "superseded", "rejected", "archived"}
VALID_SCOPE_PREFIXES = ("global", "project:", "repo:", "agent:", "topic:")


def audit_memories(paths: BrainPaths) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    with connection(paths.sqlite_path) as conn:
        memories = rows(conn, "SELECT * FROM memories ORDER BY created_at DESC")
        for memory in memories:
            mid = memory["id"]
            if memory["memory_type"] not in VALID_MEMORY_TYPES:
                errors.append(f"{mid}: invalid memory_type {memory['memory_type']}")
            if memory["status"] not in VALID_MEMORY_STATUSES:
                errors.append(f"{mid}: invalid status {memory['status']}")
            if not any(str(memory["scope"]).startswith(prefix) for prefix in VALID_SCOPE_PREFIXES):
                errors.append(f"{mid}: invalid scope {memory['scope']}")
            if not loads(memory["source_ids"], []):
                warnings.append(f"{mid}: missing source_ids")
            if memory["confidence"] is None:
                errors.append(f"{mid}: missing confidence")
    return {"memories": len(memories), "errors": errors, "warnings": warnings}


def provenance_check(paths: BrainPaths) -> dict[str, Any]:
    errors: list[str] = []
    with connection(paths.sqlite_path) as conn:
        chunk_orphans = rows(
            conn,
            """
            SELECT c.id FROM chunks c
            LEFT JOIN documents d ON d.id = c.document_id
            WHERE d.id IS NULL
            """,
        )
        for row in chunk_orphans:
            errors.append(f"chunk {row['id']} has no valid document")
        retrievals = rows(conn, "SELECT id, cited_chunk_ids FROM retrieval_events")
        valid_chunks = {row["id"] for row in rows(conn, "SELECT id FROM chunks")}
        for event in retrievals:
            for chunk_id in loads(event["cited_chunk_ids"], []):
                if chunk_id not in valid_chunks:
                    errors.append(f"retrieval {event['id']} cites missing chunk {chunk_id}")
    return {"errors": errors}
