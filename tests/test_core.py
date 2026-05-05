from __future__ import annotations

from pathlib import Path

from pkm_brain.audit import audit_memories
from pkm_brain.db import connection
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.wiki import lint_wiki


def service_for(tmp_path: Path) -> BrainService:
    return BrainService(BrainPaths.from_value(tmp_path / "brain"), prefer_model_embeddings=False)


def test_init_workspace_creates_directories_and_db(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()

    assert svc.paths.sqlite_path.exists()
    for directory in svc.paths.directories():
        assert directory.exists()


def test_ingest_markdown_chunks_and_searches(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "sqlite-decision.md"
    note.write_text(
        "# SQLite Decision\n\n"
        "SQLite is the canonical metadata store for the local-first PKM system.\n\n"
        "It stores documents, chunks, memories, and retrieval events.\n",
        encoding="utf-8",
    )

    result = svc.ingest()

    assert result.changed == 1
    assert result.chunks_created >= 1
    search = svc.search("SQLite metadata", limit=3, debug=True)
    assert search["results"]
    assert search["results"][0]["title"] == "sqlite decision"
    assert search["results"][0]["chunk_id"]
    assert search["event_id"]
    context = svc.retrieve_context("explain sqlite metadata", project="pkm-system")
    assert context["supporting_chunks"]


def test_duplicate_ingest_skips_unchanged_content(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "same.md"
    note.write_text("# Same\n\nRepeatable content.\n", encoding="utf-8")

    first = svc.ingest()
    second = svc.ingest()

    assert first.changed == 1
    assert second.changed == 0
    assert second.skipped == 1


def test_wiki_lint_accepts_valid_decision_page(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    page = svc.paths.wiki / "decisions"
    page.mkdir(parents=True)
    (page / "use-sqlite.md").write_text(
        "---\n"
        "title: Use SQLite\n"
        "page_type: decision\n"
        "id: decision-use-sqlite\n"
        "status: active\n"
        "created_at: 2026-05-05\n"
        "updated_at: 2026-05-05\n"
        "source_ids:\n"
        "  - document:test\n"
        "related: []\n"
        "tags:\n"
        "  - sqlite\n"
        "---\n\n"
        "# Use SQLite\n\n"
        "## Summary\n\nText.\n\n"
        "## Key Points\n\n- Point.\n\n"
        "## Context\n\nText.\n\n"
        "## Decision\n\nText.\n\n"
        "## Rationale\n\nText.\n\n"
        "## Alternatives Considered\n\nText.\n\n"
        "## Consequences\n\nText.\n\n"
        "## Source Evidence\n\n- document:test\n\n"
        "## Related Pages\n\nNone.\n\n"
        "## Open Questions\n\nNone.\n",
        encoding="utf-8",
    )

    result = lint_wiki(svc.paths)

    assert result["errors"] == []
    assert result["pages"] == 1


def test_memory_audit_warns_on_missing_source(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    memory_id = svc.propose_memory("PreferenceMemory", "global", "User prefers concise answers.", [], 0.8)

    audit = audit_memories(svc.paths)

    assert audit["errors"] == []
    assert any(memory_id in warning for warning in audit["warnings"])


def test_agent_session_write(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    session_id = svc.write_agent_session(
        "Implemented first slice.",
        ["src/pkm_brain/service.py"],
        ["uv run pytest"],
        "success",
        [],
    )

    with connection(svc.paths.sqlite_path) as conn:
        row = conn.execute("SELECT * FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()

    assert row is not None
    assert row["outcome"] == "success"
