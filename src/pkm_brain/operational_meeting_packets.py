from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .operational_db import OperationalStoreUnavailableError, operational_connection
from .util import new_id, now_iso


MAX_MEETING_PACKET_BYTES = 262_144
DEFAULT_MEETING_PACKET_RETENTION_DAYS = 30
DEFAULT_MEETING_PACKET_HORIZON_HOURS = 72
MEETING_PACKET_CONTENT_VERSION = "executive-brief-v2"


def load_current_meeting_packet(
    db_path: Path,
    item_id: str,
) -> dict[str, Any] | None:
    """Load a prepared packet only when it matches the event's current revision."""

    with operational_connection(db_path, write=False) as conn:
        row = conn.execute(
            """
            SELECT p.packet, p.generated_at, p.id
            FROM ops_meeting_packets p
            JOIN ops_items i ON i.id = p.item_id
            JOIN ops_observations o ON o.id = i.current_observation_id
            WHERE p.item_id = ? AND p.source_revision = o.source_revision
              AND p.expires_at > ?
              AND json_extract(p.packet, '$.content_version') = ?
            ORDER BY p.generated_at DESC, p.id DESC
            LIMIT 1
            """,
            (item_id, now_iso(), MEETING_PACKET_CONTENT_VERSION),
        ).fetchone()
    if row is None:
        return None
    packet = json.loads(str(row["packet"]))
    packet["prepared_in_advance"] = True
    packet["prepared_at"] = str(row["generated_at"])
    return packet


def save_meeting_packet(
    db_path: Path,
    item_id: str,
    packet: Mapping[str, Any],
    *,
    generated_at: str | None = None,
    retention_days: int = DEFAULT_MEETING_PACKET_RETENTION_DAYS,
) -> dict[str, Any]:
    if retention_days < 1 or retention_days > 365:
        raise ValueError("meeting packet retention_days must be between 1 and 365")
    timestamp = _canonical_timestamp(generated_at or now_iso())
    expires_at = (timestamp + timedelta(days=retention_days)).isoformat()
    canonical_packet = dict(packet)
    canonical_packet.pop("prepared_in_advance", None)
    canonical_packet.pop("prepared_at", None)
    encoded = json.dumps(
        canonical_packet,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded.encode("utf-8")) > MAX_MEETING_PACKET_BYTES:
        raise ValueError("meeting packet exceeds the bounded cache payload")
    packet_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    with operational_connection(db_path, write=True) as conn:
        item = conn.execute(
            """
            SELECT i.id, i.source_type, i.item_kind, o.source_revision
            FROM ops_items i
            JOIN ops_observations o ON o.id = i.current_observation_id
            WHERE i.id = ?
            """,
            (item_id,),
        ).fetchone()
        if item is None:
            raise ValueError(f"operational item not found: {item_id}")
        if str(item["source_type"]) != "calendar" or str(item["item_kind"]) != "event":
            raise ValueError("meeting packets may only be cached for Calendar events")
        source_revision = str(item["source_revision"])
        existing = conn.execute(
            """
            SELECT * FROM ops_meeting_packets
            WHERE item_id = ? AND source_revision = ? AND packet_hash = ?
            """,
            (item_id, source_revision, packet_hash),
        ).fetchone()
        if existing is not None:
            return {**dict(existing), "idempotent": True}
        packet_id = new_id("opsmeeting")
        conn.execute(
            """
            INSERT INTO ops_meeting_packets(
              id, item_id, source_revision, generated_at, packet, packet_hash,
              expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                packet_id,
                item_id,
                source_revision,
                timestamp.isoformat(),
                encoded,
                packet_hash,
                expires_at,
                timestamp.isoformat(),
            ),
        )
        row = conn.execute(
            "SELECT * FROM ops_meeting_packets WHERE id = ?",
            (packet_id,),
        ).fetchone()
    assert row is not None
    return {**dict(row), "idempotent": False}


def list_meetings_needing_packets(
    db_path: Path,
    *,
    as_of: str | None = None,
    horizon_hours: int = DEFAULT_MEETING_PACKET_HORIZON_HOURS,
    limit: int = 20,
) -> list[str]:
    if not 1 <= horizon_hours <= 168:
        raise ValueError("meeting packet horizon_hours must be between 1 and 168")
    if not 1 <= limit <= 100:
        raise ValueError("meeting packet limit must be between 1 and 100")
    current = _canonical_timestamp(as_of or now_iso())
    horizon = current + timedelta(hours=horizon_hours)
    with operational_connection(db_path, write=False) as conn:
        rows = conn.execute(
            """
            SELECT i.id
            FROM ops_items i
            JOIN ops_observations o ON o.id = i.current_observation_id
            LEFT JOIN ops_meeting_packets p
              ON p.item_id = i.id
             AND p.source_revision = o.source_revision
             AND p.expires_at > ?
             AND json_extract(p.packet, '$.content_version') = ?
            LEFT JOIN ops_suppression_rules r
              ON r.source_type = 'calendar'
             AND r.account_key = i.account_key
             AND r.rule_kind = 'recurring_series'
             AND r.match_key = json_extract(i.metadata, '$.recurring_event_id')
             AND r.active = 1
            WHERE i.source_type = 'calendar' AND i.item_kind = 'event'
              AND i.state = 'active' AND i.starts_at IS NOT NULL
              AND COALESCE(json_extract(i.metadata, '$.all_day'), 0) != 1
              AND LOWER(TRIM(COALESCE(
                    json_extract(i.metadata, '$.transparency'), ''
                  ))) != 'transparent'
              AND NOT (
                    LOWER(TRIM(i.title)) = 'family time'
                    OR LOWER(TRIM(i.title)) GLOB 'family time[ :–—-]*'
                  )
              AND i.starts_at <= ?
              AND COALESCE(i.ends_at, i.starts_at) >= ?
              AND p.id IS NULL AND r.id IS NULL
            ORDER BY i.starts_at, i.id
            LIMIT ?
            """,
            (
                current.isoformat(),
                MEETING_PACKET_CONTENT_VERSION,
                horizon.isoformat(),
                current.isoformat(),
                limit,
            ),
        ).fetchall()
    return [str(row["id"]) for row in rows]


def meeting_packet_readiness(db_path: Path) -> set[str]:
    current = now_iso()
    with operational_connection(db_path, write=False) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT p.item_id
            FROM ops_meeting_packets p
            JOIN ops_items i ON i.id = p.item_id
            JOIN ops_observations o ON o.id = i.current_observation_id
            WHERE p.source_revision = o.source_revision AND p.expires_at > ?
              AND json_extract(p.packet, '$.content_version') = ?
              AND COALESCE(json_extract(i.metadata, '$.all_day'), 0) != 1
              AND LOWER(TRIM(COALESCE(
                    json_extract(i.metadata, '$.transparency'), ''
                  ))) != 'transparent'
              AND NOT (
                    LOWER(TRIM(i.title)) = 'family time'
                    OR LOWER(TRIM(i.title)) GLOB 'family time[ :–—-]*'
                  )
            """,
            (current, MEETING_PACKET_CONTENT_VERSION),
        ).fetchall()
    return {str(row["item_id"]) for row in rows}


def prune_expired_meeting_packets(
    db_path: Path,
    *,
    as_of: str | None = None,
) -> int:
    timestamp = _canonical_timestamp(as_of or now_iso()).isoformat()
    with operational_connection(db_path, write=True) as conn:
        cursor = conn.execute(
            "DELETE FROM ops_meeting_packets WHERE expires_at <= ?",
            (timestamp,),
        )
        return max(0, int(cursor.rowcount))


def precompose_upcoming_meeting_packets(
    paths: Any,
    operational_service: Any,
    *,
    as_of: str | None = None,
    horizon_hours: int = DEFAULT_MEETING_PACKET_HORIZON_HOURS,
    limit: int = 20,
) -> dict[str, Any]:
    """Prepare upcoming briefs from local evidence before the user opens them."""

    # Imported lazily to keep packet persistence independent from presentation.
    from .operational_briefing import build_meeting_packet

    timestamp = _canonical_timestamp(as_of or now_iso()).isoformat()
    item_ids = list_meetings_needing_packets(
        paths.ops_sqlite_path,
        as_of=timestamp,
        horizon_hours=horizon_hours,
        limit=limit,
    )
    prepared: list[str] = []
    errors: list[dict[str, str]] = []
    for item_id in item_ids:
        try:
            packet = build_meeting_packet(paths, item_id)
            operational_service.save_meeting_packet(
                item_id,
                packet,
                generated_at=timestamp,
            )
            prepared.append(item_id)
        except Exception as exc:
            errors.append(
                {
                    "item_id": item_id,
                    "error": f"{type(exc).__name__}: {exc}"[:1000],
                }
            )
    try:
        pruned = operational_service.prune_expired_meeting_packets(as_of=timestamp)
    except Exception as exc:
        pruned = 0
        errors.append(
            {
                "item_id": "retention",
                "error": f"{type(exc).__name__}: {exc}"[:1000],
            }
        )
    return {
        "status": "partial" if errors else "complete",
        "as_of": timestamp,
        "eligible_count": len(item_ids),
        "prepared_count": len(prepared),
        "prepared_item_ids": prepared,
        "pruned_count": pruned,
        "errors": errors,
    }


def run_scheduled_meeting_preparation(
    paths: Any,
    operational_service: Any,
) -> dict[str, Any]:
    try:
        return precompose_upcoming_meeting_packets(paths, operational_service)
    except OperationalStoreUnavailableError:
        return {
            "status": "skipped",
            "skipped": True,
            "reason": "Chief-of-Staff shadow storage is not initialized yet.",
        }


def _canonical_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("meeting packet timestamps require a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)
