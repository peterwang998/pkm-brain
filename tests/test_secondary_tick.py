from __future__ import annotations

from pathlib import Path

from pkm_brain.automation import run_secondary_tick
from pkm_brain.db import connection
from pkm_brain.paths import BrainPaths
from pkm_brain.sync_setup import init_secondary
from test_capture import make_codex_fixture


def test_secondary_tick_exports_outbox_and_ingests_locally(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "secondary")
    init_secondary(paths, "secondary", "primary")
    state = make_codex_fixture(tmp_path)

    result = run_secondary_tick(
        paths,
        agent="codex",
        codex_state=state,
        claude_projects=tmp_path / "missing-claude",
        opencode_db=tmp_path / "missing-opencode.sqlite",
    )

    assert result.skipped is False
    assert result.capture["captured"] == 1
    assert result.capture["exported"] == 1
    assert result.ingest is not None
    assert result.ingest["changed"] == 1
    assert result.index_status is not None
    assert result.index_status["documents"] == 1
    assert (paths.outbox / "secondary" / "manifest.jsonl").exists()
    assert (paths.outbox / "secondary" / "agent_logs" / "codex" / "codex-session-1.md").exists()
    with connection(paths.sqlite_path) as conn:
        row = conn.execute("SELECT source_type, origin_node_id FROM documents").fetchone()
    assert row["source_type"] == "agent_session_log"
    assert row["origin_node_id"] == "secondary"
