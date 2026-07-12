from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from pkm_brain.automation import run_cos_timeout_sweep
from pkm_brain.cos_actions import (
    ACTION_TYPE_SPECS,
    apply_action,
    apply_decided_action,
    critic_prompt,
    decide_action,
    get_action,
    mark_action_residue,
    propose_action,
    record_action_audit,
    repair_refused_fact_audit_revert,
    revert_action,
)
from pkm_brain.cos_audit import run_sampled_audit
from pkm_brain.cos_policy import (
    PolicyDecision,
    classify_action_risk,
    evaluate_policy,
    promote_policy_for_autonomy,
)
from pkm_brain.db import connection
from pkm_brain.entities import resolve_entity
from pkm_brain.extraction import (
    apply_document_route_coherence,
    backfill_fact_conflict_review_questions,
    conflict_precheck_prompt,
    decide_policy_actions,
    earned_fact_decision,
    evidence_units_for_text,
    extraction_prompt,
    extract_recent_documents,
    normalized_extraction_content,
    record_critic_block_rate_anomalies,
    reclaim_unrouted_facts,
    reconcile_fact_conflict_reviews,
    resolver_precheck_conflict,
    validate_extracted_facts,
    validate_extracted_facts_with_report,
)
from pkm_brain.llm import LLMProviderError, role_env
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


def service_for(tmp_path: Path) -> BrainService:
    return BrainService(BrainPaths.from_value(tmp_path / "brain"))


def evidence_unit_ids_containing(text: str, needle: str) -> list[str]:
    return [
        unit["unit_id"]
        for unit in evidence_units_for_text(text)
        if needle in unit["text"]
    ]


def test_extraction_prompt_requires_direct_entailment_for_clean_facts() -> None:
    prompt = extraction_prompt(
        {
            "document": {"id": "doc_test", "source_type": "hyprnote_meeting"},
            "window": {"chunks": []},
            "routing_hints": [],
        }
    )

    assert "Every part of the statement must be directly entailed" in prompt
    assert "do not join it with an unsupported inference" in prompt
    assert "Preserve uncertainty and negation" in prompt
    assert "document coherence is a preference, not an absolute rule" in prompt


def test_evidence_units_propagate_speaker_identity_across_sentences() -> None:
    units = evidence_units_for_text(
        "Speaker 1: First sentence. Second sentence.\n\n"
        "Speaker 2: Different speaker."
    )

    assert [unit.get("speaker") for unit in units] == [
        "Speaker 1",
        "Speaker 1",
        "Speaker 2",
    ]


def test_extraction_normalization_ignores_empty_hyprnote_capture() -> None:
    normalized = normalized_extraction_content(
        [
            {
                "text": (
                    "---\n"
                    'source_type: "hyprnote_meeting"\n'
                    'agent: "hyprnote"\n'
                    'title: "Chase"\n'
                    'session_id: "session-1"\n'
                    'participants: "Alex, Recruiter"\n'
                    'transcript_render_version: "chronological-speaker-turns-v2"\n'
                    "---\n\n"
                    "# Meeting: Chase\n\n"
                    "## Known Participants\n\n- Alex\n- Recruiter\n\n"
                    "## Summary\n\nNo summary was captured.\n\n"
                    "## Memo\n\nNo memo was captured.\n\n"
                    "## Transcript\n\nNo transcript was captured.\n"
                )
            }
        ]
    )

    assert normalized == ""


@pytest.mark.parametrize(
    "statement",
    [
        "The hyprnote meeting document titled Chase has no captured summary.",
        "Hyprnote session session-1 has no captured memo.",
        'The meeting record "Welcome" has no captured transcript.',
    ],
)
def test_extraction_drops_wrapped_empty_capture_placeholder(
    tmp_path: Path, statement: str
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()

    report = validate_extracted_facts_with_report(
        svc.paths,
        [
            {
                "statement": statement,
                "chunk_id": "unused-placeholder-chunk",
                "evidence_unit_ids": ["u0"],
                "claim_class": "factual_update",
            }
        ],
    )

    assert report["accepted_count"] == 0
    assert report["dropped_count"] == 1
    assert report["dropped"][0]["reason"] == "low_value_placeholder_fact"


def test_extraction_document_id_filter_isolates_target_document(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    (svc.paths.inbox / "target.md").write_text(
        "# Target\n\nTargeted extraction marker is present.", encoding="utf-8"
    )
    (svc.paths.inbox / "other.md").write_text(
        "# Other\n\nUnrelated extraction marker is present.", encoding="utf-8"
    )
    svc.ingest()
    with connection(svc.paths.sqlite_path) as conn:
        target_id = conn.execute(
            "SELECT id FROM documents WHERE source_path LIKE '%/target.md'"
        ).fetchone()[0]
    provider = MarkerExtractorProvider(
        marker="Targeted extraction marker",
        statement="Targeted extraction marker is present.",
    )

    result = extract_recent_documents(
        svc.paths,
        limit=10,
        shadow=True,
        changed_only=False,
        document_ids=[target_id],
        llm_provider=provider,
    )

    assert [document["document_id"] for document in result["documents"]] == [
        target_id
    ]
    assert [candidate["statement"] for candidate in result["candidates"]] == [
        "Targeted extraction marker is present."
    ]
    assert provider.calls == 1


def test_document_route_coherence_reroutes_only_uncertain_dominant_topic() -> None:
    route_targets = {
        "projects/northstar-transition-plan.md": {
            "page_hint": "projects/northstar-transition-plan.md",
            "canonical_entity": "Northstar Transition Plan",
            "page_scope": "Contract transition milestones and timing.",
            "retrieval_purpose": "Answer transition-planning questions.",
        },
        "career/role-preferences.md": {
            "page_hint": "career/role-preferences.md",
            "canonical_entity": "Role Preferences",
            "page_scope": "Career role preferences.",
            "retrieval_purpose": "Answer career preference questions.",
        },
    }
    siblings = [
        {
            "id": f"fact_sibling_{index}",
            "statement": f"Northstar transition-plan detail {index}.",
            "page_hint": "projects/northstar-transition-plan.md",
            "section_hint": "Summary",
            "routing_confidence": 0.95,
            "metadata": {"routing": {"route_destination_valid": True}},
        }
        for index in range(3)
    ]
    uncertain = {
        "statement": "Morgan recommended extending the transition to reach a contractual milestone.",
        "page_hint": "concepts/extracted-facts.md",
        "section_hint": "Summary",
        "routing_confidence": 0.4,
        "metadata": {
            "routing": {
                "route_destination_valid": False,
                "route_review_reason": "fallback_page",
            }
        },
    }
    explicit_outlier = {
        "statement": "Alex prefers an early-stage individual-contributor role.",
        "page_hint": "career/role-preferences.md",
        "section_hint": "Summary",
        "routing_confidence": 0.92,
        "metadata": {"routing": {"route_destination_valid": True}},
    }

    routed = apply_document_route_coherence(
        [*siblings, uncertain, explicit_outlier], route_targets
    )

    assert routed[3]["page_hint"] == "projects/northstar-transition-plan.md"
    assert routed[3]["metadata"]["routing"]["route_resolution"] == (
        "document_coherence_reroute"
    )
    assert routed[4]["page_hint"] == "career/role-preferences.md"


def test_document_route_coherence_does_not_force_split_document_topics() -> None:
    route_targets = {
        page_hint: {
            "page_hint": page_hint,
            "canonical_entity": page_hint,
            "page_scope": page_hint,
            "retrieval_purpose": page_hint,
        }
        for page_hint in ("concepts/alpha.md", "concepts/beta.md")
    }
    candidates = [
        {
            "id": f"fact_{page}_{index}",
            "statement": f"{page} detail {index}",
            "page_hint": f"concepts/{page}.md",
            "section_hint": "Summary",
            "routing_confidence": 0.95,
            "metadata": {"routing": {"route_destination_valid": True}},
        }
        for page in ("alpha", "beta")
        for index in range(2)
    ]
    uncertain = {
        "statement": "A third topic without a clear destination.",
        "page_hint": "concepts/extracted-facts.md",
        "section_hint": "Summary",
        "routing_confidence": 0.3,
        "metadata": {"routing": {"route_destination_valid": False}},
    }

    routed = apply_document_route_coherence([*candidates, uncertain], route_targets)

    assert routed[-1]["page_hint"] == "concepts/extracted-facts.md"


def test_policy_first_match_and_truth_defaults_to_l3(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        safe = evaluate_policy(
            conn,
            "canonicalize_page",
            {
                "deterministic": True,
                "risk_score": 0.01,
                "target_page_paths": ["concepts/test.md"],
            },
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
            {
                "risk_tier": "low",
                "affected_fact_count": 2,
                "truth_mutation": False,
                "reversible": True,
            },
        )
        medium = evaluate_policy(
            conn,
            "page_merge",
            {"risk_tier": "medium", "affected_fact_count": 3},
        )
        clean_fact = evaluate_policy(
            conn,
            "fact_upsert",
            {
                "risk_tier": "medium",
                "clean_fact_upsert": True,
                "fact_upsert_resolution": "new_clean_fact",
                "quote_backed": True,
                "fallback_route": False,
                "resolver_precheck": "passed",
            },
        )
        entity_medium = evaluate_policy(
            conn,
            "entity_merge",
            {"risk_tier": "medium", "affected_fact_count": 3, "merged_entity_count": 2},
        )
        entity_high_certainty = evaluate_policy(
            conn,
            "entity_merge",
            {
                "risk_tier": "low",
                "merge_signal": "same_compact_name_or_alias",
                "affected_fact_count": 23,
                "merged_entity_count": 2,
                "large_topology": True,
                "cross_entity_merge": False,
                "cross_type_merge": False,
                "type_mismatch": False,
            },
        )
        large = evaluate_policy(
            conn,
            "page_merge",
            {"risk_tier": "high", "affected_fact_count": 5, "large_topology": True},
        )
        entity_large = evaluate_policy(
            conn,
            "entity_merge",
            {"risk_tier": "high", "affected_fact_count": 5, "cross_type_merge": True},
        )

    assert version == 2
    assert low.autonomy_level == "L2"
    assert low.critic_required is False
    assert low.audit_sample_rate == 0.25
    assert "Synthesis is derived" in low.reason
    assert "matched policy" not in low.reason
    assert medium.autonomy_level == "L2"
    assert medium.audit_sample_rate == 1.0
    assert clean_fact.autonomy_level == "L2"
    assert clean_fact.critic_required is True
    assert entity_medium.autonomy_level == "L2"
    assert entity_high_certainty.autonomy_level == "L1"
    assert entity_high_certainty.critic_required is False
    assert large.autonomy_level == "L3"
    assert entity_large.autonomy_level == "L3"


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
    assert (
        classify_action_risk(
            "page_merge",
            {"affected_fact_count": 20, "topology_review_threshold": 32},
            explicit_risk_tier="medium",
        )
        == "medium"
    )
    assert (
        classify_action_risk(
            "page_merge",
            {"affected_fact_count": 33, "topology_review_threshold": 32},
            explicit_risk_tier="medium",
        )
        == "high"
    )
    assert classify_action_risk("synthesize_page", {"affected_fact_count": 3}) == "low"
    assert (
        classify_action_risk(
            "entity_merge",
            {"merged_entity_count": 9},
            explicit_risk_tier="medium",
            large_topology_fact_threshold=8,
        )
        == "high"
    )
    assert (
        classify_action_risk(
            "entity_merge",
            {
                "affected_fact_count": 23,
                "merged_entity_count": 2,
                "large_topology": True,
                "merge_signal": "same_compact_name_or_alias",
                "cross_entity_merge": False,
                "cross_type_merge": False,
                "type_mismatch": False,
            },
            explicit_risk_tier="low",
            large_topology_fact_threshold=8,
        )
        == "low"
    )
    assert (
        classify_action_risk(
            "entity_merge",
            {"cross_type_merge": True},
            explicit_risk_tier="medium",
        )
        == "high"
    )


def test_timeout_sweep_resolves_only_non_truth_residue_to_uncertainty(
    tmp_path: Path,
) -> None:
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


def test_candidate_key_reuses_open_action_and_retires_legacy_sibling(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    candidate_key = "canonicalize_page:concepts/review.md"
    first = propose_action(
        svc.paths,
        "canonicalize_page",
        action_payload={"page_hint": "concepts/review.md"},
        action_features={"candidate_key": candidate_key, "reversible": True},
        target_page_paths=["concepts/review.md"],
    )
    repeated = propose_action(
        svc.paths,
        "canonicalize_page",
        action_payload={"page_hint": "concepts/review.md"},
        action_features={"candidate_key": candidate_key, "reversible": True},
        target_page_paths=["concepts/review.md"],
    )
    legacy_sibling = propose_action(
        svc.paths,
        "canonicalize_page",
        action_payload={"page_hint": "concepts/review.md"},
        action_features={"reversible": True},
        target_page_paths=["concepts/review.md"],
    )
    with connection(svc.paths.sqlite_path) as conn:
        features = json.loads(
            conn.execute(
                "SELECT action_features FROM cos_actions WHERE id = ?",
                (legacy_sibling["id"],),
            ).fetchone()[0]
        )
        features["candidate_key"] = candidate_key
        conn.execute(
            "UPDATE cos_actions SET action_features = ? WHERE id = ?",
            (json.dumps(features), legacy_sibling["id"]),
        )
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, fact_ids, question, options, status, context,
              action_id, recommended_action, risk_tier, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "question_legacy_duplicate",
                "policy_escalation",
                "[]",
                "Review duplicate candidate.",
                "[]",
                "needs_human",
                "{}",
                legacy_sibling["id"],
                "{}",
                "low",
                "2026-07-09T12:00:00+00:00",
            ),
        )

    applied = apply_action(svc.paths, first["id"])

    assert repeated["id"] == first["id"]
    assert applied["status"] == "applied"
    with connection(svc.paths.sqlite_path) as conn:
        sibling = conn.execute(
            "SELECT status, evidence_json FROM cos_actions WHERE id = ?",
            (legacy_sibling["id"],),
        ).fetchone()
        question = conn.execute(
            "SELECT status, decided_by FROM open_questions WHERE id = ?",
            ("question_legacy_duplicate",),
        ).fetchone()
    assert sibling["status"] == "dismissed"
    assert (
        json.loads(sibling["evidence_json"])["candidate_superseded"]["by_action_id"]
        == first["id"]
    )
    assert question["status"] == "dismissed"
    assert question["decided_by"] == "candidate_deduplication"


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


def test_repair_refused_fact_audit_revert_restores_reviewable_action(
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
                    "id": "fact_audit_repair",
                    "statement": "The audited fact remains active after routing drift.",
                    "entity_key": "concept:test:summary",
                    "page_hint": "concepts/test.md",
                    "section_hint": "Summary",
                    "source_ids": ["manual:test"],
                    "confidence": 0.9,
                }
            },
            action_features={"reversible": True},
        )["id"],
    )
    record_action_audit(
        svc.paths,
        action["id"],
        "sampled_bad",
        metadata={"rationale": "Fact needs review."},
    )
    with connection(svc.paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE facts SET page_hint = 'concepts/new-home.md' WHERE id = 'fact_audit_repair'"
        )

    failed = revert_action(svc.paths, action["id"])
    repaired = repair_refused_fact_audit_revert(svc.paths, action["id"])

    assert failed["status"] == "failed"
    assert repaired["status"] == "repaired"
    assert repaired["action"]["status"] == "applied"
    assert repaired["action"]["audit_status"] == "sampled_bad"
    assert repaired["action"]["evidence_json"]["audit_queue_reconciliation"][
        "outcome"
    ] == "restored_applied_status_after_refused_revert"
    with connection(svc.paths.sqlite_path) as conn:
        residue = conn.execute(
            "SELECT status, decided_by FROM open_questions WHERE action_id = ?",
            (action["id"],),
        ).fetchone()
    assert residue["status"] == "auto_resolved"
    assert residue["decided_by"] == "audit_queue_reconciliation_v1"


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
    assert contract["inverse_action_json"] == {
        "delete_contract_ids": ["contract_inverse"]
    }
    assert synthesis["inverse_action_json"] == {
        "delete_synthesis_ids": ["synthesis_inverse"]
    }
    assert canonical["inverse_action_json"] == {"noop": True}


def test_page_merge_apply_and_revert_round_trip(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    insert_test_fact(
        svc.paths,
        "fact_merge_left",
        "Left fact.",
        page_hint="concepts/alpha-payment.md",
    )
    insert_test_fact(
        svc.paths,
        "fact_merge_right",
        "Right fact.",
        page_hint="concepts/alpha-payments.md",
    )
    insert_test_contract(svc.paths, "contract_left", "concepts/alpha-payment.md")
    action = propose_action(
        svc.paths,
        "page_merge",
        action_payload={
            "candidate": {
                "page_hints": [
                    "concepts/alpha-payment.md",
                    "concepts/alpha-payments.md",
                ],
                "destination_page_hint": "concepts/alpha-payments.md",
            }
        },
        target_page_paths=["concepts/alpha-payment.md", "concepts/alpha-payments.md"],
    )

    applied = apply_action(svc.paths, action["id"])

    assert applied["status"] == "applied"
    assert applied["inverse_action_json"]["restore_facts"]
    with connection(svc.paths.sqlite_path) as conn:
        left = conn.execute(
            "SELECT page_hint FROM facts WHERE id = 'fact_merge_left'"
        ).fetchone()
        contract = conn.execute(
            "SELECT status FROM page_contracts WHERE id = 'contract_left'"
        ).fetchone()
    assert left["page_hint"] == "concepts/alpha-payments.md"
    assert contract["status"] == "superseded"

    reverted = revert_action(svc.paths, action["id"])

    assert reverted["status"] == "reverted"
    with connection(svc.paths.sqlite_path) as conn:
        left = conn.execute(
            "SELECT page_hint FROM facts WHERE id = 'fact_merge_left'"
        ).fetchone()
        contract = conn.execute(
            "SELECT status FROM page_contracts WHERE id = 'contract_left'"
        ).fetchone()
    assert left["page_hint"] == "concepts/alpha-payment.md"
    assert contract["status"] == "active"


def test_page_split_apply_and_revert_round_trip(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    insert_test_fact(
        svc.paths,
        "fact_split_summary",
        "Summary fact.",
        page_hint="projects/sprawl.md",
        section_hint="Summary",
    )
    insert_test_fact(
        svc.paths,
        "fact_split_pricing",
        "Pricing fact.",
        page_hint="projects/sprawl.md",
        section_hint="Pricing",
    )
    insert_test_fact(
        svc.paths,
        "fact_split_risk",
        "Risk fact.",
        page_hint="projects/sprawl.md",
        section_hint="Risks",
    )
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
            for row in conn.execute(
                "SELECT id, page_hint FROM facts WHERE id LIKE 'fact_split_%'"
            )
        }
    assert pages["fact_split_summary"] == "projects/sprawl.md"
    assert pages["fact_split_pricing"] == "projects/sprawl-pricing.md"
    assert pages["fact_split_risk"] == "projects/sprawl-risks.md"

    reverted = revert_action(svc.paths, action["id"])

    assert reverted["status"] == "reverted"
    with connection(svc.paths.sqlite_path) as conn:
        pages = {
            row["id"]: row["page_hint"]
            for row in conn.execute(
                "SELECT id, page_hint FROM facts WHERE id LIKE 'fact_split_%'"
            )
        }
    assert set(pages.values()) == {"projects/sprawl.md"}


def test_rename_page_apply_and_revert_round_trip(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    insert_test_fact(
        svc.paths, "fact_rename", "Rename fact.", page_hint="concepts/old-name.md"
    )
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
        action_payload={
            "from_page_hint": "concepts/old-name.md",
            "to_page_hint": "concepts/new-name.md",
        },
        target_page_paths=["concepts/old-name.md", "concepts/new-name.md"],
    )

    applied = apply_action(svc.paths, action["id"])

    assert applied["status"] == "applied"
    with connection(svc.paths.sqlite_path) as conn:
        fact = conn.execute(
            "SELECT page_hint FROM facts WHERE id = 'fact_rename'"
        ).fetchone()
        contract = conn.execute(
            "SELECT page_hint FROM page_contracts WHERE id = 'contract_rename'"
        ).fetchone()
        synthesis = conn.execute(
            "SELECT page_hint, stale FROM wiki_page_syntheses WHERE id = 'synthesis_rename'"
        ).fetchone()
    assert fact["page_hint"] == "concepts/new-name.md"
    assert contract["page_hint"] == "concepts/new-name.md"
    assert synthesis["page_hint"] == "concepts/new-name.md"
    assert synthesis["stale"] == 1

    reverted = revert_action(svc.paths, action["id"])

    assert reverted["status"] == "reverted"
    with connection(svc.paths.sqlite_path) as conn:
        fact = conn.execute(
            "SELECT page_hint FROM facts WHERE id = 'fact_rename'"
        ).fetchone()
        contract = conn.execute(
            "SELECT page_hint FROM page_contracts WHERE id = 'contract_rename'"
        ).fetchone()
        synthesis = conn.execute(
            "SELECT page_hint, stale FROM wiki_page_syntheses WHERE id = 'synthesis_rename'"
        ).fetchone()
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
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM cos_stage_watermarks WHERE stage = 'extractor'"
            ).fetchone()[0]
            == 1
        )


def test_extraction_accepts_v5_watermark_without_global_rebuild(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nWatermarked extraction marker.", encoding="utf-8")
    svc.ingest()
    provider = FakeExtractorProvider()
    first = extract_recent_documents(svc.paths, shadow=True, llm_provider=provider)
    with connection(svc.paths.sqlite_path) as conn:
        conn.execute(
            "UPDATE cos_stage_watermarks SET prompt_version = ?",
            ("extractor-evidence-units-v5",),
        )

    second = extract_recent_documents(svc.paths, shadow=True, llm_provider=provider)

    assert len(first["documents"]) == 1
    assert second["documents"] == []
    assert provider.calls == 1


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
    assert [attempt["accepted_count"] for attempt in window_validation["attempts"]] == [
        0,
        1,
    ]
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

    result = extract_recent_documents(
        svc.paths, shadow=True, llm_provider=provider, limit=5
    )

    assert result["status"] == "ok"
    assert [document["source_type"] for document in result["documents"]] == [
        "markdown_note"
    ]
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

    result = extract_recent_documents(
        svc.paths, shadow=True, llm_provider=provider, limit=1
    )

    assert result["status"] == "ok"
    assert len(result["documents"][0]["chunks"]) == 12
    assert len(result["documents"][0]["windows"]) == 3
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["source_ids"] == ["chunk:chunk_doc_windowed_11"]
    assert any(marker in prompt for prompt in provider.prompts)


def test_extraction_prefilters_low_information_windows(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    svc.paths.config_local.mkdir(parents=True, exist_ok=True)
    (svc.paths.config_local / "cos_llm.yaml").write_text(
        "extraction:\n  window:\n    max_chunks: 1\n    overlap_chunks: 0\n",
        encoding="utf-8",
    )
    marker = "Durable prefilter marker."
    insert_document_with_chunks(
        svc.paths,
        "doc_prefilter",
        "hyprnote_meeting",
        [
            "event_started_at: 2026-06-26T00:00:00+00:00\n"
            "No summary was captured.\n"
            "No memo was captured.\n"
            "No transcript was captured.",
            marker,
        ],
    )
    provider = MarkerExtractorProvider(
        marker=marker,
        statement="Durable prefilter marker is present.",
        page_hint="concepts/prefilter.md",
    )

    result = extract_recent_documents(
        svc.paths, shadow=True, llm_provider=provider, limit=1
    )

    assert provider.calls == 1
    assert len(result["documents"][0]["windows"]) == 1
    assert len(result["documents"][0]["skipped_windows"]) == 1
    assert (
        result["documents"][0]["skipped_windows"][0]["reason"]
        == "low_information_window"
    )
    assert result["validation"]["source_window_count"] == 2
    assert result["validation"]["window_count"] == 1
    assert result["validation"]["skipped_window_count"] == 1
    assert len(result["candidates"]) == 1


def test_extraction_parallelizes_window_llm_calls(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    chunk_texts = [f"Parallel extraction marker {index}." for index in range(21)]
    insert_document_with_chunks(
        svc.paths,
        "doc_parallel",
        "markdown_note",
        chunk_texts,
    )
    provider = ConcurrentWindowExtractorProvider(delay_seconds=0.05)

    result = extract_recent_documents(
        svc.paths,
        shadow=True,
        llm_provider=provider,
        limit=1,
        max_workers=4,
    )

    assert provider.calls == len(result["documents"][0]["windows"])
    assert provider.max_active > 1
    assert result["timing"]["worker_count"] == 4
    assert result["validation"]["worker_count"] == 4
    assert result["validation"]["window_count"] == 4
    assert result["validation"]["attempt_count"] == provider.calls
    assert result["validation"]["llm_duration_ms"] > 0
    assert all(
        window["duration_ms"] > 0
        for window in result["document_validations"][0]["windows"]
    )
    assert all(
        attempt["llm_duration_ms"] > 0
        for window in result["document_validations"][0]["windows"]
        for attempt in window["attempts"]
    )
    assert [
        candidate["metadata"]["window_id"] for candidate in result["candidates"]
    ] == [window["window_id"] for window in result["documents"][0]["windows"]]


def test_extraction_prompt_includes_routing_hints_without_fact_rows(
    tmp_path: Path,
) -> None:
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


def test_extraction_ranks_routing_hints_by_window_relevance(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    svc.paths.config_local.mkdir(parents=True, exist_ok=True)
    (svc.paths.config_local / "cos_llm.yaml").write_text(
        "extraction:\n  routing_hints_limit: 1\n",
        encoding="utf-8",
    )
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nNorthwind relevance marker.", encoding="utf-8")
    svc.ingest()
    insert_test_contract(
        svc.paths,
        "contract_zapier",
        "companies/zapier.md",
        canonical_entity="Zapier",
        page_scope="Facts about Zapier integrations.",
    )
    insert_test_contract(
        svc.paths,
        "contract_northwind",
        "companies/northwind.md",
        canonical_entity="Northwind",
        page_scope="Facts about Northwind customers and products.",
    )
    provider = MarkerExtractorProvider(
        marker="Northwind relevance marker.",
        statement="Northwind relevance marker is present.",
    )

    result = extract_recent_documents(svc.paths, shadow=True, llm_provider=provider)

    source_window = json.loads(provider.prompts[0].rsplit("Source window JSON:", 1)[-1])
    assert result["status"] == "ok"
    assert source_window["routing_hints"] == [
        {
            "page_hint": "companies/northwind.md",
            "canonical_entity": "Northwind",
            "page_scope": "Facts about Northwind customers and products.",
            "retrieval_purpose": "Answer test questions.",
        }
    ]
    assert result["document_validations"][0]["windows"][0][
        "routing_hint_page_hints"
    ] == ["companies/northwind.md"]


def test_extraction_routing_hints_exclude_reference_destinations(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    svc.paths.config_local.mkdir(parents=True, exist_ok=True)
    (svc.paths.config_local / "cos_llm.yaml").write_text(
        "extraction:\n  routing_hints_limit: 3\n",
        encoding="utf-8",
    )
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nNorthwind routing hygiene marker.", encoding="utf-8")
    svc.ingest()
    insert_test_wiki_page(
        svc.paths,
        "page_reference_northwind",
        "/Users/example/brain/wiki/references/spencer-alex-northwind-chat.md",
        title="Spencer Alex Northwind Chat",
        page_type="reference",
        managed=True,
    )
    insert_test_wiki_page(
        svc.paths,
        "page_agent_log_northwind",
        "references/agent_session_log/northwind-routing-hygiene.md",
        title="Northwind Routing Hygiene Log",
        page_type="reference",
        managed=True,
    )
    insert_test_wiki_page(
        svc.paths,
        "page_project_northwind",
        "/Users/example/brain/wiki/projects/northwind.md",
        title="Northwind",
        page_type="project",
        managed=True,
    )
    provider = MarkerExtractorProvider(
        marker="Northwind routing hygiene marker.",
        statement="Northwind routing hygiene marker is present.",
        page_hint="projects/northwind.md",
    )

    result = extract_recent_documents(svc.paths, shadow=True, llm_provider=provider)

    source_window = json.loads(provider.prompts[0].rsplit("Source window JSON:", 1)[-1])
    page_hints = [hint["page_hint"] for hint in source_window["routing_hints"]]
    assert result["status"] == "ok"
    assert "projects/northwind.md" in page_hints
    assert all("references/" not in page_hint for page_hint in page_hints)


def test_extraction_reference_page_hint_becomes_unrouted_residue(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    enable_simple_autonomy(svc.paths)
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nReference route marker.", encoding="utf-8")
    svc.ingest()
    provider = MarkerExtractorProvider(
        marker="Reference route marker.",
        statement="Reference route marker is present.",
        page_hint="wiki/references/agent_session_log/reference-route.md",
    )

    result = extract_recent_documents(svc.paths, shadow=False, llm_provider=provider)

    assert result["status"] == "ok"
    assert result["candidates"][0]["page_hint"] == "concepts/extracted-facts.md"
    assert result["validation"]["invalid_route_destination_count"] == 1
    routing = result["candidates"][0]["metadata"]["routing"]
    assert (
        routing["original_page_hint"]
        == "wiki/references/agent_session_log/reference-route.md"
    )
    assert routing["route_destination_valid"] is False
    assert routing["route_review_reason"] == "non_canonical_route_namespace"
    assert result["actions"][0]["status"] == "needs_human"
    with connection(svc.paths.sqlite_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
        residue = conn.execute(
            "SELECT kind FROM open_questions WHERE action_id = ?",
            (result["actions"][0]["id"],),
        ).fetchone()
    assert residue["kind"] == "unrouted_fact"


def test_extraction_normalizes_wiki_prefix_for_canonical_route(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    enable_simple_autonomy(svc.paths)
    insert_test_wiki_page(
        svc.paths,
        "page_project_databridge",
        "projects/databridge.md",
        title="DataBridge",
        page_type="project",
        managed=True,
    )
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nDataBridge route marker.", encoding="utf-8")
    svc.ingest()
    provider = MarkerExtractorProvider(
        marker="DataBridge route marker.",
        statement="DataBridge route marker is present.",
        page_hint="wiki/projects/databridge.md",
    )

    result = extract_recent_documents(svc.paths, shadow=False, llm_provider=provider)

    assert result["status"] == "ok"
    assert result["candidates"][0]["page_hint"] == "projects/databridge.md"
    assert result["validation"]["existing_route_target_count"] == 1
    assert result["actions"][0]["status"] == "auto_applied"
    with connection(svc.paths.sqlite_path) as conn:
        fact = conn.execute("SELECT page_hint FROM facts").fetchone()
    assert fact["page_hint"] == "projects/databridge.md"


def test_extraction_fuzzy_snaps_near_duplicate_canonical_route(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    insert_test_wiki_page(
        svc.paths,
        "page_agent_pm_role_notes",
        "career/agent-pm-role-notes.md",
        title="Agent PM Role Notes",
        page_type="career",
        managed=True,
    )
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nCareer route marker.", encoding="utf-8")
    svc.ingest()
    provider = MarkerExtractorProvider(
        marker="Career route marker.",
        statement="Career route marker is present.",
        page_hint="career/2026-agent-pm-role-notes.md",
    )

    result = extract_recent_documents(svc.paths, shadow=True, llm_provider=provider)

    assert result["status"] == "ok"
    assert result["candidates"][0]["page_hint"] == "career/agent-pm-role-notes.md"
    assert result["validation"]["fuzzy_snapped_route_count"] == 1
    routing = result["candidates"][0]["metadata"]["routing"]
    assert routing["route_resolution"] == "fuzzy_snapped_existing_page"
    assert routing["snapped_page_hint"] == "career/agent-pm-role-notes.md"


def test_extraction_derives_spans_from_evidence_unit_ids(tmp_path: Path) -> None:
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
                "evidence_unit_ids": evidence_unit_ids_containing(
                    chunk_text, "Document"
                ),
                "claim_class": "factual_update",
            }
        ],
        extractor_model="fake-extractor-model",
    )

    assert len(candidates) == 1
    expected_start = chunk_text.index("Document")
    assert candidates[0]["source_spans"] == [
        {
            "chunk_id": "chunk_offset",
            "start": expected_start,
            "end": expected_start + len("Document   offset marker is present."),
        }
    ]
    assert candidates[0]["evidence_quote"] == "Document   offset marker is present."
    assert candidates[0]["evidence_unit_ids"] == ["u1"]
    assert candidates[0]["extractor_model"] == "fake-extractor-model"
    missing_unit_candidates = validate_extracted_facts(
        svc.paths,
        [
            {
                "statement": "Document offset marker is present.",
                "chunk_id": "chunk_offset",
                "evidence_unit_ids": ["u99"],
                "claim_class": "factual_update",
            }
        ],
        extractor_model="fake-extractor-model",
    )

    assert missing_unit_candidates == []


def test_extraction_truncates_excess_evidence_unit_ids(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "source.md"
    note.write_text(
        "# Source\n\n"
        "Alpha evidence appears. "
        "Beta evidence appears. "
        "Gamma evidence appears. "
        "Delta evidence appears. "
        "Epsilon evidence appears. "
        "Zeta evidence appears. "
        "Eta evidence appears.",
        encoding="utf-8",
    )
    svc.ingest()
    with connection(svc.paths.sqlite_path) as conn:
        chunk = conn.execute("SELECT id, text FROM chunks LIMIT 1").fetchone()
    unit_ids = [unit["unit_id"] for unit in evidence_units_for_text(chunk["text"])]

    report = validate_extracted_facts_with_report(
        svc.paths,
        [
            {
                "statement": "Alpha evidence appears.",
                "chunk_id": chunk["id"],
                "evidence_unit_ids": unit_ids[:7],
                "claim_class": "factual_update",
            }
        ],
        extractor_model="fake-extractor-model",
    )

    assert report["rejected_count"] == 0
    assert len(report["candidates"]) == 1
    assert report["candidates"][0]["evidence_unit_ids"] == unit_ids[:5]
    assert report["candidates"][0]["metadata"]["evidence_unit_truncation"] == {
        "original_count": 7,
        "kept_count": 5,
        "truncated_count": 2,
    }


def test_extraction_accepts_structured_entity_mentions(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nLakehouseCo signed DataBridge.", encoding="utf-8")
    svc.ingest()
    with connection(svc.paths.sqlite_path) as conn:
        chunk = conn.execute("SELECT id, text FROM chunks LIMIT 1").fetchone()

    candidates = validate_extracted_facts(
        svc.paths,
        [
            {
                "statement": "LakehouseCo signed DataBridge.",
                "chunk_id": chunk["id"],
                "evidence_unit_ids": evidence_unit_ids_containing(
                    chunk["text"], "LakehouseCo"
                ),
                "claim_class": "factual_update",
                "page_hint": "companies/lakehouseco.md",
                "section_hint": "Partnerships",
                "entity_key": "LakehouseCo",
                "entities": [
                    {
                        "surface": "LakehouseCo",
                        "type": "organization",
                        "mention_kind": "named",
                        "is_primary": True,
                    },
                    {
                        "surface": "DataBridge",
                        "type": "organization",
                        "mention_kind": "named",
                        "is_primary": False,
                    },
                ],
            }
        ],
        extractor_model="fake-extractor-model",
    )

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["entity_mention"] == "LakehouseCo"
    assert candidate["entity_type"] == "organization"
    assert candidate["metadata"]["model_entity_key"] == "LakehouseCo"
    assert (
        candidate["entity_mentions"] == candidate["metadata"]["model_entity_mentions"]
    )
    assert [mention["surface"] for mention in candidate["entity_mentions"]] == [
        "LakehouseCo",
        "DataBridge",
    ]
    assert [mention["mention_kind"] for mention in candidate["entity_mentions"]] == [
        "named",
        "named",
    ]
    assert candidate["entity_mentions"][0]["mention_span"]["chunk_id"] == chunk["id"]
    assert candidate["entity_mentions"][1]["mention_span"]["chunk_id"] == chunk["id"]


def test_extraction_treats_entity_faithfulness_as_advisory(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "source.md"
    note.write_text("# Source\n\nUnity Catalog governs access.", encoding="utf-8")
    svc.ingest()
    with connection(svc.paths.sqlite_path) as conn:
        chunk = conn.execute("SELECT id, text FROM chunks LIMIT 1").fetchone()

    candidates = validate_extracted_facts(
        svc.paths,
        [
            {
                "statement": "LakehouseCo Unity Catalog governs access.",
                "chunk_id": chunk["id"],
                "evidence_unit_ids": evidence_unit_ids_containing(
                    chunk["text"], "Unity Catalog"
                ),
                "claim_class": "factual_update",
                "entities": [
                    {
                        "surface": "LakehouseCo",
                        "type": "organization",
                        "mention_kind": "named",
                        "is_primary": True,
                    }
                ],
            }
        ],
        extractor_model="fake-extractor-model",
    )

    assert len(candidates) == 1
    assert candidates[0]["entity_mention"] == "LakehouseCo"


def test_extraction_accepts_supported_numeric_paraphrase(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "source.md"
    note.write_text(
        "# Source\n\nNorthwind reached two hundred million ARR in eight or nine quarters.",
        encoding="utf-8",
    )
    svc.ingest()
    with connection(svc.paths.sqlite_path) as conn:
        chunk = conn.execute("SELECT id, text FROM chunks LIMIT 1").fetchone()

    candidates = validate_extracted_facts(
        svc.paths,
        [
            {
                "statement": "Northwind reached $200M ARR in eight or nine quarters.",
                "chunk_id": chunk["id"],
                "evidence_unit_ids": evidence_unit_ids_containing(
                    chunk["text"], "two hundred million"
                ),
                "claim_class": "factual_update",
                "entities": [
                    {
                        "surface": "Northwind",
                        "type": "organization",
                        "mention_kind": "named",
                        "is_primary": True,
                    }
                ],
            }
        ],
        extractor_model="fake-extractor-model",
    )

    assert len(candidates) == 1
    assert "two hundred million" in candidates[0]["evidence_quote"]


def test_extraction_ignores_identifier_like_numbers_in_faithfulness_gate(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "source.md"
    note.write_text(
        "# Source\n\n"
        "The product serves business customers, uses modern model access, "
        "supports the current API, offers always-on coverage, and helps "
        "initial workflows.",
        encoding="utf-8",
    )
    svc.ingest()
    with connection(svc.paths.sqlite_path) as conn:
        chunk = conn.execute("SELECT id, text FROM chunks LIMIT 1").fetchone()

    report = validate_extracted_facts_with_report(
        svc.paths,
        [
            {
                "statement": (
                    "The product serves B2B customers, uses GPT-4, supports v2 "
                    "APIs, offers 24/7 coverage, and helps zero-to-one workflows."
                ),
                "chunk_id": chunk["id"],
                "evidence_unit_ids": evidence_unit_ids_containing(
                    chunk["text"], "business customers"
                ),
                "claim_class": "factual_update",
            }
        ],
        extractor_model="fake-extractor-model",
    )

    assert len(report["candidates"]) == 1
    assert not any(
        "unsupported number" in reason
        for rejection in report["rejections"]
        for reason in rejection["reasons"]
    )


def test_extraction_rejects_unsupported_statement_number(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "source.md"
    note.write_text(
        "# Source\n\nAlex expects probably sixty seven hours a week of work.",
        encoding="utf-8",
    )
    svc.ingest()
    with connection(svc.paths.sqlite_path) as conn:
        chunk = conn.execute("SELECT id, text FROM chunks LIMIT 1").fetchone()

    report = validate_extracted_facts_with_report(
        svc.paths,
        [
            {
                "statement": "Alex expects probably sixty hours a week of work.",
                "chunk_id": chunk["id"],
                "evidence_unit_ids": evidence_unit_ids_containing(
                    chunk["text"], "sixty seven"
                ),
                "claim_class": "factual_update",
            }
        ],
        extractor_model="fake-extractor-model",
    )

    assert report["candidates"] == []
    assert any(
        "unsupported number" in reason
        for rejection in report["rejections"]
        for reason in rejection["reasons"]
    )


def test_extraction_does_not_retry_unsupported_statement_faithfulness(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "source.md"
    note.write_text(
        "# Source\n\nAlex expects probably sixty seven hours a week of work.",
        encoding="utf-8",
    )
    svc.ingest()
    provider = UnsupportedNumberExtractorProvider()

    result = extract_recent_documents(svc.paths, shadow=True, llm_provider=provider)

    assert provider.calls == 1
    assert result["candidates"] == []
    assert result["validation"]["attempt_count"] == 1
    assert result["validation"]["rejected_count"] == 1
    assert any(
        "statement_not_supported_by_evidence" in reason
        for window in result["document_validations"][0]["windows"]
        for rejection in window["rejections"]
        for reason in rejection["reasons"]
    )


def test_extraction_drops_non_claim_classes_and_marks_extracted_empty(
    tmp_path: Path,
) -> None:
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


def test_extraction_drops_placeholder_absence_facts_even_if_mislabeled(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "source.md"
    note.write_text(
        '# Source\n\n## Summary\n\nNo summary was captured for the meeting titled "Family time".',
        encoding="utf-8",
    )
    svc.ingest()
    with connection(svc.paths.sqlite_path) as conn:
        chunk = conn.execute("SELECT id, text FROM chunks LIMIT 1").fetchone()

    report = validate_extracted_facts_with_report(
        svc.paths,
        [
            {
                "statement": 'No summary was captured for the meeting titled "Family time".',
                "chunk_id": chunk["id"],
                "evidence_unit_ids": evidence_unit_ids_containing(
                    chunk["text"], "No summary"
                ),
                "claim_class": "factual_update",
            }
        ],
        extractor_model="fake-extractor-model",
    )

    assert report["candidates"] == []
    assert report["rejected_count"] == 0
    assert report["dropped_count"] == 1
    assert report["dropped"][0]["reason"] == "low_value_placeholder_fact"


def test_extraction_empty_normalized_content_skips_cosmetic_reexports(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    insert_document_with_chunks(
        svc.paths,
        "doc_empty_template",
        "hyprnote_meeting",
        [
            'title: "StreamingCo interview block"\n'
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
                'title: "StreamingCo interview block re-exported"\n'
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


def test_extraction_default_path_sends_fallback_route_to_unrouted_residue(
    tmp_path: Path,
) -> None:
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


def test_extraction_conflict_precheck_residue_includes_counterpart_options(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    insert_test_wiki_page(
        svc.paths,
        "page_alphapay",
        str(svc.paths.wiki / "concepts/alphapay.md"),
        title="AlphaPay",
        page_type="concept",
        managed=True,
    )
    with connection(svc.paths.sqlite_path) as conn:
        resolution = resolve_entity(conn, "AlphaPay", type_hint="product")
        assert resolution is not None
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, entity_id, page_hint, section_hint,
              source_ids, observed_at, confidence, status, metadata, created_at,
              truth_confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fact_alphapay_enabled",
                "AlphaPay auto-renewal is enabled by default for annual plans.",
                "concepts:alphapay:summary",
                resolution.entity_id,
                "concepts/alphapay.md",
                "Summary",
                json.dumps(["document:alphapay-old"]),
                "2026-06-26T00:00:00+00:00",
                0.9,
                "active",
                "{}",
                "2026-06-26T00:00:00+00:00",
                0.9,
            ),
        )
    marker = "AlphaPay auto-renewal is not enabled by default for annual plans."
    note = svc.paths.inbox / "source.md"
    note.write_text(f"# Source\n\n{marker}", encoding="utf-8")
    svc.ingest()
    provider = MarkerExtractorProvider(
        marker=marker,
        statement=marker,
        page_hint="concepts/alphapay.md",
        entity_mentions=[
            {
                "surface": "AlphaPay",
                "type": "product",
                "mention_kind": "named",
                "is_primary": True,
            }
        ],
    )

    result = extract_recent_documents(svc.paths, shadow=False, llm_provider=provider)

    assert result["status"] == "ok"
    assert result["actions"][0]["status"] == "needs_human"
    assert result["actions"][0]["target_fact_ids"] == ["fact_alphapay_enabled"]
    resolver_precheck = result["actions"][0]["evidence_json"]["resolver_precheck"]
    assert resolver_precheck["counterpart_fact_ids"] == ["fact_alphapay_enabled"]
    with connection(svc.paths.sqlite_path) as conn:
        question = conn.execute(
            "SELECT * FROM open_questions WHERE action_id = ?",
            (result["actions"][0]["id"],),
        ).fetchone()
    assert question["kind"] == "fact_conflict_review"
    assert json.loads(question["fact_ids"]) == ["fact_alphapay_enabled"]
    options = json.loads(question["options"])
    assert [option["option_type"] for option in options] == [
        "candidate_fact",
        "existing_fact",
    ]
    assert options[0]["statement"] == marker
    assert options[0]["evidence_quote"] == marker
    assert options[1]["fact_id"] == "fact_alphapay_enabled"
    assert "Review the candidate fact" in question["question"]
    assert resolver_precheck["relation_classifications"][0]["relation"] == "contradicts"


def test_conflict_precheck_does_not_propagate_unrelated_contested_bucket(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        resolution = resolve_entity(conn, "Alex", type_hint="person")
        assert resolution is not None
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, entity_id, page_hint, section_hint,
              source_ids, observed_at, confidence, status, metadata, created_at,
              truth_confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fact_transition_contested",
                "Alex's next vesting date is in June.",
                "career:role-preferences:summary",
                resolution.entity_id,
                "career/role-preferences.md",
                "Summary",
                "[]",
                "2026-07-10T00:00:00+00:00",
                0.9,
                "conflicted",
                "{}",
                "2026-07-10T00:00:00+00:00",
                0.9,
            ),
        )

    conflict = resolver_precheck_conflict(
        svc.paths,
        {
            "statement": "Alex is open to individual-contributor roles at startups.",
            "entity_id": resolution.entity_id,
            "entity_key": "career:role-preferences:summary",
            "page_hint": "career/role-preferences.md",
        },
    )

    assert conflict is None


def test_resolver_confirmation_can_release_deterministic_conflict_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        resolution = resolve_entity(conn, "AlphaPay", type_hint="product")
        assert resolution is not None
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, entity_id, page_hint, section_hint,
              source_ids, observed_at, confidence, status, metadata, created_at,
              truth_confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fact_alphapay_enabled",
                "AlphaPay auto-renewal is enabled by default for annual plans.",
                "concepts:alphapay:summary",
                resolution.entity_id,
                "concepts/alphapay.md",
                "Summary",
                "[]",
                "2026-07-10T00:00:00+00:00",
                0.9,
                "active",
                "{}",
                "2026-07-10T00:00:00+00:00",
                0.9,
            ),
        )
    candidate = {
        "statement": "AlphaPay auto-renewal is not enabled by default for annual plans.",
        "entity_id": resolution.entity_id,
        "entity_key": "concepts:alphapay:summary",
        "page_hint": "concepts/alphapay.md",
        "section_hint": "Summary",
        "source_ids": ["chunk:new"],
        "source_spans": [{"chunk_id": "new", "start": 0, "end": 20}],
        "evidence_quote": "AlphaPay auto-renewal is not enabled by default.",
    }
    monkeypatch.setattr(
        "pkm_brain.extraction.resolver_precheck_conflict_judgment",
        lambda *_args, **_kwargs: {
            "decision": "no_conflict",
            "counterpart_fact_ids": [],
            "rationale": "The scopes differ.",
        },
    )

    decision = earned_fact_decision(svc.paths, candidate)

    assert decision["decision"] == "apply"
    assert decision["target_fact_ids"] == []
    assert (
        decision["evidence"]["resolver_precheck"]["resolver_judgment"]["decision"]
        == "no_conflict"
    )


def test_conflict_prompt_requires_direct_irreconcilability() -> None:
    prompt = conflict_precheck_prompt(
        {"statement": "Candidate fact."},
        [{"id": "fact_existing", "statement": "Existing fact."}],
    )

    assert "cannot both be true under the same entity, topic, time, and scope" in prompt
    assert "context is insufficient to prove a direct contradiction" in prompt
    assert "require external truth" not in prompt


def test_reconcile_fact_conflict_reviews_releases_complementary_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        resolution = resolve_entity(conn, "Alex", type_hint="person")
        assert resolution is not None
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, entity_id, page_hint, section_hint,
              source_ids, observed_at, confidence, status, metadata, created_at,
              truth_confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fact_alex_vesting",
                "Alex's next vesting date is in June.",
                "people:alex:career",
                resolution.entity_id,
                "people/alex.md",
                "Career",
                "[]",
                "2026-07-10T00:00:00+00:00",
                0.9,
                "active",
                "{}",
                "2026-07-10T00:00:00+00:00",
                0.9,
            ),
        )
    candidate = {
        "statement": "Alex prefers product leadership roles at growth-stage companies.",
        "entity_id": resolution.entity_id,
        "entity_key": "people:alex:career",
        "page_hint": "people/alex.md",
        "section_hint": "Career",
        "source_ids": ["chunk:new"],
        "source_spans": [{"chunk_id": "new", "start": 0, "end": 40}],
        "evidence_quote": "Alex prefers product leadership roles.",
        "truth_confidence": 0.9,
        "routing_confidence": 0.9,
        "metadata": {"routing": {"route_destination_valid": True}},
    }
    action = propose_action(
        svc.paths,
        "fact_upsert",
        action_payload={"fact": candidate},
        action_features={
            "clean_fact_upsert": False,
            "residue_kind": "fact_conflict_review",
            "resolver_precheck": "residue",
        },
        target_fact_ids=["fact_alex_vesting"],
        target_page_paths=["people/alex.md"],
        proposed_by="extractor",
        decide=False,
    )
    mark_action_residue(
        svc.paths,
        action["id"],
        kind="fact_conflict_review",
        reason="Nearby facts are already contested.",
    )

    preview = reconcile_fact_conflict_reviews(svc.paths)

    assert preview["release_without_resolver"] == 1
    assert preview["resolver_review_required"] == 0

    def fake_decide_policy_actions(
        paths: BrainPaths,
        action_ids: list[str],
        *,
        critic_review: dict[str, object],
    ) -> list[dict[str, object]]:
        assert critic_review["disagreement_mode"] == "reject"
        with connection(paths.sqlite_path) as conn:
            conn.execute(
                """
                UPDATE cos_actions
                SET status = 'auto_applied', policy_decision = 'test_policy'
                WHERE id = ?
                """,
                (action_ids[0],),
            )
        return [get_action(paths, action_ids[0])]

    monkeypatch.setattr(
        "pkm_brain.extraction.decide_policy_actions", fake_decide_policy_actions
    )
    result = reconcile_fact_conflict_reviews(
        svc.paths,
        dry_run=False,
        critic_review={
            "max_workers": 1,
            "timeout_seconds": 30,
            "disagreement_mode": "reject",
        },
    )

    assert result["released"] == 1
    assert result["retained"] == 0
    with connection(svc.paths.sqlite_path) as conn:
        question = conn.execute(
            "SELECT status, decided_by FROM open_questions WHERE action_id = ?",
            (action["id"],),
        ).fetchone()
        action_row = conn.execute(
            """
            SELECT status, target_fact_ids, action_features, risk_tier
            FROM cos_actions WHERE id = ?
            """,
            (action["id"],),
        ).fetchone()
    assert question["status"] == "auto_resolved"
    assert question["decided_by"] == "conflict_reconciliation_v2"
    assert action_row["status"] == "auto_applied"
    assert json.loads(action_row["target_fact_ids"]) == []
    assert action_row["risk_tier"] == "medium"
    features = json.loads(action_row["action_features"])
    assert features["clean_fact_upsert"] is True
    assert features["residue_kind"] is None
    assert features["resolver_precheck"] == "passed"


def test_reconcile_fact_conflict_reviews_retains_resolver_confirmed_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        resolution = resolve_entity(conn, "AlphaPay", type_hint="product")
        assert resolution is not None
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, entity_id, page_hint, section_hint,
              source_ids, observed_at, confidence, status, metadata, created_at,
              truth_confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fact_alphapay_enabled_reconcile",
                "AlphaPay auto-renewal is enabled by default for annual plans.",
                "concepts:alphapay:summary",
                resolution.entity_id,
                "concepts/alphapay.md",
                "Summary",
                "[]",
                "2026-07-10T00:00:00+00:00",
                0.9,
                "active",
                "{}",
                "2026-07-10T00:00:00+00:00",
                0.9,
            ),
        )
    candidate = {
        "statement": "AlphaPay auto-renewal is not enabled by default for annual plans.",
        "entity_id": resolution.entity_id,
        "entity_key": "concepts:alphapay:summary",
        "page_hint": "concepts/alphapay.md",
        "section_hint": "Summary",
        "source_ids": ["chunk:new"],
        "source_spans": [{"chunk_id": "new", "start": 0, "end": 70}],
        "evidence_quote": "AlphaPay auto-renewal is not enabled by default.",
        "truth_confidence": 0.95,
        "routing_confidence": 0.95,
        "metadata": {"routing": {"route_destination_valid": True}},
    }
    action = propose_action(
        svc.paths,
        "fact_upsert",
        action_payload={"fact": candidate},
        action_features={"resolver_precheck": "residue"},
        target_fact_ids=["fact_alphapay_enabled_reconcile"],
        target_page_paths=["concepts/alphapay.md"],
        proposed_by="extractor",
        decide=False,
    )
    mark_action_residue(
        svc.paths,
        action["id"],
        kind="fact_conflict_review",
        reason="Candidate appears to contradict an existing fact.",
    )
    monkeypatch.setattr(
        "pkm_brain.extraction.resolver_precheck_conflict_judgment",
        lambda *_args, **_kwargs: {
            "decision": "conflict",
            "counterpart_fact_ids": ["fact_alphapay_enabled_reconcile"],
            "rationale": "The same setting cannot be both enabled and disabled.",
        },
    )

    result = reconcile_fact_conflict_reviews(svc.paths, dry_run=False)

    assert result["released"] == 0
    assert result["retained"] == 1
    with connection(svc.paths.sqlite_path) as conn:
        question = conn.execute(
            "SELECT status, fact_ids, options FROM open_questions WHERE action_id = ?",
            (action["id"],),
        ).fetchone()
        action_row = conn.execute(
            "SELECT target_fact_ids, evidence_json FROM cos_actions WHERE id = ?",
            (action["id"],),
        ).fetchone()
    assert question["status"] == "needs_human"
    assert json.loads(question["fact_ids"]) == ["fact_alphapay_enabled_reconcile"]
    assert len(json.loads(question["options"])) == 2
    assert json.loads(action_row["target_fact_ids"]) == [
        "fact_alphapay_enabled_reconcile"
    ]
    assert (
        json.loads(action_row["evidence_json"])["resolver_precheck"][
            "resolver_judgment"
        ]["decision"]
        == "conflict"
    )


def test_relation_classifier_suppresses_both_true_conflict_residue(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    enable_simple_autonomy(svc.paths)
    with connection(svc.paths.sqlite_path) as conn:
        resolution = resolve_entity(conn, "Alex", type_hint="person")
        assert resolution is not None
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, entity_id, page_hint, section_hint,
              source_ids, observed_at, confidence, status, metadata, created_at,
              truth_confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fact_alex_interview_count",
                "Alex had one interview scheduled for the role.",
                "people:alex:career",
                resolution.entity_id,
                "people/alex.md",
                "Career",
                json.dumps(["document:alex-old"]),
                "2026-06-26T00:00:00+00:00",
                0.9,
                "active",
                "{}",
                "2026-06-26T00:00:00+00:00",
                0.9,
            ),
        )
    marker = "Alex now has two interviews scheduled for the role."
    note = svc.paths.inbox / "source.md"
    note.write_text(f"# Source\n\n{marker}", encoding="utf-8")
    svc.ingest()
    provider = MarkerExtractorProvider(
        marker=marker,
        statement=marker,
        page_hint="people/alex.md",
        entity_mentions=[
            {
                "surface": "Alex",
                "type": "person",
                "mention_kind": "named",
                "is_primary": True,
            }
        ],
    )

    result = extract_recent_documents(svc.paths, shadow=False, llm_provider=provider)

    assert result["status"] == "ok"
    assert result["actions"][0]["status"] == "auto_applied"
    with connection(svc.paths.sqlite_path) as conn:
        residues = conn.execute("SELECT COUNT(*) FROM open_questions").fetchone()[0]
        facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    assert residues == 0
    assert facts == 2


def test_backfill_fact_conflict_review_questions_repairs_thin_residue(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        resolution = resolve_entity(conn, "AlphaPay", type_hint="product")
        assert resolution is not None
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, entity_id, page_hint, section_hint,
              source_ids, observed_at, confidence, status, metadata, created_at,
              truth_confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "fact_alphapay_enabled",
                "AlphaPay auto-renewal is enabled by default for annual plans.",
                "concepts:alphapay:summary",
                resolution.entity_id,
                "concepts/alphapay.md",
                "Summary",
                json.dumps(["document:alphapay-old"]),
                "2026-06-26T00:00:00+00:00",
                0.9,
                "active",
                "{}",
                "2026-06-26T00:00:00+00:00",
                0.9,
            ),
        )
    candidate = {
        "statement": "AlphaPay auto-renewal is not enabled by default for annual plans.",
        "entity_key": "concepts:alphapay:summary",
        "entity_mention": "AlphaPay",
        "entity_type": "product",
        "page_hint": "concepts/alphapay.md",
        "section_hint": "Summary",
        "source_ids": ["chunk:alphapay-new"],
        "source_spans": [{"chunk_id": "chunk_alphapay_new", "start": 0, "end": 70}],
        "evidence_quote": "AlphaPay auto-renewal is not enabled by default for annual plans.",
        "confidence": 0.95,
        "truth_confidence": 0.95,
    }
    action = propose_action(
        svc.paths,
        "fact_upsert",
        action_payload={"fact": candidate},
        target_page_paths=["concepts/alphapay.md"],
        proposed_by="extractor",
        decide=False,
    )
    mark_action_residue(
        svc.paths,
        action["id"],
        kind="fact_conflict_review",
        reason="Candidate appears to contradict an existing nearby fact.",
        policy_decision="earned_residue",
    )
    with connection(svc.paths.sqlite_path) as conn:
        before = conn.execute(
            "SELECT fact_ids, options FROM open_questions WHERE action_id = ?",
            (action["id"],),
        ).fetchone()
    assert json.loads(before["fact_ids"]) == []
    assert json.loads(before["options"]) == []

    result = backfill_fact_conflict_review_questions(svc.paths)

    assert result["updated"] == 1
    with connection(svc.paths.sqlite_path) as conn:
        question = conn.execute(
            "SELECT fact_ids, options FROM open_questions WHERE action_id = ?",
            (action["id"],),
        ).fetchone()
        updated_action = conn.execute(
            "SELECT target_fact_ids, evidence_json FROM cos_actions WHERE id = ?",
            (action["id"],),
        ).fetchone()
    assert json.loads(question["fact_ids"]) == ["fact_alphapay_enabled"]
    assert json.loads(updated_action["target_fact_ids"]) == ["fact_alphapay_enabled"]
    assert json.loads(updated_action["evidence_json"])["resolver_precheck"][
        "counterpart_fact_ids"
    ] == ["fact_alphapay_enabled"]
    options = json.loads(question["options"])
    assert options[0]["option_type"] == "candidate_fact"
    assert options[1]["fact_id"] == "fact_alphapay_enabled"


def test_extraction_promoted_clean_fact_requires_labeled_eval_gate(
    tmp_path: Path,
) -> None:
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


def test_extraction_promoted_clean_fact_requires_critic_after_labeled_eval_report(
    tmp_path: Path,
) -> None:
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
    assert result["actions"][0]["status"] == "needs_human"
    assert result["actions"][0]["policy_decision"] == "matched"
    assert result["actions"][0]["autonomy_level"] == "L2"
    assert result["actions"][0]["critic_decision"] == "unavailable"
    with connection(svc.paths.sqlite_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0
        residue = conn.execute(
            "SELECT kind, question FROM open_questions WHERE action_id = ?",
            (result["actions"][0]["id"],),
        ).fetchone()
    assert residue["kind"] == "policy_escalation"
    assert "critic did not agree" in residue["question"]


def test_extraction_simple_autonomy_auto_applies_clean_routed_fact(
    tmp_path: Path,
) -> None:
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
        residue_count = conn.execute("SELECT COUNT(*) FROM open_questions").fetchone()[
            0
        ]
    assert fact["statement"] == "Simple autonomy marker is present."
    assert fact["page_hint"] == "concepts/simple-autonomy.md"
    assert residue_count == 0


def test_extraction_simple_autonomy_sends_fallback_route_to_residue(
    tmp_path: Path,
) -> None:
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


def test_sampled_audit_unscoped_bad_reverts_without_demoting_policy(
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
    assert result["unscoped_bad_action_ids"] == [action["id"]]
    assert result["demoted_policy_version"] is None
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
                "policy_audit_unrelated",
                99,
                2,
                '["synthesize_page"]',
                "{}",
                "L2",
                1,
                0,
                None,
                0.25,
                0.4,
                "[]",
                1,
                "2026-06-25T00:00:00+00:00",
            ),
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
    with connection(svc.paths.sqlite_path) as conn:
        levels = {
            row["match_action_types"]: (
                row["autonomy_level"],
                bool(row["critic_required"]),
                float(row["audit_sample_rate"]),
            )
            for row in conn.execute(
                """
                SELECT match_action_types, autonomy_level, critic_required,
                       audit_sample_rate
                FROM cos_policy
                WHERE version = 100 AND priority IN (1, 2)
                """
            )
        }
    assert levels['["canonicalize_page"]'] == ("L3", False, 1.0)
    assert levels['["synthesize_page"]'] == ("L2", True, 0.25)


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
    assert decided["evidence_json"]["critic_review"] == {
        "critic_by": "fake:fake-critic-model",
        "decision": "agree",
        "rationale": "test critic judgment",
    }


def test_critic_repairs_incomplete_fact_citation_before_apply(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    note = svc.paths.inbox / "source.md"
    note.write_text(
        "# Source\n\n"
        "Speaker 1: The product is available in Europe. "
        "It is also available in Canada.",
        encoding="utf-8",
    )
    svc.ingest()
    with connection(svc.paths.sqlite_path) as conn:
        chunk = conn.execute("SELECT id, text FROM chunks LIMIT 1").fetchone()
    current_unit_ids = evidence_unit_ids_containing(chunk["text"], "Europe")
    additional_unit_ids = evidence_unit_ids_containing(chunk["text"], "Canada")
    repaired_unit_ids = [
        *current_unit_ids,
        *additional_unit_ids,
    ]
    candidate = validate_extracted_facts(
        svc.paths,
        [
            {
                "statement": "The product is available in Europe and Canada.",
                "chunk_id": chunk["id"],
                "evidence_unit_ids": current_unit_ids,
                "claim_class": "factual_update",
                "page_hint": "concepts/product-availability.md",
                "section_hint": "Summary",
            }
        ],
    )[0]
    insert_critic_policy(
        svc.paths,
        "policy_test_fact_evidence_repair",
        "L2",
        "needs_fact_evidence_repair",
        action_type="fact_upsert",
    )
    action = propose_action(
        svc.paths,
        "fact_upsert",
        action_payload={"fact": candidate},
        action_features={"needs_fact_evidence_repair": True},
        target_page_paths=[candidate["page_hint"]],
    )
    provider = RepairingCriticProvider(additional_unit_ids)

    decided = decide_action(
        svc.paths,
        action["id"],
        critic_llm_provider=provider,
        critic_disagreement_mode="reject",
    )

    assert provider.calls == 2
    assert decided["status"] == "applied"
    assert decided["critic_decision"] == "agree"
    repaired_fact = decided["evidence_json"]["payload"]["fact"]
    assert repaired_fact["evidence_unit_ids"] == repaired_unit_ids
    assert "Canada" in repaired_fact["evidence_quote"]
    repair_record = decided["evidence_json"]["critic_evidence_repair"]
    assert repair_record["repair"]["status"] == "repaired"
    assert repair_record["initial_review"]["decision"] == "evidence_incomplete"
    assert repair_record["final_review"]["decision"] == "agree"


def test_critic_llm_disagreement_blocks_l2_apply(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    insert_critic_policy(
        svc.paths, "policy_test_l2_critic_llm", "L2", "needs_l2_critic"
    )
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


def test_fact_apply_falls_back_when_entity_llm_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    calls: list[bool] = []

    def fake_apply_action(
        paths: BrainPaths,
        action_id: str,
        *,
        applied_status: str,
        allow_llm_entity_resolution: bool = True,
    ) -> dict[str, object]:
        assert paths == svc.paths
        assert applied_status == "applied"
        calls.append(allow_llm_entity_resolution)
        if allow_llm_entity_resolution:
            raise LLMProviderError("malformed entity disambiguation response")
        return {"id": action_id, "status": applied_status}

    monkeypatch.setattr("pkm_brain.cos_actions.apply_action", fake_apply_action)

    result = apply_decided_action(
        svc.paths,
        "cosact_test",
        applied_status="applied",
        action={"action_type": "fact_upsert"},
    )

    assert result["status"] == "applied"
    assert calls == [True, False]


def test_critic_disagreement_can_reject_without_human_residue(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    insert_critic_policy(
        svc.paths, "policy_test_l2_critic_reject", "L2", "needs_l2_critic"
    )
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
        critic_disagreement_mode="reject",
    )

    assert decided["status"] == "rejected"
    assert decided["autonomy_level"] == "L2"
    assert decided["critic_decision"] == "disagree"
    assert decided["evidence_json"]["rejection"]["reason"] == "critic did not agree"
    assert decided["evidence_json"]["critic_review"]["decision"] == "disagree"
    with connection(svc.paths.sqlite_path) as conn:
        residue_count = conn.execute(
            "SELECT COUNT(*) FROM open_questions WHERE action_id = ?",
            (action["id"],),
        ).fetchone()[0]
    assert residue_count == 0


def test_critic_timeout_rejects_in_rebuild_mode(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    insert_critic_policy(
        svc.paths, "policy_test_l2_critic_timeout", "L2", "needs_l2_critic"
    )
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
        critic_llm_provider=FailingCriticProvider(),
        critic_timeout_seconds=1,
        critic_disagreement_mode="reject",
    )

    assert decided["status"] == "rejected"
    assert decided["critic_decision"] == "unavailable"
    assert "timed out" in decided["evidence_json"]["critic_review"]["rationale"]
    assert decided["evidence_json"]["rejection"]["reason"] == "critic did not agree"
    with connection(svc.paths.sqlite_path) as conn:
        residue_count = conn.execute(
            "SELECT COUNT(*) FROM open_questions WHERE action_id = ?",
            (action["id"],),
        ).fetchone()[0]
    assert residue_count == 0


def test_decide_policy_actions_parallel_preserves_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    calls: list[str] = []

    def fake_decide_action(
        paths: BrainPaths, action_id: str, **kwargs: object
    ) -> dict[str, object]:
        assert paths == svc.paths
        assert kwargs["critic_disagreement_mode"] == "reject"
        time.sleep(0.05)
        calls.append(action_id)
        return {"id": action_id}

    monkeypatch.setattr("pkm_brain.extraction.decide_action", fake_decide_action)

    started = time.perf_counter()
    decided = decide_policy_actions(
        svc.paths,
        ["action_1", "action_2", "action_3", "action_4"],
        critic_review={
            "max_workers": 4,
            "timeout_seconds": 1,
            "disagreement_mode": "reject",
        },
    )

    assert time.perf_counter() - started < 0.15
    assert [action["id"] for action in decided] == [
        "action_1",
        "action_2",
        "action_3",
        "action_4",
    ]
    assert sorted(calls) == ["action_1", "action_2", "action_3", "action_4"]


def test_decide_policy_actions_isolates_worker_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    actions = [
        propose_action(
            svc.paths,
            "canonicalize_page",
            action_payload={"page_hint": f"concepts/test-{index}.md"},
        )
        for index in range(2)
    ]

    def fake_decide_action(
        paths: BrainPaths, action_id: str, **kwargs: object
    ) -> dict[str, object]:
        assert paths == svc.paths
        if action_id == actions[0]["id"]:
            raise RuntimeError("isolated decision failure")
        return {"id": action_id, "status": "applied"}

    monkeypatch.setattr("pkm_brain.extraction.decide_action", fake_decide_action)

    decided = decide_policy_actions(
        svc.paths,
        [action["id"] for action in actions],
        critic_review={
            "max_workers": 2,
            "timeout_seconds": 1,
            "disagreement_mode": "reject",
        },
    )

    assert [action["id"] for action in decided] == [
        action["id"] for action in actions
    ]
    assert decided[0]["status"] == "failed"
    assert (
        decided[0]["evidence_json"]["decision_failure"]["message"]
        == "isolated decision failure"
    )
    assert decided[1]["status"] == "applied"


def test_critic_block_rate_anomaly_creates_one_document_residue(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO documents(
              id, source_type, title, source_path, raw_path, content_hash,
              created_at, ingested_at, tags, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "doc_blocked",
                "hyprnote_meeting",
                "Blocked extraction doc",
                "raw/blocked.md",
                "raw/blocked.md",
                "hash_blocked",
                "2026-06-25T00:00:00+00:00",
                "2026-06-25T00:00:00+00:00",
                "[]",
                "active",
            ),
        )
    actions = []
    for index, critic_decision in enumerate(
        ["disagree", "disagree", "disagree", "disagree", "agree"]
    ):
        action = propose_action(
            svc.paths,
            "fact_upsert",
            action_payload={
                "fact": {
                    "id": f"fact_blocked_{index}",
                    "statement": f"Blocked fact {index}.",
                    "page_hint": "concepts/test.md",
                    "metadata": {"document_id": "doc_blocked"},
                    "confidence": 0.9,
                }
            },
        )
        with connection(svc.paths.sqlite_path) as conn:
            conn.execute(
                "UPDATE cos_actions SET critic_decision = ? WHERE id = ?",
                (critic_decision, action["id"]),
            )
        actions.append(get_action(svc.paths, action["id"]))

    record_critic_block_rate_anomalies(
        svc.paths,
        actions,
        critic_review={"block_rate_anomaly_threshold": 0.75},
    )
    record_critic_block_rate_anomalies(
        svc.paths,
        actions,
        critic_review={"block_rate_anomaly_threshold": 0.75},
    )

    with connection(svc.paths.sqlite_path) as conn:
        residues = list(
            conn.execute(
                """
                SELECT *
                FROM open_questions
                WHERE kind = 'document_extraction_anomaly'
                """
            )
        )
    assert len(residues) == 1
    assert residues[0]["status"] == "needs_human"
    assert "4/5 extracted facts" in residues[0]["question"]
    assert json.loads(residues[0]["context"])["document_id"] == "doc_blocked"


def test_critic_block_rate_anomaly_ignores_three_fact_sample(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    actions = []
    for index in range(3):
        action = propose_action(
            svc.paths,
            "fact_upsert",
            action_payload={
                "fact": {
                    "statement": f"Tiny sample fact {index}.",
                    "page_hint": "concepts/test.md",
                    "metadata": {"document_id": "doc_tiny_sample"},
                }
            },
        )
        with connection(svc.paths.sqlite_path) as conn:
            conn.execute(
                "UPDATE cos_actions SET critic_decision = 'disagree' WHERE id = ?",
                (action["id"],),
            )
        actions.append(get_action(svc.paths, action["id"]))

    record_critic_block_rate_anomalies(
        svc.paths,
        actions,
        critic_review={"block_rate_anomaly_threshold": 0.8},
    )

    with connection(svc.paths.sqlite_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM open_questions "
            "WHERE kind = 'document_extraction_anomaly'"
        ).fetchone()[0]
    assert count == 0


def test_reclaim_unrouted_facts_dry_run_routes_against_current_page_pool(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO wiki_pages(
              id, title, page_type, status, path, source_ids, related, tags,
              created_at, updated_at, managed, fact_ids
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "page_northwind",
                "Northwind",
                "organization",
                "active",
                str(svc.paths.wiki / "companies/northwind.md"),
                "[]",
                "[]",
                "[]",
                "2026-07-06T00:00:00+00:00",
                "2026-07-06T00:00:00+00:00",
                1,
                "[]",
            ),
        )
    action = propose_action(
        svc.paths,
        "fact_upsert",
        action_payload={
            "fact": {
                "statement": "Northwind builds agent customer-service software.",
                "entity_key": "concepts:concepts-extracted-facts:summary",
                "page_hint": "concepts/extracted-facts.md",
                "section_hint": "Summary",
                "source_ids": ["chunk:chunk_test"],
                "source_spans": [{"chunk_id": "chunk_test", "start": 0, "end": 40}],
                "evidence_quote": "Northwind builds agent customer-service software.",
                "confidence": 0.5,
                "truth_confidence": 0.5,
                "metadata": {
                    "routing": {
                        "original_page_hint": "concepts/extracted-facts.md",
                        "normalized_page_hint": "concepts/extracted-facts.md",
                        "route_destination_valid": False,
                        "route_resolution": "held_for_routing_review",
                        "route_review_reason": "fallback_page",
                    }
                },
            }
        },
        target_page_paths=["concepts/extracted-facts.md"],
        proposed_by="test",
        decide=False,
    )
    mark_action_residue(
        svc.paths,
        action["id"],
        kind="unrouted_fact",
        reason="Extractor routed the candidate to the fallback page.",
    )

    result = reclaim_unrouted_facts(svc.paths, dry_run=True)

    assert result["reclaimable"] == 1
    assert result["preview"][0]["old_action_id"] == action["id"]
    assert result["preview"][0]["new_page_hint"] == "companies/northwind.md"


def test_missing_critic_provider_blocks_required_critic_auto_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PKM_BRAIN_LLM_PROVIDER", raising=False)
    for suffix in ("PROVIDER", "MODEL", "MODEL_FALLBACKS", "BASE_URL"):
        monkeypatch.delenv(role_env("critic", suffix), raising=False)
    svc = service_for(tmp_path)
    svc.init_workspace()
    insert_critic_policy(
        svc.paths, "policy_test_l1_critic_missing", "L1", "needs_critic"
    )
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
        assert "directly entailed by the cited evidence" in prompt
        assert "should be human-reviewed" not in prompt
        return json.dumps(
            {"decision": self.decision, "rationale": "test critic judgment"}
        )


class RepairingCriticProvider:
    name = "fake"
    model = "fake-critic-model"

    def __init__(self, repaired_unit_ids: list[str]) -> None:
        self.repaired_unit_ids = repaired_unit_ids
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        assert "repairable_units" in prompt
        assert "Speaker 1" in prompt
        if self.calls == 1:
            return json.dumps(
                {
                    "decision": "evidence_incomplete",
                    "rationale": "The Canada sentence must be included.",
                    "repaired_evidence_unit_ids": self.repaired_unit_ids,
                }
            )
        assert "Canada" in prompt
        return json.dumps(
            {
                "decision": "agree",
                "rationale": "The repaired citation directly supports both locations.",
            }
        )


class FailingCriticProvider:
    name = "fake"
    model = "fake-critic-model"
    timeout = 30

    def complete(self, prompt: str) -> str:
        assert "Review this Chief-of-Staff action" in prompt
        assert self.timeout == 1
        raise LLMProviderError("Codex timed out after 1 seconds")


def test_critic_prompt_for_fact_upsert_is_narrow_entailment_review() -> None:
    prompt = critic_prompt(
        {
            "id": "cosact_test",
            "action_type": "fact_upsert",
            "risk_tier": "medium",
            "confidence": 0.9,
            "action_features": {"clean_fact_upsert": True},
            "target_fact_ids": [],
            "target_page_paths": [],
            "target_contract_ids": [],
            "evidence_json": {
                "payload": {
                    "fact": {
                        "statement": "Unity Catalog governs tables.",
                        "evidence_quote": "Unity Catalog governs tables.",
                    }
                }
            },
            "proposed_by": "test",
        },
        PolicyDecision(
            policy_id="policy_test",
            policy_version=1,
            policy_decision="matched",
            autonomy_level="L2",
            critic_required=True,
            reason="test policy",
        ),
    )

    assert "directly entailed by the cited evidence" in prompt
    assert "even if the fact is mundane" in prompt
    assert "should be human-reviewed" not in prompt


def first_evidence_ref_from_prompt(
    prompt: str, marker: str | None = None
) -> tuple[str, list[str]]:
    source_window = json.loads(prompt.rsplit("Source window JSON:", 1)[-1])
    for chunk in source_window["window"]["chunks"]:
        units = chunk.get("units") or []
        for unit in units:
            if marker is None or marker in unit["text"]:
                return chunk["chunk_id"], [unit["unit_id"]]
    return "missing", ["u0"]


class FakeExtractorProvider:
    name = "fake-extractor"
    model = "fake-extractor-model"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        assert "Extract atomic source-backed facts" in prompt
        chunk_id, unit_ids = first_evidence_ref_from_prompt(
            prompt, "Watermarked extraction marker"
        )
        return json.dumps(
            {
                "facts": [
                    {
                        "statement": "Watermarked extraction marker is present.",
                        "chunk_id": chunk_id,
                        "evidence_unit_ids": unit_ids,
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
                            "evidence_unit_ids": ["u0"],
                            "claim_class": "factual_update",
                        }
                    ]
                }
            )
        assert (
            "previous extractor response did not pass deterministic validation"
            in prompt
        )
        chunk_id, unit_ids = first_evidence_ref_from_prompt(
            prompt, "Retry extraction marker"
        )
        return json.dumps(
            {
                "facts": [
                    {
                        "statement": "Retry extraction marker is present.",
                        "chunk_id": chunk_id,
                        "evidence_unit_ids": unit_ids,
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
                        "evidence_unit_ids": ["u0"],
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
        chunk_id, unit_ids = first_evidence_ref_from_prompt(
            prompt, "The meeting started at 10:00"
        )
        return json.dumps(
            {
                "facts": [
                    {
                        "statement": "The meeting started at 10:00.",
                        "chunk_id": chunk_id,
                        "evidence_unit_ids": unit_ids,
                        "claim_class": "event_metadata",
                    }
                ]
            }
        )


class UnsupportedNumberExtractorProvider:
    name = "fake-extractor"
    model = "fake-extractor-model"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        chunk_id, unit_ids = first_evidence_ref_from_prompt(prompt, "sixty seven")
        return json.dumps(
            {
                "facts": [
                    {
                        "statement": "Alex expects probably sixty hours a week of work.",
                        "chunk_id": chunk_id,
                        "evidence_unit_ids": unit_ids,
                        "claim_class": "factual_update",
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
        entity_mentions: list[dict[str, object]] | None = None,
    ) -> None:
        self.marker = marker
        self.statement = statement
        self.page_hint = page_hint
        self.entity_mentions = entity_mentions
        self.calls = 0
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.calls += 1
        self.prompts.append(prompt)
        source_window = json.loads(prompt.rsplit("Source window JSON:", 1)[-1])
        for chunk in source_window["window"]["chunks"]:
            unit_ids = [
                unit["unit_id"]
                for unit in chunk.get("units") or []
                if self.marker in unit["text"]
            ]
            if unit_ids:
                return json.dumps(
                    {
                        "facts": [
                            {
                                "statement": self.statement,
                                "chunk_id": chunk["chunk_id"],
                                "evidence_unit_ids": unit_ids[:1],
                                "claim_class": "factual_update",
                                "page_hint": self.page_hint,
                                "section_hint": "Summary",
                                "entity_key": self.page_hint.removesuffix(
                                    ".md"
                                ).replace("/", ":"),
                                "extraction_confidence": 0.99,
                                "routing_confidence": 0.8,
                                "truth_confidence": 0.95,
                                **(
                                    {"entity_mentions": self.entity_mentions}
                                    if self.entity_mentions is not None
                                    else {}
                                ),
                            }
                        ]
                    }
                )
        return json.dumps({"facts": []})


class ConcurrentWindowExtractorProvider:
    name = "fake-extractor"
    model = "fake-extractor-model"

    def __init__(self, *, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def complete(self, prompt: str) -> str:
        with self.lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay_seconds)
            source_window = json.loads(prompt.rsplit("Source window JSON:", 1)[-1])
            chunk = source_window["window"]["chunks"][0]
            unit = chunk["units"][0]
            quote = str(unit["text"]).strip()
            return json.dumps(
                {
                    "facts": [
                        {
                            "statement": f"{quote} is present.",
                            "chunk_id": chunk["chunk_id"],
                            "evidence_unit_ids": [unit["unit_id"]],
                            "claim_class": "factual_update",
                            "page_hint": "concepts/parallel.md",
                            "section_hint": "Summary",
                            "entity_key": "parallel",
                        }
                    ]
                }
            )
        finally:
            with self.lock:
                self.active -= 1


def enable_simple_autonomy(paths: BrainPaths) -> None:
    paths.config_local.mkdir(parents=True, exist_ok=True)
    (paths.config_local / "cos_llm.yaml").write_text(
        "extraction:\n  simple_autonomy:\n    enabled: true\n",
        encoding="utf-8",
    )


def apply_status(paths: BrainPaths, action_id: str) -> str:
    with connection(paths.sqlite_path) as conn:
        return str(
            conn.execute(
                "SELECT status FROM cos_actions WHERE id = ?", (action_id,)
            ).fetchone()["status"]
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


def insert_test_contract(
    paths: BrainPaths,
    contract_id: str,
    page_hint: str,
    *,
    canonical_entity: str = "Test Contract",
    page_scope: str = "Facts about the test page.",
    retrieval_purpose: str = "Answer test questions.",
) -> None:
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
                canonical_entity,
                page_scope,
                retrieval_purpose,
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


def insert_test_wiki_page(
    paths: BrainPaths,
    page_id: str,
    page_hint: str,
    *,
    title: str,
    page_type: str,
    managed: bool,
) -> None:
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO wiki_pages(
              id, title, page_type, status, path, source_ids, related, tags,
              created_at, updated_at, managed, fact_ids
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                page_id,
                title,
                page_type,
                "active",
                page_hint,
                "[]",
                "[]",
                "[]",
                "2026-06-26T00:00:00+00:00",
                "2026-06-26T00:00:00+00:00",
                1 if managed else 0,
                "[]",
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


def insert_critic_policy(
    paths: BrainPaths,
    policy_id: str,
    autonomy_level: str,
    feature_name: str,
    *,
    action_type: str = "canonicalize_page",
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
                199,
                1,
                json.dumps([action_type]),
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
