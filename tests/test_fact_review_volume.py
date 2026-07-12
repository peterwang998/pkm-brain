from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from pkm_brain.cli import app
from pkm_brain.db import connection, dumps
from pkm_brain.fact_review_volume import (
    reconcile_backlog_w2a_apply,
    reconcile_backlog_w2a_dry_run,
    reconcile_backlog_w2b_apply,
    reconcile_backlog_w2b_dry_run,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.util import now_iso


runner = CliRunner()


def test_w2b_dry_run_reports_synthesize_and_unrouted_without_writes(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    created_at = now_iso()
    with connection(paths.sqlite_path) as conn:
        insert_action(
            conn,
            "action_synthesis",
            "synthesize_page",
            "needs_human",
            target_page_paths=["projects/alpha.md"],
            action_features={"risk_tier": "low", "affected_fact_count": 4},
            evidence_json={"payload": {"page_hint": "projects/alpha.md"}},
        )
        insert_question(
            conn,
            "question_synthesis",
            "policy_escalation",
            "matched policy policy_v1_low_l1_critic",
            created_at,
            action_id="action_synthesis",
            page_hint="projects/alpha.md",
        )
        insert_action(
            conn,
            "action_unrouted",
            "fact_upsert",
            "needs_human",
            evidence_json={
                "payload": {
                    "fact": {
                        "statement": "Northwind is the customer for the pilot.",
                        "entity_key": "company:northwind",
                        "page_hint": "concepts/extracted-facts.md",
                        "metadata": {
                            "routing": {
                                "snapped_page_hint": "companies/northwind.md",
                            }
                        },
                    }
                }
            },
        )
        insert_question(
            conn,
            "question_unrouted",
            "unrouted_fact",
            "Extractor routed the candidate to the fallback page.",
            created_at,
            action_id="action_unrouted",
            page_hint="concepts/extracted-facts.md",
        )

    report = reconcile_backlog_w2b_dry_run(paths, sample_limit=5)

    assert report["status"] == "dry_run"
    assert report["acceptance_boundary"]["apply_supported_by_this_command"] is True
    assert report["synthesize_page"]["linked_policy_escalation_question_count"] == 1
    assert report["synthesize_page"]["affected_action_ids"] == ["action_synthesis"]
    assert report["unrouted_inbox_batching"]["candidate_question_count"] == 1
    assert report["unrouted_inbox_batching"]["groups"][0]["page_hint"] == "companies/northwind.md"
    assert report["human_readable_escalation_reasons"]["opaque_reason_count"] == 1
    assert report["projected_after"]["question_count_removed"] == 2
    assert report["projected_after"]["new_weekly_batch_question_count"] == 1
    assert report["projected_after"]["net_question_delta"] == -1
    with connection(paths.sqlite_path) as conn:
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute("SELECT id, status FROM open_questions ORDER BY id")
        }
    assert statuses == {
        "question_synthesis": "needs_human",
        "question_unrouted": "needs_human",
    }


def test_w2b_apply_drains_synthesize_and_batches_unrouted(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    created_at = now_iso()
    with connection(paths.sqlite_path) as conn:
        insert_action(
            conn,
            "action_synthesis",
            "synthesize_page",
            "needs_human",
            target_page_paths=["projects/alpha.md"],
            action_features={"risk_tier": "low", "affected_fact_count": 4, "eval_gate": {"passed": False}},
            evidence_json={
                "payload": {
                    "synthesis": {
                        "page_hint": "projects/alpha.md",
                        "synthesis_markdown": "- Alpha synthesis [fact_alpha].",
                        "fact_ids": ["fact_alpha"],
                    }
                }
            },
        )
        insert_question(
            conn,
            "question_synthesis",
            "policy_escalation",
            "matched policy policy_v1_low_l1_critic",
            created_at,
            action_id="action_synthesis",
            page_hint="projects/alpha.md",
        )
        insert_action(
            conn,
            "action_unrouted",
            "fact_upsert",
            "needs_human",
            evidence_json={
                "payload": {
                    "fact": {
                        "id": "fact_northwind_unrouted",
                        "statement": "Northwind is the customer for the pilot.",
                        "entity_key": "company:northwind",
                        "page_hint": "concepts/extracted-facts.md",
                        "source_ids": ["document:northwind"],
                        "metadata": {
                            "routing": {
                                "snapped_page_hint": "companies/northwind.md",
                            }
                        },
                    }
                }
            },
        )
        insert_question(
            conn,
            "question_unrouted",
            "unrouted_fact",
            "Extractor routed the candidate to the fallback page.",
            created_at,
            action_id="action_unrouted",
            page_hint="concepts/extracted-facts.md",
        )

    result = reconcile_backlog_w2b_apply(paths, sample_limit=5)

    assert result["status"] == "ok"
    assert result["synthesize_page"]["applied_count"] == 1
    assert result["unrouted_inbox_batching"]["applied_count"] == 1
    assert result["unrouted_inbox_batching"]["batch_question_count"] == 1
    with connection(paths.sqlite_path) as conn:
        questions = {
            row["id"]: row["status"]
            for row in conn.execute("SELECT id, status FROM open_questions ORDER BY id")
        }
        synth = conn.execute(
            "SELECT page_hint, synthesis_markdown FROM wiki_page_syntheses"
        ).fetchone()
        fact = conn.execute(
            "SELECT page_hint, section_hint FROM facts WHERE id = 'fact_northwind_unrouted'"
        ).fetchone()
        old_action = conn.execute(
            "SELECT status FROM cos_actions WHERE id = 'action_unrouted'"
        ).fetchone()
    assert questions["question_synthesis"] == "auto_resolved"
    assert questions["question_unrouted"] == "auto_resolved"
    assert any(question_id.startswith("question_") and status == "needs_human" for question_id, status in questions.items())
    assert synth["page_hint"] == "projects/alpha.md"
    assert fact["page_hint"] == "companies/northwind.md"
    assert fact["section_hint"] == "Inbox"
    assert old_action["status"] == "rejected"


def test_reconcile_backlog_cli_applies_when_requested(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    result = runner.invoke(
        app,
        ["cos", "reconcile-backlog", "--home", str(paths.home), "--apply"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "ok"


def test_w2a_dry_run_classifies_conflicts_and_policy_escalations(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    created_at = now_iso()
    with connection(paths.sqlite_path) as conn:
        insert_fact(
            conn,
            "fact_existing",
            "AlphaPay auto-renewal is enabled by default for annual plans.",
            page_hint="concepts/alphapay.md",
        )
        insert_action(
            conn,
            "action_conflict",
            "fact_upsert",
            "needs_human",
            target_fact_ids=["fact_existing"],
            evidence_json={
                "payload": {
                    "fact": {
                        "statement": "AlphaPay auto-renewal is not enabled by default for annual plans.",
                        "entity_key": "concepts:alphapay:summary",
                        "page_hint": "concepts/alphapay.md",
                    }
                },
                "resolver_precheck": {"counterpart_fact_ids": ["fact_existing"]},
            },
        )
        insert_question(
            conn,
            "question_conflict",
            "fact_conflict_review",
            "Relation classifier says candidate contradicts existing nearby fact(s).",
            created_at,
            action_id="action_conflict",
            page_hint="concepts/alphapay.md",
            fact_ids=["fact_existing"],
        )
        insert_action(
            conn,
            "action_policy",
            "fact_upsert",
            "needs_human",
            evidence_json={
                "payload": {
                    "fact": {
                        "statement": "A clean fact has no counterpart.",
                        "page_hint": "concepts/clean.md",
                    }
                }
            },
        )
        insert_question(
            conn,
            "question_policy",
            "policy_escalation",
            "Fact upsert needs review.",
            created_at,
            action_id="action_policy",
            page_hint="concepts/clean.md",
        )

    report = reconcile_backlog_w2a_dry_run(paths, sample_limit=5)

    assert report["status"] == "dry_run"
    assert report["acceptance_boundary"]["apply_supported_by_this_command"] is True
    assert report["candidate_count"] == 2
    assert report["by_relation"]["contradicts"] == 1
    assert report["by_relation"]["unrelated"] == 1
    assert report["survivor_count"] == 1
    assert report["auto_resolvable_count"] == 1


def test_reconcile_backlog_cli_blocks_w2a_apply(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    result = runner.invoke(
        app,
        ["cos", "reconcile-backlog", "--scope", "w2a", "--home", str(paths.home), "--apply"],
    )

    assert result.exit_code == 1
    assert "--approved-by-peter" in result.stdout


def test_w2a_apply_uses_reversible_mechanics_and_keeps_conflicts_open(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    created_at = now_iso()
    with connection(paths.sqlite_path) as conn:
        insert_fact(
            conn,
            "fact_existing",
            "AlphaPay uses Stripe Checkout for renewal invoices.",
            page_hint="projects/alphapay.md",
            source_ids=["document:existing"],
        )
        insert_action(
            conn,
            "action_duplicate",
            "fact_upsert",
            "needs_human",
            target_fact_ids=["fact_existing"],
            evidence_json={
                "payload": {
                    "fact": {
                        "id": "fact_duplicate_candidate",
                        "statement": "AlphaPay uses Stripe Checkout for renewal invoices.",
                        "entity_key": "project:alphapay:summary",
                        "page_hint": "projects/alphapay.md",
                        "source_ids": ["document:new-support"],
                    }
                },
                "resolver_precheck": {"counterpart_fact_ids": ["fact_existing"]},
            },
        )
        insert_question(
            conn,
            "question_duplicate",
            "policy_escalation",
            "Duplicate candidate needs review.",
            created_at,
            action_id="action_duplicate",
            page_hint="projects/alphapay.md",
            fact_ids=["fact_existing"],
        )
        insert_action(
            conn,
            "action_clean",
            "fact_upsert",
            "needs_human",
            evidence_json={
                "payload": {
                    "fact": {
                        "id": "fact_clean",
                        "statement": "A clean source-backed fact has no counterpart.",
                        "entity_key": "concept:clean:summary",
                        "page_hint": "concepts/clean.md",
                        "source_ids": ["document:clean"],
                    }
                }
            },
        )
        insert_question(
            conn,
            "question_clean",
            "policy_escalation",
            "Clean fact needs review.",
            created_at,
            action_id="action_clean",
            page_hint="concepts/clean.md",
        )
        insert_action(
            conn,
            "action_conflict",
            "fact_upsert",
            "needs_human",
            target_fact_ids=["fact_existing"],
            evidence_json={
                "payload": {
                    "fact": {
                        "id": "fact_conflicting_candidate",
                        "statement": "AlphaPay does not use Stripe Checkout for renewal invoices.",
                        "entity_key": "project:alphapay:summary",
                        "page_hint": "projects/alphapay.md",
                    }
                },
                "resolver_precheck": {"counterpart_fact_ids": ["fact_existing"]},
            },
        )
        insert_question(
            conn,
            "question_conflict",
            "fact_conflict_review",
            "Potential contradiction.",
            created_at,
            action_id="action_conflict",
            page_hint="projects/alphapay.md",
            fact_ids=["fact_existing"],
        )

    result = reconcile_backlog_w2a_apply(
        paths,
        approved_by="Peter",
        sample_limit=10,
    )

    assert result["status"] == "ok"
    assert result["applied_count"] == 2
    assert result["survivor_count"] == 1
    assert result["applied_by_relation"] == {"supports": 1, "unrelated": 1}
    assert result["mechanical_audit"]["pass_rate"] == 1.0
    with connection(paths.sqlite_path) as conn:
        questions = {
            row["id"]: (row["status"], row["decided_by"])
            for row in conn.execute(
                "SELECT id, status, decided_by FROM open_questions ORDER BY id"
            )
        }
        actions = {
            row["id"]: row["status"]
            for row in conn.execute("SELECT id, status FROM cos_actions ORDER BY id")
        }
        existing = conn.execute(
            "SELECT statement, source_ids FROM facts WHERE id = 'fact_existing'"
        ).fetchone()
        clean = conn.execute("SELECT id FROM facts WHERE id = 'fact_clean'").fetchone()
        duplicate_candidate = conn.execute(
            "SELECT id FROM facts WHERE id = 'fact_duplicate_candidate'"
        ).fetchone()
        action_count = conn.execute("SELECT COUNT(*) FROM cos_actions").fetchone()[0]
    assert questions["question_duplicate"] == ("auto_resolved", "w2a_reconcile")
    assert questions["question_clean"] == ("auto_resolved", "w2a_reconcile")
    assert questions["question_conflict"] == ("needs_human", None)
    assert actions["action_duplicate"] == "rejected"
    assert actions["action_clean"] == "auto_applied"
    assert actions["action_conflict"] == "needs_human"
    assert existing["statement"] == "AlphaPay uses Stripe Checkout for renewal invoices."
    assert json.loads(existing["source_ids"]) == ["document:existing", "document:new-support"]
    assert clean is not None
    assert duplicate_candidate is None

    rerun = reconcile_backlog_w2a_apply(
        paths,
        approved_by="Peter",
        sample_limit=10,
    )

    assert rerun["applied_count"] == 0
    assert rerun["survivor_count"] == 1
    with connection(paths.sqlite_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cos_actions").fetchone()[0] == action_count


def test_reconcile_backlog_cli_applies_w2a_only_with_approval_flag(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    result = runner.invoke(
        app,
        [
            "cos",
            "reconcile-backlog",
            "--scope",
            "w2a",
            "--home",
            str(paths.home),
            "--apply",
            "--approved-by-peter",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "ok"


def test_w2a_apply_disables_llm_entity_disambiguation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    resolution_paths: list[object] = []

    def fake_resolve_entity(conn, mention, *args, **kwargs):
        resolution_paths.append(kwargs.get("paths"))
        return None

    monkeypatch.setattr("pkm_brain.cos_actions.resolve_entity", fake_resolve_entity)
    with connection(paths.sqlite_path) as conn:
        insert_action(
            conn,
            "action_clean",
            "fact_upsert",
            "needs_human",
            evidence_json={
                "payload": {
                    "fact": {
                        "id": "fact_clean",
                        "statement": "A deterministic fact has no counterpart.",
                        "entity_mention": "Ambiguous Entity",
                        "entity_key": "concept:ambiguous:summary",
                        "page_hint": "concepts/ambiguous.md",
                    }
                }
            },
        )
        insert_question(
            conn,
            "question_clean",
            "policy_escalation",
            "Clean fact needs review.",
            now_iso(),
            action_id="action_clean",
            page_hint="concepts/ambiguous.md",
        )

    result = reconcile_backlog_w2a_apply(paths, approved_by="Peter")

    assert result["applied_count"] == 1
    assert resolution_paths == [None]


def test_reconcile_backlog_cli_writes_dry_run_report(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    output = tmp_path / "w2b-report.json"

    result = runner.invoke(
        app,
        [
            "cos",
            "reconcile-backlog",
            "--home",
            str(paths.home),
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["scope"] == "w2b"
    assert written["report_path"] == str(output)


def insert_action(
    conn,
    action_id: str,
    action_type: str,
    status: str,
    *,
    target_fact_ids: list[str] | None = None,
    target_page_paths: list[str] | None = None,
    action_features: dict | None = None,
    evidence_json: dict | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO cos_actions(
          id, action_type, status, target_fact_ids, target_page_paths,
          target_contract_ids, action_features, inverse_action_json,
          evidence_json, audit_status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            action_id,
            action_type,
            status,
            dumps(target_fact_ids or []),
            dumps(target_page_paths or []),
            "[]",
            dumps(action_features or {}),
            "{}",
            dumps(evidence_json or {}),
            "unaudited",
            now_iso(),
        ),
    )


def insert_question(
    conn,
    question_id: str,
    kind: str,
    question: str,
    created_at: str,
    *,
    action_id: str | None,
    page_hint: str | None,
    fact_ids: list[str] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO open_questions(
          id, kind, entity_key, page_hint, fact_ids, question, options, status,
          context, created_at, action_id, recommended_action
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            question_id,
            kind,
            None,
            page_hint,
            dumps(fact_ids or []),
            question,
            "[]",
            "needs_human",
            "{}",
            created_at,
            action_id,
            "{}",
        ),
    )


def insert_fact(
    conn,
    fact_id: str,
    statement: str,
    *,
    page_hint: str,
    source_ids: list[str] | None = None,
) -> None:
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
            page_hint.removesuffix(".md").replace("/", ":"),
            page_hint,
            "Summary",
            dumps(source_ids or []),
            now_iso(),
            0.9,
            "active",
            "{}",
            now_iso(),
            0.9,
        ),
    )
