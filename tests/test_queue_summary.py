from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from pkm_brain.cli import app
from pkm_brain.db import connection
from pkm_brain.paths import BrainPaths
from pkm_brain.queue_summary import review_queue_summary
from pkm_brain.service import BrainService
from pkm_brain.util import now_iso


runner = CliRunner()


def test_review_queue_summary_counts_human_work_buckets(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    created_at = now_iso()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, entity_key, page_hint, fact_ids, question, options, status,
              context, created_at, action_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "question_conflict",
                "fact_conflict_review",
                None,
                None,
                "[]",
                "Which fact is correct?",
                "[]",
                "needs_human",
                "{}",
                created_at,
                None,
            ),
        )
        conn.execute(
            """
            INSERT INTO memories(
              id, memory_type, scope, content, confidence, source_ids, status,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "mem_proposed",
                "FactMemory",
                "project:pkm-brain",
                "Proposed memory.",
                0.8,
                "[]",
                "proposed",
                created_at,
                created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO cos_actions(
              id, action_type, status, target_fact_ids, target_page_paths,
              target_contract_ids, action_features, inverse_action_json,
              evidence_json, audit_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "action_entity_merge",
                "entity_merge",
                "proposed",
                "[]",
                "[]",
                "[]",
                "{}",
                "{}",
                "{}",
                "unaudited",
                created_at,
            ),
        )
        conn.execute(
            """
            INSERT INTO cos_actions(
              id, action_type, status, target_fact_ids, target_page_paths,
              target_contract_ids, action_features, inverse_action_json,
              evidence_json, audit_status, created_at, applied_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "action_sampled_bad",
                "fact_upsert",
                "applied",
                "[]",
                "[]",
                "[]",
                "{}",
                "{}",
                "{}",
                "sampled_bad",
                created_at,
                created_at,
            ),
        )

    summary = review_queue_summary(paths)
    cli_result = runner.invoke(app, ["cos", "queue-summary", "--home", str(paths.home)])

    assert summary["total"] == 4
    assert summary["by_kind"] == {
        "audit": 1,
        "conflicts": 1,
        "memories": 1,
        "topology": 1,
    }
    assert summary["raw"] == {
        "audit_flagged": 1,
        "fact_conflict_review": 1,
        "proposed_action": 1,
        "proposed_memory": 1,
    }
    assert summary["buckets"]["conflicts"]["oldest_age_hours"] is not None
    assert cli_result.exit_code == 0
    assert json.loads(cli_result.stdout)["by_kind"] == summary["by_kind"]
