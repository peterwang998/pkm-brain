from __future__ import annotations

import json
from pathlib import Path

from pkm_brain.capture import AgentLogCapture
from pkm_brain.paths import BrainPaths
from pkm_brain.sync_setup import init_secondary
from pkm_brain.util import file_sha256
from test_capture import make_codex_fixture


def manifest_rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_export_outbox_writes_idempotent_manifest(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "secondary")
    init_secondary(paths, "secondary", "primary")
    state = make_codex_fixture(tmp_path)
    capture = AgentLogCapture(
        paths,
        codex_state=state,
        claude_projects=tmp_path / "missing-claude",
        opencode_db=tmp_path / "missing-opencode.sqlite",
    )

    first = capture.capture(agent="codex", export_outbox=True)
    manifest = paths.outbox / "secondary" / "manifest.jsonl"
    outbox_file = paths.outbox / "secondary" / "agent_logs" / "codex" / "codex-session-1.md"
    first_manifest = manifest.read_bytes()
    first_mtime = outbox_file.stat().st_mtime_ns

    assert first.captured == 1
    assert first.exported == 1
    rows = manifest_rows(manifest)
    assert len(rows) == 1
    assert rows[0]["node_id"] == "secondary"
    assert rows[0]["source_kind"] == "agent_session_log"
    assert rows[0]["relative_path"] == "agent_logs/codex/codex-session-1.md"
    assert rows[0]["content_hash"] == file_sha256(outbox_file)

    second = capture.capture(agent="codex", export_outbox=True)

    assert second.skipped == 1
    assert second.exported == 0
    assert manifest.read_bytes() == first_manifest
    assert outbox_file.stat().st_mtime_ns == first_mtime


def test_export_outbox_updates_existing_manifest_row_for_modified_content(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "secondary")
    init_secondary(paths, "secondary", "primary")
    state = make_codex_fixture(tmp_path)
    capture = AgentLogCapture(
        paths,
        codex_state=state,
        claude_projects=tmp_path / "missing-claude",
        opencode_db=tmp_path / "missing-opencode.sqlite",
    )
    capture.capture(agent="codex", export_outbox=True)
    manifest = paths.outbox / "secondary" / "manifest.jsonl"
    first_hash = manifest_rows(manifest)[0]["content_hash"]

    (tmp_path / "rollout.jsonl").write_text(
        json.dumps({"type": "user", "text": "Build ingestion automation v2"}) + "\n",
        encoding="utf-8",
    )
    updated = capture.capture(agent="codex", export_outbox=True)

    rows = manifest_rows(manifest)
    assert updated.captured == 1
    assert updated.exported == 1
    assert len(rows) == 1
    assert rows[0]["relative_path"] == "agent_logs/codex/codex-session-1.md"
    assert rows[0]["content_hash"] != first_hash
