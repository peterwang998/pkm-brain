from __future__ import annotations

from pathlib import Path

from fake_transport import LocalRsyncTransport
from pkm_brain.db import connection, loads
from pkm_brain.sync_transfer import sync_run
from test_sync_pull_ingest import agent_markdown
from test_sync_pull_staging import primary_with_secondary, write_outbox_file


def sync_rows(sqlite_path: Path) -> list[dict[str, object]]:
    with connection(sqlite_path) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM sync_runs ORDER BY started_at")]


def test_successful_sync_run_writes_single_ok_row(tmp_path: Path) -> None:
    primary, secondary_home = primary_with_secondary(tmp_path)
    write_outbox_file(secondary_home, "agent_logs/codex/session.md", agent_markdown("session", "secondary-token"))

    result = sync_run(primary, "secondary", transport=LocalRsyncTransport(remote_home=secondary_home))

    rows = sync_rows(primary.sqlite_path)
    assert result.status == "ok"
    assert len(rows) == 1
    row = rows[0]
    assert row["peer_node_id"] == "secondary"
    assert row["direction"] == "run"
    assert row["status"] == "ok"
    assert row["files_pulled"] == 1
    assert int(row["files_pushed"]) > 0
    assert int(row["bytes_pulled"]) > 0
    assert int(row["bytes_pushed"]) > 0
    assert row["primary_ingest_run_id"]
    assert row["remote_ingest_status"] == "ok"
    assert loads(str(row["errors"]), []) == []


def test_push_failure_records_failed_row_with_errors(tmp_path: Path) -> None:
    primary, secondary_home = primary_with_secondary(tmp_path)
    write_outbox_file(secondary_home, "agent_logs/codex/session.md", agent_markdown("session", "secondary-token"))

    result = sync_run(primary, "secondary", transport=LocalRsyncTransport(remote_home=secondary_home, fail_rsync_call=2))

    rows = sync_rows(primary.sqlite_path)
    assert result.status == "failed"
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert "simulated rsync failure" in loads(str(rows[0]["errors"]), [])[0]


def test_if_reachable_unreachable_peer_records_skipped(tmp_path: Path) -> None:
    primary, secondary_home = primary_with_secondary(tmp_path)
    write_outbox_file(secondary_home, "agent_logs/codex/session.md", agent_markdown("session", "secondary-token"))

    result = sync_run(primary, "secondary", transport=LocalRsyncTransport(remote_home=secondary_home, ssh=False), if_reachable=True)

    rows = sync_rows(primary.sqlite_path)
    assert result.status == "skipped"
    assert len(rows) == 1
    assert rows[0]["status"] == "skipped"
    assert rows[0]["files_pulled"] == 0
    assert rows[0]["files_pushed"] == 0
    assert "peer unreachable" in loads(str(rows[0]["errors"]), [])[0]


def test_remote_ingest_failure_records_degraded_status(tmp_path: Path) -> None:
    primary, secondary_home = primary_with_secondary(tmp_path)
    write_outbox_file(secondary_home, "agent_logs/codex/session.md", agent_markdown("session", "secondary-token"))

    result = sync_run(primary, "secondary", transport=LocalRsyncTransport(remote_home=secondary_home, remote_ingest=False))

    rows = sync_rows(primary.sqlite_path)
    assert result.status == "ok_with_remote_ingest_failure"
    assert len(rows) == 1
    assert rows[0]["status"] == "ok_with_remote_ingest_failure"
    assert rows[0]["remote_ingest_status"] == "failed"
