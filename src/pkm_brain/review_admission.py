from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


DEFAULT_ACTIVE_LIMIT = 100
DEFAULT_DAILY_ADMISSION_LIMIT = 25


def reconcile_review_admissions(
    conn: Any,
    items: list[dict[str, Any]],
    *,
    active_limit: int = DEFAULT_ACTIVE_LIMIT,
    daily_limit: int = DEFAULT_DAILY_ADMISSION_LIMIT,
    now: str | None = None,
) -> dict[str, Any]:
    """Keep ordinary human review bounded while preserving hard safety gates."""
    timestamp = now or datetime.now(timezone.utc).isoformat(timespec="microseconds")
    day = timestamp[:10]
    candidates = {
        str(item["id"]): item
        for item in items
        if item.get("approvable") is True and str(item.get("id") or "").strip()
    }
    initialized = conn.execute(
        "SELECT initialized_at FROM review_admission_meta WHERE singleton = 1"
    ).fetchone()
    if initialized is None:
        conn.execute(
            """
            INSERT OR IGNORE INTO review_admission_meta(singleton, initialized_at)
            VALUES (1, ?)
            """,
            (timestamp,),
        )
        for key, item in candidates.items():
            insert_admission(
                conn,
                key,
                item,
                state="admitted",
                reason="existing_backlog",
                timestamp=timestamp,
            )

    rows = admission_rows(conn, candidates)
    for key, item in candidates.items():
        if key in rows:
            update_admission_metadata(conn, key, item, timestamp)

    active_keys = {
        key for key, row in rows.items() if str(row["state"]) == "admitted"
    }
    budget_used = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM review_admissions
            WHERE admission_reason = 'daily_budget'
              AND substr(admitted_at, 1, 10) = ?
            """,
            (day,),
        ).fetchone()[0]
    )

    missing = [item for key, item in candidates.items() if key not in rows]
    for item in sorted(missing, key=admission_sort_key):
        key = str(item["id"])
        if is_hard_boundary(item):
            state = "admitted"
            reason = "hard_boundary"
        elif len(active_keys) < active_limit and budget_used < daily_limit:
            state = "admitted"
            reason = "daily_budget"
            budget_used += 1
        else:
            state = "deferred"
            reason = "capacity"
        insert_admission(
            conn,
            key,
            item,
            state=state,
            reason=reason,
            timestamp=timestamp,
        )
        if state == "admitted":
            active_keys.add(key)

    rows = admission_rows(conn, candidates)
    deferred = [
        candidates[key]
        for key, row in rows.items()
        if key in candidates and str(row["state"]) == "deferred"
    ]
    for item in sorted(deferred, key=admission_sort_key):
        if len(active_keys) >= active_limit or budget_used >= daily_limit:
            break
        key = str(item["id"])
        conn.execute(
            """
            UPDATE review_admissions
            SET state = 'admitted', admission_reason = 'daily_budget',
                admitted_at = ?, updated_at = ?
            WHERE item_key = ? AND state = 'deferred'
            """,
            (timestamp, timestamp, key),
        )
        active_keys.add(key)
        budget_used += 1

    states = {
        key: str(row["state"])
        for key, row in admission_rows(conn, candidates).items()
    }
    return {
        "states": states,
        "active_limit": active_limit,
        "daily_limit": daily_limit,
        "admitted_today": budget_used,
    }


def load_review_admission_states(
    conn: Any, item_ids: list[str] | set[str]
) -> dict[str, str]:
    keys = sorted({str(value) for value in item_ids if str(value).strip()})
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    return {
        str(row["item_key"]): str(row["state"])
        for row in conn.execute(
            f"""
            SELECT item_key, state
            FROM review_admissions
            WHERE item_key IN ({placeholders})
            """,
            keys,
        )
    }


def admission_rows(conn: Any, candidates: dict[str, Any]) -> dict[str, Any]:
    keys = sorted(candidates)
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    return {
        str(row["item_key"]): row
        for row in conn.execute(
            f"SELECT * FROM review_admissions WHERE item_key IN ({placeholders})",
            keys,
        )
    }


def insert_admission(
    conn: Any,
    key: str,
    item: dict[str, Any],
    *,
    state: str,
    reason: str,
    timestamp: str,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO review_admissions(
          item_key, item_kind, group_name, state, priority,
          admission_reason, first_seen_at, admitted_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            key,
            str(item.get("kind") or "other"),
            str(item.get("group") or "other"),
            state,
            admission_priority(item),
            reason,
            timestamp,
            timestamp if state == "admitted" else None,
            timestamp,
        ),
    )


def update_admission_metadata(
    conn: Any, key: str, item: dict[str, Any], timestamp: str
) -> None:
    conn.execute(
        """
        UPDATE review_admissions
        SET item_kind = ?, group_name = ?, priority = ?, updated_at = ?
        WHERE item_key = ?
        """,
        (
            str(item.get("kind") or "other"),
            str(item.get("group") or "other"),
            admission_priority(item),
            timestamp,
            key,
        ),
    )


def is_hard_boundary(item: dict[str, Any]) -> bool:
    return str(item.get("group") or "") == "conflicts" or str(
        item.get("risk_tier") or ""
    ).lower() == "high"


def admission_priority(item: dict[str, Any]) -> int:
    groups = {
        "conflicts": 0,
        "unrouted": 1,
        "audit": 2,
        "topology": 3,
        "policy": 4,
        "anomalies": 5,
        "memories": 6,
    }
    risks = {"high": 0, "medium": 1, "low": 2}
    group = groups.get(str(item.get("group") or ""), 9)
    risk = risks.get(str(item.get("risk_tier") or "").lower(), 3)
    return group * 10 + risk


def admission_sort_key(item: dict[str, Any]) -> tuple[int, str, str]:
    return (
        admission_priority(item),
        str(item.get("created_at") or ""),
        str(item.get("id") or ""),
    )
