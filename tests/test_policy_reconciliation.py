from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from typer.testing import CliRunner

from pkm_brain.cli import app
from pkm_brain.cos_actions import decide_action, propose_action
from pkm_brain.cos_policy import (
    PolicyDecision,
    evaluate_policy,
    promote_policy_for_autonomy,
)
from pkm_brain.db import connection
from pkm_brain.extraction import validate_extracted_facts
from pkm_brain.paths import BrainPaths
from pkm_brain.policy_reconciliation import (
    reconcile_policy_escalations,
    redecide_policy_actions,
)
from pkm_brain.service import BrainService
from pkm_brain.source_evidence import evidence_units_for_text


class AgreeCriticProvider:
    name = "fake-critic"
    model = "fake-model"

    def complete(self, prompt: str) -> str:
        assert "Review this Chief-of-Staff action" in prompt
        return json.dumps(
            {
                "decision": "agree",
                "rationale": "The statement is directly supported by the payload evidence.",
            }
        )


class EvidenceRepairCriticProvider:
    name = "fake-critic"
    model = "fake-model"

    def __init__(self, repaired_unit_ids: list[str]) -> None:
        self.repaired_unit_ids = repaired_unit_ids
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        assert "repairable_units" in prompt
        if self.calls == 1:
            return json.dumps(
                {
                    "decision": "evidence_incomplete",
                    "rationale": "The Canada sentence must be added to the citation.",
                    "repaired_evidence_unit_ids": self.repaired_unit_ids,
                }
            )
        assert self.calls == 2
        assert "critic_evidence_repaired" in prompt
        return json.dumps(
            {
                "decision": "agree",
                "rationale": "The repaired citation directly supports both locations.",
            }
        )


def test_policy_reconciliation_redecides_only_currently_eligible_actions(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    eligible = propose_clean_fact(paths, "fact_eligible")
    retained = propose_action(
        paths,
        "fact_upsert",
        action_payload={
            "fact": {
                "id": "fact_retained",
                "statement": "An unsupported fact remains review-only.",
                "entity_key": "concept:test:summary",
                "page_hint": "concepts/test.md",
            }
        },
        action_features={
            "risk_tier": "high",
            "quote_backed": False,
            "fallback_route": False,
        },
        risk_tier="high",
    )
    decide_action(paths, eligible["id"])
    decide_action(paths, retained["id"])
    with connection(paths.sqlite_path) as conn:
        promote_policy_for_autonomy(
            conn,
            reason="test lenient policy repair",
            strictness="lenient",
            minimum_auto_confidence=0.6,
        )

    preview = reconcile_policy_escalations(paths)

    assert preview["status"] == "dry_run"
    assert preview["candidate_action_count"] == 2
    assert preview["eligible_action_count"] == 1
    assert preview["retained_l3_action_count"] == 1

    result = reconcile_policy_escalations(
        paths,
        dry_run=False,
        critic_review={"max_workers": 2, "disagreement_mode": "reject"},
        critic_llm_provider=AgreeCriticProvider(),
    )

    assert result["resolved_action_count"] == 1
    assert result["result_status_counts"] == {"applied": 1}
    assert result["failure_count"] == 0
    with connection(paths.sqlite_path) as conn:
        statuses = {
            row["action_id"]: row["status"]
            for row in conn.execute(
                "SELECT action_id, status FROM open_questions WHERE kind = 'policy_escalation'"
            )
        }
        applied_fact = conn.execute(
            "SELECT id FROM facts WHERE id = 'fact_eligible'"
        ).fetchone()
    assert statuses[eligible["id"]] == "auto_resolved"
    assert statuses[retained["id"]] == "needs_human"
    assert applied_fact is not None


def test_policy_reconciliation_cli_defaults_to_preview(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    result = CliRunner().invoke(
        app,
        ["cos", "reconcile-policy-escalations", "--home", str(paths.home)],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "dry_run"


def test_policy_reconciliation_retries_locked_ledger_write_without_new_critic(
    tmp_path: Path, monkeypatch
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    calls = 0

    def flaky_decide_action(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return {"id": "action_retry", "status": "applied"}

    monkeypatch.setattr(
        "pkm_brain.policy_reconciliation.decide_action", flaky_decide_action
    )
    monkeypatch.setattr("pkm_brain.db.SQLITE_LOCK_RETRY_DELAYS_SECONDS", (0.0,))
    monkeypatch.setattr("pkm_brain.db.time.sleep", lambda _delay: None)
    eligible = [
        {
            "action": {"id": "action_retry", "action_type": "synthesize_page"},
            "decision": PolicyDecision(
                policy_id="policy_test",
                policy_version=2,
                policy_decision="matched",
                autonomy_level="L2",
                critic_required=False,
            ),
        }
    ]

    decided, failures = redecide_policy_actions(
        paths,
        eligible,
        critic_review={"max_workers": 4, "disagreement_mode": "reject"},
        critic_llm_provider=None,
    )

    assert calls == 2
    assert decided["action_retry"]["status"] == "applied"
    assert failures == []


def test_redecide_policy_actions_repairs_structured_incomplete_fact_evidence(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = BrainService(paths)
    service.init_workspace()
    note = paths.inbox / "source.md"
    note.write_text(
        "# Source\n\n"
        "Speaker 1: The product is available in Europe. "
        "It is also available in Canada.",
        encoding="utf-8",
    )
    service.ingest()
    with connection(paths.sqlite_path) as conn:
        chunk = conn.execute("SELECT id, text FROM chunks LIMIT 1").fetchone()

    evidence_units = evidence_units_for_text(str(chunk["text"]))
    current_unit_ids = [
        str(unit["unit_id"]) for unit in evidence_units if "Europe" in str(unit["text"])
    ]
    additional_unit_ids = [
        str(unit["unit_id"]) for unit in evidence_units if "Canada" in str(unit["text"])
    ]
    candidate = validate_extracted_facts(
        paths,
        [
            {
                "statement": "The product is available in Europe and Canada.",
                "chunk_id": chunk["id"],
                "evidence_unit_ids": current_unit_ids,
                "claim_class": "factual_update",
                "page_hint": "concepts/product-availability.md",
                "section_hint": "Summary",
                "extraction_confidence": 0.95,
                "routing_confidence": 0.95,
                "truth_confidence": 0.95,
            }
        ],
        extractor_model="fake-extractor-model",
    )[0]
    action = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": candidate},
        action_features={
            "risk_tier": "medium",
            "clean_fact_upsert": True,
            "fact_upsert_resolution": "new_clean_fact",
            "quote_backed": True,
            "fallback_route": False,
            "resolver_precheck": "passed",
        },
        target_page_paths=[candidate["page_hint"]],
        confidence=0.95,
        risk_tier="medium",
    )
    with connection(paths.sqlite_path) as conn:
        promote_policy_for_autonomy(
            conn,
            reason="test structured critic evidence repair",
            strictness="lenient",
            minimum_auto_confidence=0.6,
        )
        decision = evaluate_policy(
            conn, action["action_type"], action["action_features"]
        )
    assert decision.autonomy_level == "L2"
    assert decision.critic_required is True

    provider = EvidenceRepairCriticProvider(additional_unit_ids)
    decided, failures = redecide_policy_actions(
        paths,
        [{"action": action, "decision": decision}],
        critic_review={"max_workers": 1, "disagreement_mode": "reject"},
        critic_llm_provider=provider,
    )

    assert failures == []
    assert provider.calls == 2
    result = decided[action["id"]]
    assert result["status"] == "applied"
    assert result["critic_decision"] == "agree"
    repaired_fact = result["evidence_json"]["payload"]["fact"]
    assert repaired_fact["evidence_unit_ids"] == [
        *current_unit_ids,
        *additional_unit_ids,
    ]
    assert "Canada" in repaired_fact["evidence_quote"]
    repair_record = result["evidence_json"]["critic_evidence_repair"]
    assert repair_record["initial_review"]["decision"] == "evidence_incomplete"
    assert repair_record["initial_review"]["repaired_evidence_unit_ids"] == (
        additional_unit_ids
    )
    assert repair_record["repair"]["status"] == "repaired"
    assert repair_record["final_review"]["decision"] == "agree"


def propose_clean_fact(paths: BrainPaths, fact_id: str) -> dict[str, object]:
    return propose_action(
        paths,
        "fact_upsert",
        action_payload={
            "fact": {
                "id": fact_id,
                "statement": "A directly quoted clean fact is supported.",
                "entity_key": "concept:test:summary",
                "page_hint": "concepts/test.md",
                "confidence": 0.95,
                "source_ids": ["document:test"],
            }
        },
        action_features={
            "risk_tier": "medium",
            "clean_fact_upsert": True,
            "fact_upsert_resolution": "new_clean_fact",
            "quote_backed": True,
            "fallback_route": False,
            "resolver_precheck": "passed",
        },
        target_page_paths=["concepts/test.md"],
        confidence=0.95,
        risk_tier="medium",
    )
