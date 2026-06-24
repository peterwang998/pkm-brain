from __future__ import annotations

import json
from pathlib import Path

from pkm_brain.audit import audit_memories, provenance_check
from pkm_brain.chunking import chunk_text, sanitize_agent_session_log
from pkm_brain.db import connection
from pkm_brain.indexes import lancedb_stats, table_names, upsert_vectors
from pkm_brain.memory_proposals import propose_failure_memories_from_sources, propose_memories_from_lineage
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.title_utils import MAX_DOCUMENT_TITLE_CHARS, TITLE_TRUNCATION_SUFFIX
from pkm_brain.util import text_sha256, token_count
from pkm_brain.wiki import lint_wiki, parse_frontmatter, synthesize_wiki
from pkm_brain.wiki_compiler import clean_agent_log_preview, select_compiler_sources
from pkm_brain.wiki_facts import rebuild_fact_retrieval_index
from pkm_brain.wiki_proposals import apply_wiki_proposal, create_wiki_proposal, inspect_wiki_proposal, latest_documents, record_wiki_interview


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
    assert search["citation_snapshots"][0]["type"] == "chunk"
    assert search["citation_snapshots"][0]["document_id"]
    assert search["citation_snapshots"][0]["logical_source_key"]
    assert search["citation_snapshots"][0]["content_hash"]
    assert "SQLite is the canonical metadata store" in search["citation_snapshots"][0]["text"]
    with connection(svc.paths.sqlite_path) as conn:
        retrieval = conn.execute(
            "SELECT citation_snapshots FROM retrieval_events WHERE id = ?",
            (search["event_id"],),
        ).fetchone()
    stored_snapshots = json.loads(retrieval["citation_snapshots"])
    assert stored_snapshots[0]["type"] == "chunk"
    context = svc.retrieve_context("explain sqlite metadata", project="pkm-system")
    assert context["supporting_chunks"]
    assert context["citations"] == context["citation_snapshots"]
    assert context["citation_snapshots"][0]["type"] == "chunk"


def test_ingest_bounds_frontmatter_title_without_dropping_body(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    long_title = "Title " + ("x" * 2000)
    note = svc.paths.inbox / "long-title.md"
    note.write_text(
        "---\n"
        f'title: "{long_title}"\n'
        "---\n\n"
        "# Full Body\n\n"
        "body-content-marker is retained in chunks.\n",
        encoding="utf-8",
    )

    result = svc.ingest()

    assert result.changed == 1
    with connection(svc.paths.sqlite_path) as conn:
        row = conn.execute("SELECT id, title FROM documents").fetchone()
        chunk_text = conn.execute("SELECT GROUP_CONCAT(text, '\n') AS text FROM chunks WHERE document_id = ?", (row["id"],)).fetchone()
    assert len(row["title"]) == MAX_DOCUMENT_TITLE_CHARS
    assert row["title"].endswith(TITLE_TRUNCATION_SUFFIX)
    assert "body-content-marker" in chunk_text["text"]


def test_latest_documents_bounds_historical_giant_titles(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "historical-title.md"
    note.write_text("# Historical Title\n\nShort preview body.\n", encoding="utf-8")
    svc.ingest()
    giant_title = "Historical " + ("y" * 2000)
    with connection(svc.paths.sqlite_path) as conn:
        conn.execute("UPDATE documents SET title = ?", (giant_title,))

    documents = latest_documents(svc.paths, limit=8)

    assert len(documents[0]["title"]) == MAX_DOCUMENT_TITLE_CHARS
    assert documents[0]["title"].endswith(TITLE_TRUNCATION_SUFFIX)
    assert documents[0]["preview"] == "# Historical Title Short preview body."


def test_search_uses_source_aware_reranking_and_backfill(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    meeting = svc.paths.inbox / "alpha-meeting.md"
    meeting.write_text(
        "---\n"
        'source_type: "hyprnote_meeting"\n'
        'title: "Alpha Pricing Meeting"\n'
        "---\n\n"
        "# Meeting\n\nAlpha pricing marker describes durable customer context.\n",
        encoding="utf-8",
    )
    log = svc.paths.inbox / "agent_logs" / "codex" / "alpha-log.md"
    log.parent.mkdir(parents=True)
    log.write_text(
        "---\n"
        'source_type: "agent_session_log"\n'
        'title: "Alpha Pricing Agent Log"\n'
        "---\n\n"
        "- session_meta: You are Codex, a coding agent based on GPT-5.\n"
        "- event_msg: alpha pricing marker command trace noise.\n",
        encoding="utf-8",
    )
    svc.ingest()

    result = svc.search("alpha pricing marker", limit=2, debug=True)

    assert len(result["results"]) == 2
    assert result["results"][0]["source_type"] == "hyprnote_meeting"
    assert result["results"][0]["retrieval_score"] >= result["results"][1]["retrieval_score"]
    assert result["debug"]["selected_chunk_reasons"][0]["retrieval_noise_reasons"] == []
    assert any(
        row["source_type"] == "agent_session_log"
        for row in result["debug"]["reranked_candidates"]
    )


def test_search_agent_query_can_rank_agent_logs_highly(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    log = svc.paths.inbox / "agent_logs" / "codex" / "tool-session.md"
    log.parent.mkdir(parents=True)
    log.write_text(
        "---\n"
        'source_type: "agent_session_log"\n'
        'title: "Codex Agent Tool Session"\n'
        "---\n\n"
        "## User Requests\n\nInvestigate the agent tool session retry marker.\n",
        encoding="utf-8",
    )
    note = svc.paths.inbox / "tool-note.md"
    note.write_text("# Tool Note\n\nInvestigate the tool session retry marker.\n", encoding="utf-8")
    svc.ingest()

    result = svc.search("agent tool session retry marker", limit=2, debug=True)

    assert result["results"][0]["source_type"] == "agent_session_log"
    assert any("source_type agent_session_log" in reason for reason in result["results"][0]["selection_reasons"])


def test_oversized_block_splits_with_overlap() -> None:
    text = " ".join(f"word{i}" for i in range(2600))

    chunks = chunk_text(text, "agent_session_log", target_tokens=1000, overlap_tokens=200)

    assert len(chunks) == 3
    assert all(chunk.token_count <= 1000 for chunk in chunks)
    assert chunks[0].text.split()[-200:] == chunks[1].text.split()[:200]
    assert chunks[1].text.split()[-200:] == chunks[2].text.split()[:200]
    assert [chunk.chunk_index for chunk in chunks] == [0, 1, 2]
    assert chunks[0].start_offset < chunks[1].start_offset < chunks[2].start_offset
    assert chunks[0].end_offset < chunks[1].end_offset < chunks[2].end_offset


def test_small_block_keeps_single_chunk() -> None:
    chunks = chunk_text("short note\n\nwith two paragraphs", "agent_session_log", target_tokens=1000, overlap_tokens=200)

    assert len(chunks) == 1
    assert chunks[0].text == "short note\n\nwith two paragraphs"


def test_agent_session_log_chunking_sanitizes_retrieval_and_tool_noise() -> None:
    text = (
        "---\n"
        'source_type: "agent_session_log"\n'
        "---\n\n"
        "## User Requests\n\n"
        "Keep the durable request.\n\n"
        '{"supporting_chunks": [{"chunk_id": "chunk_noise", "document_id": "doc_noise", '
        '"content_hash": "abc", "text": "retrieved text should not be indexed"}], '
        '"citation_snapshots": [{"type": "chunk", "text": "citation text"}]}\n\n'
        "Output:\n"
        + ("tool-line " * 700)
        + "\n\n"
        "Referenced chunk_stable and document:doc_stable.\n"
    )

    chunks = chunk_text(text, "agent_session_log", target_tokens=1000, overlap_tokens=200)
    indexed = "\n".join(chunk.text for chunk in chunks)

    assert "Keep the durable request." in indexed
    assert "Referenced chunk_stable" in indexed
    assert "retrieved text should not be indexed" not in indexed
    assert "citation_snapshots" not in indexed
    assert "tool-line" not in indexed
    assert "[omitted retrieved context dump]" in indexed
    assert "[omitted large tool output]" in indexed


def test_agent_session_log_sanitizer_preserves_message_progression_and_compact_tools() -> None:
    indexed = sanitize_agent_session_log(
        "---\n"
        'source_type: "agent_session_log"\n'
        "---\n\n"
        "## User Requests\n\n"
        "User asked to diagnose nightly ingest failures.\n\n"
        "## Assistant Responses\n\n"
        "I will inspect the failing run and verify the index after cleanup.\n\n"
        "## Tool / Command Activity\n\n"
        "- tool_call: functions.exec_command cmd='uv run brain index doctor --home /Users/Peter/brain'\n"
        "- event_msg: index doctor returned missing_vector_count=0 and stale_vector_count=0.\n\n"
        "## Assistant Responses\n\n"
        "Nightly ingest is healthy after the cleanup.\n"
    )

    assert "User asked to diagnose nightly ingest failures." in indexed
    assert "I will inspect the failing run" in indexed
    assert "uv run brain index doctor" in indexed
    assert "missing_vector_count=0" in indexed
    assert "Nightly ingest is healthy after the cleanup." in indexed
    assert "[omitted" not in indexed


def test_agent_session_log_sanitizer_truncates_verbose_tool_output_without_losing_summary() -> None:
    noisy_output = "\n".join(f"stack frame {index}: verbose dependency resolver trace" for index in range(80))
    indexed = sanitize_agent_session_log(
        "## User Requests\n\n"
        "User asked to add regression tests for agent session sanitization.\n\n"
        "## Tool / Command Activity\n\n"
        "- tool_call: functions.exec_command cmd='uv run pytest tests/test_core.py'\n"
        "Output:\n"
        "FAILED tests/test_core.py::test_agent_session_marker - AssertionError: marker missing\n"
        f"{noisy_output}\n"
        "Process exited with code 1\n"
        "Wall time: 12.3 seconds\n\n"
        "## Assistant Responses\n\n"
        "I fixed the sanitizer and reran the focused test.\n"
    )

    assert "User asked to add regression tests" in indexed
    assert "uv run pytest tests/test_core.py" in indexed
    assert "FAILED tests/test_core.py::test_agent_session_marker" in indexed
    assert "Process exited with code 1" in indexed
    assert "Wall time: 12.3 seconds" in indexed
    assert "I fixed the sanitizer" in indexed
    assert "stack frame 0" in indexed
    assert "stack frame 4" not in indexed
    assert "[omitted large tool output]" in indexed


def test_agent_session_log_sanitizer_preserves_short_tool_arguments() -> None:
    indexed = sanitize_agent_session_log(
        "## Tool / Command Activity\n\n"
        '- function_call: {"name": "update_plan", "arguments": {"step": "Add focused sanitizer tests"}}\n\n'
        "## Assistant Responses\n\n"
        "The compact tool call explains why the next edit happened.\n"
    )

    assert '"name": "update_plan"' in indexed
    assert "Add focused sanitizer tests" in indexed
    assert "compact tool call explains" in indexed
    assert "[omitted" not in indexed


def test_agent_session_log_sanitizer_redacts_retrieval_negative_controls() -> None:
    indexed = sanitize_agent_session_log(
        "## User Requests\n\n"
        "Keep fixed negative controls like ZephyrMart geothermal coffee roasting in Iceland.\n\n"
        "## Assistant Responses\n\n"
        "The eval report mentions zephyrmart geothermal coffee roasting in iceland again.\n"
    )

    assert "ZephyrMart geothermal coffee roasting in Iceland" not in indexed
    assert "zephyrmart geothermal coffee roasting in iceland" not in indexed
    assert indexed.count("[omitted retrieval negative-control fixture]") == 2
    assert "Keep fixed negative controls like" in indexed
    assert "The eval report mentions" in indexed


def test_ingested_agent_session_logs_do_not_index_retrieval_negative_controls(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    log = svc.paths.inbox / "agent_logs" / "codex" / "eval-session.md"
    log.parent.mkdir(parents=True)
    log.write_text(
        "---\n"
        'source_type: "agent_session_log"\n'
        'session_id: "eval-session"\n'
        "---\n\n"
        "## User Requests\n\n"
        "Run negative control ZephyrMart geothermal coffee roasting in Iceland.\n",
        encoding="utf-8",
    )

    result = svc.ingest()

    assert result.changed == 1
    with connection(svc.paths.sqlite_path) as conn:
        document = conn.execute("SELECT raw_path FROM documents").fetchone()
        indexed_text = conn.execute("SELECT text FROM chunks").fetchone()["text"]

    assert "ZephyrMart geothermal coffee roasting in Iceland" in Path(document["raw_path"]).read_text(encoding="utf-8")
    assert "ZephyrMart geothermal coffee roasting in Iceland" not in indexed_text
    assert "[omitted retrieval negative-control fixture]" in indexed_text


def test_index_doctor_reports_lancedb_health(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "sqlite-decision.md"
    note.write_text("# SQLite Decision\n\nSQLite stores metadata and chunks.\n", encoding="utf-8")
    svc.ingest()

    result = svc.index_doctor()

    assert result["status"] == "ok"
    assert result["sqlite_chunks"] >= 1
    assert result["lancedb"]["rows"] == result["sqlite_chunks"]
    assert result["missing_vector_count"] == 0
    assert result["stale_vector_count"] == 0


def test_rebuild_vector_index_from_sqlite_chunks(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "sqlite-decision.md"
    note.write_text("# SQLite Decision\n\nSQLite stores metadata and chunks.\n", encoding="utf-8")
    svc.ingest()
    before = lancedb_stats(svc.paths.lancedb_path)

    result = svc.rebuild_vector_index()

    assert result["status"] == "ok"
    assert result["sqlite_chunks"] == before["rows"]
    assert result["after"]["rows"] == before["rows"]
    assert result["backup_retained"] is True
    assert Path(result["backup_path"]).exists()
    search = svc.search("SQLite metadata", limit=3)
    assert search["results"]


def test_optimize_indexes_is_safe_when_lancedb_exists(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "sqlite-decision.md"
    note.write_text("# SQLite Decision\n\nSQLite stores metadata and chunks.\n", encoding="utf-8")
    svc.ingest()

    result = svc.optimize_indexes()

    assert result["status"] == "ok"
    assert result["after"]["rows"] == result["before"]["rows"]


def test_reindex_chunks_rewrites_existing_oversized_document(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    raw_path = svc.paths.raw / "agent_session_log" / "2026" / "05" / "oversized.md"
    raw_path.parent.mkdir(parents=True)
    text = " ".join(f"token{i}" for i in range(2600))
    raw_path.write_text(text, encoding="utf-8")
    document_id = "doc_oversized"
    chunk_id = "chunk_oversized"

    with connection(svc.paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO documents(
              id, source_type, title, source_path, raw_path, content_hash,
              origin_node_id, logical_source_key, created_at, ingested_at,
              project, tags, version, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                "agent_session_log",
                "Oversized Session",
                str(raw_path),
                str(raw_path),
                text_sha256(text),
                "<local>",
                str(raw_path),
                "2026-05-20T00:00:00+00:00",
                "2026-05-20T00:00:00+00:00",
                None,
                "[]",
                1,
                "active",
            ),
        )
        conn.execute(
            """
            INSERT INTO chunks(
              id, document_id, chunk_index, corpus_type, text, heading_path,
              start_offset, end_offset, token_count, content_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chunk_id,
                document_id,
                0,
                "raw",
                text,
                "",
                0,
                len(text),
                token_count(text),
                text_sha256(text),
                "2026-05-20T00:00:00+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO chunk_fts(chunk_id, title, text, heading_path, project, tags) VALUES (?, ?, ?, ?, ?, ?)",
            (chunk_id, "Oversized Session", text, "", "", ""),
        )
    upsert_vectors(
        svc.paths.lancedb_path,
        [
            {
                "chunk_id": chunk_id,
                "document_id": document_id,
                "text": text,
                "vector": svc.embedding_provider.embed([text])[0],
            }
        ],
    )

    dry_run = svc.reindex_chunks(dry_run=True, target_tokens=1000, overlap_tokens=200)
    result = svc.reindex_chunks(target_tokens=1000, overlap_tokens=200)

    assert dry_run["status"] == "dry_run"
    assert dry_run["affected_documents"] == 1
    assert dry_run["documents"][0]["current_chunks"] == 1
    assert dry_run["documents"][0]["projected_chunks"] == 3
    assert result["status"] == "ok"
    assert result["rewritten_chunks"] == 3
    with connection(svc.paths.sqlite_path) as conn:
        chunk_rows = list(conn.execute("SELECT * FROM chunks WHERE document_id = ? ORDER BY chunk_index", (document_id,)))
        fts_count = conn.execute("SELECT COUNT(*) FROM chunk_fts WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id = ?)", (document_id,)).fetchone()[0]
    assert len(chunk_rows) == 3
    assert fts_count == 3
    assert max(row["token_count"] for row in chunk_rows) <= 1000
    assert lancedb_stats(svc.paths.lancedb_path)["rows"] == 3
    assert svc.index_doctor()["status"] == "ok"
    search = svc.search("token999 token1000", limit=5)
    assert search["results"]


def test_provenance_check_allows_snapshot_drift_and_flags_malformed_snapshots(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "snapshot-drift.md"
    note.write_text("# Snapshot Drift\n\nsnapshot drift marker original evidence.\n", encoding="utf-8")
    svc.ingest()
    context = svc.retrieve_context("snapshot drift marker")
    snapshot = context["citation_snapshots"][0]

    with connection(svc.paths.sqlite_path) as conn:
        conn.execute("DELETE FROM chunk_fts")
        conn.execute("DELETE FROM chunks")

    audit = provenance_check(svc.paths)

    assert audit["errors"] == []
    assert any(snapshot["chunk_id"] in warning for warning in audit["warnings"])
    assert any("no longer matches a current chunk" in warning for warning in audit["warnings"])

    with connection(svc.paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO retrieval_events(
              id, query, timestamp, caller, returned_chunk_ids, selected_chunk_ids, citation_snapshots, debug
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "retrieval_malformed",
                "bad",
                "2026-05-20T00:00:00+00:00",
                "test",
                "[]",
                "[]",
                json.dumps({"not": "a list"}),
                "{}",
            ),
        )

    audit = provenance_check(svc.paths)

    assert any("malformed citation_snapshots" in error for error in audit["errors"])


def test_reset_retrieval_index_preserves_documents_and_rebuilds_artifacts(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "reset-index.md"
    note.write_text("# Reset Index\n\nreset index marker preserves document identity.\n", encoding="utf-8")
    svc.ingest()
    with connection(svc.paths.sqlite_path) as conn:
        document_id = conn.execute("SELECT id FROM documents").fetchone()["id"]
        old_chunk_ids = {row["id"] for row in conn.execute("SELECT id FROM chunks")}

    wiki_dir = svc.paths.wiki / "concepts"
    wiki_dir.mkdir(parents=True)
    (wiki_dir / "reset-index.md").write_text(
        "---\n"
        "title: Reset Index\n"
        "page_type: concept\n"
        "id: concept-reset-index\n"
        "status: active\n"
        f"source_ids:\n  - document:{document_id}\n"
        "related: []\n"
        "tags: []\n"
        "---\n\n"
        "# Reset Index\n\n"
        "## Summary\n\nreset index marker wiki evidence.\n",
        encoding="utf-8",
    )
    batch_id = svc.propose_wiki_update(
        "Reset index proposal",
        "Verify document references survive index reset.",
        [f"document:{document_id}"],
        [
            {
                "target_path": "concepts/reset-index.md",
                "operation": "append_section",
                "section_name": "Notes",
                "proposed_markdown": "## Notes\n\nPreserve document identity.\n",
                "rationale": "Regression coverage.",
                "source_ids": [f"document:{document_id}"],
                "confidence": 0.8,
            }
        ],
        0.8,
    )
    svc.retrieve_context("reset index marker")
    svc.record_context_feedback("document", f"document:{document_id}", useful=True)
    upsert_vectors(
        svc.paths.lancedb_path,
        [
            {
                "chunk_id": "chunk_stale",
                "document_id": "doc_stale",
                "text": "stale vector",
                "vector": svc.embedding_provider.embed(["stale vector"])[0],
            }
        ],
    )

    result = svc.reset_retrieval_index()

    assert result["status"] == "ok"
    with connection(svc.paths.sqlite_path) as conn:
        document_ids = {row["id"] for row in conn.execute("SELECT id FROM documents")}
        new_chunk_ids = {row["id"] for row in conn.execute("SELECT id FROM chunks")}
        retrieval_count = conn.execute("SELECT COUNT(*) FROM retrieval_events").fetchone()[0]
        exposed_count = conn.execute(
            "SELECT COUNT(*) FROM context_lineage_events WHERE retrieval_event_id IS NOT NULL"
        ).fetchone()[0]
        feedback_count = conn.execute(
            "SELECT COUNT(*) FROM context_lineage_events WHERE event_type = 'explicit_useful'"
        ).fetchone()[0]
        fts_count = conn.execute("SELECT COUNT(*) FROM chunk_fts").fetchone()[0]
        proposal_sources = json.loads(
            conn.execute("SELECT source_ids FROM wiki_change_batches WHERE id = ?", (batch_id,)).fetchone()["source_ids"]
        )

    assert document_ids == {document_id}
    assert new_chunk_ids
    assert new_chunk_ids.isdisjoint(old_chunk_ids)
    assert retrieval_count == 0
    assert exposed_count == 0
    assert feedback_count == 1
    assert fts_count == len(new_chunk_ids)
    assert proposal_sources == [f"document:{document_id}"]
    doctor = svc.index_doctor()
    assert doctor["missing_vector_count"] == 0
    assert doctor["stale_vector_count"] == 0
    assert provenance_check(svc.paths)["errors"] == []


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
            if "You are selecting source documents" in prompt:
                assert "Prefer user-supplied/manual sources" in prompt
                return json.dumps(
                    {
                        "selected_source_ids": [source_id],
                        "rationale": "The architecture note directly supports semantic wiki pages.",
                        "source_rationales": [{"source_id": source_id, "reason": "Primary architecture evidence."}],
                        "warnings": [],
                    }
                )
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
    assert result["llm_compile"]["source_selection"]["selected_by_type"]["markdown_note"] == 1
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
    assert any(citation["type"] == "chunk" for citation in context["citations"])
    assert any(
        citation["type"] == "wiki_page"
        and citation["relative_path"] == "concepts/wiki-synthesis-layer.md"
        and any(source_id.startswith("document:") for source_id in citation["source_ids"])
        for citation in context["citations"]
    )


def test_wiki_source_selection_uses_llm_choice_without_hard_agent_log_cap() -> None:
    documents: list[dict[str, object]] = []
    for index in range(8):
        documents.append(
            {
                "id": f"doc_log_{index}",
                "title": f"Recent Agent Log {index}",
                "source_type": "agent_session_log",
                "source_path": f"/tmp/log-{index}.md",
                "ingested_at": f"2026-05-20T0{index}:00:00+00:00",
            }
        )
    documents.extend(
        [
            {
                "id": "doc_meeting",
                "title": "Older Meeting",
                "source_type": "hyprnote_meeting",
                "source_path": "/tmp/meeting.md",
                "ingested_at": "2026-05-01T00:00:00+00:00",
            },
            {
                "id": "doc_note",
                "title": "Older Note",
                "source_type": "markdown_note",
                "source_path": "/tmp/note.md",
                "ingested_at": "2026-05-02T00:00:00+00:00",
            },
        ]
    )

    class FakeProvider:
        name = "fake"
        model = "test"

        def complete(self, prompt: str) -> str:
            assert "Prefer user-supplied/manual sources" in prompt
            assert "Recent Agent Log 7" in prompt
            assert "Older Meeting" in prompt
            return json.dumps(
                {
                    "selected_source_ids": [
                        "document:doc_meeting",
                        "document:doc_note",
                        "document:doc_log_7",
                        "document:doc_log_6",
                        "document:doc_log_5",
                    ],
                    "rationale": "The older human sources are semantic evidence, and three logs are relevant implementation history.",
                    "source_rationales": [
                        {"source_id": "document:doc_meeting", "reason": "Meeting evidence."},
                        {"source_id": "document:doc_note", "reason": "Manual note evidence."},
                        {"source_id": "document:doc_log_7", "reason": "Relevant implementation history."},
                    ],
                    "warnings": [],
                }
            )

    selected = select_compiler_sources(FakeProvider(), documents, source_limit=5)

    selected_ids = selected["diagnostics"]["selected_source_ids"]
    assert "document:doc_meeting" in selected_ids
    assert "document:doc_note" in selected_ids
    assert selected["diagnostics"]["selected_by_type"]["agent_session_log"] == 3
    assert "agent_log_cap" not in selected["diagnostics"]
    assert selected["diagnostics"]["dropped_agent_log_count"] == 5
    assert selected["diagnostics"]["selector_rationale"].startswith("The older human sources")


def test_wiki_source_selector_filters_unknown_ids_and_trims_to_limit() -> None:
    documents = [
        {
            "id": "doc_note",
            "title": "Manual Note",
            "source_type": "manual_entry",
            "source_path": "/tmp/note.md",
            "ingested_at": "2026-05-02T00:00:00+00:00",
        },
        {
            "id": "doc_transcript",
            "title": "Meeting Transcript",
            "source_type": "meeting_transcript",
            "source_path": "/tmp/transcript.txt",
            "ingested_at": "2026-05-03T00:00:00+00:00",
        },
    ]

    class FakeProvider:
        name = "fake"
        model = "test"

        def complete(self, prompt: str) -> str:
            assert "preferred semantic source" in prompt
            return json.dumps(
                {
                    "selected_source_ids": [
                        "document:doc_note",
                        "document:missing",
                        "document:doc_transcript",
                    ],
                    "rationale": "Manual source first.",
                    "source_rationales": [],
                    "warnings": ["missing id should be ignored"],
                }
            )

    selected = select_compiler_sources(FakeProvider(), documents, source_limit=1)

    assert selected["diagnostics"]["selected_source_ids"] == ["document:doc_note"]
    assert any("unknown source_id" in warning for warning in selected["diagnostics"]["selector_warnings"])
    assert any("trimmed to requested limit 1" in warning for warning in selected["diagnostics"]["selector_warnings"])


def test_agent_log_compiler_preview_is_cleaned_and_capped() -> None:
    preview = clean_agent_log_preview(
        "# Agent Session\n\n"
        "- session_meta: You are Codex, a coding agent based on GPT-5.\n"
        "## User Requests\n\n"
        "Keep the useful request.\n\n"
        "## Tool / Command Activity\n\n"
        "- event_msg: noisy tool output\n"
        "## Assistant Responses\n\n"
        + " ".join(f"detail{i}" for i in range(400)),
        max_chars=300,
    )

    assert "session_meta" not in preview
    assert "You are Codex" not in preview
    assert "event_msg" not in preview
    assert "Keep the useful request" in preview
    assert len(preview) <= 320


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


def test_retrieve_context_returns_no_strong_match_for_absent_topic(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "ketch-guidepoint.md"
    note.write_text(
        "---\n"
        'source_type: "hyprnote_meeting"\n'
        'title: "Ketch Guidepoint Teleconference"\n'
        "---\n\n"
        "# Meeting: Ketch Guidepoint Teleconference\n\n"
        "Ketch enterprise privacy software helps customers automate consent and data subject requests.\n",
        encoding="utf-8",
    )
    svc.ingest()

    context = svc.retrieve_context(
        "What does Brain know about ZephyrMart geothermal coffee roasting in Iceland?",
        mode="compact",
    )
    search = svc.search("ZephyrMart geothermal coffee roasting Iceland", limit=5)

    assert context["retrieval_verdict"] == "no_strong_match"
    assert context["retrieval_confidence"] < 0.4
    assert context["supporting_chunks"] == []
    assert context["relevant_wiki_pages"] == []
    assert search["retrieval_verdict"] == "no_strong_match"
    assert search["results"] == []
    assert search["relevant_wiki_pages"] == []


def test_retrieve_context_prefers_managed_pages_and_ignores_agent_title_drag(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    log = svc.paths.inbox / "agent_logs" / "codex" / "ai-children-title-only.md"
    log.parent.mkdir(parents=True)
    log.write_text(
        "---\n"
        'source_type: "agent_session_log"\n'
        'agent: "codex"\n'
        'session_id: "ai-children-title-only"\n'
        'title: "AI Chinese children songs publishing business idea"\n'
        "---\n\n"
        "# Agent Session\n\n"
        "## Summary\n\n"
        "SQLite retry backoff implementation notes and shell permission reminders unrelated to the idea.\n",
        encoding="utf-8",
    )
    svc.ingest()
    with connection(svc.paths.sqlite_path) as conn:
        document_id = conn.execute("SELECT id FROM documents WHERE title = ?", ("AI Chinese children songs publishing business idea",)).fetchone()["id"]

    project_dir = svc.paths.wiki / "projects"
    reference_dir = svc.paths.wiki / "references" / "agent_session_log"
    project_dir.mkdir(parents=True)
    reference_dir.mkdir(parents=True)
    (project_dir / "ai-childrens-song-localization-business.md").write_text(
        "---\n"
        "title: AI Children's Song Localization Business\n"
        "page_type: project\n"
        "id: managed-ai-childrens-song-localization-business\n"
        "status: active\n"
        "managed: true\n"
        f"source_ids:\n  - document:{document_id}\n"
        "related: []\n"
        "tags:\n  - managed\n"
        "---\n\n"
        "# AI Children's Song Localization Business\n\n"
        "## Summary\n\n"
        "A managed page for using AI to adapt Chinese children's songs and publish English versions.\n",
        encoding="utf-8",
    )
    (reference_dir / "ai-children-title-only.md").write_text(
        "---\n"
        "title: AI Chinese children songs publishing business idea\n"
        "page_type: reference\n"
        "id: reference-ai-children-title-only\n"
        "status: active\n"
        f"source_ids:\n  - document:{document_id}\n"
        "related: []\n"
        "tags:\n  - agent_session_log\n"
        "---\n\n"
        "# AI Chinese children songs publishing business idea\n\n"
        "## Summary\n\n"
        "Reference page synthesized from an agent session log.\n",
        encoding="utf-8",
    )

    context = svc.retrieve_context(
        "What does Brain know about the AI Chinese children songs publishing business idea?",
        mode="compact",
    )
    search = svc.search("AI Chinese children songs publishing business idea", limit=5)

    assert context["retrieval_verdict"] == "found"
    assert context["relevant_wiki_pages"][0]["relative_path"] == "projects/ai-childrens-song-localization-business.md"
    assert context["relevant_wiki_pages"][0]["managed"] is True
    assert all(chunk["source_type"] != "agent_session_log" for chunk in context["supporting_chunks"])
    assert search["retrieval_verdict"] == "found"
    assert search["relevant_wiki_pages"][0]["relative_path"] == "projects/ai-childrens-song-localization-business.md"


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


def test_retrieve_context_records_exposures_without_rank_boost(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "lineage-note.md"
    note.write_text("# Lineage Note\n\nlineage exposure marker local evidence.\n", encoding="utf-8")
    svc.ingest()
    memory_id = svc.propose_memory("FactMemory", "global", "Lineage exposure memory.", ["document:test"], 0.8)
    svc.approve_memory(memory_id)
    wiki_dir = svc.paths.wiki / "concepts"
    wiki_dir.mkdir(parents=True)
    (wiki_dir / "lineage-exposure.md").write_text(
        "---\n"
        "title: Lineage Exposure\n"
        "page_type: concept\n"
        "id: concept-lineage-exposure\n"
        "status: active\n"
        "source_ids: []\n"
        "related: []\n"
        "tags: []\n"
        "---\n\n"
        "# Lineage Exposure\n\n"
        "## Summary\n\nlineage exposure marker wiki page.\n",
        encoding="utf-8",
    )

    first = svc.retrieve_context("lineage exposure marker", debug=True)
    second = svc.retrieve_context("lineage exposure marker", debug=True)

    with connection(svc.paths.sqlite_path) as conn:
        counts = {
            row["target_type"]: row["count"]
            for row in conn.execute(
                """
                SELECT target_type, COUNT(*) AS count
                FROM context_lineage_events
                WHERE event_type = 'exposed'
                GROUP BY target_type
                """
            )
        }
    assert first["retrieval_event_id"]
    assert counts["chunk"] >= 1
    assert counts["memory"] >= 1
    assert counts["wiki_page"] >= 1
    assert all(chunk.get("lineage_score", 0.0) == 0.0 for chunk in second["supporting_chunks"])


def test_context_feedback_adjusts_ranking_as_capped_tiebreaker(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    first = svc.paths.inbox / "feedback-a.md"
    second = svc.paths.inbox / "feedback-b.md"
    first.write_text("# Feedback A\n\nlineage feedback marker alpha.\n", encoding="utf-8")
    second.write_text("# Feedback B\n\nlineage feedback marker beta.\n", encoding="utf-8")
    svc.ingest()
    with connection(svc.paths.sqlite_path) as conn:
        chunks = [dict(row) for row in conn.execute("SELECT c.id, d.title FROM chunks c JOIN documents d ON d.id = c.document_id")]
    first_chunk = next(row["id"] for row in chunks if row["title"] == "feedback a")
    second_chunk = next(row["id"] for row in chunks if row["title"] == "feedback b")

    svc.record_context_feedback("chunk", second_chunk, useful=True, note="good context")
    svc.record_context_feedback("chunk", first_chunk, useful=False, note="wrong context")
    result = svc.search("lineage feedback marker", limit=2, debug=True)

    by_id = {row["chunk_id"]: row for row in result["results"]}
    assert by_id[second_chunk]["lineage_score"] > 0
    assert by_id[first_chunk]["lineage_score"] < 0
    assert result["results"][0]["chunk_id"] == second_chunk
    assert any("explicit useful" in reason for reason in by_id[second_chunk]["selection_reasons"])
    assert any("explicit not useful" in reason for reason in by_id[first_chunk]["selection_reasons"])


def test_agent_log_ingest_records_stable_id_lineage_once_per_session(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    log = svc.paths.inbox / "agent_logs" / "codex" / "stable-refs.md"
    log.parent.mkdir(parents=True)
    log.write_text(
        "---\n"
        'source_type: "agent_session_log"\n'
        'session_id: "stable-session"\n'
        'title: "Stable refs"\n'
        "---\n\n"
        "Referenced chunk_repeat and chunk_repeat plus document:doc_repeat and concepts/retrieval.md.\n",
        encoding="utf-8",
    )

    svc.ingest()
    svc.ingest()

    with connection(svc.paths.sqlite_path) as conn:
        events = [dict(row) for row in conn.execute("SELECT target_type, target_id, agent_session_id FROM context_lineage_events")]
    assert sorted((event["target_type"], event["target_id"]) for event in events) == [
        ("chunk", "chunk_repeat"),
        ("document", "doc_repeat"),
        ("wiki_page", "concepts/retrieval.md"),
    ]
    assert {event["agent_session_id"] for event in events} == {"stable-session"}


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
    svc.propose_memory("BusinessIdeaMemory", "user:Peter:business_ideas", "A user-scoped business idea.", ["agent_session:test"], 0.8)
    svc.propose_memory("PersonalLogisticsMemory", "user:Peter:home", "A user-scoped logistics fact.", ["agent_session:test"], 0.8)
    invalid_id = svc.propose_memory("NopeMemory", "global", "Invalid memory type.", ["agent_session:test"], 0.4)
    legacy_type_id = svc.propose_memory(
        "infrastructure",
        "global",
        "Legacy infrastructure type must be migrated before audit.",
        ["agent_session:test"],
        0.4,
    )
    invalid_scope_id = svc.propose_memory(
        "FactMemory",
        "user",
        "Legacy user scope must be migrated before audit.",
        ["agent_session:test"],
        0.4,
    )

    audit = audit_memories(svc.paths)

    assert any(invalid_id in error and "invalid memory_type" in error for error in audit["errors"])
    assert any(legacy_type_id in error and "invalid memory_type infrastructure" in error for error in audit["errors"])
    assert any(invalid_scope_id in error and "invalid scope user" in error for error in audit["errors"])
    assert not any("AgentFailurePatternMemory" in error for error in audit["errors"])
    assert not any("BusinessIdeaMemory" in error for error in audit["errors"])
    assert not any("PersonalLogisticsMemory" in error for error in audit["errors"])


def test_memory_audit_warns_for_inactive_legacy_schema(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    archived_id = svc.propose_memory(
        "FactMemory",
        "legacy-scope",
        "Archived legacy memory should not fail nightly maintenance.",
        ["agent_session:test"],
        0.4,
    )
    svc.archive_memory(archived_id)

    audit = audit_memories(svc.paths)

    assert audit["errors"] == []
    assert any(archived_id in warning and "invalid scope legacy-scope" in warning for warning in audit["warnings"])


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
    unrelated_id = svc.propose_memory("FactMemory", "global", "Hightouch diligence discussed an agentic CDP role.", ["agent_session:test"], 0.7)
    svc.approve_memory(active_id)

    context = svc.retrieve_context("Use active trusted memory and candidate review guidance for the current agent task.")

    active_ids = {memory["id"] for memory in context["active_memories"]}
    candidate_ids = {memory["id"] for memory in context["candidate_memories"]}
    assert active_id in active_ids
    assert candidate_id not in active_ids
    assert candidate_id in candidate_ids
    assert active_id not in candidate_ids
    assert unrelated_id not in candidate_ids

    unrelated = svc.retrieve_context("What does Brain know about mango orchard irrigation sensors in Fresno?")
    assert active_id not in {memory["id"] for memory in unrelated["active_memories"]}
    assert candidate_id not in {memory["id"] for memory in unrelated["candidate_memories"]}
    assert unrelated_id not in {memory["id"] for memory in unrelated["candidate_memories"]}


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


def test_lineage_memory_proposal_requires_independent_evidence(tmp_path: Path, monkeypatch) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO context_lineage_events(
              id, target_type, target_id, event_type, agent_session_id,
              weight, metadata, created_at
            ) VALUES (
              'lineage_one', 'chunk', 'chunk_noisy', 'agent_referenced_id',
              'session-one', 0.25, '{}', '2026-05-20T00:00:00+00:00'
            )
            """
        )

    result = propose_memories_from_lineage(svc.paths, provider_name="fake")

    assert result["created"] is False
    assert result["reason"] == "no eligible lineage clusters found"


def test_lineage_memory_proposal_creates_proposed_memory_from_three_sessions(tmp_path: Path, monkeypatch) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        for index in range(3):
            conn.execute(
                """
                INSERT INTO context_lineage_events(
                  id, target_type, target_id, event_type, agent_session_id,
                  weight, metadata, created_at
                ) VALUES (?, 'chunk', 'chunk_shared', 'agent_referenced_id', ?, 0.25, ?, ?)
                """,
                (
                    f"lineage_ref_{index}",
                    f"session-{index}",
                    json.dumps({"source_id": f"document:doc_log_{index}"}),
                    f"2026-05-20T0{index}:00:00+00:00",
                ),
            )

    class FakeProvider:
        name = "fake"
        model = "test"

        def complete(self, prompt: str) -> str:
            assert "chunk:chunk_shared" in prompt
            return json.dumps(
                {
                    "memories": [
                        {
                            "cluster_id": "chunk:chunk_shared",
                            "memory_type": "FactMemory",
                            "scope": "global",
                            "content": "Use the shared retrieval chunk as durable evidence when answering this recurring workflow question.",
                            "source_ids": ["chunk_shared", "document:doc_log_0", "document:doc_log_1", "document:doc_log_2"],
                            "rationale": "Three independent sessions referenced the same stable chunk id.",
                            "confidence": 0.78,
                        }
                    ]
                }
            )

    monkeypatch.setattr("pkm_brain.memory_proposals.get_provider", lambda provider_name=None: FakeProvider())

    result = propose_memories_from_lineage(svc.paths, provider_name="fake")

    assert result["created_count"] == 1
    proposed = svc.get_memory(result["memory_ids"][0])
    assert proposed["status"] == "proposed"
    assert proposed["source_ids"]
    assert result["memories"][0]["independent_session_count"] == 3
    assert result["memories"][0]["rationale"]


def test_lineage_memory_proposal_accepts_two_sessions_plus_useful_feedback(tmp_path: Path, monkeypatch) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        for index in range(2):
            conn.execute(
                """
                INSERT INTO context_lineage_events(
                  id, target_type, target_id, event_type, agent_session_id,
                  weight, metadata, created_at
                ) VALUES (?, 'document', 'doc_shared', 'agent_referenced_id', ?, 0.25, '{}', ?)
                """,
                (f"lineage_doc_ref_{index}", f"session-{index}", f"2026-05-20T0{index}:00:00+00:00"),
            )
    svc.record_context_feedback("document", "document:doc_shared", useful=True)

    class FakeProvider:
        name = "fake"
        model = "test"

        def complete(self, prompt: str) -> str:
            assert "2 distinct sessions plus explicit useful feedback" in prompt
            return json.dumps(
                {
                    "memories": [
                        {
                            "cluster_id": "document:doc_shared",
                            "memory_type": "ProjectMemory",
                            "scope": "global",
                            "content": "Treat the shared document as recurring project context when related questions come up.",
                            "source_ids": ["document:doc_shared"],
                            "rationale": "Two sessions referenced it and a reviewer marked it useful.",
                            "confidence": 0.8,
                        }
                    ]
                }
            )

    monkeypatch.setattr("pkm_brain.memory_proposals.get_provider", lambda provider_name=None: FakeProvider())

    result = propose_memories_from_lineage(svc.paths, provider_name="fake")

    assert result["created_count"] == 1
    assert svc.get_memory(result["memory_ids"][0])["status"] == "proposed"


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


def test_retrieve_context_returns_facts_and_contested_pairs(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        for fact_id, statement, status, group_id, truth_confidence in [
            ("fact_active", "Fact retrieval marker is authoritative.", "active", None, 0.8),
            ("fact_old", "Fact retrieval marker is stale.", "superseded", None, 0.8),
            ("fact_low", "Low confidence fact retrieval marker.", "active", None, 0.2),
            ("fact_left", "Contested marker is blue.", "conflicted", "factconflict_test", 0.8),
            ("fact_right", "Contested marker is green.", "conflicted", "factconflict_test", 0.8),
            ("fact_weak", "Keep the Buy Me a Coffee link as a small About-page item.", "active", None, 0.9),
        ]:
            conn.execute(
                """
                INSERT INTO facts(
                  id, statement, entity_key, page_hint, section_hint, source_ids,
                  observed_at, confidence, status, conflict_group_id, metadata,
                  created_at, truth_confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fact_id,
                    statement,
                    "concept:test:summary",
                    "concepts/test.md",
                    "Summary",
                    "[]",
                    "2026-06-23T00:00:00+00:00",
                    truth_confidence,
                    status,
                    group_id,
                    "{}",
                    "2026-06-23T00:00:00+00:00",
                    truth_confidence,
                ),
            )
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, page_hint, section_hint, source_ids,
              observed_at, confidence, status, metadata, created_at, truth_confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fact_legacy_backfill",
                "The available preview does not include the researched answer, so durable benefit claims should not yet be compiled.",
                "open_loops:optum-fertility-reimbursement-partner-insurance:summary",
                "open_loops/optum-fertility-reimbursement-partner-insurance.md",
                "Summary",
                "[]",
                "2026-06-23T00:00:00+00:00",
                0.68,
                "active",
                json.dumps(
                    {
                        "migration": "wiki_fact_backfill_v1",
                        "source": "existing_wiki",
                    }
                ),
                "2026-06-23T00:00:00+00:00",
                0.68,
            ),
        )
        rebuild_fact_retrieval_index(conn)

    active_context = svc.retrieve_context("fact retrieval marker")
    contested_context = svc.retrieve_context("contested marker")
    negative_context = svc.retrieve_context("ZephyrMart geothermal coffee roasting in Iceland")
    backfill_negative_context = svc.retrieve_context("Pixel lighthouse insurance claims")
    backfill_positive_context = svc.retrieve_context("optum fertility insurance")

    active_ids = {fact["id"] for fact in active_context["relevant_facts"]}
    assert "fact_active" in active_ids
    assert "fact_old" not in active_ids
    assert "fact_low" not in active_ids
    assert "fact_weak" not in active_ids
    contested = [
        fact
        for fact in contested_context["relevant_facts"]
        if fact["id"] == "fact_left"
    ][0]
    assert contested["contested"] is True
    assert {fact["id"] for fact in contested["contested_facts"]} == {
        "fact_left",
        "fact_right",
    }
    assert negative_context["relevant_facts"] == []
    assert backfill_negative_context["relevant_facts"] == []
    assert {
        fact["id"] for fact in backfill_positive_context["relevant_facts"]
    } == {"fact_legacy_backfill"}


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
