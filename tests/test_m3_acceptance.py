from __future__ import annotations

import json
from pathlib import Path

from fake_transport import LocalRsyncTransport
from pkm_brain.db import connection
from pkm_brain.daemon import SerialJobScheduler
from pkm_brain.paths import BrainPaths
from pkm_brain.sync_setup import add_peer, init_primary, init_secondary
from pkm_brain.sync_transfer import sync_run
from pkm_brain.util import file_sha256
from test_sync_pull_ingest import agent_markdown


def write_child_outbox(child_home: Path, node_id: str, relative_path: str, text: str) -> None:
    outbox = child_home / "outbox" / node_id
    path = outbox / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    manifest = outbox / "manifest.jsonl"
    row = {
        "node_id": node_id,
        "source_kind": "agent_session_log",
        "agent": "codex",
        "session_id": path.stem,
        "relative_path": relative_path,
        "content_hash": file_sha256(path),
        "captured_at": "2026-07-08T12:00:00Z",
        "source_path": f"/{node_id}/source.jsonl",
    }
    with manifest.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def test_m3_three_home_multi_child_sync_simulation(tmp_path: Path) -> None:
    primary = BrainPaths.from_value(tmp_path / "primary")
    child_a = BrainPaths.from_value(tmp_path / "child-a")
    child_b = BrainPaths.from_value(tmp_path / "child-b")
    init_primary(primary, "primary")
    init_secondary(child_a, "secondary-a", "primary")
    init_secondary(child_b, "secondary-b", "primary")
    add_peer(primary, "secondary-a", "secondary-a.local", "peter", child_a.home)
    add_peer(primary, "secondary-b", "secondary-b.local", "peter", child_b.home)
    write_child_outbox(
        child_a.home,
        "secondary-a",
        "agent_logs/codex/session-a.md",
        agent_markdown("session-a", "secondary-a-token"),
    )
    write_child_outbox(
        child_b.home,
        "secondary-b",
        "agent_logs/codex/session-b.md",
        agent_markdown("session-b", "secondary-b-token"),
    )

    result_a = sync_run(primary, "secondary-a", transport=LocalRsyncTransport(remote_node_id="secondary-a", remote_home=child_a.home))
    result_b = sync_run(primary, "secondary-b", transport=LocalRsyncTransport(remote_node_id="secondary-b", remote_home=child_b.home))
    result_a_fanout = sync_run(primary, "secondary-a", transport=LocalRsyncTransport(remote_node_id="secondary-a", remote_home=child_a.home))

    assert result_a.status == "ok"
    assert result_b.status == "ok"
    assert result_a_fanout.status == "ok"
    assert (primary.inbox / "external/secondary-a/agent_logs/codex/session-a.md").exists()
    assert (primary.inbox / "external/secondary-b/agent_logs/codex/session-b.md").exists()
    with connection(primary.sqlite_path) as conn:
        origins = {
            row["origin_node_id"]: row["logical_source_key"]
            for row in conn.execute("SELECT origin_node_id, logical_source_key FROM documents")
        }
    assert origins == {
        "secondary-a": "agent_logs/codex/session-a.md",
        "secondary-b": "agent_logs/codex/session-b.md",
    }
    for child in (child_a, child_b):
        with connection(child.sqlite_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] >= 2


def test_m3_per_child_scheduler_pause_is_isolated(tmp_path: Path) -> None:
    primary = BrainPaths.from_value(tmp_path / "primary")
    child_a = tmp_path / "child-a"
    child_b = tmp_path / "child-b"
    init_primary(primary, "primary")
    add_peer(primary, "secondary-a", "secondary-a.local", "peter", child_a)
    add_peer(primary, "secondary-b", "secondary-b.local", "peter", child_b)

    scheduler = SerialJobScheduler(primary)
    scheduler.set_enabled("sync:secondary-a", False)

    jobs = {job["id"]: job for job in scheduler.as_dict()["jobs"]}
    assert jobs["sync:secondary-a"]["enabled"] is False
    assert jobs["sync:secondary-b"]["enabled"] is True
