from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable

from .db import best_effort_write_connection, dumps, is_sqlite_locked_error
from .util import new_id, now_iso


ExposureRecorder = Callable[[sqlite3.Connection, str], None]


def record_retrieval_telemetry(
    db_path: Path,
    *,
    query: str,
    caller: str,
    returned_chunk_ids: list[str],
    selected_chunk_ids: list[str],
    citation_snapshots: list[dict[str, Any]],
    debug_json: str,
    exposure_recorder: ExposureRecorder | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Record ancillary retrieval telemetry without blocking evidence reads."""

    event_id = new_id("retrieval")
    try:
        with best_effort_write_connection(db_path) as conn:
            conn.execute(
                """
                INSERT INTO retrieval_events(
                  id, query, timestamp, caller, returned_chunk_ids, selected_chunk_ids, citation_snapshots, debug
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    query,
                    now_iso(),
                    caller,
                    dumps(returned_chunk_ids),
                    dumps(selected_chunk_ids),
                    dumps(citation_snapshots),
                    debug_json,
                ),
            )
            if exposure_recorder:
                exposure_recorder(conn, event_id)
    except sqlite3.OperationalError as exc:
        if not is_sqlite_locked_error(exc):
            raise
        return None, {
            "status": "skipped",
            "reason": "database_locked",
            "impact": "evidence_unaffected",
            "message": (
                "Evidence retrieval completed, but retrieval telemetry was not "
                "recorded because the local database was busy."
            ),
        }
    return event_id, None
