from __future__ import annotations

import json
from pathlib import Path

from fake_transport import LocalRsyncTransport
from pkm_brain.paths import BrainPaths
from pkm_brain.sync_setup import add_peer, init_primary, init_secondary
from pkm_brain.sync_status import sync_status
from pkm_brain.sync_transfer import sync_run
from pkm_brain.util import file_sha256
from test_sync_pull_ingest import agent_markdown
from test_sync_pull_staging import primary_with_secondary, write_outbox_file


def test_sync_status_reports_last_success_and_failure(tmp_path: Path) -> None:
    primary, secondary_home = primary_with_secondary(tmp_path)
    write_outbox_file(secondary_home, "agent_logs/codex/session.md", agent_markdown("session", "secondary-token"))
    sync_run(primary, "secondary", transport=LocalRsyncTransport(remote_home=secondary_home))
    transport = LocalRsyncTransport(remote_home=secondary_home, fail_rsync_call=1)
    sync_run(primary, "secondary", transport=transport)

    status = sync_status(primary, transport=transport)
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
    transport = LocalRsyncTransport(remote_home=secondary_home)
    sync_run(primary, "secondary", transport=transport)
    (secondary_home / "wiki" / "remote-only.md").write_text("# Remote drift\n", encoding="utf-8")

    status = sync_status(primary, transport=transport)

    assert status["peers"][0]["mirror_current"] is False
    assert any("mirror divergence" in warning for warning in status["warnings"])


def test_sync_status_fetches_remote_hash_over_transport(tmp_path: Path) -> None:
    primary = BrainPaths.from_value(tmp_path / "primary")
    secondary_home = tmp_path / "actual-secondary"
    init_primary(primary, "primary")
    add_peer(primary, "secondary", "secondary.local", "peter", Path("/not-mounted/secondary"))
    (secondary_home / "wiki").mkdir(parents=True)
    (secondary_home / "wiki" / "remote.md").write_text("# Remote\n", encoding="utf-8")
    transport = LocalRsyncTransport(remote_home=secondary_home)

    status = sync_status(primary, transport=transport)
    peer = status["peers"][0]

    assert peer["remote_manifest_hash"] is not None
    assert any("brain sync mirror-hash --json" in command[-1] for command in transport.commands)


def test_sync_status_counts_custom_secondary_outbox_path_from_remote_config(tmp_path: Path) -> None:
    primary = BrainPaths.from_value(tmp_path / "primary")
    secondary = BrainPaths.from_value(tmp_path / "secondary")
    custom_outbox = tmp_path / "external-disk" / "secondary-outbox"
    init_primary(primary, "primary")
    add_peer(primary, "secondary", "secondary.local", "peter", secondary.home, outbox_path=custom_outbox)
    init_secondary(secondary, "secondary", "primary", outbox_path=custom_outbox)
    outbox_file = custom_outbox / "agent_logs" / "codex" / "session.md"
    outbox_file.parent.mkdir(parents=True)
    outbox_file.write_text(agent_markdown("session", "secondary-token"), encoding="utf-8")
    manifest = custom_outbox / "manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "node_id": "secondary",
                "source_kind": "agent_session_log",
                "agent": "codex",
                "session_id": "session",
                "relative_path": "agent_logs/codex/session.md",
                "content_hash": file_sha256(outbox_file),
                "captured_at": "2026-05-18T12:00:00Z",
                "source_path": "/secondary/source.jsonl",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    transport = LocalRsyncTransport(remote_home=secondary.home)

    status = sync_status(primary, transport=transport)
    peer = status["peers"][0]

    assert peer["pending_outbox_count"] == 1
    assert peer["remote_outbox_path"] == str(custom_outbox)
