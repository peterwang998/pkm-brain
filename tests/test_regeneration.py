from __future__ import annotations

import json
from pathlib import Path

import pytest

from pkm_brain.cos_actions import propose_action
from pkm_brain.db import connection
from pkm_brain.evals import run_eval
from pkm_brain.extraction import load_extraction_route_targets
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


def test_rebuild_facts_apply_resets_with_contract_seed_and_reapplies_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    write_labeled_extraction_eval(paths)
    write_page(paths.wiki / "concepts" / "legacy.md", managed=True)
    write_page(paths.wiki / "concepts" / "hand.md", managed=False)
    insert_document_with_chunk(paths, "doc_note", "markdown_note")
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, page_hint, source_ids, observed_at,
              confidence, status, confirmed_by_user, metadata, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fact_legacy",
                "A confirmed fact should survive regeneration.",
                "concept:legacy:summary",
                "concepts/legacy.md",
                json.dumps(["document:doc_note"]),
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
            INSERT INTO entities(id, name, entity_type, aliases, status, source_ids, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("entity_legacy", "Legacy", "concept", "[]", "active", "[]", "2026-07-05T00:00:00+00:00"),
        )
        conn.execute(
            """
            INSERT INTO fact_entities(
              id, fact_id, entity_id, is_primary, mention_text, mention_kind, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("fe_legacy", "fact_legacy", "entity_legacy", 1, "Legacy", "named", "2026-07-05T00:00:00+00:00"),
        )
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, fact_ids, question, options, status, context, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "question_legacy",
                "conflict",
                json.dumps(["fact_legacy"]),
                "Legacy question?",
                "[]",
                "open",
                "{}",
                "2026-07-05T00:00:00+00:00",
            ),
        )

    def fake_extract_recent_documents(
        paths_arg: BrainPaths,
        **kwargs: object,
    ) -> dict[str, object]:
        assert paths_arg == paths
        assert kwargs["shadow"] is False
        assert kwargs["changed_only"] is False
        assert kwargs["critic_disagreement_mode"] == "reject"
        assert kwargs["offset"] == 0
        route_targets = load_extraction_route_targets(paths_arg)
        assert route_targets["concepts/legacy.md"]["_routing_source"] == "page_contract"
        assert not (paths_arg.wiki / "concepts" / "legacy.md").exists()
        insert_active_fact(
            paths_arg,
            "fact_rebuilt",
            "A confirmed fact should survive regeneration.",
            "concepts/legacy.md",
        )
        return {
            "status": "ok",
            "documents": [{"document_id": "doc_note"}],
            "candidates": [{"id": "candidate_rebuilt"}],
            "actions": [{"id": "cosact_rebuilt", "status": "applied", "critic_decision": "agree"}],
            "timing": {"total_duration_ms": 1},
            "validation": {},
        }

    monkeypatch.setattr("pkm_brain.regeneration.cos_provider_status", ready_provider_status)
    monkeypatch.setattr("pkm_brain.regeneration.extract_recent_documents", fake_extract_recent_documents)

    result = rebuild_facts_from_sources(
        paths,
        from_sources=True,
        dry_run=False,
        limit=1,
    )

    assert result["status"] == "applied"
    assert result["reset"] is True
    assert result["seeded_contracts"]["applied_count"] == 1
    assert Path(result["artifacts"]["backup"]["db_backup"]).exists()
    assert Path(result["artifacts"]["human_state_export"]["path"]).exists()
    assert result["reapplied_confirmations"]["matched_count"] == 1
    assert (paths.wiki / "concepts" / "hand.md").exists()
    assert (paths.wiki / "concepts" / "legacy.md").exists()
    with connection(paths.sqlite_path) as conn:
        legacy = conn.execute("SELECT status FROM facts WHERE id = 'fact_legacy'").fetchone()
        rebuilt = conn.execute(
            "SELECT status, confirmed_by_user FROM facts WHERE id = 'fact_rebuilt'"
        ).fetchone()
        legacy_entity = conn.execute("SELECT status FROM entities WHERE id = 'entity_legacy'").fetchone()
        fact_entity_count = conn.execute("SELECT COUNT(*) FROM fact_entities WHERE id = 'fe_legacy'").fetchone()[0]
        legacy_question = conn.execute("SELECT status FROM open_questions WHERE id = 'question_legacy'").fetchone()
        contract_count = conn.execute("SELECT COUNT(*) FROM page_contracts WHERE status = 'active'").fetchone()[0]

    assert legacy["status"] == "archived"
    assert rebuilt["status"] == "active"
    assert rebuilt["confirmed_by_user"] == 1
    assert legacy_entity["status"] == "archived"
    assert fact_entity_count == 0
    assert legacy_question["status"] == "dismissed"
    assert contract_count == 1


def test_rebuild_facts_apply_continuation_does_not_reset_existing_rebuilt_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    write_labeled_extraction_eval(paths)
    insert_document_with_chunk(paths, "doc_one", "markdown_note")
    insert_document_with_chunk(paths, "doc_two", "markdown_note")
    insert_active_fact(paths, "fact_existing_rebuilt", "Existing rebuilt fact.", "concepts/existing.md")

    def fake_extract_recent_documents(
        paths_arg: BrainPaths,
        **kwargs: object,
    ) -> dict[str, object]:
        assert kwargs["offset"] == 1
        insert_active_fact(paths_arg, "fact_continued", "Continuation fact.", "concepts/continued.md")
        return {
            "status": "ok",
            "documents": [{"document_id": "doc_two"}],
            "candidates": [],
            "actions": [],
            "timing": {},
            "validation": {},
        }

    monkeypatch.setattr("pkm_brain.regeneration.cos_provider_status", ready_provider_status)
    monkeypatch.setattr("pkm_brain.regeneration.extract_recent_documents", fake_extract_recent_documents)

    result = rebuild_facts_from_sources(
        paths,
        from_sources=True,
        dry_run=False,
        limit=1,
        offset=1,
    )

    assert result["reset"] is False
    assert result["reset_result"]["status"] == "skipped"
    with connection(paths.sqlite_path) as conn:
        existing = conn.execute(
            "SELECT status FROM facts WHERE id = 'fact_existing_rebuilt'"
        ).fetchone()
        continued = conn.execute(
            "SELECT status FROM facts WHERE id = 'fact_continued'"
        ).fetchone()
    assert existing["status"] == "active"
    assert continued["status"] == "active"


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


def write_labeled_extraction_eval(paths: BrainPaths) -> None:
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


def ready_provider_status(paths: BrainPaths) -> dict[str, object]:
    assert paths.home
    roles = [
        {"role": role, "configured": True, "missing": []}
        for role in ("extractor", "resolver", "gardener", "synthesizer", "critic", "auditor")
    ]
    return {"roles": roles, "warnings": []}


def insert_document_with_chunk(
    paths: BrainPaths, document_id: str, source_type: str
) -> None:
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO documents(
              id, source_type, title, source_path, raw_path, content_hash,
              created_at, ingested_at, tags, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id,
                source_type,
                f"Document {document_id}",
                f"/tmp/{document_id}.md",
                f"/tmp/raw/{document_id}.md",
                f"hash-{document_id}",
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
                f"chunk_{document_id}",
                document_id,
                0,
                "raw",
                f"{document_id} source text.",
                "",
                0,
                30,
                5,
                f"hash-chunk-{document_id}",
                "2026-07-05T00:00:00+00:00",
            ),
        )


def insert_active_fact(
    paths: BrainPaths, fact_id: str, statement: str, page_hint: str
) -> None:
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, page_hint, section_hint, source_ids,
              observed_at, confidence, status, metadata, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fact_id,
                statement,
                f"concept:{Path(page_hint).stem}:summary",
                page_hint,
                "Summary",
                json.dumps(["document:doc_note"]),
                "2026-07-05T00:00:00+00:00",
                0.9,
                "active",
                "{}",
                "2026-07-05T00:00:00+00:00",
            ),
        )
