from __future__ import annotations

from pathlib import Path

from pkm_brain.db import connection, dumps
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.sync_status import sync_conflicts
from pkm_brain.util import now_iso


def test_sync_conflicts_lists_logical_sources_seen_under_multiple_origins(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    timestamp = now_iso()
    with connection(paths.sqlite_path) as conn:
        for document_id, origin in [("doc_primary", "primary"), ("doc_secondary", "secondary")]:
            conn.execute(
                """
                INSERT INTO documents(
                  id, source_type, title, source_path, raw_path, content_hash,
                  origin_node_id, logical_source_key, created_at, ingested_at,
                  project, tags, version, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document_id,
                    "agent_session_log",
                    "Same Session",
                    "/same/source.md",
                    f"/raw/{document_id}.md",
                    document_id,
                    origin,
                    "agent_logs/codex/same-session.md",
                    timestamp,
                    timestamp,
                    None,
                    dumps([]),
                    1,
                    "active",
                ),
            )

    result = sync_conflicts(paths)

    assert result["count"] == 1
    conflict = result["conflicts"][0]
    assert conflict["logical_source_key"] == "agent_logs/codex/same-session.md"
    assert conflict["origins"] == ["primary", "secondary"]
    assert conflict["document_ids"] == ["doc_primary", "doc_secondary"]
