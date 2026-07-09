from __future__ import annotations

import json
from pathlib import Path

from pkm_brain.cos_actions import apply_action
from pkm_brain.cos_policy import promote_policy_for_autonomy
from pkm_brain.db import connection
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.synthesizer import generate_page_syntheses
from pkm_brain.wiki_facts import active_facts_by_page, active_page_synthesis


def service_for(tmp_path: Path) -> BrainService:
    return BrainService(BrainPaths.from_value(tmp_path / "brain"))


def insert_fact(
    paths: BrainPaths,
    fact_id: str,
    statement: str,
    *,
    page_hint: str = "concepts/synthesis-test.md",
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
                "concepts:synthesis-test:summary",
                page_hint,
                "Summary",
                json.dumps([f"document:{fact_id}"]),
                "2026-06-26T00:00:00+00:00",
                0.9,
                "active",
                "{}",
                "2026-06-26T00:00:00+00:00",
                0.9,
            ),
        )


class FakeSynthesizerProvider:
    name = "fake"
    model = "fake-synth-model"

    def complete(self, prompt: str) -> str:
        assert "non-canonical wiki synthesis" in prompt
        return json.dumps(
            {
                "syntheses": [
                    {
                        "page_hint": "concepts/synthesis-test.md",
                        "synthesis_markdown": "- The synthesis test page has supported active facts [fact_synth_a].",
                        "fact_ids": ["fact_synth_a"],
                    }
                ]
            }
        )


def test_page_synthesizer_skips_without_provider(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    insert_fact(svc.paths, "fact_synth_a", "The synthesis test page has supported active facts.")

    result = generate_page_syntheses(svc.paths)

    assert result["status"] == "skipped"
    assert result["reason"] == "No CoS LLM provider configured for synthesizer role"
    assert result["actions"] == []


def test_page_synthesizer_generates_cited_shadow_candidate(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    insert_fact(svc.paths, "fact_synth_a", "The synthesis test page has supported active facts.")

    result = generate_page_syntheses(svc.paths, llm_provider=FakeSynthesizerProvider())

    assert result["status"] == "ok"
    assert result["shadow"] is True
    assert result["candidate_count"] == 1
    candidate = result["candidates"][0]
    assert candidate["fact_ids"] == ["fact_synth_a"]
    assert candidate["model"] == "fake-synth-model"
    assert candidate["fact_hash"]
    assert result["actions"] == []


def test_page_synthesizer_proposes_reversible_action(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        promote_policy_for_autonomy(conn, reason="test synthesis L2 policy")
    insert_fact(svc.paths, "fact_synth_a", "The synthesis test page has supported active facts.")

    result = generate_page_syntheses(
        svc.paths,
        shadow=False,
        llm_provider=FakeSynthesizerProvider(),
    )

    assert result["candidate_count"] == 1
    action = result["actions"][0]
    assert action["action_type"] == "synthesize_page"
    assert action["status"] == "applied"
    assert action["autonomy_level"] == "L2"
    assert action["critic_by"] is None
    applied = apply_action(svc.paths, action["id"])
    facts = active_facts_by_page(svc.paths, ["concepts/synthesis-test.md"])["concepts/synthesis-test.md"]
    synthesis = active_page_synthesis(svc.paths, "concepts/synthesis-test.md", facts)
    assert applied["inverse_action_json"]["delete_synthesis_ids"]
    assert synthesis is not None
    assert synthesis["fact_ids"] == ["fact_synth_a"]
    assert "fact_synth_a" in synthesis["synthesis_markdown"]
