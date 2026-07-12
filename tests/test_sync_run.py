from __future__ import annotations

from pathlib import Path

from fake_transport import LocalRsyncTransport
from pkm_brain.db import connection
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.sync_transfer import sync_run
from test_sync_pull_ingest import agent_markdown
from test_sync_pull_staging import primary_with_secondary, write_outbox_file


def test_sync_run_pulls_pushes_and_runs_remote_ingest(tmp_path: Path) -> None:
    primary, secondary_home = primary_with_secondary(tmp_path)
    write_outbox_file(secondary_home, "agent_logs/codex/session.md", agent_markdown("session", "secondary-token"))
    transport = LocalRsyncTransport(remote_home=secondary_home)

    result = sync_run(primary, "secondary", transport=transport)

    assert result.status == "ok"
    assert result.pull is not None
    assert result.pull["status"] == "ok"
    assert result.push is not None
    assert result.push["status"] == "ok"
    assert result.remote_ingest is not None
    assert result.remote_ingest["returncode"] == 0
    assert len(transport.rsync_commands) == 5
    assert transport.commands[-1][-1].startswith("brain sync rebuild-mirror-index --home")
    assert (secondary_home / "raw" / "agent_session_log").exists()
    with connection(secondary_home / "db" / "brain.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] >= 1
    search = BrainService(BrainPaths.from_value(secondary_home)).search("secondary-token")
    assert search["results"]


def test_sync_run_aborts_push_when_pull_fails(tmp_path: Path) -> None:
    primary, secondary_home = primary_with_secondary(tmp_path)
    write_outbox_file(secondary_home, "agent_logs/codex/session.md", agent_markdown("session", "secondary-token"))
    transport = LocalRsyncTransport(remote_home=secondary_home, fail_rsync=True)

    result = sync_run(primary, "secondary", transport=transport)

    assert result.status == "failed"
    assert result.push is None
    assert len(transport.rsync_commands) == 1


def test_sync_run_reports_remote_ingest_failure_as_degraded(tmp_path: Path) -> None:
    primary, secondary_home = primary_with_secondary(tmp_path)
    write_outbox_file(secondary_home, "agent_logs/codex/session.md", agent_markdown("session", "secondary-token"))
    transport = LocalRsyncTransport(remote_home=secondary_home, remote_ingest=False)

    result = sync_run(primary, "secondary", transport=transport)

    assert result.status == "ok_with_remote_ingest_failure"
    assert result.remote_ingest is not None
    assert result.remote_ingest["returncode"] == 1
