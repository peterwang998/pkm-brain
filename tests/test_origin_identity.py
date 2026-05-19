from __future__ import annotations

from pathlib import Path

from pkm_brain.db import connection
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


def service_for(tmp_path: Path) -> BrainService:
    return BrainService(BrainPaths.from_value(tmp_path / "brain"), prefer_model_embeddings=False)


def write_agent_log(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        'source_type: "agent_session_log"\n'
        'agent: "codex"\n'
        'session_id: "same-session"\n'
        'title: "Same Session"\n'
        "---\n\n"
        "# Agent Session: Same Session\n\n"
        f"- {token}\n",
        encoding="utf-8",
    )


def test_local_ingest_stamps_origin_identity(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    (svc.paths.config_local / "node_id").write_text("local-node\n", encoding="utf-8")
    note = svc.paths.inbox / "note.md"
    note.write_text("# Note\n\nOrigin identity.\n", encoding="utf-8")

    svc.ingest()

    with connection(svc.paths.sqlite_path) as conn:
        row = conn.execute("SELECT origin_node_id, logical_source_key FROM documents").fetchone()
    assert row["origin_node_id"] == "local-node"
    assert row["logical_source_key"] == str(note)


def test_same_source_path_different_origins_coexist(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    log = svc.paths.inbox / "agent_logs" / "codex" / "same-session.md"
    write_agent_log(log, "initial-token")

    first = svc.ingest(log, origin_node_id="primary")
    second = svc.ingest(log, origin_node_id="secondary")

    assert first.changed == 1
    assert second.changed == 1
    with connection(svc.paths.sqlite_path) as conn:
        docs = [
            dict(row)
            for row in conn.execute(
                "SELECT origin_node_id, source_path FROM documents ORDER BY origin_node_id"
            )
        ]
    assert docs == [
        {"origin_node_id": "primary", "source_path": str(log)},
        {"origin_node_id": "secondary", "source_path": str(log)},
    ]


def test_reingest_replaces_only_matching_origin_snapshot(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    log = svc.paths.inbox / "agent_logs" / "codex" / "same-session.md"
    write_agent_log(log, "primary-token-v1")
    svc.ingest(log, origin_node_id="primary")
    svc.ingest(log, origin_node_id="secondary")

    write_agent_log(log, "primary-token-v2")
    result = svc.ingest(log, origin_node_id="primary")

    assert result.changed == 1
    assert result.documents_replaced == 1
    with connection(svc.paths.sqlite_path) as conn:
        docs = [dict(row) for row in conn.execute("SELECT id, origin_node_id FROM documents ORDER BY origin_node_id")]
        texts = [
            (row["origin_node_id"], row["text"])
            for row in conn.execute(
                """
                SELECT d.origin_node_id, c.text
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                ORDER BY d.origin_node_id
                """
            )
        ]
    assert [row["origin_node_id"] for row in docs] == ["primary", "secondary"]
    assert any(origin == "primary" and "primary-token-v2" in text for origin, text in texts)
    assert any(origin == "secondary" and "primary-token-v1" in text for origin, text in texts)
