from __future__ import annotations

from pathlib import Path

from pkm_brain.db import connection, init_db


def test_sync_runs_schema_has_spec_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "brain.sqlite"
    init_db(db_path)

    with connection(db_path) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(sync_runs)")}

    assert {
        "id",
        "peer_node_id",
        "direction",
        "started_at",
        "finished_at",
        "status",
        "files_pulled",
        "files_pushed",
        "bytes_pulled",
        "bytes_pushed",
        "primary_ingest_run_id",
        "remote_ingest_status",
        "errors",
    }.issubset(columns)
