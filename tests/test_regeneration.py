from __future__ import annotations

import json
from pathlib import Path

import pytest

from pkm_brain.cos_actions import propose_action
from pkm_brain.db import connection
from pkm_brain.evals import run_eval
from pkm_brain.paths import BrainPaths
from pkm_brain.regeneration import (
    backup_runtime_brain,
    export_human_state,
    rebuild_facts_from_sources,
)
from pkm_brain.service import BrainService


def test_export_human_state_captures_preservation_inputs(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    write_page(paths.wiki / "concepts" / "hand-note.md", managed=False)
    write_page(paths.wiki / "concepts" / "managed-note.md", managed=True)
    write_page(paths.wiki / "references" / "raw-log.md", managed=False)
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, page_hint, source_ids, observed_at,
              confidence, status, confirmed_by_user, metadata, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fact_confirmed",
                "A confirmed fact should be preserved.",
                "concept:test:summary",
                "concepts/test.md",
                "[]",
                "2026-07-05T00:00:00+00:00",
                0.9,
                "active",
                1,
                "{}",
                "2026-07-05T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, page_hint, source_ids, observed_at,
              confidence, status, conflict_group_id, metadata, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fact_conflicted",
                "A conflicted fact should be exported as review state.",
                "concept:test:summary",
                "concepts/test.md",
                "[]",
                "2026-07-05T00:00:00+00:00",
                0.7,
                "conflicted",
                "conflict_test",
                "{}",
                "2026-07-05T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, entity_key, page_hint, fact_ids, question, options,
              status, answer, context, created_at, answered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "question_answered",
                "conflict",
                "concept:test:summary",
                "concepts/test.md",
                "[]",
                "Which fact wins?",
                "[]",
                "answered",
                json.dumps({"selected_fact_id": "fact_confirmed"}),
                "{}",
                "2026-07-05T00:00:00+00:00",
                "2026-07-05T00:00:00+00:00",
            ),
        )

    result = export_human_state(paths, output_dir=tmp_path / "exports")
    payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))

    assert result["counts"]["confirmed_facts"] == 1
    assert result["counts"]["answered_or_dismissed_open_questions"] == 1
    assert result["counts"]["conflicted_facts"] == 1
    assert result["counts"]["hand_authored_pages"] == 1
    assert payload["hand_authored_pages"][0]["relative_path"] == "concepts/hand-note.md"


def test_backup_runtime_brain_copies_db_and_wiki(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    write_page(paths.wiki / "concepts" / "hand-note.md", managed=False)

    result = backup_runtime_brain(paths, output_dir=tmp_path / "backup")

    assert Path(result["db_backup"]).exists()
    assert Path(result["wiki_backup"]).exists()
    assert Path(result["wiki_backup"], "concepts", "hand-note.md").exists()
    assert Path(result["path"], "backup_metadata.json").exists()


def test_rebuild_facts_dry_run_reports_scope_without_mutating(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO documents(
              id, source_type, title, source_path, raw_path, content_hash,
              created_at, ingested_at, tags, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "doc_note",
                "markdown_note",
                "Semantic retrieval note",
                "/tmp/source.md",
                "/tmp/raw.md",
                "hash-doc",
                "2026-07-05T00:00:00+00:00",
                "2026-07-05T00:00:00+00:00",
                "[]",
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
                "chunk_note",
                "doc_note",
                0,
                "raw",
                "This note discusses semantic retrieval with sentence embeddings.",
                "",
                0,
                60,
                8,
                "hash-chunk",
                "2026-07-05T00:00:00+00:00",
            ),
        )

    result = rebuild_facts_from_sources(
        paths,
        from_sources=True,
        dry_run=True,
        source_types=["markdown_note"],
        limit=1,
    )

    assert result["status"] == "dry_run"
    assert result["extraction_eval_gate"]["ready"] is False
    assert result["extraction_eval_gate"]["reason"] == "no extraction eval report found"
    assert result["scope"]["selected_document_count"] == 1
    assert result["scope"]["selected_window_count"] == 1
    assert result["scope"]["selected_documents"][0]["document_id"] == "doc_note"
    assert any("Autonomous fact upsert is blocked" in note for note in result["notes"])


def test_rebuild_facts_dry_run_reports_labeled_eval_gate_ready(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    paths.evals.mkdir(parents=True, exist_ok=True)
    (paths.evals / "extraction_labels.jsonl").write_text(
        json.dumps(
            {
                "id": "clean_routed_fact",
                "statement": "A clean fact is supported and routed.",
                "page_hint": "concepts/clean.md",
                "expected_page_hint": "concepts/clean.md",
                "keep": True,
                "supported_by_quote": True,
                "route_correct": True,
                "auto_eligible": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_eval(paths, suite="extraction")

    result = rebuild_facts_from_sources(paths, from_sources=True, dry_run=True)

    assert result["extraction_eval_gate"]["ready"] is True
    assert result["extraction_eval_gate"]["label_policy"] == "labeled"
    assert result["extraction_eval_gate"]["label_case_count"] == 1


def test_rebuild_facts_apply_is_intentionally_blocked(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    with pytest.raises(ValueError, match="destructive rebuild-facts apply"):
        rebuild_facts_from_sources(paths, from_sources=True, dry_run=False)


def test_propose_action_creates_missing_curation_run_for_run_id(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    action = propose_action(
        paths,
        "fact_upsert",
        run_id="cosrun_test",
        action_payload={"fact": {"id": "fact_test"}},
    )

    with connection(paths.sqlite_path) as conn:
        run = conn.execute("SELECT * FROM wiki_curation_runs WHERE id = ?", ("cosrun_test",)).fetchone()
    assert action["run_id"] == "cosrun_test"
    assert run is not None
    assert run["group_by"] == "cos_action"


def write_page(path: Path, *, managed: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "---",
                f"id: {path.stem}",
                f"title: {path.stem}",
                "page_type: concept",
                "status: active",
                f"managed: {'true' if managed else 'false'}",
                "source_ids: []",
                "related: []",
                "tags: []",
                "---",
                "",
                "# Note",
                "",
                "Body.",
                "",
            ]
        ),
        encoding="utf-8",
    )
