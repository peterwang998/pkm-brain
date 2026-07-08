from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any

from .db import loads, rows, connection
from .paths import BrainPaths
from .util import now_iso


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
VALID_EXACT_MEMORY_SCOPES = {"global"}
VALID_SCOPE_PREFIXES = ("project:", "repo:", "agent:", "topic:", "user:")
INACTIVE_MEMORY_STATUSES = {"superseded", "rejected", "archived"}
STALE_ACTIVE_MEMORY_DAYS = 180


def valid_memory_scope(scope: str) -> bool:
    value = scope.strip()
    if value in VALID_EXACT_MEMORY_SCOPES:
        return True
    return any(value.startswith(prefix) and len(value) > len(prefix) for prefix in VALID_SCOPE_PREFIXES)


def audit_memories(paths: BrainPaths, *, stale_after_days: int = STALE_ACTIVE_MEMORY_DAYS) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    active_by_normalized_content: dict[tuple[str, str, str], list[str]] = {}
    now = parse_audit_timestamp(now_iso()) or datetime.now(timezone.utc)
    stale_before = now - timedelta(days=stale_after_days)
    with connection(paths.sqlite_path) as conn:
        memories = rows(conn, "SELECT * FROM memories ORDER BY created_at DESC")
        valid_sources = load_memory_source_indexes(conn)
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
            if not valid_memory_scope(scope):
                append_memory_schema_issue(
                    errors,
                    warnings,
                    status,
                    f"{mid}: invalid scope {scope}",
                )
            source_ids = memory_source_ids(memory, errors)
            if not source_ids:
                warnings.append(f"{mid}: missing source_ids")
            warnings.extend(unresolved_source_warnings(mid, source_ids, valid_sources))
            if memory["confidence"] is None:
                errors.append(f"{mid}: missing confidence")
            if status == "active":
                normalized_content = normalize_memory_content(str(memory["content"] or ""))
                if normalized_content:
                    key = (memory_type, scope, normalized_content)
                    active_by_normalized_content.setdefault(key, []).append(mid)
                checked_at = parse_audit_timestamp(str(memory["last_seen_at"] or memory["updated_at"] or ""))
                if checked_at and checked_at < stale_before:
                    warnings.append(
                        f"{mid}: active memory has not been seen or updated for {stale_after_days}+ days"
                    )
            if status == "superseded":
                if not memory["reviewed_at"]:
                    warnings.append(f"{mid}: superseded memory missing reviewed_at")
                if not str(memory["review_reason"] or "").strip():
                    warnings.append(f"{mid}: superseded memory missing review_reason")
        for key, memory_ids in sorted(active_by_normalized_content.items()):
            if len(memory_ids) > 1:
                memory_type, scope, _content = key
                warnings.append(
                    f"duplicate active memories for {memory_type} in {scope}: {', '.join(sorted(memory_ids))}"
                )
    return {
        "memories": len(memories),
        "errors": errors,
        "warnings": warnings,
        "checks": {
            "stale_after_days": stale_after_days,
            "duplicate_active_content": True,
            "unresolved_source_ids": True,
            "superseded_review_metadata": True,
        },
    }


def memory_source_ids(memory: Any, errors: list[str]) -> list[str]:
    try:
        parsed = loads(memory["source_ids"], [])
    except Exception as exc:
        errors.append(f"{memory['id']}: malformed source_ids: {exc}")
        return []
    if not isinstance(parsed, list):
        errors.append(f"{memory['id']}: source_ids must be a list")
        return []
    return [str(item) for item in parsed if str(item or "").strip()]


def load_memory_source_indexes(conn: Any) -> dict[str, set[str]]:
    indexes: dict[str, set[str]] = {
        "document": set(),
        "chunk": set(),
        "retrieval_event": set(),
    }
    if audit_table_exists(conn, "documents"):
        indexes["document"] = {str(row["id"]) for row in conn.execute("SELECT id FROM documents")}
    if audit_table_exists(conn, "chunks"):
        indexes["chunk"] = {str(row["id"]) for row in conn.execute("SELECT id FROM chunks")}
    if audit_table_exists(conn, "retrieval_events"):
        indexes["retrieval_event"] = {
            str(row["id"]) for row in conn.execute("SELECT id FROM retrieval_events")
        }
    return indexes


def unresolved_source_warnings(
    memory_id: str,
    source_ids: list[str],
    valid_sources: dict[str, set[str]],
) -> list[str]:
    warnings: list[str] = []
    for source_id in source_ids:
        prefix, _, value = source_id.partition(":")
        if prefix not in valid_sources:
            continue
        if value and value not in valid_sources[prefix]:
            warnings.append(f"{memory_id}: unresolved source_id {source_id}")
    return warnings


def normalize_memory_content(content: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", content.lower()))


def parse_audit_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def audit_table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    )


def append_memory_schema_issue(
    errors: list[str],
    warnings: list[str],
    status: str,
    message: str,
) -> None:
    if status == "proposed" or status in INACTIVE_MEMORY_STATUSES:
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
