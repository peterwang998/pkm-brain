from __future__ import annotations

from pathlib import Path

from pkm_brain.db import connection
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


def test_rebuild_mirror_index_indexes_raw_without_copying_raw(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "secondary")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    raw_file = paths.raw / "agent_session_log" / "2026" / "05" / "session.md"
    raw_file.parent.mkdir(parents=True)
    raw_file.write_text(
        "---\n"
        'source_type: "agent_session_log"\n'
        'title: "Mirrored Session"\n'
        "---\n\n"
        "# Mirrored Session\n\n"
        "remote-search-token\n",
        encoding="utf-8",
    )

    result = svc.rebuild_mirror_index()
    second = svc.rebuild_mirror_index()

    assert result["documents_indexed"] == 1
    assert result["chunks_created"] >= 1
    assert result["vector_rebuild"]["status"] == "ok"
    assert second["documents_indexed"] == 1
    assert second["vector_rebuild"]["status"] == "ok"
    assert sorted(paths.raw.rglob("*.md")) == [raw_file]
    with connection(paths.sqlite_path) as conn:
        document = conn.execute("SELECT source_path, raw_path, origin_node_id FROM documents").fetchone()
        assert document["source_path"] == str(raw_file)
        assert document["raw_path"] == str(raw_file)
        assert document["origin_node_id"] == "mirror"
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    search = svc.search("remote-search-token")
    assert search["results"]


def test_rebuild_mirror_index_rebuilds_vectors_after_source_changes(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "secondary")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    raw_file = paths.raw / "markdown_note" / "2026" / "05" / "note.md"
    raw_file.parent.mkdir(parents=True)
    raw_file.write_text("# Mirror\n\nold-token\n", encoding="utf-8")
    first = svc.rebuild_mirror_index()

    raw_file.write_text("# Mirror\n\nnew-token\n", encoding="utf-8")
    second = svc.rebuild_mirror_index()
    doctor = svc.index_doctor()

    assert first["vector_rebuild"]["status"] == "ok"
    assert second["vector_rebuild"]["status"] == "ok"
    assert doctor["status"] == "ok"
    assert doctor["missing_vector_count"] == 0
    assert doctor["stale_vector_count"] == 0
    assert svc.search("new-token")["results"]
