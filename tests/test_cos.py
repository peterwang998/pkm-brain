from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from pkm_brain.automation import run_cos_timeout_sweep
from pkm_brain.cos_actions import (
    ACTION_TYPE_SPECS,
    apply_action,
    decide_action,
    get_action,
    propose_action,
    revert_action,
)
from pkm_brain.cos_audit import run_sampled_audit
from pkm_brain.cos_policy import classify_action_risk, evaluate_policy, promote_policy_for_autonomy
from pkm_brain.db import connection
from pkm_brain.extraction import extract_recent_documents, validate_extracted_facts
from pkm_brain.llm import role_env
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


def service_for(tmp_path: Path) -> BrainService:
    return BrainService(BrainPaths.from_value(tmp_path / "brain"), prefer_model_embeddings=False)


def test_policy_first_match_and_truth_defaults_to_l3(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        safe = evaluate_policy(
            conn,
            "canonicalize_page",
            {"deterministic": True, "risk_score": 0.01, "target_page_paths": ["concepts/test.md"]},
        )
        truth = evaluate_policy(
            conn,
            "resolve_conflict",
            {"truth_mutation": True, "risk_score": 0.1},
        )

    assert safe.autonomy_level == "L0"
    assert safe.policy_version == 1
    assert truth.autonomy_level == "L3"
    assert truth.timeout_allowed is False


def test_policy_eval_gate_requires_non_skipped_report(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    report_path = write_eval_report(
        svc.paths.home,
        "topology",
        passed=True,
        metrics={"skipped": True, "reason": "empty fixture"},
    )
    with connection(svc.paths.sqlite_path) as conn:
        decision = evaluate_policy(
            conn,
            "canonicalize_page",
            {
                "deterministic": True,
                "risk_score": 0.01,
                "target_page_paths": ["concepts/test.md"],
                "eval_gate": {"suite": "topology", "report_path": str(report_path)},
            },
        )

    assert decision.autonomy_level == "L3"
    assert decision.policy_decision == "eval_gate_failed"
    assert "skipped" in decision.reason


def test_policy_eval_gate_accepts_passing_report(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    report_path = write_eval_report(
        svc.paths.home,
        "topology",
        passed=True,
        metrics={"merge_split_f1": 0.91},
    )
    with connection(svc.paths.sqlite_path) as conn:
        decision = evaluate_policy(
            conn,
            "canonicalize_page",
            {
                "deterministic": True,
                "risk_score": 0.01,
                "target_page_paths": ["concepts/test.md"],
                "eval_gate": {"suite": "topology", "report_path": str(report_path)},
            },
        )

    assert decision.autonomy_level == "L0"
    assert decision.policy_decision == "matched"


def test_policy_promotion_matches_low_medium_and_large_topology(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        version = promote_policy_for_autonomy(
            conn,
            reason="test promotion",
            large_topology_fact_threshold=4,
        )
        low = evaluate_policy(
            conn,
            "synthesize_page",
            {"risk_tier": "low", "affected_fact_count": 2},
        )
        medium = evaluate_policy(
            conn,
            "page_merge",
            {"risk_tier": "medium", "affected_fact_count": 3},
        )
        large = evaluate_policy(
            conn,
            "page_merge",
            {"risk_tier": "high", "affected_fact_count": 5, "large_topology": True},
        )

    assert version == 2
    assert low.autonomy_level == "L1"
    assert low.critic_required is True
    assert medium.autonomy_level == "L2"
    assert medium.audit_sample_rate == 1.0
    assert large.autonomy_level == "L3"


def test_action_risk_classification_large_topology_overrides_medium() -> None:
    assert (
        classify_action_risk(
            "page_merge",
            {"affected_fact_count": 9},
            explicit_risk_tier="medium",
            large_topology_fact_threshold=8,
        )
        == "high"
    )
    assert classify_action_risk("synthesize_page", {"affected_fact_count": 3}) == "low"


def test_timeout_sweep_resolves_only_non_truth_residue_to_uncertainty(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, fact_ids, question, options, status, context,
              recommended_action, auto_resolve_after, risk_tier, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "question_truth_timeout",
                "conflict",
                json.dumps(["fact_a", "fact_b"]),
                "Which truth claim is current?",
                "[]",
                "needs_human",
                json.dumps({"action_type": "display_contested"}),
                json.dumps({"action_type": "display_contested"}),
                "2026-06-25T00:00:00+00:00",
                "high",
                "2026-06-24T00:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, fact_ids, question, options, status, context,
              recommended_action, auto_resolve_after, risk_tier, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "question_topology_timeout",
                "policy_escalation",
                "[]",
                "Should these pages merge?",
                "[]",
                "needs_human",
                json.dumps({"action_type": "page_merge"}),
                json.dumps({"action_type": "page_merge"}),
                "2026-06-25T00:00:00+00:00",
                "high",
                "2026-06-24T00:00:00+00:00",
            ),
        )

    result = run_cos_timeout_sweep(svc.paths, now="2026-06-26T00:00:00+00:00")

    assert result["resolved_count"] == 1
    assert result["skipped_truth_count"] == 1
    with connection(svc.paths.sqlite_path) as conn:
        truth = conn.execute(
            "SELECT status, answer, decided_by FROM open_questions WHERE id = ?",
            ("question_truth_timeout",),
        ).fetchone()
        topology = conn.execute(
            "SELECT status, answer, decided_by FROM open_questions WHERE id = ?",
            ("question_topology_timeout",),
        ).fetchone()

    assert truth["status"] == "needs_human"
    assert truth["answer"] is None
    assert truth["decided_by"] is None
    assert topology["status"] == "timeout_resolved"
    assert topology["decided_by"] == "timeout_uncertainty"
    assert json.loads(topology["answer"])["resolution"] == "uncertainty"


def test_l3_decision_records_policy_version_and_creates_residue(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    action = propose_action(
        svc.paths,
        "fact_upsert",
        action_payload={
            "fact": {
                "id": "fact_policy",
                "statement": "Policy gated fact.",
                "entity_key": "concept:test:summary",
                "page_hint": "concepts/test.md",
                "confidence": 0.6,
            }
        },
        action_features={"truth_mutation": False, "risk_score": 0.4},
        target_page_paths=["concepts/test.md"],
    )

    decided = decide_action(svc.paths, action["id"])

    assert decided["status"] == "needs_human"
    assert decided["policy_version"] == 1
    assert decided["autonomy_level"] == "L3"
    with connection(svc.paths.sqlite_path) as conn:
        residue = conn.execute(
            "SELECT * FROM open_questions WHERE action_id = ?", (action["id"],)
        ).fetchone()
    assert residue is not None
    assert residue["status"] == "needs_human"


def test_fact_upsert_revert_round_trip_and_drift_refusal(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    upsert = propose_action(
        svc.paths,
        "fact_upsert",
        action_payload={
            "fact": {
                "id": "fact_roundtrip",
                "statement": "Round-trip fact.",
                "entity_key": "concept:test:summary",
                "page_hint": "concepts/test.md",
                "section_hint": "Summary",
                "source_ids": ["document:doc_test"],
                "confidence": 0.9,
            }
        },
        action_features={"reversible": True},
    )
    applied = apply_action(svc.paths, upsert["id"])

    assert applied["status"] == "applied"
    with connection(svc.paths.sqlite_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 1

    reverted = revert_action(svc.paths, upsert["id"])

    assert reverted["status"] == "reverted"
    with connection(svc.paths.sqlite_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0

    apply_action(svc.paths, upsert["id"])
    rehome = propose_action(
        svc.paths,
        "rehome_fact",
        action_payload={"fact_id": "fact_roundtrip", "page_hint": "concepts/other.md"},
        action_features={"reversible": True},
    )
    applied_rehome = apply_action(svc.paths, rehome["id"])
    with connection(svc.paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE facts SET statement = ? WHERE id = ?",
            ("Drifted fact.", "fact_roundtrip"),
        )

    failed_revert = revert_action(svc.paths, applied_rehome["id"])

    assert failed_revert["status"] == "failed"
    with connection(svc.paths.sqlite_path) as conn:
        residue = conn.execute(
            "SELECT * FROM open_questions WHERE action_id = ?", (applied_rehome["id"],)
        ).fetchone()
    assert residue is not None
    assert residue["kind"] == "revert_drift"


def test_action_type_support_map_and_declared_failure(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()

    assert ACTION_TYPE_SPECS["fact_upsert"]["implemented"] is True
    assert ACTION_TYPE_SPECS["page_merge"]["implemented"] is True
    assert ACTION_TYPE_SPECS["page_split"]["implemented"] is True
    assert ACTION_TYPE_SPECS["rename_page"]["implemented"] is True

    with pytest.raises(ValueError, match="unknown cos action type"):
        propose_action(svc.paths, "invented_action_type")


def test_implemented_action_inverse_payload_shapes(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()

    fact = apply_action(
        svc.paths,
        propose_action(
            svc.paths,
            "fact_upsert",
            action_payload={
                "fact": {
                    "id": "fact_inverse",
                    "statement": "Inverse fact.",
                    "entity_key": "concept:test:summary",
                    "page_hint": "concepts/test.md",
                    "section_hint": "Summary",
                    "confidence": 0.8,
                }
            },
        )["id"],
    )
    contract = apply_action(
        svc.paths,
        propose_action(
            svc.paths,
            "edit_contract",
            action_payload={
                "contract": {
                    "id": "contract_inverse",
                    "page_hint": "concepts/test.md",
                    "page_scope": "Test page.",
                }
            },
        )["id"],
    )
    synthesis = apply_action(
        svc.paths,
        propose_action(
            svc.paths,
            "synthesize_page",
            action_payload={
                "synthesis": {
                    "id": "synthesis_inverse",
                    "page_hint": "concepts/test.md",
                    "synthesis_markdown": "- Derived from fact_inverse.",
                    "fact_ids": ["fact_inverse"],
                }
            },
        )["id"],
    )
    canonical = apply_action(
        svc.paths,
        propose_action(
            svc.paths,
            "canonicalize_page",
            target_page_paths=["concepts/test.md"],
        )["id"],
    )

    assert fact["inverse_action_json"] == {"delete_fact_ids": ["fact_inverse"]}
    assert contract["inverse_action_json"] == {"delete_contract_ids": ["contract_inverse"]}
    assert synthesis["inverse_action_json"] == {"delete_synthesis_ids": ["synthesis_inverse"]}
    assert canonical["inverse_action_json"] == {"noop": True}


def test_page_merge_apply_and_revert_round_trip(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    insert_test_fact(svc.paths, "fact_merge_left", "Left fact.", page_hint="concepts/alpha-payment.md")
    insert_test_fact(svc.paths, "fact_merge_right", "Right fact.", page_hint="concepts/alpha-payments.md")
    insert_test_contract(svc.paths, "contract_left", "concepts/alpha-payment.md")
    action = propose_action(
        svc.paths,
        "page_merge",
        action_payload={
            "candidate": {
                "page_hints": ["concepts/alpha-payment.md", "concepts/alpha-payments.md"],
                "destination_page_hint": "concepts/alpha-payments.md",
            }
        },
        target_page_paths=["concepts/alpha-payment.md", "concepts/alpha-payments.md"],
    )

    applied = apply_action(svc.paths, action["id"])

    assert applied["status"] == "applied"
    assert applied["inverse_action_json"]["restore_facts"]
    with connection(svc.paths.sqlite_path) as conn:
        left = conn.execute("SELECT page_hint FROM facts WHERE id = 'fact_merge_left'").fetchone()
        contract = conn.execute("SELECT status FROM page_contracts WHERE id = 'contract_left'").fetchone()
    assert left["page_hint"] == "concepts/alpha-payments.md"
    assert contract["status"] == "superseded"

    reverted = revert_action(svc.paths, action["id"])

    assert reverted["status"] == "reverted"
    with connection(svc.paths.sqlite_path) as conn:
        left = conn.execute("SELECT page_hint FROM facts WHERE id = 'fact_merge_left'").fetchone()
        contract = conn.execute("SELECT status FROM page_contracts WHERE id = 'contract_left'").fetchone()
    assert left["page_hint"] == "concepts/alpha-payment.md"
    assert contract["status"] == "active"


def test_page_split_apply_and_revert_round_trip(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    insert_test_fact(svc.paths, "fact_split_summary", "Summary fact.", page_hint="projects/sprawl.md", section_hint="Summary")
    insert_test_fact(svc.paths, "fact_split_pricing", "Pricing fact.", page_hint="projects/sprawl.md", section_hint="Pricing")
    insert_test_fact(svc.paths, "fact_split_risk", "Risk fact.", page_hint="projects/sprawl.md", section_hint="Risks")
    action = propose_action(
        svc.paths,
        "page_split",
        action_payload={"candidate": {"page_hints": ["projects/sprawl.md"]}},
        target_page_paths=["projects/sprawl.md"],
    )

    applied = apply_action(svc.paths, action["id"])

    assert applied["status"] == "applied"
    with connection(svc.paths.sqlite_path) as conn:
        pages = {
            row["id"]: row["page_hint"]
            for row in conn.execute("SELECT id, page_hint FROM facts WHERE id LIKE 'fact_split_%'")
        }
    assert pages["fact_split_summary"] == "projects/sprawl.md"
    assert pages["fact_split_pricing"] == "projects/sprawl-pricing.md"
    assert pages["fact_split_risk"] == "projects/sprawl-risks.md"

    reverted = revert_action(svc.paths, action["id"])

    assert reverted["status"] == "reverted"
    with connection(svc.paths.sqlite_path) as conn:
        pages = {
            row["id"]: row["page_hint"]
            for row in conn.execute("SELECT id, page_hint FROM facts WHERE id LIKE 'fact_split_%'")
        }
    assert set(pages.values()) == {"projects/sprawl.md"}


def test_rename_page_apply_and_revert_round_trip(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    insert_test_fact(svc.paths, "fact_rename", "Rename fact.", page_hint="concepts/old-name.md")
    insert_test_contract(svc.paths, "contract_rename", "concepts/old-name.md")
    with connection(svc.paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO wiki_page_syntheses(
              id, page_hint, synthesis_markdown, fact_ids, generated_at, stale
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "synthesis_rename",
                "concepts/old-name.md",
                "- Rename synthesis [fact_rename].",
                json.dumps(["fact_rename"]),
                "2026-06-26T00:00:00+00:00",
                0,
            ),
        )
    action = propose_action(
        svc.paths,
        "rename_page",
        action_payload={"from_page_hint": "concepts/old-name.md", "to_page_hint": "concepts/new-name.md"},
        target_page_paths=["concepts/old-name.md", "concepts/new-name.md"],
    )

    applied = apply_action(svc.paths, action["id"])

    assert applied["status"] == "applied"
    with connection(svc.paths.sqlite_path) as conn:
        fact = conn.execute("SELECT page_hint FROM facts WHERE id = 'fact_rename'").fetchone()
        contract = conn.execute("SELECT page_hint FROM page_contracts WHERE id = 'contract_rename'").fetchone()
        synthesis = conn.execute("SELECT page_hint, stale FROM wiki_page_syntheses WHERE id = 'synthesis_rename'").fetchone()
    assert fact["page_hint"] == "concepts/new-name.md"
    assert contract["page_hint"] == "concepts/new-name.md"
    assert synthesis["page_hint"] == "concepts/new-name.md"
    assert synthesis["stale"] == 1

    reverted = revert_action(svc.paths, action["id"])

    assert reverted["status"] == "reverted"
    with connection(svc.paths.sqlite_path) as conn:
        fact = conn.execute("SELECT page_hint FROM facts WHERE id = 'fact_rename'").fetchone()
        contract = conn.execute("SELECT page_hint FROM page_contracts WHERE id = 'contract_rename'").fetchone()
        synthesis = conn.execute("SELECT page_hint, stale FROM wiki_page_syntheses WHERE id = 'synthesis_rename'").fetchone()
    assert fact["page_hint"] == "concepts/old-name.md"
    assert contract["page_hint"] == "concepts/old-name.md"
    assert synthesis["page_hint"] == "concepts/old-name.md"
    assert synthesis["stale"] == 0


def test_extraction_shadow_is_safe_without_llm_config(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nShadow extraction marker.", encoding="utf-8")
    svc.ingest()

    result = extract_recent_documents(svc.paths, shadow=True)

    assert result["status"] == "skipped"
    with connection(svc.paths.sqlite_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cos_actions").fetchone()[0] == 0


def test_extraction_watermark_skips_unchanged_document(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nWatermarked extraction marker.", encoding="utf-8")
    svc.ingest()
    provider = FakeExtractorProvider()

    first = extract_recent_documents(svc.paths, shadow=True, llm_provider=provider)
    second = extract_recent_documents(svc.paths, shadow=True, llm_provider=provider)

    assert first["status"] == "ok"
    assert len(first["documents"]) == 1
    assert first["candidates"][0]["extractor_model"] == "fake-extractor-model"
    assert second["status"] == "ok"
    assert second["documents"] == []
    assert provider.calls == 1
    with connection(svc.paths.sqlite_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cos_stage_watermarks WHERE stage = 'extractor'").fetchone()[0] == 1


def test_extraction_retries_semantically_invalid_response(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nRetry extraction marker.", encoding="utf-8")
    svc.ingest()
    provider = RetryExtractorProvider()

    result = extract_recent_documents(svc.paths, shadow=True, llm_provider=provider)

    assert result["status"] == "ok"
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["statement"] == "Retry extraction marker is present."
    window_validation = result["document_validations"][0]["windows"][0]
    assert window_validation["selected_attempt"] == 2
    assert [attempt["accepted_count"] for attempt in window_validation["attempts"]] == [0, 1]
    assert window_validation["raw_fact_count"] == 2
    assert window_validation["accepted_count"] == 1
    assert window_validation["accepted_from_retry_count"] == 1
    assert window_validation["final_rejected_count"] == 0
    assert window_validation["total_rejected_count"] == 1
    assert provider.calls == 2


def test_extraction_does_not_mark_all_invalid_batch_ok(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nInvalid extraction marker.", encoding="utf-8")
    svc.ingest()
    provider = AlwaysInvalidExtractorProvider()

    result = extract_recent_documents(svc.paths, shadow=True, llm_provider=provider)

    assert result["status"] == "ok"
    assert result["candidates"] == []
    assert result["document_validations"][0]["windows"][0]["selected_attempt"] == 1
    assert provider.calls == 2
    with connection(svc.paths.sqlite_path) as conn:
        watermark = conn.execute(
            "SELECT status, metadata FROM cos_stage_watermarks WHERE stage = 'extractor'"
        ).fetchone()
    assert watermark["status"] == "invalid"
    assert json.loads(watermark["metadata"])["validation"]["rejected_count"] == 1


def test_extraction_skips_agent_session_logs_by_default(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    log = svc.paths.inbox / "agent-log.md"
    log.write_text(
        '---\nsource_type: "agent_session_log"\n---\n\n# Log\n\nAgent log marker.',
        encoding="utf-8",
    )
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nMarkdown extraction marker.", encoding="utf-8")
    svc.ingest()
    provider = MarkerExtractorProvider(
        marker="Markdown extraction marker.",
        statement="Markdown extraction marker is present.",
    )

    result = extract_recent_documents(svc.paths, shadow=True, llm_provider=provider, limit=5)

    assert result["status"] == "ok"
    assert [document["source_type"] for document in result["documents"]] == ["markdown_note"]
    assert len(result["candidates"]) == 1
    assert provider.calls == 1


def test_extraction_windows_all_chunks_without_truncation(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    marker = "Window coverage marker is beyond the old clip."
    chunk_texts = [f"Chunk {index} has ordinary text." for index in range(11)]
    chunk_texts.append(("x" * 2105) + marker)
    insert_document_with_chunks(
        svc.paths,
        "doc_windowed",
        "markdown_note",
        chunk_texts,
    )
    provider = MarkerExtractorProvider(
        marker=marker,
        statement="Window coverage marker appears in a late chunk.",
    )

    result = extract_recent_documents(svc.paths, shadow=True, llm_provider=provider, limit=1)

    assert result["status"] == "ok"
    assert len(result["documents"][0]["chunks"]) == 12
    assert len(result["documents"][0]["windows"]) == 3
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["source_ids"] == ["chunk:chunk_doc_windowed_11"]
    assert any(marker in prompt for prompt in provider.prompts)


def test_extraction_prompt_includes_routing_hints_without_fact_rows(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nRouting hint marker.", encoding="utf-8")
    svc.ingest()
    insert_test_contract(svc.paths, "contract_routing", "concepts/routing-target.md")
    insert_test_fact(
        svc.paths,
        "fact_should_not_leak",
        "This existing fact row should not appear in the extractor prompt.",
        page_hint="concepts/routing-target.md",
    )
    provider = MarkerExtractorProvider(
        marker="Routing hint marker.",
        statement="Routing hint marker is present.",
    )

    result = extract_recent_documents(svc.paths, shadow=True, llm_provider=provider)

    assert result["status"] == "ok"
    assert "concepts/routing-target.md" in provider.prompts[0]
    assert "This existing fact row should not appear" not in provider.prompts[0]


def test_extraction_derives_spans_from_evidence_quote(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    chunk_text = "Prefix text.\nDocument   offset marker is present.\nSuffix text."
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
                "doc_offset",
                "note",
                "Offset Source",
                "/tmp/offset-source.md",
                "/tmp/offset-source.md",
                "doc-hash",
                "<local>",
                "/tmp/offset-source.md",
                "2026-06-26T00:00:00+00:00",
                "2026-06-26T00:00:00+00:00",
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
                "chunk_offset",
                "doc_offset",
                0,
                "raw",
                chunk_text,
                "",
                100,
                100 + len(chunk_text),
                5,
                "chunk-hash",
                "2026-06-26T00:00:00+00:00",
            ),
        )

    candidates = validate_extracted_facts(
        svc.paths,
        [
            {
                "statement": "Document offset marker is present.",
                "chunk_id": "chunk_offset",
                "evidence_quote": "Document offset marker is present.",
                "claim_class": "factual_update",
            }
        ],
        extractor_model="fake-extractor-model",
    )

    assert len(candidates) == 1
    expected_start = chunk_text.index("Document")
    assert candidates[0]["source_spans"] == [
        {"chunk_id": "chunk_offset", "start": expected_start, "end": expected_start + len("Document   offset marker is present.")}
    ]
    assert candidates[0]["extractor_model"] == "fake-extractor-model"
    missing_quote_candidates = validate_extracted_facts(
        svc.paths,
        [
            {
                "statement": "Document offset marker is present.",
                "chunk_id": "chunk_offset",
                "evidence_quote": "fabricated quote",
                "claim_class": "factual_update",
            }
        ],
        extractor_model="fake-extractor-model",
    )

    assert missing_quote_candidates == []


def test_extraction_drops_non_claim_classes_and_marks_extracted_empty(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nThe meeting started at 10:00.", encoding="utf-8")
    svc.ingest()
    provider = NonClaimExtractorProvider()

    first = extract_recent_documents(svc.paths, shadow=True, llm_provider=provider)
    second = extract_recent_documents(svc.paths, shadow=True, llm_provider=provider)

    assert first["status"] == "ok"
    assert first["candidates"] == []
    assert first["validation"]["dropped_count"] == 1
    assert first["validation"]["rejected_count"] == 0
    assert second["documents"] == []
    assert provider.calls == 1
    with connection(svc.paths.sqlite_path) as conn:
        watermark = conn.execute(
            "SELECT status, metadata FROM cos_stage_watermarks WHERE stage = 'extractor'"
        ).fetchone()
    assert watermark["status"] == "extracted_empty"
    metadata = json.loads(watermark["metadata"])
    assert metadata["candidate_count"] == 0
    assert metadata["validation"]["dropped_count"] == 1


def test_extraction_empty_normalized_content_skips_cosmetic_reexports(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    insert_document_with_chunks(
        svc.paths,
        "doc_empty_template",
        "hyprnote_meeting",
        [
            'title: "Netflix interview block"\n'
            'event_started_at: "2026-06-25T16:00:00+00:00"\n'
            "No summary was captured.\n"
            "No memo was captured.\n"
            "No transcript was captured.\n"
        ],
    )
    provider = FakeExtractorProvider()

    first = extract_recent_documents(svc.paths, shadow=True, llm_provider=provider)
    with connection(svc.paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE documents SET content_hash = ? WHERE id = ?",
            ("raw-hash-after-cosmetic-reexport", "doc_empty_template"),
        )
        conn.execute(
            "UPDATE chunks SET text = ?, content_hash = ? WHERE document_id = ?",
            (
                'title: "Netflix interview block re-exported"\n'
                'event_started_at: "2026-06-25T16:00:01+00:00"\n'
                "No summary was captured.\n"
                "No memo was captured.\n"
                "No transcript was captured.\n",
                "chunk-hash-after-cosmetic-reexport",
                "doc_empty_template",
            ),
        )
    second = extract_recent_documents(svc.paths, shadow=True, llm_provider=provider)

    assert first["status"] == "ok"
    assert len(first["documents"]) == 1
    assert first["documents"][0]["windows"] == []
    assert first["candidates"] == []
    assert first["validation"]["accepted_count"] == 0
    assert first["validation"]["dropped_count"] == 0
    assert second["status"] == "ok"
    assert second["documents"] == []
    assert provider.calls == 0
    with connection(svc.paths.sqlite_path) as conn:
        watermark = conn.execute(
            "SELECT status, content_hash, metadata FROM cos_stage_watermarks WHERE stage = 'extractor'"
        ).fetchone()
    assert watermark["status"] == "extracted_empty"
    assert str(watermark["content_hash"]).startswith("normalized:")
    assert json.loads(watermark["metadata"])["normalized_content_empty"] is True


def test_extraction_without_simple_autonomy_stays_l3(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nDefault gated marker.", encoding="utf-8")
    svc.ingest()
    provider = MarkerExtractorProvider(
        marker="Default gated marker.",
        statement="Default gated marker is present.",
        page_hint="concepts/default-gated.md",
    )

    result = extract_recent_documents(svc.paths, shadow=False, llm_provider=provider)

    assert result["status"] == "ok"
    assert result["actions"][0]["status"] == "needs_human"
    assert result["actions"][0]["autonomy_level"] == "L3"
    with connection(svc.paths.sqlite_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
        residue = conn.execute(
            "SELECT kind FROM open_questions WHERE action_id = ?",
            (result["actions"][0]["id"],),
        ).fetchone()
    assert residue["kind"] == "policy_escalation"


def test_extraction_default_path_sends_fallback_route_to_unrouted_residue(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nDefault fallback marker.", encoding="utf-8")
    svc.ingest()
    provider = MarkerExtractorProvider(
        marker="Default fallback marker.",
        statement="Default fallback marker is present.",
    )

    result = extract_recent_documents(svc.paths, shadow=False, llm_provider=provider)

    assert result["status"] == "ok"
    assert result["actions"][0]["status"] == "needs_human"
    assert result["actions"][0]["policy_decision"] == "earned_residue"
    with connection(svc.paths.sqlite_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
        residue = conn.execute(
            "SELECT kind, question FROM open_questions WHERE action_id = ?",
            (result["actions"][0]["id"],),
        ).fetchone()
    assert residue["kind"] == "unrouted_fact"
    assert "fallback page" in residue["question"]


def test_extraction_promoted_clean_fact_requires_labeled_eval_gate(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        promote_policy_for_autonomy(conn, reason="test clean fact promotion")
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nNo labels marker.", encoding="utf-8")
    svc.ingest()
    provider = MarkerExtractorProvider(
        marker="No labels marker.",
        statement="No labels marker is present.",
        page_hint="concepts/no-labels.md",
    )

    result = extract_recent_documents(svc.paths, shadow=False, llm_provider=provider)

    assert result["status"] == "ok"
    assert result["actions"][0]["status"] == "needs_human"
    assert result["actions"][0]["policy_decision"] == "eval_gate_failed"
    assert result["actions"][0]["autonomy_level"] == "L3"
    with connection(svc.paths.sqlite_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


def test_extraction_promoted_clean_fact_applies_with_labeled_eval_report(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    write_eval_report(
        svc.paths.home,
        "extraction",
        passed=True,
        metrics={"label_policy": "labeled", "label_case_count": 1},
    )
    with connection(svc.paths.sqlite_path) as conn:
        promote_policy_for_autonomy(conn, reason="test clean fact promotion")
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nEarned autonomy marker.", encoding="utf-8")
    svc.ingest()
    provider = MarkerExtractorProvider(
        marker="Earned autonomy marker.",
        statement="Earned autonomy marker is present.",
        page_hint="concepts/earned-autonomy.md",
    )

    result = extract_recent_documents(svc.paths, shadow=False, llm_provider=provider)

    assert result["status"] == "ok"
    assert result["actions"][0]["status"] == "applied"
    assert result["actions"][0]["policy_decision"] == "matched"
    assert result["actions"][0]["autonomy_level"] == "L2"
    with connection(svc.paths.sqlite_path) as conn:
        fact = conn.execute("SELECT statement, page_hint FROM facts").fetchone()
        residue_count = conn.execute("SELECT COUNT(*) FROM open_questions WHERE status = 'needs_human'").fetchone()[0]
    assert fact["statement"] == "Earned autonomy marker is present."
    assert fact["page_hint"] == "concepts/earned-autonomy.md"
    assert residue_count == 0


def test_extraction_simple_autonomy_auto_applies_clean_routed_fact(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    enable_simple_autonomy(svc.paths)
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nSimple autonomy marker.", encoding="utf-8")
    svc.ingest()
    provider = MarkerExtractorProvider(
        marker="Simple autonomy marker.",
        statement="Simple autonomy marker is present.",
        page_hint="concepts/simple-autonomy.md",
    )

    result = extract_recent_documents(svc.paths, shadow=False, llm_provider=provider)

    assert result["status"] == "ok"
    assert result["actions"][0]["status"] == "auto_applied"
    assert result["actions"][0]["policy_decision"] == "simple_autonomy"
    assert result["actions"][0]["autonomy_level"] == "L2"
    with connection(svc.paths.sqlite_path) as conn:
        fact = conn.execute("SELECT statement, page_hint FROM facts").fetchone()
        residue_count = conn.execute("SELECT COUNT(*) FROM open_questions").fetchone()[0]
    assert fact["statement"] == "Simple autonomy marker is present."
    assert fact["page_hint"] == "concepts/simple-autonomy.md"
    assert residue_count == 0


def test_extraction_simple_autonomy_sends_fallback_route_to_residue(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    enable_simple_autonomy(svc.paths)
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nFallback route marker.", encoding="utf-8")
    svc.ingest()
    provider = MarkerExtractorProvider(
        marker="Fallback route marker.",
        statement="Fallback route marker is present.",
    )

    result = extract_recent_documents(svc.paths, shadow=False, llm_provider=provider)

    assert result["status"] == "ok"
    assert result["actions"][0]["status"] == "needs_human"
    assert result["actions"][0]["policy_decision"] == "simple_residue"
    with connection(svc.paths.sqlite_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
        residue = conn.execute(
            "SELECT kind, question FROM open_questions WHERE action_id = ?",
            (result["actions"][0]["id"],),
        ).fetchone()
    assert residue["kind"] == "unrouted_fact"
    assert "fallback page" in residue["question"]


def test_sampled_audit_without_auditor_does_not_mark_ok(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    action = apply_action(
        svc.paths,
        propose_action(
            svc.paths,
            "canonicalize_page",
            action_payload={"page_hint": "concepts/test.md"},
            target_page_paths=["concepts/test.md"],
        )["id"],
    )

    result = run_sampled_audit(svc.paths)

    assert result["mode"] == "stub"
    assert result["sampled"] == 1
    assert result["audited"] == []
    assert result["missing_action_ids"] == [action["id"]]
    assert get_action(svc.paths, action["id"])["audit_status"] == "unaudited"


def test_sampled_audit_records_auditor_ok(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    action = apply_action(
        svc.paths,
        propose_action(
            svc.paths,
            "canonicalize_page",
            action_payload={"page_hint": "concepts/test.md"},
            target_page_paths=["concepts/test.md"],
        )["id"],
    )

    result = run_sampled_audit(
        svc.paths,
        llm_provider=FakeAuditorProvider({action["id"]: "ok"}),
    )

    assert result["mode"] == "configured"
    assert result["audited"][0]["audit_status"] == "sampled_ok"
    audit = result["audited"][0]["evidence_json"]["audits"][-1]
    assert audit["metadata"]["source"] == "auditor_llm"
    assert audit["metadata"]["decision"] == "ok"
    assert "fake-auditor" == audit["metadata"]["provider"]


def test_sampled_audit_respects_zero_policy_sample_rate(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO cos_policy(
              id, version, priority, match_action_types, match_predicate,
              autonomy_level, critic_required, timeout_allowed,
              timeout_after_seconds, audit_sample_rate, demotion_threshold,
              auto_revert_signals, active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "policy_test_zero_audit",
                99,
                1,
                '["canonicalize_page"]',
                json.dumps({"eq": {"skip_audit_sample": True}}),
                "L2",
                0,
                0,
                None,
                0.0,
                0.1,
                "[]",
                1,
                "2026-06-25T00:00:00+00:00",
            ),
        )
    action = propose_action(
        svc.paths,
        "canonicalize_page",
        action_payload={"page_hint": "concepts/test.md"},
        action_features={"skip_audit_sample": True},
        target_page_paths=["concepts/test.md"],
    )
    decided = decide_action(svc.paths, action["id"])

    result = run_sampled_audit(svc.paths, llm_provider=FailingAuditorProvider())

    assert decided["status"] == "applied"
    assert decided["policy_id"] == "policy_test_zero_audit"
    assert result["mode"] == "configured"
    assert result["sampled"] == 0
    assert result["audited"] == []


def test_sampled_audit_bad_demotes_policy_and_reverts(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    action = apply_action(
        svc.paths,
        propose_action(
            svc.paths,
            "fact_upsert",
            action_payload={
                "fact": {
                    "id": "fact_audit_bad",
                    "statement": "Audited fact.",
                    "entity_key": "concept:test:summary",
                    "page_hint": "concepts/test.md",
                    "confidence": 0.9,
                }
            },
        )["id"],
    )

    result = run_sampled_audit(
        svc.paths,
        auto_revert_bad=True,
        llm_provider=FakeAuditorProvider({action["id"]: "bad"}),
    )

    assert result["bad_action_ids"] == [action["id"]]
    assert result["demoted_policy_version"] == 2
    assert result["reverted"][0]["status"] == "reverted"
    assert get_action(svc.paths, action["id"])["status"] == "reverted"
    with connection(svc.paths.sqlite_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


def test_sampled_audit_does_not_demote_below_policy_bad_rate_threshold(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    insert_audit_policy(
        svc.paths,
        "policy_audit_threshold_high",
        {"audit_threshold_case": "below"},
        demotion_threshold=0.75,
    )
    first = decide_action(
        svc.paths,
        propose_action(
            svc.paths,
            "canonicalize_page",
            action_payload={"page_hint": "concepts/one.md"},
            action_features={"audit_threshold_case": "below"},
            target_page_paths=["concepts/one.md"],
        )["id"],
    )
    second = decide_action(
        svc.paths,
        propose_action(
            svc.paths,
            "canonicalize_page",
            action_payload={"page_hint": "concepts/two.md"},
            action_features={"audit_threshold_case": "below"},
            target_page_paths=["concepts/two.md"],
        )["id"],
    )

    result = run_sampled_audit(
        svc.paths,
        llm_provider=FakeAuditorProvider({first["id"]: "bad", second["id"]: "ok"}),
    )

    assert result["bad_action_ids"] == [first["id"]]
    assert result["demotion_evidence"] == []
    assert result["demoted_policy_version"] is None


def test_sampled_audit_demotes_when_policy_bad_rate_exceeds_threshold(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    insert_audit_policy(
        svc.paths,
        "policy_audit_threshold_low",
        {"audit_threshold_case": "above"},
        demotion_threshold=0.4,
    )
    first = decide_action(
        svc.paths,
        propose_action(
            svc.paths,
            "canonicalize_page",
            action_payload={"page_hint": "concepts/one.md"},
            action_features={"audit_threshold_case": "above"},
            target_page_paths=["concepts/one.md"],
        )["id"],
    )
    second = decide_action(
        svc.paths,
        propose_action(
            svc.paths,
            "canonicalize_page",
            action_payload={"page_hint": "concepts/two.md"},
            action_features={"audit_threshold_case": "above"},
            target_page_paths=["concepts/two.md"],
        )["id"],
    )

    result = run_sampled_audit(
        svc.paths,
        llm_provider=FakeAuditorProvider({first["id"]: "bad", second["id"]: "ok"}),
    )

    assert result["demoted_policy_version"] == 100
    assert result["demotion_evidence"] == [
        {
            "policy_id": "policy_audit_threshold_low",
            "policy_version": 99,
            "audited_count": 2,
            "bad_count": 1,
            "bad_rate": 0.5,
            "demotion_threshold": 0.4,
        }
    ]


def test_sampled_audit_auto_revert_refuses_on_drift_and_creates_residue(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    action = apply_action(
        svc.paths,
        propose_action(
            svc.paths,
            "fact_upsert",
            action_payload={
                "fact": {
                    "id": "fact_audit_drift",
                    "statement": "Original audited fact.",
                    "entity_key": "concept:test:summary",
                    "page_hint": "concepts/test.md",
                    "confidence": 0.9,
                }
            },
        )["id"],
    )
    with connection(svc.paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE facts SET statement = ? WHERE id = ?",
            ("Drifted audited fact.", "fact_audit_drift"),
        )

    result = run_sampled_audit(
        svc.paths,
        auto_revert_bad=True,
        llm_provider=FakeAuditorProvider({action["id"]: "bad"}),
    )

    assert result["reverted"][0]["status"] == "failed"
    assert get_action(svc.paths, action["id"])["status"] == "failed"
    with connection(svc.paths.sqlite_path) as conn:
        residue = conn.execute(
            "SELECT * FROM open_questions WHERE action_id = ?", (action["id"],)
        ).fetchone()
    assert residue is not None
    assert residue["kind"] == "revert_drift"


def test_critic_disagreement_blocks_l1_auto_apply(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO cos_policy(
              id, version, priority, match_action_types, match_predicate,
              autonomy_level, critic_required, timeout_allowed,
              timeout_after_seconds, audit_sample_rate, demotion_threshold,
              auto_revert_signals, active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "policy_test_l1_critic",
                99,
                1,
                '["canonicalize_page"]',
                json.dumps({"eq": {"needs_critic": True}}),
                "L1",
                1,
                0,
                None,
                1.0,
                0.1,
                "[]",
                1,
                "2026-06-25T00:00:00+00:00",
            ),
        )
    action = propose_action(
        svc.paths,
        "canonicalize_page",
        action_payload={"page_hint": "concepts/test.md"},
        action_features={"needs_critic": True},
        target_page_paths=["concepts/test.md"],
    )

    decided = decide_action(
        svc.paths,
        action["id"],
        critic_by="critic-test",
        critic_decision="disagree",
    )

    assert decided["status"] == "needs_human"
    assert decided["autonomy_level"] == "L1"
    assert decided["critic_decision"] == "disagree"
    with connection(svc.paths.sqlite_path) as conn:
        residue = conn.execute(
            "SELECT * FROM open_questions WHERE action_id = ?", (action["id"],)
        ).fetchone()
    assert residue is not None
    assert residue["kind"] == "policy_escalation"


def test_critic_llm_agreement_allows_l1_auto_apply(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    insert_critic_policy(svc.paths, "policy_test_l1_critic_llm", "L1", "needs_critic")
    action = propose_action(
        svc.paths,
        "canonicalize_page",
        action_payload={"page_hint": "concepts/test.md"},
        action_features={"needs_critic": True},
        target_page_paths=["concepts/test.md"],
    )

    decided = decide_action(
        svc.paths,
        action["id"],
        critic_llm_provider=FakeCriticProvider("agree"),
    )

    assert decided["status"] == "auto_applied"
    assert decided["autonomy_level"] == "L1"
    assert decided["critic_by"] == "fake:fake-critic-model"
    assert decided["critic_decision"] == "agree"


def test_critic_llm_disagreement_blocks_l2_apply(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    insert_critic_policy(svc.paths, "policy_test_l2_critic_llm", "L2", "needs_l2_critic")
    action = propose_action(
        svc.paths,
        "canonicalize_page",
        action_payload={"page_hint": "concepts/test.md"},
        action_features={"needs_l2_critic": True},
        target_page_paths=["concepts/test.md"],
    )

    decided = decide_action(
        svc.paths,
        action["id"],
        critic_llm_provider=FakeCriticProvider("disagree"),
    )

    assert decided["status"] == "needs_human"
    assert decided["autonomy_level"] == "L2"
    assert decided["critic_decision"] == "disagree"


def test_missing_critic_provider_blocks_required_critic_auto_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PKM_BRAIN_LLM_PROVIDER", raising=False)
    for suffix in ("PROVIDER", "MODEL", "MODEL_FALLBACKS", "BASE_URL"):
        monkeypatch.delenv(role_env("critic", suffix), raising=False)
    svc = service_for(tmp_path)
    svc.init_workspace()
    insert_critic_policy(svc.paths, "policy_test_l1_critic_missing", "L1", "needs_critic")
    action = propose_action(
        svc.paths,
        "canonicalize_page",
        action_payload={"page_hint": "concepts/test.md"},
        action_features={"needs_critic": True},
        target_page_paths=["concepts/test.md"],
    )

    decided = decide_action(svc.paths, action["id"])

    assert decided["status"] == "needs_human"
    assert decided["critic_by"] == "critic:unconfigured"
    assert decided["critic_decision"] == "unavailable"


class FakeCriticProvider:
    name = "fake"
    model = "fake-critic-model"

    def __init__(self, decision: str) -> None:
        self.decision = decision

    def complete(self, prompt: str) -> str:
        assert "Review this Chief-of-Staff action" in prompt
        return json.dumps({"decision": self.decision, "rationale": "test critic judgment"})


class FakeExtractorProvider:
    name = "fake-extractor"
    model = "fake-extractor-model"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        assert "Extract atomic source-backed facts" in prompt
        chunk_ids = re.findall(r"['\"]chunk_id['\"]:\s*['\"]([^'\"]+)['\"]", prompt)
        chunk_id = next((item for item in chunk_ids if item != "missing"), "missing")
        return json.dumps(
            {
                "facts": [
                        {
                            "statement": "Watermarked extraction marker is present.",
                            "chunk_id": chunk_id,
                            "evidence_quote": "Watermarked extraction marker.",
                            "claim_class": "factual_update",
                        }
                    ]
                }
        )


class RetryExtractorProvider:
    name = "fake-extractor"
    model = "fake-extractor-model"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        if self.calls == 1:
            return json.dumps(
                {
                    "facts": [
                        {
                            "statement": "Retry extraction marker is present.",
                            "chunk_id": "missing",
                            "evidence_quote": "Retry extraction marker.",
                            "claim_class": "factual_update",
                        }
                    ]
                }
            )
        assert "previous extractor response did not pass deterministic validation" in prompt
        source_cards = prompt.rsplit("Source window JSON:", 1)[-1]
        chunk_ids = re.findall(r"['\"]chunk_id['\"]:\s*['\"]([^'\"]+)['\"]", source_cards)
        chunk_id = next((item for item in chunk_ids if item != "missing"), "missing")
        return json.dumps(
            {
                "facts": [
                    {
                        "statement": "Retry extraction marker is present.",
                        "chunk_id": chunk_id,
                        "evidence_quote": "Retry extraction marker.",
                        "claim_class": "factual_update",
                    }
                ]
            }
        )


class AlwaysInvalidExtractorProvider:
    name = "fake-extractor"
    model = "fake-extractor-model"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        return json.dumps(
            {
                "facts": [
                        {
                            "statement": "Invalid extraction marker is present.",
                            "chunk_id": "missing",
                            "evidence_quote": "Invalid extraction marker.",
                            "claim_class": "factual_update",
                        }
                    ]
                }
        )


class NonClaimExtractorProvider:
    name = "fake-extractor"
    model = "fake-extractor-model"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        return json.dumps(
            {
                "facts": [
                    {
                        "statement": "The meeting started at 10:00.",
                        "chunk_id": re.findall(r"['\"]chunk_id['\"]:\s*['\"]([^'\"]+)['\"]", prompt)[0],
                        "evidence_quote": "The meeting started at 10:00.",
                        "claim_class": "event_metadata",
                    }
                ]
            }
        )


class MarkerExtractorProvider:
    name = "fake-extractor"
    model = "fake-extractor-model"

    def __init__(
        self,
        *,
        marker: str,
        statement: str,
        page_hint: str = "concepts/extracted-facts.md",
    ) -> None:
        self.marker = marker
        self.statement = statement
        self.page_hint = page_hint
        self.calls = 0
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        source_window = json.loads(prompt.rsplit("Source window JSON:", 1)[-1])
        for chunk in source_window["window"]["chunks"]:
            if self.marker in chunk["text"]:
                return json.dumps(
                    {
                        "facts": [
                            {
                                "statement": self.statement,
                                "chunk_id": chunk["chunk_id"],
                                "evidence_quote": self.marker,
                                "claim_class": "factual_update",
                                "page_hint": self.page_hint,
                                "section_hint": "Summary",
                                "entity_key": self.page_hint.removesuffix(".md").replace("/", ":"),
                                "extraction_confidence": 0.99,
                                "routing_confidence": 0.8,
                                "truth_confidence": 0.95,
                            }
                        ]
                    }
                )
        return json.dumps({"facts": []})


def enable_simple_autonomy(paths: BrainPaths) -> None:
    paths.config_local.mkdir(parents=True, exist_ok=True)
    (paths.config_local / "cos_llm.yaml").write_text(
        "extraction:\n"
        "  simple_autonomy:\n"
        "    enabled: true\n",
        encoding="utf-8",
    )


def apply_status(paths: BrainPaths, action_id: str) -> str:
    with connection(paths.sqlite_path) as conn:
        return str(
            conn.execute("SELECT status FROM cos_actions WHERE id = ?", (action_id,)).fetchone()[
                "status"
            ]
        )


def write_eval_report(
    home: Path, suite: str, *, passed: bool, metrics: dict[str, object]
) -> Path:
    path = home / "reports" / "evals" / f"eval-{suite}-test.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "id": "eval_test",
                "suite": suite,
                "reports": [
                    {
                        "suite": suite,
                        "fixture_count": 1,
                        "metrics": metrics,
                        "threshold": {},
                        "passed": passed,
                    }
                ],
                "passed": passed,
            }
        ),
        encoding="utf-8",
    )
    return path


def insert_document_with_chunks(
    paths: BrainPaths,
    document_id: str,
    source_type: str,
    chunk_texts: list[str],
) -> None:
    with connection(paths.sqlite_path) as conn:
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
                source_type,
                "Windowed Source",
                f"/tmp/{document_id}.md",
                f"/tmp/{document_id}.md",
                f"{document_id}-hash",
                "<local>",
                f"/tmp/{document_id}.md",
                "2026-06-26T00:00:00+00:00",
                "2026-06-26T00:00:00+00:00",
                None,
                "[]",
                1,
                "active",
            ),
        )
        for index, text in enumerate(chunk_texts):
            conn.execute(
                """
                INSERT INTO chunks(
                  id, document_id, chunk_index, corpus_type, text, heading_path,
                  start_offset, end_offset, token_count, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"chunk_{document_id}_{index}",
                    document_id,
                    index,
                    "raw",
                    text,
                    "",
                    index * 1000,
                    index * 1000 + len(text),
                    max(1, len(text.split())),
                    f"{document_id}-chunk-{index}-hash",
                    "2026-06-26T00:00:00+00:00",
                ),
            )


def insert_test_fact(
    paths: BrainPaths,
    fact_id: str,
    statement: str,
    *,
    page_hint: str,
    section_hint: str = "Summary",
) -> None:
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, page_hint, section_hint, source_ids,
              observed_at, confidence, status, metadata, created_at, truth_confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fact_id,
                statement,
                "concept:test:summary",
                page_hint,
                section_hint,
                json.dumps([f"document:{fact_id}"]),
                "2026-06-26T00:00:00+00:00",
                0.9,
                "active",
                "{}",
                "2026-06-26T00:00:00+00:00",
                0.9,
            ),
        )


def insert_test_contract(paths: BrainPaths, contract_id: str, page_hint: str) -> None:
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO page_contracts(
              id, page_hint, canonical_entity, page_scope, retrieval_purpose,
              what_belongs_here, what_does_not_belong_here, freshness_policy,
              related_pages, version, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                contract_id,
                page_hint,
                "Test Contract",
                "Facts about the test page.",
                "Answer test questions.",
                "Test facts.",
                "Unrelated facts.",
                "Refresh when facts change.",
                "[]",
                1,
                "active",
                "2026-06-26T00:00:00+00:00",
                "2026-06-26T00:00:00+00:00",
            ),
        )


def insert_audit_policy(
    paths: BrainPaths,
    policy_id: str,
    match: dict[str, object],
    *,
    demotion_threshold: float,
) -> None:
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO cos_policy(
              id, version, priority, match_action_types, match_predicate,
              autonomy_level, critic_required, timeout_allowed,
              timeout_after_seconds, audit_sample_rate, demotion_threshold,
              auto_revert_signals, active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                policy_id,
                99,
                1,
                '["canonicalize_page"]',
                json.dumps({"eq": match}),
                "L2",
                0,
                0,
                None,
                1.0,
                demotion_threshold,
                "[]",
                1,
                "2026-06-25T00:00:00+00:00",
            ),
        )


def insert_critic_policy(paths: BrainPaths, policy_id: str, autonomy_level: str, feature_name: str) -> None:
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO cos_policy(
              id, version, priority, match_action_types, match_predicate,
              autonomy_level, critic_required, timeout_allowed,
              timeout_after_seconds, audit_sample_rate, demotion_threshold,
              auto_revert_signals, active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                policy_id,
                199,
                1,
                '["canonicalize_page"]',
                json.dumps({"eq": {feature_name: True}}),
                autonomy_level,
                1,
                0,
                None,
                1.0,
                0.1,
                "[]",
                1,
                "2026-06-25T00:00:00+00:00",
            ),
        )


class FakeAuditorProvider:
    name = "fake-auditor"
    model = "fake-model"

    def __init__(self, decisions: dict[str, str]) -> None:
        self.decisions = decisions

    def complete(self, prompt: str) -> str:
        assert "independent auditor" in prompt
        return json.dumps(
            {
                "audits": [
                    {
                        "action_id": action_id,
                        "decision": decision,
                        "rationale": f"Auditor marked {action_id} as {decision}.",
                        "confidence": 0.9,
                    }
                    for action_id, decision in self.decisions.items()
                ]
            }
        )


class FailingAuditorProvider:
    name = "failing-auditor"
    model = "fake-model"

    def complete(self, prompt: str) -> str:
        raise AssertionError(f"auditor should not be called: {prompt[:80]}")
