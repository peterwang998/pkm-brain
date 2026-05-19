from __future__ import annotations

from pathlib import Path

from fake_transport import LocalRsyncTransport
from pkm_brain.db import connection
from pkm_brain.service import BrainService
from pkm_brain.sync_transfer import sync_pull
from test_sync_pull_staging import primary_with_secondary, write_outbox_file


def agent_markdown(session_id: str, token: str) -> str:
    return (
        "---\n"
        'source_type: "agent_session_log"\n'
        'agent: "codex"\n'
        f'session_id: "{session_id}"\n'
        'title: "Same Session"\n'
        "---\n\n"
        "# Agent Session: Same Session\n\n"
        f"- {token}\n"
    )


def test_pull_ingests_with_peer_origin_and_relative_logical_key(tmp_path: Path) -> None:
    primary, secondary_home = primary_with_secondary(tmp_path)
    write_outbox_file(secondary_home, "agent_logs/codex/secondary-session.md", agent_markdown("secondary-session", "secondary-token"))

    result = sync_pull(primary, "secondary", transport=LocalRsyncTransport(remote_home=secondary_home), run_id="run-ingest")

    assert result.status == "ok"
    with connection(primary.sqlite_path) as conn:
        row = conn.execute("SELECT origin_node_id, logical_source_key FROM documents").fetchone()
    assert row["origin_node_id"] == "secondary"
    assert row["logical_source_key"] == "agent_logs/codex/secondary-session.md"


def test_primary_and_secondary_same_session_id_coexist_after_pull(tmp_path: Path) -> None:
    primary, secondary_home = primary_with_secondary(tmp_path)
    local_log = primary.inbox / "agent_logs" / "codex" / "same-session.md"
    local_log.parent.mkdir(parents=True, exist_ok=True)
    local_log.write_text(agent_markdown("same-session", "primary-token"), encoding="utf-8")
    BrainService(primary, prefer_model_embeddings=False).ingest(local_log)
    write_outbox_file(secondary_home, "agent_logs/codex/same-session.md", agent_markdown("same-session", "secondary-token"))

    sync_pull(primary, "secondary", transport=LocalRsyncTransport(remote_home=secondary_home), run_id="run-coexist")

    with connection(primary.sqlite_path) as conn:
        docs = [dict(row) for row in conn.execute("SELECT origin_node_id, logical_source_key FROM documents ORDER BY origin_node_id")]
    assert docs == [
        {"origin_node_id": "primary", "logical_source_key": str(local_log)},
        {"origin_node_id": "secondary", "logical_source_key": "agent_logs/codex/same-session.md"},
    ]


def test_pull_ingest_errors_return_and_record_failed_status(tmp_path: Path, monkeypatch) -> None:
    class FailedIngest:
        def as_dict(self) -> dict[str, object]:
            return {"run_id": "ingest-failed", "errors": ["ingest exploded"]}

    def fake_ingest(self, path, dry_run=False, origin_node_id=None, retry_quarantine=False):
        return FailedIngest()

    primary, secondary_home = primary_with_secondary(tmp_path)
    write_outbox_file(secondary_home, "agent_logs/codex/session.md", agent_markdown("session", "secondary-token"))
    monkeypatch.setattr(BrainService, "ingest", fake_ingest)

    result = sync_pull(primary, "secondary", transport=LocalRsyncTransport(remote_home=secondary_home), run_id="run-ingest-error")

    assert result.status == "failed"
    assert result.errors == ["ingest exploded"]
    with connection(primary.sqlite_path) as conn:
        row = conn.execute("SELECT status, errors FROM sync_runs WHERE peer_node_id = 'secondary'").fetchone()
    assert row["status"] == "failed"
    assert "ingest exploded" in row["errors"]
