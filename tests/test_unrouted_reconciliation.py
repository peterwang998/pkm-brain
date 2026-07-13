from __future__ import annotations

import json

from pkm_brain.cos_actions import apply_action, propose_action
from pkm_brain.cos_policy import promote_policy_for_autonomy
from pkm_brain.db import connection, dumps
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.unrouted_reconciliation import reconcile_unrouted_inbox_batches


class NetflixRouteResolver:
    name = "fake-route-resolver"
    model = "fake-route-model"

    def complete(self, prompt: str) -> str:
        return json.dumps(
            {
                "decisions": [
                    {
                        "candidate_index": 0,
                        "decision": "route_existing",
                        "page_hint": "open_loops/netflix-pm-interview-preparation.md",
                        "confidence": 0.96,
                        "rationale": "The fact and source concern Netflix interview preparation.",
                    }
                ]
            }
        )


def test_legacy_unrouted_batch_rehomes_fact_and_closes_batch(tmp_path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO wiki_pages(
              id, title, page_type, status, path, source_ids, related, tags,
              created_at, updated_at, managed, fact_ids
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "page_netflix_interview",
                "Netflix PM Interview Preparation",
                "open_loop",
                "active",
                str(paths.wiki / "open_loops/netflix-pm-interview-preparation.md"),
                "[]",
                "[]",
                "[]",
                "2026-07-01T00:00:00+00:00",
                "2026-07-01T00:00:00+00:00",
                1,
                "[]",
            ),
        )
        promote_policy_for_autonomy(
            conn,
            reason="test validated rehome",
            strictness="lenient",
            minimum_auto_confidence=0.6,
        )
    fact = {
        "id": "fact_netflix_interview",
        "statement": "Netflix PM interview preparation includes a recruiter screen.",
        "entity_key": "inbox:netflix",
        "page_hint": "inbox/concepts-concepts-extracted-facts-summary.md",
        "section_hint": "Inbox",
        "source_ids": ["chunk:chunk_netflix"],
        "source_spans": [{"chunk_id": "chunk_netflix", "start": 0, "end": 40}],
        "evidence_quote": "Netflix PM interview preparation includes a recruiter screen.",
        "confidence": 0.9,
        "truth_confidence": 0.9,
        "routing_confidence": 0.5,
        "extraction_confidence": 0.9,
        "metadata": {
            "routing": {
                "route_destination_valid": False,
                "route_resolution": "held_for_routing_review",
            }
        },
    }
    old_action = propose_action(
        paths,
        "fact_upsert",
        action_payload={"fact": fact},
        target_fact_ids=[fact["id"]],
        target_page_paths=[fact["page_hint"]],
        proposed_by="w2b_reconcile_unrouted_inbox",
        risk_tier="medium",
    )
    apply_action(paths, old_action["id"])
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO open_questions(
              id, kind, page_hint, fact_ids, question, options, status, context,
              recommended_action, risk_tier, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "question_netflix_batch",
                "unrouted_inbox_batch",
                fact["page_hint"],
                "[]",
                "One legacy batch fact needs routing.",
                "[]",
                "needs_human",
                dumps({"new_action_ids": [old_action["id"]]}),
                "{}",
                "low",
                "2026-07-01T00:00:00+00:00",
            ),
        )

    provider = NetflixRouteResolver()
    preview = reconcile_unrouted_inbox_batches(
        paths, dry_run=True, llm_provider=provider
    )
    result = reconcile_unrouted_inbox_batches(
        paths, dry_run=False, llm_provider=provider
    )

    assert preview["routable_count"] == 1
    assert preview["requires_human_count"] == 0
    assert result["applied_route_count"] == 1
    with connection(paths.sqlite_path) as conn:
        routed_fact = conn.execute(
            "SELECT page_hint, section_hint FROM facts WHERE id = ?", (fact["id"],)
        ).fetchone()
        question = conn.execute(
            "SELECT status, decided_by FROM open_questions WHERE id = ?",
            ("question_netflix_batch",),
        ).fetchone()
    assert routed_fact["page_hint"] == "open_loops/netflix-pm-interview-preparation.md"
    assert routed_fact["section_hint"] == "Inbox"
    assert question["status"] == "auto_resolved"
    assert question["decided_by"] == "unrouted_batch_reconciliation_v2"
