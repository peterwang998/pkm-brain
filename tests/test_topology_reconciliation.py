from __future__ import annotations

import json
from pathlib import Path

from pkm_brain.curation_settings import update_curation_settings
from pkm_brain.db import connection
from pkm_brain.gardener import (
    merge_candidate,
    page_split_candidate,
    propose_gardener_action,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.topology_reconciliation import reconcile_topology_proposals
from pkm_brain.util import now_iso


class KeepTopologyProvider:
    name = "fake-gardener"
    model = "fake-model"

    def __init__(self, candidate_keys: list[str]) -> None:
        self.candidate_keys = candidate_keys

    def complete(self, _prompt: str) -> str:
        return json.dumps(
            {
                "judgments": [
                    {
                        "candidate_key": key,
                        "decision": "keep",
                        "rationale": "Current evidence still supports this candidate.",
                    }
                    for key in self.candidate_keys
                ]
            }
        )


def test_topology_reconciliation_rechecks_old_candidates_under_current_bias(
    tmp_path: Path,
) -> None:
    paths, candidates = topology_fixture(tmp_path)

    result = reconcile_topology_proposals(paths)

    assert result["status"] == "dry_run"
    assert result["settings"] == {
        "merge_aggressiveness": 0.8,
        "split_aggressiveness": 0.2,
    }
    assert result["open_unique_by_type"] == {"page_merge": 1, "page_split": 2}
    assert result["currently_admitted_by_type"] == {
        "page_merge": 1,
        "page_split": 1,
    }
    assert result["no_longer_admitted_by_type"] == {"page_split": 1}
    assert candidates["weak_split"]["candidate_key"] in {
        item["candidate_key"] for item in result["no_longer_admitted_examples"]
    }


def test_topology_reconciliation_rejects_stale_and_routes_survivors_to_policy(
    tmp_path: Path,
) -> None:
    paths, candidates = topology_fixture(tmp_path)
    provider = KeepTopologyProvider(
        [
            candidates["merge"]["candidate_key"],
            candidates["strong_split"]["candidate_key"],
        ]
    )

    result = reconcile_topology_proposals(
        paths,
        dry_run=False,
        critic_review={"max_workers": 2, "disagreement_mode": "reject"},
        gardener_llm_provider=provider,
    )

    assert result["failure_count"] == 0
    assert result["closed_group_counts"]["stale_page_split"] == 1
    assert result["remaining_open_by_type"] == {"page_merge": 1, "page_split": 1}
    with connection(paths.sqlite_path) as conn:
        actions = {
            row["candidate_key"]: row
            for row in conn.execute(
                """
                SELECT
                  json_extract(action_features, '$.candidate_key') AS candidate_key,
                  status, policy_version, autonomy_level, action_features
                FROM cos_actions
                WHERE id IN (?, ?, ?)
                """,
                (
                    candidates["weak_action_id"],
                    candidates["strong_action_id"],
                    candidates["merge_action_id"],
                ),
            )
        }
    assert actions[candidates["weak_split"]["candidate_key"]]["status"] == "rejected"
    for key in (
        candidates["strong_split"]["candidate_key"],
        candidates["merge"]["candidate_key"],
    ):
        action = actions[key]
        assert action["status"] == "needs_human"
        assert action["policy_version"] is not None
        assert action["autonomy_level"] == "L3"
        features = json.loads(action["action_features"])
        assert features["eval_gate"] == {"suite": "topology"}


def topology_fixture(tmp_path: Path) -> tuple[BrainPaths, dict[str, object]]:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    update_curation_settings(
        paths,
        "lenient",
        merge_aggressiveness=0.8,
        split_aggressiveness=0.2,
    )
    sections = ["Architecture", "Evaluation", "Operations"]
    with connection(paths.sqlite_path) as conn:
        for index in range(6):
            insert_fact(
                conn,
                f"fact_weak_{index}",
                page_hint="concepts/weak-split.md",
                section=sections[index % len(sections)],
                statement=f"Weak split detail {index}.",
                source_id=f"document:weak-{index}",
            )
        strong_sections = ["Alpha", "Beta", "Gamma", "Delta"]
        for index in range(12):
            insert_fact(
                conn,
                f"fact_strong_{index}",
                page_hint="concepts/strong-split.md",
                section=strong_sections[index % len(strong_sections)],
                statement=f"Strong split detail {index}.",
                source_id=f"document:strong-{index}",
            )
        insert_fact(
            conn,
            "fact_merge_left",
            page_hint="concepts/alpha-payment.md",
            section="Summary",
            statement="AlphaPay payment retries use Stripe Checkout.",
            source_id="document:alpha-shared",
        )
        insert_fact(
            conn,
            "fact_merge_right",
            page_hint="concepts/alpha-payments.md",
            section="Summary",
            statement="AlphaPay payment retries use Stripe Checkout.",
            source_id="document:alpha-shared",
        )

    weak_split = page_split_candidate(
        {
            "relative_path": "concepts/weak-split.md",
            "active_fact_count": 6,
            "section_counts": {section: 2 for section in sections},
        },
        {},
        split_aggressiveness=0.5,
    )
    strong_split = page_split_candidate(
        {
            "relative_path": "concepts/strong-split.md",
            "active_fact_count": 12,
            "section_counts": {
                "Alpha": 3,
                "Beta": 3,
                "Gamma": 3,
                "Delta": 3,
            },
        },
        {},
        split_aggressiveness=0.5,
    )
    merge = merge_candidate(
        {
            "relative_path": "concepts/alpha-payment.md",
            "active_fact_count": 1,
            "fact_tokens": ["alphapay", "payment", "retry", "stripe"],
            "entity_keys": ["product:alphapay:billing"],
            "source_ids": ["document:alpha-shared"],
        },
        {
            "relative_path": "concepts/alpha-payments.md",
            "active_fact_count": 1,
            "fact_tokens": ["alphapay", "payment", "retry", "stripe"],
            "entity_keys": ["product:alphapay:billing"],
            "source_ids": ["document:alpha-shared"],
        },
        {},
        merge_aggressiveness=0.5,
    )
    assert weak_split is not None
    assert strong_split is not None
    assert merge is not None
    weak_action = propose_gardener_action(paths, weak_split)
    strong_action = propose_gardener_action(paths, strong_split)
    merge_action = propose_gardener_action(paths, merge)
    return paths, {
        "weak_split": weak_split,
        "strong_split": strong_split,
        "merge": merge,
        "weak_action_id": weak_action["id"],
        "strong_action_id": strong_action["id"],
        "merge_action_id": merge_action["id"],
    }


def insert_fact(
    conn,
    fact_id: str,
    *,
    page_hint: str,
    section: str,
    statement: str,
    source_id: str,
) -> None:
    timestamp = now_iso()
    conn.execute(
        """
        INSERT INTO facts(
          id, statement, entity_key, page_hint, section_hint, source_ids,
          confidence, status, metadata, created_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', '{}', ?, ?)
        """,
        (
            fact_id,
            statement,
            f"concept:test:{section.lower()}",
            page_hint,
            section,
            dumps_json([source_id]),
            0.95,
            timestamp,
            timestamp,
        ),
    )


def dumps_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)
