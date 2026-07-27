from __future__ import annotations

from typing import Any

from .db import connection, dumps
from .gmail_sensitive_data import sanitize_gmail_model_payload
from .paths import BrainPaths
from .util import new_id, now_iso


def persist_agent_session(
    paths: BrainPaths,
    *,
    summary: str,
    files_touched: list[str],
    commands_run: list[str],
    outcome: str,
    unresolved_issues: list[str],
) -> str:
    sanitized: dict[str, Any] = sanitize_gmail_model_payload(
        {
            "summary": summary,
            "files_touched": files_touched,
            "commands_run": commands_run,
            "outcome": outcome,
            "unresolved_issues": unresolved_issues,
        }
    )
    session_id = new_id("session")
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO agent_sessions(id, summary, files_touched, commands_run, outcome, unresolved_issues, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                sanitized["summary"],
                dumps(sanitized["files_touched"]),
                dumps(sanitized["commands_run"]),
                sanitized["outcome"],
                dumps(sanitized["unresolved_issues"]),
                now_iso(),
            ),
        )
    return session_id
