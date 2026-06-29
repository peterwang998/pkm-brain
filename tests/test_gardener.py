from __future__ import annotations

import json
from pathlib import Path

from pkm_brain.db import connection
from pkm_brain.contracts import insert_contract_direct
from pkm_brain.gardener import generate_gardener_candidates, propose_gardener_action
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

    assert result["llm_judgment"]["enabled"] is True
    assert result["llm_judgment"]["dropped_candidate_count"] == 1
    assert not any(candidate["action_type"] == "page_merge" for candidate in result["candidates"])


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
