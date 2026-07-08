from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .db import connection
from .paths import BrainPaths
from .util import now_iso


TOPOLOGY_ACTION_TYPES = {
    "page_merge",
    "page_split",
    "rename_page",
    "archive_page",
    "rehome_fact",
    "edit_contract",
    "entity_merge",
    "entity_split",
    "synthesize_page",
}


def review_queue_summary(paths: BrainPaths) -> dict[str, Any]:
    generated_at = now_iso()
    now = parse_timestamp(generated_at) or datetime.now(timezone.utc)
    buckets: dict[str, dict[str, Any]] = {}
    raw: dict[str, int] = {}
    raw_buckets: dict[str, dict[str, Any]] = {}
    with connection(paths.sqlite_path) as conn:
        if table_exists(conn, "open_questions"):
            for row in conn.execute(
                """
                SELECT kind, COUNT(*) AS count, MIN(created_at) AS oldest_created_at
                FROM open_questions
                WHERE status IN ('open', 'needs_human')
                GROUP BY kind
                """
            ):
                raw_kind = str(row["kind"] or "other")
                group = queue_group_for_kind(raw_kind)
                entry = bucket_entry(int(row["count"]), row["oldest_created_at"], now)
                merge_bucket(buckets, group, entry)
                raw[raw_kind] = int(row["count"])
                raw_buckets[raw_kind] = entry
        if table_exists(conn, "memories"):
            row = conn.execute(
                """
                SELECT COUNT(*) AS count, MIN(created_at) AS oldest_created_at
                FROM memories
                WHERE status = 'proposed'
                """
            ).fetchone()
            add_count_bucket(buckets, raw, raw_buckets, "proposed_memory", row, now)
        if table_exists(conn, "cos_actions"):
            question_action_ids = active_question_action_ids(conn)
            action_row = proposed_topology_action_summary(conn, question_action_ids)
            add_count_bucket(buckets, raw, raw_buckets, "proposed_action", action_row, now)
            audit_row = audit_flagged_action_summary(conn)
            add_count_bucket(buckets, raw, raw_buckets, "audit_flagged", audit_row, now)
    by_kind = {kind: int(bucket["count"]) for kind, bucket in sorted(buckets.items())}
    return {
        "generated_at": generated_at,
        "total": sum(by_kind.values()),
        "by_kind": by_kind,
        "raw": dict(sorted(raw.items())),
        "buckets": dict(sorted(buckets.items())),
        "raw_buckets": dict(sorted(raw_buckets.items())),
    }


def add_count_bucket(
    buckets: dict[str, dict[str, Any]],
    raw: dict[str, int],
    raw_buckets: dict[str, dict[str, Any]],
    raw_kind: str,
    row: Any,
    now: datetime,
) -> None:
    count = int(row["count"] if row else 0)
    if count <= 0:
        return
    group = queue_group_for_kind(raw_kind)
    entry = bucket_entry(count, row["oldest_created_at"], now)
    merge_bucket(buckets, group, entry)
    raw[raw_kind] = count
    raw_buckets[raw_kind] = entry


def bucket_entry(count: int, oldest_created_at: Any, now: datetime) -> dict[str, Any]:
    oldest = str(oldest_created_at or "")
    parsed = parse_timestamp(oldest)
    age_hours = None
    if parsed:
        age_hours = round((now - parsed).total_seconds() / 3600, 2)
    return {
        "count": count,
        "oldest_created_at": oldest or None,
        "oldest_age_hours": age_hours,
    }


def merge_bucket(buckets: dict[str, dict[str, Any]], group: str, entry: dict[str, Any]) -> None:
    existing = buckets.get(group)
    if not existing:
        buckets[group] = dict(entry)
        return
    existing["count"] = int(existing["count"]) + int(entry["count"])
    existing_oldest = parse_timestamp(str(existing.get("oldest_created_at") or ""))
    entry_oldest = parse_timestamp(str(entry.get("oldest_created_at") or ""))
    if entry_oldest and (not existing_oldest or entry_oldest < existing_oldest):
        existing["oldest_created_at"] = entry["oldest_created_at"]
        existing["oldest_age_hours"] = entry["oldest_age_hours"]


def queue_group_for_kind(kind: str, action_type: str | None = None) -> str:
    normalized = str(kind or "").strip()
    if normalized in {"fact_conflict_review", "conflict"}:
        return "conflicts"
    if normalized == "unrouted_fact":
        return "unrouted"
    if normalized == "document_extraction_anomaly":
        return "anomalies"
    if normalized == "proposed_memory":
        return "memories"
    if normalized == "audit_flagged":
        return "audit"
    if normalized == "proposed_action":
        return "topology"
    if normalized == "policy_escalation" and action_type in TOPOLOGY_ACTION_TYPES:
        return "topology"
    return normalized or "other"


def active_question_action_ids(conn: Any) -> set[str]:
    if not table_exists(conn, "open_questions") or not column_exists(conn, "open_questions", "action_id"):
        return set()
    return {
        str(row["action_id"])
        for row in conn.execute(
            """
            SELECT action_id
            FROM open_questions
            WHERE action_id IS NOT NULL
              AND status IN ('open', 'needs_human')
            """
        )
        if str(row["action_id"] or "").strip()
    }


def proposed_topology_action_summary(conn: Any, exclude_action_ids: set[str]) -> Any:
    placeholders = ",".join("?" for _ in exclude_action_ids)
    exclude = f"AND id NOT IN ({placeholders})" if exclude_action_ids else ""
    params: list[Any] = list(exclude_action_ids)
    return conn.execute(
        f"""
        SELECT COUNT(*) AS count, MIN(created_at) AS oldest_created_at
        FROM cos_actions
        WHERE status IN ('proposed', 'needs_human')
          AND action_type IN ({",".join("?" for _ in TOPOLOGY_ACTION_TYPES)})
          {exclude}
        """,
        [*TOPOLOGY_ACTION_TYPES, *params],
    ).fetchone()


def audit_flagged_action_summary(conn: Any) -> Any:
    return conn.execute(
        """
        SELECT COUNT(*) AS count, MIN(COALESCE(applied_at, created_at)) AS oldest_created_at
        FROM cos_actions
        WHERE audit_status = 'sampled_bad'
          AND status NOT IN ('reverted', 'rejected', 'dismissed')
        """
    ).fetchone()


def table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    )


def column_exists(conn: Any, table: str, column: str) -> bool:
    return any(row["name"] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
