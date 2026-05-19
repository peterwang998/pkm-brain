from __future__ import annotations

import json
from pathlib import Path

from pkm_brain.audit import audit_memories
from pkm_brain.db import connection
from pkm_brain.indexes import table_names
from pkm_brain.memory_proposals import propose_failure_memories_from_sources
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.wiki import lint_wiki, parse_frontmatter, synthesize_wiki
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


def test_lancedb_table_names_accepts_result_objects() -> None:
    class Result:
        tables = ["chunks"]

    class DB:
        def list_tables(self) -> Result:
            return Result()

    assert table_names(DB()) == ["chunks"]


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


def test_agent_session_log_reingest_replaces_previous_snapshot(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    log = svc.paths.inbox / "agent_logs" / "codex" / "codex-session-1.md"
    log.parent.mkdir(parents=True)
    log.write_text(
        "---\n"
        'source_type: "agent_session_log"\n'
        'agent: "codex"\n'
        'session_id: "codex-session-1"\n'
        'title: "Codex Session One"\n'
        "---\n\n"
        "# Agent Session: Codex Session One\n\n"
        "## User Requests\n\n"
        "- first-only-token\n",
        encoding="utf-8",
    )

    first = svc.ingest()
    with connection(svc.paths.sqlite_path) as conn:
        first_doc = conn.execute("SELECT id, raw_path FROM documents WHERE source_path = ?", (str(log),)).fetchone()
    assert first.changed == 1
    assert first_doc is not None
    first_raw = Path(first_doc["raw_path"])
    assert first_raw.exists()

    log.write_text(
        "---\n"
        'source_type: "agent_session_log"\n'
        'agent: "codex"\n'
        'session_id: "codex-session-1"\n'
        'title: "Codex Session One"\n'
        "---\n\n"
        "# Agent Session: Codex Session One\n\n"
        "## User Requests\n\n"
        "- second-only-token\n",
        encoding="utf-8",
    )

    second = svc.ingest()

    assert second.changed == 1
    assert second.documents_replaced == 1
    assert not first_raw.exists()
    with connection(svc.paths.sqlite_path) as conn:
        docs = [dict(row) for row in conn.execute("SELECT id, raw_path FROM documents WHERE source_path = ?", (str(log),))]
        chunks = [row["text"] for row in conn.execute("SELECT text FROM chunks")]
        fts_text = [row["text"] for row in conn.execute("SELECT text FROM chunk_fts")]
    assert len(docs) == 1
    assert docs[0]["id"] != first_doc["id"]
    assert any("second-only-token" in text for text in chunks)
    assert not any("first-only-token" in text for text in chunks)
    assert any("second-only-token" in text for text in fts_text)
    assert not any("first-only-token" in text for text in fts_text)


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


def test_parse_frontmatter_ignores_delimiters_inside_yaml_values() -> None:
    text = "---\ntitle: \"Plan --- Section\"\npage_type: reference\n---\n\n# Body\n"

    frontmatter, body = parse_frontmatter(text)

    assert frontmatter is not None
    assert frontmatter["title"] == "Plan --- Section"
    assert body.startswith("\n# Body")


def test_wiki_synthesis_creates_reference_pages_for_documents(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "sqlite-decision.md"
    note.write_text(
        "# SQLite Decision\n\nSQLite is the canonical metadata store.\n",
        encoding="utf-8",
    )
    svc.ingest()

    dry = synthesize_wiki(svc.paths, dry_run=True, with_llm=False)
    assert dry["created"]
    assert not list(svc.paths.wiki.rglob("*.md"))

    result = synthesize_wiki(svc.paths, with_llm=False)
    assert result["created"]
    assert result["lint"]["errors"] == []
    pages = list((svc.paths.wiki / "references").rglob("*.md"))
    assert len(pages) == 1
    text = pages[0].read_text(encoding="utf-8")
    assert "page_type: reference" in text
    assert "document:" in text

    second = synthesize_wiki(svc.paths, with_llm=False)
    assert second["created"] == []
    assert second["skipped"]


def test_wiki_synthesis_caps_reference_filenames(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "long-title.md"
    note.write_text(f"# {'very long title ' * 80}\n\nBody.\n", encoding="utf-8")
    svc.ingest()

    result = synthesize_wiki(svc.paths, with_llm=False)

    assert result["lint"]["errors"] == []
    page = next((svc.paths.wiki / "references").rglob("*.md"))
    assert len(page.name) < 150


def test_wiki_synthesis_compiles_semantic_pages_with_default_llm(tmp_path: Path, monkeypatch) -> None:
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
    with connection(svc.paths.sqlite_path) as conn:
        document_id = conn.execute("SELECT id FROM documents").fetchone()["id"]
    source_id = f"document:{document_id}"

    class FakeProvider:
        name = "fake"
        model = "test"

        def complete(self, prompt: str) -> str:
            return json.dumps(
                {
                    "title": "Compile PKM wiki",
                    "rationale": "Source-backed semantic pages from architecture note.",
                    "confidence": 0.9,
                    "pages": [
                        {
                            "page_type": "project",
                            "slug": "pkm-brain",
                            "title": "PKM Brain",
                            "summary": "PKM Brain is a personal knowledge management second brain.",
                            "key_points": ["The wiki is compiled from immutable source documents."],
                            "sections": {
                                "Current State": "The project has local ingestion and LLM wiki synthesis.",
                                "Goals": "Keep a durable, source-backed second brain.",
                                "Decisions": "- [[decisions/use-sqlite-for-canonical-metadata]]",
                                "Open Loops": "- None yet.",
                                "Timeline": "Compiled from source-backed ingestion events.",
                            },
                            "related": ["concepts/wiki-synthesis-layer", "decisions/use-sqlite-for-canonical-metadata"],
                            "tags": ["pkm", "second-brain"],
                            "source_ids": [source_id],
                            "confidence": 0.9,
                        },
                        {
                            "page_type": "concept",
                            "slug": "wiki-synthesis-layer",
                            "title": "Wiki Synthesis Layer",
                            "summary": "The wiki synthesis layer creates human-readable compiled Markdown pages.",
                            "key_points": ["Semantic pages are the main reading layer."],
                            "sections": {
                                "Definition": "A source-backed Markdown compilation layer.",
                                "Why It Matters": "It keeps knowledge from being re-derived from raw chunks on every query.",
                                "How It Works": "The LLM proposes source-backed pages and the system renders/lints them.",
                                "Related Decisions": "- [[decisions/use-sqlite-for-canonical-metadata]]",
                            },
                            "related": ["projects/pkm-brain", "decisions/use-sqlite-for-canonical-metadata"],
                            "tags": ["wiki", "synthesis"],
                            "source_ids": [source_id],
                            "confidence": 0.9,
                        },
                        {
                            "page_type": "decision",
                            "slug": "use-sqlite-for-canonical-metadata",
                            "title": "Use SQLite For Canonical Metadata",
                            "summary": "Use SQLite as the canonical metadata store.",
                            "key_points": ["SQLite is local and inspectable."],
                            "sections": {
                                "Context": "The system needs a local metadata store.",
                                "Decision": "Use SQLite as the canonical metadata store.",
                                "Rationale": "SQLite is portable, inspectable, and sufficient for local use.",
                                "Alternatives Considered": "- Postgres\n- Files only",
                                "Consequences": "Indexes can be rebuilt while SQLite keeps canonical metadata.",
                            },
                            "related": ["projects/pkm-brain", "concepts/wiki-synthesis-layer"],
                            "tags": ["decision", "sqlite"],
                            "source_ids": [source_id],
                            "confidence": 0.9,
                        },
                    ],
                }
            )

    monkeypatch.setattr("pkm_brain.wiki_compiler.get_provider", lambda provider_name=None: FakeProvider())

    result = synthesize_wiki(svc.paths)

    assert result["lint"]["errors"] == []
    assert result["llm_compile"]["provider"] == "fake"
    assert result["llm_compile"]["status"] == "ok"
    index = (svc.paths.wiki / "index.md").read_text(encoding="utf-8")
    concept = (svc.paths.wiki / "concepts" / "wiki-synthesis-layer.md").read_text(encoding="utf-8")
    decision = (svc.paths.wiki / "decisions" / "use-sqlite-for-canonical-metadata.md").read_text(encoding="utf-8")
    project = (svc.paths.wiki / "projects" / "pkm-brain.md").read_text(encoding="utf-8")

    assert "[[concepts/wiki-synthesis-layer]]" in index
    assert "page_type: concept" in concept
    assert "Reference page synthesized from" not in concept
    assert "[[decisions/use-sqlite-for-canonical-metadata]]" in concept
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


def test_retrieve_context_reranks_clean_sources_and_source_linked_wiki(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    clean = svc.paths.inbox / "ketch-guidepoint.md"
    clean.write_text(
        "---\n"
        'source_type: "hyprnote_meeting"\n'
        'title: "Ketch Guidepoint Teleconference"\n'
        "---\n\n"
        "# Meeting: Ketch Guidepoint Teleconference\n\n"
        "## ROI Framing and Business Case\n\n"
        "Ketch's commercial value to enterprise customers is cost avoidance from regulatory fines, "
        "litigation risk, and audit costs. It also improves operational compliance by automating "
        "data subject requests and consent enforcement across downstream enterprise systems.\n\n"
        "## Go-to-Market Motion\n\n"
        "The enterprise buying motion spans legal, privacy, engineering, data, and marketing teams.\n",
        encoding="utf-8",
    )
    noisy = svc.paths.inbox / "ketch-agent-log.md"
    noisy.write_text(
        "---\n"
        'source_type: "agent_session_log"\n'
        'title: "restore previous session discussion about Ketch"\n'
        "---\n\n"
        "- session_meta: You are Codex, a coding agent based on GPT-5.\n"
        "- response_item: What is the commercial value of Ketch to enterprise customers?\n"
        "- event_msg: Ketch commercial value enterprise customers regulatory fines operational compliance.\n"
        "- response_item: Ketch commercial value enterprise customers regulatory fines operational compliance.\n",
        encoding="utf-8",
    )
    svc.ingest()
    with connection(svc.paths.sqlite_path) as conn:
        row = conn.execute("SELECT id FROM documents WHERE title = ?", ("Ketch Guidepoint Teleconference",)).fetchone()
    assert row is not None

    reference_dir = svc.paths.wiki / "references" / "hyprnote_meeting"
    reference_dir.mkdir(parents=True)
    (reference_dir / "ketch-guidepoint.md").write_text(
        "---\n"
        "title: Ketch Guidepoint Teleconference\n"
        "page_type: reference\n"
        "id: reference-ketch-guidepoint\n"
        "status: active\n"
        f"source_ids:\n  - document:{row['id']}\n"
        "related: []\n"
        "tags:\n  - ketch\n"
        "---\n\n"
        "# Ketch Guidepoint Teleconference\n\n"
        "## Summary\n\nKetch enterprise value centers on privacy compliance automation.\n",
        encoding="utf-8",
    )
    concept_dir = svc.paths.wiki / "concepts"
    concept_dir.mkdir(parents=True)
    (concept_dir / "local-first-agent-memory.md").write_text(
        "---\n"
        "title: Local First Agent Memory\n"
        "page_type: concept\n"
        "id: concept-local-first-agent-memory\n"
        "status: active\n"
        "source_ids: []\n"
        "related: []\n"
        "tags:\n  - brain\n"
        "---\n\n"
        "# Local First Agent Memory\n\n"
        "## Summary\n\nLocal brain evidence can be used by agents during retrieval.\n",
        encoding="utf-8",
    )

    context = svc.retrieve_context(
        "Explain the commercial value of Ketch to enterprise customers. Use only local brain evidence.",
        budget=4000,
        debug=True,
    )

    assert context["supporting_chunks"][0]["source_type"] == "hyprnote_meeting"
    assert "session_meta" not in context["supporting_chunks"][0]["text"].lower()
    assert context["supporting_chunks"][0]["selection_reasons"]
    assert context["supporting_chunks"][0]["raw_context"]["raw_path"]
    assert context["supporting_chunks"][0]["raw_context"]["source_path"]
    assert any("recency boost" in reason for reason in context["supporting_chunks"][0]["selection_reasons"])
    assert any(
        row["source_type"] == "agent_session_log"
        for row in context["retrieval_debug"]["suppressed_chunk_reasons"]
    )
    wiki_paths = [page["relative_path"] for page in context["relevant_wiki_pages"]]
    assert "references/hyprnote_meeting/ketch-guidepoint.md" in wiki_paths
    assert "concepts/local-first-agent-memory.md" not in wiki_paths


def test_retrieve_context_compacts_noisy_agent_logs_with_hard_budget(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    log = svc.paths.inbox / "agent_logs" / "codex" / "retrieval-policy-session.md"
    log.parent.mkdir(parents=True)
    noisy_words = " ".join(f"noise{i}" for i in range(1800))
    log.write_text(
        "---\n"
        'source_type: "agent_session_log"\n'
        'agent: "codex"\n'
        'session_id: "retrieval-policy-session"\n'
        'title: "Retrieval Policy Session"\n'
        "---\n\n"
        "# Agent Session: Retrieval Policy Session\n\n"
        "- session_meta: You are Codex, a coding agent based on GPT-5.\n\n"
        "## User Requests\n\n"
        f"Build a retrieval policy with a hard budget and compact noisy agent logs. {noisy_words}\n",
        encoding="utf-8",
    )
    svc.ingest()

    context = svc.retrieve_context(
        "agent retrieval policy hard budget compact noisy logs",
        mode="compact",
        debug=True,
    )

    chunk = context["supporting_chunks"][0]
    assert context["retrieval_mode"] == "compact"
    assert chunk["source_type"] == "agent_session_log"
    assert chunk["excerpted"] is True
    assert chunk["returned_token_count"] <= 600
    assert chunk["original_token_count"] > chunk["returned_token_count"]
    assert "session_meta" not in chunk["text"].lower()
    assert sum(row["returned_token_count"] for row in context["supporting_chunks"]) <= context["budget"]


def test_retrieve_context_explicit_budget_caps_first_selected_chunk(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "huge-retrieval-policy.md"
    note.write_text(
        "---\n"
        "title: Huge Retrieval Policy\n"
        "---\n\n"
        "# Huge Retrieval Policy\n\n"
        + "retrieval policy budget "
        + " ".join(f"filler{i}" for i in range(600)),
        encoding="utf-8",
    )
    svc.ingest()

    context = svc.retrieve_context("retrieval policy budget", budget=80)

    assert context["budget"] == 80
    assert context["supporting_chunks"]
    assert sum(row["returned_token_count"] for row in context["supporting_chunks"]) <= 80
    assert context["supporting_chunks"][0]["returned_token_count"] <= 80
    assert context["supporting_chunks"][0]["excerpted"] is True


def test_retrieve_context_broad_mode_returns_more_agent_log_text_than_compact(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    log = svc.paths.inbox / "agent_logs" / "codex" / "broad-retrieval-session.md"
    log.parent.mkdir(parents=True)
    log.write_text(
        "agent retrieval broad compact comparison "
        + " ".join(f"detail{i}" for i in range(2200))
        + " final retrieval policy detail",
        encoding="utf-8",
    )
    svc.ingest()

    compact = svc.retrieve_context("agent retrieval broad compact comparison", mode="compact")
    broad = svc.retrieve_context("agent retrieval broad compact comparison", mode="broad")

    assert compact["supporting_chunks"][0]["returned_token_count"] <= 600
    assert broad["supporting_chunks"][0]["returned_token_count"] > compact["supporting_chunks"][0]["returned_token_count"]
    assert broad["supporting_chunks"][0]["returned_token_count"] <= 1800


def test_retrieve_context_latest_query_boosts_recent_documents(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    old_note = svc.paths.inbox / "old-policy.md"
    new_note = svc.paths.inbox / "new-policy.md"
    old_note.write_text(
        "---\n"
        "title: Retrieval Policy Reference\n"
        "---\n\n"
        "# Retrieval Policy Reference\n\n"
        "retrieval policy shared marker old-only detail.\n",
        encoding="utf-8",
    )
    new_note.write_text(
        "---\n"
        "title: Retrieval Policy Reference\n"
        "---\n\n"
        "# Retrieval Policy Reference\n\n"
        "retrieval policy shared marker new-only detail.\n",
        encoding="utf-8",
    )
    svc.ingest()
    with connection(svc.paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE documents SET created_at = ?, ingested_at = ? WHERE source_path = ?",
            ("2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00", str(old_note)),
        )
        conn.execute(
            "UPDATE documents SET created_at = ?, ingested_at = ? WHERE source_path = ?",
            ("2026-05-18T00:00:00+00:00", "2026-05-18T00:00:00+00:00", str(new_note)),
        )

    context = svc.retrieve_context("latest retrieval policy shared marker")

    assert "new-only" in context["supporting_chunks"][0]["text"]
    assert any("recency intent boost" in reason for reason in context["supporting_chunks"][0]["selection_reasons"])


def test_memory_audit_warns_on_missing_source(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    memory_id = svc.propose_memory("PreferenceMemory", "global", "User prefers concise answers.", [], 0.8)

    audit = audit_memories(svc.paths)

    assert audit["errors"] == []
    assert any(memory_id in warning for warning in audit["warnings"])


def test_memory_audit_accepts_failure_pattern_and_rejects_invalid_type(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    svc.propose_memory("AgentFailurePatternMemory", "agent:codex", "When tests fail, inspect the failing assertion before editing.", ["agent_session:test"], 0.8)
    invalid_id = svc.propose_memory("NopeMemory", "global", "Invalid memory type.", ["agent_session:test"], 0.4)

    audit = audit_memories(svc.paths)

    assert any(invalid_id in error and "invalid memory_type" in error for error in audit["errors"])
    assert not any("AgentFailurePatternMemory" in error for error in audit["errors"])


def test_memory_review_status_updates(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    approved_id = svc.propose_memory("AgentFailurePatternMemory", "agent:codex", "Approved failure pattern.", ["agent_session:test"], 0.8)
    rejected_id = svc.propose_memory("AgentFailurePatternMemory", "agent:codex", "Rejected failure pattern.", ["agent_session:test"], 0.8)
    archived_id = svc.propose_memory("AgentFailurePatternMemory", "agent:codex", "Archived failure pattern.", ["agent_session:test"], 0.8)

    approved = svc.approve_memory(approved_id)
    rejected = svc.reject_memory(rejected_id, "Too speculative.")
    archived = svc.archive_memory(archived_id)

    assert approved["status"] == "active"
    assert approved["reviewed_at"]
    assert rejected["status"] == "rejected"
    assert rejected["review_reason"] == "Too speculative."
    assert archived["status"] == "archived"


def test_retrieve_context_separates_active_and_candidate_memories(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    active_id = svc.propose_memory("AgentFailurePatternMemory", "global", "Active memory is trusted.", ["agent_session:test"], 0.8)
    candidate_id = svc.propose_memory("AgentFailurePatternMemory", "global", "Candidate memory needs review.", ["agent_session:test"], 0.7)
    svc.approve_memory(active_id)

    context = svc.retrieve_context("Use memory guidance for the current agent task.")

    active_ids = {memory["id"] for memory in context["active_memories"]}
    candidate_ids = {memory["id"] for memory in context["candidate_memories"]}
    assert active_id in active_ids
    assert candidate_id not in active_ids
    assert candidate_id in candidate_ids
    assert active_id not in candidate_ids


def test_failure_memory_proposals_dedupe_existing_active_and_create_proposed(tmp_path: Path, monkeypatch) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    session_id = svc.write_agent_session(
        "Tests failed after editing retrieval.",
        ["src/pkm_brain/service.py"],
        ["uv run pytest tests/test_core.py"],
        "failed",
        ["The agent did not inspect the assertion before changing code."],
    )
    existing_id = svc.propose_memory(
        "AgentFailurePatternMemory",
        "agent:codex",
        "When tests fail, inspect the failing assertion before editing.",
        [f"agent_session:{session_id}"],
        0.8,
    )
    svc.approve_memory(existing_id)

    class FakeProvider:
        name = "fake"
        model = "test"

        def complete(self, prompt: str) -> str:
            return (
                '{"memories": ['
                '{"content": "When tests fail, inspect the failing assertion before editing.", "scope": "agent:codex", '
                f'"source_ids": ["agent_session:{session_id}"], "confidence": 0.8}},'
                '{"content": "When pytest fails after retrieval edits, inspect the failing assertion and selected context before changing ranking code.", '
                '"scope": "agent:codex", '
                f'"source_ids": ["agent_session:{session_id}"], "confidence": 0.82}}'
                "]}"
            )

    monkeypatch.setattr("pkm_brain.memory_proposals.get_provider", lambda provider_name=None: FakeProvider())

    result = propose_failure_memories_from_sources(svc.paths, provider_name="fake")

    assert result["created_count"] == 1
    assert len(result["skipped_duplicates"]) == 1
    created = svc.get_memory(result["memory_ids"][0])
    assert created["memory_type"] == "AgentFailurePatternMemory"
    assert created["status"] == "proposed"


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
