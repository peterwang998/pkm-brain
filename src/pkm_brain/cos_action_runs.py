from __future__ import annotations

from typing import Any

from .db import connection, dumps
from .paths import BrainPaths


def complete_action_run(
    paths: BrainPaths,
    run_id: str | None,
    *,
    summary: dict[str, Any] | None = None,
) -> bool:
    """Close the action-batch placeholder created by ``propose_action``."""

    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        return False
    with connection(paths.sqlite_path) as conn:
        cursor = conn.execute(
            """
            UPDATE wiki_curation_runs
            SET status = 'complete', summary = ?
            WHERE id = ? AND group_by = 'cos_action'
            """,
            (
                dumps(
                    {
                        "created_by": "propose_action",
                        "completed_by": "extract_recent_documents",
                        **dict(summary or {}),
                    }
                ),
                normalized_run_id,
            ),
        )
    return cursor.rowcount > 0
