from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .operational_db import operational_connection
from .util import new_id, now_iso


CALENDAR_SERIES_RULE_KIND = "recurring_series"


def suppress_calendar_series(
    db_path: Path,
    item_id: str,
    *,
    reason: str = "Hidden by the operator as a recurring non-meeting.",
    updated_at: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Hide every occurrence of one Calendar series without editing Calendar."""

    normalized_item_id = item_id.strip()
    normalized_reason = reason.strip()
    if not normalized_item_id:
        raise ValueError("calendar item id is required")
    if not normalized_reason or len(normalized_reason) > 2000:
        raise ValueError("calendar-series suppression reason is invalid")
    timestamp = _canonical_timestamp(updated_at or now_iso())
    effective_as_of = _canonical_timestamp(as_of or timestamp)
    with operational_connection(db_path, write=True) as conn:
        row = conn.execute(
            """
            SELECT id, source_type, account_key, item_kind, title, metadata
            FROM ops_items
            WHERE id = ?
            """,
            (normalized_item_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"operational item not found: {normalized_item_id}")
        if str(row["source_type"]) != "calendar" or str(row["item_kind"]) != "event":
            raise ValueError("only recurring Calendar events can be hidden as a series")
        metadata = json.loads(str(row["metadata"]))
        series_key = str(metadata.get("recurring_event_id") or "").strip()
        if not series_key:
            raise ValueError("this Calendar event is not part of a recurring series")
        account_key = str(row["account_key"])
        existing = conn.execute(
            """
            SELECT * FROM ops_suppression_rules
            WHERE source_type = 'calendar' AND account_key = ?
              AND rule_kind = ? AND match_key = ?
            """,
            (account_key, CALENDAR_SERIES_RULE_KIND, series_key),
        ).fetchone()
        if existing is None:
            rule_id = new_id("opssuppress")
            conn.execute(
                """
                INSERT INTO ops_suppression_rules(
                  id, source_type, account_key, rule_kind, match_key, label,
                  reason, active, created_at, updated_at, restored_at
                ) VALUES (?, 'calendar', ?, ?, ?, ?, ?, 1, ?, ?, NULL)
                """,
                (
                    rule_id,
                    account_key,
                    CALENDAR_SERIES_RULE_KIND,
                    series_key,
                    str(row["title"])[:500],
                    normalized_reason,
                    timestamp,
                    timestamp,
                ),
            )
            idempotent = False
        else:
            rule_id = str(existing["id"])
            idempotent = bool(existing["active"])
            if not idempotent:
                conn.execute(
                    """
                    UPDATE ops_suppression_rules
                    SET active = 1, label = ?, reason = ?, updated_at = ?,
                        restored_at = NULL
                    WHERE id = ?
                    """,
                    (str(row["title"])[:500], normalized_reason, timestamp, rule_id),
                )
        output = _rule_with_count(conn, rule_id, as_of=effective_as_of)
    return {**output, "idempotent": idempotent}


def restore_calendar_series(
    db_path: Path,
    rule_id: str,
    *,
    updated_at: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    normalized = rule_id.strip()
    if not normalized:
        raise ValueError("calendar-series suppression id is required")
    timestamp = _canonical_timestamp(updated_at or now_iso())
    effective_as_of = _canonical_timestamp(as_of or timestamp)
    with operational_connection(db_path, write=True) as conn:
        row = conn.execute(
            """
            SELECT * FROM ops_suppression_rules
            WHERE id = ? AND source_type = 'calendar' AND rule_kind = ?
            """,
            (normalized, CALENDAR_SERIES_RULE_KIND),
        ).fetchone()
        if row is None:
            raise ValueError(f"calendar-series suppression not found: {normalized}")
        idempotent = not bool(row["active"])
        if not idempotent:
            conn.execute(
                """
                UPDATE ops_suppression_rules
                SET active = 0, restored_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, normalized),
            )
        output = _rule_with_count(conn, normalized, as_of=effective_as_of)
    return {**output, "idempotent": idempotent}


def list_calendar_series_suppressions(
    db_path: Path,
    *,
    active_only: bool = True,
    as_of: str | None = None,
) -> list[dict[str, Any]]:
    effective_as_of = _canonical_timestamp(as_of or now_iso())
    with operational_connection(db_path, write=False) as conn:
        rows = conn.execute(
            """
            SELECT r.*,
                   COUNT(i.id) AS hidden_count,
                   MIN(CASE WHEN i.starts_at >= ? THEN i.starts_at END) AS next_starts_at
            FROM ops_suppression_rules r
            LEFT JOIN ops_items i
              ON i.source_type = 'calendar'
             AND i.account_key = r.account_key
             AND json_extract(i.metadata, '$.recurring_event_id') = r.match_key
             AND i.state = 'active'
             AND COALESCE(i.ends_at, i.starts_at) >= ?
            WHERE r.source_type = 'calendar' AND r.rule_kind = ?
              AND (? = 0 OR r.active = 1)
            GROUP BY r.id
            ORDER BY r.updated_at DESC, r.id
            """,
            (
                effective_as_of,
                effective_as_of,
                CALENDAR_SERIES_RULE_KIND,
                int(active_only),
            ),
        ).fetchall()
    return [_rule_row(row) for row in rows]


def active_calendar_series_keys(db_path: Path) -> set[tuple[str, str]]:
    """Return (account_key, recurring_event_id) pairs hidden from Today."""

    with operational_connection(db_path, write=False) as conn:
        rows = conn.execute(
            """
            SELECT account_key, match_key
            FROM ops_suppression_rules
            WHERE source_type = 'calendar' AND rule_kind = ? AND active = 1
            """,
            (CALENDAR_SERIES_RULE_KIND,),
        ).fetchall()
    return {(str(row["account_key"]), str(row["match_key"])) for row in rows}


def calendar_card_is_series_suppressed(
    card: Mapping[str, Any],
    active_keys: set[tuple[str, str]],
) -> bool:
    series_key = str(card.get("recurring_event_id") or "").strip()
    return bool(series_key) and (str(card.get("account_key") or ""), series_key) in active_keys


def _rule_with_count(conn: Any, rule_id: str, *, as_of: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT r.*,
               COUNT(i.id) AS hidden_count,
               MIN(CASE WHEN i.starts_at >= ? THEN i.starts_at END) AS next_starts_at
        FROM ops_suppression_rules r
        LEFT JOIN ops_items i
          ON i.source_type = 'calendar'
         AND i.account_key = r.account_key
         AND json_extract(i.metadata, '$.recurring_event_id') = r.match_key
         AND i.state = 'active'
         AND COALESCE(i.ends_at, i.starts_at) >= ?
        WHERE r.id = ?
        GROUP BY r.id
        """,
        (as_of, as_of, rule_id),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"suppression rule disappeared: {rule_id}")
    return _rule_row(row)


def _rule_row(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "source_type": str(row["source_type"]),
        "account_key": str(row["account_key"]),
        "rule_kind": str(row["rule_kind"]),
        "match_key": str(row["match_key"]),
        "label": str(row["label"]),
        "reason": str(row["reason"]),
        "active": bool(row["active"]),
        "hidden_count": max(0, int(row["hidden_count"] or 0)),
        "next_starts_at": row["next_starts_at"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "restored_at": row["restored_at"],
    }


def _canonical_timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("calendar-series suppression timestamps require a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()
