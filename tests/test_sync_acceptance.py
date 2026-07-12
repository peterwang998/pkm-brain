from __future__ import annotations

from pathlib import Path

from fake_transport import LocalRsyncTransport
from pkm_brain.db import connection
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.sync_acceptance import run_acceptance_report
from test_sync_pull_ingest import agent_markdown
from test_sync_pull_staging import primary_with_secondary, write_outbox_file


def check_statuses(report: dict[str, object]) -> dict[str, str]:
    return {check["name"]: check["status"] for check in report["checks"]}  # type: ignore[index]


def test_acceptance_report_blocks_without_sync_config(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    report = run_acceptance_report(paths, test_connection_now=False)

    statuses = check_statuses(report)
    assert report["ready"] is False
    assert statuses["schema_migrations"] == "ok"
    assert statuses["sync_config"] == "fail"
    assert statuses["peer_configured"] == "fail"


def test_acceptance_report_checks_single_configured_peer(tmp_path: Path) -> None:
    primary, secondary_home = primary_with_secondary(tmp_path)

    report = run_acceptance_report(
        primary, transport=LocalRsyncTransport(remote_home=secondary_home)
    )

    statuses = check_statuses(report)
    assert report["ready"] is True
    assert report["complete"] is False
    assert report["peer_node_id"] == "secondary"
    assert statuses["sync_doctor"] == "ok"
    assert statuses["connection_test"] == "ok"
    assert statuses["sync_run"] == "skipped"


def test_acceptance_report_can_execute_sync_and_verify_retrieval(
    tmp_path: Path,
) -> None:
    primary, secondary_home = primary_with_secondary(tmp_path)
    phrase = "secondary acceptance unique token"
    write_outbox_file(
        secondary_home, "agent_logs/codex/session.md", agent_markdown("session", phrase)
    )

    report = run_acceptance_report(
        primary,
        transport=LocalRsyncTransport(remote_home=secondary_home),
        run_sync_now=True,
        retrieval_phrase=phrase,
    )

    statuses = check_statuses(report)
    assert report["ready"] is True
    assert report["complete"] is True
    assert statuses["sync_run"] == "ok"
    assert statuses["retrieval"] == "ok"
    assert report["sync_run"]["status"] == "ok"  # type: ignore[index]
    with connection(primary.sqlite_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM retrieval_events").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM context_lineage_events WHERE retrieval_event_id IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
