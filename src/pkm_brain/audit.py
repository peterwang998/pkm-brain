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
    "BusinessIdeaMemory",
    "PersonalLogisticsMemory",
}
VALID_MEMORY_STATUSES = {"proposed", "active", "superseded", "rejected", "archived"}
VALID_SCOPE_PREFIXES = ("global", "project:", "repo:", "agent:", "topic:", "user:")
INACTIVE_MEMORY_STATUSES = {"superseded", "rejected", "archived"}


def audit_memories(paths: BrainPaths) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    with connection(paths.sqlite_path) as conn:
        memories = rows(conn, "SELECT * FROM memories ORDER BY created_at DESC")
        for memory in memories:
            mid = memory["id"]
            memory_type = str(memory["memory_type"])
            scope = str(memory["scope"])
            status = str(memory["status"])
            if memory_type not in VALID_MEMORY_TYPES:
                append_memory_schema_issue(
                    errors,
                    warnings,
                    status,
                    f"{mid}: invalid memory_type {memory_type}",
                )
            if status not in VALID_MEMORY_STATUSES:
                errors.append(f"{mid}: invalid status {status}")
            if not any(scope.startswith(prefix) for prefix in VALID_SCOPE_PREFIXES):
                append_memory_schema_issue(
                    errors,
                    warnings,
                    status,
                    f"{mid}: invalid scope {scope}",
                )
            if not loads(memory["source_ids"], []):
                warnings.append(f"{mid}: missing source_ids")
            if memory["confidence"] is None:
                errors.append(f"{mid}: missing confidence")
    return {"memories": len(memories), "errors": errors, "warnings": warnings}


def append_memory_schema_issue(
    errors: list[str],
    warnings: list[str],
    status: str,
    message: str,
) -> None:
    if status in INACTIVE_MEMORY_STATUSES:
        warnings.append(message)
        return
    errors.append(message)


def provenance_check(paths: BrainPaths) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
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
        retrievals = rows(conn, "SELECT id, citation_snapshots FROM retrieval_events")
        current_chunks = rows(
            conn,
            """
            SELECT c.id, c.content_hash, d.logical_source_key
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            """,
        )
        valid_chunks = {row["id"] for row in current_chunks}
        current_chunk_keys = {
            (row["logical_source_key"], row["content_hash"])
            for row in current_chunks
            if row["logical_source_key"] and row["content_hash"]
        }
        for event in retrievals:
            try:
                snapshots = loads(event["citation_snapshots"], [])
            except Exception as exc:
                errors.append(f"retrieval {event['id']} has malformed citation_snapshots: {exc}")
                continue
            if not isinstance(snapshots, list):
                errors.append(f"retrieval {event['id']} has malformed citation_snapshots: expected list")
                continue
            for index, snapshot in enumerate(snapshots):
                if not isinstance(snapshot, dict):
                    errors.append(f"retrieval {event['id']} snapshot {index} is malformed: expected object")
                    continue
                snapshot_type = snapshot.get("type")
                if snapshot_type == "chunk":
                    required = ["text", "document_id", "logical_source_key", "content_hash"]
                    missing = [key for key in required if not snapshot.get(key)]
                    if missing:
                        errors.append(
                            f"retrieval {event['id']} chunk snapshot {index} missing {', '.join(missing)}"
                        )
                        continue
                    chunk_id = str(snapshot.get("chunk_id") or "")
                    if chunk_id and chunk_id not in valid_chunks:
                        warnings.append(f"retrieval {event['id']} snapshot chunk {chunk_id} no longer exists")
                    key = (snapshot.get("logical_source_key"), snapshot.get("content_hash"))
                    if key not in current_chunk_keys:
                        warnings.append(
                            f"retrieval {event['id']} chunk snapshot {index} no longer matches a current chunk"
                        )
                elif snapshot_type == "wiki_page":
                    if not snapshot.get("relative_path") or not snapshot.get("title"):
                        errors.append(f"retrieval {event['id']} wiki_page snapshot {index} is malformed")
                    if not isinstance(snapshot.get("source_ids", []), list):
                        errors.append(f"retrieval {event['id']} wiki_page snapshot {index} has non-list source_ids")
                elif snapshot_type == "fact":
                    required = ["fact_id", "statement"]
                    missing = [key for key in required if not snapshot.get(key)]
                    if missing:
                        errors.append(
                            f"retrieval {event['id']} fact snapshot {index} missing {', '.join(missing)}"
                        )
                    if not isinstance(snapshot.get("source_ids", []), list):
                        errors.append(f"retrieval {event['id']} fact snapshot {index} has non-list source_ids")
                    if not isinstance(snapshot.get("source_spans", []), list):
                        errors.append(f"retrieval {event['id']} fact snapshot {index} has non-list source_spans")
                else:
                    errors.append(f"retrieval {event['id']} snapshot {index} has unknown type {snapshot_type!r}")
    return {"errors": errors, "warnings": warnings}
