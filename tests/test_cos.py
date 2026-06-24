from __future__ import annotations

from pathlib import Path

from pkm_brain.cos_actions import apply_action, decide_action, propose_action, revert_action
from pkm_brain.cos_policy import evaluate_policy
from pkm_brain.db import connection
from pkm_brain.extraction import extract_recent_documents
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
