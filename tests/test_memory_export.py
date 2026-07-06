from __future__ import annotations

from pathlib import Path

from pkm_brain.db import connection
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


def service_for(path: Path) -> BrainService:
    return BrainService(BrainPaths.from_value(path))


def test_approve_writes_memory_export_and_export_all_is_idempotent(tmp_path: Path) -> None:
    svc = service_for(tmp_path / "brain")
    svc.init_workspace()
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nMemory evidence.\n", encoding="utf-8")
    svc.ingest()
    with connection(svc.paths.sqlite_path) as conn:
        document_id = conn.execute("SELECT id FROM documents").fetchone()["id"]

    memory_id = svc.propose_memory(
        "ProjectMemory",
        "project:pkm-brain",
        "PKM Brain exports reviewed memories as Markdown.",
        [f"document:{document_id}"],
        0.9,
    )
    approved = svc.approve_memory(memory_id)
    path = svc.paths.memory / "project:pkm-brain" / f"{memory_id}.md"

    assert approved["status"] == "active"
    assert path.exists()
    assert "memory_type: ProjectMemory" in path.read_text(encoding="utf-8")

    svc.export_all_memories()
    first = path.read_bytes()
    svc.export_all_memories()
    second = path.read_bytes()
    assert first == second


def test_export_all_removes_stale_export_after_scope_change(tmp_path: Path) -> None:
    svc = service_for(tmp_path / "brain")
    svc.init_workspace()
    memory_id = svc.propose_memory("FactMemory", "global", "Migrated memory.", [], 0.8)
    svc.approve_memory(memory_id)
    with connection(svc.paths.sqlite_path) as conn:
        conn.execute("UPDATE memories SET scope = ? WHERE id = ?", ("user:Peter", memory_id))
    svc.export_all_memories()
    stale_path = svc.paths.memory / "user:Peter" / f"{memory_id}.md"
    assert stale_path.exists()

    with connection(svc.paths.sqlite_path) as conn:
        conn.execute("UPDATE memories SET scope = ? WHERE id = ?", ("global", memory_id))

    result = svc.export_all_memories()
    canonical_path = svc.paths.memory / "global" / f"{memory_id}.md"

    assert canonical_path.exists()
    assert not stale_path.exists()
    assert str(stale_path) in result["removed"]


def test_memory_import_refuses_missing_sources_unless_allowed(tmp_path: Path) -> None:
    source = service_for(tmp_path / "source-brain")
    target = service_for(tmp_path / "target-brain")
    source.init_workspace()
    target.init_workspace()
    memory_id = source.propose_memory(
        "ProjectMemory",
        "project:pkm-brain",
        "Imported memory body.",
        ["document:missing"],
        0.8,
    )
    source.approve_memory(memory_id)

    refused = target.import_memories(source.paths.memory)
    allowed = target.import_memories(source.paths.memory, allow_missing_sources=True)

    assert refused["imported"] == []
    assert refused["errors"]
    assert allowed["imported"] == [memory_id]
    assert target.get_memory(memory_id)["content"] == "Imported memory body."


def test_reject_removes_export_file(tmp_path: Path) -> None:
    svc = service_for(tmp_path / "brain")
    svc.init_workspace()
    memory_id = svc.propose_memory("ProjectMemory", "global", "Temporary memory.", [], 0.8)
    svc.approve_memory(memory_id)
    path = svc.paths.memory / "global" / f"{memory_id}.md"
    assert path.exists()

    svc.reject_memory(memory_id, "not durable")

    assert not path.exists()
