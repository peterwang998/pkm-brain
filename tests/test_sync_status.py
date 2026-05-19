from __future__ import annotations

from pathlib import Path

from fake_transport import LocalRsyncTransport
from pkm_brain.sync_status import sync_status
from pkm_brain.sync_transfer import sync_run
from test_sync_pull_ingest import agent_markdown
from test_sync_pull_staging import primary_with_secondary, write_outbox_file


def test_sync_status_reports_last_success_and_failure(tmp_path: Path) -> None:
    primary, secondary_home = primary_with_secondary(tmp_path)
    write_outbox_file(secondary_home, "agent_logs/codex/session.md", agent_markdown("session", "secondary-token"))
    sync_run(primary, "secondary", transport=LocalRsyncTransport(remote_home=secondary_home))
    sync_run(primary, "secondary", transport=LocalRsyncTransport(remote_home=secondary_home, fail_rsync_call=1))

    status = sync_status(primary)
    peer = status["peers"][0]

    assert status["configured"] is True
    assert peer["peer_node_id"] == "secondary"
    assert peer["last_successful_pull"]["status"] == "ok"
    assert peer["last_successful_push"]["status"] == "ok"
    assert peer["last_failed_run"]["status"] == "failed"
    assert "simulated rsync failure" in peer["last_failed_run"]["error_summary"]
    assert peer["pending_outbox_count"] == 1


def test_sync_status_warns_on_mirror_divergence(tmp_path: Path) -> None:
    primary, secondary_home = primary_with_secondary(tmp_path)
    write_outbox_file(secondary_home, "agent_logs/codex/session.md", agent_markdown("session", "secondary-token"))
    sync_run(primary, "secondary", transport=LocalRsyncTransport(remote_home=secondary_home))
    (secondary_home / "wiki" / "remote-only.md").write_text("# Remote drift\n", encoding="utf-8")

    status = sync_status(primary)

    assert status["peers"][0]["mirror_current"] is False
    assert any("mirror divergence" in warning for warning in status["warnings"])
