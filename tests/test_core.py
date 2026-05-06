from __future__ import annotations

from pathlib import Path

from pkm_brain.audit import audit_memories
from pkm_brain.db import connection
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.wiki import lint_wiki, synthesize_wiki
from pkm_brain.wiki_proposals import apply_wiki_proposal, create_wiki_proposal, inspect_wiki_proposal, record_wiki_interview


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


def test_wiki_synthesis_creates_reference_pages_for_documents(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "sqlite-decision.md"
    note.write_text(
        "# SQLite Decision\n\nSQLite is the canonical metadata store.\n",
        encoding="utf-8",
    )
    svc.ingest()

    dry = synthesize_wiki(svc.paths, dry_run=True)
    assert dry["created"]
    assert not list(svc.paths.wiki.rglob("*.md"))

    result = synthesize_wiki(svc.paths)
    assert result["created"]
    assert result["lint"]["errors"] == []
    pages = list((svc.paths.wiki / "references").rglob("*.md"))
    assert len(pages) == 1
    text = pages[0].read_text(encoding="utf-8")
    assert "page_type: reference" in text
    assert "document:" in text

    second = synthesize_wiki(svc.paths)
    assert second["created"] == []
    assert second["skipped"]


def test_wiki_synthesis_compiles_semantic_pages(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "pkm-architecture.md"
    note.write_text(
        "# PKM Brain Architecture\n\n"
        "PKM Brain is a personal knowledge management second brain.\n\n"
        "SQLite is the canonical metadata store for documents and chunks.\n\n"
        "The wiki synthesis layer should create human-readable compiled markdown pages.\n",
        encoding="utf-8",
    )
    svc.ingest()

    result = synthesize_wiki(svc.paths)

    assert result["lint"]["errors"] == []
    index = (svc.paths.wiki / "index.md").read_text(encoding="utf-8")
    concept = (svc.paths.wiki / "concepts" / "wiki-synthesis-layer.md").read_text(encoding="utf-8")
    decision = (svc.paths.wiki / "decisions" / "use-sqlite-for-canonical-metadata.md").read_text(encoding="utf-8")
    project = (svc.paths.wiki / "projects" / "pkm-brain.md").read_text(encoding="utf-8")

    assert "[[concepts/wiki-synthesis-layer]]" in index
    assert "page_type: concept" in concept
    assert "Reference page synthesized from" not in concept
    assert "[[decisions/maintain-wiki-as-compiled-markdown]]" in concept
    assert "document:" in concept
    assert "page_type: decision" in decision
    assert "Use SQLite as the canonical metadata store" in decision
    assert "page_type: project" in project
    assert "[[decisions/use-sqlite-for-canonical-metadata]]" in project

    context = svc.retrieve_context("explain the wiki synthesis layer and sqlite metadata")
    assert context["relevant_wiki_pages"]
    assert context["supporting_chunks"]
    assert any(page["relative_path"] == "concepts/wiki-synthesis-layer.md" for page in context["relevant_wiki_pages"])
    assert any(str(citation).startswith("document:") for citation in context["citations"])


def test_memory_audit_warns_on_missing_source(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    memory_id = svc.propose_memory("PreferenceMemory", "global", "User prefers concise answers.", [], 0.8)

    audit = audit_memories(svc.paths)

    assert audit["errors"] == []
    assert any(memory_id in warning for warning in audit["warnings"])


def test_wiki_proposal_interview_and_apply_patches_section(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    target = svc.paths.wiki / "concepts"
    target.mkdir(parents=True)
    (target / "test-concept.md").write_text(
        "---\n"
        "title: Test Concept\n"
        "page_type: concept\n"
        "id: concept-test\n"
        "status: active\n"
        "created_at: 2026-05-06\n"
        "updated_at: 2026-05-06\n"
        "source_ids:\n"
        "  - document:test\n"
        "related: []\n"
        "tags: []\n"
        "---\n\n"
        "# Test Concept\n\n"
        "## Summary\n\nOld summary.\n\n"
        "## Key Points\n\n- Old point.\n\n"
        "## Definition\n\nOld.\n\n"
        "## Why It Matters\n\nOld.\n\n"
        "## How It Works\n\nOld.\n\n"
        "## Related Decisions\n\n- None.\n\n"
        "## Source Evidence\n\n- document:test\n\n"
        "## Related Pages\n\n- None.\n\n"
        "## Open Questions\n\n- None.\n",
        encoding="utf-8",
    )
    batch_id = create_wiki_proposal(
        svc.paths,
        title="Update test concept",
        rationale="Better synthesis.",
        source_ids=["document:test"],
        changes=[
            {
                "target_path": "concepts/test-concept.md",
                "operation": "replace_section",
                "section_name": "Summary",
                "proposed_markdown": "New source-backed summary.",
                "rationale": "Improve summary.",
                "source_ids": ["document:test"],
                "confidence": 0.9,
            }
        ],
        confidence=0.9,
    )

    proposal = inspect_wiki_proposal(svc.paths, batch_id)
    assert proposal["status"] == "proposed"
    assert proposal["items"][0]["target_path"] == "concepts/test-concept.md"

    reviewed = record_wiki_interview(
        svc.paths,
        batch_id,
        ["Approve?"],
        ["Yes"],
        "approved",
    )
    assert reviewed["status"] == "approved"

    result = apply_wiki_proposal(svc.paths, batch_id)
    assert result["lint"]["errors"] == []
    text = (target / "test-concept.md").read_text(encoding="utf-8")
    assert "New source-backed summary." in text
    assert "Old summary." not in text
    assert inspect_wiki_proposal(svc.paths, batch_id)["status"] == "applied"


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
