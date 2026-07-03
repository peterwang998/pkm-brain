from __future__ import annotations

import json
from pathlib import Path

import pytest

from pkm_brain.cos_actions import apply_action, propose_action, revert_action
from pkm_brain.db import connection, loads
from pkm_brain.entities import normalize_entity_name, resolve_entity
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.wiki_facts import resolve_fact_groups


def service_for(tmp_path: Path) -> BrainService:
    return BrainService(BrainPaths.from_value(tmp_path / "brain"))


def test_entity_normalization_collapses_case_and_format_variants(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()

    assert normalize_entity_name("Unity Catalog") == normalize_entity_name("unity_catalog")
    assert normalize_entity_name("  Hightouch  ") == normalize_entity_name("hightouch")
    with connection(svc.paths.sqlite_path) as conn:
        first = resolve_entity(conn, "Sierra")
        second = resolve_entity(conn, "sierra")
        third = resolve_entity(conn, "Sierra")
        row = conn.execute("SELECT aliases FROM entities WHERE id = ?", (first.entity_id,)).fetchone()

    assert first.entity_id == second.entity_id == third.entity_id
    assert first.resolution_method == "created"
    assert second.resolution_method == "exact"
    assert loads(row["aliases"], []) == ["sierra"]


def test_entity_type_blocks_cross_kind_surface_match(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()

    with connection(svc.paths.sqlite_path) as conn:
        person = resolve_entity(conn, "Jordan", type_hint="person")
        organization = resolve_entity(conn, "Jordan", type_hint="organization")
        same_person = resolve_entity(conn, "jordan", type_hint="person")
        rows = conn.execute(
            """
            SELECT id, name, entity_type
            FROM entities
            WHERE name = 'Jordan'
            ORDER BY entity_type
            """
        ).fetchall()

    assert person.entity_id != organization.entity_id
    assert same_person.entity_id == person.entity_id
    assert {row["entity_type"] for row in rows} == {"organization", "person"}


def test_entity_resolver_llm_chooses_from_closed_candidates(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()

    with connection(svc.paths.sqlite_path) as conn:
        peter = resolve_entity(conn, "Peter", type_hint="person")
        peter_wang = resolve_entity(conn, "Peter Wang", type_hint="person")
        provider = FakeEntityResolverProvider(peter_wang.entity_id)
        resolved = resolve_entity(
            conn,
            "Peter",
            type_hint="person",
            paths=svc.paths,
            llm_provider=provider,
            context={"statement": "Peter Wang joined the interview loop."},
        )
        unknown = resolve_entity(
            conn,
            "Peter",
            type_hint="person",
            paths=svc.paths,
            llm_provider=FakeEntityResolverProvider("entity_missing"),
            context={"statement": "Peter stayed as the short-name entity."},
        )

    assert resolved.entity_id == peter_wang.entity_id
    assert resolved.resolution_method == "llm"
    assert unknown.entity_id == peter.entity_id
    assert provider.prompts and "Never invent an entity_id" in provider.prompts[0]


def test_fact_upsert_writes_primary_entity_link_across_page_routes(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()

    first = apply_fact(svc.paths, "fact_databricks", "career/databricks.md", "Peter Wang")
    second = apply_fact(svc.paths, "fact_google", "career/google.md", "Peter Wang")

    with connection(svc.paths.sqlite_path) as conn:
        facts = conn.execute(
            """
            SELECT id, entity_key, entity_id, page_hint
            FROM facts
            WHERE id IN (?, ?)
            ORDER BY id
            """,
            ("fact_databricks", "fact_google"),
        ).fetchall()
        links = conn.execute(
            """
            SELECT fact_id, entity_id, is_primary, mention_text, resolution_method
            FROM fact_entities
            WHERE fact_id IN (?, ?)
            ORDER BY fact_id
            """,
            ("fact_databricks", "fact_google"),
        ).fetchall()

    assert first["status"] == "applied"
    assert second["status"] == "applied"
    assert facts[0]["entity_id"] == facts[1]["entity_id"]
    assert facts[0]["entity_key"] != facts[1]["entity_key"]
    assert [link["is_primary"] for link in links] == [1, 1]
    assert {link["mention_text"] for link in links} == {"Peter Wang"}
    assert {link["resolution_method"] for link in links} == {"created", "exact"}


def test_fact_upsert_writes_secondary_entity_mentions_and_types(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    mentions = [
        {
            "surface": "Databricks",
            "entity_type": "organization",
            "mention_kind": "named",
            "is_primary": True,
            "mention_span": {"chunk_id": "chunk_source", "start": 0, "end": 10},
            "confidence": 0.9,
        },
        {
            "surface": "Hightouch",
            "entity_type": "organization",
            "mention_kind": "named",
            "is_primary": False,
            "mention_span": {"chunk_id": "chunk_source", "start": 18, "end": 27},
            "confidence": 0.8,
        },
    ]

    apply_fact(
        svc.paths,
        "fact_partnership",
        "career/databricks.md",
        "Databricks",
        entity_mentions=mentions,
    )

    with connection(svc.paths.sqlite_path) as conn:
        fact = conn.execute("SELECT entity_id FROM facts WHERE id = 'fact_partnership'").fetchone()
        links = conn.execute(
            """
            SELECT fe.fact_id, fe.entity_id, fe.is_primary, fe.mention_text,
                   fe.mention_span, fe.mention_kind, fe.resolution_method, e.name, e.entity_type
            FROM fact_entities fe
            JOIN entities e ON e.id = fe.entity_id
            WHERE fe.fact_id = 'fact_partnership'
            ORDER BY fe.is_primary DESC, e.name
            """
        ).fetchall()

    assert len(links) == 2
    assert links[0]["name"] == "Databricks"
    assert links[0]["is_primary"] == 1
    assert links[0]["mention_kind"] == "named"
    assert links[0]["entity_type"] == "organization"
    assert links[0]["entity_id"] == fact["entity_id"]
    assert links[1]["name"] == "Hightouch"
    assert links[1]["is_primary"] == 0
    assert links[1]["mention_kind"] == "named"
    assert links[1]["entity_type"] == "organization"
    assert loads(links[1]["mention_span"], {}) == {
        "chunk_id": "chunk_source",
        "start": 18,
        "end": 27,
    }


def test_fact_upsert_drops_generic_deictic_and_missing_kind_entities(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()

    apply_fact(
        svc.paths,
        "fact_generic_mentions",
        "career/team.md",
        "our team",
        entity_mentions=[
            {
                "surface": "our team",
                "entity_type": "organization",
                "mention_kind": "deictic",
                "is_primary": True,
            },
            {
                "surface": "engineers",
                "entity_type": "other",
                "mention_kind": "generic",
                "is_primary": False,
            },
        ],
    )
    apply_fact(
        svc.paths,
        "fact_missing_kind",
        "career/team.md",
        "Product Marketing",
        entity_mentions=[
            {
                "surface": "Product Marketing",
                "entity_type": "organization",
                "is_primary": True,
            }
        ],
    )

    with connection(svc.paths.sqlite_path) as conn:
        facts = conn.execute(
            "SELECT id, statement, entity_id FROM facts ORDER BY id"
        ).fetchall()
        link_count = conn.execute("SELECT COUNT(*) FROM fact_entities").fetchone()[0]
        entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]

    assert {row["id"]: row["entity_id"] for row in facts} == {
        "fact_generic_mentions": None,
        "fact_missing_kind": None,
    }
    assert "our team has a fact" in facts[0]["statement"]
    assert link_count == 0
    assert entity_count == 0


def test_fact_upsert_admits_concepts_only_when_configured(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()

    default_mentions = [
        {
            "surface": "Databricks",
            "entity_type": "organization",
            "mention_kind": "named",
            "is_primary": True,
        },
        {
            "surface": "Lineage",
            "entity_type": "concept",
            "mention_kind": "concept",
            "is_primary": False,
        },
    ]
    apply_fact(
        svc.paths,
        "fact_concept_default",
        "companies/databricks.md",
        "Databricks",
        entity_mentions=default_mentions,
    )

    (svc.paths.config_local / "cos_llm.yaml").write_text(
        "entity:\n"
        "  admit_kinds:\n"
        "    - named\n"
        "    - concept\n",
        encoding="utf-8",
    )
    concept_mentions = [
        {**default_mentions[0], "surface": "Hightouch"},
        default_mentions[1],
    ]
    apply_fact(
        svc.paths,
        "fact_concept_enabled",
        "companies/hightouch.md",
        "Hightouch",
        entity_mentions=concept_mentions,
    )

    with connection(svc.paths.sqlite_path) as conn:
        default_links = conn.execute(
            "SELECT mention_text FROM fact_entities WHERE fact_id = 'fact_concept_default'"
        ).fetchall()
        enabled_links = conn.execute(
            """
            SELECT fe.mention_text, fe.mention_kind, e.entity_type
            FROM fact_entities fe
            JOIN entities e ON e.id = fe.entity_id
            WHERE fe.fact_id = 'fact_concept_enabled'
            ORDER BY fe.is_primary DESC, fe.mention_text
            """
        ).fetchall()

    assert [row["mention_text"] for row in default_links] == ["Databricks"]
    assert {row["mention_text"] for row in enabled_links} == {"Hightouch", "Lineage"}
    assert {
        (row["mention_text"], row["mention_kind"], row["entity_type"])
        for row in enabled_links
    } == {
        ("Hightouch", "named", "organization"),
        ("Lineage", "concept", "concept"),
    }


def test_fact_upsert_without_entity_mentions_does_not_use_route_entity_key(
    tmp_path: Path,
) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    fact = {
        "id": "fact_without_mentions",
        "statement": "A route-only fact should not mint an entity.",
        "entity_key": "concepts:concepts-extracted-facts:summary",
        "page_hint": "concepts/extracted-facts.md",
        "section_hint": "Summary",
        "source_ids": ["document:test"],
        "observed_at": "2026-06-30T00:00:00+00:00",
        "confidence": 0.9,
        "status": "active",
        "metadata": {"source": "source_to_facts_extraction"},
        "source_spans": [],
        "extraction_method": "llm",
        "truth_confidence": 0.9,
    }

    apply_action(
        svc.paths,
        propose_action(
            svc.paths,
            "fact_upsert",
            action_payload={"fact": fact},
            proposed_by="test",
            risk_tier="medium",
        )["id"],
    )

    with connection(svc.paths.sqlite_path) as conn:
        stored = conn.execute(
            "SELECT entity_id FROM facts WHERE id = 'fact_without_mentions'"
        ).fetchone()
        link_count = conn.execute("SELECT COUNT(*) FROM fact_entities").fetchone()[0]
        entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]

    assert stored["entity_id"] is None
    assert link_count == 0
    assert entity_count == 0


def test_entity_merge_repoints_links_denorm_and_aliases(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    apply_fact(
        svc.paths,
        "fact_hightouch",
        "companies/hightouch.md",
        "Hightouch",
        entity_mentions=[
            {
                "surface": "Hightouch",
                "entity_type": "organization",
                "mention_kind": "named",
                "is_primary": True,
            }
        ],
    )
    apply_fact(
        svc.paths,
        "fact_high_touch",
        "companies/high-touch.md",
        "High Touch",
        entity_mentions=[
            {
                "surface": "High Touch",
                "entity_type": "organization",
                "mention_kind": "named",
                "is_primary": True,
            }
        ],
    )
    with connection(svc.paths.sqlite_path) as conn:
        canonical_id = entity_id_by_name(conn, "Hightouch")
        source_id = entity_id_by_name(conn, "High Touch")

    action = apply_action(
        svc.paths,
        propose_action(
            svc.paths,
            "entity_merge",
            action_payload={
                "canonical_entity_id": canonical_id,
                "merged_entity_ids": [source_id],
            },
            action_features={
                "affected_fact_count": 1,
                "merged_entity_count": 1,
            },
            risk_tier="medium",
        )["id"],
    )

    with connection(svc.paths.sqlite_path) as conn:
        source = conn.execute(
            "SELECT status, merged_into FROM entities WHERE id = ?",
            (source_id,),
        ).fetchone()
        canonical = conn.execute(
            "SELECT aliases FROM entities WHERE id = ?",
            (canonical_id,),
        ).fetchone()
        fact = conn.execute(
            "SELECT entity_id FROM facts WHERE id = 'fact_high_touch'"
        ).fetchone()
        link = conn.execute(
            """
            SELECT entity_id
            FROM fact_entities
            WHERE fact_id = 'fact_high_touch' AND is_primary = 1
            """
        ).fetchone()

    assert source["status"] == "merged"
    assert source["merged_into"] == canonical_id
    assert fact["entity_id"] == canonical_id
    assert link["entity_id"] == canonical_id
    assert "High Touch" in loads(canonical["aliases"], [])
    assert "restore_entities" in action["inverse_action_json"]
    assert action["target_fact_ids"] == ["fact_high_touch"]

    revert_action(svc.paths, action["id"])

    with connection(svc.paths.sqlite_path) as conn:
        restored_source = conn.execute(
            "SELECT status, merged_into FROM entities WHERE id = ?",
            (source_id,),
        ).fetchone()
        restored_fact = conn.execute(
            "SELECT entity_id FROM facts WHERE id = 'fact_high_touch'"
        ).fetchone()
        restored_link = conn.execute(
            """
            SELECT entity_id
            FROM fact_entities
            WHERE fact_id = 'fact_high_touch' AND is_primary = 1
            """
        ).fetchone()

    assert restored_source["status"] == "active"
    assert restored_source["merged_into"] is None
    assert restored_fact["entity_id"] == source_id
    assert restored_link["entity_id"] == source_id


def test_entity_split_restores_prior_links_and_entity_status(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    apply_fact(
        svc.paths,
        "fact_hightouch",
        "companies/hightouch.md",
        "Hightouch",
        entity_mentions=[
            {
                "surface": "Hightouch",
                "entity_type": "organization",
                "mention_kind": "named",
                "is_primary": True,
            }
        ],
    )
    apply_fact(
        svc.paths,
        "fact_high_touch",
        "companies/high-touch.md",
        "High Touch",
        entity_mentions=[
            {
                "surface": "High Touch",
                "entity_type": "organization",
                "mention_kind": "named",
                "is_primary": True,
            }
        ],
    )
    with connection(svc.paths.sqlite_path) as conn:
        canonical_id = entity_id_by_name(conn, "Hightouch")
        source_id = entity_id_by_name(conn, "High Touch")
    merged = apply_action(
        svc.paths,
        propose_action(
            svc.paths,
            "entity_merge",
            action_payload={
                "canonical_entity_id": canonical_id,
                "merged_entity_ids": [source_id],
            },
            risk_tier="medium",
        )["id"],
    )

    apply_action(
        svc.paths,
        propose_action(
            svc.paths,
            "entity_split",
            action_payload={"merge_inverse": merged["inverse_action_json"]},
            risk_tier="medium",
        )["id"],
    )

    with connection(svc.paths.sqlite_path) as conn:
        source = conn.execute(
            "SELECT status, merged_into FROM entities WHERE id = ?",
            (source_id,),
        ).fetchone()
        canonical = conn.execute(
            "SELECT aliases FROM entities WHERE id = ?",
            (canonical_id,),
        ).fetchone()
        fact = conn.execute(
            "SELECT entity_id FROM facts WHERE id = 'fact_high_touch'"
        ).fetchone()
        link = conn.execute(
            """
            SELECT entity_id
            FROM fact_entities
            WHERE fact_id = 'fact_high_touch' AND is_primary = 1
            """
        ).fetchone()

    assert source["status"] == "active"
    assert source["merged_into"] is None
    assert fact["entity_id"] == source_id
    assert link["entity_id"] == source_id
    assert "High Touch" not in loads(canonical["aliases"], [])


def test_entity_merge_hard_blocks_type_mismatch(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    apply_fact(
        svc.paths,
        "fact_jordan_person",
        "people/jordan.md",
        "Jordan",
        entity_mentions=[
            {
                "surface": "Jordan",
                "entity_type": "person",
                "mention_kind": "named",
                "is_primary": True,
            }
        ],
    )
    apply_fact(
        svc.paths,
        "fact_jordan_org",
        "companies/jordan.md",
        "Jordan",
        entity_mentions=[
            {
                "surface": "Jordan",
                "entity_type": "organization",
                "mention_kind": "named",
                "is_primary": True,
            }
        ],
    )
    with connection(svc.paths.sqlite_path) as conn:
        person_id = entity_id_by_type(conn, "person")
        org_id = entity_id_by_type(conn, "organization")

    action = propose_action(
        svc.paths,
        "entity_merge",
        action_payload={
            "canonical_entity_id": person_id,
            "merged_entity_ids": [org_id],
        },
        risk_tier="medium",
    )
    with pytest.raises(ValueError, match="type mismatch"):
        apply_action(svc.paths, action["id"])

    with connection(svc.paths.sqlite_path) as conn:
        org = conn.execute(
            "SELECT status, merged_into FROM entities WHERE id = ?",
            (org_id,),
        ).fetchone()
        fact_entities = {
            row["id"]: row["entity_id"]
            for row in conn.execute("SELECT id, entity_id FROM facts ORDER BY id")
        }

    assert org["status"] == "active"
    assert org["merged_into"] is None
    assert fact_entities == {
        "fact_jordan_org": org_id,
        "fact_jordan_person": person_id,
    }


def test_resolve_fact_groups_uses_entity_id_not_page_section_bucket(tmp_path: Path) -> None:
    svc = service_for(tmp_path)
    svc.init_workspace()
    with connection(svc.paths.sqlite_path) as conn:
        alpha = resolve_entity(conn, "Alpha")
        beta = resolve_entity(conn, "Beta")
        insert_replacement_fact(
            conn,
            "fact_alpha",
            "Alpha is available.",
            entity_id=alpha.entity_id,
        )
        insert_replacement_fact(
            conn,
            "fact_beta",
            "Alpha is not available.",
            entity_id=beta.entity_id,
        )

    result = resolve_fact_groups(svc.paths, ["concepts:shared:status"])

    assert result["conflict_group_ids"] == []
    with connection(svc.paths.sqlite_path) as conn:
        statuses = {
            row["id"]: row["status"]
            for row in conn.execute("SELECT id, status FROM facts ORDER BY id")
        }
        question_count = conn.execute(
            "SELECT COUNT(*) FROM open_questions WHERE kind = 'conflict'"
        ).fetchone()[0]
    assert statuses == {"fact_alpha": "active", "fact_beta": "active"}
    assert question_count == 0


def apply_fact(
    paths: BrainPaths,
    fact_id: str,
    page_hint: str,
    model_entity_key: str,
    *,
    entity_mentions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    fact = {
        "id": fact_id,
        "statement": f"{model_entity_key} has a fact on {page_hint}.",
        "entity_key": page_hint.removesuffix(".md").replace("/", ":"),
        "page_hint": page_hint,
        "section_hint": "Summary",
        "source_ids": ["document:test"],
        "observed_at": "2026-06-28T00:00:00+00:00",
        "confidence": 0.9,
        "status": "active",
        "metadata": {
            "model_entity_key": model_entity_key,
            **({"model_entity_mentions": entity_mentions} if entity_mentions else {}),
        },
        "source_spans": [],
        "extraction_method": "llm",
        "truth_confidence": 0.9,
    }
    if entity_mentions:
        fact["entity_mentions"] = entity_mentions
    return apply_action(
        paths,
        propose_action(
            paths,
            "fact_upsert",
            action_payload={"fact": fact},
            proposed_by="test",
            risk_tier="medium",
        )["id"],
    )


class FakeEntityResolverProvider:
    name = "fake-resolver"
    model = "fake-resolver-model"

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps(
            {
                "decision": "existing",
                "entity_id": self.entity_id,
                "rationale": "test resolver choice",
            }
        )


def entity_id_by_name(conn: object, name: str) -> str:
    row = conn.execute("SELECT id FROM entities WHERE name = ?", (name,)).fetchone()
    assert row is not None
    return str(row["id"])


def entity_id_by_type(conn: object, entity_type: str) -> str:
    row = conn.execute(
        "SELECT id FROM entities WHERE entity_type = ?",
        (entity_type,),
    ).fetchone()
    assert row is not None
    return str(row["id"])


def insert_replacement_fact(
    conn: object,
    fact_id: str,
    statement: str,
    *,
    entity_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO facts(
          id, statement, entity_key, entity_id, page_hint, section_hint,
          source_ids, observed_at, confidence, status, metadata, created_at,
          truth_confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fact_id,
            statement,
            "concepts:shared:status",
            entity_id,
            "concepts/shared.md",
            "Status",
            json.dumps(["document:test"]),
            "2026-06-28T00:00:00+00:00",
            0.9,
            "active",
            json.dumps({"operation": "replace_section"}),
            "2026-06-28T00:00:00+00:00",
            0.9,
        ),
    )
