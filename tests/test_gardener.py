from __future__ import annotations

import json
from pathlib import Path

from pkm_brain.cos_actions import apply_action
from pkm_brain.db import connection
from pkm_brain.contracts import insert_contract_direct
from pkm_brain.gardener import (
    apply_gardener_judgment,
    generate_gardener_candidates,
    propose_gardener_action,
)
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


def service_for(tmp_path: Path) -> BrainService:
    return BrainService(BrainPaths.from_value(tmp_path / "brain"), prefer_model_embeddings=False)


def insert_fact(
    conn,
    fact_id: str,
    statement: str,
    *,
    page_hint: str,
    entity_key: str,
    section_hint: str = "Summary",
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
            entity_key,
            page_hint,
            section_hint,
            json.dumps(source_ids or []),
            "2026-06-24T00:00:00+00:00",
            0.85,
            "active",
            "{}",
            "2026-06-24T00:00:00+00:00",
            0.85,
        ),
    )


def insert_contract(
    conn,
    *,
    contract_id: str,
    page_hint: str,
    canonical_entity: str,
    what_belongs_here: str,
    what_does_not_belong_here: str,
) -> None:
    insert_contract_direct(
        conn,
        {
            "id": contract_id,
            "page_hint": page_hint,
            "canonical_entity": canonical_entity,
            "page_scope": f"Facts about {canonical_entity}.",
            "retrieval_purpose": f"Answer questions about {canonical_entity}.",
            "what_belongs_here": what_belongs_here,
            "what_does_not_belong_here": what_does_not_belong_here,
            "freshness_policy": "Refresh when facts change.",
            "related_pages": [],
            "version": 1,
            "status": "active",
        },
    )


def insert_entity(
    conn,
    entity_id: str,
    name: str,
    *,
    entity_type: str = "organization",
    aliases: list[str] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO entities(
          id, name, entity_type, aliases, status, source_ids, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity_id,
            name,
            entity_type,
            json.dumps(aliases or []),
            "active",
            "[]",
            "2026-06-24T00:00:00+00:00",
        ),
    )


def link_fact_to_entity(
    conn,
    fact_id: str,
    entity_id: str,
    *,
    mention_text: str,
) -> None:
    conn.execute("UPDATE facts SET entity_id = ? WHERE id = ?", (entity_id, fact_id))
    conn.execute(
        """
        INSERT INTO fact_entities(
          id, fact_id, entity_id, is_primary, mention_text, mention_kind,
          resolution_method, confidence, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"factentity_{fact_id}",
            fact_id,
            entity_id,
            1,
            mention_text,
            "named",
            "created",
            0.9,
            "2026-06-24T00:00:00+00:00",
        ),
    )


def test_gardener_merge_candidates_require_fact_overlap(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        insert_fact(
            conn,
            "fact_alpha_left",
            "AlphaPay payment retry uses Stripe Checkout for renewal invoices.",
            page_hint="concepts/alpha-payment.md",
            entity_key="product:alphapay:billing",
            source_ids=["document:alpha-billing"],
        )
        insert_fact(
            conn,
            "fact_alpha_right",
            "AlphaPay payment retry uses Stripe Checkout for renewal invoices.",
            page_hint="concepts/alpha-payments.md",
            entity_key="product:alphapay:billing",
            source_ids=["document:alpha-billing"],
        )
        insert_fact(
            conn,
            "fact_beta",
            "BetaLaunch regional pricing is tracked in the launch checklist.",
            page_hint="concepts/beta-payment.md",
            entity_key="product:betalaunch:pricing",
            source_ids=["document:beta-pricing"],
        )

    result = generate_gardener_candidates(svc.paths)
    merges = [candidate for candidate in result["candidates"] if candidate["action_type"] == "page_merge"]

    assert len(merges) == 1
    assert merges[0]["page_hints"] == [
        "concepts/alpha-payment.md",
        "concepts/alpha-payments.md",
    ]
    assert merges[0]["fact_token_overlap"] > 0.5
    assert merges[0]["shared_source_count"] == 1


def test_entity_gardener_proposes_and_applies_compact_duplicate_merge(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        insert_entity(conn, "entity_hightouch", "Hightouch")
        insert_entity(conn, "entity_high_touch", "High Touch")
        insert_fact(
            conn,
            "fact_hightouch",
            "Hightouch uses warehouse-native customer data activation.",
            page_hint="companies/hightouch.md",
            entity_key="companies:hightouch:summary",
            source_ids=["document:hightouch"],
        )
        link_fact_to_entity(
            conn,
            "fact_hightouch",
            "entity_hightouch",
            mention_text="Hightouch",
        )
        insert_fact(
            conn,
            "fact_high_touch",
            "High Touch uses warehouse-native customer data activation.",
            page_hint="companies/high-touch.md",
            entity_key="companies:high-touch:summary",
            source_ids=["document:hightouch"],
        )
        link_fact_to_entity(
            conn,
            "fact_high_touch",
            "entity_high_touch",
            mention_text="High Touch",
        )

    result = generate_gardener_candidates(svc.paths, shadow=False)
    candidate = next(
        candidate
        for candidate in result["candidates"]
        if candidate["action_type"] == "entity_merge"
    )
    action = next(action for action in result["actions"] if action["action_type"] == "entity_merge")

    assert candidate["canonical_entity_id"] == "entity_hightouch"
    assert candidate["merged_entity_ids"] == ["entity_high_touch"]
    assert candidate["merge_signal"] == "same_compact_name_or_alias"
    assert candidate["risk_tier"] == "low"
    assert action["target_fact_ids"] == ["fact_hightouch", "fact_high_touch"]
    assert action["action_features"]["merged_entity_count"] == 2
    assert action["evidence_json"]["payload"] == {
        "canonical_entity_id": "entity_hightouch",
        "merged_entity_ids": ["entity_high_touch"],
        "reason": "entity names differ only by spacing or punctuation",
        "candidate_key": "entity_merge:entities:entity_high_touch,entity_hightouch",
    }

    apply_action(svc.paths, action["id"])

    with connection(svc.paths.sqlite_path) as conn:
        source = conn.execute(
            "SELECT status, merged_into FROM entities WHERE id = 'entity_high_touch'"
        ).fetchone()
        fact = conn.execute(
            "SELECT entity_id FROM facts WHERE id = 'fact_high_touch'"
        ).fetchone()
        link = conn.execute(
            "SELECT entity_id FROM fact_entities WHERE fact_id = 'fact_high_touch'"
        ).fetchone()

    assert source["status"] == "merged"
    assert source["merged_into"] == "entity_hightouch"
    assert fact["entity_id"] == "entity_hightouch"
    assert link["entity_id"] == "entity_hightouch"


def test_entity_gardener_skips_known_type_mismatch(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        insert_entity(conn, "entity_jordan_person", "Jordan", entity_type="person")
        insert_entity(conn, "entity_jordan_org", "Jordan", entity_type="organization")
        insert_fact(
            conn,
            "fact_jordan_person",
            "Jordan runs weekly product reviews.",
            page_hint="people/jordan.md",
            entity_key="people:jordan:summary",
        )
        link_fact_to_entity(
            conn,
            "fact_jordan_person",
            "entity_jordan_person",
            mention_text="Jordan",
        )
        insert_fact(
            conn,
            "fact_jordan_org",
            "Jordan sells enterprise workflow software.",
            page_hint="companies/jordan.md",
            entity_key="companies:jordan:summary",
        )
        link_fact_to_entity(
            conn,
            "fact_jordan_org",
            "entity_jordan_org",
            mention_text="Jordan",
        )

    result = generate_gardener_candidates(svc.paths)

    assert not any(candidate["action_type"] == "entity_merge" for candidate in result["candidates"])


def test_entity_gardener_llm_can_drop_entity_merge_candidate(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        insert_entity(conn, "entity_sierra", "Sierra")
        insert_entity(conn, "entity_sierra_poc", "Sierra POC")
        insert_fact(
            conn,
            "fact_sierra",
            "Sierra is evaluating customer-facing AI agent workflows.",
            page_hint="companies/sierra.md",
            entity_key="companies:sierra:summary",
            source_ids=["document:sierra"],
        )
        link_fact_to_entity(conn, "fact_sierra", "entity_sierra", mention_text="Sierra")
        insert_fact(
            conn,
            "fact_sierra_poc",
            "Sierra POC planning tracks pilot scope and success criteria.",
            page_hint="projects/sierra-poc.md",
            entity_key="projects:sierra-poc:summary",
            source_ids=["document:sierra"],
        )
        link_fact_to_entity(
            conn,
            "fact_sierra_poc",
            "entity_sierra_poc",
            mention_text="Sierra POC",
        )

    first = generate_gardener_candidates(svc.paths)
    merge = next(candidate for candidate in first["candidates"] if candidate["action_type"] == "entity_merge")

    class FakeProvider:
        name = "fake"
        model = "test"

        def complete(self, prompt: str) -> str:
            assert "Candidates may be page topology changes or entity merges" in prompt
            assert "entity_sierra_poc" in prompt
            return json.dumps(
                {
                    "judgments": [
                        {
                            "candidate_key": merge["candidate_key"],
                            "decision": "drop",
                            "rationale": "The POC is a project, not the company identity.",
                        }
                    ]
                }
            )

    result = generate_gardener_candidates(svc.paths, llm_provider=FakeProvider())
    dropped = result["llm_judgment"]["dropped"]

    assert result["llm_judgment"]["dropped_candidate_count"] == 1
    assert dropped[0]["candidate_key"] == merge["candidate_key"]
    assert dropped[0]["action_type"] == "entity_merge"
    assert dropped[0]["entity_names"]["entity_sierra_poc"] == "Sierra POC"
    assert dropped[0]["llm_judgment"]["rationale"] == "The POC is a project, not the company identity."
    assert not any(candidate["candidate_key"] == merge["candidate_key"] for candidate in result["candidates"])


def test_gardener_no_provider_leaves_deterministic_candidates_unchanged(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        insert_fact(
            conn,
            "fact_alpha_left",
            "AlphaPay payment retry uses Stripe Checkout for renewal invoices.",
            page_hint="concepts/alpha-payment.md",
            entity_key="product:alphapay:billing",
            source_ids=["document:alpha-billing"],
        )
        insert_fact(
            conn,
            "fact_alpha_right",
            "AlphaPay payment retry uses Stripe Checkout for renewal invoices.",
            page_hint="concepts/alpha-payments.md",
            entity_key="product:alphapay:billing",
            source_ids=["document:alpha-billing"],
        )

    first = generate_gardener_candidates(svc.paths)
    second = generate_gardener_candidates(svc.paths, llm_provider=None, provider=None)

    assert first["llm_judgment"]["enabled"] is False
    assert [candidate["candidate_key"] for candidate in first["candidates"]] == [
        candidate["candidate_key"] for candidate in second["candidates"]
    ]


def test_gardener_llm_judgment_can_drop_candidate(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        insert_fact(
            conn,
            "fact_alpha_left",
            "AlphaPay payment retry uses Stripe Checkout for renewal invoices.",
            page_hint="concepts/alpha-payment.md",
            entity_key="product:alphapay:billing",
            source_ids=["document:alpha-billing"],
        )
        insert_fact(
            conn,
            "fact_alpha_right",
            "AlphaPay payment retry uses Stripe Checkout for renewal invoices.",
            page_hint="concepts/alpha-payments.md",
            entity_key="product:alphapay:billing",
            source_ids=["document:alpha-billing"],
        )

    class FakeProvider:
        name = "fake"
        model = "test"

        def complete(self, prompt: str) -> str:
            assert "Review deterministic PKM Brain gardener candidates" in prompt
            return json.dumps(
                {
                    "judgments": [
                        {
                            "candidate_key": "page_merge:concepts/alpha-payment.md,concepts/alpha-payments.md:",
                            "decision": "drop",
                            "rationale": "Duplicate-looking pages are intentionally separate in this fixture.",
                        }
                    ]
                }
            )

    result = generate_gardener_candidates(svc.paths, llm_provider=FakeProvider())
    dropped = result["llm_judgment"]["dropped"]

    assert result["llm_judgment"]["enabled"] is True
    assert result["llm_judgment"]["dropped_candidate_count"] == 1
    assert dropped[0]["candidate_key"] == "page_merge:concepts/alpha-payment.md,concepts/alpha-payments.md:"
    assert dropped[0]["action_type"] == "page_merge"
    assert dropped[0]["page_hints"] == [
        "concepts/alpha-payment.md",
        "concepts/alpha-payments.md",
    ]
    assert dropped[0]["reason"] == "near-duplicate page hints with overlapping fact evidence"
    assert dropped[0]["llm_judgment"] == {
        "candidate_key": "page_merge:concepts/alpha-payment.md,concepts/alpha-payments.md:",
        "decision": "drop",
        "rationale": "Duplicate-looking pages are intentionally separate in this fixture.",
    }
    assert not any(candidate["action_type"] == "page_merge" for candidate in result["candidates"])


def test_gardener_per_candidate_discrimination_drop_is_auditable() -> None:
    candidate = {
        "action_type": "entity_merge",
        "candidate_key": "entity_merge:entities:entity_mercury_bank,entity_mercury_planetarium",
        "entity_ids": ["entity_mercury_bank", "entity_mercury_planetarium"],
        "canonical_entity_id": "entity_mercury_bank",
        "merged_entity_ids": ["entity_mercury_planetarium"],
        "entity_names": {
            "entity_mercury_bank": "Mercury Bank",
            "entity_mercury_planetarium": "Mercury Planetarium",
        },
        "entity_types": {
            "entity_mercury_bank": "organization",
            "entity_mercury_planetarium": "organization",
        },
        "risk_tier": "medium",
        "score": 0.72,
        "similarity": 0.72,
        "merge_signal": "near_name_with_evidence_overlap",
        "reason": "similar entity names share incidental token evidence",
    }

    class FakeProvider:
        name = "fake"
        model = "test"

        def complete(self, prompt: str) -> str:
            assert "Mercury Bank" in prompt
            assert "Mercury Planetarium" in prompt
            return json.dumps(
                {
                    "judgments": [
                        {
                            "candidate_key": candidate["candidate_key"],
                            "decision": "drop",
                            "rationale": "The shared Mercury token is incidental; these are separate organizations.",
                        }
                    ]
                }
            )

    result = apply_gardener_judgment(
        [candidate],
        [],
        {},
        paths=None,
        llm_provider=FakeProvider(),
        provider=None,
    )

    assert result["candidates"] == []
    assert result["summary"]["dropped_candidate_count"] == 1
    assert result["summary"]["dropped"][0]["candidate_key"] == candidate["candidate_key"]
    assert result["summary"]["dropped"][0]["merge_signal"] == "near_name_with_evidence_overlap"
    assert (
        result["summary"]["dropped"][0]["llm_judgment"]["rationale"]
        == "The shared Mercury token is incidental; these are separate organizations."
    )


def test_gardener_llm_judgment_failure_is_candidate_local(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        for prefix, label in [("alpha", "AlphaPay"), ("beta", "BetaLaunch")]:
            insert_fact(
                conn,
                f"fact_{prefix}_left",
                f"{label} renewal billing uses Stripe Checkout for invoice recovery.",
                page_hint=f"concepts/{prefix}-payment.md",
                entity_key=f"product:{prefix}:billing",
                source_ids=[f"document:{prefix}-billing"],
            )
            insert_fact(
                conn,
                f"fact_{prefix}_right",
                f"{label} renewal billing uses Stripe Checkout for invoice recovery.",
                page_hint=f"concepts/{prefix}-payments.md",
                entity_key=f"product:{prefix}:billing",
                source_ids=[f"document:{prefix}-billing"],
            )

    first = generate_gardener_candidates(svc.paths, max_candidates=10)
    bad_key = first["candidates"][0]["candidate_key"]

    class FakeProvider:
        name = "fake"
        model = "test"

        def complete(self, prompt: str) -> str:
            if bad_key in prompt:
                raise TimeoutError("simulated gardener timeout")
            return json.dumps({"judgments": []})

    result = generate_gardener_candidates(svc.paths, max_candidates=10, llm_provider=FakeProvider())
    retained = {candidate["candidate_key"]: candidate for candidate in result["candidates"]}

    assert result["llm_judgment"]["mode"] == "per_candidate"
    assert result["llm_judgment"]["error_count"] == 1
    assert result["llm_judgment"]["timeout_count"] == 1
    assert result["llm_judgment"]["needs_review_count"] == 1
    assert retained[bad_key]["needs_review"] is True
    assert retained[bad_key]["risk_tier"] == "high"
    assert retained[bad_key]["llm_judgment"]["needs_review"] is True


def test_gardener_judges_more_candidates_than_output_cap(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        for prefix, label in [("alpha", "AlphaPay"), ("beta", "BetaLaunch")]:
            insert_fact(
                conn,
                f"fact_{prefix}_left",
                f"{label} renewal billing uses Stripe Checkout for invoice recovery.",
                page_hint=f"concepts/{prefix}-payment.md",
                entity_key=f"product:{prefix}:billing",
                source_ids=[f"document:{prefix}-billing"],
            )
            insert_fact(
                conn,
                f"fact_{prefix}_right",
                f"{label} renewal billing uses Stripe Checkout for invoice recovery.",
                page_hint=f"concepts/{prefix}-payments.md",
                entity_key=f"product:{prefix}:billing",
                source_ids=[f"document:{prefix}-billing"],
            )

    class FakeProvider:
        name = "fake"
        model = "test"

        def __init__(self) -> None:
            self.calls = 0

        def complete(self, prompt: str) -> str:
            assert "Review deterministic PKM Brain gardener candidates" in prompt
            self.calls += 1
            return json.dumps({"judgments": []})

    fake = FakeProvider()
    result = generate_gardener_candidates(svc.paths, max_candidates=1, llm_provider=fake)

    assert result["candidate_count"] == 1
    assert result["llm_judgment"]["candidate_input_count"] > result["candidate_count"]
    assert result["llm_judgment"]["truncated_kept_candidate_count"] > 0
    assert result["llm_judgment"]["truncated_kept_candidates"][0]["candidate_key"]
    assert fake.calls == result["llm_judgment"]["candidate_input_count"]


def test_gardener_tiers_reasoning_effort_by_candidate_signal(tmp_path: Path, monkeypatch) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    calls: list[str] = []

    class FakeProvider:
        name = "fake"
        model = "test"

        def __init__(self) -> None:
            self.reasoning_effort = "xhigh"
            self.timeout = 600

        def complete(self, prompt: str) -> str:
            calls.append(self.reasoning_effort)
            return json.dumps(
                {
                    "judgments": [
                        {
                            "candidate_key": "entity_merge:entities:entity_hightouch,entity_high_touch",
                            "decision": "keep",
                            "rationale": "Compact name match.",
                        },
                        {
                            "candidate_key": "entity_merge:entities:entity_sierra,entity_sierra_poc",
                            "decision": "keep",
                            "rationale": "Needs semantic review.",
                        },
                    ]
                }
            )

    def fake_get_cos_role_provider(paths, role, *, provider=None, llm_provider=None):
        return FakeProvider()

    monkeypatch.setattr(
        "pkm_brain.gardener.get_cos_role_provider",
        fake_get_cos_role_provider,
    )
    result = apply_gardener_judgment(
        [
            {
                "action_type": "entity_merge",
                "candidate_key": "entity_merge:entities:entity_hightouch,entity_high_touch",
                "risk_tier": "low",
                "merge_signal": "same_compact_name_or_alias",
                "score": 0.94,
            },
            {
                "action_type": "entity_merge",
                "candidate_key": "entity_merge:entities:entity_sierra,entity_sierra_poc",
                "risk_tier": "medium",
                "merge_signal": "name_containment",
                "score": 0.78,
            },
        ],
        [],
        {},
        paths=svc.paths,
        llm_provider=None,
        provider="codex",
    )

    assert sorted(calls) == ["low", "xhigh"]
    assert result["summary"]["effort_counts"] == {"low": 1, "xhigh": 1}


def test_gardener_rehome_candidate_has_destination_and_action_payload(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        insert_fact(
            conn,
            "fact_single",
            "The Tesla wall connector permit belongs with the home EV charging project.",
            page_hint="concepts/patio-ev-outlet.md",
            entity_key="project:home-ev-charging:electrical",
            section_hint="Electrical",
            source_ids=["document:ev-plan"],
        )
        insert_fact(
            conn,
            "fact_destination_a",
            "Home EV charging work includes the Tesla wall connector permit.",
            page_hint="projects/home-ev-charging.md",
            entity_key="project:home-ev-charging:electrical",
            section_hint="Electrical",
            source_ids=["document:ev-plan"],
        )
        insert_fact(
            conn,
            "fact_destination_b",
            "Home EV charging has a panel-load review before installation.",
            page_hint="projects/home-ev-charging.md",
            entity_key="project:home-ev-charging:electrical",
            section_hint="Risks",
            source_ids=["document:ev-load"],
        )

    result = generate_gardener_candidates(svc.paths)
    rehome = next(candidate for candidate in result["candidates"] if candidate["action_type"] == "rehome_fact")
    action = propose_gardener_action(svc.paths, rehome)

    assert rehome["fact_id"] == "fact_single"
    assert rehome["destination_page_hint"] == "projects/home-ev-charging.md"
    assert action["target_fact_ids"] == ["fact_single"]
    assert action["target_page_paths"] == [
        "concepts/patio-ev-outlet.md",
        "projects/home-ev-charging.md",
    ]
    assert action["evidence_json"]["payload"] == {
        "fact_id": "fact_single",
        "page_hint": "projects/home-ev-charging.md",
    }


def test_gardener_llm_judgment_rationale_is_persisted_on_action(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        insert_fact(
            conn,
            "fact_single",
            "The Tesla wall connector permit belongs with the home EV charging project.",
            page_hint="concepts/patio-ev-outlet.md",
            entity_key="project:home-ev-charging:electrical",
            section_hint="Electrical",
            source_ids=["document:ev-plan"],
        )
        insert_fact(
            conn,
            "fact_destination_a",
            "Home EV charging work includes the Tesla wall connector permit.",
            page_hint="projects/home-ev-charging.md",
            entity_key="project:home-ev-charging:electrical",
            section_hint="Electrical",
            source_ids=["document:ev-plan"],
        )
        insert_fact(
            conn,
            "fact_destination_b",
            "Home EV charging has a panel-load review before installation.",
            page_hint="projects/home-ev-charging.md",
            entity_key="project:home-ev-charging:electrical",
            section_hint="Risks",
            source_ids=["document:ev-load"],
        )

    class FakeProvider:
        name = "fake"
        model = "test"

        def complete(self, prompt: str) -> str:
            assert "rehome_fact" in prompt
            return json.dumps(
                {
                    "judgments": [
                        {
                            "candidate_key": (
                                "rehome_fact:concepts/patio-ev-outlet.md,"
                                "projects/home-ev-charging.md:fact_single"
                            ),
                            "decision": "keep",
                            "rationale": "Fact and destination share EV charging evidence.",
                            "risk_tier": "low",
                        }
                    ]
                }
            )

    result = generate_gardener_candidates(svc.paths, shadow=False, llm_provider=FakeProvider())
    action = next(action for action in result["actions"] if action["action_type"] == "rehome_fact")

    assert action["proposed_by"] == "gardener_llm"
    assert action["evidence_json"]["gardener_judgment"]["rationale"] == "Fact and destination share EV charging evidence."


def test_gardener_rehome_rejects_destination_contract_exclusion(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        insert_fact(
            conn,
            "fact_orchard_single",
            "Mango orchard irrigation sensors in Fresno need soil moisture calibration.",
            page_hint="concepts/mango-sensors.md",
            entity_key="project:mango-orchard:irrigation",
            section_hint="Irrigation",
            source_ids=["document:mango"],
        )
        for index in range(2):
            insert_fact(
                conn,
                f"fact_ev_{index}",
                f"Home EV charging permit item {index} belongs to the charger project.",
                page_hint="projects/home-ev-charging.md",
                entity_key="project:home-ev-charging:electrical",
                section_hint="Electrical",
                source_ids=[f"document:ev-{index}"],
            )
        insert_contract(
            conn,
            contract_id="contract_ev",
            page_hint="projects/home-ev-charging.md",
            canonical_entity="Home EV Charging",
            what_belongs_here="Tesla charger, electrical panel, and EV permitting facts.",
            what_does_not_belong_here="Mango orchard irrigation sensor facts.",
        )

    result = generate_gardener_candidates(svc.paths)

    assert not any(candidate["action_type"] == "rehome_fact" for candidate in result["candidates"])


def test_gardener_rehome_accepts_compatible_destination_contract(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        insert_fact(
            conn,
            "fact_ev_single",
            "The Tesla wall connector permit belongs with the home EV charging project.",
            page_hint="concepts/patio-ev-outlet.md",
            entity_key="project:home-ev-charging:electrical",
            section_hint="Electrical",
            source_ids=["document:ev-plan"],
        )
        for index in range(2):
            insert_fact(
                conn,
                f"fact_ev_destination_{index}",
                f"Home EV charging work includes Tesla wall connector permit item {index}.",
                page_hint="projects/home-ev-charging.md",
                entity_key="project:home-ev-charging:electrical",
                section_hint="Electrical",
                source_ids=[f"document:ev-{index}"],
            )
        insert_contract(
            conn,
            contract_id="contract_ev",
            page_hint="projects/home-ev-charging.md",
            canonical_entity="Home EV Charging",
            what_belongs_here="Tesla wall connector, EV charging, electrical panel, and permit facts.",
            what_does_not_belong_here="Mango orchard irrigation sensor facts.",
        )

    result = generate_gardener_candidates(svc.paths)
    rehome = next(candidate for candidate in result["candidates"] if candidate["action_type"] == "rehome_fact")

    assert rehome["fact_id"] == "fact_ev_single"
    assert rehome["destination_page_hint"] == "projects/home-ev-charging.md"
    assert rehome["contract_validation"]["valid"] is True


def test_gardener_flags_page_facts_that_violate_existing_contract(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        for index in range(2):
            insert_fact(
                conn,
                f"fact_mango_{index}",
                f"Mango orchard irrigation sensor item {index} needs calibration.",
                page_hint="projects/home-ev-charging.md",
                entity_key="project:mango-orchard:irrigation",
                section_hint="Irrigation",
                source_ids=[f"document:mango-{index}"],
            )
        insert_contract(
            conn,
            contract_id="contract_ev",
            page_hint="projects/home-ev-charging.md",
            canonical_entity="Home EV Charging",
            what_belongs_here="Tesla charger, electrical panel, and EV permitting facts.",
            what_does_not_belong_here="Mango orchard irrigation sensor facts.",
        )

    result = generate_gardener_candidates(svc.paths)
    contract = next(candidate for candidate in result["candidates"] if candidate["action_type"] == "edit_contract")

    assert contract["page_hints"] == ["projects/home-ev-charging.md"]
    assert contract["reason"] == "active facts do not conform to the current page contract"
    assert contract["contract_violations"][0]["reason"] == "fact content overlaps contract exclusions"


def test_gardener_flags_dense_pages_and_missing_contracts(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    sections = ["Pricing", "Technical", "Customers", "Risks", "Roadmap"]
    with connection(svc.paths.sqlite_path) as conn:
        for index, section in enumerate(sections):
            insert_fact(
                conn,
                f"fact_sprawl_{index}",
                f"Sprawling platform {section.lower()} item {index} needs a narrower home.",
                page_hint="projects/sprawling-platform.md",
                entity_key="project:sprawling-platform:summary",
                section_hint=section,
                source_ids=[f"document:sprawl-{index}"],
            )

    result = generate_gardener_candidates(svc.paths)
    split = next(candidate for candidate in result["candidates"] if candidate["action_type"] == "page_split")
    contract = next(candidate for candidate in result["candidates"] if candidate["action_type"] == "edit_contract")

    assert split["page_hints"] == ["projects/sprawling-platform.md"]
    assert set(split["section_counts"]) == set(sections)
    assert contract["contract"]["page_hint"] == "projects/sprawling-platform.md"
    assert contract["contract"]["id"].startswith("contract_gardener_")


def test_gardener_suppresses_recent_failed_candidate_keys(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        insert_fact(
            conn,
            "fact_alpha_left",
            "AlphaPay payment retry uses Stripe Checkout for renewal invoices.",
            page_hint="concepts/alpha-payment.md",
            entity_key="product:alphapay:billing",
            source_ids=["document:alpha-billing"],
        )
        insert_fact(
            conn,
            "fact_alpha_right",
            "AlphaPay payment retry uses Stripe Checkout for renewal invoices.",
            page_hint="concepts/alpha-payments.md",
            entity_key="product:alphapay:billing",
            source_ids=["document:alpha-billing"],
        )

    first = generate_gardener_candidates(svc.paths)
    merge = next(candidate for candidate in first["candidates"] if candidate["action_type"] == "page_merge")
    action = propose_gardener_action(svc.paths, merge)
    with connection(svc.paths.sqlite_path) as conn:
        conn.execute("UPDATE cos_actions SET status = 'failed' WHERE id = ?", (action["id"],))

    second = generate_gardener_candidates(svc.paths)

    assert merge["candidate_key"] not in {
        candidate["candidate_key"] for candidate in second["candidates"]
    }
